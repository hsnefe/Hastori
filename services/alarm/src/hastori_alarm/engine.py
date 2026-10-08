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

A device clock that is set back while the device keeps running is the exception: every new sample
would look older than the newest one and be dropped as a duplicate, so the alarm would be blind
until the clock caught up again. A sample CLOCK_JUMP_S or more behind the newest one that is not a
known duplicate is a candidate; the next sample must continue from it (within CLOCK_JUMP_CONFIRM_S)
before the device's sequence restarts (`clock_jumps` counts them). One wrong timestamp on its own
would otherwise wipe a half-counted duration, a clear hold and the reactive window.

A `no_data` rule is the odd one out: it fires on the absence of samples, so it also needs the
wall clock (`Engine.tick`) and measures silence in the time samples *arrived*, not in device time
(a device with a wrong clock is not silent; a backlog replayed after an outage is not old):

    normal   --no sample of the metric for duration_s-->     active    (transition `opened`)
    active   --a sample arrives-->                           clearing
    clearing --samples keep coming for CLEAR_HOLD_S-->       normal    (transition `cleared`)
    clearing --a gap longer than MAX_GAP_S-->                clearing  (the hold starts again)

Silence is only the device's fault while the rest of the pipeline is working: the alarm opens only
if some sample (of any device) arrived at or after the moment the limit was crossed, and when
samples come back after PIPELINE_GAP_S without any, every silence clock starts again. A broker,
ingestion or RabbitMQ outage therefore does not turn every device into an alarm.
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

# A sample this far (or further) behind the device's newest one, and not a duplicate of something
# already seen, means the device clock was set back; smaller slips are dropped as out of order.
CLOCK_JUMP_S = 60.0
# The sample after a jump candidate must follow it this closely to confirm the jump.
CLOCK_JUMP_CONFIRM_S = MAX_GAP_S
# How far back the engine remembers the timestamps it has accepted (to recognise redeliveries).
SEEN_WINDOW_S = 900.0

