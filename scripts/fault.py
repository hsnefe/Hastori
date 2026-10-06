"""Trigger a simulator fault: python scripts/fault.py <device-key> <kind> [duration_s].

The default duration is 120 s: the overheat reaches 80 C after ~16 s, the demo alarm rule wants
30 s above it, and the alarm then stays open for over a minute so it can be acknowledged.
"""

import json
import sys
import urllib.request

from hastori_common.settings import get_settings
from hastori_simulator.signals import DEFAULT_FAULT_S


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: fault.py <device-key> <kind> [duration_s]")
    device, kind = sys.argv[1], sys.argv[2]
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_FAULT_S
    settings = get_settings()
    port = settings.sim_control_port
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/faults",
        data=json.dumps({"device": device, "kind": kind, "duration_s": duration}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.sim_control_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.read().decode())


if __name__ == "__main__":
    main()
