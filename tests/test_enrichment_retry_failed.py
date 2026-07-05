"""Tests for task #30: hklii enrich --retry-failed.

`checkpoint.reset_enrichment_failed_to_pending(kinds)` flips
{kind}_status='failed' back to 'pending' for the listed kinds so the
existing pending_any_enrichment / _enrich_one flow processes them
without any per-row status-fork logic. CLI --retry-failed hook wires
this to the enrich subcommand.

Motivating case: the 81 rows with appeal_history_status='failed' that
accumulated during the last production scrape aren't currently retryable
via `hklii enrich` — pending_any_enrichment filters only
{k}_status='pending'.
"""
from __future__ import annotations

from click.testing import CliRunner

from hklii_downloader.checkpoint import CheckpointDB


def _seed_downloaded(db, court, year, number):
    db.upsert_case(court, year, number, f"[{year}] X {number}",
                   "T", "2023-01-01")
    db.mark_downloaded(court, year, number, ["html", "json", "txt"])


class TestResetEnrichmentFailed:
    def test_reset_single_kind_flips_only_that_kind(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_downloaded(db, "hkcfi", 2023, 1)
            db.mark_enrichment("hkcfi", 2023, 1, "summary_en", "failed",
                               error="challenge-page")
            db.mark_enrichment("hkcfi", 2023, 1, "summary_zh", "failed",
                               error="challenge-page")
            db.mark_enrichment("hkcfi", 2023, 1, "appeal_history", "failed",
                               error="500 upstream")

            n = db.reset_enrichment_failed_to_pending(["appeal_history"])

            assert n == 1
            enrich = db.get_enrichment("hkcfi", 2023, 1)
            assert enrich["appeal_history"] == "pending"
            # untouched
            assert enrich["summary_en"] == "failed"
            assert enrich["summary_zh"] == "failed"
        finally:
            db.close()

    def test_reset_multiple_kinds(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_downloaded(db, "hkcfi", 2023, 1)
            for k in ("summary_en", "summary_zh", "appeal_history"):
                db.mark_enrichment("hkcfi", 2023, 1, k, "failed", error="x")

            n = db.reset_enrichment_failed_to_pending(
                ["summary_en", "summary_zh", "appeal_history"],
            )

            assert n == 3
            enrich = db.get_enrichment("hkcfi", 2023, 1)
            assert all(v == "pending" for v in enrich.values())
        finally:
            db.close()

    def test_reset_clears_error_text(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_downloaded(db, "hkcfi", 2023, 1)
            db.mark_enrichment("hkcfi", 2023, 1, "appeal_history", "failed",
                               error="500 upstream")

            db.reset_enrichment_failed_to_pending(["appeal_history"])

            errs = db.get_enrichment_errors("hkcfi", 2023, 1)
            assert "appeal_history" not in errs
        finally:
            db.close()

    def test_reset_ignores_non_failed_rows(self, tmp_path):
        """Rows in 'na' / 'downloaded' / 'pending' state are left alone —
        we don't reclassify 'na' (source URL didn't exist) as pending."""
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_downloaded(db, "hkcfi", 2023, 1)
            db.mark_enrichment("hkcfi", 2023, 1, "summary_en", "na")
            db.mark_enrichment("hkcfi", 2023, 1, "summary_zh", "downloaded")
            db.mark_enrichment("hkcfi", 2023, 1, "appeal_history", "failed",
                               error="e")

            n = db.reset_enrichment_failed_to_pending(
                ["summary_en", "summary_zh", "appeal_history"],
            )

            assert n == 1
            enrich = db.get_enrichment("hkcfi", 2023, 1)
            assert enrich["summary_en"] == "na"
            assert enrich["summary_zh"] == "downloaded"
            assert enrich["appeal_history"] == "pending"
        finally:
            db.close()

    def test_reset_rejects_unknown_kind(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            import pytest
            with pytest.raises(ValueError):
                db.reset_enrichment_failed_to_pending(["bogus_kind"])
        finally:
            db.close()


class TestEnrichRetryFailedCli:
    def test_enrich_help_lists_retry_failed(self):
        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["enrich", "--help"])
        assert result.exit_code == 0
        assert "--retry-failed" in result.output
