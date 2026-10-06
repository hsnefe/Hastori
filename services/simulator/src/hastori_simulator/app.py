"""Simulator runtime: one MQTT connection per device plus a fault control API."""

import asyncio
import hmac
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiomqtt
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from hastori_common.logging import configure_logging
from hastori_common.seed_data import SeedData, derive_device_password, load_seed
from hastori_common.settings import Settings, get_settings
from hastori_simulator.signals import FAULT_KINDS, DeviceModel, FaultKind, sample_site

log = logging.getLogger("simulator")

INTERVAL_S = 2.0
TZ = ZoneInfo("Europe/Istanbul")
SITE_PROFILES = {"izmir": "factory", "antalya": "hotel"}
BACKOFF_MAX_S = 30.0


class FaultRequest(BaseModel):
    device: str
    kind: str
    duration_s: float = Field(default=40, gt=0, le=3600)


class Simulator:
    def __init__(self, seed: SeedData, settings: Settings) -> None:
        self.seed = seed
        self.settings = settings
        self.models: dict[str, DeviceModel] = {}
        self.by_site: dict[str, list[DeviceModel]] = {}
        for d in seed.devices:
            profile = SITE_PROFILES.get(d.site, "factory")
            m = DeviceModel(d.key, d.type, d.site, settings.sim_seed, profile)
            self.models[d.key] = m
            self.by_site.setdefault(d.site, []).append(m)
        self.queues: dict[str, asyncio.Queue[bytes]] = {
            d.key: asyncio.Queue(maxsize=5) for d in seed.devices
        }

    def tick(self, now: float) -> None:
        local = datetime.fromtimestamp(now, TZ)
        local_hour = local.hour + local.minute / 60
        for models in self.by_site.values():
            readings = sample_site(models, now, local_hour)
            for key, metrics in readings.items():
                if self.models[key].is_offline(now):
                    continue
                payload = json.dumps({"ts": round(now, 3), "metrics": metrics}).encode()
                q = self.queues[key]
                if q.full():
                    q.get_nowait()  # drop oldest while the broker is unreachable
                q.put_nowait(payload)

    async def tick_loop(self) -> None:
        next_t = time.monotonic()
        while True:
            self.tick(time.time())
            next_t += INTERVAL_S
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def device_loop(self, key: str) -> None:
        dev = self.seed.device_by_key(key)
        topic = self.seed.topic(dev)
        password = derive_device_password(self.settings.mqtt_device_secret, dev.id)
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt_host,
                    port=self.settings.mqtt_port,
                    username=str(dev.id),
                    password=password,
                    identifier=f"sim-{dev.id}",
                    protocol=aiomqtt.ProtocolVersion.V5,
                    tls_params=aiomqtt.TLSParameters(ca_certs=self.settings.mqtt_ca_file),
                ) as client:
                    log.info("connected", extra={"device": key})
                    backoff = 1.0
                    while True:
                        payload = await self.queues[key].get()
                        await client.publish(topic, payload, qos=1)
            except aiomqtt.MqttError as exc:
                log.warning(
                    "mqtt error, reconnecting",
                    extra={"device": key, "error": str(exc), "retry_in_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)


def build_api(sim: Simulator, token: str) -> FastAPI:
    def require_token(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "missing or wrong bearer token")

    api = FastAPI(title="Hastori simulator control", dependencies=[Depends(require_token)])

    def _status(key: str) -> dict[str, object]:
        f = sim.models[key].fault
        if f is None:
            return {}
        return {
            "device": key,
            "kind": f.kind,
            "started_at": f.start,
            "duration_s": f.duration_s,
            "remaining_s": max(0.0, f.start + f.duration_s - time.time()),
        }

    @api.post("/faults")
    def create_fault(req: FaultRequest) -> dict[str, object]:
        if req.device not in sim.models:
            raise HTTPException(404, f"unknown device {req.device!r}")
        if req.kind not in FAULT_KINDS:
            raise HTTPException(422, f"kind must be one of {list(FAULT_KINDS)}")
        kind: FaultKind = req.kind  # type: ignore[assignment]
        sim.models[req.device].set_fault(kind, time.time(), req.duration_s)
        log.info("fault set", extra={"device": req.device, "kind": kind})
        return _status(req.device)

    @api.get("/faults")
    def list_faults() -> list[dict[str, object]]:
        return [s for k in sim.models if (s := _status(k))]

    @api.delete("/faults/{device}")
    def delete_fault(device: str) -> dict[str, str]:
        if device not in sim.models:
            raise HTTPException(404, f"unknown device {device!r}")
        sim.models[device].clear_fault()
        return {"cleared": device}

    return api


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    sim = Simulator(load_seed(), settings)
    # Bind all interfaces inside the container; compose publishes the port on 127.0.0.1 only.
    config = uvicorn.Config(
        build_api(sim, settings.sim_control_token),
        host="0.0.0.0",
        port=settings.sim_control_port,
        log_level="warning",
    )
    tasks = [
        asyncio.create_task(sim.tick_loop()),
        asyncio.create_task(uvicorn.Server(config).serve()),
    ]
    tasks += [asyncio.create_task(sim.device_loop(d.key)) for d in sim.seed.devices]
    log.info("simulator started", extra={"devices": len(sim.seed.devices)})
    await asyncio.gather(*tasks)