# No sample from any device for this long: the pipeline was down (or the whole fleet was), not one
# device. Devices publish every 2 s.
PIPELINE_GAP_S = 20.0

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
    kind: str  # threshold | reactive_ratio | no_data (the threshold fields are unused for no_data)
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
    # no_data: wall-clock time a sample of the metric last arrived (the time the rule was
    # installed until one does), and the arrival time of the previous sample.
    seen_at: float = -math.inf
    last_arrival: float | None = None

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

    def feed(self, sample: Sample, arrived: float | None = None) -> list[Transition]:
        if self.rule.kind == "no_data":
            return self._feed_no_data(sample, sample.ts if arrived is None else arrived)
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

    def _feed_no_data(self, sample: Sample, arrived: float) -> list[Transition]:
        if self.rule.metric not in sample.metrics or sample.ts < self.floor_ts:
            return []
        silence = arrived - self.seen_at
        self.seen_at = max(self.seen_at, arrived)
        gap = self.last_arrival is not None and arrived - self.last_arrival > MAX_GAP_S
        self.last_arrival = arrived
        if self.phase is Phase.ACTIVE:
            self.peak = max(self.peak or 0.0, silence)
            self.phase, self.clear_since = Phase.CLEARING, arrived
        elif self.phase is Phase.CLEARING:
            assert self.clear_since is not None
            self.peak = max(self.peak or 0.0, silence)
            if gap:
                self.clear_since = arrived  # the data is not continuous: the hold starts again
            elif arrived - self.clear_since >= CLEAR_HOLD_S:
                peak = self.peak
                self._to_normal()
                return [Transition("cleared", self.rule, arrived, peak, peak)]
        return []

    def tick(self, now: float, pipeline_until: float) -> list[Transition]:
        """no_data only: silence is judged against the wall clock, between samples."""
        if self.rule.kind != "no_data":
            return []
        silent_for = now - self.seen_at
        if self.phase is Phase.NORMAL:
            deadline = self.seen_at + self.rule.duration_s
            if now >= deadline and pipeline_until >= deadline:
                self.phase, self.peak = Phase.ACTIVE, silent_for
                return [Transition("opened", self.rule, deadline, silent_for, silent_for)]
        elif self.phase is Phase.ACTIVE:
            self.peak = max(self.peak or 0.0, silent_for)
        return []

    def restart_sequence(self) -> None:
        """The device clock jumped back: forget the continuity of the old timeline. A half-counted
        duration or clear hold starts again, an open alarm stays open (its peak is kept)."""
        if self.phase is Phase.PENDING:
            self._to_normal()
        elif self.phase is Phase.CLEARING:
            self.phase, self.clear_since = Phase.ACTIVE, None
        self.last_ts, self.floor_ts = None, -math.inf
        self.last_arrival = None
        self.window = self._new_window()

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
        self._seen: dict[UUID, set[float]] = {}  # device -> accepted timestamps (recent)
        self._floor = -math.inf  # samples before this were covered by the start-up replay
        self._live: set[UUID] = set()  # devices whose backlog is over (see mark_live)
        self._jump_candidate: dict[UUID, float] = {}  # device -> ts of an unconfirmed back-jump
        self._arrival = -math.inf  # wall-clock time a sample of any device last arrived
        self.clock_jumps = 0

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
                self._states[rid] = RuleState(rule, seen_at=now)
            elif not st.rule.same_logic(rule):
                fresh = RuleState(rule, floor_ts=st.floor_ts, seen_at=st.seen_at)
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
    def history_floor(self, since: float) -> None:
        """Samples older than `since` were already covered by the start-up replay (or are older
        than it looked): until the device is live again, an old message that arrives is a backlog
        queued during the outage, never a clock jump."""
        self._floor = since

    def mark_live(self, device_id: UUID) -> None:
        """A reading of this device arrived in real time: the backlog is behind us, so from now
        on an old timestamp is a clock that was set back and no longer a message that queued."""
        self._live.add(device_id)

    def feed(self, sample: Sample, arrived: float | None = None) -> list[Transition]:
        """`arrived`: wall-clock time the sample reached us (default: its own timestamp, right
        for a replay from the database). Only no_data rules use it."""
        # Millisecond resolution: the same reading must compare equal whether it came from a
        # message or from the database.
        sample = Sample(sample.device_id, round(sample.ts, 3), sample.metrics)
        device, ts = sample.device_id, sample.ts
        arrived = ts if arrived is None else arrived
        self._note_arrival(arrived)
        newest = self._newest.get(device)
        seen = self._seen.setdefault(device, set())
        if newest is not None and ts <= newest:
            if (
                ts in seen
                or newest - ts < CLOCK_JUMP_S
                or (ts < self._floor and device not in self._live)
            ):
                return []  # a duplicate, a backlog or a small slip: already counted or too late
            candidate = self._jump_candidate.get(device)
            if candidate is None or not 0 < ts - candidate <= CLOCK_JUMP_CONFIRM_S:
                self._jump_candidate[device] = ts  # wait for the next sample to confirm
                return []
            del self._jump_candidate[device]
            self._restart_sequence(device)
            seen = self._seen[device]
        else:
            self._jump_candidate.pop(device, None)  # the stream carries on: it was a one-off
        self._newest[device] = ts
        seen.add(ts)
        if len(seen) > 2 * SEEN_WINDOW_S:  # pruned lazily; ~450 entries are live at a 2 s interval
            seen.difference_update([t for t in seen if t < ts - SEEN_WINDOW_S])
        out: list[Transition] = []
        for rid in self._by_device.get(device, ()):
            out.extend(self._states[rid].feed(sample, arrived))
        return out

    def _note_arrival(self, arrived: float) -> None:
        if arrived - self._arrival > PIPELINE_GAP_S:
            # Samples are back after a stretch without any from anyone: whatever was silent in
            # between was the pipeline's silence. Every no_data clock starts again.
            for st in self._states.values():
                if st.rule.kind == "no_data":
                    st.seen_at = max(st.seen_at, arrived)
        self._arrival = max(self._arrival, arrived)

    def tick(self, now: float) -> list[Transition]:
        """Judge silence against the wall clock (no_data rules). Call it every few seconds."""
        out: list[Transition] = []
        for st in self._states.values():
            out.extend(st.tick(now, self._arrival))
        return out

    def _restart_sequence(self, device: UUID) -> None:
        self.clock_jumps += 1
        self._seen[device] = set()
        for rid in self._by_device.get(device, ()):
            self._states[rid].restart_sequence()

    # -- inspection --------------------------------------------------------------------------
    def phase_of(self, rule_id: UUID) -> Phase | None:
        st = self._states.get(rule_id)
        return st.phase if st else None

    def open_count(self) -> int:
        return sum(1 for st in self._states.values() if st.is_open)

    def newest_ts(self, device_id: UUID) -> float | None:
        return self._newest.get(device_id)
