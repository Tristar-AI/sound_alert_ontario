import os
from dotenv import load_dotenv
load_dotenv()

LINE_NAME = os.getenv('LINE_NAME')
DEVICE = os.getenv('AUDIO_DEVICE', 'plughw:0,0')
CHECK_INTERVAL = float(os.getenv('CHECK_INTERVAL', '1.0'))
CONNECT_TIMEOUT = float(os.getenv('CONNECT_TIMEOUT', '2.0'))
MAX_HOLD_RETRIES = int(os.getenv('MAX_HOLD_RETRIES', '3'))

LINE_11 = {
    'TEAM_ID': os.getenv('TEAM_ID_11'),
    'FACTORY_ID': os.getenv('FACTORY_ID_11'),
    'STATION_ID': os.getenv('STATION_ID_11'),
    'SOUND': os.getenv('SOUND_11'),
}

LINE_12 = {
    'TEAM_ID': os.getenv('TEAM_ID_12'),
    'FACTORY_ID': os.getenv('FACTORY_ID_12'),
    'STATION_ID': os.getenv('STATION_ID_12'),
    'SOUND': os.getenv('SOUND_12'),
}

LINE_TESTING = {
    'TEAM_ID': os.getenv('TEAM_ID_TESTING'),
    'FACTORY_ID': os.getenv('FACTORY_ID_TESTING'),
    'STATION_ID': os.getenv('STATION_ID_TESTING'),
    'SOUND': os.getenv('SOUND_TESTING'),
}


def _required_str(key: str) -> str:
    """Read an env var as a string; raise OSError with the key name if absent."""
    val = os.getenv(key)
    if not val:
        raise OSError(f"Required environment variable '{key}' is not set")
    return val


DB_CONFIG = {
    'host': _required_str('HOST'),
    'database': _required_str('DATABASE'),
    'user': _required_str('DB_USER'),
    'password': _required_str('PASSWORD'),
    'connect_timeout': int(CONNECT_TIMEOUT),
    'options': f"-c statement_timeout={int(CONNECT_TIMEOUT * 1000)}",
}

RABBIT_URL = (
    f"amqp://{os.getenv('RABBIT_USER')}:{os.getenv('RABBIT_PS')}"
    f"@{os.getenv('RABBIT_HOST')}:{os.getenv('RABBIT_PORT')}/"
)
