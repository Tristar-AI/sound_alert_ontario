"""Threaded DatabaseReader reliability: observable start/poll/close behavior."""

import threading
import time

import pytest

import get_latest_database_values as database


class OrdinarySQLError(Exception):
    pgcode = "42601"


class FakeSource:
    """Injectable defect source with a scripted read_defect behavior."""

    def __init__(self, behavior):
        self._behavior = behavior
        self.calls = []
        self.closed = False

    def read_defect(self):
        self.calls.append(threading.current_thread())
        return self._behavior()

    def close(self):
        self.closed = True


class PostgresScript:
    """Scripted connector: each query pops one outcome; counts connections."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.connects = 0

    def __call__(self, **config):
        self.connects += 1
        script = self

        class Connection:
            closed = 0
            autocommit = False

            def close(self):
                self.closed = 1

            def cursor(self):
                class Cursor:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    def execute(self, query, params):
                        kind, payload = script.outcomes.pop(0)
                        if kind == "raise":
                            raise payload
                        self._row = payload

                    def fetchone(self):
                        return self._row

                return Cursor()

        return Connection()


def make_reader(source, **kwargs):
    return database.DatabaseReader(source, **kwargs)


def collect(reader, timeout=4.0):
    updates = []
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        updates.extend(reader.poll())
        if not reader.busy:
            return updates
        time.sleep(0.01)
    raise AssertionError("reader did not finish within the test deadline")


def wait_until(predicate, timeout=4.0):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_successive_starts_each_deliver_defect_value():
    """Run two database checks. Each one should report what it saw."""
    values = iter([False, True])
    reader = make_reader(FakeSource(lambda: next(values)), request_timeout=5)
    try:
        for expected in (False, True):
            assert reader.start()
            updates = collect(reader)
            assert [(item.kind, item.value, item.error) for item in updates] == [("defect", expected, None)]
    finally:
        reader.close()


def test_second_start_while_busy_is_rejected_without_queueing():
    """Start one slow check. A second start should fail."""
    release = threading.Event()
    source = FakeSource(lambda: (release.wait(10), True)[1])
    reader = make_reader(source, request_timeout=10)
    try:
        assert reader.start()
        assert not reader.start()
        assert wait_until(lambda: len(source.calls) == 1)
        release.set()
        updates = collect(reader)
        assert [(item.kind, item.value) for item in updates] == [("defect", True)]
        assert len(source.calls) == 1
        assert not reader.busy
    finally:
        release.set()
        reader.close()


def test_ordinary_sql_error_is_reported_and_worker_is_reused():
    """Make one check fail with bad SQL. The next check should still work."""
    outcomes = [database.DefectStatusQueryError("syntax error"), True]

    def behavior():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader = make_reader(FakeSource(behavior), request_timeout=5)
    try:
        assert reader.start()
        first = collect(reader)
        assert [item.kind for item in first] == ["defect"]
        assert first[0].error is not None

        assert reader.start()
        second = collect(reader)
        assert [(item.kind, item.value) for item in second] == [("defect", True)]
        assert second[0].error is None
    finally:
        reader.close()


def test_transport_failure_is_reported_then_next_start_recovers():
    """Break the first check. The next check should work after a short wait."""
    outcomes = [database.DefectStatusUnavailable("connection lost"), True]

    def behavior():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader = make_reader(FakeSource(behavior), request_timeout=5, retry_interval=0.3)
    try:
        assert reader.start()
        assert [item.kind for item in collect(reader)] == ["error"]

        time.sleep(0.5)
        assert reader.start()
        second = collect(reader)
        assert [(item.kind, item.value) for item in second] == [("defect", True)]
        assert second[0].error is None
    finally:
        reader.close()


def test_deadline_reports_error_and_bars_replacement_beside_retired_thread():
    """Let a check take too long. Another check should not start."""
    # Short deadline/cooldown; the blocked query outlives both, so any restart
    # while it is still running would start a second worker beside it.
    release = threading.Event()
    source = FakeSource(lambda: (release.wait(10), True)[1])
    reader = make_reader(source, request_timeout=0.3, retry_interval=0.3)
    try:
        assert reader.start()
        assert [item.kind for item in collect(reader)] == ["error"]
        assert not reader.busy
        assert wait_until(lambda: bool(source.calls))
        assert source.calls[0].is_alive()

        time.sleep(0.5)
        assert not reader.busy
        assert not reader.start()
        assert len(source.calls) == 1
    finally:
        release.set()
        reader.close()


def test_reply_completed_after_deadline_is_rejected():
    """Let one check run too long. Its late answer should be thrown away."""
    release = threading.Event()
    source = FakeSource(
        lambda: (release.wait(10), True)[1] if len(source.calls) == 1 else False
    )
    reader = make_reader(source, request_timeout=0.3, retry_interval=0.2)
    try:
        assert reader.start()
        assert [item.kind for item in collect(reader)] == ["error"]

        release.set()
        seen = []

        def _ready():
            seen.extend(reader.poll())
            return reader.start()

        assert wait_until(_ready, timeout=4.0)
        second = collect(reader)
        assert [(item.kind, item.value) for item in second] == [("defect", False)]
        assert not [u for u in seen if u.kind == "defect" and u.value is True]
    finally:
        release.set()
        reader.close()


def test_cooldown_rejects_immediate_restart_then_allows_retry():
    """Fail the first check. A later retry should work."""
    outcomes = [database.DefectStatusUnavailable("connection refused"), True]

    def behavior():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader = make_reader(FakeSource(behavior), request_timeout=2, retry_interval=0.3)
    try:
        assert reader.start()
        assert [item.kind for item in collect(reader)] == ["error"]
        assert not reader.start()
        time.sleep(0.5)
        assert reader.start()
        assert [(item.kind, item.value) for item in collect(reader)] == [("defect", True)]
    finally:
        reader.close()


def test_close_returns_promptly_with_blocked_query_and_bars_restart():
    """Close while a check is stuck. It should finish fast and stay closed."""
    release = threading.Event()
    source = FakeSource(lambda: (release.wait(10), True)[1])
    reader = make_reader(source, request_timeout=10)
    try:
        assert reader.start()
        assert wait_until(lambda: len(source.calls) == 1)

        started = time.monotonic()
        reader.close()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert not reader.start()
        assert len(source.calls) == 1
        assert source.calls[0].is_alive()
    finally:
        release.set()
        reader.close()


def test_postgres_source_reuses_healthy_connection():
    """Read twice through a fake connector. Both values should arrive on one connection."""
    script = PostgresScript([("row", (False,)), ("row", (True,))])
    source = database.PostgresDefectStatusSource(
        {"host": "db.invalid"}, 1, 2, 3, connect=script
    )
    try:
        assert source.read_defect() is False
        assert source.read_defect() is True
        assert script.connects == 1
    finally:
        source.close()


def test_postgres_source_keeps_connection_after_ordinary_error_but_reconnects_after_transport():
    """Fail with bad SQL, then with a dropped connection. Only the drop should reconnect."""
    script = PostgresScript(
        [
            ("raise", OrdinarySQLError("syntax error")),
            ("row", (False,)),
            ("raise", OSError("connection lost")),
            ("row", (True,)),
        ]
    )
    source = database.PostgresDefectStatusSource(
        {"host": "db.invalid"}, 1, 2, 3, connect=script
    )
    try:
        with pytest.raises(database.DefectStatusQueryError):
            source.read_defect()
        assert source.read_defect() is False
        assert script.connects == 1

        with pytest.raises(database.DefectStatusUnavailable):
            source.read_defect()
        assert source.read_defect() is True
        assert script.connects == 2
    finally:
        source.close()
