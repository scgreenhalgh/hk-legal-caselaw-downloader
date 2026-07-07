"""Contract test for open_readonly under WAL: reader coexists with writer.

Design §4 line 50 (docs/viewer-design.md) claims:

    "WAL mode already set by the downloader (checkpoint.py:220) enables
     concurrent reader + writer."

test_viewer_db.py already pins the read-only + writes-raise contract of
:func:`open_readonly`, but never exercises the *concurrent-writer*
scenario the design promises. This file is that Tier-4 wrong-side test.

Scenario:

1. Bootstrap a WAL-mode viewer.db with one committed row.
2. Open a writer connection and hold an uncommitted transaction with an
   INSERT into ``viewer_hub_cache``.
3. While the writer's txn is still open, call ``open_readonly`` and
   SELECT — must succeed, must not raise ``SQLITE_BUSY``, must return
   the pre-write snapshot (WAL isolation).
4. After the writer commits, a fresh ``open_readonly`` must see the new
   row.

5-lens pins:
- L1 (silent skip): the uncommitted writer's row is filtered by
  snapshot isolation, NOT silently included in the reader's view.
- L2 (semantic drift): binds "concurrent reader + writer" to a real
  observable — snapshot before commit, new value after.
- L4 (wrong-side test): the existing ``test_viewer_db`` file pinned the
  read-only side (writes raise); this file pins the concurrency side
  the design promises.
- L5 (ambiguous state): three assertion points pin (a) no-BUSY, (b)
  pre-commit snapshot, (c) post-commit view — no ambiguity.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.schema import VIEWER_HUB_CACHE_DDL


def _bootstrap_wal_db_with_one_row(db_path: Path) -> None:
    """Create ``db_path`` in WAL journal_mode with one committed row.

    Sanity-asserts that ``PRAGMA journal_mode=WAL`` returned ``wal`` —
    on some filesystems (network mounts, exotic tmpfs) SQLite silently
    falls back to another mode, which would make this whole test a
    no-op.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode == ("wal",), f"failed to enable WAL, got: {mode!r}"
        conn.executescript(VIEWER_HUB_CACHE_DDL)
        conn.execute(
            "INSERT INTO viewer_hub_cache(case_key, inbound_count, computed_at) "
            "VALUES (?, ?, ?)",
            ("hkcfa/2020/1", 5, "2026-07-07T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_open_readonly_coexists_with_uncommitted_writer(tmp_path: Path) -> None:
    """WAL guarantee: ``open_readonly`` never blocks or errors on a
    concurrent uncommitted writer, and it sees the pre-write snapshot.

    Uses two connection objects in the same thread — the writer opens
    with ``isolation_level=None`` and explicit ``BEGIN IMMEDIATE`` so
    the transaction hold is unambiguous. The reader is a fresh
    ``open_readonly`` call while the writer's txn is still open.

    Threading is not required: SQLite's WAL isolation is per-connection,
    not per-thread. Same-thread two-connection is deterministic and
    avoids race-condition flakes.
    """
    db = tmp_path / "viewer.db"
    _bootstrap_wal_db_with_one_row(db)

    # Writer: autocommit-off via isolation_level=None + explicit
    # BEGIN IMMEDIATE. INSERT stays uncommitted until we say COMMIT.
    writer = sqlite3.connect(str(db), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO viewer_hub_cache(case_key, inbound_count, computed_at) "
            "VALUES (?, ?, ?)",
            ("hkcfa/2020/2", 10, "2026-07-07T00:00:00"),
        )
        # Do NOT commit yet — writer's txn is held open across the
        # reader block below.

        # -- Assertion (a): reader does NOT raise SQLITE_BUSY. --
        # Wrap in try/except that fails explicitly on OperationalError,
        # so a regression here reports "WAL contract broken" instead of
        # a bare stack trace two levels down.
        reader = open_readonly(db)
        try:
            try:
                rows_during_write = reader.execute(
                    "SELECT case_key, inbound_count FROM viewer_hub_cache "
                    "ORDER BY case_key"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                pytest.fail(
                    "WAL concurrency contract broken: open_readonly SELECT "
                    f"raised OperationalError while writer held txn: {exc}"
                )

            # -- Assertion (b): snapshot isolation — writer's uncommitted
            # ('hkcfa/2020/2', 10) is INVISIBLE, only the committed
            # ('hkcfa/2020/1', 5) is returned. --
            assert rows_during_write == [("hkcfa/2020/1", 5)]
        finally:
            reader.close()

        writer.execute("COMMIT")
    finally:
        writer.close()

    # -- Assertion (c): a fresh open_readonly after commit sees the new
    # row. This proves the reader isn't stuck on a stale snapshot cached
    # somewhere in the open_readonly helper. --
    reader_post_commit = open_readonly(db)
    try:
        rows_post_commit = reader_post_commit.execute(
            "SELECT case_key, inbound_count FROM viewer_hub_cache "
            "ORDER BY case_key"
        ).fetchall()
        assert rows_post_commit == [
            ("hkcfa/2020/1", 5),
            ("hkcfa/2020/2", 10),
        ]
    finally:
        reader_post_commit.close()
