import logging
import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root, so scripts work from any directory (no effect inside the containers,
# where configuration arrives through the environment).
ROOT = Path(__file__).resolve().parents[4]

DEFAULT_DEVICE_SECRET = "change-me-demo-device-secret"  # noqa: S105
DEMO_CONTROL_TOKEN = "demo-control-token"  # noqa: S105
DEMO_JWT_SECRET = "change-me-demo-jwt-secret-0123456789abcdef"  # noqa: S105

log = logging.getLogger("settings")


class Settings(BaseSettings):
    """All service configuration, read from the environment / .env."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hastori:hastori_demo@127.0.0.1:5432/hastori"

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 8883
    # Relative paths are resolved against the repository root, so scripts work from any directory.
    mqtt_ca_file: str = str(ROOT / "infra" / "mosquitto" / "certs" / "ca.crt")
    mqtt_ingestion_user: str = "ingestion"
    mqtt_ingestion_password: str = "ingestion_demo"
    mqtt_health_user: str = "healthcheck"
    mqtt_health_password: str = "health_demo"
    mqtt_device_secret: str = Field(default=DEFAULT_DEVICE_SECRET, min_length=8)

    rabbitmq_url: str = "amqp://hastori:hastori_demo@127.0.0.1:5672/"
    redis_url: str = "redis://:redis_demo@127.0.0.1:6379/0"

    # Signs the API's access tokens (HS256): anyone who knows it can mint a token for any user.
    jwt_secret: str = Field(default=DEMO_JWT_SECRET, min_length=32)
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 7 * 24 * 3600
    # An already used refresh token still works this long (parallel tabs); later it means theft.
    refresh_grace_s: int = 20
    # Secure cookies need HTTPS (browsers exempt localhost); switch on behind the gateway.
    cookie_secure: bool = False

    seed_system_admin_password: str = "admin_demo_pw"
    seed_site_admin_password: str = "siteadmin_demo_pw"
    seed_viewer_password: str = "viewer_demo_pw"

    sim_seed: int = 42
    sim_control_port: int = 8002
    # Bearer token for the simulator's fault API (it can inject faults into every device).
    sim_control_token: str = DEMO_CONTROL_TOKEN
    ingest_http_port: int = 8001
    alarm_http_port: int = 8003
    api_http_port: int = 8000
    # Stable id: the MQTT session (and the messages queued for it) belongs to this id.
    ingest_client_id: str = "ingestion-1"
    ingest_shutdown_deadline_s: float = 20.0
    alarm_shutdown_deadline_s: float = 20.0
    log_level: str = "INFO"

    @field_validator("mqtt_ca_file")
    @classmethod
    def _absolute_ca_path(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else ROOT / path)


# Values that ship in the code or in .env.example. A service that is meant to run for real must
# not start with them just because an environment variable went missing.
_DEMO_VALUES: dict[str, tuple[str, ...]] = {
    "database_url": ("hastori_demo",),
    "mqtt_ingestion_password": ("ingestion_demo",),
    "mqtt_health_password": ("health_demo",),
    "mqtt_device_secret": (DEFAULT_DEVICE_SECRET,),
    "rabbitmq_url": ("hastori_demo",),
    "redis_url": ("redis_demo",),
    "jwt_secret": (DEMO_JWT_SECRET,),
    "sim_control_token": (DEMO_CONTROL_TOKEN,),
}


def check_secrets(settings: Settings, fields: Iterable[str]) -> None:
    """Raise if any of `fields` still holds a published demo value.

    `ALLOW_DEMO_SECRETS=1` switches the check off (throwaway local runs and tests).
    """
    if os.environ.get("ALLOW_DEMO_SECRETS") == "1":
        return
    bad = [
        f
        for f in fields
        if any(demo in str(getattr(settings, f)) for demo in _DEMO_VALUES.get(f, ()))
    ]
    if bad:
        raise RuntimeError(
            f"refusing to start with the published demo value for: {', '.join(bad)}. "
            "Run `make env` to generate a .env with random secrets "
            "(or set ALLOW_DEMO_SECRETS=1 for a throwaway run)."
        )


@lru_cache
def get_settings(strict: tuple[str, ...] = ()) -> Settings:
    """Settings from the environment. `strict` names the secrets this process really needs:
    it refuses to start if one still has a demo value."""
    settings = Settings()
    check_secrets(settings, strict)
    return settings
