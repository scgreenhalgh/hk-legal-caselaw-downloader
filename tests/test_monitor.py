"""Tests for `hklii monitor` — read-only health snapshot of a scrape."""
from __future__ import annotations

from datetime import datetime, timezone

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.monitor import MonitorRunner

EMPTY_ERR = "empty-content, doc-fetch-failed on getjudgment"
HTTP503_ERR = "http-503 after 1 retries"


def _build_checkpoint(tmp_path, *, last_seen_at=1_700_000_000):
    """15 cases: 4 downloaded, 2 in_progress, 3 failed, 6 pending."""
    out = tmp_path / "out"
    out.mkdir()
    db = CheckpointDB(str(out / ".checkpoint.db"))
    for i in range(1, 16):
        db.upsert_case(
            "hkcfi", 2024, i, f"[2024] HKCFI {i}", f"Case {i}",
            "2024-01-01", last_seen_at=last_seen_at,
        )
    # 4 downloaded
    for i in range(1, 5):
        db.mark_downloaded("hkcfi", 2024, i, ["html", "txt", "json"])
    # 3 failed: two share an error prefix, one distinct
    db.mark_failed("hkcfi", 2024, 5, EMPTY_ERR)
    db.mark_failed("hkcfi", 2024, 6, EMPTY_ERR)
    db.mark_failed("hkcfi", 2024, 7, HTTP503_ERR)
    # 2 in_progress (claim arbitrary pending rows)
    db.claim_pending()
    db.claim_pending()
    db.close()
    return out


class TestCheckpointReader:
    def test_status_counts(self, tmp_path):
        out = _build_checkpoint(tmp_path)
        summary = MonitorRunner(out).run()
        cp = summary["checkpoint"]
        assert cp["downloaded"] == 4
        assert cp["in_progress"] == 2
        assert cp["failed"] == 3
        assert cp["pending"] == 6
        assert cp["total"] == 15

    def test_top_error_prefixes_sorted_desc(self, tmp_path):
        out = _build_checkpoint(tmp_path)
        summary = MonitorRunner(out).run()
        prefixes = summary["checkpoint"]["top_error_prefixes"]
        # Two distinct 40-char prefixes, most frequent first.
        assert prefixes[0] == {"prefix": EMPTY_ERR[:40], "count": 2}
        assert {"prefix": HTTP503_ERR[:40], "count": 1} in prefixes
        assert len(prefixes) == 2
