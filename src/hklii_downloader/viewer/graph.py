"""Read-only citation-graph helpers over checkpoint.db + viewer.db.

Reader functions take an already-opened sqlite3.Connection. Most helpers
read checkpoint.db (via viewer.db.open_readonly); hub_cases and
inbound_counts read the viewer-owned viewer.db (see viewer.schema).
Row shapes are dicts so callers can evolve returned columns without
dataclass churn.

See docs/viewer-design.md §3. Option 3 scope: this module lives in the
viewer sub-package, not at hklii_downloader top level — the design's
'shared with future RAG' argument is deferred until RAG exists.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable


class ViewerCacheMissing(Exception):
    """viewer.db is missing an expected table.

    Signals that ``hklii viewer index`` has not been run yet. Route
    handlers catch this to render the 'run index first' banner — L1
    lens: never conflate 'missing setup' with 'setup succeeded but
    cache is empty' (which returns ``[]``).
    """


# Curial precedence for ORDER BY on court slugs. Higher courts (lower rank
# number) come first. Covers all 13 shipped-downloader canonical slugs
# (ALL_COURTS in hklii_downloader/cli.py). ELSE 99 is reserved for
# schema-drift or genuinely unknown courts — a UKPC citer must not land
# in that bucket. ukpc is tied with hkca at rank 1: pre-1997 UK Privy
# Council was the ultimate appellate court for HK; post-1997 CFA
# replaced it but UKPC precedents are still cited with near-apex weight.
_COURT_RANK_WHEN_ELSE = """
    WHEN 'hkcfa'  THEN 0
    WHEN 'hkca'   THEN 1
    WHEN 'ukpc'   THEN 1
    WHEN 'hkcfi'  THEN 2
    WHEN 'hkdc'   THEN 3
    WHEN 'hkmagc' THEN 4
    WHEN 'hkfc'   THEN 5
    WHEN 'hkldt'  THEN 6
    WHEN 'hklat'  THEN 7
    WHEN 'hkct'   THEN 8
    WHEN 'hksct'  THEN 9
    WHEN 'hkcrc'  THEN 10
    WHEN 'hkoat'  THEN 11
    ELSE 99
""".strip()


def _court_rank_case(key_column: str) -> str:
    """SQL CASE expression returning a numeric court rank from a case_key column.

    Callers pass a bareword column name (``from_key`` / ``to_key``) — NEVER
    user input. Injection risk is nil because callers are static strings
    in this module only.
    """
    return (
        f"CASE substr({key_column}, 1, instr({key_column}, '/') - 1)\n"
        f"    {_COURT_RANK_WHEN_ELSE}\n"
        f"END"
    )


def cited_by(
    conn: sqlite3.Connection,
    case_key: str,
    *,
    court_filter: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> list[dict]:
    """Return cases that cite ``case_key``, ordered by court authority.

    Order: court_rank ASC (over the citer court), first_seen DESC within
    court. Bilingual (en+tc) citer rows collapse to one row per citer case;
    the ``langs`` column preserves both language codes.

    Returns ``[]`` for a case_key with no incoming citations — L5
    ambiguous-state: absence of citations is a legitimate answer,
    distinct from a raise, distinct from a missing hub cache.
    """
    offset = max(0, (page - 1) * per_page)
    params: list[object] = [case_key]
    court_pred = ""
    if court_filter is not None:
        court_pred = (
            "AND substr(from_key, 1, instr(from_key, '/') - 1) = ?"
        )
        params.append(court_filter)
    params.extend([per_page, offset])

    sql = f"""
        SELECT
            from_key,
            to_key,
            substr(from_key, 1, instr(from_key, '/') - 1) AS from_court,
            GROUP_CONCAT(DISTINCT citer_lang) AS langs,
            MAX(citer_freq) AS citer_freq,
            MIN(position) AS position,
            MAX(first_seen) AS first_seen
        FROM citations
        WHERE to_key = ?
        {court_pred}
        GROUP BY from_key
        ORDER BY {_court_rank_case("from_key")} ASC, first_seen DESC
        LIMIT ? OFFSET ?
    """
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def authorities_cited(
    conn: sqlite3.Connection,
    case_key: str,
    *,
    page: int = 1,
    per_page: int = 50,
) -> list[dict]:
    """Return cases cited BY ``case_key``, ordered by cited court authority.

    Symmetric to :func:`cited_by`. Order: cited court_rank ASC (over
    ``to_key``), first_seen DESC within court. Bilingual citation rows
    for the same (from, to) collapse to one; ``langs`` preserves both.

    Returns ``[]`` for a case_key that cites nothing (or doesn't exist
    in the corpus — same L5 signal).
    """
    offset = max(0, (page - 1) * per_page)
    sql = f"""
        SELECT
            from_key,
            to_key,
            substr(to_key, 1, instr(to_key, '/') - 1) AS to_court,
            GROUP_CONCAT(DISTINCT citer_lang) AS langs,
            MAX(citer_freq) AS citer_freq,
            MIN(position) AS position,
            MAX(first_seen) AS first_seen
        FROM citations
        WHERE from_key = ?
        GROUP BY to_key
        ORDER BY {_court_rank_case("to_key")} ASC, first_seen DESC
        LIMIT ? OFFSET ?
    """
    cur = conn.execute(sql, [case_key, per_page, offset])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def parallel_cites(
    conn: sqlite3.Connection,
    case_key: str,
) -> list[str]:
    """Return the parallel-citation strings for ``case_key``, sorted ASC.

    Reads case_parallel_cites, the small 11k-row shipped table. Returns
    an empty list for a case_key with no parallel cites (L5).
    """
    cur = conn.execute(
        "SELECT parallel_cite FROM case_parallel_cites "
        "WHERE case_key = ? ORDER BY parallel_cite ASC",
        [case_key],
    )
    return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# viewer.db readers — hub_cases + inbound_counts read viewer.db, not checkpoint.db
# ---------------------------------------------------------------------------


def _require_viewer_hub_cache_table(conn: sqlite3.Connection) -> None:
    """Fail loudly if viewer_hub_cache is absent — L1 signal that setup
    was skipped, distinguishable from 'cache exists but empty'.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema "
        "WHERE type='table' AND name='viewer_hub_cache'"
    ).fetchone()
    if row is None:
        raise ViewerCacheMissing(
            "viewer_hub_cache table missing — run `hklii viewer index`"
        )


