"""Prometheus metrics and request ids.

`GET /metrics` sits outside /api/v1 like /healthz: the gateway routes only /api/* to the API, so it
is reachable from the compose network (Prometheus) and 127.0.0.1:8000, never from the internet.
"""

import re
import time
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hastori_common.logging import request_id

REQUEST_ID_HEADER = "X-Request-ID"
# Caddy sends a UUID; anything else a client made up is replaced, not logged.
_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Probes and the scrape itself would drown the panel of real traffic.
_NOT_COUNTED = frozenset({"/metrics", "/healthz", "/readyz"})


class ApiMetrics:
    """One registry per application (tests build many applications in one process)."""

    def __init__(self, open_sockets: Callable[[], float]) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "api_requests",
            "HTTP requests answered, by route template",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "api_request_duration_seconds",
            "Seconds from request to the end of the response",
            ["method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        ws = Gauge("api_ws_connections", "Open live-update WebSockets", registry=self.registry)
        ws.set_function(open_sockets)


def route_template(scope: Scope) -> str:
    """The matched route with its parameters as placeholders: /api/v1/sites/{site_id}/devices.

    Built from the path itself: with nested routers FastAPI reports only the innermost router's
    part of it. Ids never become label values (one series per id would grow without bound).
    """
    if scope.get("route") is None:
        return "unmatched"
    names = {str(v): k for k, v in scope.get("path_params", {}).items()}
    return "/".join(f"{{{names[p]}}}" if p in names else p for p in scope["path"].split("/"))


class ObservabilityMiddleware:
    """Request id for logs and responses, request count and latency per route template.

    Plain ASGI, not BaseHTTPMiddleware: it must not buffer responses or touch WebSockets beyond
    tagging their log lines.
    """

    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        rid = incoming if incoming and _VALID_ID.match(incoming) else uuid.uuid4().hex
        # Not reset afterwards: every request runs in a task of its own, and the error handler
        # of last resort (outside this middleware) still logs with it.
        request_id.set(rid)
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        status = 500  # unless a response starts: an exception escaped
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = route_template(scope)
            if route not in _NOT_COUNTED:
                method = scope["method"]
                self.metrics.requests.labels(method, route, str(status)).inc()
                self.metrics.latency.labels(method, route).observe(time.perf_counter() - started)


router = APIRouter(tags=["health"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    registry = request.app.state.metrics.registry
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
