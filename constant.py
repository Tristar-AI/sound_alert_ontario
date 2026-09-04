import os
from dotenv import load_dotenv
load_dotenv()

SERIAL_PORT = os.getenv('SERIAL_PORT')
BAUD_RATE = os.getenv('BAUD_RATE')
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600

LINE_11 = {
    'TEAM_ID': os.getenv('TEAM_ID_1'),
    'FACTORY_ID': os.getenv('FACTORY_ID_1'),
    'STATION_ID': os.getenv('STATION_ID_1'),
    'LINE_EMAIL_ID': os.getenv('LINE_1_EMAIL_ID'),
    'SOUND': os.getenv('SOUND_11'),
}

LINE_12 = {
    'TEAM_ID': os.getenv('TEAM_ID_4'),
    'FACTORY_ID': os.getenv('FACTORY_ID_4'),
    'STATION_ID': os.getenv('STATION_ID_4'),
    'LINE_EMAIL_ID': os.getenv('LINE_4_EMAIL_ID'),
    'SOUND': os.getenv('SOUND_12'),
}


LINE_TESTING = {
    'TEAM_ID': os.getenv('TEAM_ID_TESTING'),
    'FACTORY_ID': os.getenv('FACTORY_ID_TESTING'),
    'STATION_ID': os.getenv('STATION_ID_TESTING'),
    'LINE_EMAIL_ID': os.getenv('LINE_TESTING_EMAIL_ID'),
    'SOUND': os.getenv('SOUND_TESTING'),
}



DB_CONFIG = {
    'host': os.getenv('HOST', 'dashboard.c7blcbt6vhon.us-east-1.rds.amazonaws.com'),
    'database': os.getenv('DATABASE', 'production'),
    'user': os.getenv('DB_USER', 'spectrum_stacklight_ro'),
    'password': os.getenv('PASSWORD', ''),
}

RABBIT_URL = f"amqp://{os.getenv('RABBIT_USER')}:{os.getenv('RABBIT_PS')}@{os.getenv('RABBIT_HOST')}:{os.getenv('RABBIT_PORT')}/"