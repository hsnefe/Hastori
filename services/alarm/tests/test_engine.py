"""The alarm state machine, driven with virtual time: no broker, no database."""

import uuid
from collections.abc import Iterable, Sequence

from hastori_alarm.engine import (
    CLEAR_HOLD_S,
    Engine,
    Phase,
    Rule,
    Sample,
    Transition,
)

DEVICE = uuid.UUID(int=1)
T0 = 1_800_000_000.0
STEP = 2.0  # devices publish every 2 s


def rule(**over: object) -> Rule:
    base: dict[str, object] = {
        "id": uuid.UUID(int=100),
        "device_id": DEVICE,
        "name": "Kompresör-1 yüksek sıcaklık",
        "kind": "threshold",
        "metric": "temperature_c",
        "operator": ">",
        "threshold": 80.0,
        "clear_threshold": 75.0,
        "duration_s": 30,
        "window_s": None,
        "severity": "critical",
    }
    base.update(over)
    return Rule(**base)  # type: ignore[arg-type]


def temps(values: Iterable[float], start: float = T0, step: float = STEP) -> list[Sample]:
    return [Sample(DEVICE, start + i * step, {"temperature_c": v}) for i, v in enumerate(values)]


def engine_with(*rules: Rule) -> Engine:
    e = Engine()
    e.set_rules(rules or (rule(),), now=T0)
    return e


def run(e: Engine, samples: Iterable[Sample]) -> list[Transition]:
    out: list[Transition] = []
    for s in samples:
        out.extend(e.feed(s))
    return out


def kinds(ts: Sequence[Transition]) -> list[str]:
    return [t.kind for t in ts]


# -- the demo scenario ------------------------------------------------------------------------


def test_overheat_opens_at_exactly_duration_and_clears_after_the_hold() -> None:
    e = engine_with()
    series = [70.0] * 5 + [85.0] * 25 + [70.0] * 12  # 50 s above, then recovery
    out = run(e, temps(series))
    assert kinds(out) == ["opened", "cleared"]
    opened, cleared = out
    first_above = T0 + 5 * STEP
    assert opened.ts == first_above + 30  # the 30th second, not a sample later
    assert opened.value == 85.0 and opened.peak == 85.0
    first_below = T0 + 30 * STEP
    assert cleared.ts == first_below + CLEAR_HOLD_S
    assert cleared.peak == 85.0 and not cleared.forced


def test_a_single_spike_never_alarms() -> None:
    e = engine_with()
    out = run(e, temps([70.0] * 5 + [90.0] + [70.0] * 30))
    assert out == []
    assert e.phase_of(rule().id) is Phase.NORMAL


def test_a_value_just_below_the_duration_does_not_alarm() -> None:
    e = engine_with()
    out = run(e, temps([85.0] * 15 + [70.0] * 5))  # 28 s above (15 samples span 28 s)
    assert out == []


# -- hysteresis and flapping ------------------------------------------------------------------


def test_a_value_hovering_at_the_threshold_never_opens() -> None:
    e = engine_with()
    out = run(e, temps([79.0, 81.0] * 100))  # crosses 80 every other sample
    assert out == []


def test_a_value_hovering_between_the_thresholds_keeps_the_alarm_open() -> None:
    e = engine_with()
    out = run(e, temps([85.0] * 20))
    assert kinds(out) == ["opened"]
    # 76 is back under the threshold but not under the clear threshold (75): still active
    assert run(e, temps([76.0] * 40, start=T0 + 100)) == []
    assert e.phase_of(rule().id) is Phase.ACTIVE
    # dipping below 75 and coming back before the hold is over does not clear it either
    assert run(e, temps([74.0, 74.0, 76.0] * 20, start=T0 + 200)) == []
    assert e.phase_of(rule().id) is Phase.ACTIVE


def test_the_alarm_clears_only_after_a_full_hold_below_the_clear_threshold() -> None:
    e = engine_with()
    run(e, temps([85.0] * 20))
    start = T0 + 100
    out = run(e, temps([70.0] * 5, start=start))  # 8 s below: not yet
    assert out == []
    out = run(e, temps([70.0] * 2, start=start + 10))
    assert kinds(out) == ["cleared"] and out[0].ts == start + CLEAR_HOLD_S


