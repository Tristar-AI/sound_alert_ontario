"""Threaded DatabaseReader reliability: observable start/poll/close behavior."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import get_latest_database_values as database

CONFIG = {"host": "db.invalid", "database": "test", "user": "test", "password": "test"}


class OrdinarySQLError(Exception):
    pgcode = "42601"


def make_reader(**kwargs):
    return database.DatabaseReader(CONFIG, 1, 2, 3, **kwargs)


def open_connection():
    return SimpleNamespace(closed=0, close=lambda: None)


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


def test_successive_queries_reuse_the_same_connection():
    reader = make_reader(request_timeout=5)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()) as fake_connect, \
         patch.object(database, "get_defect_status", return_value=False):
        try:
            for _ in range(2):
                assert reader.start()
                updates = collect(reader)
                assert [(item.kind, item.value, item.error) for item in updates] == [("defect", False, None)]
            assert fake_connect.call_count == 1
        finally:
            reader.close()


def test_second_start_while_busy_is_rejected_without_queueing():
    release = threading.Event()
    calls = []

    def fake_status(*args, **kwargs):
        calls.append(1)
        release.wait(10)
        return True

    reader = make_reader(request_timeout=10)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()), \
         patch.object(database, "get_defect_status", side_effect=fake_status):
        try:
            assert reader.start()
            assert not reader.start()
            assert wait_until(lambda: len(calls) == 1)
            release.set()
            updates = collect(reader)
            assert [(item.kind, item.value) for item in updates] == [("defect", True)]
            assert len(calls) == 1
            assert not reader.busy
        finally:
            release.set()
            reader.close()


def test_ordinary_sql_error_is_reported_and_worker_is_reused():
    outcomes = [OrdinarySQLError("syntax error"), True]

    def fake_status(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader = make_reader(request_timeout=5)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()) as fake_connect, \
         patch.object(database, "get_defect_status", side_effect=fake_status):
        try:
            assert reader.start()
            first = collect(reader)
            assert [item.kind for item in first] == ["defect"]
            assert first[0].error is not None

            assert reader.start()
            second = collect(reader)
            assert [(item.kind, item.value) for item in second] == [("defect", True)]
            assert second[0].error is None
            assert fake_connect.call_count == 1
        finally:
            reader.close()


def test_transport_failure_is_reported_then_next_start_recovers():
    outcomes = [OSError("connection lost"), True]

    def fake_status(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader = make_reader(request_timeout=5, retry_interval=0.3)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()), \
         patch.object(database, "get_defect_status", side_effect=fake_status):
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
    # Short deadline/cooldown; the blocked query outlives both, so any restart
    # while it is still running would start a second worker beside it.
    release = threading.Event()
    entered = []

    def fake_status(*args, **kwargs):
        entered.append(threading.current_thread())
        release.wait(10)
        return True

    reader = make_reader(request_timeout=0.3, retry_interval=0.3)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()), \
         patch.object(database, "get_defect_status", side_effect=fake_status):
        try:
            assert reader.start()
            assert [item.kind for item in collect(reader)] == ["error"]
            assert not reader.busy
            assert wait_until(lambda: bool(entered))
            assert entered[0].is_alive()

            time.sleep(0.5)
            assert not reader.busy
            assert not reader.start()
            assert len(entered) == 1
        finally:
            release.set()
            reader.close()


def test_reply_completed_after_deadline_is_rejected():
    release = threading.Event()
    calls = []

    def fake_status(*args, **kwargs):
        first = not calls
        calls.append(threading.current_thread())
        if first:
            release.wait(10)
            return True
        return False

    reader = make_reader(request_timeout=0.3, retry_interval=0.2)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()), \
         patch.object(database, "get_defect_status", side_effect=fake_status):
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
    attempts = []
    dummy = open_connection()

    def fake_connect(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("connection refused")
        return dummy

    reader = make_reader(request_timeout=2, retry_interval=0.3)
    with patch.object(database.psycopg2, "connect", side_effect=fake_connect), \
         patch.object(database, "get_defect_status", return_value=True):
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
    release = threading.Event()
    entered = []

    def fake_status(*args, **kwargs):
        entered.append(threading.current_thread())
        release.wait(10)
        return True

    reader = make_reader(request_timeout=10)
    with patch.object(database.psycopg2, "connect", return_value=open_connection()), \
         patch.object(database, "get_defect_status", side_effect=fake_status):
        try:
            assert reader.start()
            assert wait_until(lambda: len(entered) == 1)

            started = time.monotonic()
            reader.close()
            elapsed = time.monotonic() - started

            assert elapsed < 1.0
            assert not reader.start()
            assert len(entered) == 1
            assert entered[0].is_alive()
        finally:
            release.set()
            reader.close()
