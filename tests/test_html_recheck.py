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
