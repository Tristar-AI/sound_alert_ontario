import argparse
import os
import signal
import sys
import time

from loguru import logger

from constant import CHECK_INTERVAL, DEVICE, LINE_11, LINE_12, LINE_NAME, LINE_TESTING
from get_latest_database_values import get_defect_status
from speaker_handler import SpeakerHandler

_LINE_MAP = {
    '11': LINE_11,
    '12': LINE_12,
    'testing': LINE_TESTING,
}


def _resolve_line(name: str | None) -> dict:
    """Return the line config dict for LINE_NAME, or raise ValueError naming valid values."""
    if name not in _LINE_MAP:
        valid = sorted(_LINE_MAP)
        raise ValueError(
            f"LINE_NAME={name!r} is not recognized. Set LINE_NAME to one of: {valid}"
        )
    return _LINE_MAP[name]


class SoundController:
    def __init__(self, team_id, factory_id, station_id, sound, interval=CHECK_INTERVAL):
        self.team_id = team_id
        self.factory_id = factory_id
        self.station_id = station_id
        self.interval = interval
        self._closed = False
        self._defect_state = None  # last observed state; None means "not yet polled"

        self.speaker_handler = SpeakerHandler(sound=sound, device=DEVICE)

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
                try:
                    defect = get_defect_status(self.team_id, self.factory_id, self.station_id)
                except Exception as exc:
                    logger.error(f"defect poll failed, treating as no-defect: {exc}")
                    defect = False

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


def main():
    parser = argparse.ArgumentParser(description="Defect sound alert daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, log the defect state, and exit without touching the speaker",
    )
    args = parser.parse_args()

    line_config = _resolve_line(LINE_NAME)
    team_id = line_config['TEAM_ID']
    factory_id = line_config['FACTORY_ID']
    station_id = line_config['STATION_ID']
    sound = line_config['SOUND']

    logger.info(
        f"line={LINE_NAME!r} team={team_id} factory={factory_id} station={station_id}"
    )

    if args.once:
        try:
            defect = get_defect_status(team_id, factory_id, station_id)
        except Exception as exc:
            logger.error(f"defect poll failed, treating as no-defect: {exc}")
            defect = False
        logger.info(f"defect={defect}")
        sys.exit(0)

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
