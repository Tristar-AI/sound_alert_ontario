"""Monitor state transitions with no database or audio hardware access."""

from unittest.mock import Mock

import pytest

import monitor
from get_latest_database_values import DatabaseUpdate


class FakeSpeaker:
    def __init__(self):
        self.playing = False

    def play_sound(self):
        self.playing = True

    def stop_all(self):
        self.playing = False


@pytest.fixture
def live_controller(monkeypatch):
    speaker = FakeSpeaker()
    reader = Mock(busy=False)
    reader.poll.return_value = []
    monkeypatch.setattr(monitor, "TESTING", None)
    monkeypatch.setattr(monitor, "SpeakerHandler", Mock(return_value=speaker))
    monkeypatch.setattr(monitor, "DatabaseReader", Mock(return_value=reader))
    ctl = monitor.SoundController(1, 2, 3, "unused")
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
    reader.poll.side_effect = [[], [DatabaseUpdate("error", error="connection lost")]]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [False, False]


def test_sounding_alarm_restarts_while_next_query_pends(monkeypatch, live_controller):
    """Sound the alarm first. It should restart even while waiting."""
    ctl, speaker, reader = live_controller
    calls = {"count": 0}

    def pending_poll():
        calls["count"] += 1
        if calls["count"] == 1:
            reader.busy = False
            return [DatabaseUpdate("defect", True)]
        reader.busy = True
        return []

    reader.poll.side_effect = lambda: pending_poll()

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
    reader.poll.side_effect = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("error", error="connection lost")],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, True]


def test_sounding_alarm_persists_through_restart_cooldown(monkeypatch, live_controller):
    """Sound the alarm first. It should keep sounding during a short wait."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.poll.side_effect = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("error", error="connection lost")],
    ]
    reader.start.return_value = False

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, True]


def test_silence_persists_through_query_error_after_clear(monkeypatch, live_controller):
    """Start quiet with no problem. It should stay quiet after a bad read."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.poll.side_effect = [
        [DatabaseUpdate("defect", False)],
        [DatabaseUpdate("error", error="connection lost")],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [False, False]


def test_alarm_clears_on_later_successful_read(monkeypatch, live_controller):
    """Sound the alarm first. It should stop when the problem is gone."""
    ctl, speaker, reader = live_controller
    reader.busy = False
    reader.poll.side_effect = [
        [DatabaseUpdate("defect", True)],
        [DatabaseUpdate("defect", False)],
    ]

    states = drive_monitor(monkeypatch, ctl, speaker, 2)

    assert states == [True, False]


def test_testing_true_sounds_without_database(monkeypatch):
    """Use test mode turned on. The speaker should sound."""
    speaker = FakeSpeaker()
    monkeypatch.setattr(monitor, "TESTING", True)
    monkeypatch.setattr(monitor, "SpeakerHandler", Mock(return_value=speaker))
    monkeypatch.setattr(monitor, "DatabaseReader", Mock(side_effect=AssertionError("database access")))
    ctl = monitor.SoundController(1, 2, 3, "unused")
    try:
        states = drive_monitor(monkeypatch, ctl, speaker, 1)

        assert states == [True]
    finally:
        ctl.close()


def test_testing_false_stays_silent_without_database(monkeypatch):
    """Use test mode turned off. The speaker should stay quiet."""
    speaker = FakeSpeaker()
    monkeypatch.setattr(monitor, "TESTING", False)
    monkeypatch.setattr(monitor, "SpeakerHandler", Mock(return_value=speaker))
    monkeypatch.setattr(monitor, "DatabaseReader", Mock(side_effect=AssertionError("database access")))
    ctl = monitor.SoundController(1, 2, 3, "unused")
    try:
        states = drive_monitor(monkeypatch, ctl, speaker, 1)

        assert states == [False]
    finally:
        ctl.close()


def test_speaker_stop_failure_still_closes_database(monkeypatch, live_controller):
    """Make the speaker fail to stop. The database should still close."""
    ctl, speaker, reader = live_controller
    monkeypatch.setattr(speaker, "stop_all", Mock(side_effect=RuntimeError("stop failed")))

    ctl.close()
    ctl.close()

    reader.close.assert_called_once()


def test_database_close_failure_still_stops_speaker(live_controller):
    """Make the database fail to close. The speaker should still stop."""
    ctl, speaker, reader = live_controller
    speaker.playing = True
    reader.close.side_effect = RuntimeError("close failed")

    ctl.close()

    assert not speaker.playing


def test_loop_error_stops_speaker_and_closes_database(live_controller):
    """The monitor hits an error. The speaker should stop. The database should close."""
    ctl, speaker, reader = live_controller
    speaker.playing = True
    reader.poll.side_effect = RuntimeError("loop failed")

    with pytest.raises(RuntimeError, match="loop failed"):
        ctl.monitor_continuous()

    assert not speaker.playing
    reader.close.assert_called_once()


def test_closed_controller_ignores_further_monitoring(monkeypatch, live_controller):
    """Close first, then watch again. Nothing more should happen."""
    ctl, speaker, reader = live_controller
    ctl.close()
    monkeypatch.setattr(monitor.time, "sleep", Mock(side_effect=AssertionError("loop resumed")))

    ctl.monitor_continuous()

    reader.poll.assert_not_called()
    assert not speaker.playing
