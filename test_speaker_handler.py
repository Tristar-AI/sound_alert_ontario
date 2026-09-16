import argparse
import time

from loguru import logger

from constant import DEVICE
from speaker_handler import SpeakerHandler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual speaker test")
    parser.add_argument("--sound", required=True, help="Path to WAV file")
    parser.add_argument("--device", default=DEVICE, help="ALSA device (default: %(default)s)")
    args = parser.parse_args()

    handler = SpeakerHandler(sound=args.sound, device=args.device)
    handler.play_sound()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        handler.stop_all()
        logger.info("Stopped.")
