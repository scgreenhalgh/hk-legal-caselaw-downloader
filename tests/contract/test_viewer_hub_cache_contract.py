"""Contract test for the shared ``viewer_hub_cache`` table.

Per design doc §10 "Contract tests per shared table" (as amended:
lives in the viewer worktree at ``tests/contract/`` rather than the
downloader package).

Why this test exists (schema-drift alarm):

    The hub cache is written by one code path (``hklii viewer index``
    populates it via cross-DB read of ``checkpoint.db.citations``,
    Phase 5+) and read by another (``viewer.graph.hub_cases``, shipped
    Phase 1). If either side silently changes column names, primary
    key shape, ordering, or the "table missing vs empty" distinction,
    a route can start returning wrong data with no test failure
    anywhere else in the suite.

    This contract test locks the current writer/reader agreement:

      * Schema shape — ``WITHOUT ROWID`` on the ``case_key`` PK is a
        performance contract, not incidental. Losing it would silently
        double the row footprint and the reader would still work.
      * Idempotent UPSERT — the "index" step re-runs and must update
        rows in place rather than duplicate them.
      * Reader ordering — ``inbound_count DESC, case_key ASC`` is a UX
        contract with the /court/{slug} template's "top hubs" list.
        L3 pin against docstring drift: the docstring says one thing,
        the SQL another, and only this test can catch it.
      * Empty vs missing — the reader raises
        :class:`ViewerCacheMissing` when the table is absent (banner
        state) and returns ``[]`` when it exists but has zero rows
        (legitimate no-hubs answer). L5 ambiguous-state contract.

    Columns exercised: ``case_key``, ``inbound_count``, ``computed_at``
    (every non-derived column on the table).

The writer side uses raw SQL rather than a public helper because no
downloader / viewer helper currently populates ``viewer_hub_cache`` —
the cache builder lands in a later phase. When it does, this test's
UPSERT statement is the reference shape it must emit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import ViewerCacheMissing, hub_cases
from hklii_downloader.viewer.schema import create_schema


# The canonical UPSERT the future writer must emit. Kept as a module
# constant so the drift alarm points at ONE place — if a future writer
# helper lands, its SQL should match this string modulo whitespace.
HUB_CACHE_UPSERT_SQL = (
    "INSERT INTO viewer_hub_cache (case_key, inbound_count, computed_at) "
    "VALUES (?, ?, ?) "
    "ON CONFLICT (case_key) DO UPDATE SET "
    "inbound_count = excluded.inbound_count, "
    "computed_at   = excluded.computed_at"
)


def _fresh_viewer_db(tmp_path: Path) -> Path:
    """Build a fresh viewer.db with the shipped DDL and return its path."""
    db = tmp_path / "viewer.db"
    conn = sqlite3.connect(str(db))
    try:
        create_schema(conn)
    finally:
        conn.close()
    return db


def _upsert(db: Path, case_key: str, inbound_count: int, computed_at: str) -> None:
    """Writer-side helper — raw UPSERT of one row, one commit.

    Uses :data:`HUB_CACHE_UPSERT_SQL` so a change to the reference
    UPSERT shape lands in one place.
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            HUB_CACHE_UPSERT_SQL, (case_key, inbound_count, computed_at)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assertion 1 — schema shape: WITHOUT ROWID
# ---------------------------------------------------------------------------


def test_schema_creates_viewer_hub_cache_without_rowid(tmp_path: Path) -> None:
    """``create_schema`` builds the table with the ``WITHOUT ROWID`` flag.

    A regular rowid table would still work at runtime but the PK
    ``case_key`` is a TEXT slug (~14 chars) — with WITHOUT ROWID SQLite
    clusters the row directly on the PK and skips the hidden 8-byte
    rowid column plus its separate index. Losing this flag doubles the
    on-disk footprint of a 100k-row cache without any behavioural
    warning, so it's a contract worth pinning.

    The check reads the shipped DDL out of ``sqlite_schema`` — that's
    the same DDL SQLite would replay on any other reader connection,
    including a future :class:`viewer.graph` opener. The assertion is
    case-insensitive on the flag itself (SQLite normalises but doesn't
    upper-case) and substring-based so a whitespace tweak in
    ``VIEWER_HUB_CACHE_DDL`` doesn't spuriously fail.
    """
    db = _fresh_viewer_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='viewer_hub_cache'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "viewer_hub_cache table not created by create_schema"
    assert "WITHOUT ROWID" in row[0].upper(), (
        "viewer_hub_cache lost its WITHOUT ROWID flag — the case_key "
        "TEXT PK relies on the clustered-on-PK layout for compactness. "
        f"DDL was: {row[0]!r}"
    )


# ---------------------------------------------------------------------------
# Assertion 2 — UPSERT idempotency on case_key PK
# ---------------------------------------------------------------------------


