import argparse
import signal
import sys
import time
from typing import Optional

from loguru import logger

from constant import CHECK_INTERVAL, DEVICE, LINE_11, LINE_12, LINE_TESTING, MAX_HOLD_RETRIES
from get_latest_database_values import get_defect_status
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
        self._defect_state = None  # last confirmed DB state; None means "not yet polled"
        self._outage_stopped = False
        self._consecutive_failures = 0

        self.speaker_handler = SpeakerHandler(sound=sound, device=DEVICE)

    def _keep_alarm_alive(self) -> None:
        """Restart the speaker from last confirmed state before talking to the database."""
        if self._closed:
            return
        if self._defect_state is True and not self._outage_stopped:
            self.speaker_handler.play_sound()

    def _read_defect(self) -> Optional[bool]:
        """One bounded poll. Returns the bool on success, None on failure or shutdown."""
        if self._closed:
            return None
        try:
            return get_defect_status(self.team_id, self.factory_id, self.station_id)
        except Exception as exc:
            logger.error(f"defect poll failed, holding last state: {exc}")
            return None

    def _apply_result(self, defect: Optional[bool]) -> None:
        """Apply this tick's result, or ignore it if we have already closed."""
        if self._closed:
            return
        if defect is None:
            self._consecutive_failures += 1
            if self._consecutive_failures > MAX_HOLD_RETRIES and not self._outage_stopped:
                logger.error(
                    f"database unavailable after {MAX_HOLD_RETRIES} retries; stopping sound"
                )
                try:
                    self.speaker_handler.stop_all()
                except Exception as exc:
                    logger.error(f"speaker stop failed after outage threshold: {exc}")
                self._outage_stopped = True
            return
        if self._outage_stopped:
            logger.info("database recovered; resuming from live state")
            self._outage_stopped = False
        self._consecutive_failures = 0
        if defect != self._defect_state:
            logger.info(f"defect={defect}")
            self._defect_state = defect
        if defect:
            self.speaker_handler.play_sound()
        else:
            self.speaker_handler.stop_all()

    def close(self):
        """Stop the speaker and mark this controller as closed. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.speaker_handler.stop_all()
        except Exception as exc:
            logger.error(f"speaker stop failed during close: {exc}")
        logger.info("SoundController closed")

    def monitor_continuous(self):
        try:
            while not self._closed:
                self._keep_alarm_alive()
                defect = self._read_defect()
                self._apply_result(defect)
                if self._closed:
                    break
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
