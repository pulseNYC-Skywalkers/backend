"""Three-state circuit breaker.

The whole point: a 429 is *normal throttling*, not an outage, so 429s never touch
this breaker. Only real failures (5xx, timeouts, connection errors) count. If we
naively tripped on 429s the breaker would sit open almost permanently given a 70%
throttle rate — that distinction is the design point.

States:
  closed     -> requests flow; count consecutive real failures
  open       -> after N real failures we stop sending for a cooldown
  half_open  -> after cooldown we let a few probes through; success closes, any
                failure re-opens
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from observability.metrics import COUNTERS, get_logger

log = get_logger("breaker")


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        halfopen_probes: int,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.halfopen_probes = halfopen_probes

        self.state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._halfopen_inflight = 0
        self._halfopen_successes = 0
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        """Return True if a request may proceed right now."""
        async with self._lock:
            now = time.monotonic()
            if self.state == State.CLOSED:
                return True
            if self.state == State.OPEN:
                if now - self._opened_at >= self.cooldown_seconds:
                    # Cooldown elapsed: move to half-open and admit a probe.
                    self.state = State.HALF_OPEN
                    self._halfopen_inflight = 1
                    self._halfopen_successes = 0
                    log.info("breaker -> half_open (probing)")
                    COUNTERS.incr("breaker_halfopen_transitions")
                    return True
                return False
            # HALF_OPEN: admit a bounded number of concurrent probes.
            if self._halfopen_inflight < self.halfopen_probes:
                self._halfopen_inflight += 1
                return True
            return False

    async def on_success(self) -> None:
        async with self._lock:
            if self.state == State.HALF_OPEN:
                self._halfopen_successes += 1
                self._halfopen_inflight = max(0, self._halfopen_inflight - 1)
                if self._halfopen_successes >= self.halfopen_probes:
                    self.state = State.CLOSED
                    self._failures = 0
                    log.info("breaker -> closed (recovered)")
                    COUNTERS.incr("breaker_closed_transitions")
            else:
                self._failures = 0

    async def on_real_failure(self) -> None:
        """Call ONLY for true failures (5xx / timeout / connection error)."""
        async with self._lock:
            COUNTERS.incr("real_failures")
            if self.state == State.HALF_OPEN:
                self.state = State.OPEN
                self._opened_at = time.monotonic()
                self._halfopen_inflight = 0
                log.warning("breaker -> open (probe failed)")
                COUNTERS.incr("breaker_open_transitions")
                return
            self._failures += 1
            if self.state == State.CLOSED and self._failures >= self.failure_threshold:
                self.state = State.OPEN
                self._opened_at = time.monotonic()
                log.warning("breaker -> open (%d consecutive real failures)", self._failures)
                COUNTERS.incr("breaker_open_transitions")

    def seconds_until_retry(self) -> float:
        if self.state != State.OPEN:
            return 0.0
        return max(0.0, self.cooldown_seconds - (time.monotonic() - self._opened_at))
