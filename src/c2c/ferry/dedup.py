"""In-memory msg_id dedup with a sliding time window."""
from __future__ import annotations

from typing import Callable


class Dedup:
    def __init__(self, window_ms: int, now_ms: Callable[[], int]) -> None:
        self._window = window_ms
        self._now = now_ms
        self._seen: dict[str, int] = {}

    def seen(self, msg_id: str) -> bool:
        was_seen = self.check(msg_id)
        self.record(msg_id)
        return was_seen

    def check(self, msg_id: str) -> bool:
        """Return whether msg_id is currently recorded within the window,
        without recording it or refreshing its timestamp (a pure peek)."""
        now = self._now()
        self._evict(now)
        prev = self._seen.get(msg_id)
        return prev is not None and (now - prev) <= self._window

    def record(self, msg_id: str) -> None:
        """Record/refresh msg_id's timestamp now."""
        now = self._now()
        self._evict(now)
        self._seen[msg_id] = now

    def _evict(self, now: int) -> None:
        cutoff = now - self._window
        for k in [k for k, t in self._seen.items() if t < cutoff]:
            del self._seen[k]
