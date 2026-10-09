"""Synthetic history for the demo: DAYS of one-minute readings from the simulator's own model.

A fresh demo has seconds of data, so the "last 7 days" energy card would be empty. This writes
the same signals the live simulator produces (`hastori_simulator.signals`, so the daily shape of
the factory and the hotel is the real one) into `measurements`, one reading per device and metric
every minute, and then refreshes the 1-minute continuous aggregate for exactly that window.

- The data is synthetic and is labelled so in the README; it never opens alarms (no faults are
  injected, nothing goes through the outbox).
- It stops where real data starts (or at the start of the current minute) and never overwrites:
  rows that exist stay (ON CONFLICT DO NOTHING).
- Raw data is kept 7 days (migration 0002): asking for more is refused, and the oldest partial
  day may be dropped again by the retention job, the aggregate keeps it.
- The refresh always has both bounds. `refresh_continuous_aggregate('measurements_1m', NULL, NULL)`
  would rebuild the aggregate from the raw data that is left and delete older materialised
  minutes (risk D1).
"""

import argparse
import asyncio
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from hastori_common.seed_data import SeedData, load_seed
from hastori_common.settings import get_settings
from hastori_simulator.signals import DeviceModel, sample_site

MAX_DAYS = 7
STEP_S = 60
BATCH_ROWS = 50_000
SITE_PROFILES = {"izmir": "factory", "antalya": "hotel"}  # as in the simulator
TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class Row:
    time: datetime
    device_id: UUID
    metric: str
    value: float


def generate(seed: SeedData, sim_seed: int, start: datetime, end: datetime) -> Iterator[Row]:
    """Readings for [start, end) every STEP_S seconds, in time order. Deterministic."""
    by_site: dict[str, list[DeviceModel]] = {}
    for d in seed.devices:
        profile = SITE_PROFILES.get(d.site, "factory")
        by_site.setdefault(d.site, []).append(DeviceModel(d.key, d.type, d.site, sim_seed, profile))
    ids = {d.key: d.id for d in seed.devices}
    t = start
    while t < end:
        local = t.astimezone(TZ)
        hour = local.hour + local.minute / 60
        for models in by_site.values():
            for key, metrics in sample_site(models, t.timestamp(), hour).items():
                for metric, value in metrics.items():
                    yield Row(t, ids[key], metric, value)
        t += timedelta(seconds=STEP_S)


def window(now: datetime, days: int, first_real: datetime | None) -> tuple[datetime, datetime]:
    """Whole minutes from `days` ago up to the start of real data (or of this minute)."""
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    if first_real is not None:
        end = min(end, first_real.astimezone(UTC).replace(second=0, microsecond=0))
    start = (now - timedelta(days=days)).astimezone(UTC).replace(second=0, microsecond=0)
    return start, end


async def run(days: int) -> None:
    if not 1 <= days <= MAX_DAYS:
        raise SystemExit(f"DAYS must be 1..{MAX_DAYS}: raw data is kept {MAX_DAYS} days")
    settings = get_settings(strict=("database_url",))
    seed = load_seed()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        first_real = await conn.fetchval("SELECT min(time) FROM measurements")
        start, end = window(datetime.now(UTC), days, first_real)
        if end <= start:
            print("nothing to write: real data already covers the period")
            return
        written = 0
        batch: list[Row] = []

        async def flush() -> int:
            if not batch:
                return 0
            status = await conn.execute(
                "INSERT INTO measurements (time, device_id, metric, value) "
                "SELECT * FROM unnest($1::timestamptz[], $2::uuid[], $3::text[], $4::float8[]) "
                "ON CONFLICT DO NOTHING",
                [r.time for r in batch],
                [r.device_id for r in batch],
                [r.metric for r in batch],
                [r.value for r in batch],
            )
            batch.clear()
            return int(status.split()[-1])

        for row in generate(seed, settings.sim_seed, start, end):
            batch.append(row)
            if len(batch) >= BATCH_ROWS:
                written += await flush()
        written += await flush()
        # Both bounds, always (see the module docstring). CALL runs outside a transaction here.
        await conn.execute(
            "CALL refresh_continuous_aggregate("
            "'measurements_1m', $1::timestamptz, $2::timestamptz)",
            start,
            end,
        )
        minutes = math.ceil((end - start).total_seconds() / 60)
        print(f"backfilled {written} readings, {minutes} minutes: {start:%F %R} .. {end:%F %R} UTC")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=MAX_DAYS)
    asyncio.run(run(parser.parse_args().days))


if __name__ == "__main__":
    main()
