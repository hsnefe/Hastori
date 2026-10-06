"""Physically consistent signal model for the demo devices.

Pure logic with an injectable clock: every call takes the time as an argument, so tests can
drive a virtual clock. All ratios below are demo assumptions, named as constants.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Literal

FaultKind = Literal["overheat", "spike", "compensation_failure", "offline"]
FAULT_KINDS: tuple[str, ...] = ("overheat", "spike", "compensation_failure", "offline")

# Default length of an injected fault. The overheat ramp crosses 80 C after ~16 s, so a shorter
# fault cannot satisfy the 30 s alarm rule of the demo (test_default_overheat_trips_the_demo_rule).
DEFAULT_FAULT_S = 60.0

VOLTAGE_V = 400.0
SQRT3 = math.sqrt(3.0)
NOISE_FRACTION = 0.03

COMPRESSOR_LOADED_KW = 45.0
COMPRESSOR_IDLE_KW = 12.0
COMPRESSOR_CYCLE_S = (360.0, 480.0)
COMPRESSOR_LOADED_FRACTION = 0.6
CHILLER_KW = (30.0, 60.0)
CHILLER_PERIOD_S = 1800.0
LIGHTING_OTHER_KW = {"izmir": 10.0, "antalya": 14.0}
DEFAULT_OTHER_KW = 10.0

TAN_PHI_NORMAL = (0.12, 0.16)
TAN_PHI_FAILED = 0.35

TEMP_RANGE = {"compressor": (60.0, 72.0), "chiller": (30.0, 45.0), "energy_analyzer": (30.0, 38.0)}
TEMP_TAU_S = 60.0
OVERHEAT_PEAK_C = 85.0
OVERHEAT_RAMP_S = 20.0
OVERHEAT_RECOVERY_TAU_S = 8.0
SPIKE_C = 90.0

# Physical bounds shared in spirit with ingestion validation.
BOUNDS = {
    "active_power_kw": (0.0, 1000.0),
    "reactive_power_kvar": (0.0, 1000.0),
    "current_a": (0.0, 2000.0),
    "temperature_c": (-40.0, 150.0),
}


def current_from_power(p_kw: float, tan_phi: float) -> float:
    """I = P * 1000 / (sqrt(3) * 400 V * cos(phi)), with cos(phi) = 1 / sqrt(1 + tan^2)."""
    cos_phi = 1.0 / math.sqrt(1.0 + tan_phi**2)
    return p_kw * 1000.0 / (SQRT3 * VOLTAGE_V * cos_phi)


def daily_profile(kind: str, hour: float) -> float:
    """Load factor 0.55..1.0: factory shift 08-18, hotel evening peak."""
    if kind == "factory":
        if 8.0 <= hour < 18.0:
            return 1.0
        return 0.6
    # hotel: baseline 0.7 with a smooth evening peak around 20:00
    return 0.7 + 0.3 * math.exp(-(((hour - 20.0) / 2.5) ** 2))


@dataclass
class Fault:
    kind: FaultKind
    start: float
    duration_s: float

    def active(self, now: float) -> bool:
        return self.start <= now < self.start + self.duration_s


@dataclass
class DeviceModel:
    key: str
    type: str
    site_key: str
    seed: int
    site_profile: str = "factory"
    rng: random.Random = field(init=False)
    cycle_s: float = field(init=False)
    phase: float = field(init=False)
    tan_phi: float = field(init=False)
    temperature: float | None = None
    last_time: float | None = None
    last_p_kw: float = 0.0
    fault: Fault | None = None
    _spike_pending: bool = False
    _overheat_start_temp: float = 0.0

    def __post_init__(self) -> None:
        self.rng = random.Random(f"{self.seed}:{self.key}")
        self.cycle_s = self.rng.uniform(*COMPRESSOR_CYCLE_S)
        self.phase = self.rng.uniform(0.0, self.cycle_s)
        self.tan_phi = self.rng.uniform(*TAN_PHI_NORMAL)

    # -- faults -----------------------------------------------------------------------------
    def set_fault(self, kind: FaultKind, now: float, duration_s: float) -> None:
        self.fault = Fault(kind, now, duration_s)
        self._spike_pending = kind == "spike"
        if kind == "overheat":
            self._overheat_start_temp = self.temperature or sum(TEMP_RANGE[self.type]) / 2

    def clear_fault(self) -> None:
        self.fault = None
        self._spike_pending = False

    def is_offline(self, now: float) -> bool:
        return bool(self.fault and self.fault.kind == "offline" and self.fault.active(now))

    # -- base signals -----------------------------------------------------------------------
    def _load_fraction(self, now: float) -> float:
        """0..1 position within the normal operating range, by device type."""
        if self.type == "compressor":
            pos = (now + self.phase) % self.cycle_s
            return 1.0 if pos < self.cycle_s * COMPRESSOR_LOADED_FRACTION else 0.0
        if self.type == "chiller":
            return 0.5 + 0.5 * math.sin(2 * math.pi * (now + self.phase) / CHILLER_PERIOD_S)
        return 0.5

    def device_power(self, now: float, hour: float) -> float:
        profile = daily_profile(self.site_profile, hour)
        if self.type == "compressor":
            base = COMPRESSOR_LOADED_KW if self._load_fraction(now) >= 1.0 else COMPRESSOR_IDLE_KW
            base *= profile
        elif self.type == "chiller":
            lo, hi = CHILLER_KW
            base = (lo + (hi - lo) * self._load_fraction(now)) * profile
        else:
            base = 0.0
        return max(0.0, base * (1.0 + self.rng.gauss(0.0, NOISE_FRACTION)))

    def _drift_tan_phi(self) -> float:
        lo, hi = TAN_PHI_NORMAL
        self.tan_phi = min(hi, max(lo, self.tan_phi + self.rng.gauss(0.0, 0.002)))
        if self.fault and self.fault.kind == "compensation_failure":
            return TAN_PHI_FAILED
        return self.tan_phi

    def _temperature_target(self, now: float, p_kw: float) -> float:
        lo, hi = TEMP_RANGE[self.type]
        if self.type == "compressor":
            frac = (p_kw / (COMPRESSOR_LOADED_KW)) if p_kw > 0 else 0.0
        elif self.type == "chiller":
            frac = self._load_fraction(now)
        else:
            frac = min(1.0, p_kw / 150.0)
        return lo + (hi - lo) * min(1.0, max(0.0, frac))

    def _advance_temperature(self, now: float, p_kw: float) -> float:
        target = self._temperature_target(now, p_kw)
        if self.temperature is None:
            self.temperature = target
        dt = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
        f = self.fault
        if f and f.kind == "overheat":
            if f.active(now):
                ramp = min(1.0, (now - f.start) / OVERHEAT_RAMP_S)
                self.temperature = self._overheat_start_temp + ramp * (
                    OVERHEAT_PEAK_C - self._overheat_start_temp
                )
            else:
                alpha = 1.0 - math.exp(-dt / OVERHEAT_RECOVERY_TAU_S)
                self.temperature += (target - self.temperature) * alpha
        else:
            alpha = 1.0 - math.exp(-dt / TEMP_TAU_S)
            self.temperature += (target - self.temperature) * alpha
        return self.temperature

    # -- public -----------------------------------------------------------------------------
    def sample(self, now: float, hour: float, panel_p_kw: float | None = None) -> dict[str, float]:
        """One reading. panel_p_kw: precomputed active power for energy analyzers."""
        p = (
            panel_p_kw
            if self.type == "energy_analyzer" and panel_p_kw is not None
            else (self.device_power(now, hour))
        )
        tan_phi = self._drift_tan_phi()
        temp = self._advance_temperature(now, p)
        self.last_time = now
        self.last_p_kw = p
        reported_temp = temp + self.rng.gauss(0.0, 0.2)
        if self._spike_pending and self.fault and self.fault.kind == "spike":
            reported_temp = SPIKE_C
            self._spike_pending = False
            self.fault = None
        values = {
            "active_power_kw": p,
            "reactive_power_kvar": p * tan_phi,
            "current_a": current_from_power(p, tan_phi),
            "temperature_c": reported_temp,
        }
        return {k: min(BOUNDS[k][1], max(BOUNDS[k][0], v)) for k, v in values.items()}


def sample_site(devices: list[DeviceModel], now: float, hour: float) -> dict[str, dict[str, float]]:
    """Sample all devices of one site; the main-panel analyzer = sum of the others + other loads."""
    out: dict[str, dict[str, float]] = {}
    others_kw = 0.0
    for d in devices:
        if d.type == "energy_analyzer":
            continue
        out[d.key] = d.sample(now, hour)
        others_kw += out[d.key]["active_power_kw"]
    for d in devices:
        if d.type == "energy_analyzer":
            site_key = d.site_key
            other = LIGHTING_OTHER_KW.get(site_key, DEFAULT_OTHER_KW) * daily_profile(
                d.site_profile, hour
            )
            panel = (others_kw + other) * (1.0 + d.rng.gauss(0.0, 0.005))
            out[d.key] = d.sample(now, hour, panel_p_kw=panel)
    return out
