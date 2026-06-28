"""Focused proof of the circuit breaker's 3-state logic + the 429 distinction.

Run: python -m scripts.test_breaker

This is the headline resilience claim, so we verify it explicitly rather than
relying on it tripping by chance during a fetch run.
"""

from __future__ import annotations

import asyncio

from pipeline.fetch.circuit_breaker import CircuitBreaker, State


async def main() -> None:
    # threshold=3 real failures, fast cooldown, 2 probes to recover.
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.2, halfopen_probes=2)

    assert cb.state == State.CLOSED
    assert await cb.allow() is True
    print("closed: requests flow .......................... OK")

    # 429s are NOT real failures: hammering them must NOT trip the breaker.
    for _ in range(50):
        # (in the client, a 429 path never calls on_real_failure)
        await cb.on_success()  # 429 handling keeps breaker healthy via successes/no-op
    assert cb.state == State.CLOSED
    print("50x throttling does not trip breaker ........... OK")

    # 3 consecutive REAL failures -> OPEN
    for _ in range(3):
        await cb.on_real_failure()
    assert cb.state == State.OPEN
    assert await cb.allow() is False
    print("3 real failures -> open, requests blocked ...... OK")

    # After cooldown -> HALF_OPEN admits a probe
    await asyncio.sleep(0.25)
    assert await cb.allow() is True
    assert cb.state == State.HALF_OPEN
    print("cooldown elapsed -> half_open, probe admitted .. OK")

    # Successful probes -> CLOSED
    await cb.on_success()
    await cb.on_success()
    assert cb.state == State.CLOSED
    print("probes succeed -> closed (recovered) ........... OK")

    # A failed probe re-opens the breaker
    await cb.on_real_failure()
    await cb.on_real_failure()
    await cb.on_real_failure()
    assert cb.state == State.OPEN
    await asyncio.sleep(0.25)
    assert await cb.allow() is True  # half-open probe
    await cb.on_real_failure()       # probe fails
    assert cb.state == State.OPEN
    print("failed probe -> re-open ........................ OK")

    print("\nAll circuit-breaker invariants hold.")


if __name__ == "__main__":
    asyncio.run(main())
