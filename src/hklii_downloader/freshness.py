"""Phase D2 freshness runner — stub.

Full implementation lands in the paired feat commit. This stub exists
solely so ``tests/test_freshness.py`` reaches assertion-level failures
under the TDD ordering (test → fail → implement → pass).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreshnessRow:
    kind: str
    scope: str
    lang: str


@dataclass(frozen=True)
class ProbeOutcome:
    row: FreshnessRow
    url: str
    ok: bool
    live_count: int | None
    live_updated_at: str | None
    probed_at: int
    error: str | None


def dispatch_url(category: str, slug: str, lang: str) -> str | None:
    raise NotImplementedError


def _fresh(row) -> bool:
    raise NotImplementedError


class FreshnessRunner:
    def __init__(self, *, get, checkpoint, matrix, timeout: float = 15.0):
        raise NotImplementedError
