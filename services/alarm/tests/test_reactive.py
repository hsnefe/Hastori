"""Reactive energy ratio rules and the sliding window behind them."""

import uuid
from collections.abc import Callable

import pytest

from hastori_alarm.engine import Engine, Phase, Rule, Sample, Transition
from hastori_alarm.reactive import MAX_GAP_S, MIN_WINDOW_KWH, ReactiveWindow

DEVICE = uuid.UUID(int=7)
T0 = 1_800_000_000.0
STEP = 2.0
RULE_ID = uuid.UUID(int=200)


def ratio_rule(**over: object) -> Rule:
    base: dict[str, object] = {
        "id": RULE_ID,
        "device_id": DEVICE,
        "name": "Pano reaktif oran",
        "kind": "reactive_ratio",
        "metric": "reactive_power_kvar",
        "operator": ">",
        "threshold": 0.18,
        "clear_threshold": 0.165,
        "duration_s": 0,
        "window_s": 600,
        "severity": "warning",
    }
    base.update(over)
    return Rule(**base)  # type: ignore[arg-type]


def stream(
    seconds: int, tan_phi: Callable[[float], float], p_kw: float = 100.0, start: float = T0
) -> list[Sample]:
    out = []
    for i in range(int(seconds / STEP)):
        t = i * STEP
        out.append(
            Sample(
                DEVICE,
                start + t,
                {"active_power_kw": p_kw, "reactive_power_kvar": p_kw * tan_phi(t)},
            )
        )
    return out


def feed_all(e: Engine, samples: list[Sample]) -> list[Transition]:
    out: list[Transition] = []
    for s in samples:
        out.extend(e.feed(s))
    return out


def engine() -> Engine:
    e = Engine()
    e.set_rules([ratio_rule()], now=T0)
    return e


# -- the window -------------------------------------------------------------------------------


def test_the_ratio_is_reactive_energy_over_active_energy() -> None:
    w = ReactiveWindow(600)
    last = None
    for i in range(400):
        last = w.add(T0 + i * STEP, 100.0, 14.0)
    assert last == pytest.approx(0.14)


def test_the_window_forgets_what_is_older_than_its_length() -> None:
    w = ReactiveWindow(600)
    for i in range(300):  # 10 min at 0.35
        w.add(T0 + i * STEP, 100.0, 35.0)
    ratio = None
    for i in range(300, 600):  # then 10 min at 0.14
        ratio = w.add(T0 + i * STEP, 100.0, 14.0)
    assert ratio == pytest.approx(0.14, abs=0.002)


def test_there_is_no_ratio_until_the_window_holds_enough_active_energy() -> None:
    w = ReactiveWindow(600)
    # 5 kW is 0.0014 kWh per 1 s: 2 kWh takes 400 samples
    seen = [w.add(T0 + i * STEP, 5.0, 5.0) for i in range(100)]
    assert all(r is None for r in seen)
    assert MIN_WINDOW_KWH == 2.0


def test_capacitive_power_does_not_lower_the_inductive_ratio() -> None:
    w = ReactiveWindow(600)
    ratio = None
    for i in range(400):
        q = 40.0 if i % 2 == 0 else -40.0  # half the time capacitive
        ratio = w.add(T0 + i * STEP, 100.0, q)
    assert ratio == pytest.approx(0.20)  # 40 over every second sample, not 0


def test_a_silent_stretch_adds_no_energy() -> None:
    w = ReactiveWindow(600)
    w.add(T0, 100.0, 14.0)
    w.add(T0 + 3600, 100.0, 14.0)  # an hour later
    assert w._sum_p == pytest.approx(100.0 * MAX_GAP_S)


# -- the rule ---------------------------------------------------------------------------------


def test_a_compensation_failure_warns_after_about_two_minutes_and_clears() -> None:
    e = engine()
    base = lambda t: 0.14  # noqa: E731
    out = feed_all(e, stream(900, base))  # 15 minutes of normal operation
    assert out == []

    start = T0 + 900
    failed = lambda t: 0.35 if t < 600 else 0.14  # noqa: E731  # 10 minutes of failure
    out = feed_all(e, stream(1500, failed, start=start))
    assert [t.kind for t in out] == ["opened", "cleared"]
    opened, cleared = out
    # ratio(t) = 0.14 + 0.21 * t / 600: it passes 0.18 after about 114 s
    assert 100 <= opened.ts - start <= 130
    assert opened.value > 0.18
    assert cleared.ts > start + 600  # it clears only after the failure has ended and aged out


def test_normal_tan_phi_up_to_the_top_of_its_range_never_warns() -> None:
    e = engine()
    out = feed_all(e, stream(3600, lambda t: 0.16))
    assert out == [] and e.phase_of(RULE_ID) is Phase.NORMAL


def test_a_quiet_night_is_not_evaluated() -> None:
    e = engine()
    out = feed_all(e, stream(1800, lambda t: 0.5, p_kw=3.0))  # 3 kW: under the 2 kWh floor
    assert out == []


def test_a_restart_replays_the_window_and_reaches_the_same_answer() -> None:
    samples = stream(300, lambda t: 0.14) + stream(900, lambda t: 0.35, start=T0 + 300)
    reference = feed_all(engine(), samples)
    assert [t.kind for t in reference] == ["opened"]

    fresh = engine()
    replayed = feed_all(fresh, samples)  # everything the database still has
    assert [(t.kind, t.ts) for t in replayed] == [(t.kind, t.ts) for t in reference]


def test_an_open_reactive_alarm_adopted_after_a_restart_clears_on_real_recovery() -> None:
    samples = stream(300, lambda t: 0.14) + stream(1200, lambda t: 0.35, start=T0 + 300)
    first = engine()
    out = feed_all(first, samples)
    opened_at = out[0].ts

    # the service restarts: the alarm is open in the database
    second = engine()
    second.adopt_open(RULE_ID, opened_at, peak=0.25)
    assert feed_all(second, samples) == []  # the same data does not close or reopen it
    recovery = stream(1200, lambda t: 0.14, start=T0 + 1500)
    out = feed_all(second, recovery)
    assert [t.kind for t in out] == ["cleared"]