def test_peak_is_the_worst_value_of_the_excursion() -> None:
    e = engine_with()
    out = run(e, temps([82.0] * 5 + [91.0] + [83.0] * 20 + [70.0] * 8))
    assert kinds(out) == ["opened", "cleared"]
    assert out[0].peak == 91.0  # the spike during pending counts toward the peak
    assert out[1].peak == 91.0


# -- gaps -------------------------------------------------------------------------------------


def test_a_gap_restarts_the_duration() -> None:
    e = engine_with()
    # 20 s above, 12 s of silence, 20 s above: each stretch is shorter than 30 s
    first = temps([85.0] * 11)  # spans 20 s
    second = temps([85.0] * 11, start=T0 + 20 + 12)
    assert run(e, first + second) == []
    # the second stretch keeps counting from the sample after the gap and opens 30 s later
    more = temps([85.0] * 10, start=T0 + 32 + 22)
    out = run(e, more)
    assert kinds(out) == ["opened"] and out[0].ts == T0 + 32 + 30


def test_a_gap_while_clearing_restarts_the_hold() -> None:
    e = engine_with()
    run(e, temps([85.0] * 20))
    t = T0 + 100
    assert run(e, temps([70.0] * 4, start=t)) == []  # clearing, 6 s in
    out = run(e, temps([70.0] * 6, start=t + 6 + 30))  # after 30 s of silence
    assert kinds(out) == ["cleared"] and out[0].ts == t + 6 + 30 + CLEAR_HOLD_S


def test_an_open_alarm_stays_open_when_the_device_goes_silent() -> None:
    e = engine_with()
    run(e, temps([85.0] * 20))
    assert e.open_count() == 1  # no data, no conclusion: it does not close by itself


# -- ordering ---------------------------------------------------------------------------------


def test_a_duplicate_message_changes_nothing() -> None:
    once = run(engine_with(), temps([85.0] * 25 + [70.0] * 10))
    doubled = []
    for s in temps([85.0] * 25 + [70.0] * 10):
        doubled += [s, s]
    assert kinds(run(engine_with(), doubled)) == kinds(once)
    assert [t.ts for t in run(engine_with(), doubled)] == [t.ts for t in once]


def test_an_out_of_order_message_is_ignored() -> None:
    e = engine_with()
    samples = temps([85.0] * 25)
    late = Sample(DEVICE, T0 + 4, {"temperature_c": 70.0})  # old and cool
    out = run(e, samples[:20] + [late] + samples[20:])
    assert kinds(out) == ["opened"]


def test_a_device_clock_behind_by_minutes_gives_the_same_result() -> None:
    series = [70.0] * 5 + [85.0] * 25 + [70.0] * 12
    straight = run(engine_with(), temps(series))
    skewed = run(engine_with(), temps(series, start=T0 - 300))
    assert [(t.kind, t.ts + 300) for t in skewed] == [(t.kind, t.ts) for t in straight]


def test_a_clock_set_back_by_an_hour_does_not_blind_the_alarm() -> None:
    """B3: after the jump every sample is older than the newest one; the sequence restarts."""
    e = engine_with()
    assert run(e, temps([70.0] * 30)) == []
    after_jump = temps([85.0] * 25, start=T0 - 3600)  # the device clock went back one hour
    out = run(e, after_jump)
    assert kinds(out) == ["opened"] and e.clock_jumps == 1


def test_a_jump_restarts_a_half_counted_duration() -> None:
    e = engine_with()
    run(e, temps([85.0] * 10))  # 18 s pending
    out = run(e, temps([85.0] * 10, start=T0 - 600))  # 18 s again after the jump: not 36 s
    assert out == [] and e.phase_of(rule().id) is Phase.PENDING


def test_an_open_alarm_stays_open_across_a_jump_and_closes_on_the_new_timeline() -> None:
    e = engine_with()
    run(e, temps([85.0] * 25))
    assert e.phase_of(rule().id) is Phase.ACTIVE
    out = run(e, temps([70.0] * 8, start=T0 - 7200))
    assert kinds(out) == ["cleared"] and out[0].peak == 85.0


def test_an_adopted_alarm_is_not_ignored_after_a_jump_before_its_opening_time() -> None:
    e = engine_with()
    e.adopt_open(rule().id, T0 + 100, peak=88.0)  # floor_ts = T0 + 100
    run(e, temps([85.0] * 5, start=T0 + 200))
    out = run(e, temps([70.0] * 8, start=T0 - 3600))  # the new timeline is before floor_ts
    assert kinds(out) == ["cleared"]


