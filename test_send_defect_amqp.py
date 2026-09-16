"""Manual test publisher for sending dummy defects over AMQP."""
import argparse
import datetime
import os
from typing import Any, Optional, Tuple

from ares.messaging.rabbitmq.client import RabbitClient
from ares.messaging.rabbitmq.data.declarables import Exchange
from ares.messaging.rabbitmq.encoder.packer import JsonPacker

from constant import LINE_11, LINE_12, LINE_TESTING, RABBIT_URL

_LINE_MAP = {
    "11": LINE_11,
    "12": LINE_12,
    "testing": LINE_TESTING,
}

DEFECT_TYPE = "eyelash_large"


class RabbitDAO:
    def __init__(
        self, client: RabbitClient, exchange: Exchange, routing_key: str
    ) -> None:
        self._client = client
        self._exchange = exchange
        self._routing_key = routing_key

    def publish(self, msg: Any, timeout: Optional[float] = None) -> bool:
        return self._client.publish(
            msg,
            self._exchange,
            self._routing_key,
            [],
            timeout,
        )


def _create_publisher() -> RabbitDAO:
    return RabbitDAO(
        RabbitClient(RABBIT_URL),
        Exchange("autonomous_station_exchange", "direct", True, JsonPacker()),
        "autonomous_defect_queue",
    )


def send_defect_amqp(
    defect_types: list[str],
    team_id: int,
    factory_id: int,
    station_id: int,
    s3_image: str = os.getenv("S3_IMAGE"),
    publisher: Optional[RabbitDAO] = None,
) -> Tuple[datetime.datetime, bool]:
    dt = datetime.datetime.now(datetime.timezone.utc)
    if publisher is None:
        publisher = _create_publisher()
    publish_success = publisher.publish(
        {
            "s3_image": s3_image,
            "defect_types": defect_types,
            "timestamp": str(dt.isoformat()),
            "station_id": int(station_id),
            "factory_id": int(factory_id),
            "team_id": int(team_id),
        }
    )
    return dt, publish_success


def load_line(line: str) -> dict:
    if line not in _LINE_MAP:
        valid = ", ".join(_LINE_MAP)
        raise ValueError(f"line={line!r} is not recognized. Use one of: {valid}")
    return _LINE_MAP[line]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a dummy eyelash_large defect over AMQP"
    )
    parser.add_argument(
        "line",
        choices=tuple(_LINE_MAP),
        help="Line to send a dummy defect for (11, 12, or testing)",
    )
    args = parser.parse_args()

    line_config = load_line(args.line)
    team_id = line_config["TEAM_ID"]
    factory_id = line_config["FACTORY_ID"]
    station_id = line_config["STATION_ID"]

    sent_at, publish_success = send_defect_amqp(
        defect_types=[DEFECT_TYPE],
        team_id=team_id,
        factory_id=factory_id,
        station_id=station_id,
    )
    print(f"Sent at: {sent_at.isoformat()} | publish_success={publish_success}")


if __name__ == "__main__":
    main()
