"""Grounding primitives — the anti-hallucination guarantee.

A "fact" is only allowed to exist if we can point to the exact character span in
the source text that it came from. If a value can't be located in the source, we
return None (grounded-or-null). This is what stops a model — or a buggy parser —
from inventing a measurement that isn't in the note.
"""

from __future__ import annotations

import hashlib
from typing import Any


def source_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def find_span(text: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Locate `needle` in `text` (case-insensitive). Returns [start, end) or None."""
    if not text or needle is None:
        return None
    needle = str(needle)
    if not needle:
        return None
    idx = text.lower().find(needle.lower(), start)
    if idx < 0:
        return None
    return (idx, idx + len(needle))


def fact(value: Any, span: tuple[int, int] | None, source: str, confidence: float) -> dict[str, Any] | None:
    """Build a grounded fact, or None if it isn't grounded. No span -> no fact."""
    if value is None or span is None:
        return None
    return {
        "value": value,
        "span": [span[0], span[1]],
        "source": source,
        "confidence": round(confidence, 2),
    }


def verify_against_source(value: str, text: str) -> tuple[int, int] | None:
    """Used for LLM output: only keep a value the model claims to have read if it
    actually appears verbatim in the source. Otherwise it's a hallucination."""
    return find_span(text, value)
