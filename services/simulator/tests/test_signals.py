import math

from hastori_simulator.signals import (
    BOUNDS,
    DeviceModel,
    current_from_power,
    sample_site,
)

T0 = 1_700_000_000.0
HOUR = 10.0


def _site(seed: int = 1) -> list[DeviceModel]:
    return [
        DeviceModel("izmir-pano", "energy_analyzer", "izmir", seed),
        DeviceModel("izmir-komp-1", "compressor", "izmir", seed),
        DeviceModel("izmir-sogutma-1", "chiller", "izmir", seed),
    ]


def test_current_formula() -> None:
    # unity power factor: I = P*1000 / (sqrt(3)*400)
    assert math.isclose(current_from_power(10.0, 0.0), 10_000 / (math.sqrt(3) * 400))
    # lower power factor needs more current for the same kW
    assert current_from_power(10.0, 0.5) > current_from_power(10.0, 0.0)


def test_panel_equals_sum_of_others_plus_other_loads() -> None:
    devices = _site()
    out = sample_site(devices, T0, HOUR)
    others = out["izmir-komp-1"]["active_power_kw"] + out["izmir-sogutma-1"]["active_power_kw"]
    panel = out["izmir-pano"]["active_power_kw"]
    assert panel > others
    assert panel - others < 15.0  # lighting/other loads only (about 10 kW, plus noise)


def test_seed_is_reproducible() -> None:
    a = sample_site(_site(5), T0, HOUR)
    b = sample_site(_site(5), T0, HOUR)
    assert a == b


def test_overheat_crosses_80_within_30_seconds() -> None:
    devices = _site()
    comp = devices[1]
    now = T0
    for _ in range(10):
        sample_site(devices, now, HOUR)
        now += 2
    comp.set_fault("overheat", now, 40)
    crossed_at = None
    for _ in range(15):
        out = sample_site(devices, now, HOUR)
        if out[comp.key]["temperature_c"] > 80 and crossed_at is None:
            crossed_at = now - comp.fault.start if comp.fault else None
        now += 2
    assert crossed_at is not None and crossed_at <= 30


def test_overheat_recovers_below_75_after_duration() -> None:
    devices = _site()
    comp = devices[1]
    now = T0
    sample_site(devices, now, HOUR)
    comp.set_fault("overheat", now, 30)
    last = 0.0
    for _ in range(60):
        now += 2
        last = sample_site(devices, now, HOUR)[comp.key]["temperature_c"]
    assert last < 75


def test_spike_is_a_single_sample() -> None:
    devices = _site()
    comp = devices[1]
    now = T0
    sample_site(devices, now, HOUR)
    comp.set_fault("spike", now, 60)
    temps = []
    for _ in range(4):
        now += 2
        temps.append(sample_site(devices, now, HOUR)[comp.key]["temperature_c"])
    assert temps[0] == 90.0
    assert all(t < 80 for t in temps[1:])


def test_compensation_failure_raises_tan_phi() -> None:
    devices = _site()
    comp = devices[1]
    comp.set_fault("compensation_failure", T0, 60)
    out = sample_site(devices, T0 + 2, HOUR)[comp.key]
    assert math.isclose(out["reactive_power_kvar"] / out["active_power_kw"], 0.35, rel_tol=1e-6)


def test_offline_flag() -> None:
    comp = _site()[1]
    comp.set_fault("offline", T0, 30)
    assert comp.is_offline(T0 + 10)
    assert not comp.is_offline(T0 + 40)


def test_values_stay_within_physical_bounds() -> None:
    devices = _site()
    now = T0
    for i in range(2000):
        for metrics in sample_site(devices, now, (i / 100) % 24).values():
            for k, v in metrics.items():
                lo, hi = BOUNDS[k]
                assert math.isfinite(v) and lo <= v <= hi
        now += 2


def _longest_run_above(threshold: float, seed: int) -> float:
    """Seconds the default overheat fault keeps a compressor continuously above `threshold`."""
    from hastori_common.seed_data import load_seed
    from hastori_simulator.signals import DEFAULT_FAULT_S

    interval = 2.0
    models = _site(seed)
    t = T0
    for _ in range(60):  # warm up one minute so the start temperature is a real one
        sample_site(models, t, HOUR)
        t += interval
    comp = models[1]
    comp.set_fault("overheat", t, DEFAULT_FAULT_S)
    longest = run = 0
    end = t + DEFAULT_FAULT_S + 30
    while t < end:
        temp = sample_site(models, t, HOUR)["izmir-komp-1"]["temperature_c"]
        run = run + 1 if temp > threshold else 0
        longest = max(longest, run)
        t += interval
    assert load_seed()  # the seed file must stay loadable for the rule test below
    return (longest - 1) * interval


def test_default_overheat_trips_the_demo_rule() -> None:
    """The headline demo scenario: `make fault` must keep the temperature above the seeded
    rule's threshold for longer than its duration_s, with margin, or no alarm ever opens."""
    from hastori_common.seed_data import load_seed

    seed = load_seed()
    rule = next(r for r in seed.alarm_rules if r.metric == "temperature_c" and r.operator == ">")
    for s in range(5):
        above = _longest_run_above(rule.threshold, s)
        assert above >= rule.duration_s + 6, (s, above, rule.duration_s)


def test_compensation_failure_ends_with_its_duration() -> None:
    """The reactive-ratio alarm can only clear if the fault itself ends."""
    devices = _site()
    pano = devices[0]
    now = T0
    for _ in range(10):
        sample_site(devices, now, HOUR)
        now += 2
    pano.set_fault("compensation_failure", now, 20)

    def ratio(at: float) -> float:
        m = sample_site(devices, at, HOUR)["izmir-pano"]
        return m["reactive_power_kvar"] / m["active_power_kw"]

    during = [ratio(now + i * 2) for i in range(10)]  # first 20 s
    after = [ratio(now + 20 + i * 2) for i in range(10)]
    assert all(r > 0.3 for r in during)
    assert all(r < 0.2 for r in after)
