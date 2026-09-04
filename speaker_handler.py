import argparse
import subprocess
import os
import time
import signal
from loguru import logger
from constant import WAV_FILE_SHORT, WAV_FILE_LONG, DEVICE

class SpeakerHandler:
    def __init__(self, device=DEVICE, sound=sound):
        self._player_proc = None
        self.trigger_time_short = None
        self.trigger_time_long = None

        if not os.path.isfile(sound):
            raise FileNotFoundError(f"Sound file not found: {sound}")

    def _kill_aplay(self):
        subprocess.run(
            ["pkill", "-f", "aplay"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_all(self):
        """Stop the shell loop and any child `aplay` processes."""
        if self._player_proc is not None and self._player_proc.poll() is None:
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
        logger.info(f"Playing sound: {self.sound}")
        self._player_proc = subprocess.Popen(
            ["aplay", "-D", self.device, self.sound],
            start_new_session=True,
        )


