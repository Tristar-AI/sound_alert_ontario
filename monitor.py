import argparse
import signal
import sys
import time

from loguru import logger

from constant import CHECK_INTERVAL, DB_CONFIG, DEVICE, LINE_11, LINE_12, LINE_TESTING
from get_latest_database_values import DatabaseReader, TESTING
from speaker_handler import SpeakerHandler

_LINE_MAP = {
    "11": LINE_11,
    "12": LINE_12,
    "testing": LINE_TESTING,
}


class SoundController:
    def __init__(self, team_id, factory_id, station_id, sound, interval=CHECK_INTERVAL):
        self.team_id = team_id
        self.factory_id = factory_id
        self.station_id = station_id
        self.interval = interval
        self._closed = False
        self._defect_state = None  # last observed state; None means "not yet polled"

        self.speaker_handler = SpeakerHandler(sound=sound, device=DEVICE)
        self._reader = DatabaseReader(DB_CONFIG, team_id, factory_id, station_id) if TESTING is None else None

    def _read_defect(self):
        """Return the latest completed defect state without blocking."""
        if TESTING is not None:
            return TESTING

        assert self._reader is not None
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
            self.speaker_handler.stop_all()
        except Exception as exc:
            logger.error(f"speaker shutdown failed: {exc}")
        try:
            if self._reader is not None:
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
                    self.speaker_handler.play_sound()
                else:
                    self.speaker_handler.stop_all()

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

    controller = SoundController(
        team_id=team_id,
        factory_id=factory_id,
        station_id=station_id,
        sound=sound,
        interval=CHECK_INTERVAL,
    )

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
