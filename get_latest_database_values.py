import contextlib
import os
import psycopg2
from psycopg2.extensions import connection as psycopg2_connection
from typing import Any, Optional
from constant import DB_CONFIG
from loguru import logger


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
) -> bool:
    """
    Returns whether or not there is a defect actively on the dashboard page.

    """
    if TESTING is not None:  # If testing, can set return value
        return TESTING
    
    query = """
        select
            val = '#F40505' is_red
        from
            operator_color
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

    with contextlib.closing(psycopg2.connect(**DB_CONFIG)) as conn:
        conn.autocommit = True # Fix for the error associated with the connection reaper
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return bool(row[0]) if row and row[0] is not None else False

if __name__ == "__main__":
    pass