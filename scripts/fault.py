"""Trigger a simulator fault: python scripts/fault.py <device-key> <kind> [duration_s]."""

import json
import sys
import urllib.request

from hastori_common.settings import get_settings


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: fault.py <device-key> <kind> [duration_s]")
    device, kind = sys.argv[1], sys.argv[2]
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
    port = get_settings().sim_control_port
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/faults",
        data=json.dumps({"device": device, "kind": kind, "duration_s": duration}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.read().decode())


if __name__ == "__main__":
    main()
