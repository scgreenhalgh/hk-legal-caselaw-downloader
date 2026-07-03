"""Tests for CheckpointDB — SQLite checkpoint with WAL mode."""
from __future__ import annotations

import pytest

from hklii_downloader.checkpoint import CheckpointDB, CaseRecord


class TestCheckpointDB:
    def test_upsert_and_stats(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1234, "[2023] HKCFI 1234", "Test v Test", "2023-06-15")
        stats = db.stats()
        assert stats["total"] == 1
        assert stats["pending"] == 1

    def test_upsert_is_idempotent(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        assert db.stats()["total"] == 1

    def test_claim_pending_returns_case(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        record = db.claim_pending()
        assert record is not None
        assert record.court == "hkcfi"
        assert record.year == 2023
        assert record.number == 1
        assert record.status == "in_progress"

    def test_claim_pending_atomic(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        first = db.claim_pending()
        second = db.claim_pending()
        assert first is not None
        assert second is None

    def test_claim_pending_filters_by_court(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A", "2023-01-01")
        db.upsert_case("hkca", 2023, 1, "[2023] HKCA 1", "B", "2023-01-01")
        record = db.claim_pending(court="hkca")
        assert record is not None
        assert record.court == "hkca"

    def test_mark_downloaded(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html", "txt"])
        stats = db.stats()
        assert stats["downloaded"] == 1
        assert stats["pending"] == 0

    def test_mark_failed(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 1, "404 Not Found")
        stats = db.stats()
        assert stats["failed"] == 1

    def test_release_in_progress(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        assert db.stats()["in_progress"] == 1
        db.release_in_progress()
        assert db.stats()["in_progress"] == 0
        assert db.stats()["pending"] == 1

    def test_pending_cases(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 2, "[2023] HKCFI 2", "B", "2023-01-02")
        db.upsert_case("hkca", 2023, 1, "[2023] HKCA 1", "C", "2023-01-03")
        cases = db.pending_cases()
        assert len(cases) == 3
        hkcfi_cases = db.pending_cases(courts=["hkcfi"])
        assert len(hkcfi_cases) == 2

    def test_wal_mode_enabled(self):
        db = CheckpointDB(":memory:")
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal" or mode == "memory"

    def test_case_record_fields(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 42, "[2023] HKCFI 42", "Test Case", "2023-06-15")
        record = db.claim_pending()
        assert record.neutral == "[2023] HKCFI 42"
        assert record.title == "Test Case"
        assert record.date == "2023-06-15"

    def test_stats_all_statuses(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "N1", "T1", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 2, "N2", "T2", "2023-01-02")
        db.upsert_case("hkcfi", 2023, 3, "N3", "T3", "2023-01-03")
        db.upsert_case("hkcfi", 2023, 4, "N4", "T4", "2023-01-04")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html"])
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 2, "error")
        db.claim_pending()
        stats = db.stats()
        assert stats == {"total": 4, "pending": 1, "in_progress": 1, "downloaded": 1, "failed": 1}

    def test_close(self):
        db = CheckpointDB(":memory:")
        db.close()


class TestEnrichmentStatus:
    """summary_en, summary_zh, appeal_history tracked independently."""

    def _seed(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 25, "[2026] HKCFA 25", "HKSAR v X", "2026-06-17")
        return db

    def test_new_case_has_pending_enrichment(self):
        db = self._seed()
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row == {
            "summary_en": "pending",
            "summary_zh": "pending",
            "appeal_history": "pending",
        }

    def test_mark_enrichment_downloaded(self):
        db = self._seed()
        db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "downloaded")
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "downloaded"
        assert row["summary_zh"] == "pending"
        assert row["appeal_history"] == "pending"

    def test_mark_enrichment_na(self):
        db = self._seed()
        db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "na")
        db.mark_enrichment("hkcfa", 2026, 25, "summary_zh", "na")
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "na"
        assert row["summary_zh"] == "na"

    def test_mark_enrichment_failed_with_error(self):
        db = self._seed()
        db.mark_enrichment(
            "hkcfa", 2026, 25, "appeal_history", "failed",
            error="ConnectTimeout after 3 retries",
        )
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["appeal_history"] == "failed"
        errs = db.get_enrichment_errors("hkcfa", 2026, 25)
        assert "appeal_history" in errs
        assert "ConnectTimeout" in errs["appeal_history"]

    def test_pending_enrichment_iterates_only_pending(self):
        """Enrichment only applies to cases whose judgment is already
        downloaded — you can't extract summary URLs from a file you don't
        have. So pending_enrichment filters by both."""
        db = CheckpointDB(":memory:")
        for i in range(3):
            db.upsert_case("hkcfa", 2026, i+1, f"N{i+1}", f"T{i+1}", "2026-01-01")
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, i+1, ["html"])
        db.mark_enrichment("hkcfa", 2026, 1, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 2, "summary_en", "na")
        pending = db.pending_enrichment("summary_en")
        nums = sorted(r.number for r in pending)
        assert nums == [3]

    def test_pending_enrichment_excludes_undownloaded_cases(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 1, "N1", "T1", "2026-01-01")
        db.upsert_case("hkcfa", 2026, 2, "N2", "T2", "2026-01-02")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        pending = db.pending_enrichment("summary_en")
        assert [r.number for r in pending] == [1]

    def test_enrichment_stats_reports_counts(self):
        db = CheckpointDB(":memory:")
        for i in range(4):
            db.upsert_case("hkcfa", 2026, i+1, f"N{i+1}", f"T{i+1}", "2026-01-01")
        db.mark_enrichment("hkcfa", 2026, 1, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 2, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 3, "summary_en", "na")
        db.mark_enrichment("hkcfa", 2026, 4, "summary_en", "failed")
        stats = db.enrichment_stats()
        assert stats["summary_en"] == {
            "pending": 0, "downloaded": 2, "na": 1, "failed": 1,
        }

    def test_invalid_enrichment_kind_raises(self):
        db = self._seed()
        with pytest.raises(ValueError, match="kind"):
            db.mark_enrichment("hkcfa", 2026, 25, "not_a_kind", "downloaded")

    def test_invalid_enrichment_status_raises(self):
        db = self._seed()
        with pytest.raises(ValueError, match="status"):
            db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "weird")

    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE cases (
            court TEXT NOT NULL, year INTEGER NOT NULL, number INTEGER NOT NULL,
            neutral TEXT NOT NULL, title TEXT NOT NULL, date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            formats TEXT, error TEXT,
            PRIMARY KEY (court, year, number))""")
        conn.execute("INSERT INTO cases VALUES ('hkcfa', 2026, 1, 'N1', 'T1', '2026-01-01', 'downloaded', NULL, NULL)")
        conn.commit()
        conn.close()

        db = CheckpointDB(str(db_path))
        row = db.get_enrichment("hkcfa", 2026, 1)
        assert row == {
            "summary_en": "pending",
            "summary_zh": "pending",
            "appeal_history": "pending",
        }
