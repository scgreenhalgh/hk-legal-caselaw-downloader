"""Tests for case enumeration from getcasefiles API."""
from __future__ import annotations

import json

import httpx

from hklii_downloader.enumerator import (
    CaseEntry,
    enumerate_court,
    parse_case_entry,
)

SAMPLE_ENTRY = {
    "neutral": "[2023] HKCFI 1234",
    "path": "/en/cases/hkcfi/2023/1234",
    "date": "2023-06-15T00:00:00+08:00",
    "parallel": [],
    "cases": [{"title": "HKSAR v. Chan Tai Man", "act": "HCCC123/2023"}],
}


class TestParseCaseEntry:
    def test_parses_full_entry(self):
        entry = parse_case_entry(SAMPLE_ENTRY, "hkcfi")
        assert entry.court == "hkcfi"
        assert entry.year == 2023
        assert entry.number == 1234
        assert entry.neutral == "[2023] HKCFI 1234"
        assert entry.title == "HKSAR v. Chan Tai Man"
        assert entry.date == "2023-06-15T00:00:00+08:00"

    def test_extracts_year_and_number_from_path(self):
        entry = parse_case_entry(
            {**SAMPLE_ENTRY, "path": "/en/cases/hkca/2022/999"},
            "hkca",
        )
        assert entry.year == 2022
        assert entry.number == 999

    def test_empty_cases_list(self):
        entry = parse_case_entry({**SAMPLE_ENTRY, "cases": []}, "hkcfi")
        assert entry.title == ""

    def test_api_url(self):
        entry = parse_case_entry(SAMPLE_ENTRY, "hkcfi")
        assert "getjudgment" in entry.api_url
        assert "abbr=hkcfi" in entry.api_url
        assert "year=2023" in entry.api_url
        assert "num=1234" in entry.api_url


