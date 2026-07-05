"""Corpus validator — audits DB ↔ disk agreement across eight checks.

Reads-only against a completed scrape output directory (checkpoint.db
+ downloaded artifacts). Emits a typed ValidationReport that both a
JSON writer and a text writer consume. No HKLII traffic; no
--direct fallback; safe to run against a live corpus.

See scratchpad/VALIDATOR_SPEC.md for the design rationale, edge cases,
and TDD plan this implementation follows.
"""
from __future__ import annotations

import json
import random
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .content_shape import _looks_like_challenge_page
from .scraper import _extension_for_body

SCHEMA_VERSION = 1

# Ordered check keys — the spec's evaluation order (§2). Same order used
# for the --checks CLI option's parse-and-normalise, and for the text
# writer's section ordering.
CHECK_KEYS = (
    "presence",
    "magic",
    "challenge_html",
    "stem_coords",
    "neutral_in_body",
    "enrichment",
    "orphans",
    "html_pending",
)

# Sidecar suffixes we distinguish from base judgment sidecars during
# orphan/stem attribution. Ordering matters: longer suffixes first so
# `hkcfi_2023_1.appeal_history.json` matches before falling through to
# the generic `.json` handling below.
_ENRICHMENT_SUFFIXES = (
    ".summary_en.html",
    ".summary_zh.html",
    ".appeal_history.json",
)

# Judgment-body sidecar extensions we recognise for orphan/presence
# attribution. Doc-family covers the three magic-driven extensions
# scraper.py:44 chooses between.
_JUDGMENT_EXTS = (".html", ".txt", ".json", ".doc", ".docx", ".rtf")
_DOC_FAMILY_EXTS = (".doc", ".docx", ".rtf")

_STEM_PATTERN = re.compile(r"^([a-z]+)_(\d{4})_(\d+)$")
_SLUG_PATTERN = re.compile(r"^hk[a-z]+$")
_CITATION_YEAR_PREFIX = re.compile(r"^\[\d{4}\]\s*")


@dataclass
class Discrepancy:
    severity: str  # "fatal" | "warn" | "info"
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

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def _stem_of(name: str) -> str | None:
    """Return the judgment stem embedded in a filename, or None if the
    filename is not a recognised judgment/sidecar shape.

    Sidecar suffixes (.summary_*.html, .appeal_history.json) are peeled
    first — for `hkcfi_2023_1.appeal_history.json` the stem is
    `hkcfi_2023_1`, not `hkcfi_2023_1.appeal_history`.
    """
    for suf in _ENRICHMENT_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    root, dot, ext = name.rpartition(".")
    if not root or not dot:
        return None
    if f".{ext}" not in _JUDGMENT_EXTS:
        return None
    return root


def _parse_stem(stem: str) -> tuple[str, int, int] | None:
    m = _STEM_PATTERN.match(stem)
    if not m:
        return None
    court, year, number = m.groups()
    return court, int(year), int(number)


def _normalise_citation(text: str) -> str:
    """Fold whitespace + case for substring citation matching.

    Drops the `[YYYY]` prefix on neutral citations, lowercases everything,
    normalises NBSP/other Unicode whitespace to a single space. Applied
    to both haystack (body) and needle (neutral) so `[2023] HKCFI 155`
    matches `hkcfi\xa0155` inside a body.
    """
    s = _CITATION_YEAR_PREFIX.sub("", text)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


