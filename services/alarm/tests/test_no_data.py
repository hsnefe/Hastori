"""no_data rules: a device that stopped reporting. Virtual time, like the engine tests."""

import uuid

from hastori_alarm.engine import (
    CLEAR_HOLD_S,
    PIPELINE_GAP_S,
    Engine,
    Phase,
    Rule,
    Sample,
    Transition,
)

SILENT = uuid.UUID(int=1)  # the device under test
OTHER = uuid.UUID(int=2)  # a neighbour that keeps the pipeline visibly alive
RULE_ID = uuid.UUID(int=100)
T0 = 1_800_000_000.0
LIMIT = 60


def no_data_rule(**over: object) -> Rule:
    base: dict[str, object] = {
        "id": RULE_ID,
        "device_id": SILENT,
        "name": "Kompresör-1 veri yok",
        "kind": "no_data",
        "metric": "temperature_c",
        "operator": ">",
        "threshold": 0.0,
        "clear_threshold": 0.0,
        "duration_s": LIMIT,
        "window_s": None,
        "severity": "warning",
    }
    base.update(over)
    return Rule(**base)  # type: ignore[arg-type]


def engine(**over: object) -> Engine:
    e = Engine()
    e.set_rules([no_data_rule(**over)], now=T0)
    return e


def reading(device: uuid.UUID, at: float, **metrics: float) -> Sample:
    """A sample that arrives at `at` and carries the same time as its device timestamp."""
    return Sample(device, at, metrics or {"temperature_c": 70.0})


def stream(
    e: Engine, device: uuid.UUID, start: float, end: float, step: float = 2.0
) -> list[Transition]:
    """Samples every `step` seconds in [start, end), each ticked as the service would."""
    out: list[Transition] = []
    t = start
    while t < end:
        out += e.feed(reading(device, t), arrived=t)
        out += e.tick(t)
        t += step
    return out


def names(ts: list[Transition]) -> list[str]:
    return [t.kind for t in ts]


# -- opening ------------------------------------------------------------------------------------


def test_a_device_that_goes_quiet_opens_the_alarm_when_the_limit_is_crossed() -> None:
    e = engine()
    assert stream(e, SILENT, T0, T0 + 20) == []  # alive
    last = T0 + 18
    out = stream(e, OTHER, T0 + 20, T0 + 200)  # the neighbour carries on, the device does not
    assert names(out) == ["opened"]
    opened = out[0]
    assert opened.ts == last + LIMIT  # when the limit was crossed, not when we noticed
    assert LIMIT <= opened.value <= LIMIT + 5  # the silence so far
    assert e.phase_of(RULE_ID) is Phase.ACTIVE
    assert e.open_count() == 1


def test_the_alarm_does_not_open_before_the_limit() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 10)
    out = stream(e, OTHER, T0 + 10, T0 + 8 + LIMIT - 1)
    assert out == [] and e.phase_of(RULE_ID) is Phase.NORMAL


def test_a_device_that_never_reported_opens_after_the_limit_from_the_rule_being_installed() -> None:
    e = engine()
    out = stream(e, OTHER, T0, T0 + 120)
    assert names(out) == ["opened"] and out[0].ts == T0 + LIMIT


def test_another_metric_of_the_same_device_is_not_a_sign_of_life() -> None:
    e = engine()
    out: list[Transition] = []
    for i in range(100):
        t = T0 + 2 * i
        out += e.feed(reading(SILENT, t, current_a=40.0), arrived=t)  # no temperature
        out += e.tick(t)
    assert names(out) == ["opened"]


def test_a_device_clock_that_is_wrong_does_not_make_it_silent() -> None:
    """Silence is the time since a sample ARRIVED: the timestamp inside may be anything."""
    e = engine()
    out: list[Transition] = []
    for i in range(100):
        arrived = T0 + 2 * i
        # an hour behind, but advancing: not a "clock jump" either
        out += e.feed(Sample(SILENT, arrived - 3600, {"temperature_c": 70.0}), arrived=arrived)
        out += e.tick(arrived)
    assert out == []


# -- the pipeline is not the device ---------------------------------------------------------------


def test_nothing_opens_while_the_whole_pipeline_is_silent() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    stream(e, OTHER, T0, T0 + 20)
    # ingestion is down: no sample from anyone for ten minutes, the clock keeps ticking
    out: list[Transition] = []
    for s in range(20, 620, 5):
        out += e.tick(T0 + s)
    assert out == [] and e.phase_of(RULE_ID) is Phase.NORMAL


def test_when_the_pipeline_comes_back_every_silence_clock_starts_again() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    resume = T0 + 20 + 600
    # the neighbour is first to report; the device has been silent for ten minutes of wall time
    out = e.feed(reading(OTHER, resume), arrived=resume)
    out += e.tick(resume + 1)
    assert out == [] and e.phase_of(RULE_ID) is Phase.NORMAL
    # the device is still not back after the limit counted from the resumption: now it is its fault
    out = stream(e, OTHER, resume + 2, resume + 2 + LIMIT + 10)
    assert names(out) == ["opened"] and out[0].ts == resume + LIMIT


