"""Tests for `hklii monitor` — read-only health snapshot of a scrape."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

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
            {"ts": _iso(90), "kind": "request_failed"},    # out (>30m ago)
            {"ts": _iso(60), "kind": "request_success"},   # out
            {"ts": _iso(25), "kind": "request_success"},   # in
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


class TestProxyHotspots:
    def test_flags_single_outlier_proxy(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        rows = []
        # Production topology: 19 proxies with 1 failure each, one proxy with
        # 100 — a clear >3σ outlier (mean ~5.95, pstdev ~21.6, thr ~70.5).
        for i in range(19):
            rows.append({"ts": _iso(15), "kind": "request_failed",
                         "proxy_url": f"http://p{i}:{i}"})
        for _ in range(100):
            rows.append({"ts": _iso(10), "kind": "request_failed",
                         "proxy_url": "http://p19:19"})
        _write_events(out, rows)
        hotspots = MonitorRunner(out, window_min=30, now=NOW).run()["events"]["proxy_hotspots"]
        assert len(hotspots) == 1
        assert hotspots[0]["proxy_url"] == "http://p19:19"
        assert hotspots[0]["failed"] == 100

    def test_no_hotspot_when_evenly_distributed(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        rows = []
        for i in range(20):
            for _ in range(5):  # identical failure counts → zero variance
                rows.append({"ts": _iso(12), "kind": "request_failed",
                             "proxy_url": f"http://p{i}:{i}"})
        _write_events(out, rows)
        hotspots = MonitorRunner(out, window_min=30, now=NOW).run()["events"]["proxy_hotspots"]
        assert hotspots == []

    def test_out_of_window_failures_excluded_from_hotspots(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        rows = [{"ts": _iso(120), "kind": "request_failed",
                 "proxy_url": "http://p0:0"} for _ in range(500)]
        # 500 old failures on p0, but all outside the window → not a hotspot.
        rows += [{"ts": _iso(5), "kind": "request_failed",
                  "proxy_url": f"http://p{i}:{i}"} for i in range(1, 4)]
        _write_events(out, rows)
        hotspots = MonitorRunner(out, window_min=30, now=NOW).run()["events"]["proxy_hotspots"]
        assert all(h["proxy_url"] != "http://p0:0" for h in hotspots)


class TestRecentChallenges:
    def test_returns_last_five_newest_first(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        rows = [
            {"ts": _iso(30 - i), "kind": "challenge_detected",
             "url": f"https://www.hklii.hk/api/getjudgment?n={i}",
             "proxy_url": f"http://p{i}:{i}"}
            for i in range(7)  # i=0 oldest (_iso(30)) .. i=6 newest (_iso(24))
        ]
        _write_events(out, rows)
        challenges = MonitorRunner(out, window_min=40, now=NOW).run()["events"]["recent_challenges"]
        assert len(challenges) == 5
        # newest first: i=6 then 5,4,3,2
        assert challenges[0]["url"] == "https://www.hklii.hk/api/getjudgment?n=6"
        assert challenges[0]["proxy_url"] == "http://p6:6"
        assert challenges[-1]["url"] == "https://www.hklii.hk/api/getjudgment?n=2"


class TestRateAndEta:
    def test_rate_and_eta_from_last_seen_at(self, tmp_path):
        seen = 1_700_000_000
        out = _build_checkpoint(tmp_path, last_seen_at=seen)
        # 2h after enumeration: 4 downloaded → 2/hr; 8 remaining (6 pending +
        # 2 in_progress) → ETA 4h.
        now = datetime.fromtimestamp(seen, tz=timezone.utc) + timedelta(hours=2)
        cp = MonitorRunner(out, now=now).run()["checkpoint"]
        assert cp.get("run_start_source") == "min_last_seen_at"
        assert cp["runtime_hours"] == pytest.approx(2.0)
        assert cp["downloaded_per_hour"] == pytest.approx(2.0)
        assert cp["eta_hours"] == pytest.approx(4.0)

    def test_runtime_surfaced_at_top_level(self, tmp_path):
        seen = 1_700_000_000
        out = _build_checkpoint(tmp_path, last_seen_at=seen)
        now = datetime.fromtimestamp(seen, tz=timezone.utc) + timedelta(hours=3)
        summary = MonitorRunner(out, now=now).run()
        assert summary["runtime_hours"] == pytest.approx(3.0)

    def test_falls_back_to_mtime_when_no_last_seen_at(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        for i in range(1, 5):  # no last_seen_at → NULL
            db.upsert_case("hkcfi", 2024, i, f"N{i}", f"T{i}", "2024-01-01")
        db.mark_downloaded("hkcfi", 2024, 1, ["html"])
        db.close()

        now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        mtime = (now - timedelta(hours=5)).timestamp()
        os.utime(out / ".checkpoint.db", (mtime, mtime))

        cp = MonitorRunner(out, now=now).run()["checkpoint"]
        assert cp.get("run_start_source") == "checkpoint_mtime"
        assert cp["runtime_hours"] == pytest.approx(5.0, abs=0.05)
        assert cp.get("warning"), "mtime fallback should record a warning string"

    def test_rate_none_when_no_runtime(self, tmp_path):
        seen = 1_700_000_000
        out = _build_checkpoint(tmp_path, last_seen_at=seen)
        now = datetime.fromtimestamp(seen, tz=timezone.utc)  # zero elapsed
        cp = MonitorRunner(out, now=now).run()["checkpoint"]
        assert cp["downloaded_per_hour"] is None
        assert cp["eta_hours"] is None


def _cp(**over):
    base = {
        "downloaded": 1000, "in_progress": 5, "failed": 10, "pending": 500,
        "total": 1515, "downloaded_per_hour": 7000.0, "eta_hours": 0.07,
        "runtime_hours": 4.0, "top_error_prefixes": [],
    }
    base.update(over)
    return base


def _ev(**over):
    base = {"window_min": 30, "counts_by_kind": {},
            "proxy_hotspots": [], "recent_challenges": []}
    base.update(over)
    return base


def _levels(alerts):
    return [a["level"] for a in alerts]


class TestEvaluateAlerts:
    def _runner(self, tmp_path, workers=20):
        return MonitorRunner(tmp_path, workers=workers)

    def test_healthy_within_tolerance(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(top_error_prefixes=[{"prefix": "http-503", "count": 3}]),
            _ev(), 4.0,
        )
        assert alerts == []

    def test_critical_error_prefix_over_100(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(top_error_prefixes=[
                {"prefix": "empty-content, doc-fetch-failed", "count": 187}]),
            _ev(), 4.0,
        )
        assert "CRITICAL" in _levels(alerts)
        assert any("187" in a["reason"] for a in alerts)

    def test_critical_in_progress_over_4x_workers(self, tmp_path):
        r = self._runner(tmp_path, workers=20)
        alerts = r.evaluate_alerts(_cp(in_progress=100), _ev(), 4.0)
        crit = [a for a in alerts if a["level"] == "CRITICAL"]
        assert any("in_progress" in a["reason"] for a in crit)

    def test_in_progress_at_4x_not_critical(self, tmp_path):
        r = self._runner(tmp_path, workers=20)
        alerts = r.evaluate_alerts(_cp(in_progress=80), _ev(), 4.0)
        assert not any("in_progress" in a["reason"] for a in alerts)

    def test_critical_low_rate_after_one_hour(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(downloaded_per_hour=3000.0), _ev(), 4.0)
        assert "CRITICAL" in _levels(alerts)
        assert any("rate" in a["reason"] for a in alerts)

    def test_low_rate_ignored_under_one_hour(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(downloaded_per_hour=100.0), _ev(), 0.5)
        assert not any("rate" in a["reason"] for a in alerts)

    def test_warn_error_prefix_between_20_and_100(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(top_error_prefixes=[{"prefix": "http-403", "count": 50}]),
            _ev(), 4.0,
        )
        assert _levels(alerts) == ["WARN"]

    def test_warn_degraded_event(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(), _ev(counts_by_kind={"degraded": 2}), 4.0)
        assert any("degraded" in a["reason"] for a in alerts)
        assert "CRITICAL" not in _levels(alerts)

    def test_warn_pool_exhausted_event(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(), _ev(counts_by_kind={"pool_exhausted": 1}), 4.0)
        assert any("pool_exhausted" in a["reason"] for a in alerts)

    def test_warn_proxy_hotspot(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(),
            _ev(proxy_hotspots=[{"proxy_url": "http://p3:3", "failed": 90}]),
            4.0,
        )
        assert any("p3" in a["reason"] for a in alerts)

    def test_warn_rate_between_4000_and_6000(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(downloaded_per_hour=5000.0), _ev(), 4.0)
        assert _levels(alerts) == ["WARN"]

    def test_critical_and_warn_coexist(self, tmp_path):
        r = self._runner(tmp_path)
        alerts = r.evaluate_alerts(
            _cp(in_progress=100,
                top_error_prefixes=[{"prefix": "http-403", "count": 50}]),
            _ev(counts_by_kind={"degraded": 1}), 4.0,
        )
        # A critical (in_progress) and warns (error-prefix, degraded) all fire;
        # run() collapses these to a single CRITICAL severity (pair 6).
        assert "CRITICAL" in _levels(alerts)
        assert "WARN" in _levels(alerts)

    def test_no_events_suppresses_event_alerts(self, tmp_path):
        r = self._runner(tmp_path)
        # events=None (a --no-events run) → checkpoint alerts still fire,
        # but no degraded/pool_exhausted/hotspot alerts.
        alerts = r.evaluate_alerts(_cp(in_progress=100), None, 4.0)
        assert "CRITICAL" in _levels(alerts)
        assert not any("degraded" in a["reason"] for a in alerts)


def _summary(**over):
    base = {
        "severity": "HEALTHY",
        "banner": "hklii scrape @ ./output — hour 4.2, 47320/114398 (41.4%)",
        "runtime_hours": 4.2,
        "checkpoint": {
            "downloaded": 47320, "in_progress": 19, "failed": 142,
            "pending": 66917, "total": 114398,
            "downloaded_per_hour": 7100.0, "eta_hours": 9.4,
            "runtime_hours": 4.2, "run_start": "2026-07-04T08:00:00+00:00",
            "run_start_source": "min_last_seen_at", "warning": None,
            "top_error_prefixes": [
                {"prefix": "empty-content, doc-fetch-failed", "count": 87},
                {"prefix": "http-503", "count": 33},
            ],
        },
        "events": {
            "window_min": 30,
            "counts_by_kind": {
                "request_success": 4102, "request_failed": 88, "warmup": 0,
                "challenge_detected": 0, "pool_exhausted": 0, "degraded": 0,
            },
            "proxy_hotspots": [],
            "recent_challenges": [],
        },
        "log": {"recent_warnings": [
            "[16:47:12] FAILED hkcfi/2024/1023: empty-content, doc-fetch-failed"]},
        "alerts": [],
    }
    base.update(over)
    return base


class TestRenderJson:
    def test_valid_json_with_documented_shape(self, tmp_path):
        text = MonitorRunner(tmp_path).render_json(_summary())
        obj = json.loads(text)  # must be valid JSON
        assert obj["severity"] == "HEALTHY"
        assert set(obj) >= {
            "severity", "banner", "runtime_hours", "checkpoint", "events",
            "log", "alerts"}
        assert obj["checkpoint"]["downloaded"] == 47320
        assert obj["checkpoint"]["downloaded_per_hour"] == 7100.0
        assert obj["events"]["counts_by_kind"]["request_success"] == 4102


class TestRenderText:
    def test_healthy_headline_has_banner_rate_eta(self, tmp_path):
        text = MonitorRunner(tmp_path).render_text(_summary())
        head = (text.splitlines() or [""])[0]
        assert head.startswith("[HEALTHY] hklii scrape @ ./output")
        assert "47320/114398 (41.4%)" in head
        assert "~7100/hr" in head
        assert "ETA ~9.4h" in head

    def test_critical_headline_leads_with_alert_reason(self, tmp_path):
        summary = _summary(
            severity="CRITICAL",
            alerts=[{"level": "CRITICAL",
                     "reason": "error-prefix 'empty-content, doc-fetch-failed' has 187 hits",
                     "detail": "x"}],
        )
        head = (MonitorRunner(tmp_path).render_text(summary).splitlines() or [""])[0]
        assert head.startswith("[CRITICAL]")
        assert "187 hits" in head

    def test_has_all_sections(self, tmp_path):
        text = MonitorRunner(tmp_path).render_text(_summary())
        assert "downloaded" in text and "47320" in text
        assert "top error prefixes" in text
        assert "empty-content, doc-fetch-failed" in text and "87" in text
        assert "recent events" in text
        assert "request_success" in text and "4102" in text
        assert "hotspots" in text
        assert "log warnings" in text
        assert "FAILED hkcfi/2024/1023" in text

    def test_events_section_na_when_no_events(self, tmp_path):
        text = MonitorRunner(tmp_path).render_text(_summary(events=None))
        assert "N/A" in text


class TestLogReader:
    LOG = (
        "2026-07-04 16:40:00,000 INFO    hklii_downloader.scraper: enumerating hkcfi\n"
        "2026-07-04 16:44:03,111 WARNING hklii_downloader.scraper: FAILED hkcfi/2024/1019: http-503\n"
        "2026-07-04 16:47:12,123 WARNING hklii_downloader.scraper: FAILED hkcfi/2024/1023: empty-content, doc-fetch-failed\n"
    )

    def test_tails_warnings_only(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()
        (out / "scrape.log").write_text(self.LOG, encoding="utf-8")
        warns = MonitorRunner(out).run()["log"]["recent_warnings"]
        assert warns == [
            "[16:44:03] FAILED hkcfi/2024/1019: http-503",
            "[16:47:12] FAILED hkcfi/2024/1023: empty-content, doc-fetch-failed",
        ]

    def test_missing_log_file_yields_none(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()
        log = MonitorRunner(out).run()["log"]
        assert log["recent_warnings"] is None

    def test_run_sets_banner(self, tmp_path):
        seen = 1_700_000_000
        out = _build_checkpoint(tmp_path, last_seen_at=seen)
        now = datetime.fromtimestamp(seen, tz=timezone.utc) + timedelta(hours=2)
        banner = MonitorRunner(out, now=now).run()["banner"]
        assert "hour 2.0" in banner
        assert "4/15 (26.7%)" in banner
