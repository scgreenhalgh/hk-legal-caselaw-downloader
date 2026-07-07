"""Contract test for the shared checkpoint.db table ``ord_reg_edges``.

Per docs/viewer-design.md section 10: writes a canonical row via the
``checkpoint.py`` public API, reads through ``viewer/graph.py``, asserts
semantic equivalence — what the writer put in equals what the reader
sees back. Guards against schema drift between the downloader-side
writer and the viewer-side reader.

Landing under ``tests/contract/`` in the viewer worktree (amended from
the design's original "downloader package" directive — contract tests
now live where the viewer code lives).

Columns exercised: parent_cap, child_cap, lang, title, first_seen.
"""

from __future__ import annotations

import sqlite3

import pytest

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.viewer import graph


class TestSchemaDrift:
    """Schema-shape guards. Fire loudly if a future migration weakens
    the writer's DDL without a matching reader update."""

    def test_pk_parent_child_lang_enforced_at_schema_level(self, tmp_path):
        """A raw INSERT of a duplicate ``(parent_cap, child_cap, lang)``
        tuple raises ``sqlite3.IntegrityError``.

        ``insert_ord_reg_edges`` uses ``INSERT OR IGNORE`` which silently
        swallows duplicates — that would pass whether or not the schema
        PK exists. This test bypasses the public API and hits raw INSERT
        so a future migration that drops the PK fails here first, not
        after production data corruption.
        """
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.insert_ord_reg_edges(
                [("32", "32A", "en", "T1")],
                first_seen="2026-07-06T05:00:00Z",
            )
            with pytest.raises(sqlite3.IntegrityError):
                db._conn.execute(
                    "INSERT INTO ord_reg_edges "
                    "(parent_cap, child_cap, lang, title, first_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("32", "32A", "en", "T2", "2026-07-06T05:00:01Z"),
                )
        finally:
            db.close()

    def test_idx_ore_child_index_targets_child_cap(self, tmp_path):
        """``idx_ore_child`` exists AND indexes the ``child_cap`` column.

        The child_cap lookup path (given "32A", find its parent "32") is
        the reverse-lookup viewer route. Dropping this index would
        silently degrade viewer perf; renaming it to a different column
        would break the reverse-lookup query plan. sqlite_master.sql is
        inspected so either failure mode fires the alarm at CI time.
        """
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            row = db._conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_ore_child'"
            ).fetchone()
            assert row is not None, (
                "idx_ore_child missing on ord_reg_edges"
            )
            assert row[0] is not None, (
                "idx_ore_child has no CREATE INDEX SQL (auto-created?)"
            )
            assert "child_cap" in row[0], (
                f"idx_ore_child not on child_cap column: {row[0]!r}"
            )
        finally:
            db.close()


class TestRoundTrip:
    """Writer -> reader semantic equivalence. What
    ``insert_ord_reg_edges`` puts in, ``graph.ord_reg_children`` pulls
    back — same columns, same values, same lang segregation."""

    def test_all_columns_roundtrip_through_reader(self, tmp_path):
        """Insert two edges via ``checkpoint.insert_ord_reg_edges``,
        read via ``graph.ord_reg_children``, assert all five columns
        (``parent_cap``, ``child_cap``, ``lang``, ``title``,
        ``first_seen``) come back with the values that went in.
        """
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            first_seen = "2026-07-06T05:00:00Z"
            db.insert_ord_reg_edges(
                [
                    ("32", "32A", "en",
                     "Companies (Requirements for Documents) Regulation"),
                    ("32", "32B", "en",
                     "Companies (Forms) Regulations"),
                ],
                first_seen=first_seen,
            )

            assert hasattr(graph, "ord_reg_children"), (
                "viewer.graph.ord_reg_children reader is missing — "
                "add it so the writer/reader contract holds"
            )

            rows = graph.ord_reg_children(db._conn, "32", "en")
            assert isinstance(rows, list)
            assert len(rows) == 2

            # Each row exposes all five DB columns as dict keys — a
            # reader that drops a column silently trips this.
            for r in rows:
                assert set(r.keys()) >= {
                    "parent_cap", "child_cap", "lang",
                    "title", "first_seen",
                }, r

            by_child = {r["child_cap"]: r for r in rows}

            assert by_child["32A"]["parent_cap"] == "32"
            assert by_child["32A"]["lang"] == "en"
            assert by_child["32A"]["title"] == (
                "Companies (Requirements for Documents) Regulation"
            )
            assert by_child["32A"]["first_seen"] == first_seen

            assert by_child["32B"]["parent_cap"] == "32"
            assert by_child["32B"]["title"] == "Companies (Forms) Regulations"
            assert by_child["32B"]["first_seen"] == first_seen
        finally:
            db.close()

    def test_reader_segregates_by_lang(self, tmp_path):
        """Bilingual edges (same parent + child, different lang) survive
        as two distinct rows and the reader filters by ``lang``.

        L2 lens: semantic drift risk if the reader drops the ``lang``
        WHERE clause — the viewer would silently show cross-lang mixed
        titles under a single-language route.
        """
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.insert_ord_reg_edges(
                [
                    ("32", "32A", "en", "English title"),
                    ("32", "32A", "tc", "Traditional Chinese title"),
                ],
                first_seen="ts",
            )

            assert hasattr(graph, "ord_reg_children"), (
                "viewer.graph.ord_reg_children reader is missing"
            )

            en_rows = graph.ord_reg_children(db._conn, "32", "en")
            tc_rows = graph.ord_reg_children(db._conn, "32", "tc")

            assert len(en_rows) == 1
            assert en_rows[0]["lang"] == "en"
            assert en_rows[0]["title"] == "English title"

            assert len(tc_rows) == 1
            assert tc_rows[0]["lang"] == "tc"
            assert tc_rows[0]["title"] == "Traditional Chinese title"
        finally:
            db.close()
