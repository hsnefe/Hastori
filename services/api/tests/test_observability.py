"""/metrics and request ids."""

import json
import logging
import re

import pytest

from conftest import ApiHarness
from hastori_common.logging import JsonFormatter


def sample(text: str, name: str, **labels: str) -> float | None:
    """Value of one sample in Prometheus text format (labels must match exactly)."""
    for line in text.splitlines():
        m = re.match(r"^(\w+)(?:\{(.*)\})? (\S+)$", line)
        if not m or m.group(1) != name:
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
        if found == labels:
            return float(m.group(3))
    return None


async def scrape(api: ApiHarness) -> str:
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://test"
    ) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


async def test_requests_are_counted_by_route_template_not_by_path(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_viewer")
    sites = (await c.get("/sites")).json()
    for site in sites:
        assert (await c.get(f"/sites/{site['id']}/devices")).status_code == 200
    assert (await c.get("/no-such-thing")).status_code == 404

    text = await scrape(api)
    route = "/api/v1/sites/{site_id}/devices"
    count = sample(text, "api_requests_total", method="GET", route=route, status="200")
    assert count == len(sites)
    assert sample(text, "api_requests_total", method="GET", route="unmatched", status="404") == 1
    assert sample(text, "api_request_duration_seconds_count", method="GET", route=route) == len(
        sites
    )
    assert str(sites[0]["id"]) not in text  # no unbounded label values
    assert sample(text, "api_requests_total", method="GET", route="/metrics", status="200") is None


async def test_open_websockets_are_a_gauge(api: ApiHarness) -> None:
    import uuid

    assert sample(await scrape(api), "api_ws_connections") == 0
    api.app.state.ws_open[uuid.uuid4()] = 2
    assert sample(await scrape(api), "api_ws_connections") == 2


async def test_the_gateway_request_id_is_echoed(api: ApiHarness) -> None:
    c = api.client()
    rid = "0b7c9a52-3f0e-4c55-9a43-1d2f6a7b8c9d"
    r = await c.get("/sites", headers={"X-Request-ID": rid})
    assert r.status_code == 401
    assert r.headers["X-Request-ID"] == rid


@pytest.mark.parametrize("sent", [None, "x", "a b c d e f g h", "<script>" * 3, "a" * 65])
async def test_a_missing_or_odd_request_id_is_replaced(api: ApiHarness, sent: str | None) -> None:
    c = api.client()
    headers = {"X-Request-ID": sent} if sent is not None else {}
    r = await c.get("/sites", headers=headers)
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["X-Request-ID"])


class JsonLines(logging.Handler):
    """Formats as the services do, at the moment of logging."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


async def test_log_lines_of_a_request_carry_its_id(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hastori_api.queries import sites

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sites, "list_sites", boom)
    import httpx

    signed_in = await api.signed_in("izmir_viewer")
    # The error handler of last resort answers, then re-raises for the server to log.
    transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
    c = httpx.AsyncClient(transport=transport, base_url="http://test/api/v1")
    c.headers["Authorization"] = signed_in.headers["Authorization"]
    rid = "11111111-2222-3333-4444-555555555555"
    handler = JsonLines()
    logger = logging.getLogger("api")
    logger.addHandler(handler)
    try:
        r = await c.get("/sites", headers={"X-Request-ID": rid})
    finally:
        logger.removeHandler(handler)
        await c.aclose()
    assert r.status_code == 500
    assert r.headers["X-Request-ID"] == rid
    errors = [line for line in handler.lines if line["msg"] == "unhandled error"]
    assert errors and errors[-1]["request_id"] == rid
