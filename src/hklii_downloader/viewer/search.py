"""Search index build helpers over the on-disk corpus + viewer.db.

Owns:
- BodySource dataclass: one entry per (case, language) on disk
- discover_body_sources: bilingual sibling probe
- (Phase 2.5+) extract_plaintext, body_sha256, upsert_case, rebuild_index

See docs/viewer-design.md §4.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hklii_downloader.viewer.body_render.text import iter_text_nodes


#: Any run of whitespace (including newlines) collapses to a single space
#: in the extracted plaintext. Matches source-format churn that would
#: otherwise perturb body_sha256.
_WHITESPACE_RUN = re.compile(r"\s+")


def extract_plaintext(html_content: str | bytes) -> str:
    """Extract normalized plaintext for FTS indexing.

    Uses iter_text_nodes with DEFAULT_SKIP_TAGS (a/code/pre) plus the
    always-skip infrastructure set (script/style/head/…). Concatenates
    yielded text nodes with a single space, then collapses whitespace
    runs and strips the ends.

    Empty body → empty string. Callers may skip writing an FTS row for
    an empty body, or write a row whose body_sha256 is the empty-string
    sentinel — either is valid; the point is that the two states are
    distinguishable.
    """
    nodes = list(iter_text_nodes(html_content))
    if not nodes:
        return ""
    joined = " ".join(nodes)
    return _WHITESPACE_RUN.sub(" ", joined).strip()


def body_sha256(plaintext: str) -> str:
    """Return the SHA-256 hex digest of ``plaintext`` (utf-8 encoded).

    Basis of the incremental-index diff: a case's body_sha256 unchanged
    since the last index run means we can skip the reindex.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# index_case — single-case orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexResult:
    """Summary of what index_case did for one case.

    action codes:
      - 'no_case_row': case_key absent from checkpoint.cases (skip)
      - 'no_body_on_disk': cases row exists but no body files (skip)
      - 'indexed': at least one (lang) inserted or replaced
      - 'unchanged': every (lang)'s body_sha256 matched the stored row
    """

    case_key: str
    action: str
    langs_indexed: tuple[str, ...] = field(default_factory=tuple)
    langs_unchanged: tuple[str, ...] = field(default_factory=tuple)