def test_a_small_slip_is_still_dropped() -> None:
    e = engine_with()
    samples = temps([85.0] * 25)
    late = Sample(DEVICE, samples[-1].ts - 59.0, {"temperature_c": 70.0})
    assert run(e, [*samples, late]) == run(engine_with(), samples) and e.clock_jumps == 0


def test_exactly_sixty_seconds_behind_is_a_jump() -> None:
    e = engine_with()
    samples = temps([70.0] * 5)
    run(e, samples)
    behind = samples[-1].ts - 62.0  # the confirming sample lands exactly 60 s behind
    run(e, [Sample(DEVICE, behind, {"temperature_c": 70.0})])
    assert e.clock_jumps == 0  # one sample alone is only a candidate
    run(e, [Sample(DEVICE, behind + 2.0, {"temperature_c": 70.0})])
    assert e.clock_jumps == 1  # the next one continues from it: the clock really moved


def test_one_wrong_timestamp_does_not_wipe_a_pending_alarm() -> None:
    """A single bad reading must not restart the device's timeline: that would drop a
    half-counted duration."""
    e = engine_with()
    samples = temps([85.0] * 10)  # breaching, 20 s into a 30 s duration
    run(e, samples)
    assert e.phase_of(rule().id) is Phase.PENDING
    run(e, [Sample(DEVICE, samples[-1].ts - 3600.0, {"temperature_c": 20.0})])
    follow = temps([85.0] * 10, start=samples[-1].ts + 2.0)
    out = run(e, follow)
    assert e.clock_jumps == 0
    assert any(t.kind == "opened" for t in out)  # the count carried on and completed


def test_two_unrelated_old_timestamps_are_not_a_jump() -> None:
    e = engine_with()
    samples = temps([70.0] * 10)
    run(e, samples)
    run(e, [Sample(DEVICE, samples[-1].ts - 3600.0, {"temperature_c": 70.0})])
    run(e, [Sample(DEVICE, samples[-1].ts - 7200.0, {"temperature_c": 70.0})])
    assert e.clock_jumps == 0


def test_a_redelivered_old_message_is_a_duplicate_not_a_jump() -> None:
    e = engine_with()
    samples = temps([70.0] * 100)
    run(e, samples)
    run(e, samples[:5])  # a redelivery of readings seen 3 minutes ago
    assert e.clock_jumps == 0


def test_a_backlog_older_than_the_replay_is_not_a_jump() -> None:
    """After a long alarm-service outage the queue holds messages the replay did not cover."""
    e = engine_with()
    run(e, temps([70.0] * 100, start=T0 + 1000))
    e.history_floor(T0 + 1000)
    run(e, temps([85.0] * 40, start=T0))  # queued before the replay window began
    assert e.clock_jumps == 0 and e.phase_of(rule().id) is Phase.NORMAL
    e.mark_live(DEVICE)  # a live reading arrived: now an old timestamp is a clock set back
    run(e, temps([85.0] * 40, start=T0 - 3600))
    assert e.clock_jumps == 1


def test_the_jump_restarts_a_reactive_window() -> None:
    e = engine_with(
        rule(
            id=uuid.UUID(int=101),
            kind="reactive_ratio",
            metric="reactive_power_kvar",
            threshold=0.18,
            clear_threshold=0.165,
            duration_s=0,
            window_s=600,
        )
    )
    run(
        e,
        [
            Sample(DEVICE, T0 + i * 2, {"active_power_kw": 10.0, "reactive_power_kvar": 1.0})
            for i in range(40)
        ],
    )
    assert e.clock_jumps == 0
    for i in range(2):  # a candidate and the sample that confirms it
        run(
            e,
            [
                Sample(
                    DEVICE, T0 - 3600 + i * 2, {"active_power_kw": 10.0, "reactive_power_kvar": 1.0}
                )
            ],
        )
    assert e.clock_jumps == 1


# -- restart ----------------------------------------------------------------------------------


