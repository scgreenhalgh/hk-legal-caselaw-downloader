"""Phase D1 discovery — stub replaced in the next commit."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatabaseMatrix:
    cases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    legis: dict[str, tuple[str, ...]] = field(default_factory=dict)
    other: dict[str, tuple[str, ...]] = field(default_factory=dict)


def parse_databases_matrix(html: str) -> DatabaseMatrix:
    return DatabaseMatrix()