class TestEnumerateCourt:
    async def test_single_page(self):
        response_data = {
            "totalfiles": 2,
            "judgments": [
                {**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"},
                {**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/2"},
            ],
        }

        async def mock_get(url, **kwargs):
            return httpx.Response(200, json=response_data)

        cases = await enumerate_court("hkcfi", mock_get)
        assert len(cases) == 2
        assert cases[0].number == 1
        assert cases[1].number == 2

    async def test_pagination(self):
        page1 = {
            "totalfiles": 3,
            "judgments": [
                {**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"},
                {**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/2"},
            ],
        }
        page2 = {
            "totalfiles": 3,
            "judgments": [
                {**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/3"},
            ],
        }
        pages = [page1, page2]
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            data = pages[call_count]
            call_count += 1
            return httpx.Response(200, json=data)

        cases = await enumerate_court("hkcfi", mock_get, items_per_page=2)
        assert len(cases) == 3
        assert call_count == 2

    async def test_empty_court(self):
        async def mock_get(url, **kwargs):
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []})

        cases = await enumerate_court("hkcfi", mock_get)
        assert cases == []

    async def test_saves_raw_response_when_dir_given(self, tmp_path):
        """save_response_to writes the raw JSON body for each page — audit
        trail so an operator can reproduce 'what did HKLII list on this
        date' from disk alone."""
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }

        async def mock_get(url, **kwargs):
            return httpx.Response(200, json=response_data)

        await enumerate_court(
            "hkcfi", mock_get, save_response_to=tmp_path,
        )
        # Expect exactly one file for this single-page response
        court_dir = tmp_path / "hkcfi_en"
        assert court_dir.exists()
        files = list(court_dir.glob("*.json"))
        assert len(files) == 1, f"expected 1 saved response, got {files}"
        # Filename should include the page number
        assert "page" in files[0].name
        # Content is the full response
        import json as _json
        stored = _json.loads(files[0].read_text())
        assert stored["totalfiles"] == 1

    async def test_saves_one_file_per_page(self, tmp_path):
        page1 = {
            "totalfiles": 3,
            "judgments": [{**SAMPLE_ENTRY, "path": f"/en/cases/hkcfi/2023/{i}"}
                          for i in [1, 2]],
        }
        page2 = {
            "totalfiles": 3,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/3"}],
        }
        pages = [page1, page2]
        idx = 0

        async def mock_get(url, **kw):
            nonlocal idx
            data = pages[idx]
            idx += 1
            return httpx.Response(200, json=data)

        await enumerate_court(
            "hkcfi", mock_get, items_per_page=2, save_response_to=tmp_path,
        )
        files = sorted((tmp_path / "hkcfi_en").glob("*.json"))
        assert len(files) == 2

    async def test_save_response_uses_atomic_write_text(self, tmp_path):
        """S-5: enum-cache writes must go through atomic_write_text so a
        Ctrl-C or ENOSPC mid-write doesn't leave a truncated JSON at the
        final path where downstream audit tooling would silently misread
        it. Existing implementation uses non-atomic Path.write_text."""
        from unittest.mock import patch
        from hklii_downloader import enumerator as enum_mod

        async def mock_get(url, **kw):
            return httpx.Response(
                200, json={"totalfiles": 0, "judgments": []}
            )

        with patch.object(enum_mod, "atomic_write_text") as m:
            await enumerate_court("hkcfi", mock_get, save_response_to=tmp_path)

        assert m.called, (
            "expected enumerator to write enum-cache via atomic_write_text"
        )
        call = m.call_args
        dest = call.args[0] if call.args else call.kwargs.get("path")
        assert dest is not None and str(dest).endswith(".json"), (
            f"expected atomic_write_text called on a .json path, got {dest!r}"
        )

    async def test_no_save_when_dir_is_none(self, tmp_path):
        response_data = {"totalfiles": 0, "judgments": []}

        async def mock_get(url, **kwargs):
            return httpx.Response(200, json=response_data)

        await enumerate_court("hkcfi", mock_get, save_response_to=None)
        assert list(tmp_path.iterdir()) == []

    async def test_on_page_callback(self):
        pages_seen = []

        async def mock_get(url, **kwargs):
            return httpx.Response(200, json={
                "totalfiles": 1,
                "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
            })

        def on_page(page_num, total_pages, count):
            pages_seen.append((page_num, total_pages, count))

        await enumerate_court("hkcfi", mock_get, on_page=on_page)
        assert len(pages_seen) == 1
        assert pages_seen[0] == (1, 1, 1)

    async def test_retries_transient_connect_error(self):
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectTimeout("upstream timeout")
            return httpx.Response(200, json=response_data)

        try:
            cases = await enumerate_court("hkcfi", mock_get, backoff_base=0.0)
        except httpx.ConnectTimeout:
            cases = None

        assert cases is not None, (
            "enumerate_court should retry transient ConnectTimeout, not propagate"
        )
        assert calls == 3
        assert len(cases) == 1

    async def test_retries_transient_read_error(self):
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 2:
                raise httpx.ReadError("connection reset by peer")
            return httpx.Response(200, json=response_data)

        cases = await enumerate_court("hkcfi", mock_get, backoff_base=0.0)
        assert calls == 2, (
            f"expected ReadError to be retried, got calls={calls}"
        )
        assert len(cases) == 1

    async def test_retries_transient_remote_protocol_error(self):
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 2:
                raise httpx.RemoteProtocolError("server closed mid-header")
            return httpx.Response(200, json=response_data)

        cases = await enumerate_court("hkcfi", mock_get, backoff_base=0.0)
        assert calls == 2
        assert len(cases) == 1

    async def test_retries_on_429(self):
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 2:
                return httpx.Response(429, text="<html>Rate limited</html>")
            return httpx.Response(200, json=response_data)

        cases = await enumerate_court("hkcfi", mock_get, backoff_base=0.0)
        assert calls == 2, f"expected 429 to be retried, got calls={calls}"
        assert len(cases) == 1

    async def test_retries_on_5xx(self):
        response_data = {
            "totalfiles": 1,
            "judgments": [{**SAMPLE_ENTRY, "path": "/en/cases/hkcfi/2023/1"}],
        }
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(503, text="<html>Bad gateway</html>")
            return httpx.Response(200, json=response_data)

        cases = await enumerate_court("hkcfi", mock_get, backoff_base=0.0)
        assert calls == 3, f"expected 503 to be retried, got calls={calls}"
        assert len(cases) == 1

    async def test_permanent_404_does_not_retry(self):
        calls = 0

        async def mock_get(url, **kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                404, text="not found",
                request=httpx.Request("GET", url),
            )

        raised = None
        try:
            await enumerate_court(
                "hkcfi", mock_get, max_retries=3, backoff_base=0.0,
            )
        except Exception as e:
            raised = e
        assert raised is not None, "404 must raise, not silently return empty"
        assert isinstance(raised, httpx.HTTPStatusError), (
            f"expected HTTPStatusError on 404, got {type(raised).__name__}"
        )
        assert calls == 1, f"404 must not be retried, got calls={calls}"
