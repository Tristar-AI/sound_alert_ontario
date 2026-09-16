"""Manual fixed-defect speaker check without database access.

Uses the TESTING override workflow with a fixed value so audio can be
verified by hand. Not collected by pytest; run directly.
"""

import os
import signal
import sys
import time
from typing import Optional

from loguru import logger

from get_latest_database_values import DatabaseUpdate


class StaticDefectReader:
    """Non-database DefectReader returning a fixed defect value.

    Used for TESTING true/false overrides so the controller never touches
    the database in test mode. Matches the DatabaseReader poll/start/busy/close
    shape so monitoring stays orchestration-only.
    """

    def __init__(self, value: bool):
        self._value = bool(value)
        self._closed = False

    @property
    def busy(self) -> bool:
        return False

    def poll(self) -> list:
        if self._closed:
            return []
        return [DatabaseUpdate("defect", value=self._value, completed_at=time.monotonic())]

    def start(self) -> bool:
        # No background query to start; poll already carries the fixed value.
        return False

    def close(self) -> None:
        self._closed = True


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


def main():
    from constant import CHECK_INTERVAL, DEVICE, LINE_TESTING
    from monitor import SoundController
    from speaker_handler import SpeakerHandler

    try:
        testing = _testing_override(os.getenv("TESTING"))
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)
    if testing is None:
        logger.error("TESTING must be set to 'true' or 'false' for the manual harness")
        sys.exit(1)

    line_config = LINE_TESTING
    team_id = line_config["TEAM_ID"]
    factory_id = line_config["FACTORY_ID"]
    station_id = line_config["STATION_ID"]
    sound = line_config["SOUND"]

    logger.info(
        f"testing={testing!r} team={team_id} factory={factory_id} station={station_id}"
    )

    reader = StaticDefectReader(testing)
    speaker = SpeakerHandler(sound=sound, device=DEVICE)
    controller = SoundController(reader=reader, speaker=speaker, interval=CHECK_INTERVAL)

    def _sigterm_handler(signum, frame):
        logger.info("SIGTERM received, shutting down")
        controller.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        controller.monitor_continuous()
    except Exception as exc:
        logger.error(f"Program error: {exc}")
        raise
    finally:
        controller.close()


if __name__ == "__main__":
    main()
