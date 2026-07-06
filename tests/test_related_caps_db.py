"""Tests for the ord_reg_edges + relatedcap_fetches checkpoint tables."""
from __future__ import annotations

import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestSchema:
    def test_tables_present(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            tables = {r[0] for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "ord_reg_edges" in tables
            assert "relatedcap_fetches" in tables
        finally:
            db.close()

    def test_ord_reg_edges_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {r[1] for r in db._conn.execute(
                "PRAGMA table_info(ord_reg_edges)"
            ).fetchall()}
            for name in ("parent_cap", "child_cap", "lang", "title", "first_seen"):
                assert name in cols
        finally:
            db.close()

    def test_relatedcap_fetches_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {r[1] for r in db._conn.execute(
                "PRAGMA table_info(relatedcap_fetches)"
            ).fetchall()}
            for name in ("cap_number", "abbr", "lang", "status",
                         "fetched_at", "edge_count", "error"):
                assert name in cols
        finally:
            db.close()


class TestFetchTracker:
    def test_upsert_relatedcap_fetch_defaults_pending(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32", "reg", "en")
            row = db._conn.execute(
                "SELECT cap_number, abbr, lang, status FROM relatedcap_fetches"
            ).fetchone()
            assert row == ("32", "reg", "en", "pending")
        finally:
            db.close()

    def test_mark_relatedcap_ok(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32", "reg", "en")
            db.mark_relatedcap_ok("32", "reg", "en", edge_count=14,
                                    fetched_at="2026-07-06T05:00:00Z")
            row = db._conn.execute(
                "SELECT status, edge_count, fetched_at FROM relatedcap_fetches"
            ).fetchone()
            assert row == ("ok", 14, "2026-07-06T05:00:00Z")
        finally:
            db.close()

    def test_mark_relatedcap_failed(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32A", "reg", "en")
            db.mark_relatedcap_failed(
                "32A", "reg", "en",
                error="HTTP 500 (alpha-suffix caps not accepted)",
            )
            row = db._conn.execute(
                "SELECT status, error FROM relatedcap_fetches"
            ).fetchone()
            assert row[0] == "error"
            assert "500" in row[1]
        finally:
            db.close()

    def test_claim_pending_relatedcap(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32", "reg", "en")
            rec = db.claim_pending_relatedcap()
            assert rec is not None
            row = db._conn.execute(
                "SELECT status FROM relatedcap_fetches"
            ).fetchone()
            assert row[0] == "in_progress"
        finally:
            db.close()

    def test_relatedcap_stats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for cap in ("1", "2", "3"):
                db.upsert_relatedcap_fetch(cap, "reg", "en")
            db.mark_relatedcap_ok("1", "reg", "en", edge_count=4,
                                   fetched_at="ts")
            stats = db.relatedcap_stats()
            assert stats["total"] == 3
            assert stats["ok"] == 1
            assert stats["pending"] == 2
        finally:
            db.close()


class TestEdgeInsert:
    def test_insert_ord_reg_edges(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            edges = [
                ("32", "32A", "en", "Companies (Requirements for Documents) Regulation"),
                ("32", "32B", "en", "Companies (Forms) Regulations"),
            ]
            db.insert_ord_reg_edges(edges, first_seen="2026-07-06T05:00:00Z")
            rows = db._conn.execute(
                "SELECT parent_cap, child_cap, lang, title "
                "FROM ord_reg_edges ORDER BY child_cap"
            ).fetchall()
            assert len(rows) == 2
            assert rows[0] == ("32", "32A", "en",
                                "Companies (Requirements for Documents) Regulation")
        finally:
            db.close()

    def test_insert_is_idempotent(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            edges = [("32", "32A", "en", "T")]
            db.insert_ord_reg_edges(edges, first_seen="ts1")
            db.insert_ord_reg_edges(edges, first_seen="ts2")
            n = db._conn.execute(
                "SELECT COUNT(*) FROM ord_reg_edges"
            ).fetchone()[0]
            assert n == 1
        finally:
            db.close()
