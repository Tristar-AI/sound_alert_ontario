import contextlib
import os
import psycopg2
from psycopg2.extensions import connection as psycopg2_connection
from typing import Any, Optional
from constant import DB_CONFIG
from loguru import logger

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


def _testing_override(val: Optional[str]) -> Optional[bool]:
    """Parse TESTING as a bool override. Unset/empty means 'use the database'."""
    if val is None:
        return None
    normalized = val.strip().strip("\"'").lower()
    if normalized == "":
        return None
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no"):
        return False
    raise ValueError(f"TESTING must be true/false (or empty), got: {val!r}")


TESTING = _testing_override(os.getenv("TESTING"))


class StationStatusDao:
    def __init__(self):
        self._db_conn: Optional[psycopg2_connection] = None

    @property
    def is_open(self):
        return self._db_conn is not None

    def open(self, timeout: float = 1.0):
        if not self._db_conn:
            self._db_conn = psycopg2.connect(**DB_CONFIG)
            self._db_conn.autocommit = True
        else:
            raise Exception("Cannot open connection, connection already open.")

    def close(self):
        if self._db_conn is not None:
            try:
                self._db_conn.close()
            except Exception:
                pass
        self._db_conn = None

    def get_status(self, team_id: int, factory_id: int, station_id: int):
        if self.is_open:
            res = False
            query = """
                SELECT EXISTS (
                    SELECT 1
                    FROM current_status
                    WHERE team_id = %s
                    AND factory_id = %s
                    AND station_id = %s
                    AND status_name = 'paused'
                    AND removed_at IS NULL
                ) AS is_paused
            """
            if self._db_conn is not None:
                with self._db_conn.cursor() as cur:
                    cur.execute(query, (team_id, factory_id, station_id))
                    result = cur.fetchone()

                if result is not None:
                    res = result[0]
            return res
        else:
            raise Exception("No DB connection.")


def get_defect_status(
    team_id: int,
    factory_id: int,
    station_id: int,
    connection=None,
) -> bool:
    """
    Returns whether or not there is a defect actively on the dashboard page.

    """
    if TESTING is not None:  # If testing, can set return value
        return TESTING
    
    query = """
        select
            sum(uc.val) > 0 as defects_visible
        from
            unacked_count uc
        where
            team_id = %(team_id)s
            and factory_id = %(factory_id)s
            and station_id = %(station_id)s
    """

    params = {
        "team_id": team_id,
        "factory_id": factory_id,
        "station_id": station_id,
    }

    if connection is not None:
        with connection.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return bool(row[0]) if row and row[0] is not None else False

    with contextlib.closing(psycopg2.connect(**DB_CONFIG)) as conn:
        conn.autocommit = True # Fix for the error associated with the connection reaper
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return bool(row[0]) if row and row[0] is not None else False


@dataclass(frozen=True)
class DatabaseUpdate:
    kind: str
    value: Any = None
    error: Optional[str] = None
    completed_at: float = 0.0


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:1000]}"


def _is_transport_error(error, connection) -> bool:
    if connection is None or connection.closed:
        return True
    sqlstate = getattr(error, "pgcode", None) or getattr(error, "sqlstate", None)
    if sqlstate:
        return sqlstate.startswith("08") or sqlstate in ("57P01", "57P02", "57P03")
    return isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError, OSError))


class DatabaseReader:
    """Submit at most one bounded defect query and collect replies without blocking."""

    def __init__(self, config, team_id, factory_id, station_id, *, request_timeout: float = 60.0, retry_interval: float = 10.0):
        self.config = dict(config)
        self.team_id = team_id
        self.factory_id = factory_id
        self.station_id = station_id
        self.request_timeout = float(request_timeout)
        self.retry_interval = float(retry_interval)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._connection = None
        self._future = None
        self._retired = None
        self._busy = False
        self._deadline = 0.0
        self._cooldown_until = 0.0
        self._closed = False
        self._pending_error = None

    @property
    def busy(self) -> bool:
        return self._busy

    def _run_query(self, team_id, factory_id, station_id):
        try:
            if self._connection is None or self._connection.closed:
                self._connection = psycopg2.connect(**self.config)
                self._connection.autocommit = True
        except Exception as error:
            return DatabaseUpdate("error", error=_error_text(error), completed_at=time.monotonic())
        try:
            value = get_defect_status(team_id, factory_id, station_id, self._connection)
            return DatabaseUpdate("defect", value=value, completed_at=time.monotonic())
        except Exception as error:
            if _is_transport_error(error, self._connection):
                try:
                    self._connection.close()
                except Exception:
                    pass
                self._connection = None
                return DatabaseUpdate("error", error=_error_text(error), completed_at=time.monotonic())
            return DatabaseUpdate("defect", error=_error_text(error), completed_at=time.monotonic())

    def _close_connection(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def start(self) -> bool:
        if self._closed or self._busy:
            return False
        if self._retired is not None:
            if not self._retired.done():
                return False
            self._retired = None
        if time.monotonic() < self._cooldown_until:
            return False
        if self._executor is None:
            return False
        self._deadline = time.monotonic() + self.request_timeout
        try:
            self._future = self._executor.submit(self._run_query, self.team_id, self.factory_id, self.station_id)
        except (OSError, RuntimeError) as error:
            self._record_failure(_error_text(error))
            return False
        self._busy = True
        return True

    def poll(self) -> list:
        if self._closed:
            return []
        updates = [self._pending_error] if self._pending_error is not None else []
        self._pending_error = None
        if self._retired is not None and self._retired.done():
            self._retired = None
        if not self._busy:
            return updates
        future = self._future
        if future is not None and future.done():
            self._future = None
            try:
                update = future.result()
            except Exception:
                self._record_failure("database worker exited", updates)
                return updates
            if update.kind == "error":
                updates.append(update)
                self._record_failure("", None)
                return updates
            if update.completed_at > self._deadline:
                self._record_failure("database query deadline exceeded", updates)
                return updates
            updates.append(update)
            self._busy = False
            self._deadline = 0.0
            return updates
        if time.monotonic() >= self._deadline:
            self._retired = self._future
            self._future = None
            self._busy = False
            self._deadline = 0.0
            self._cooldown_until = time.monotonic() + self.retry_interval
            updates.append(DatabaseUpdate("error", error="database query deadline exceeded", completed_at=time.monotonic()))
            return updates
        return updates

    def _record_failure(self, reason: str, updates=None) -> None:
        if reason:
            update = DatabaseUpdate("error", error=reason, completed_at=time.monotonic())
            if updates is not None:
                updates.append(update)
            else:
                self._pending_error = update
        self._busy = False
        self._deadline = 0.0
        self._cooldown_until = time.monotonic() + self.retry_interval

    def close(self) -> None:
        if self._closed and self._executor is None:
            return
        self._closed = True
        self._busy = False
        self._pending_error = None
        self._future = None
        self._retired = None
        executor, self._executor = self._executor, None
        if executor is not None:
            try:
                executor.submit(self._close_connection)
            except RuntimeError:
                pass
            executor.shutdown(wait=False)

if __name__ == "__main__":
    pass