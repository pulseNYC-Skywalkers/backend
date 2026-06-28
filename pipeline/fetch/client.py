"""Resilient async HTTP client for the (flaky) EHR API.

Brings together the three resilience mechanisms:
  * Adaptive rate limiter  -> paces the whole run's send rate
  * Per-request backoff     -> exponential backoff + jitter, honoring Retry-After
  * Circuit breaker         -> opens on REAL failures only, never on 429

Key invariant: a 429 is the *expected* state (API up ~30%). It triggers backoff
and rate-limit easing but does NOT trip the breaker and does NOT consume the
real-failure attempt budget — it has its own generous throttle budget plus an
overall deadline. Result: API unreliability costs wall-clock time, not completeness.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings
from observability.metrics import COUNTERS, get_logger
from pipeline.fetch.circuit_breaker import CircuitBreaker
from pipeline.fetch.rate_limiter import AdaptiveRateLimiter

log = get_logger("fetch")


@dataclass
class FetchResult:
    ok: bool
    http_status: int | None
    payload: Any | None
    attempts: int
    outcome: str          # success | gave_up | not_found
    last_modified_at: str | None = None
    error: str | None = None


class ResilientClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        breaker: CircuitBreaker | None = None,
        limiter: AdaptiveRateLimiter | None = None,
        on_event=None,        # callback(task_key, attempt, outcome, http_status, detail)
        on_auth_refresh=None,  # async callback() -> refresh token/cert on 401
    ) -> None:
        cfg = settings.FETCH
        self._client = client
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=cfg.breaker_failure_threshold,
            cooldown_seconds=cfg.breaker_cooldown_seconds,
            halfopen_probes=cfg.breaker_halfopen_probes,
        )
        self._limiter = limiter or AdaptiveRateLimiter()
        self._on_event = on_event
        self._on_auth_refresh = on_auth_refresh
        self.max_real_attempts = cfg.max_attempts
        self.base_backoff = cfg.base_backoff_seconds
        self.max_backoff = cfg.max_backoff_seconds
        # Generous throttle budget: 429s are normal, so allow many before giving up.
        self.max_throttle_retries = max(40, cfg.max_attempts * 10)
        # Malformed/truncated bodies are data issues, not outages: retry a few times.
        self.max_malformed_retries = 3
        self.max_auth_refreshes = 2
        # Hard wall-clock guard per task so nothing loops forever.
        self.task_deadline_seconds = 180.0

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def _backoff(self, n: int) -> float:
        """Exponential backoff with full jitter."""
        raw = min(self.max_backoff, self.base_backoff * (2 ** max(0, n - 1)))
        return random.uniform(0, raw)

    def _emit(self, task_key, attempt, outcome, http_status, detail):
        COUNTERS.incr(f"outcome.{outcome}")
        if self._on_event:
            self._on_event(task_key, attempt, outcome, http_status, detail)

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    async def get(self, task_key: str, url: str) -> FetchResult:
        """Fetch one URL with full resilience. `task_key` is for event logging."""
        started = time.monotonic()
        real_attempts = 0
        throttle_retries = 0
        malformed_retries = 0
        auth_refreshes = 0
        loop_n = 0

        while True:
            loop_n += 1
            if time.monotonic() - started > self.task_deadline_seconds:
                self._emit(task_key, loop_n, "gave_up", None, "deadline exceeded")
                return FetchResult(False, None, None, loop_n, "gave_up",
                                  error="task deadline exceeded")

            # Respect the breaker (open on real failures only).
            if not await self._breaker.allow():
                wait = max(0.5, self._breaker.seconds_until_retry())
                self._emit(task_key, loop_n, "breaker_open", None, f"wait {wait:.1f}s")
                COUNTERS.incr("breaker_blocked")
                await asyncio.sleep(min(wait, 2.0))
                continue

            await self._limiter.acquire()
            COUNTERS.incr("requests_sent")

            try:
                resp = await self._client.get(url)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
                    httpx.RemoteProtocolError) as exc:
                real_attempts += 1
                await self._breaker.on_real_failure()
                self._emit(task_key, loop_n, "real_failure", None, type(exc).__name__)
                if real_attempts >= self.max_real_attempts:
                    self._emit(task_key, loop_n, "gave_up", None, "max real attempts")
                    return FetchResult(False, None, None, loop_n, "gave_up",
                                      error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(self._backoff(real_attempts))
                continue

            status = resp.status_code

            if status == 429:
                throttle_retries += 1
                self._limiter.on_throttled()
                COUNTERS.incr("throttled_429")
                self._emit(task_key, loop_n, "throttled_429", 429, None)
                if throttle_retries >= self.max_throttle_retries:
                    self._emit(task_key, loop_n, "gave_up", 429, "max throttle retries")
                    return FetchResult(False, 429, None, loop_n, "gave_up",
                                      error="exceeded throttle retry budget")
                retry_after = self._parse_retry_after(resp)
                delay = retry_after if retry_after is not None else self._backoff(throttle_retries)
                # Add jitter even on Retry-After to avoid a thundering herd.
                await asyncio.sleep(delay + random.uniform(0, 0.25))
                continue

            if 500 <= status < 600:
                real_attempts += 1
                await self._breaker.on_real_failure()
                self._emit(task_key, loop_n, "real_failure", status, f"HTTP {status}")
                if real_attempts >= self.max_real_attempts:
                    self._emit(task_key, loop_n, "gave_up", status, "max real attempts")
                    return FetchResult(False, status, None, loop_n, "gave_up",
                                      error=f"HTTP {status}")
                await asyncio.sleep(self._backoff(real_attempts))
                continue

            if status == 401:
                # Auth/credential expiry: transient, not a data failure. Refresh
                # the token/cert and retry. Does NOT trip the breaker.
                if self._on_auth_refresh and auth_refreshes < self.max_auth_refreshes:
                    auth_refreshes += 1
                    self._emit(task_key, loop_n, "auth_refresh", 401, None)
                    COUNTERS.incr("auth_refreshes")
                    await self._on_auth_refresh()
                    continue
                self._emit(task_key, loop_n, "gave_up", 401, "auth refresh exhausted")
                return FetchResult(False, 401, None, loop_n, "gave_up",
                                  error="unauthorized (refresh failed)")

            if status == 404:
                await self._breaker.on_success()
                self._emit(task_key, loop_n, "not_found", 404, None)
                return FetchResult(False, 404, None, loop_n, "not_found",
                                  error="HTTP 404")

            if 200 <= status < 300:
                # A 2xx means the endpoint is healthy -> breaker recovers, even if
                # the body turns out malformed (which is a separate data concern).
                await self._breaker.on_success()
                self._limiter.on_success()
                try:
                    payload = resp.json()
                except Exception as exc:
                    # Truncated/invalid body: retry a few times (could be transient),
                    # then quarantine. Never trips the breaker.
                    malformed_retries += 1
                    COUNTERS.incr("malformed_bodies")
                    self._emit(task_key, loop_n, "malformed", status, str(exc)[:120])
                    if malformed_retries >= self.max_malformed_retries:
                        return FetchResult(False, status, None, loop_n, "malformed",
                                          error=f"invalid json: {exc}")
                    await asyncio.sleep(self._backoff(malformed_retries))
                    continue
                self._emit(task_key, loop_n, "success", status, None)
                return FetchResult(
                    True, status, payload, loop_n, "success",
                    last_modified_at=resp.headers.get("Last-Modified"),
                )

            # Unexpected status: treat conservatively as a real failure.
            real_attempts += 1
            await self._breaker.on_real_failure()
            self._emit(task_key, loop_n, "real_failure", status, f"unexpected HTTP {status}")
            if real_attempts >= self.max_real_attempts:
                return FetchResult(False, status, None, loop_n, "gave_up",
                                  error=f"unexpected HTTP {status}")
            await asyncio.sleep(self._backoff(real_attempts))
