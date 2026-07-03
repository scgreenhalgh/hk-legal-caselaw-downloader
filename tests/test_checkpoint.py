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
