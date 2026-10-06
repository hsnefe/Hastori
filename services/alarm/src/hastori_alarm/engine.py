"""Alarm rule state machine. Pure: no I/O and no clock, so the same samples always give the same
transitions, whether they arrive live, are replayed after a restart, or are split across two runs.

Per rule (shown for operator '>'; '<' is the mirror image):

    normal   --value > threshold-->                    pending   (the clock starts at that sample)
    pending  --value <= threshold, or a data gap-->    normal    (a spike never alarms)
    pending  --held duration_s-->                      active    (transition `opened`)
    active   --value < clear_threshold-->              clearing
    clearing --value >= clear_threshold, or a gap-->   active
    clearing --held CLEAR_HOLD_S-->                    normal    (transition `cleared`)

Time is always the timestamp of the sample: a device clock that runs behind shifts every sample by
the same amount and changes nothing; a backlog replayed at full speed behaves like the live
stream. Acknowledging an alarm is not a state here: an acknowledged alarm is still active.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID

from hastori_alarm.reactive import MAX_GAP_S, ReactiveWindow

# How long the value must stay below the clear threshold before the alarm closes (demo assumption).
CLEAR_HOLD_S = 10.0

ACTIVE_METRIC = "active_power_kw"
REACTIVE_METRIC = "reactive_power_kvar"


class Phase(StrEnum):
    NORMAL = "normal"
    PENDING = "pending"
    ACTIVE = "active"
    CLEARING = "clearing"


@dataclass(frozen=True)
class Rule:
    id: UUID
    device_id: UUID
    name: str
    kind: str  # threshold | reactive_ratio
    metric: str
    operator: str  # '>' | '<'
    threshold: float
    clear_threshold: float
    duration_s: float
    window_s: int | None
    severity: str

    def breached(self, value: float) -> bool:
        return value > self.threshold if self.operator == ">" else value < self.threshold

    def recovered(self, value: float) -> bool:
        return (
            value < self.clear_threshold if self.operator == ">" else value > self.clear_threshold
        )

    def worse(self, a: float, b: float) -> float:
        return max(a, b) if self.operator == ">" else min(a, b)

    def same_logic(self, other: "Rule") -> bool:
        """Everything that changes what a sample means (names and severity do not)."""
        return (
            self.kind,
            self.metric,
            self.operator,
            self.threshold,
            self.clear_threshold,
            self.duration_s,
            self.window_s,
        ) == (
            other.kind,
            other.metric,
            other.operator,
            other.threshold,
            other.clear_threshold,
            other.duration_s,
            other.window_s,
        )


@dataclass(frozen=True)
class Sample:
    device_id: UUID
    ts: float
    metrics: Mapping[str, float]


@dataclass(frozen=True)
class Transition:
    kind: Literal["opened", "cleared"]
    rule: Rule
    ts: float
    value: float  # opened: the value that opened it; cleared: the value that closed it
    peak: float  # the worst value of the excursion so far
    forced: bool = False  # closed because the rule or device went away, not because of the data


@dataclass
class RuleState:
    rule: Rule
    phase: Phase = Phase.NORMAL
    pending_since: float | None = None
    clear_since: float | None = None
    last_ts: float | None = None
    peak: float | None = None
    # Samples older than this are not this rule's business: an alarm that is already open in the
    # database was opened at this time, and what the data did before it says nothing about it.
    floor_ts: float = -math.inf
    window: ReactiveWindow | None = field(default=None)

    def __post_init__(self) -> None:
        self.window = self._new_window()

    def _new_window(self) -> ReactiveWindow | None:
        if self.rule.kind == "reactive_ratio" and self.rule.window_s:
            return ReactiveWindow(self.rule.window_s)
        return None

    def value_of(self, sample: Sample) -> float | None:
        if self.rule.kind == "reactive_ratio":
            p, q = sample.metrics.get(ACTIVE_METRIC), sample.metrics.get(REACTIVE_METRIC)
            if p is None or q is None or self.window is None:
                return None
            return self.window.add(sample.ts, p, q)
        return sample.metrics.get(self.rule.metric)

    def feed(self, sample: Sample) -> list[Transition]:
        # The window sees every sample, even those before the floor: it needs its history.
        value = self.value_of(sample)
        if sample.ts < self.floor_ts or value is None:
            return []
        ts, rule = sample.ts, self.rule
        gap = self.last_ts is not None and ts - self.last_ts > MAX_GAP_S
        self.last_ts = ts
        if gap:
            # Continuity is gone: a half-counted duration or hold must start again.
            if self.phase is Phase.PENDING:
                self._to_normal()
            elif self.phase is Phase.CLEARING:
                self.phase, self.clear_since = Phase.ACTIVE, None

        out: list[Transition] = []
        if self.phase is Phase.NORMAL and rule.breached(value):
            self.phase, self.pending_since, self.peak = Phase.PENDING, ts, value
        if self.phase is Phase.PENDING:
            assert self.pending_since is not None and self.peak is not None
            if not rule.breached(value):
                self._to_normal()
            else:
                self.peak = rule.worse(self.peak, value)
                if ts - self.pending_since >= rule.duration_s:
                    self.phase = Phase.ACTIVE
                    out.append(Transition("opened", rule, ts, value, self.peak))
        elif self.phase is Phase.ACTIVE:
            self.peak = value if self.peak is None else rule.worse(self.peak, value)
            if rule.recovered(value):
                self.phase, self.clear_since = Phase.CLEARING, ts
        elif self.phase is Phase.CLEARING:
            assert self.clear_since is not None
            self.peak = value if self.peak is None else rule.worse(self.peak, value)
            if not rule.recovered(value):
                self.phase, self.clear_since = Phase.ACTIVE, None
            elif ts - self.clear_since >= CLEAR_HOLD_S:
                out.append(Transition("cleared", rule, ts, value, self.peak))
                self._to_normal()
        return out

    def _to_normal(self) -> None:
        self.phase, self.pending_since, self.clear_since, self.peak = Phase.NORMAL, None, None, None

    @property
    def is_open(self) -> bool:
        return self.phase in (Phase.ACTIVE, Phase.CLEARING)


class Engine:
    """All rules at once. `feed` takes samples in time order, per device."""

    def __init__(self) -> None:
        self._states: dict[UUID, RuleState] = {}
        self._by_device: dict[UUID, list[UUID]] = {}
        self._newest: dict[UUID, float] = {}  # device -> newest sample ts seen

    # -- rules -------------------------------------------------------------------------------
    def set_rules(self, rules: Iterable[Rule], now: float) -> list[Transition]:
        """Install the current set of enabled rules.

        A rule that is gone (deleted, disabled, its device deactivated) while its alarm is open
        yields a forced `cleared` at `now`. A rule whose logic changed forgets a half-counted
        duration; an alarm that is open stays open and starts counting its clear hold again.
        """
        wanted = {r.id: r for r in rules}
        out: list[Transition] = []
        for rid in list(self._states):
            if rid not in wanted:
                gone = self._states.pop(rid)
                if gone.is_open:
                    peak = gone.peak if gone.peak is not None else gone.rule.threshold
                    out.append(Transition("cleared", gone.rule, now, peak, peak, forced=True))
        for rid, rule in wanted.items():
            st = self._states.get(rid)
            if st is None:
                self._states[rid] = RuleState(rule)
            elif not st.rule.same_logic(rule):
                fresh = RuleState(rule, floor_ts=st.floor_ts)
                if st.is_open:
                    fresh.phase, fresh.peak = Phase.ACTIVE, st.peak
                self._states[rid] = fresh
            else:
                st.rule = rule  # a new name or severity does not touch the state
        self._by_device = {}
        for rid, st in self._states.items():
            self._by_device.setdefault(st.rule.device_id, []).append(rid)
        return out

    def adopt_open(self, rule_id: UUID, opened_at: float, peak: float | None) -> None:
        """An alarm for this rule is already open in the database (opened at `opened_at`)."""
        st = self._states.get(rule_id)
        if st is not None:
            st.phase, st.peak, st.floor_ts = Phase.ACTIVE, peak, opened_at
            st.pending_since = st.clear_since = None

    # -- samples -----------------------------------------------------------------------------
    def feed(self, sample: Sample) -> list[Transition]:
        # Millisecond resolution: the same reading must compare equal whether it came from a
        # message or from the database.
        sample = Sample(sample.device_id, round(sample.ts, 3), sample.metrics)
        newest = self._newest.get(sample.device_id)
        if newest is not None and sample.ts <= newest:
            return []  # a duplicate or an out-of-order message: already counted
        self._newest[sample.device_id] = sample.ts
        out: list[Transition] = []
        for rid in self._by_device.get(sample.device_id, ()):
            out.extend(self._states[rid].feed(sample))
        return out

    # -- inspection --------------------------------------------------------------------------
    def phase_of(self, rule_id: UUID) -> Phase | None:
        st = self._states.get(rule_id)
        return st.phase if st else None

    def open_count(self) -> int:
        return sum(1 for st in self._states.values() if st.is_open)

    def newest_ts(self, device_id: UUID) -> float | None:
        return self._newest.get(device_id)
