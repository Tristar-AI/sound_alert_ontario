import argparse
import signal
import sys
import time

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
        self._defect_state = None  # last observed state; None means "not yet polled"
        self._outage_stopped = False

        self.speaker_handler = SpeakerHandler(sound=sound, device=DEVICE)

    def _read_defect(self) -> bool:
        """Return the current defect state, holding the last state and retrying on failure.

        On failure: keep the last state (sound/no sound) and retry the
        connection in a loop. After MAX_HOLD_RETRIES failed retries, stop the sound, then keep retrying until the
        database returns and re-query immediately.
        """

        # Try to get the defect status
        try:
            return get_defect_status(self.team_id, self.factory_id, self.station_id)
        except Exception as exc:
            logger.error(f"defect poll failed, holding last state and retrying: {exc}")

        # If defect status query fails, continuely query the database for and updated value
        # After MAX_HOLD_RETRIES, turn off the sound
        retries = 0
        while True:
            retries += 1

            # If we've retried MAX_HOLD_RETRIES times and the sound is still on, turn it off
            if retries > MAX_HOLD_RETRIES and not self._outage_stopped:
                logger.error(
                    f"database unavailable after {MAX_HOLD_RETRIES} retries; stopping sound"
                )
                self.speaker_handler.stop_all()
                self._outage_stopped = True

            # Sleep for the interval and try to get the defect status again
            time.sleep(self.interval)  # guard against a busy-spin on fast-failing connects
            try:
                defect = get_defect_status(self.team_id, self.factory_id, self.station_id)
            except Exception as exc:
                logger.error(f"retry {retries} failed: {exc}")
                continue
            if self._outage_stopped:
                logger.info("database recovered; resuming from live state")
                self._outage_stopped = False
            return defect

    def close(self):
        """Stop the speaker and mark this controller as closed. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self.speaker_handler.stop_all()
        logger.info("SoundController closed")

    def monitor_continuous(self):
        try:
            while True:
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
