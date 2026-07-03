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
            return httpx.Response(200, text="<html>Press Summary Body</html>")

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
            return httpx.Response(200, text="<html>ok</html>")

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
            ])

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
            return httpx.Response(200, json=[])

        await fetch_appeal_history("HCA2268/2025", mock_get)
        # slash must be percent-encoded so it's a caseno value, not a path segment
        assert "HCA2268%2F2025" in captured["url"]

    async def test_returns_empty_list_when_no_history(self):
        from hklii_downloader.enrichment import fetch_appeal_history

        async def mock_get(url, **kw):
            return httpx.Response(200, json=[])

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