def hub_cases(
    vw_conn: sqlite3.Connection,
    *,
    court: str | None = None,
    min_inbound: int = 5,
    limit: int = 100,
) -> list[dict]:
    """Return top-ranked hub cases from viewer.db.viewer_hub_cache.

    Order: inbound_count DESC, case_key ASC (stable tiebreak).

    Args:
      court: restrict to a court slug prefix (e.g. 'hkcfa')
      min_inbound: default 5 — filters out cases too small to be 'hubs'
      limit: max rows returned (default 100)

    Raises :class:`ViewerCacheMissing` when the table is absent (L1).
    Returns ``[]`` when the table exists but the query yields no rows (L5).
    """
    _require_viewer_hub_cache_table(vw_conn)

    where_clauses = ["inbound_count >= ?"]
    params: list[object] = [min_inbound]
    if court is not None:
        where_clauses.append(
            "substr(case_key, 1, instr(case_key, '/') - 1) = ?"
        )
        params.append(court)
    params.append(limit)

    sql = f"""
        SELECT
            case_key,
            substr(case_key, 1, instr(case_key, '/') - 1) AS court,
            inbound_count,
            computed_at
        FROM viewer_hub_cache
        WHERE {' AND '.join(where_clauses)}
        ORDER BY inbound_count DESC, case_key ASC
        LIMIT ?
    """
    cur = vw_conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def inbound_counts(
    vw_conn: sqlite3.Connection,
    case_keys: Iterable[str],
) -> dict[str, int]:
    """Batch inbound-count lookup for a set of case_keys.

    Missing case_keys are ABSENT from the returned dict — L5 ambiguous-state:
    callers can distinguish 'cached with 0' (present, value 0) from
    'never computed' (absent from dict).

    An empty ``case_keys`` input returns ``{}`` without touching the schema —
    routes that batch-decorate empty result sets don't spuriously raise
    :class:`ViewerCacheMissing`.

    Raises :class:`ViewerCacheMissing` when case_keys is non-empty and the
    table is absent — L1.
    """
    keys = list(case_keys)
    if not keys:
        return {}

    _require_viewer_hub_cache_table(vw_conn)

    placeholders = ",".join("?" * len(keys))
    cur = vw_conn.execute(
        f"SELECT case_key, inbound_count FROM viewer_hub_cache "
        f"WHERE case_key IN ({placeholders})",
        keys,
    )
    return {row[0]: row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# appeal_chain — reads output/{court}/{year}/{stem}.appeal_history.json
# ---------------------------------------------------------------------------


def appeal_chain(output_root: str | Path, case_key: str) -> list[dict]:
    """Read a case's appeal chain from its per-case JSON sidecar.

    Path: ``{output_root}/{court}/{year}/{court}_{year}_{n}.appeal_history.json``
    (the FLAT layout the downloader writes — see docs/viewer-design.md §5).
    Returns the parsed JSON list of ``{act, judgments: [...]}`` dicts.

    Contract:
      - File absent → ``[]`` (legitimate 'no appeal chain'; most corpus
        cases do not have a sidecar and that is not an error)
      - File present but not valid JSON → :class:`json.JSONDecodeError`
        propagates (L1: real data corruption is not silently hidden)
      - Malformed ``case_key`` (< 2 slashes) → :class:`ValueError`
    """
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year, num = parts[0], parts[1], parts[2]
    path = (
        Path(output_root)
        / court
        / year
        / f"{court}_{year}_{num}.appeal_history.json"
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return json.loads(raw)
