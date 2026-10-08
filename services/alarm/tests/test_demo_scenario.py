"""The demo, in virtual time: the real simulator model (day 1) feeding the real state machine with
the rules of seed/demo.yaml. These are the timings `make e2e` expects from the running stack, so
a change to the simulator, a rule or a constant that would break the demo fails here first."""

import uuid

import pytest

from hastori_alarm.engine import Engine, Rule, Sample, Transition
from hastori_common.seed_data import load_seed
from hastori_simulator.signals import DEFAULT_FAULT_S, DeviceModel, sample_site

SEED = load_seed()
STEP = 2.0
T0 = 1_800_000_000.0
HOUR = 10.0  # the factory runs its day shift


def engine_rule(seed_rule: object) -> Rule:
    r = seed_rule
    return Rule(
        id=r.id,  # type: ignore[attr-defined]
        device_id=SEED.device_by_key(r.device).id,  # type: ignore[attr-defined]
        name=r.name,  # type: ignore[attr-defined]
        kind=r.kind,  # type: ignore[attr-defined]
        metric=r.metric,  # type: ignore[attr-defined]
        operator=r.operator,  # type: ignore[attr-defined]
        threshold=r.threshold,  # type: ignore[attr-defined]
        clear_threshold=r.clear_threshold,  # type: ignore[attr-defined]
        duration_s=r.duration_s,  # type: ignore[attr-defined]
        window_s=r.window_s,  # type: ignore[attr-defined]
        severity=r.severity,  # type: ignore[attr-defined]
    )


class Site:
    """İzmir: the simulator models with the engine behind them."""

    def __init__(self, seed: int = 7) -> None:
        self.models = [
            DeviceModel("izmir-pano", "energy_analyzer", "izmir", seed),
            DeviceModel("izmir-komp-1", "compressor", "izmir", seed),
            DeviceModel("izmir-komp-2", "compressor", "izmir", seed),
            DeviceModel("izmir-sogutma-1", "chiller", "izmir", seed),
        ]
        self.now = T0
        self.engine = Engine()
        rules = [
            engine_rule(r) for r in SEED.alarm_rules if SEED.device_by_key(r.device).site == "izmir"
        ]
        self.engine.set_rules(rules, now=T0)
        self.transitions: list[Transition] = []
        self.series: dict[str, list[tuple[float, dict[str, float]]]] = {}

    def model(self, key: str) -> DeviceModel:
        return next(m for m in self.models if m.key == key)

    def run(self, seconds: float) -> None:
        for _ in range(int(seconds / STEP)):
            readings = sample_site(self.models, self.now, HOUR)
            for key, metrics in readings.items():
                self.series.setdefault(key, []).append((self.now, metrics))
                device = SEED.device_by_key(key).id
                self.transitions += self.engine.feed(Sample(device, self.now, metrics))
            self.now += STEP

    def first_above(self, key: str, metric: str, limit: float, since: float) -> float:
        return next(t for t, m in self.series[key] if t >= since and m[metric] > limit)


def kinds(site: Site, rule_id: uuid.UUID) -> list[str]:
    return [t.kind for t in site.transitions if t.rule.id == rule_id]


TEMP_RULE = next(
    r for r in SEED.alarm_rules if r.device == "izmir-komp-1" and r.kind == "threshold"
)
RATIO_RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-pano")


def test_normal_operation_raises_no_alarm_for_an_hour() -> None:
    site = Site()
    site.run(3600)
    assert site.transitions == []


def test_the_overheat_demo_opens_one_alarm_30_seconds_after_80_degrees_and_leaves_time_to_ack() -> (
    None
):
    site = Site()
    site.run(120)  # warm up
    fault_start = site.now
    site.model("izmir-komp-1").set_fault("overheat", fault_start, DEFAULT_FAULT_S)
    site.run(DEFAULT_FAULT_S + 120)

    assert kinds(site, TEMP_RULE.id) == ["opened", "cleared"]
    opened, cleared = site.transitions
    first_hot = site.first_above("izmir-komp-1", "temperature_c", 80.0, fault_start)
    assert 30.0 <= opened.ts - first_hot <= 32.5  # exactly the 30 s of the rule, to the sample
    # the 20 s ramp starts from the temperature of the moment (60-72 C), so 80 C comes after 8-18 s
    assert 8 <= first_hot - fault_start <= 18
    # the alarm stays open long enough to walk through the demo (acknowledge it on screen)
    assert cleared.ts - opened.ts >= 60
    assert cleared.peak > 84.0 and opened.rule.severity == "critical"


def test_a_spike_raises_nothing() -> None:
    site = Site()
    site.run(120)
    site.model("izmir-komp-1").set_fault("spike", site.now, 30)
    site.run(300)
    assert site.transitions == []
    assert max(m["temperature_c"] for _, m in site.series["izmir-komp-1"]) >= 90  # it happened


def test_the_compensation_failure_warns_after_a_couple_of_minutes_and_clears_when_it_ages_out() -> (
    None
):
    site = Site()
    site.run(900)  # a full window of normal operation
    assert site.transitions == []
    fault_start = site.now
    site.model("izmir-pano").set_fault("compensation_failure", fault_start, 600)
    site.run(600 + 1500)

    assert kinds(site, RATIO_RULE.id) == ["opened", "cleared"]
    opened, cleared = site.transitions
    # the e2e check allows 30-270 s; the model sits well inside
    assert 30 <= opened.ts - fault_start <= 270
    assert opened.rule.severity == "warning" and opened.value > 0.18
    # it clears only after the window has forgotten the failure (10 minutes), not at its end
    assert cleared.ts > fault_start + 600


def test_a_shorter_failure_is_forgotten_by_the_window_without_a_warning() -> None:
    site = Site()
    site.run(900)
    site.model("izmir-pano").set_fault("compensation_failure", site.now, 60)  # one minute only
    site.run(60 + 1200)
    assert kinds(site, RATIO_RULE.id) == []  # a minute of 0.35 averages out in a 10 minute window


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_overheat_timing_does_not_depend_on_the_noise(seed: int) -> None:
    site = Site(seed)
    site.run(120)
    start = site.now
    site.model("izmir-komp-1").set_fault("overheat", start, DEFAULT_FAULT_S)
    site.run(DEFAULT_FAULT_S + 120)
    assert kinds(site, TEMP_RULE.id) == ["opened", "cleared"]
    first_hot = site.first_above("izmir-komp-1", "temperature_c", 80.0, start)
    assert 30.0 <= site.transitions[0].ts - first_hot <= 32.5
