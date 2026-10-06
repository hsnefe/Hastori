"""Generate the Mosquitto password and ACL files from seed/demo.yaml."""

import subprocess
from pathlib import Path

from hastori_common.seed_data import derive_device_password, load_seed
from hastori_common.settings import get_settings

OUT = Path("infra/mosquitto")


def main() -> None:
    settings = get_settings()
    seed = load_seed()

    users: dict[str, str] = {
        settings.mqtt_ingestion_user: settings.mqtt_ingestion_password,
        settings.mqtt_health_user: settings.mqtt_health_password,
    }
    acl: list[str] = [
        f"user {settings.mqtt_ingestion_user}",
        "topic read $share/ingestion/sites/+/devices/+/telemetry",
        "topic read sites/+/devices/+/telemetry",
        "",
        f"user {settings.mqtt_health_user}",
        "topic read $SYS/broker/uptime",
        "",
    ]
    for dev in seed.devices:
        users[str(dev.id)] = derive_device_password(settings.mqtt_device_secret, dev.id)
        acl += [f"user {dev.id}", f"topic write {seed.topic(dev)}", ""]

    OUT.mkdir(parents=True, exist_ok=True)
    passwd = OUT / "passwd"
    passwd.write_text(
        "".join(f"{u}:{p}\n" for u, p in users.items()), encoding="utf-8", newline="\n"
    )
    (OUT / "acl").write_text("\n".join(acl), encoding="utf-8", newline="\n")

    # Hash in place with mosquitto_passwd -U inside the broker image.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{OUT.resolve()}:/work",
            "eclipse-mosquitto:2",
            "mosquitto_passwd",
            "-U",
            "/work/passwd",
        ],
        check=True,
    )
    print(f"wrote {len(users)} users to {OUT / 'passwd'} and ACL to {OUT / 'acl'}")


if __name__ == "__main__":
    main()
