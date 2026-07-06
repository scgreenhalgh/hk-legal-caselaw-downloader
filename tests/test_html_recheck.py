"""Tests for HtmlRecheckRunner and `hklii recheck-html` subcommand.

Motivation: HKLII displays "Only the Word format is available at the
moment" for recent-2026 judgments. The scraper stamps
html_pending_at_hklii on doc-fallback capture. This runner walks those
flagged rows and re-fetches getjudgment; if HTML is now available it
saves and clears the flag, otherwise it bumps the timestamp.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB


SAMPLE_RESP = {
    "cases": [{"title": "T v T", "act": "HCCC1/2023"}],
    "db": "hkcfi",
    "date": "2023-06-15",
    "neutral": "[2023] HKCFI 1",
    "parallel_citation": [],
    "content": "",
    "doc": "https://legalref.judiciary.hk/doc/word.doc",
    "has_translation": False,
}


def _make_db_with_pending_row() -> CheckpointDB:
    db = CheckpointDB(":memory:")
    db.upsert_case("hkcfi", 2026, 3816, "[2026] HKCFI 3816", "T v T", "2026-07-01")
    db.mark_downloaded("hkcfi", 2026, 3816, ["doc"], html_pending_ts=1751600000)
    return db


class TestCheckpointHelpers:
    def test_bump_html_pending_ts_updates_column(self):
        db = _make_db_with_pending_row()
        db.bump_html_pending_ts("hkcfi", 2026, 3816, 1999999999)
        row = db._conn.execute(
            "SELECT html_pending_at_hklii FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] == 1999999999

    def test_bump_html_pending_ts_does_not_touch_status_or_formats(self):
        db = _make_db_with_pending_row()
        db.bump_html_pending_ts("hkcfi", 2026, 3816, 1999999999)
        row = db._conn.execute(
            "SELECT status, formats FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] == "downloaded"
        assert json.loads(row[1]) == ["doc"]

    def test_get_formats_returns_current_formats(self):
        db = _make_db_with_pending_row()
        assert db.get_formats("hkcfi", 2026, 3816) == ["doc"]

    def test_get_formats_returns_none_for_missing_row(self):
        db = _make_db_with_pending_row()
        assert db.get_formats("hkcfi", 9999, 1) is None


class TestHtmlRecheckRunner:
    async def test_html_available_now_saves_and_clears_flag(self, tmp_path):
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()

        async def mock_get(url, **kw):
            resp = {**SAMPLE_RESP, "content": "<html><body>Full text now.</body></html>"}
            return httpx.Response(200, json=resp,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "txt", "json"},
        )
        result = await runner.recheck_all()
        assert result["newly_captured"] == 1
        assert result["still_pending"] == 0

        row = db._conn.execute(
            "SELECT html_pending_at_hklii, formats FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] is None, "flag should be cleared once HTML is captured"
        formats = json.loads(row[1])
        assert "doc" in formats, "doc format from prior capture should be preserved"
        assert "html" in formats, "newly captured html should be added"

    async def test_html_available_now_writes_files_to_disk(self, tmp_path):
        """Whole-codebase review (L4): the sibling test above asserts DB
        state (formats union, flag cleared) but never checks that the
        {stem}.html / {stem}.txt / {stem}.json files hit disk. A future
        regression that removes save_judgment_local from _recheck_one
        would leave the DB claiming the row is captured while nothing
        exists on disk — silent corpus damage. Pin the observable
        side-effect."""
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()

        async def mock_get(url, **kw):
            resp = {**SAMPLE_RESP, "content": "<html><body>Full text now.</body></html>"}
            return httpx.Response(200, json=resp,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "txt", "json"},
        )
        await runner.recheck_all()

        stem = "hkcfi_2026_3816"
        d = tmp_path / "hkcfi" / "2026"
        for ext in ("html", "txt", "json"):
            path = d / f"{stem}.{ext}"
            assert path.exists(), f"expected {path} on disk after recheck"
            assert path.stat().st_size > 0, f"{path} is 0-byte"

    async def test_still_empty_bumps_timestamp(self, tmp_path):
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()
        original_ts = 1751600000

        async def mock_get(url, **kw):
            # Still empty at HKLII.
            return httpx.Response(200, json=SAMPLE_RESP,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "txt", "json"},
        )
        result = await runner.recheck_all()
        assert result["newly_captured"] == 0
        assert result["still_pending"] == 1

        row = db._conn.execute(
            "SELECT html_pending_at_hklii, formats FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] is not None
        assert row[0] > original_ts, (
            f"timestamp should be bumped forward from {original_ts}, got {row[0]}"
        )
        assert json.loads(row[1]) == ["doc"], "formats should stay unchanged"

    async def test_challenge_page_marks_failed(self, tmp_path):
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()

        async def mock_get(url, **kw):
            resp = {**SAMPLE_RESP, "content": "<html>Just a moment... cloudflare</html>"}
            return httpx.Response(200, json=resp,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"},
        )
        result = await runner.recheck_all()
        assert result["failed"] == 1

    async def test_no_pending_rows_returns_zero_counts(self, tmp_path):
        from hklii_downloader.html_recheck import HtmlRecheckRunner
        db = CheckpointDB(":memory:")
        # No rows with html_pending_at_hklii set.

        async def mock_get(url, **kw):
            raise AssertionError("should not be called when no pending rows")

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"},
        )
        result = await runner.recheck_all()
        assert result == {"newly_captured": 0, "still_pending": 0, "failed": 0}


class _CapturingEvents:
    """Minimal StructuredEventLogger stand-in for tests. Records calls
    to emit() and sample_failure() so we can assert on them without
    hitting the real event-file/dumper machinery."""

    def __init__(self):
        self.events: list[dict] = []
        self.samples: list[dict] = []

    def emit(self, kind: str, **fields) -> None:
        self.events.append({"kind": kind, **fields})

    def sample_failure(
        self, name: str, body: str, headers, is_challenge: bool = False,
    ) -> None:
        self.samples.append({
            "name": name,
            "body_preview": body[:80] if isinstance(body, str) else body,
            "is_challenge": is_challenge,
        })


class TestHtmlRecheckRunnerEvents:
    """Task #38 — HtmlRecheckRunner missed challenge_detected and
    case_failed emissions. Post-fix, the recheck path emits the same
    shape scraper.py already does so post-run analytics can count
    challenges hit during the recheck sweep."""

    async def test_challenge_page_emits_challenge_detected(self, tmp_path):
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()
        events = _CapturingEvents()

        async def mock_get(url, **kw):
            resp = {**SAMPLE_RESP,
                    "content": "<html>Just a moment... cloudflare</html>"}
            return httpx.Response(200, json=resp,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"}, events=events,
        )
        result = await runner.recheck_all()

        assert result["failed"] == 1
        kinds = [e["kind"] for e in events.events]
        assert "challenge_detected" in kinds, (
            f"expected challenge_detected emission, got {kinds}"
        )
        # Body sample should have been dumped for post-run WAF forensics
        assert any(s["is_challenge"] for s in events.samples), (
            "expected sample_failure(is_challenge=True), got "
            f"{events.samples}"
        )

    async def test_http_failure_emits_case_failed(self, tmp_path):
        """Non-200 upstream (or json-parse failure) counts as failed and
        must land in the events stream so operators can tell "recheck
        pass had upstream failures" from "no rows were pending"."""
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()
        events = _CapturingEvents()

        async def mock_get(url, **kw):
            return httpx.Response(503, text="upstream busy",
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"}, events=events,
        )
        result = await runner.recheck_all()

        assert result["failed"] == 1
        kinds = [e["kind"] for e in events.events]
        assert "case_failed" in kinds, (
            f"expected case_failed emission, got {kinds}"
        )

    async def test_request_error_emits_case_failed(self, tmp_path):
        """Whole-codebase review (L4): the httpx.RequestError branch
        (html_recheck.py:111-117) was untested — test_http_failure_
        emits_case_failed covers only the 503 non-200 branch. A
        regression that removed the case_failed emit from the
        RequestError arm would go undetected."""
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()
        events = _CapturingEvents()

        async def mock_get(url, **kw):
            raise httpx.ConnectError("network partition")

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"}, events=events,
        )
        result = await runner.recheck_all()

        assert result["failed"] == 1
        kinds = [e["kind"] for e in events.events]
        assert "case_failed" in kinds, (
            f"RequestError branch didn't emit case_failed: {kinds}"
        )
        # Error class must distinguish from HTTP-status failure so
        # operators can separately count transport vs upstream errors.
        request_error_events = [
            e for e in events.events
            if e["kind"] == "case_failed"
            and e.get("error_class") == "request_error"
        ]
        assert request_error_events, (
            "expected case_failed with error_class='request_error', "
            f"got {events.events}"
        )

    async def test_events_none_stays_backwards_compatible(self, tmp_path):
        """Passing events=None (or omitting it entirely) must not crash —
        the CLI --no-events path still needs to work."""
        from hklii_downloader.html_recheck import HtmlRecheckRunner

        db = _make_db_with_pending_row()

        async def mock_get(url, **kw):
            resp = {**SAMPLE_RESP,
                    "content": "<html>Just a moment... cloudflare</html>"}
            return httpx.Response(200, json=resp,
                                  request=httpx.Request("GET", url))

        runner = HtmlRecheckRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html"},
        )
        result = await runner.recheck_all()
        assert result["failed"] == 1


