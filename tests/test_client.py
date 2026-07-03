"""Characterization tests for client.py.

Lock existing fetch_judgment, save_judgment, make_async_client behavior
before refactoring in step 1.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.client import (
    Judgment,
    _BROWSER_HEADERS,
    fetch_judgment,
    make_async_client,
    parse_judgment_response,
    save_judgment,
    save_judgment_local,
)
from hklii_downloader.parser import HKLIICase

SAMPLE_CASE = HKLIICase(lang="en", court="hkcfi", year=2023, number=1234)

SAMPLE_API_RESPONSE = {
    "cases": [{"title": "HKSAR v. Chan Tai Man", "act": "HCCC123/2023"}],
    "db": "hkcfi",
    "date": "2023-06-15T00:00:00+08:00",
    "neutral": "[2023] HKCFI 1234",
    "parallel_citation": ["[2023] 5 HKC 789"],
    "content": "<p>Judgment content.</p>",
    "doc": "https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc",
    "has_translation": True,
}


def _make_judgment(**overrides) -> Judgment:
    defaults = dict(
        case=SAMPLE_CASE,
        title="HKSAR v. Chan Tai Man",
        case_number="HCCC123/2023",
        court_name="hkcfi",
        date="2023-06-15T00:00:00+08:00",
        neutral_citation="[2023] HKCFI 1234",
        parallel_citations=["[2023] 5 HKC 789"],
        content_html="<p>Judgment content.</p>",
        doc_url="https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc",
        has_translation=True,
    )
    defaults.update(overrides)
    return Judgment(**defaults)


class TestMakeAsyncClient:
    def test_default_timeout(self):
        client = make_async_client()
        assert client.timeout == httpx.Timeout(30)

    def test_custom_timeout(self):
        client = make_async_client(timeout=60)
        assert client.timeout == httpx.Timeout(60)

    def test_follow_redirects_enabled(self):
        client = make_async_client()
        assert client.follow_redirects is True

    def test_browser_headers_applied(self):
        client = make_async_client()
        for key, value in _BROWSER_HEADERS.items():
            assert client.headers[key] == value

    def test_chrome_user_agent_no_python(self):
        client = make_async_client()
        ua = client.headers["User-Agent"]
        assert "Chrome/" in ua
        assert "python" not in ua.lower()

    def test_no_proxy_by_default(self):
        client = make_async_client()
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)

    def test_trust_env_disabled(self):
        """trust_env=False prevents httpx from honoring proxy env vars."""
        import inspect

        source = inspect.getsource(make_async_client)
        assert "trust_env=False" in source


class TestFetchJudgment:
    @pytest.mark.asyncio
    async def test_parses_full_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SAMPLE_API_RESPONSE)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            judgment = await fetch_judgment(SAMPLE_CASE, client)

        assert judgment.case == SAMPLE_CASE
        assert judgment.title == "HKSAR v. Chan Tai Man"
        assert judgment.case_number == "HCCC123/2023"
        assert judgment.court_name == "hkcfi"
        assert judgment.date == "2023-06-15T00:00:00+08:00"
        assert judgment.neutral_citation == "[2023] HKCFI 1234"
        assert judgment.parallel_citations == ["[2023] 5 HKC 789"]
        assert judgment.content_html == "<p>Judgment content.</p>"
        assert judgment.doc_url == (
            "https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc"
        )
        assert judgment.has_translation is True

    @pytest.mark.asyncio
    async def test_empty_cases_list_defaults(self):
        data = {**SAMPLE_API_RESPONSE, "cases": []}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=data)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            judgment = await fetch_judgment(SAMPLE_CASE, client)

        assert judgment.title == ""
        assert judgment.case_number == ""

    @pytest.mark.asyncio
    async def test_missing_optional_fields(self):
        data = {"cases": [{"title": "Test"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=data)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            judgment = await fetch_judgment(SAMPLE_CASE, client)

        assert judgment.court_name == ""
        assert judgment.date == ""
        assert judgment.neutral_citation == ""
        assert judgment.parallel_citations == []
        assert judgment.content_html == ""
        assert judgment.doc_url is None
        assert judgment.has_translation is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("falsy_val", ["", None, 0, False])
    async def test_falsy_doc_url_becomes_none(self, falsy_val):
        """data.get("doc") or None converts all falsy values to None."""
        data = {**SAMPLE_API_RESPONSE, "doc": falsy_val}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=data)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            judgment = await fetch_judgment(SAMPLE_CASE, client)

        assert judgment.doc_url is None

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_judgment(SAMPLE_CASE, client)

    @pytest.mark.asyncio
    async def test_uses_case_api_url(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return httpx.Response(200, json=SAMPLE_API_RESPONSE)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await fetch_judgment(SAMPLE_CASE, client)

        assert len(captured) == 1
        assert "getjudgment" in captured[0]
        assert "abbr=hkcfi" in captured[0]
        assert "year=2023" in captured[0]
        assert "num=1234" in captured[0]


class TestSaveJudgment:
    @pytest.mark.asyncio
    async def test_creates_nested_output_dir(self, tmp_path):
        judgment = _make_judgment()
        out = tmp_path / "nested" / "deep"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            await save_judgment(judgment, out, {"html"}, client=client)
        assert out.is_dir()

    @pytest.mark.asyncio
    async def test_save_html(self, tmp_path):
        judgment = _make_judgment()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(judgment, tmp_path, {"html"}, client=client)
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.html"
        assert saved[0].read_text() == "<p>Judgment content.</p>"

    @pytest.mark.asyncio
    async def test_save_txt(self, tmp_path):
        judgment = _make_judgment()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(judgment, tmp_path, {"txt"}, client=client)
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.txt"
        assert "Judgment content." in saved[0].read_text()

    @pytest.mark.asyncio
    async def test_save_json_structure(self, tmp_path):
        judgment = _make_judgment()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(judgment, tmp_path, {"json"}, client=client)
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.json"
        meta = json.loads(saved[0].read_text())
        assert meta == {
            "title": "HKSAR v. Chan Tai Man",
            "case_number": "HCCC123/2023",
            "court": "hkcfi",
            "date": "2023-06-15T00:00:00+08:00",
            "neutral_citation": "[2023] HKCFI 1234",
            "parallel_citations": ["[2023] 5 HKC 789"],
            "doc_url": "https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc",
            "has_translation": True,
            "url": "https://www.hklii.hk/en/cases/hkcfi/2023/1234",
        }

    @pytest.mark.asyncio
    async def test_json_indent_and_unicode(self, tmp_path):
        judgment = _make_judgment(title="陳大文 v. 香港特區政府")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            await save_judgment(judgment, tmp_path, {"json"}, client=client)
        raw = (tmp_path / "hkcfi_2023_1234.json").read_text(encoding="utf-8")
        assert "\n  " in raw
        assert "陳大文" in raw

    @pytest.mark.asyncio
    async def test_save_doc_downloads_binary(self, tmp_path):
        judgment = _make_judgment()
        doc_bytes = b"\xd0\xcf\x11\xe0fake-doc-content"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=doc_bytes)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            saved = await save_judgment(judgment, tmp_path, {"doc"}, client=client)
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.doc"
        assert saved[0].read_bytes() == doc_bytes

    @pytest.mark.asyncio
    async def test_doc_skipped_when_no_url(self, tmp_path):
        judgment = _make_judgment(doc_url=None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(judgment, tmp_path, {"doc"}, client=client)
        assert saved == []

    @pytest.mark.asyncio
    async def test_doc_download_raises_on_http_error(self, tmp_path):
        judgment = _make_judgment()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await save_judgment(judgment, tmp_path, {"doc"}, client=client)

    @pytest.mark.asyncio
    async def test_multiple_formats_all_saved(self, tmp_path):
        judgment = _make_judgment(doc_url=None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(
                judgment, tmp_path, {"html", "txt", "json"}, client=client
            )
        names = {p.name for p in saved}
        assert names == {
            "hkcfi_2023_1234.html",
            "hkcfi_2023_1234.txt",
            "hkcfi_2023_1234.json",
        }

    @pytest.mark.asyncio
    async def test_format_order_is_html_txt_json_doc(self, tmp_path):
        """Paths returned in code order: html, txt, json, doc."""
        judgment = _make_judgment(doc_url=None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(
                judgment, tmp_path, {"html", "txt", "json"}, client=client
            )
        assert [p.suffix for p in saved] == [".html", ".txt", ".json"]

    @pytest.mark.asyncio
    async def test_empty_formats_saves_nothing(self, tmp_path):
        judgment = _make_judgment()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            saved = await save_judgment(judgment, tmp_path, set(), client=client)
        assert saved == []


class TestJudgmentContentText:
    def test_strips_html_tags(self):
        j = _make_judgment(content_html="<p>Hello <b>world</b></p>")
        assert "Hello world" in j.content_text

    def test_delegates_to_html_to_text(self):
        j = _make_judgment(content_html="<div><span>Test</span></div>")
        assert "<div>" not in j.content_text
        assert "Test" in j.content_text


class TestParseJudgmentResponse:
    def test_parses_full_response(self):
        judgment = parse_judgment_response(SAMPLE_CASE, SAMPLE_API_RESPONSE)
        assert judgment.case == SAMPLE_CASE
        assert judgment.title == "HKSAR v. Chan Tai Man"
        assert judgment.case_number == "HCCC123/2023"
        assert judgment.court_name == "hkcfi"
        assert judgment.date == "2023-06-15T00:00:00+08:00"
        assert judgment.neutral_citation == "[2023] HKCFI 1234"
        assert judgment.parallel_citations == ["[2023] 5 HKC 789"]
        assert judgment.content_html == "<p>Judgment content.</p>"
        assert judgment.doc_url == (
            "https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc"
        )
        assert judgment.has_translation is True

    def test_empty_cases_list(self):
        data = {**SAMPLE_API_RESPONSE, "cases": []}
        judgment = parse_judgment_response(SAMPLE_CASE, data)
        assert judgment.title == ""
        assert judgment.case_number == ""

    def test_missing_fields(self):
        data = {"cases": [{"title": "Test"}]}
        judgment = parse_judgment_response(SAMPLE_CASE, data)
        assert judgment.court_name == ""
        assert judgment.date == ""
        assert judgment.neutral_citation == ""
        assert judgment.parallel_citations == []
        assert judgment.content_html == ""
        assert judgment.doc_url is None
        assert judgment.has_translation is False

    @pytest.mark.parametrize("falsy_val", ["", None, 0, False])
    def test_falsy_doc_url_becomes_none(self, falsy_val):
        data = {**SAMPLE_API_RESPONSE, "doc": falsy_val}
        judgment = parse_judgment_response(SAMPLE_CASE, data)
        assert judgment.doc_url is None


class TestSaveJudgmentLocal:
    def test_save_html(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, {"html"})
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.html"
        assert saved[0].read_text() == "<p>Judgment content.</p>"

    def test_save_txt(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, {"txt"})
        assert len(saved) == 1
        assert saved[0].name == "hkcfi_2023_1234.txt"
        assert "Judgment content." in saved[0].read_text()

    def test_save_json_structure(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, {"json"})
        assert len(saved) == 1
        meta = json.loads(saved[0].read_text())
        assert meta == {
            "title": "HKSAR v. Chan Tai Man",
            "case_number": "HCCC123/2023",
            "court": "hkcfi",
            "date": "2023-06-15T00:00:00+08:00",
            "neutral_citation": "[2023] HKCFI 1234",
            "parallel_citations": ["[2023] 5 HKC 789"],
            "doc_url": "https://legalref.judiciary.hk/doc/hkcfi_2023_1234.doc",
            "has_translation": True,
            "url": "https://www.hklii.hk/en/cases/hkcfi/2023/1234",
        }

    def test_creates_output_dir(self, tmp_path):
        judgment = _make_judgment()
        out = tmp_path / "nested" / "deep"
        save_judgment_local(judgment, out, {"html"})
        assert out.is_dir()

    def test_doc_format_ignored(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, {"doc"})
        assert saved == []

    def test_format_order_html_txt_json(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, {"html", "txt", "json"})
        assert [p.suffix for p in saved] == [".html", ".txt", ".json"]

    def test_empty_formats(self, tmp_path):
        judgment = _make_judgment()
        saved = save_judgment_local(judgment, tmp_path, set())
        assert saved == []

    def test_json_preserves_unicode(self, tmp_path):
        judgment = _make_judgment(title="陳大文 v. 香港特區政府")
        save_judgment_local(judgment, tmp_path, {"json"})
        raw = (tmp_path / "hkcfi_2023_1234.json").read_text(encoding="utf-8")
        assert "陳大文" in raw
