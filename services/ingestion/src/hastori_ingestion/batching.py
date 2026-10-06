"""Batch flush policy: flush at max_size rows or when the oldest row is max_age_s old."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BatchPolicy:
    max_size: int = 500
    max_age_s: float = 1.0

    def should_flush(self, size: int, first_added_at: float | None, now: float) -> bool:
        if size == 0 or first_added_at is None:
            return False
        return size >= self.max_size or now - first_added_at >= self.max_age_s

    def wait_s(self, first_added_at: float | None, now: float) -> float | None:
        """How long the writer may block waiting for the next item (None = indefinitely)."""
        if first_added_at is None:
            return None
        return max(0.0, first_added_at + self.max_age_s - now)
