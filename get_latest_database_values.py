"""Postgres defect source and bounded asynchronous reader."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import psycopg2


@dataclass(frozen=True)
class DatabaseUpdate:
    kind: str
    value: Any = None
    error: Optional[str] = None
    completed_at: float = 0.0


class DefectStatusUnavailable(Exception):
    """The defect query could not reach the database; retry after cooldown."""


class DefectStatusQueryError(Exception):
    """The database answered with a query failure; the connection stays usable."""


class DefectStatusSource(Protocol):
    """Synchronous defect query.

    Implementations raise DefectStatusUnavailable for transport failures and
    DefectStatusQueryError for ordinary query failures.
    """
    def read_defect(self) -> bool:
        ...

    def close(self) -> None:
        ...


class DefectReader(Protocol):
    """Single-worker async reader: start one bounded query, poll without blocking."""

    @property
    def busy(self) -> bool:
        ...

    def start(self) -> bool:
        ...

    def poll(self) -> list:
        ...

    def close(self) -> None:
        ...


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:1000]}"


def _is_transport_error(error: BaseException) -> bool:
    sqlstate = getattr(error, "pgcode", None) or getattr(error, "sqlstate", None)
    if sqlstate:
        return sqlstate.startswith("08") or sqlstate in ("57P01", "57P02", "57P03")
    return isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError, OSError))


_DEFECT_QUERY = """
    select
        sum(uc.val) > 0 as defects_visible
    from
        unacked_count uc
    where
        team_id = %(team_id)s
        and factory_id = %(factory_id)s
        and station_id = %(station_id)s
"""


class PostgresDefectStatusSource:
    """Postgres-backed defect source with an injected connector."""

    def __init__(
        self,
        config: dict,
        team_id: int,
        factory_id: int,
        station_id: int,
        *,
        connect: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = dict(config)
        self._team_id = team_id
        self._factory_id = factory_id
        self._station_id = station_id
        self._connect = connect if connect is not None else psycopg2.connect
        self._connection: Optional[Any] = None

    def read_defect(self) -> bool:
        try:
            if self._connection is None or self._connection.closed:
                connection = self._connect(**self._config)
                connection.autocommit = True
                self._connection = connection
        except Exception as error:
            self._connection = None
            raise DefectStatusUnavailable(_error_text(error)) from error
        try:
            params = {
                "team_id": self._team_id,
                "factory_id": self._factory_id,
                "station_id": self._station_id,
            }
            with self._connection.cursor() as cur:
                cur.execute(_DEFECT_QUERY, params)
                row = cur.fetchone()
                return bool(row[0]) if row and row[0] is not None else False
        except Exception as error:
            connection = self._connection
            if _is_transport_error(error) or connection is None or bool(getattr(connection, "closed", False)):
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                self._connection = None
                raise DefectStatusUnavailable(_error_text(error)) from error
            raise DefectStatusQueryError(_error_text(error)) from error

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class DatabaseReader:
    """Submit at most one bounded defect query and collect replies without blocking."""

    def __init__(
        self,
        source: DefectStatusSource,
        *,
        request_timeout: float = 60.0,
        retry_interval: float = 10.0,
    ) -> None:
        self._source = source
        self.request_timeout = float(request_timeout)
        self.retry_interval = float(retry_interval)
        self._executor = ThreadPoolExecutor(max_workers=1)
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

    def _run_query(self):
        try:
            value = self._source.read_defect()
            return DatabaseUpdate("defect", value=value, completed_at=time.monotonic())
        except DefectStatusUnavailable as error:
            return DatabaseUpdate("error", error=_error_text(error), completed_at=time.monotonic())
        except DefectStatusQueryError as error:
            return DatabaseUpdate("defect", error=_error_text(error), completed_at=time.monotonic())

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
            self._future = self._executor.submit(self._run_query)
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
            close = getattr(self._source, "close", None)
            if callable(close):
                try:
                    executor.submit(close)
                except RuntimeError:
                    pass
            executor.shutdown(wait=False)
