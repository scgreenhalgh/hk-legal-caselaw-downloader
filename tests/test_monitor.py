"""Tests for `hklii monitor` — read-only health snapshot of a scrape."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.monitor import MonitorRunner

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _write_events(out, rows) -> None:
    lines = [json.dumps(r) for r in rows]
    (out / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

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


class TestEventsReader:
    def test_counts_by_kind_within_window_only(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        # events.jsonl is append-only, so rows land in chronological order
        # (oldest first, newest at EOF) — the fixture models that.
        _write_events(out, [
            {"ts": _iso(90), "kind": "request_failed"},    # out (window=30)
            {"ts": _iso(60), "kind": "request_success"},   # out
            {"ts": _iso(45), "kind": "request_success"},   # in
            {"ts": _iso(20), "kind": "request_success"},   # in
            {"ts": _iso(10), "kind": "warmup"},            # in
            {"ts": _iso(5), "kind": "request_failed"},     # in
        ])
        summary = MonitorRunner(out, window_min=30, now=NOW).run()
        ev = summary["events"]
        assert ev["window_min"] == 30
        counts = ev["counts_by_kind"]
        assert counts.get("request_success") == 2
        assert counts.get("request_failed") == 1
        assert counts.get("warmup") == 1
        # Tracked-but-absent kinds are 0-filled for a stable render
        # (.get returns None for an absent key, so == 0 still asserts presence).
        assert counts.get("challenge_detected") == 0
        assert counts.get("pool_exhausted") == 0
        assert counts.get("degraded") == 0

    def test_missing_events_file_yields_none(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        # No events.jsonl written (a --no-events run).
        summary = MonitorRunner(out, now=NOW).run()
        assert summary["events"] is None

    def test_large_file_reads_only_window_fast(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        # 120k out-of-window rows, then 3 in-window rows at EOF. A naive
        # whole-file scan would parse 120k JSON objects; the backward tail
        # should stop at the first out-of-window ts.
        old = _iso(600)  # 10h ago, far outside a 30-min window
        with (out / "events.jsonl").open("w", encoding="utf-8") as fh:
            line = json.dumps({"ts": old, "kind": "request_success"}) + "\n"
            fh.write(line * 120_000)
            for _ in range(3):
                fh.write(json.dumps({"ts": _iso(2), "kind": "request_failed"}) + "\n")
        t0 = time.monotonic()
        summary = MonitorRunner(out, window_min=30, now=NOW).run()
        elapsed = time.monotonic() - t0
        counts = summary["events"]["counts_by_kind"]
        assert counts.get("request_failed") == 3
        assert counts.get("request_success") == 0
        assert elapsed < 2.0, f"events read took {elapsed:.2f}s (>2s budget)"