def test_upsert_on_case_key_is_idempotent(tmp_path: Path) -> None:
    """``INSERT ... ON CONFLICT (case_key) DO UPDATE`` updates in place.

    The future writer will re-run the index step on every
    ``hklii viewer index --incremental`` invocation, hitting the same
    case_keys as their inbound_count evolves. A non-idempotent INSERT
    would either raise IntegrityError (blocking the re-run) or double
    the row up (blowing up the reader's ordering + count reporting).

    We verify:

      * A second UPSERT for the same case_key updates the existing row
        (row count stays at 1, inbound_count reflects the new value).
      * A different case_key inserts a second row (row count becomes 2).
    """
    db = _fresh_viewer_db(tmp_path)
    key = "hkcfa/2020/32"

    # First upsert — insert.
    _upsert(db, key, inbound_count=5, computed_at="2026-07-07T00:00:00")
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT case_key, inbound_count, computed_at "
            "FROM viewer_hub_cache"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(key, 5, "2026-07-07T00:00:00")]

    # Second upsert with the SAME case_key and different columns — must update
    # in place, not duplicate.
    _upsert(db, key, inbound_count=17, computed_at="2026-07-08T00:00:00")
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT case_key, inbound_count, computed_at "
            "FROM viewer_hub_cache"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(key, 17, "2026-07-08T00:00:00")], (
        "second UPSERT for the same case_key did not update in place — "
        "either the ON CONFLICT clause dropped or the PK changed shape"
    )

    # Third upsert with a DIFFERENT case_key — inserts a second row.
    _upsert(
        db, "hkca/2018/524",
        inbound_count=42, computed_at="2026-07-08T00:00:00",
    )
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM viewer_hub_cache"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2


# ---------------------------------------------------------------------------
# Assertion 3 — hub_cases reader ordering (L3 docstring drift)
# ---------------------------------------------------------------------------


def test_hub_cases_orders_by_inbound_count_desc_then_case_key_asc(
    tmp_path: Path,
) -> None:
    """The reader returns rows ordered by ``inbound_count DESC``, then
    ``case_key ASC`` as the stable tiebreak.

    Both halves of the order matter and must be pinned:

      * ``inbound_count DESC`` — the docstring on :func:`hub_cases`
        says "top-ranked hub cases", which only makes sense with DESC.
        A silent flip to ASC would push the *quietest* cases to the
        top and no other test in the suite would notice (the browse
        route's snapshot templates test presence, not order-by-count).

      * ``case_key ASC`` — the tiebreak. Without a deterministic
        secondary sort, the /court/{slug} page's top-hubs list would
        reshuffle on every hklii viewer index run (SQLite is free to
        return equally-ranked rows in any order absent an ORDER BY
        clause on the tie column).

    L3 pin against docstring drift: this is the concrete evidence that
    the SQL matches the promise.
    """
    db = _fresh_viewer_db(tmp_path)
    # Two ranks + a tie inside the top rank.
    _upsert(db, "hkca/2018/524",  inbound_count=100, computed_at="t")
    _upsert(db, "hkcfa/1999/17",  inbound_count=50,  computed_at="t")
    _upsert(db, "hkcfa/1999/72",  inbound_count=50,  computed_at="t")  # tie w/ /17
    _upsert(db, "hkcfa/2020/32",  inbound_count=10,  computed_at="t")

    conn = open_readonly(db)
    try:
        rows = hub_cases(conn, min_inbound=5, limit=100)
    finally:
        conn.close()

    keys_and_counts = [(r["case_key"], r["inbound_count"]) for r in rows]
    assert keys_and_counts == [
        ("hkca/2018/524",  100),
        ("hkcfa/1999/17",   50),   # tie broken by case_key ASC → /17 before /72
        ("hkcfa/1999/72",   50),
        ("hkcfa/2020/32",   10),
    ], (
        "hub_cases ordering drifted — expected inbound_count DESC then "
        "case_key ASC (stable tiebreak). Got: " + repr(keys_and_counts)
    )


# ---------------------------------------------------------------------------
# Assertion 4 — empty (schema present, 0 rows) vs missing (table absent)
# ---------------------------------------------------------------------------


def test_empty_table_returns_list_missing_table_raises(tmp_path: Path) -> None:
    """L5 ambiguous-state contract: two distinct "no rows" answers.

      * Table exists with zero rows → ``hub_cases`` returns ``[]``.
        This is the legitimate "nothing cached yet, but setup ran" state
        — the /court/{slug} template renders an empty top-hubs section
        (or a "no hubs" hint) rather than the setup-error banner.

      * Table missing → ``hub_cases`` raises :class:`ViewerCacheMissing`.
        This is the "setup was skipped" state — the route catches the
        exception and renders the "run `hklii viewer index`" banner.

    Collapsing these two states into one would either hide the "run
    the indexer" prompt (bad UX) or spam it for a corpus with no cases
    over the min_inbound threshold (worse UX). Both directions must
    stay distinguishable.
    """
    # Case A: schema present, zero rows → [].
    db = _fresh_viewer_db(tmp_path)
    conn = open_readonly(db)
    try:
        assert hub_cases(conn) == []
    finally:
        conn.close()

    # Case B: DB file exists but table absent → ViewerCacheMissing.
    other = tmp_path / "no_table.db"
    sqlite3.connect(str(other)).close()  # empty DB, no schema
    conn = open_readonly(other)
    try:
        with pytest.raises(ViewerCacheMissing):
            hub_cases(conn)
    finally:
        conn.close()
