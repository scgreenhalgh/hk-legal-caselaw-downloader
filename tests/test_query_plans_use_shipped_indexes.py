"""Smoke test: shipped query indexes are actually used by helper SQL.

Design §10 line 323 (docs/viewer-design.md):

    Perf tests use EXPLAIN QUERY PLAN, not wall-clock (verdict-wrong-tool fix):
    ``assert 'idx_cit_to' in plan and 'SCAN' not in plan``. Scale-invariant
    — catches missing-index at 20 rows and 11,450 rows alike.

Guard rail rationale — L1 lens (silent skip): if someone drops ``idx_cit_to``,
or refactors a helper to a query shape the optimizer cannot route through the
index, the WHERE clause still returns correct rows, just via a full scan of
citations (~240k rows in production). Functional tests stay green while
production degrades ms → seconds. This test surfaces that failure mode at
20-row fixture scale by asserting on the query plan directly.

Why capture-then-explain, not extract-and-replay: ``graph.py`` builds SQL
inline with f-strings (e.g. ``_court_rank_case`` interpolation) — there is
no module-level SQL constant to import. ``set_trace_callback`` gives us the
exact string the optimizer saw, with parameters already substituted, without
coupling this test to any private symbol.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import (
    authorities_cited,
    cited_by,
    hub_cases,
)

from tests._route_helpers import (
    build_viewer_db,
    seed_citations,
    seed_hub_cache,
)


def _explain(conn: sqlite3.Connection, sql: str) -> str:
    """Concatenate the ``detail`` column of every EXPLAIN QUERY PLAN row.

    EXPLAIN QUERY PLAN rows are ``(id, parent, notused, detail)``; only
    ``detail`` carries the readable node names ('SEARCH … USING INDEX …',
    'SCAN …', 'USE TEMP B-TREE …'). Joining with newlines gives a single
    string the callers can substring-match against.
    """
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return "\n".join(row[3] for row in rows)


def _capture_last_sql(
    conn: sqlite3.Connection,
    run: Callable[[], object],
) -> str:
    """Return the LAST SQL statement executed by ``run()``.

    Some helpers (``hub_cases``) issue a schema-existence check before
    the main read query — we want the read query's plan, so ``[-1]``.
    Trace-callback substitutes parameters inline, so the captured string
    is directly executable under EXPLAIN QUERY PLAN.
    """
    captured: list[str] = []
    conn.set_trace_callback(lambda s: captured.append(s))
    try:
        run()
    finally:
        conn.set_trace_callback(None)
    assert captured, "helper did not execute any SQL"
    return captured[-1]


# ---------------------------------------------------------------------------
# citations table — idx_cit_to (on to_key) + PRIMARY KEY (leading from_key)
# ---------------------------------------------------------------------------


def test_cited_by_query_plan_uses_idx_cit_to(tmp_path: Path) -> None:
    """cited_by MUST route through ``idx_cit_to`` on ``to_key``.

    L1 silent-skip: without the index the query still returns correct rows,
    just via SCAN of the full citations table (~240k rows in production).
    Assert on the plan directly so the regression is loud at 20-row scale.

    The design §10 pattern is quoted verbatim (line 323): ``assert
    'idx_cit_to' in plan and 'SCAN' not in plan``. We narrow ``SCAN`` to
    ``SCAN citations`` because 'USE TEMP B-TREE FOR ORDER BY' does not
    contain 'SCAN' but a future optimizer node might.
    """
    db = tmp_path / "checkpoint.db"
    seed_citations(
        db,
        [
            ("hkcfi/2023/1", "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        sql = _capture_last_sql(
            conn, lambda: cited_by(conn, "hkcfa/2020/1")
        )
        plan = _explain(conn, sql)
        assert "idx_cit_to" in plan, plan
        assert "SCAN citations" not in plan, plan
    finally:
        conn.close()


def test_authorities_cited_query_plan_uses_primary_key_on_from_key(
    tmp_path: Path,
) -> None:
    """authorities_cited MUST route through the citations PK on ``from_key``.

    Citations PK is ``(from_key, to_key, citer_lang) WITHOUT ROWID`` —
    ``from_key`` is the leading column, so ``WHERE from_key = ?`` can seek
    directly. Any refactor that wraps ``from_key`` in a function
    (``LOWER(from_key)``, ``substr(from_key, ...)``) breaks the index
    route silently — L2 semantic-drift lens: query still returns correct
    rows, just via full-scan. Plan assertion pins the invariant.
    """
    db = tmp_path / "checkpoint.db"
    seed_citations(
        db,
        [
            ("hkcfi/2023/1", "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        sql = _capture_last_sql(
            conn, lambda: authorities_cited(conn, "hkcfi/2023/1")
        )
        plan = _explain(conn, sql)
        assert "PRIMARY KEY" in plan, plan
        assert "SCAN citations" not in plan, plan
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# viewer_hub_cache — WITHOUT ROWID so PK IS the storage; no secondary index
# ---------------------------------------------------------------------------


def test_hub_cases_query_plan_uses_without_rowid_pk_as_covering_scan(
    tmp_path: Path,
) -> None:
    """hub_cases MUST NOT rely on a secondary index.

    viewer_hub_cache is ``WITHOUT ROWID`` with ``case_key`` as PK — the
    b-tree IS the storage. Every column is reachable from the PK's leaves
    without a separate index seek; SQLite plans a plain ``SCAN
    viewer_hub_cache`` and picks columns off the b-tree row directly.

    L3 docstring-drift lens: viewer/schema.py's DDL says WITHOUT ROWID
    precisely because the PK covers every read. If someone drops the
    WITHOUT ROWID clause (making the PK a separate index) or adds a
    secondary index without justifying the write-path cost, the plan
    changes and this test fires — the design intent surfaces.

    Assertion pair:
      - plan mentions ``viewer_hub_cache`` (the query targets that table)
      - plan does NOT reference ``USING INDEX`` (no secondary path)
      - shipped schema still declares ``WITHOUT ROWID`` (design intact)
    """
    db = tmp_path / "viewer.db"
    build_viewer_db(db)
    seed_hub_cache(
        db,
        [
            ("hkcfa/2020/1", 100, "2026-07-07T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        sql = _capture_last_sql(conn, lambda: hub_cases(conn))
        plan = _explain(conn, sql)
        assert "viewer_hub_cache" in plan, plan
        assert "USING INDEX" not in plan, plan
        # Belt-and-suspenders: pin the WITHOUT ROWID design in the
        # shipped schema DDL — if it silently flips to WITH ROWID, the
        # 'no USING INDEX' assertion above still passes (a scan of the
        # implicit rowid table looks the same to the plan reader), so
        # this second assertion catches that specific drift.
        row = conn.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='viewer_hub_cache'"
        ).fetchone()
        assert row is not None, "viewer_hub_cache table missing from schema"
        assert "WITHOUT ROWID" in row[0], row[0]
    finally:
        conn.close()
