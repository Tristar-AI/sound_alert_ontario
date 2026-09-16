"""Monitor state transitions with no database or audio hardware access."""

import pytest

import monitor
from get_latest_database_values import DatabaseUpdate
from test_monitor_manual import StaticDefectReader, _testing_override


class FakeSpeaker:
    def __init__(self):
        self.playing = False
        self.play_calls = 0
        self.stop_calls = 0
        self.stop_error = None

    def play_sound(self):
        self.play_calls += 1
        self.playing = True

    def stop_all(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.playing = False


class FakeReader:
    """Behavioral DefectReader double: scripted poll replies plus busy flag."""

    def __init__(self, script=(), busy=False):
        # Each script entry is a poll reply (list of DatabaseUpdate),
        # an exception to raise, or a zero-arg callable returning a reply.
        self.script = list(script)
        self.busy = busy
        self.start_value = True
        self.start_calls = 0
        self.poll_calls = 0
        self.close_calls = 0
        self.closed = False
        self.close_error = None

    def poll(self):
        self.poll_calls += 1
        if self.script:
            nxt = self.script.pop(0)
            if callable(nxt):
                return nxt()
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt
        return []

    def start(self):
        self.start_calls += 1
        return self.start_value

    def close(self):
        self.close_calls += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def live_controller():
    speaker = FakeSpeaker()
    reader = FakeReader(busy=False)
    ctl = monitor.SoundController(reader=reader, speaker=speaker)
    yield ctl, speaker, reader
    ctl.close()


def drive_monitor(monkeypatch, ctl, speaker, cycles):
    """Run monitor_continuous for a fixed number of cycles.

    Each sleep marks one completed cycle: record the observable speaker
    state, then close the controller so the loop exits deterministically.
    """
    states = []

    def fake_sleep(_):
        states.append(speaker.playing)
        if len(states) >= cycles:
            ctl.close()

    monkeypatch.setattr(monitor.time, "sleep", fake_sleep)
    ctl.monitor_continuous()
    return states


def test_startup_outage_keeps_speaker_silent(monkeypatch, live_controller):
    """Start while the database is down. The speaker should stay quiet."""
    ctl, speaker, reader = live_controller
    reader.busy = True
    reader.script = [[], [DatabaseUpdate("error", error="connection lost")]]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [False, False]


def test_sounding_alarm_restarts_while_next_query_pends(monkeypatch, live_controller):
    """Sound the alarm first. It should restart even while waiting."""
    ctl, speaker, reader = live_controller

    def first():
        reader.busy = False
        return [DatabaseUpdate("defect", True)]

    def second():
        reader.busy = True
        return []

    reader.script = [first, second]

    # The player dies locally after the first cycle; the next cycle must
    # restart it even though the database query is still pending.
    states = []
    original_close = ctl.close

    def fake_sleep(_):
        states.append(speaker.playing)
        if len(states) == 1:
            speaker.stop_all()
        if len(states) >= 2:
            original_close()

    monkeypatch.setattr(monitor.time, "sleep", fake_sleep)
    ctl.monitor_continuous()

    assert states == [True, True]


def test_sounding_alarm_persists_through_query_error(monkeypatch, live_controller):
    """Sound the alarm first. It should keep sounding after a bad read."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.script = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("error", error="connection lost")],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, True]


def test_sounding_alarm_persists_through_restart_cooldown(monkeypatch, live_controller):
    """Sound the alarm first. It should keep sounding during a short wait."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.script = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("error", error="connection lost")],
    ]
    reader.start_value = False

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, True]


def test_silence_persists_through_query_error_after_clear(monkeypatch, live_controller):
    """Start quiet with no problem. It should stay quiet after a bad read."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.script = [
        [DatabaseUpdate("defect", False)],
        [DatabaseUpdate("error", error="connection lost")],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [False, False]


def test_alarm_clears_on_later_successful_read(monkeypatch, live_controller):
    """Sound the alarm first. It should stop when the problem is gone."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.script = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("defect", False)],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, False]


def test_testing_true_sounds_without_database(monkeypatch):
    """Use a fixed true source. The speaker should sound with no database."""
    speaker = FakeSpeaker()
    reader = StaticDefectReader(True)
    ctl = monitor.SoundController(reader=reader, speaker=speaker)
    try:
        states = drive_monitor(monkeypatch, ctl, speaker, 1)

        assert states == [True]
    finally:
        ctl.close()


def test_testing_false_stays_silent_without_database(monkeypatch):
    """Use a fixed false source. The speaker should stay quiet with no database."""
    speaker = FakeSpeaker()
    reader = StaticDefectReader(False)
    ctl = monitor.SoundController(reader=reader, speaker=speaker)
    try:
        states = drive_monitor(monkeypatch, ctl, speaker, 1)

        assert states == [False]
    finally:
        ctl.close()


def test_speaker_stop_failure_still_closes_reader(live_controller):
    """Make the speaker fail to stop. The reader should still close."""
    ctl, speaker, reader = live_controller
    speaker.stop_error = RuntimeError("stop failed")

    ctl.close()
    ctl.close()

    assert reader.closed
    assert reader.close_calls == 1


def test_reader_close_failure_still_stops_speaker(live_controller):
    """Make the reader fail to close. The speaker should still stop."""
    ctl, speaker, reader = live_controller
    speaker.playing = True
    reader.close_error = RuntimeError("close failed")

    ctl.close()

    assert not speaker.playing


def test_loop_error_stops_speaker_and_closes_reader(live_controller):
    """The monitor hits an error. The speaker should stop. The reader should close."""
    ctl, speaker, reader = live_controller
    speaker.playing = True
    reader.script = [RuntimeError("loop failed")]

    with pytest.raises(RuntimeError, match="loop failed"):
        ctl.monitor_continuous()

    assert not speaker.playing
    assert reader.closed
    assert reader.close_calls == 1


def test_closed_controller_ignores_further_monitoring(monkeypatch, live_controller):
    """Close first, then watch again. Nothing more should happen."""
    ctl, speaker, reader = live_controller
    ctl.close()
    polls_before = reader.poll_calls

    def fail_sleep(_):
        raise AssertionError("loop resumed")

    monkeypatch.setattr(monitor.time, "sleep", fail_sleep)

    ctl.monitor_continuous()

    assert reader.poll_calls == polls_before
    assert not speaker.playing


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
    ],
)
def test_testing_override_parsing(raw, expected):
    """Raw TESTING text should map to a fixed override or database mode."""
    assert _testing_override(raw) == expected


def test_testing_override_rejects_unknown():
    """An unrecognized TESTING value should fail fast at composition time."""
    with pytest.raises(ValueError, match="TESTING"):
        _testing_override("maybe")


def test_static_reader_close_stops_fixed_updates():
    """Closing the fixed source should stop further fixed updates."""
    reader = StaticDefectReader(True)
    try:
        assert reader.poll() != []
    finally:
        reader.close()
    assert reader.poll() == []
