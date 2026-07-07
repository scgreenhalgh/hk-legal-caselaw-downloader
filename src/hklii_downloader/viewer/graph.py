"""Read-only citation-graph helpers over checkpoint.db.

All helpers take an already-opened read-only sqlite3.Connection (usually
from viewer.db.open_readonly). None of them write. Row shapes are dicts
so callers can evolve the returned columns without dataclass churn.

See docs/viewer-design.md §3. Option 3 scope: this module lives in the
viewer sub-package, not at hklii_downloader top level — the design's
'shared with future RAG' argument is deferred until RAG exists.
"""

from __future__ import annotations

import sqlite3

# Curial precedence for ORDER BY on citer courts. Higher courts (lower
# rank number) come first — a CFA citation is more authoritative than
# a DC one. ELSE 99 catches any court slug added upstream we haven't
# ranked yet (defensive; the ORDER BY still produces a deterministic
# result, just with unknown-court citers at the bottom).
_ORDER_BY_COURT_RANK = """
    CASE substr(from_key, 1, instr(from_key, '/') - 1)
        WHEN 'hkcfa'  THEN 0
        WHEN 'hkca'   THEN 1
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
    END
""".strip()


def cited_by(
    conn: sqlite3.Connection,
    case_key: str,
    *,
    court_filter: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> list[dict]:
    """Return cases that cite ``case_key``, ordered by court authority.

    Order: court_rank ASC, first_seen DESC within court.

    Bilingual (en+tc) citer rows collapse to one row per citer case; the
    ``langs`` column preserves both language codes as a comma-separated
    string (e.g. ``'en,tc'``).

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
            MAX(position) AS position,
            MAX(first_seen) AS first_seen
        FROM citations
        WHERE to_key = ?
        {court_pred}
        GROUP BY from_key
        ORDER BY {_ORDER_BY_COURT_RANK} ASC, first_seen DESC
        LIMIT ? OFFSET ?
    """
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