class Validator:
    """Runs the eight checks and returns a ValidationReport.

    Stateless per run(): construct with a DB handle + output dir + optional
    checks/sample/seed; call run() to get a fresh report. Read-only against
    the DB. --fix remediation lives on a separate method (`apply_fixes`).
    """

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
        if checks is None:
            self._checks = list(CHECK_KEYS)
        else:
            unknown = [c for c in checks if c not in CHECK_KEYS]
            if unknown:
                raise ValueError(f"unknown check(s): {unknown!r}")
            self._checks = list(checks)
        self._sample = sample
        self._seed = seed

    def run(self) -> ValidationReport:
        rows = self._select_rows()
        stems_in_db: set[str] = {f"{r[0]}_{r[1]}_{r[2]}" for r in rows}

        discrepancies: list[Discrepancy] = []
        files_examined = 0

        for row in rows:
            court, year, number, neutral, formats_json, se, sz, ah, _hp = row
            formats = json.loads(formats_json) if formats_json else []
            stem = f"{court}_{year}_{number}"
            case_dir = self._output_dir / court / str(year)

            if "presence" in self._checks:
                for fmt in formats:
                    path = self._locate_format_file(case_dir, stem, fmt)
                    files_examined += 1
                    if path is None:
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="presence",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}.{fmt}",
                            expected=fmt,
                            detail=f"expected {fmt} file missing or zero-byte",
                        ))

            if "magic" in self._checks:
                for ext in _DOC_FAMILY_EXTS:
                    path = case_dir / f"{stem}{ext}"
                    if not path.exists():
                        continue
                    files_examined += 1
                    with open(path, "rb") as f:
                        head = f.read(4)
                    resolved = _extension_for_body(head)
                    if resolved is None:
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="magic",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}{ext}",
                            expected=ext,
                            observed=head.hex(),
                            detail=f"unknown doc-family magic {head.hex()!r}",
                        ))
                    elif resolved != ext:
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="magic",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}{ext}",
                            expected=ext,
                            observed=resolved,
                            detail=(
                                f"body magic implies {resolved} but extension "
                                f"is {ext}"
                            ),
                        ))

            if "challenge_html" in self._checks:
                for suffix in (".html", ".summary_en.html", ".summary_zh.html"):
                    path = case_dir / f"{stem}{suffix}"
                    if not path.exists():
                        continue
                    files_examined += 1
                    try:
                        body = path.read_text(errors="replace")
                    except OSError:
                        continue
                    if _looks_like_challenge_page(body):
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="challenge_html",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}{suffix}",
                            detail="body matches a WAF/challenge page marker",
                        ))

            if "neutral_in_body" in self._checks and neutral:
                body_path = case_dir / f"{stem}.txt"
                if not body_path.exists():
                    body_path = case_dir / f"{stem}.html"
                if body_path.exists():
                    try:
                        body = body_path.read_text(errors="replace")
                    except OSError:
                        body = ""
                    needle = _normalise_citation(neutral)
                    haystack = _normalise_citation(body)
                    if needle and needle not in haystack:
                        discrepancies.append(Discrepancy(
                            severity="warn", check="neutral_in_body",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{body_path.name}",
                            expected=needle,
                            detail=f"body missing normalised citation {needle!r}",
                        ))

            if "enrichment" in self._checks:
                pairs = (
                    ("summary_en", ".summary_en.html", se),
                    ("summary_zh", ".summary_zh.html", sz),
                    ("appeal_history", ".appeal_history.json", ah),
                )
                for kind, suffix, status in pairs:
                    path = case_dir / f"{stem}{suffix}"
                    exists = path.exists()
                    if status == "downloaded" and not exists:
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="enrichment",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}{suffix}",
                            expected="present",
                            observed="missing",
                            detail=(
                                f"{kind}_status='downloaded' but sidecar missing"
                            ),
                        ))
                    elif status != "downloaded" and exists:
                        discrepancies.append(Discrepancy(
                            severity="fatal", check="enrichment",
                            court=court, year=year, number=number,
                            path=f"{court}/{year}/{stem}{suffix}",
                            expected="absent",
                            observed="present",
                            detail=(
                                f"{kind}_status={status!r} but sidecar exists"
                            ),
                        ))

        if "stem_coords" in self._checks:
            for path in self._walk_output():
                stem = _stem_of(path.name)
                if stem is None:
                    continue
                parsed = _parse_stem(stem)
                if parsed is None:
                    continue
                p_court, p_year, p_num = parsed
                slug_dir = path.parent.parent.name
                year_dir = path.parent.name
                if slug_dir != p_court or year_dir != str(p_year):
                    discrepancies.append(Discrepancy(
                        severity="fatal", check="stem_coords",
                        court=p_court, year=p_year, number=p_num,
                        path=str(path.relative_to(self._output_dir)),
                        expected=f"{p_court}/{p_year}/",
                        observed=f"{slug_dir}/{year_dir}/",
                        detail="filename stem does not match parent directory",
                    ))

        if "orphans" in self._checks:
            for path in self._walk_output():
                stem = _stem_of(path.name)
                if stem is None or stem not in stems_in_db:
                    discrepancies.append(Discrepancy(
                        severity="warn", check="orphans",
                        path=str(path.relative_to(self._output_dir)),
                        detail=(
                            f"file has no matching DB row (inferred stem="
                            f"{stem!r})"
                        ),
                    ))

        html_pending_count = 0
        if "html_pending" in self._checks:
            html_pending_count = self._db._conn.execute(
                "SELECT COUNT(*) FROM cases "
                "WHERE html_pending_at_hklii IS NOT NULL"
            ).fetchone()[0]

        counts_by_sev = {"fatal": 0, "warn": 0, "info": 0}
        for d in discrepancies:
            counts_by_sev[d.severity] = counts_by_sev.get(d.severity, 0) + 1

        return ValidationReport(
            schema_version=SCHEMA_VERSION,
            output_dir=str(self._output_dir),
            generated_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            counts={
                "rows_examined": len(rows),
                "files_examined": files_examined,
                "checks_run": list(self._checks),
                "discrepancies_by_severity": counts_by_sev,
                "sampled": self._sample is not None,
                "html_pending_at_hklii": html_pending_count,
            },
            discrepancies=discrepancies,
            enrichment_stats=self._db.enrichment_stats(),
            checkpoint_stats=self._db.stats(),
        )

    def _locate_format_file(
        self, case_dir: Path, stem: str, fmt: str,
    ) -> Path | None:
        """Return an existing non-zero file for the given fmt, or None.

        For `fmt='doc'`, accept any of .doc / .docx / .rtf — magic drives
        the on-disk extension (scraper.py:44). Mirrors the docx-fallback
        used in verify_downloaded_against_files (checkpoint.py:278-303).
        """
        if fmt == "doc":
            candidates = [case_dir / f"{stem}{e}" for e in _DOC_FAMILY_EXTS]
        else:
            candidates = [case_dir / f"{stem}.{fmt}"]
        for p in candidates:
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def _select_rows(self) -> list[tuple]:
        cur = self._db._conn.execute(
            "SELECT court, year, number, neutral, formats, "
            "summary_en_status, summary_zh_status, appeal_history_status, "
            "html_pending_at_hklii "
            "FROM cases WHERE status='downloaded' "
            "ORDER BY court, year, number"
        )
        rows = cur.fetchall()
        if self._sample is not None:
            rng = random.Random(self._seed)
            rng.shuffle(rows)
            rows = rows[: self._sample]
        return rows

    def _walk_output(self) -> Iterable[Path]:
        """Yield judgment/sidecar files under recognised court/year dirs.

        Skips .enum_cache/ and failure_samples/ at the output root; skips
        anything whose top-level name doesn't match the `hk<letters>` slug
        shape (checked via _SLUG_PATTERN).
        """
        if not self._output_dir.exists():
            return
        for slug_dir in self._output_dir.iterdir():
            if not slug_dir.is_dir():
                continue
            if not _SLUG_PATTERN.match(slug_dir.name):
                continue
            for year_dir in slug_dir.iterdir():
                if not year_dir.is_dir():
                    continue
                for f in year_dir.iterdir():
                    if f.is_file():
                        yield f
