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
            cases = await enumerate_court("hkcfi", mock_get)
        except httpx.ConnectTimeout:
            cases = None

        assert cases is not None, (
            "enumerate_court should retry transient ConnectTimeout, not propagate"
        )
        assert calls == 3
        assert len(cases) == 1
