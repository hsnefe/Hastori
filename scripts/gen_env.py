"""Create or complete .env from .env.example with freshly generated random secrets.

Usage: python scripts/gen_env.py [--public] [--force] [output-path]

- No .env yet: writes one with random secrets.
- .env exists: leaves every value alone (existing volumes were initialised with its passwords)
  and only appends keys that were added to .env.example since, so a new variable does not
  silently fall back to a hard-coded default in the code.
- Refuses to create a fresh .env while the data volumes of an earlier stack still exist: the new
  random database/RabbitMQ passwords would not match them and nothing could log in. Restore the
  old .env, or `docker compose down -v` to start from scratch, or pass --force.
- Demo user passwords (SEED_*) keep their documented values: they are the logins of the demo.
  `--public` randomises them too and prints them once, for a demo that is reachable from the
  internet.

Needs only the standard library, so it runs before `uv sync`.
"""

import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOLUMES = ("hastori_tsdb-data", "hastori_rabbitmq-data")
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "MQTT_INGESTION_PASSWORD",
    "MQTT_HEALTH_PASSWORD",
    "MQTT_DEVICE_SECRET",
    "RABBITMQ_PASSWORD",
    "SIM_CONTROL_TOKEN",
)
SEED_KEYS = ("SEED_SYSTEM_ADMIN_PASSWORD", "SEED_SITE_ADMIN_PASSWORD", "SEED_VIEWER_PASSWORD")


def rand() -> str:
    return secrets.token_hex(16)


def existing_volumes() -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "volume", "ls", "-q"], capture_output=True, text=True, timeout=20
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []  # no Docker here: nothing to protect
    return [v for v in VOLUMES if v in out]


def parse(text: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, flags=re.M))


def build_new(template: str, public: bool) -> tuple[str, dict[str, str]]:
    keys = SECRET_KEYS + (SEED_KEYS if public else ())
    values = {key: rand() for key in keys}
    text = template
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
    return text, values


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    public, force = "--public" in sys.argv, "--force" in sys.argv
    out = Path(args[0]) if args else ROOT / ".env"
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    if out.exists():
        current = out.read_text(encoding="utf-8")
        have = parse(current)
        missing = {k: v for k, v in parse(template).items() if k not in have}
        if not missing:
            print(f"{out} exists and is complete, leaving it alone")
            return
        extra = "".join(f"{k}={rand() if k in SECRET_KEYS else v}\n" for k, v in missing.items())
        sep = "" if current.endswith("\n") else "\n"
        out.write_text(
            f"{current}{sep}# added from .env.example\n{extra}", encoding="utf-8", newline="\n"
        )
        print(f"{out} exists; added missing keys: {', '.join(missing)}")
        return

    volumes = existing_volumes()
    if volumes and not force:
        sys.exit(
            f"{out} is missing but the data volumes {', '.join(volumes)} still exist. They were "
            "initialised with the passwords of the old .env, which a new random one cannot match.\n"
            "Restore the old .env, or run `docker compose --profile sim down -v` to start from "
            "scratch (deletes all data), or pass --force."
        )
    text, values = build_new(template, public)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out} with generated secrets")
    if public:
        for key in SEED_KEYS:
            print(f"  {key}={values[key]}")
        print("(shown once; they are also in .env)")


if __name__ == "__main__":
    main()
