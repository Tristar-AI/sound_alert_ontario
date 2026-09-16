import argparse
import os
import signal
import sys
import time
from typing import Optional, Protocol

from loguru import logger

from constant import CHECK_INTERVAL, DB_CONFIG, DEVICE, LINE_11, LINE_12, LINE_TESTING
from get_latest_database_values import (
    DatabaseReader,
    DatabaseUpdate,
    DefectReader,
    PostgresDefectStatusSource,
)
from speaker_handler import SpeakerHandler

_LINE_MAP = {
    "11": LINE_11,
    "12": LINE_12,
    "testing": LINE_TESTING,
}


class Speaker(Protocol):
    """Focused audio sink; SpeakerHandler satisfies this contract."""

    def play_sound(self) -> None: ...
    def stop_all(self) -> None: ...


class StaticDefectReader:
    """Non-database DefectReader returning a fixed defect value.

    Used for TESTING true/false overrides so the controller never touches
    the database in test mode. Matches the DatabaseReader poll/start/busy/close
    shape so SoundController stays orchestration-only.
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


class SoundController:
    """Poll a reader and drive a speaker. Monitoring orchestration only."""

    def __init__(self, reader: DefectReader, speaker: Speaker, interval: float = CHECK_INTERVAL):
        self._reader = reader
        self._speaker = speaker
        self.interval = interval
        self._closed = False
        self._defect_state = None  # last observed state; None means "not yet polled"

    def _read_defect(self):
        """Return the latest completed defect state without blocking."""
        defect = self._defect_state
        for update in self._reader.poll():
            if update.error is not None:
                logger.error(f"defect poll failed, retaining last known state: {update.error}")
            elif update.kind == "defect":
                defect = update.value
        if not self._reader.busy:
            self._reader.start()
        return defect

    def close(self):
        """Stop the speaker and mark this controller as closed. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            self._speaker.stop_all()
        except Exception as exc:
            logger.error(f"speaker shutdown failed: {exc}")
        try:
            self._reader.close()
        except Exception as exc:
            logger.error(f"database reader shutdown failed: {exc}")
        logger.info("SoundController closed")

    def monitor_continuous(self):
        try:
            while not self._closed:
                defect = self._read_defect()

                # Log only on transitions so the journal stays readable at 1 Hz
                if defect != self._defect_state:
                    logger.info(f"defect={defect}")
                    self._defect_state = defect

                if defect:
                    self._speaker.play_sound()
                else:
                    self._speaker.stop_all()

                time.sleep(self.interval)

        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        finally:
            self.close()


def load_line(line: str) -> dict:
    if line not in _LINE_MAP:
        valid = ", ".join(_LINE_MAP)
        raise ValueError(f"line={line!r} is not recognized. Use one of: {valid}")
    return _LINE_MAP[line]


def build_reader(team_id, factory_id, station_id, testing: Optional[bool] = None) -> DefectReader:
    """Compose the defect source. Only construction site for DatabaseReader."""
    if testing is not None:
        return StaticDefectReader(testing)
    source = PostgresDefectStatusSource(DB_CONFIG, team_id, factory_id, station_id)
    return DatabaseReader(source)


def main():
    parser = argparse.ArgumentParser(description="Factory line sound alert")
    parser.add_argument(
        "line",
        choices=tuple(_LINE_MAP),
        help="Line to monitor (11, 12, or testing)",
    )
    args = parser.parse_args()

    try:
        line_config = load_line(args.line)
        testing = _testing_override(os.getenv("TESTING"))
    except (EnvironmentError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    team_id = line_config["TEAM_ID"]
    factory_id = line_config["FACTORY_ID"]
    station_id = line_config["STATION_ID"]
    sound = line_config["SOUND"]

    logger.info(
        f"line={args.line!r} team={team_id} factory={factory_id} station={station_id}"
    )

    reader = build_reader(team_id, factory_id, station_id, testing=testing)
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
