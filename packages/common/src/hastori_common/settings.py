import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root, so scripts work from any directory (no effect inside the containers,
# where configuration arrives through the environment).
ROOT = Path(__file__).resolve().parents[4]

DEFAULT_DEVICE_SECRET = "change-me-demo-device-secret"  # noqa: S105

log = logging.getLogger("settings")


class Settings(BaseSettings):
    """All service configuration, read from the environment / .env."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hastori:hastori_demo@127.0.0.1:5432/hastori"

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 8883
    mqtt_ca_file: str = str(ROOT / "infra" / "mosquitto" / "certs" / "ca.crt")
    mqtt_ingestion_user: str = "ingestion"
    mqtt_ingestion_password: str = "ingestion_demo"
    mqtt_health_user: str = "healthcheck"
    mqtt_health_password: str = "health_demo"
    mqtt_device_secret: str = Field(default=DEFAULT_DEVICE_SECRET, min_length=8)

    rabbitmq_url: str = "amqp://hastori:hastori_demo@127.0.0.1:5672/"

    seed_system_admin_password: str = "admin_demo_pw"
    seed_site_admin_password: str = "siteadmin_demo_pw"
    seed_viewer_password: str = "viewer_demo_pw"

    sim_seed: int = 42
    sim_control_port: int = 8002
    # Bearer token for the simulator's fault API (it can inject faults into every device).
    sim_control_token: str = "demo-control-token"  # noqa: S105
    ingest_http_port: int = 8001
    # Stable id: the MQTT session (and the messages queued for it) belongs to this id.
    ingest_client_id: str = "ingestion-1"
    ingest_shutdown_deadline_s: float = 20.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.mqtt_device_secret == DEFAULT_DEVICE_SECRET:
        log.warning("MQTT_DEVICE_SECRET is the public demo value; run `make env` for a random one")
    return settings
