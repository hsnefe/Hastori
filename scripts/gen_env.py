"""Create or complete .env from .env.example with freshly generated random secrets.

Usage: python scripts/gen_env.py [--public] [--force] [output-path]

- No .env yet: writes one with random secrets.
- .env exists: leaves every value alone (existing volumes were initialised with its passwords)
  and only appends keys that were added to .env.example since, so a new variable does not
  silently fall back to a hard-coded default in the code. The one exception is `--public`: it
  replaces the SEED_* demo-user passwords (then run `make seed-reset`).
- Refuses to create a fresh .env while the data volumes of an earlier stack still exist: the new
  random database/RabbitMQ passwords would not match them and nothing could log in. Restore the
  old .env, or `docker compose down -v` to start from scratch, or pass --force.
- Demo user passwords (SEED_*) keep their documented values: they are the logins of the demo.
  `--public` randomises them too and prints them once, for a demo that is reachable from the
  internet.

Needs only the standard library, so it runs before `uv sync`.
"""

import contextlib
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOLUMES = ("hastori_tsdb-data", "hastori_rabbitmq-data", "hastori_redis-data")
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "MQTT_INGESTION_PASSWORD",
    "MQTT_HEALTH_PASSWORD",
    "MQTT_DEVICE_SECRET",
    "RABBITMQ_PASSWORD",
    "REDIS_PASSWORD",
    "JWT_SECRET",
    "SIM_CONTROL_TOKEN",
    "GRAFANA_ADMIN_PASSWORD",
)
# Compose reads the Redis password from a file (a secret), not from the environment.
REDIS_SECRET_FILE = ROOT / "infra" / "redis" / "redis.pw"
SEED_KEYS = ("SEED_SYSTEM_ADMIN_PASSWORD", "SEED_SITE_ADMIN_PASSWORD", "SEED_VIEWER_PASSWORD")


def rand(key: str = "") -> str:
    # The JWT signing key is 64 hex characters (256 bits); the rest are 32.
    return secrets.token_hex(32 if key == "JWT_SECRET" else 16)


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
    values = {key: rand(key) for key in keys}
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
    text = text.replace(":redis_demo@127.0.0.1:6379", f":{values['REDIS_PASSWORD']}@127.0.0.1:6379")
    return text, values


def private(path: Path) -> None:
    """Owner-only where the platform has modes (a no-op on Windows)."""
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def set_key(env_text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=", env_text, flags=re.M):
        return re.sub(rf"^{key}=.*$", f"{key}={value}", env_text, flags=re.M)
    return f"{env_text.rstrip()}\n{key}={value}\n"


def rotate_seed_passwords(env_text: str) -> tuple[str, dict[str, str]]:
    """New random SEED_* passwords inside an existing .env (`--public` on a stack that is
    already set up). The database still holds the old hashes until `make seed-reset`."""
    values = {key: rand() for key in SEED_KEYS}
    for key, value in values.items():
        if re.search(rf"^{key}=", env_text, flags=re.M):
            env_text = re.sub(rf"^{key}=.*$", f"{key}={value}", env_text, flags=re.M)
        else:
            env_text = f"{env_text.rstrip()}\n{key}={value}\n"
    return env_text, values


def write_redis_secret(env_text: str) -> None:
    """infra/redis/redis.pw mirrors REDIS_PASSWORD (compose secret); rewritten on every run so
    the two cannot drift apart."""
    password = parse(env_text).get("REDIS_PASSWORD")
    if not password:
        return
    REDIS_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    REDIS_SECRET_FILE.write_text(password, encoding="utf-8", newline="\n")
    private(REDIS_SECRET_FILE)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    public, force = "--public" in sys.argv, "--force" in sys.argv
    out = Path(args[0]) if args else ROOT / ".env"
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    if out.exists():
        current = out.read_text(encoding="utf-8")
        if public:
            current, values = rotate_seed_passwords(current)
            out.write_text(current, encoding="utf-8", newline="\n")
            private(out)
            print(f"{out} exists: new random demo-user passwords (shown once, also in .env):")
            for key in SEED_KEYS:
                print(f"  {key}={values[key]}")
            print("Run `make seed-reset` so the database takes them over.")
            current = set_key(current, "API_DOCS", "false")  # no Swagger on a public address
            out.write_text(current, encoding="utf-8", newline="\n")
        have = parse(current)
        missing = {k: v for k, v in parse(template).items() if k not in have}
        if not missing:
            write_redis_secret(current)
            print(f"{out} exists and is complete, leaving it alone")
            return
        fresh = {k: rand(k) for k in missing if k in SECRET_KEYS}
        lines = []
        for k, v in missing.items():
            if k in fresh:
                v = fresh[k]
            elif k == "REDIS_URL":  # embeds the password: keep it consistent with REDIS_PASSWORD
                password = fresh.get("REDIS_PASSWORD") or have["REDIS_PASSWORD"]
                v = v.replace(":redis_demo@", f":{password}@")
            lines.append(f"{k}={v}\n")
        sep = "" if current.endswith("\n") else "\n"
        updated = f"{current}{sep}# added from .env.example\n{''.join(lines)}"
        out.write_text(updated, encoding="utf-8", newline="\n")
        private(out)
        write_redis_secret(updated)
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
    if public:
        text = set_key(text, "API_DOCS", "false")
    out.write_text(text, encoding="utf-8", newline="\n")
    private(out)
    write_redis_secret(text)
    print(f"wrote {out} with generated secrets")
    if public:
        for key in SEED_KEYS:
            print(f"  {key}={values[key]}")
        print("(shown once; they are also in .env)")


if __name__ == "__main__":
    main()
