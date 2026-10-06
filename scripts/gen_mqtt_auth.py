"""Generate Mosquitto credentials and ACLs from seed/demo.yaml.

The password and ACL files go into the named volume `hastori_mosquitto-auth` with the owner and
mode Mosquitto wants (0700 for the password file), instead of a bind mount from the host where
the mode cannot be controlled on Windows. The plain-text intermediate exists only in a temporary
directory for the duration of the call. The healthcheck password is written to a file that
compose mounts as a secret, so it does not show up in `docker inspect`.
"""

import subprocess
import tempfile
from pathlib import Path

from hastori_common.seed_data import derive_device_password, load_seed
from hastori_common.settings import ROOT, get_settings

VOLUME = "hastori_mosquitto-auth"
HEALTH_PW_FILE = ROOT / "infra" / "mosquitto" / "health.pw"
IMAGE = "eclipse-mosquitto:2.1.2-alpine"


def main() -> None:
    settings = get_settings(
        strict=("mqtt_ingestion_password", "mqtt_health_password", "mqtt_device_secret")
    )
    seed = load_seed()

    users: dict[str, str] = {
        settings.mqtt_ingestion_user: settings.mqtt_ingestion_password,
        settings.mqtt_health_user: settings.mqtt_health_password,
    }
    acl: list[str] = [
        f"user {settings.mqtt_ingestion_user}",
        "topic read sites/+/devices/+/telemetry",
        "",
        f"user {settings.mqtt_health_user}",
        "topic read $SYS/broker/uptime",
        "",
    ]
    for dev in seed.devices:
        users[str(dev.id)] = derive_device_password(settings.mqtt_device_secret, dev.id)
        acl += [f"user {dev.id}", f"topic write {seed.topic(dev)}", ""]

    HEALTH_PW_FILE.write_text(settings.mqtt_health_password, encoding="utf-8", newline="\n")

    subprocess.run(["docker", "volume", "create", VOLUME], check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "passwd").write_text(
            "".join(f"{u}:{p}\n" for u, p in users.items()), encoding="utf-8", newline="\n"
        )
        (tmp_path / "acl").write_text("\n".join(acl), encoding="utf-8", newline="\n")
        # Hash in place with the broker's own tool, then fix owner and mode.
        script = (
            "cp /in/passwd /in/acl /out/ && chmod 0700 /out/passwd && chmod 0600 /out/acl"
            " && mosquitto_passwd -U /out/passwd"
            " && chown mosquitto:mosquitto /out/passwd /out/acl"
        )
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{tmp_path}:/in:ro", "-v", f"{VOLUME}:/out",
                "--entrypoint", "sh", IMAGE, "-c", script,
            ],
            check=True,
        )  # fmt: skip
    print(f"wrote {len(users)} users and the ACL to volume {VOLUME}")


if __name__ == "__main__":
    main()