def test_a_restart_in_the_middle_of_pending_gives_the_same_transitions() -> None:
    """The service replays the last minutes from the database into a fresh engine."""
    series = temps([70.0] * 5 + [85.0] * 25 + [70.0] * 12)
    reference = run(engine_with(), series)

    for cut in range(1, len(series)):  # a restart after every possible sample
        before = run(engine_with(), series[:cut])
        replayed = engine_with()
        after = run(replayed, series)  # replay everything the database still has, then live
        cut_ts = series[cut - 1].ts
        combined = before + [t for t in after if t.ts > cut_ts]
        assert [(t.kind, t.ts) for t in combined] == [(t.kind, t.ts) for t in reference], cut


def test_an_alarm_that_is_open_in_the_database_is_not_closed_by_older_data() -> None:
    e = engine_with()
    opened_at = T0 + 100
    e.adopt_open(rule().id, opened_at, peak=88.0)
    # the replay window starts before the alarm opened and shows normal values then
    out = run(e, temps([70.0] * 20, start=T0))  # all before opened_at
    assert out == [] and e.phase_of(rule().id) is Phase.ACTIVE
    out = run(e, temps([70.0] * 8, start=opened_at + 20))  # real recovery afterwards
    assert kinds(out) == ["cleared"] and out[0].peak == 88.0


# -- the other operator -----------------------------------------------------------------------


def test_less_than_rules_mirror_the_greater_than_rules() -> None:
    low = rule(metric="current_a", operator="<", threshold=10.0, clear_threshold=15.0)
    e = Engine()
    e.set_rules([low], now=T0)
    series = [30.0] * 3 + [5.0] * 25 + [20.0] * 8
    samples = [Sample(DEVICE, T0 + i * STEP, {"current_a": v}) for i, v in enumerate(series)]
    out = run(e, samples)
    assert kinds(out) == ["opened", "cleared"]
    assert out[0].peak == 5.0  # the worst value is the lowest one


def test_samples_without_the_metric_are_skipped() -> None:
    e = engine_with()
    samples = [Sample(DEVICE, T0 + i * STEP, {"current_a": 1.0}) for i in range(40)]
    assert run(e, samples) == []


# -- rule changes -----------------------------------------------------------------------------


def test_changing_the_threshold_during_pending_restarts_the_count() -> None:
    e = engine_with()
    run(e, temps([85.0] * 10))  # 18 s into the 30 s
    e.set_rules([rule(threshold=82.0)], now=T0 + 20)
    out = run(e, temps([85.0] * 20, start=T0 + 20))
    assert kinds(out) == ["opened"] and out[0].ts == T0 + 20 + 30


def test_renaming_a_rule_does_not_touch_its_state() -> None:
    e = engine_with()
    run(e, temps([85.0] * 10))
    e.set_rules([rule(name="Another name", severity="warning")], now=T0 + 20)
    out = run(e, temps([85.0] * 10, start=T0 + 20))
    assert kinds(out) == ["opened"] and out[0].ts == T0 + 30  # the count was never reset
    assert out[0].rule.severity == "warning"


def test_removing_a_rule_closes_its_open_alarm() -> None:
    e = engine_with()
    run(e, temps([85.0] * 25))
    out = e.set_rules([], now=T0 + 500)
    assert len(out) == 1 and out[0].kind == "cleared" and out[0].forced
    assert out[0].ts == T0 + 500 and e.open_count() == 0


def test_removing_a_rule_that_never_alarmed_yields_nothing() -> None:
    e = engine_with()
    run(e, temps([85.0] * 5))
    assert e.set_rules([], now=T0 + 100) == []


def test_changing_the_threshold_of_an_open_alarm_keeps_it_open() -> None:
    e = engine_with()
    run(e, temps([85.0] * 25))
    assert e.set_rules([rule(threshold=90.0, clear_threshold=88.0)], now=T0 + 100) == []
    assert e.phase_of(rule().id) is Phase.ACTIVE


def test_two_devices_do_not_interfere() -> None:
    other = uuid.UUID(int=2)
    r2 = rule(id=uuid.UUID(int=101), device_id=other)
    e = Engine()
    e.set_rules([rule(), r2], now=T0)
    hot = temps([85.0] * 25)
    cool = [Sample(other, s.ts, {"temperature_c": 60.0}) for s in hot]
    out = run(e, [x for pair in zip(hot, cool, strict=True) for x in pair])
    assert kinds(out) == ["opened"] and out[0].rule.id == rule().id
