"""Tests for press summary + appeal history fetch/save helpers."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


class TestFetchPressSummary:
    async def test_returns_html_content(self):
        from hklii_downloader.enrichment import fetch_press_summary

        captured = {}

        async def mock_get(url, **kw):
            captured["url"] = url
            return httpx.Response(200, text="<html>Press Summary Body</html>",
                                  request=httpx.Request("GET", url))

        html = await fetch_press_summary(
            "/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm",
            mock_get,
        )
        assert html == "<html>Press Summary Body</html>"
        assert captured["url"].startswith("https://www.hklii.hk/")
        assert captured["url"].endswith("ES.htm")

    async def test_accepts_absolute_url_unchanged(self):
        from hklii_downloader.enrichment import fetch_press_summary
        captured = {}

        async def mock_get(url, **kw):
            captured["url"] = url
            return httpx.Response(200, text="<html>ok</html>",
                                  request=httpx.Request("GET", url))

        await fetch_press_summary("https://www.hklii.hk/xyz.htm", mock_get)
        assert captured["url"] == "https://www.hklii.hk/xyz.htm"

    async def test_raises_on_non_2xx(self):
        from hklii_downloader.enrichment import fetch_press_summary

        async def mock_get(url, **kw):
            return httpx.Response(404, text="", request=httpx.Request("GET", url))

        with pytest.raises(httpx.HTTPStatusError):
            await fetch_press_summary("/x.htm", mock_get)


class TestFetchAppealHistory:
    async def test_calls_getappealhistory_with_caseno(self):
        from hklii_downloader.enrichment import fetch_appeal_history
        captured = {}

        async def mock_get(url, **kw):
            captured["url"] = url
            return httpx.Response(200, json=[
                {"act": "FACC3/2025", "judgments": [
                    {"neutral": "[2026] HKCFA 25", "path": "/en/cases/hkcfa/2026/25",
                     "date": "2026-06-17", "lang": "EN", "remarks": ""}]},
            ], request=httpx.Request("GET", url))

        result = await fetch_appeal_history("FACC3/2025", mock_get)
        assert isinstance(result, list)
        assert result[0]["act"] == "FACC3/2025"
        assert "/api/getappealhistory" in captured["url"]
        assert "FACC3" in captured["url"]

    async def test_url_encodes_slash_in_caseno(self):
        from hklii_downloader.enrichment import fetch_appeal_history
        captured = {}

        async def mock_get(url, **kw):
            captured["url"] = url
            return httpx.Response(200, json=[],
                                  request=httpx.Request("GET", url))

        await fetch_appeal_history("HCA2268/2025", mock_get)
        # slash must be percent-encoded so it's a caseno value, not a path segment
        assert "HCA2268%2F2025" in captured["url"]

    async def test_returns_empty_list_when_no_history(self):
        from hklii_downloader.enrichment import fetch_appeal_history

        async def mock_get(url, **kw):
            return httpx.Response(200, json=[],
                                  request=httpx.Request("GET", url))

        result = await fetch_appeal_history("X/2025", mock_get)
        assert result == []


class TestSavePressSummaryLocal:
    def test_saves_english(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local
        path = save_press_summary_local(
            "<html>en body</html>", tmp_path, "hkcfa_2026_25", "en",
        )
        assert path.name == "hkcfa_2026_25.summary_en.html"
        assert path.read_text() == "<html>en body</html>"

    def test_saves_chinese(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local
        path = save_press_summary_local(
            "<html>中文</html>", tmp_path, "hkcfa_2026_25", "zh",
        )
        assert path.name == "hkcfa_2026_25.summary_zh.html"
        assert "中文" in path.read_text()

    def test_creates_output_dir(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local
        deep = tmp_path / "does" / "not" / "exist"
        path = save_press_summary_local(
            "<p>x</p>", deep, "hkcfa_2026_25", "en",
        )
        assert path.exists()

    def test_rejects_invalid_lang(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local
        with pytest.raises(ValueError, match="lang"):
            save_press_summary_local("<p>x</p>", tmp_path, "stem", "fr")


class TestEnrichSummariesForCase:
    async def test_oserror_during_save_marks_failed(self, tmp_path):
        """Disk-full (or EACCES/EROFS) during save must mark_enrichment
        failed with a descriptive error, not silently leave 'pending'."""
        from unittest.mock import patch
        from hklii_downloader.enrichment import enrich_summaries_for_case
        from hklii_downloader.checkpoint import CheckpointDB

        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-01-01")
        html = ('<a href="/doc/foo/es.htm">Press Summary (English)</a>')

        async def mock_get(url, **kw):
            return httpx.Response(200, text="<html>en body</html>",
                                  request=httpx.Request("GET", url))

        with patch("pathlib.Path.write_text",
                   side_effect=OSError("[Errno 28] No space left on device")):
            await enrich_summaries_for_case(
                mock_get, db, "hkcfa", 2026, 25,
                "hkcfa_2026_25", tmp_path, html,
            )

        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "failed", (
            f"OSError during save should mark failed, got {row['summary_en']}"
        )
        errs = db.get_enrichment_errors("hkcfa", 2026, 25)
        assert "summary_en" in errs
        assert "No space" in errs["summary_en"] or "Errno 28" in errs["summary_en"]

    async def test_fetch_press_summary_rejects_challenge_page(self, tmp_path):
        """B5: WAF interstitials returned with HTTP 200 must not land on disk
        as press summaries. If the response body looks like a challenge page
        (Cloudflare 'Just a moment...' etc.), mark the enrichment as failed
        with a descriptive error and do NOT stamp 'downloaded' nor write the
        file.

        Without this guard, `hklii enrich` never revisits the case (it filters
        on status='pending') and the corpus silently accumulates WAF HTML
        mislabelled as English press summaries.
        """
        from hklii_downloader.enrichment import enrich_summaries_for_case
        from hklii_downloader.checkpoint import CheckpointDB

        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-01-01")
        html = '<a href="/doc/foo/es.htm">Press Summary (English)</a>'

        challenge_body = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>Checking your browser before accessing... "
            "Please enable JavaScript. cloudflare</body></html>"
        )

        async def mock_get(url, **kw):
            return httpx.Response(200, text=challenge_body,
                                  request=httpx.Request("GET", url))

        await enrich_summaries_for_case(
            mock_get, db, "hkcfa", 2026, 25,
            "hkcfa_2026_25", tmp_path, html,
        )

        # No file on disk: the WAF interstitial must not be persisted.
        summary_path = tmp_path / "hkcfa_2026_25.summary_en.html"
        assert not summary_path.exists(), (
            f"challenge-page HTML must not be saved, but found: {summary_path}"
        )

        # DB row marked failed, not stamped 'downloaded'.
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "failed", (
            f"challenge page must mark failed, got {row['summary_en']}"
        )

        # Error message must name the cause so operators can diagnose.
        errs = db.get_enrichment_errors("hkcfa", 2026, 25)
        assert "summary_en" in errs
        assert "challenge-page" in errs["summary_en"], (
            f"error should name 'challenge-page', got: {errs['summary_en']!r}"
        )


class TestEnrichAppealHistoryForCase:
    async def test_oserror_during_save_marks_failed(self, tmp_path):
        from unittest.mock import patch
        from hklii_downloader.enrichment import enrich_appeal_history_for_case
        from hklii_downloader.checkpoint import CheckpointDB

        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-01-01")

        async def mock_get(url, **kw):
            return httpx.Response(200, json=[{"act": "FACC3/2025", "judgments": []}],
                                  request=httpx.Request("GET", url))

        with patch("pathlib.Path.write_text",
                   side_effect=OSError("[Errno 30] Read-only file system")):
            await enrich_appeal_history_for_case(
                mock_get, db, "hkcfa", 2026, 25,
                "hkcfa_2026_25", tmp_path, "FACC3/2025",
            )

        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["appeal_history"] == "failed"


class TestSaveAppealHistoryLocal:
    def test_saves_as_json(self, tmp_path):
        from hklii_downloader.enrichment import save_appeal_history_local
        data = [{"act": "FACC3/2025", "judgments": [
            {"neutral": "[2026] HKCFA 25", "path": "/en/cases/hkcfa/2026/25"}]}]
        path = save_appeal_history_local(data, tmp_path, "hkcfa_2026_25")
        assert path.name == "hkcfa_2026_25.appeal_history.json"
        parsed = json.loads(path.read_text())
        assert parsed == data

    def test_preserves_non_ascii(self, tmp_path):
        from hklii_downloader.enrichment import save_appeal_history_local
        data = [{"act": "X/2025", "judgments": [
            {"neutral": "[2026] HKCFA 27", "title": "青山道"}]}]
        path = save_appeal_history_local(data, tmp_path, "hkcfa_2026_27")
        assert "青山道" in path.read_text()
