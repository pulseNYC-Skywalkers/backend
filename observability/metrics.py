"""Lightweight, dependency-free observability for the pipeline.

Two things the demo cares about:
  1. A structured logger so every stage logs consistently.
  2. Per-stage counters (requests, 429s, real failures, retries, breaker trips,
     cache hits) that we can print at the end of a run and later surface in the
     dashboard. This is the "observability/" layer from the maturity story.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                          datefmt="%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


@dataclass
class Counters:
    """Thread-safe counter bag. Used to quantify the resilience story."""

    _values: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _started_at: float = field(default_factory=time.monotonic)

    def incr(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self._values[key] += amount

    def get(self, key: str) -> float:
        with self._lock:
            return self._values[key]

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            snap = dict(self._values)
        snap["elapsed_seconds"] = round(time.monotonic() - self._started_at, 2)
        return snap

    def render(self) -> str:
        return json.dumps(self.snapshot(), indent=2, sort_keys=True)


# Process-wide counters instance (a run is a process).
COUNTERS = Counters()
