"""Adaptive global rate limiter (AIMD).

Paces our *global* outbound request rate so we don't hammer an API that's already
throttling us. On a 429 we multiplicatively back the rate off; on success we
additively (gently) speed back up. This complements per-request backoff: backoff
fixes one request, the limiter fixes the whole run's send rate.
"""

from __future__ import annotations

import asyncio
import time

from observability.metrics import get_logger

log = get_logger("ratelimiter")


class AdaptiveRateLimiter:
    def __init__(
        self,
        *,
        min_interval: float = 0.005,
        start_interval: float = 0.01,
        max_interval: float = 0.04,
        backoff_factor: float = 1.2,
        recovery_factor: float = 0.85,
    ) -> None:
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.backoff_factor = backoff_factor
        self.recovery_factor = recovery_factor
        self._interval = start_interval
        self._next_allowed = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until this caller is allowed to send, respecting global pace."""
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)

    def on_throttled(self) -> None:
        self._interval = min(self.max_interval, self._interval * self.backoff_factor)

    def on_success(self) -> None:
        self._interval = max(self.min_interval, self._interval * self.recovery_factor)

    @property
    def interval(self) -> float:
        return self._interval