class TestRecheckHtmlSubcommand:
    def test_subcommand_registered(self):
        from click.testing import CliRunner
        from hklii_downloader.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["recheck-html", "--help"])
        assert result.exit_code == 0
        assert "recheck-html" in result.output.lower() or "pending" in result.output.lower()

    def test_subcommand_requires_proxy_or_direct(self):
        from click.testing import CliRunner
        from hklii_downloader.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["recheck-html"])
        assert result.exit_code != 0
        assert "proxy" in result.output.lower() or "--direct" in result.output.lower()


class TestRecheckHtmlMaxAgeDaysFlag:
    """`--max-age-days N` bounds the queue by case date so `hklii update`
    can call recheck-html without wasting API calls on ancient rows.
    Threading: cli → _run_recheck_html → HtmlRecheckRunner → pending_html_recheck.
    """

    def test_flag_appears_in_help(self):
        from click.testing import CliRunner
        from hklii_downloader.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["recheck-html", "--help"])
        assert result.exit_code == 0
        assert "--max-age-days" in result.output, result.output

    def test_max_age_days_flag_reaches_pending_html_recheck(
        self, tmp_path, monkeypatch,
    ):
        """The flag value must arrive at CheckpointDB.pending_html_recheck."""
        from click.testing import CliRunner
        from hklii_downloader.cli import main
        from hklii_downloader.checkpoint import CheckpointDB
        # Prime a checkpoint DB so cli doesn't UsageError-out
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2026, 1, "[2026] HKCFI 1", "T", "2026-07-01")
        db.mark_downloaded("hkcfi", 2026, 1, ["doc"], html_pending_ts=1)
        db.close()

        calls = []
        orig = CheckpointDB.pending_html_recheck

        def spy(self, limit=None, max_age_days=None, _today_iso=None):
            calls.append({
                "limit": limit,
                "max_age_days": max_age_days,
                "_today_iso": _today_iso,
            })
            return orig(
                self, limit=limit, max_age_days=max_age_days,
                _today_iso=_today_iso,
            )

        monkeypatch.setattr(
            "hklii_downloader.checkpoint.CheckpointDB.pending_html_recheck",
            spy,
        )
        # Stub out ProxyPool.preflight + recheck_all so we don't need a real pool
        async def fake_preflight(self):
            from hklii_downloader.proxy_pool import PreflightResult
            return PreflightResult(home_ip="1.2.3.4", healthy_proxies=[], leaked=[])
        monkeypatch.setattr(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", fake_preflight,
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "recheck-html", "-o", str(tmp_path),
            "--direct", "--yes",
            "--max-age-days", "30",
            "--limit", "5",
        ])
        # Filter to calls that came through the CLI path — HtmlRecheckRunner
        # calls pending_html_recheck internally too.
        relevant = [c for c in calls if c["max_age_days"] == 30]
        assert relevant, (
            f"expected pending_html_recheck called with max_age_days=30; "
            f"got calls={calls}, output={result.output}"
        )

    def test_absent_flag_passes_none(self, tmp_path, monkeypatch):
        """Back-compat: absent flag → max_age_days=None (unlimited)."""
        from click.testing import CliRunner
        from hklii_downloader.cli import main
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2026, 1, "[2026] HKCFI 1", "T", "2026-07-01")
        db.mark_downloaded("hkcfi", 2026, 1, ["doc"], html_pending_ts=1)
        db.close()

        calls = []
        orig = CheckpointDB.pending_html_recheck

        def spy(self, limit=None, max_age_days=None, _today_iso=None):
            calls.append(max_age_days)
            return orig(
                self, limit=limit, max_age_days=max_age_days,
                _today_iso=_today_iso,
            )

        monkeypatch.setattr(
            "hklii_downloader.checkpoint.CheckpointDB.pending_html_recheck",
            spy,
        )
        async def fake_preflight(self):
            from hklii_downloader.proxy_pool import PreflightResult
            return PreflightResult(home_ip="1.2.3.4", healthy_proxies=[], leaked=[])
        monkeypatch.setattr(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", fake_preflight,
        )

        runner = CliRunner()
        runner.invoke(main, [
            "recheck-html", "-o", str(tmp_path), "--direct", "--yes",
        ])
        # None means "unbounded" — that must be one of the observed calls
        assert None in calls, f"expected None among calls; got {calls}"
