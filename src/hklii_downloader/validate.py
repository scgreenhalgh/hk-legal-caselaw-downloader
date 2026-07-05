"""Corpus validator — audit DB ↔ disk agreement.

Stub. Full implementation lands in the paired impl commit; this stub exists
so the test file can execute assertions instead of failing on ModuleNotFoundError.
Every check returns an empty discrepancy list.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class Discrepancy:
    severity: str
    check: str
    detail: str
    court: str | None = None
    year: int | None = None
    number: int | None = None
    path: str | None = None
    expected: str | None = None
    observed: str | None = None


@dataclass
class ValidationReport:
    schema_version: int
    output_dir: str
    generated_at: str
    counts: dict
    discrepancies: list[Discrepancy] = field(default_factory=list)
    enrichment_stats: dict = field(default_factory=dict)
    checkpoint_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "output_dir": self.output_dir,
            "generated_at": self.generated_at,
            "counts": self.counts,
            "discrepancies": [asdict(d) for d in self.discrepancies],
            "enrichment_stats": self.enrichment_stats,
            "checkpoint_stats": self.checkpoint_stats,
        }


class Validator:
    def __init__(
        self,
        db,
        output_dir,
        checks: list[str] | None = None,
        sample: int | None = None,
        seed: int | None = None,
    ) -> None:
        self._db = db
        self._output_dir = Path(output_dir)
        self._checks = checks
        self._sample = sample
        self._seed = seed

    def run(self) -> ValidationReport:
        return ValidationReport(
            schema_version=SCHEMA_VERSION,
            output_dir=str(self._output_dir),
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            counts={
                "rows_examined": 0,
                "files_examined": 0,
                "checks_run": self._checks or [],
                "discrepancies_by_severity": {"fatal": 0, "warn": 0, "info": 0},
                "sampled": self._sample is not None,
            },
            discrepancies=[],
            enrichment_stats={},
            checkpoint_stats={},
        )