def test_a_pipeline_gap_shorter_than_the_limit_is_not_a_reset() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    stream(e, OTHER, T0, T0 + 20)
    quiet = PIPELINE_GAP_S - 1  # a hiccup that is no outage
    out = stream(e, OTHER, T0 + 20 + quiet, T0 + 200)
    assert names(out) == ["opened"]  # the device's own clock ran on


def test_a_backlog_that_arrives_late_counts_as_seen_at_the_time_it_arrives() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    # the broker hands over what it queued: old device timestamps, delivered now
    now = T0 + 500
    out: list[Transition] = []
    for i in range(30):
        out += e.feed(Sample(SILENT, T0 + 20 + 2 * i, {"temperature_c": 70.0}), arrived=now)
    out += e.tick(now + 1)
    assert out == []


# -- clearing -----------------------------------------------------------------------------------


def opened_engine() -> tuple[Engine, float]:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    out = stream(e, OTHER, T0 + 20, T0 + 100)
    assert names(out) == ["opened"]
    return e, T0 + 100


def test_the_alarm_clears_after_the_device_has_reported_steadily_for_the_hold() -> None:
    e, now = opened_engine()
    out = stream(e, SILENT, now, now + CLEAR_HOLD_S + 4)
    assert names(out) == ["cleared"]
    cleared = out[0]
    assert cleared.value >= LIMIT and cleared.peak >= LIMIT  # how long it was silent
    assert e.phase_of(RULE_ID) is Phase.NORMAL and e.open_count() == 0


def test_a_single_sample_does_not_clear_it() -> None:
    e, now = opened_engine()
    assert e.feed(reading(SILENT, now), arrived=now) == []
    assert e.phase_of(RULE_ID) is Phase.CLEARING
    # ...and when the device goes quiet again the alarm is still the same open one
    out = stream(e, OTHER, now + 2, now + 60)
    assert out == [] and e.open_count() == 1


def test_a_gap_during_the_hold_starts_the_hold_again() -> None:
    e, now = opened_engine()
    e.feed(reading(SILENT, now), arrived=now)
    later = now + 30  # silent again for 30 s, but within the limit: no new alarm either
    e.feed(reading(OTHER, later), arrived=later)
    out = stream(e, SILENT, later, later + CLEAR_HOLD_S - 2)
    assert out == []  # the hold counts from the first sample after the gap
    out = stream(e, SILENT, later + CLEAR_HOLD_S - 2, later + CLEAR_HOLD_S + 4)
    assert names(out) == ["cleared"]


def test_the_peak_is_the_longest_silence() -> None:
    e, now = opened_engine()
    assert e.tick(now + 40) == []  # still silent
    out = stream(e, SILENT, now + 40, now + 40 + CLEAR_HOLD_S + 4)
    assert names(out) == ["cleared"]
    # the last sample before the silence arrived at T0 + 18, the first one after it at T0 + 140
    assert out[0].peak == (now + 40) - (T0 + 18)


# -- restarts and edits -------------------------------------------------------------------------


def test_an_alarm_that_is_open_in_the_database_is_adopted_and_clears_when_data_returns() -> None:
    e = engine()
    e.adopt_open(RULE_ID, opened_at=T0 - 300, peak=300.0)
    assert e.phase_of(RULE_ID) is Phase.ACTIVE
    out = stream(e, SILENT, T0, T0 + CLEAR_HOLD_S + 4)
    assert names(out) == ["cleared"]
    assert out[0].peak >= 300.0  # the earlier peak is kept


def test_samples_older_than_the_alarm_do_not_clear_it() -> None:
    e = engine()
    e.adopt_open(RULE_ID, opened_at=T0, peak=60.0)
    out = stream(e, SILENT, T0 - 100, T0 - 20)  # replayed history from before it opened
    assert out == [] and e.phase_of(RULE_ID) is Phase.ACTIVE


def test_editing_the_limit_keeps_the_silence_that_has_been_counted() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    e.set_rules([no_data_rule(duration_s=120)], now=T0 + 30)
    out = stream(e, OTHER, T0 + 20, T0 + 18 + 120 + 6)
    assert names(out) == ["opened"] and out[0].ts == T0 + 18 + 120


def test_renaming_the_rule_changes_nothing() -> None:
    e = engine()
    stream(e, SILENT, T0, T0 + 20)
    e.set_rules([no_data_rule(name="başka ad", severity="critical")], now=T0 + 30)
    out = stream(e, OTHER, T0 + 20, T0 + 100)
    assert names(out) == ["opened"] and out[0].rule.severity == "critical"


def test_a_removed_rule_closes_its_open_alarm() -> None:
    e, now = opened_engine()
    closed = e.set_rules([], now=now)
    assert names(closed) == ["cleared"] and closed[0].forced


def test_threshold_rules_ignore_ticks() -> None:
    e = Engine()
    threshold = Rule(
        uuid.UUID(int=7), SILENT, "t", "threshold", "temperature_c", ">", 80.0, 75.0, 30, None,
        "critical",
    )  # fmt: skip
    e.set_rules([threshold], now=T0)
    e.feed(reading(OTHER, T0), arrived=T0)
    assert e.tick(T0 + 1000) == []
