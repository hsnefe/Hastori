"""Reactive energy ratio over a sliding window. Pure: time is the timestamp of the samples."""

from collections import deque

# Two consecutive samples further apart than this are a gap in the data, not a long sample: the
# energy of a silent stretch is not invented. (Demo assumption: devices publish every 2 s.)
MAX_GAP_S = 10.0
# Below this much active energy in the window the ratio is noise (a quiet night, the first
# seconds after a start): no value, so no alarm. Demo assumption.
MIN_WINDOW_KWH = 2.0


class ReactiveWindow:
    """Sum(Q) / Sum(P) of the last `window_s` seconds, weighted by the time each sample covers.

    Only inductive (positive) reactive power counts: capacitive power from an over-compensated
    bank is a different problem and must not hide an inductive excess in the same window.
    """

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, float, float]] = deque()  # (ts, p*dt, q*dt)
        self._sum_p = 0.0
        self._sum_q = 0.0
        self._prev_ts: float | None = None

    def add(self, ts: float, p_kw: float, q_kvar: float) -> float | None:
        """Add a sample (in time order); returns the ratio, or None while there is too little
        active energy in the window to say anything."""
        dt = 0.0 if self._prev_ts is None else min(max(ts - self._prev_ts, 0.0), MAX_GAP_S)
        self._prev_ts = ts
        ep, eq = max(p_kw, 0.0) * dt, max(q_kvar, 0.0) * dt
        self._samples.append((ts, ep, eq))
        self._sum_p += ep
        self._sum_q += eq
        while self._samples and self._samples[0][0] <= ts - self.window_s:
            _, old_p, old_q = self._samples.popleft()
            self._sum_p -= old_p
            self._sum_q -= old_q
        self._sum_p, self._sum_q = max(self._sum_p, 0.0), max(self._sum_q, 0.0)  # float drift
        if self._sum_p / 3600.0 < MIN_WINDOW_KWH:
            return None
        return self._sum_q / self._sum_p