def _default_now_iso() -> str:
    """UTC ISO-8601 with second precision + trailing 'Z' (matches the
    format the downloader uses on first_seen etc)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fetch_case_row(
    cp_conn: sqlite3.Connection, case_key: str
) -> dict | None:
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year_s, num_s = parts[0], parts[1], parts[2]
    row = cp_conn.execute(
        "SELECT court, year, number, neutral, title, date, lang "
        "FROM cases WHERE court = ? AND year = ? AND number = ?",
        [court, int(year_s), int(num_s)],
    ).fetchone()
    if row is None:
        return None
    return {
        "court": row[0],
        "year": row[1],
        "number": row[2],
        "neutral": row[3],
        "title": row[4],
        "date": row[5],
        "lang": row[6],
    }


def index_case(
    vw_conn: sqlite3.Connection,
    cp_conn: sqlite3.Connection,
    output_root: str | Path,
    case_key: str,
    *,
    now_iso: str | None = None,
) -> IndexResult:
    """Index one case into viewer.db.

    Reads case metadata from ``cp_conn`` (checkpoint.db, expected columns:
    court/year/number/neutral/title/date/lang). Discovers on-disk bodies
    via :func:`discover_body_sources`. For each source: extracts plaintext,
    computes sha, compares to the stored sha in fts_cases; if unchanged,
    skips; otherwise INSERT-OR-REPLACEs both fts_cases and case_bodies
    (which triggers fts_body sync via the schema's AFTER-DELETE/INSERT
    triggers).

    Returns an :class:`IndexResult` summarizing action taken.
    ``now_iso`` overrides the timestamp for testing; production omits it
    and gets UTC now.
    """
    now = now_iso if now_iso is not None else _default_now_iso()
    case_row = _fetch_case_row(cp_conn, case_key)
    if case_row is None:
        return IndexResult(case_key=case_key, action="no_case_row")

    sources = discover_body_sources(output_root, case_key, case_row["lang"])
    if not sources:
        return IndexResult(case_key=case_key, action="no_body_on_disk")

    indexed: list[str] = []
    unchanged: list[str] = []
    for source in sources:
        html_bytes = source.path.read_bytes()
        plaintext = extract_plaintext(html_bytes)
        sha = body_sha256(plaintext)

        existing = vw_conn.execute(
            "SELECT body_sha256 FROM fts_cases "
            "WHERE case_key = ? AND lang = ?",
            [case_key, source.lang],
        ).fetchone()
        if existing is not None and existing[0] == sha:
            unchanged.append(source.lang)
            continue

        # UPSERT — INSERT OR REPLACE would delete-then-reinsert case_bodies,
        # but SQLite suppresses DELETE-triggers on REPLACE unless
        # PRAGMA recursive_triggers=1 (per-connection, easy to forget).
        # ON CONFLICT DO UPDATE fires the AFTER UPDATE trigger which keeps
        # the row's id stable and correctly resyncs fts_body — proven in
        # the schema tests + a manual trace.
        vw_conn.execute(
            "INSERT INTO fts_cases "
            "(case_key, lang, court, year, number, neutral, title, date, "
            " body_source, body_sha256, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (case_key, lang) DO UPDATE SET "
            " court = excluded.court, year = excluded.year, "
            " number = excluded.number, neutral = excluded.neutral, "
            " title = excluded.title, date = excluded.date, "
            " body_source = excluded.body_source, "
            " body_sha256 = excluded.body_sha256, "
            " indexed_at = excluded.indexed_at",
            [
                case_key,
                source.lang,
                case_row["court"],
                case_row["year"],
                case_row["number"],
                case_row["neutral"],
                case_row["title"],
                case_row["date"],
                source.source_kind,
                sha,
                now,
            ],
        )
        vw_conn.execute(
            "INSERT INTO case_bodies "
            "(case_key, lang, title, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (case_key, lang) DO UPDATE SET "
            " title = excluded.title, body = excluded.body",
            [case_key, source.lang, case_row["title"], plaintext],
        )
        indexed.append(source.lang)

    vw_conn.commit()
    if indexed:
        return IndexResult(
            case_key=case_key,
            action="indexed",
            langs_indexed=tuple(indexed),
            langs_unchanged=tuple(unchanged),
        )
    return IndexResult(
        case_key=case_key,
        action="unchanged",
        langs_unchanged=tuple(unchanged),
    )


@dataclass(frozen=True)
class BodySource:
    """One indexable body for a (case, language) pair.

    Attributes:
      lang: 'en' or 'tc' — the language the body is written in
      path: absolute or relative Path to the file on disk
      source_kind: one of 'html', 'tc.html', 'generated.html' — the
        physical file variant. Downstream (Phase 3 render) uses this
        to pick the right dispatch branch (native HKLII shape vs
        pandoc fragment).
    """

    lang: str
    path: Path
    source_kind: str


def discover_body_sources(
    output_root: str | Path,
    case_key: str,
    case_lang: str,
) -> list[BodySource]:
    """Enumerate the on-disk body sources for a case.

    Rules (design §4 line 82, INDEX-time enumeration):
      - ``{stem}.tc.html`` is unambiguously TC (regardless of case.lang)
      - ``{stem}.html`` is EN when a .tc.html sibling exists (bilingual
        pair); otherwise it reflects case.lang
      - ``{stem}.generated.html`` fills the case.lang slot as a fallback
        for cases without a real .html body; it never overrides a real
        .html

    Returns one BodySource per language present on disk. Empty list if
    the case has no body files (L5: distinct from a raise — the case
    simply has nothing to index yet).

    Raises ValueError for a malformed case_key (< 2 slashes).
    """
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year, num = parts[0], parts[1], parts[2]
    stem = f"{court}_{year}_{num}"
    d = Path(output_root) / court / year

    html_path = d / f"{stem}.html"
    tc_html_path = d / f"{stem}.tc.html"
    gen_html_path = d / f"{stem}.generated.html"

    sources: list[BodySource] = []

    # .tc.html: always TC
    if tc_html_path.exists():
        sources.append(
            BodySource(lang="tc", path=tc_html_path, source_kind="tc.html")
        )

    # .html: EN if bilingual, else case.lang
    if html_path.exists():
        html_lang = "en" if tc_html_path.exists() else case_lang
        sources.append(
            BodySource(lang=html_lang, path=html_path, source_kind="html")
        )

    # .generated.html: covers case.lang if that language has no source yet
    covered_langs = {s.lang for s in sources}
    if case_lang not in covered_langs and gen_html_path.exists():
        sources.append(
            BodySource(
                lang=case_lang,
                path=gen_html_path,
                source_kind="generated.html",
            )
        )

    return sources
