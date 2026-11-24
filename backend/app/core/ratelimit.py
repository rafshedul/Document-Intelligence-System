"""Sliding-window rate limiter (in memory, per process).

Good enough for a single API process. With several API replicas, each has its own counters, so put a
shared limiter (Redis, or the reverse proxy) in front for a hard limit.
"""
import threading
import time
from collections import defaultdict, deque
from typing import Callable


class RateLimiter:
    def __init__(self, limit: Callable[[], int], window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._limit, self._window, self._clock = limit, window_seconds, clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, cost: int = 1) -> float | None:
        """Record `cost` hits for key. Returns None if allowed, else seconds until it would be."""
        limit = self._limit()
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self._window:
                q.popleft()
            if len(q) + cost > limit:
                oldest = q[0] if q else now
                return max(0.1, oldest + self._window - now) if q else self._window
            q.extend([now] * cost)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
