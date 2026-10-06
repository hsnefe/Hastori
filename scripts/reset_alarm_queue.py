"""Delete the `alarm.telemetry` queue (and its dead-letter queue) so it is declared again with the
current arguments.

RabbitMQ refuses to declare an existing queue with different arguments (PRECONDITION_FAILED), and
ingestion reports "rabbitmq unavailable" until that is resolved. Run this once after upgrading
from a checkout that declared the queue differently, then restart ingestion:

    make reset-alarm-queue && docker compose restart ingestion

Messages waiting in the queue are lost; the raw measurements are in the database, and the alarm
service replays the last minutes from there when it starts.
"""

import base64
import json
import re
import sys
import urllib.error
import urllib.request

from hastori_common.messaging import ALARM_QUEUE, DLQ
from hastori_common.settings import get_settings

MANAGEMENT_URL = "http://127.0.0.1:15672"


def delete_queue(name: str, auth: str) -> str:
    req = urllib.request.Request(f"{MANAGEMENT_URL}/api/queues/%2F/{name}", method="DELETE")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"deleted {name} (HTTP {resp.status})"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return f"{name} does not exist"
        raise


def main() -> None:
    url = get_settings(strict=("rabbitmq_url",)).rabbitmq_url
    m = re.match(r"amqp://([^:]+):([^@]+)@", url)
    if not m:
        sys.exit("RABBITMQ_URL has no user and password")
    auth = base64.b64encode(f"{m.group(1)}:{m.group(2)}".encode()).decode()
    for name in (ALARM_QUEUE, DLQ):
        try:
            print(delete_queue(name, auth))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            sys.exit(f"cannot reach the RabbitMQ management API at {MANAGEMENT_URL}: {exc}")
    print("restart ingestion so it declares the queue again: docker compose restart ingestion")


if __name__ == "__main__":
    main()
