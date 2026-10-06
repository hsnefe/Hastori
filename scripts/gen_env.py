"""Create .env from .env.example with freshly generated random secrets.

Usage: python scripts/gen_env.py [output-path]

Never overwrites an existing file (existing volumes were initialised with its passwords).
Demo user passwords (SEED_*) keep their documented values: they are the logins of the demo.
Needs only the standard library, so it runs before `uv sync`.
"""

import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env"
    if out.exists():
        print(f"{out} exists, leaving it alone")
        return
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    def rand() -> str:
        return secrets.token_hex(16)

    values = {
        "POSTGRES_PASSWORD": rand(),
        "MQTT_INGESTION_PASSWORD": rand(),
        "MQTT_HEALTH_PASSWORD": rand(),
        "MQTT_DEVICE_SECRET": rand(),
        "RABBITMQ_PASSWORD": rand(),
        "SIM_CONTROL_TOKEN": rand(),
    }
    for key, value in values.items():
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    # URLs embed the passwords; keep them consistent.
    text = text.replace(
        "hastori:hastori_demo@127.0.0.1:5432",
        f"hastori:{values['POSTGRES_PASSWORD']}@127.0.0.1:5432",
    )
    text = text.replace(
        "hastori:hastori_demo@127.0.0.1:5672",
        f"hastori:{values['RABBITMQ_PASSWORD']}@127.0.0.1:5672",
    )
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out} with generated secrets")


if __name__ == "__main__":
    main()
