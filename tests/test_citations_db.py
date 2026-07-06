"""Tests for the citations graph checkpoint tables.

Three new tables per docs/citation-graph-design.md §2.1:
  * citations           — the edges themselves (from_key → to_key)
  * noteup_fetches      — per-source-case tracker (idempotent resume)
  * case_parallel_cites — law-report parallel citations captured from
                          getcasenoteup responses (used to de-dup at
                          RAG query time).
"""
from __future__ import annotations

import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestSchema:
    def test_tables_present(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            tables = {
                r[0] for r in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for name in ("citations", "noteup_fetches", "case_parallel_cites"):
                assert name in tables, f"missing {name}"
        finally:
            db.close()

    def test_citations_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {r[1] for r in db._conn.execute(
                "PRAGMA table_info(citations)"
            ).fetchall()}
            for name in (
                "from_key", "to_key", "citer_lang",
                "citer_freq", "position", "first_seen",
            ):
                assert name in cols, f"missing {name}"
        finally:
            db.close()

    def test_noteup_fetches_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {r[1] for r in db._conn.execute(
                "PRAGMA table_info(noteup_fetches)"
            ).fetchall()}
            for name in (
                "court", "year", "number",
                "status", "fetched_at", "edge_count", "error",
            ):
                assert name in cols, f"missing {name}"
        finally:
            db.close()


class TestNoteupAccessors:
    def test_upsert_noteup_fetch_defaults_pending(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_noteup_fetch("hkcfa", 2023, 32)
            row = db._conn.execute(
                "SELECT court, year, number, status, edge_count "
                "FROM noteup_fetches"
            ).fetchone()
            assert row == ("hkcfa", 2023, 32, "pending", None)
        finally:
            db.close()

    def test_mark_noteup_ok_records_edge_count_and_time(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_noteup_fetch("hkcfa", 2023, 32)
            db.mark_noteup_ok("hkcfa", 2023, 32, edge_count=28,
                              fetched_at="2026-07-06T05:00:00Z")
            row = db._conn.execute(
                "SELECT status, edge_count, fetched_at, error "
                "FROM noteup_fetches"
            ).fetchone()
            assert row == ("ok", 28, "2026-07-06T05:00:00Z", None)
        finally:
            db.close()

    def test_mark_noteup_failed_records_error(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_noteup_fetch("hkcfa", 2023, 32)
            db.mark_noteup_failed("hkcfa", 2023, 32,
                                   error="HTTP 500 after retries")
            row = db._conn.execute(
                "SELECT status, error FROM noteup_fetches"
            ).fetchone()
            assert row == ("error", "HTTP 500 after retries")
        finally:
            db.close()

    def test_claim_pending_noteup_flips_to_in_progress(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_noteup_fetch("hkcfa", 2023, 32)
            db.upsert_noteup_fetch("hkcfi", 2023, 100)
            rec = db.claim_pending_noteup()
            assert rec is not None
            row = db._conn.execute(
                "SELECT status FROM noteup_fetches "
                "WHERE court=? AND year=? AND number=?",
                (rec.court, rec.year, rec.number),
            ).fetchone()
            assert row[0] == "in_progress"
        finally:
            db.close()

    def test_claim_pending_returns_none_when_empty(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            assert db.claim_pending_noteup() is None
        finally:
            db.close()

    def test_noteup_stats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for i in range(3):
                db.upsert_noteup_fetch("hkcfa", 2023, i + 1)
            db.mark_noteup_ok("hkcfa", 2023, 1, edge_count=5,
                              fetched_at="2026-07-06T05:00:00Z")
            db.mark_noteup_failed("hkcfa", 2023, 2, error="e")
            stats = db.noteup_stats()
            assert stats["pending"] == 1
            assert stats["ok"] == 1
            assert stats["error"] == 1
            assert stats["total"] == 3
        finally:
            db.close()


class TestEdgeInsert:
    def test_insert_citation_edges(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            edges = [
                ("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0),
                ("hkcfi/2022/200", "hkcfa/2020/32", "en", 1, 1),
            ]
            db.insert_citation_edges(edges, first_seen="2026-07-06T05:00:00Z")
            rows = db._conn.execute(
                "SELECT from_key, to_key, citer_lang, citer_freq, position "
                "FROM citations ORDER BY from_key"
            ).fetchall()
            assert rows == [
                ("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0),
                ("hkcfi/2022/200", "hkcfa/2020/32", "en", 1, 1),
            ]
        finally:
            db.close()

    def test_insert_citation_edges_is_idempotent(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            edges = [("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0)]
            db.insert_citation_edges(edges, first_seen="ts1")
            db.insert_citation_edges(edges, first_seen="ts2")
            n = db._conn.execute(
                "SELECT COUNT(*) FROM citations"
            ).fetchone()[0]
            assert n == 1
        finally:
            db.close()


class TestParallelCites:
    def test_insert_parallel_cites(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.insert_parallel_cites("hkcfa/2020/32", ["[2020] 6 HKC 46", "(2020) 23 HKCFAR 199"])
            rows = db._conn.execute(
                "SELECT case_key, parallel_cite FROM case_parallel_cites "
                "ORDER BY parallel_cite"
            ).fetchall()
            assert rows == [
                ("hkcfa/2020/32", "(2020) 23 HKCFAR 199"),
                ("hkcfa/2020/32", "[2020] 6 HKC 46"),
            ]
        finally:
            db.close()

    def test_insert_parallel_cites_is_idempotent(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.insert_parallel_cites("hkcfa/2020/32", ["[2020] 6 HKC 46"])
            db.insert_parallel_cites("hkcfa/2020/32", ["[2020] 6 HKC 46"])
            n = db._conn.execute(
                "SELECT COUNT(*) FROM case_parallel_cites"
            ).fetchone()[0]
            assert n == 1
        finally:
            db.close()
