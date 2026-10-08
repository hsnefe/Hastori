import logging

import uvicorn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from hastori_api.app import create_app
from hastori_common.logging import configure_logging
from hastori_common.settings import get_settings


def main() -> None:
    settings = get_settings(strict=("database_url", "jwt_secret", "redis_url"))
    configure_logging(settings.log_level)
    engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={"command_timeout": 30},
    )
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        # The WebSocket hub sits on one pub/sub connection for days: a link that died silently
        # (a dropped network, a restarted Redis behind NAT) must be noticed, not waited on.
        health_check_interval=15,
        socket_keepalive=True,
    )
    app = create_app(settings, engine, redis, owns_resources=True)
    logging.getLogger("api").info("starting", extra={"port": settings.api_http_port})
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.api_http_port,
        log_level="warning",
        # The gateway's X-Forwarded-For carries the real client; believe it only from our proxies.
        # A client message is read whole before any size check: cap what uvicorn accepts (the
        # protocol allows 4 KiB, see MAX_CLIENT_MESSAGE).
        ws_max_size=8192,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
    )


if __name__ == "__main__":
    main()
