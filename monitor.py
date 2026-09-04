class SoundController:
    def __init__(self, station_id, team_id, factory_id, sound, interval):
        self.station_id = station_id
        self.team_id = team_id
        self.factory_id = factory_id
        self.interval = interval

        self.speaker_handler = SpeakerHandler(device=DEVICE, sound=sound)
        self.defect_status_dao = DefectStatusDao(station_id, team_id, factory_id)

    
    def monitor_continuous(self, team_id, factory_id, station_id, sound):
        try:
            while True:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                defect_state = self.update_from_database(team_id, factory_id, station_id)
                
                if defect_state:
                    self.speaker_handler.play_sound()
                else:
                    self.speaker_handler.stop_all()
                
                time.sleep(self.interval)
                
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        except Exception as e:
            logger.error(f"Error during continuous monitoring: {e}")
            raise
        finally:
            self.close()

def main(line):
    """Main function - modify these parameters as needed"""
    if line == '11':
        line_config = LINE_11
    elif line == '12':
        line_config = LINE_12
    else:
        raise ValueError(f"Invalid line: {line}")

    team_id = line_config['TEAM_ID']
    factory_id = line_config['FACTORY_ID']
    station_id = line_config['STATION_ID']
    sound = line_config['SOUND']
    
    try:
        sound_controller = SoundController(
            station_id, 
            team_id,    
            factory_id,
            sound
        )

        sound_controller.monitor_continuous(
            team_id=team_id,
            factory_id=factory_id,
            station_id=station_id,
            sound=sound
        )
        
    except KeyboardInterrupt:
        logger.info("Program interrupted by user")
    except Exception as e:
        logger.error(f"Program error: {e}")
        raise
    finally:
        if stacklight_controller:
            stacklight_controller.close()
        logger.info("Program ended")


def _load_line_from_config() -> str:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with config_path.open() as f:
        specs = yaml.safe_load(f)["specs"]
    return str(specs["line_name"])


if __name__ == "__main__":
    main(_load_line_from_config())