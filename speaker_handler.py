import argparse
import os
import shlex
import signal
import subprocess

from loguru import logger

from constant import DEVICE


class SpeakerHandler:
    def __init__(self, sound, device=DEVICE):
        self._player_proc = None

        self.sound = sound
        self.device = device

        if not os.path.isfile(self.sound):
            raise FileNotFoundError(f"Sound file not found: {self.sound}")

    def _kill_aplay(self):
        subprocess.run(
            ["pkill", "-f", "aplay"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_playing(self) -> bool:
        return self._player_proc is not None and self._player_proc.poll() is None

    def stop_all(self):
        """Stop the shell loop and any child `aplay` processes."""
        if self._player_proc is None:
            return
        if self._player_proc.poll() is None:
            try:
                os.killpg(self._player_proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self._player_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._player_proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self._kill_aplay()
        self._player_proc = None

    def play_sound(self):
        if self.is_playing():
            return
        logger.info(f"Playing sound: {self.sound}")
        d = shlex.quote(self.device)
        s = shlex.quote(self.sound)
        self._player_proc = subprocess.Popen(
            f"while true; do aplay -D {d} {s}; done",
            shell=True,
            start_new_session=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual speaker test")
    parser.add_argument("--sound", required=True, help="Path to WAV file")
    parser.add_argument("--device", default=DEVICE, help="ALSA device (default: %(default)s)")
    args = parser.parse_args()

    handler = SpeakerHandler(sound=args.sound, device=args.device)
    handler.play_sound()
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        handler.stop_all()
        logger.info("Stopped.")
