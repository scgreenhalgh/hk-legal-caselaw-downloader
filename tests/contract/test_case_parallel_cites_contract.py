"""Contract test for the shared ``case_parallel_cites`` table.

Design §10 "Contract tests per shared table": the writer
(hklii_downloader.checkpoint) and the reader (viewer.graph) live in
the same package but on different sides of a schema seam. If either
side unilaterally renames a column, changes a type, drops the PK, or
alters ordering semantics, this test alarms.

Table shape (checkpoint.py._SCHEMA):

    CREATE TABLE case_parallel_cites (
        case_key      TEXT NOT NULL,       -- "hkcfa/2020/32"
        parallel_cite TEXT NOT NULL,       -- "[2020] 6 HKC 46"
        PRIMARY KEY (case_key, parallel_cite)
    ) WITHOUT ROWID;

Reader: :func:`hklii_downloader.viewer.graph.parallel_cites` —
``SELECT parallel_cite ... WHERE case_key=? ORDER BY parallel_cite ASC``.

Assertions:
  1. Round-trip — write via ``CheckpointDB.insert_parallel_cites`` →
     read via ``viewer.graph.parallel_cites`` → rows come back in ASC
     order (semantic equivalence of what writer put in vs what reader
     sees).
  2. PK rejection — duplicate ``(case_key, parallel_cite)`` inserted
     via raw SQL (bypassing the public API's ``INSERT OR IGNORE``)
     raises ``sqlite3.IntegrityError``. Locks the PK constraint against
     a future migration that widened or dropped it.
  3. Case-key isolation — ``parallel_cites('a')`` returns only rows for
     ``a``; rows for a second case_key ``b`` are not visible.

Note: this is a regression test on already-shipped schema+reader; it
passes at green immediately and only fires red on future drift. No
"failing output" phase because there is no implementation gap — the
seam is being locked, not built.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import parallel_cites


def test_round_trip_write_via_checkpoint_reads_back_via_viewer(
    tmp_path: Path,
) -> None:
    """Writer-side insert appears through the viewer-side reader in ASC order.

    The reader's ORDER BY is BINARY-collation ASCII sort:
      - '(' (40) < '[' (91) → "(2020) 23 HKCFAR 199" first
      - Within '[' prefix, position 7 tiebreaks: '6' (54) < 'H' (72)
    """
    db_path = tmp_path / "checkpoint.db"
    db = CheckpointDB(str(db_path))
    try:
        db.insert_parallel_cites(
            "hkcfa/2020/32",
            [
                "[2020] 6 HKC 46",
                "(2020) 23 HKCFAR 199",
                "[2020] HKCFA 32",
            ],
        )
    finally:
        db.close()

    conn = open_readonly(db_path)
    try:
        assert parallel_cites(conn, "hkcfa/2020/32") == [
            "(2020) 23 HKCFAR 199",
            "[2020] 6 HKC 46",
            "[2020] HKCFA 32",
        ]
    finally:
        conn.close()


def test_duplicate_case_key_parallel_cite_rejected_by_primary_key(
    tmp_path: Path,
) -> None:
    """PK ``(case_key, parallel_cite)`` blocks a raw duplicate INSERT.

    The public API (:meth:`CheckpointDB.insert_parallel_cites`) uses
    ``INSERT OR IGNORE`` — its idempotency test lives in
    tests/test_citations_db.py. This contract instead exercises the
    underlying PK: a raw ``INSERT INTO`` (no OR-IGNORE) duplicating the
    key must raise :class:`sqlite3.IntegrityError`. If a future
    migration widened the PK or dropped ``WITHOUT ROWID``, the raw
    insert would silently succeed and this test would flip red.
    """
    db_path = tmp_path / "checkpoint.db"
    db = CheckpointDB(str(db_path))
    try:
        db.insert_parallel_cites("hkcfa/2020/32", ["[2020] 6 HKC 46"])
        # Bypass INSERT OR IGNORE to hit the constraint directly.
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO case_parallel_cites "
                "(case_key, parallel_cite) VALUES (?, ?)",
                ("hkcfa/2020/32", "[2020] 6 HKC 46"),
            )
    finally:
        db.close()


def test_parallel_cites_isolated_across_case_keys(tmp_path: Path) -> None:
    """Rows for one case_key never leak into another's reader result.

    Guards the reader's ``WHERE case_key = ?`` predicate against a
    schema migration that (say) folded case_key into a JSON column or
    accidentally broadened the query.
    """
    db_path = tmp_path / "checkpoint.db"
    db = CheckpointDB(str(db_path))
    try:
        db.insert_parallel_cites(
            "hkcfa/2020/1",
            ["[2020] HKCFA 1", "[2020] 6 HKC 10"],
        )
        db.insert_parallel_cites(
            "hkcfa/2020/2",
            ["[2020] HKCFA 2", "[2020] 6 HKC 20"],
        )
    finally:
        db.close()

    conn = open_readonly(db_path)
    try:
        assert parallel_cites(conn, "hkcfa/2020/1") == [
            "[2020] 6 HKC 10",
            "[2020] HKCFA 1",
        ]
        assert parallel_cites(conn, "hkcfa/2020/2") == [
            "[2020] 6 HKC 20",
            "[2020] HKCFA 2",
        ]
        # Unknown key returns [] — the reader does not fall back to
        # 'match anything' if the WHERE clause fails to bind.
        assert parallel_cites(conn, "hkcfa/2020/999") == []
    finally:
        conn.close()
