"""The synthetic history generator (scripts/backfill.py): pure parts, no database."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from conftest import load_script
from hastori_common.seed_data import load_seed

backfill = load_script("backfill")
SEED = load_seed()
START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # a Thursday, 03:00 in Istanbul


def rows(hours: int, sim_seed: int = 7) -> list[Any]:
    return list(backfill.generate(SEED, sim_seed, START, START + timedelta(hours=hours)))


def test_one_reading_per_device_metric_and_minute() -> None:
    got = rows(1)
    assert len(got) == len(SEED.devices) * 4 * 60
    keys = {(r.device_id, r.metric, r.time) for r in got}
    assert len(keys) == len(got)  # the unique index on (device, metric, time) would hold
    assert {r.time for r in got} == {START + timedelta(minutes=m) for m in range(60)}


def test_deterministic_for_a_seed_and_different_between_seeds() -> None:
    assert rows(2, 7) == rows(2, 7)
    assert rows(2, 7) != rows(2, 8)


def test_the_hotel_peaks_in_the_evening_and_the_factory_in_the_shift() -> None:
    day = list(backfill.generate(SEED, 7, START, START + timedelta(days=1)))

    def kwh(site_key: str, hour_from: int, hour_to: int) -> float:
        analyzer = next(
            d.id for d in SEED.devices if d.site == site_key and d.type == "energy_analyzer"
        )
        local = [
            r.value
            for r in day
            if r.device_id == analyzer
            and r.metric == "active_power_kw"
            and hour_from <= r.time.astimezone(backfill.TZ).hour < hour_to
        ]
        return float(sum(local) / len(local))

    assert kwh("izmir", 9, 17) > kwh("izmir", 1, 6) * 1.2  # shift vs night
    assert kwh("antalya", 19, 22) > kwh("antalya", 3, 6)  # evening peak vs night


def test_values_stay_inside_physical_bounds_and_never_look_like_a_fault() -> None:
    got = rows(24)
    assert all(r.value >= 0 for r in got if r.metric != "temperature_c")
    assert max(r.value for r in got if r.metric == "temperature_c") < 80.0  # no alarm history


def test_window_stops_where_real_data_starts() -> None:
    now = datetime(2026, 10, 9, 12, 34, 56, tzinfo=UTC)
    start, end = backfill.window(now, 7, None)
    assert (start, end) == (datetime(2026, 10, 2, 12, 34, tzinfo=UTC), now.replace(second=0))
    real = datetime(2026, 10, 9, 10, 0, 30, tzinfo=UTC)
    assert backfill.window(now, 7, real)[1] == real.replace(second=0)


@pytest.mark.parametrize("days", [0, 8])
async def test_more_than_the_retention_is_refused(days: int) -> None:
    with pytest.raises(SystemExit):
        await backfill.run(days)
