from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All service configuration, read from the environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hastori:hastori_demo@127.0.0.1:5432/hastori"

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 8883
    mqtt_ca_file: str = "infra/mosquitto/certs/ca.crt"
    mqtt_ingestion_user: str = "ingestion"
    mqtt_ingestion_password: str = "ingestion_demo"
    mqtt_health_user: str = "healthcheck"
    mqtt_health_password: str = "health_demo"
    mqtt_device_secret: str = Field(default="change-me-demo-device-secret", min_length=8)

    rabbitmq_url: str = "amqp://hastori:hastori_demo@127.0.0.1:5672/"

    seed_system_admin_password: str = "admin_demo_pw"
    seed_site_admin_password: str = "siteadmin_demo_pw"
    seed_viewer_password: str = "viewer_demo_pw"

    sim_seed: int = 42
    sim_control_port: int = 8002
    ingest_http_port: int = 8001
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
