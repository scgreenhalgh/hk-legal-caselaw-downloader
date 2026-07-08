"""Tests for the UKPC (hopt-C family) scraper module.

Wire-level parity tests to pin the endpoint URLs (design contract with
HKLII), plus shape tests for parsing gethoptfiles and getother responses,
plus a save-to-disk test that lands files in the case-family layout the
viewer's render pipeline expects.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.ukpc import (
    HOPT_C_COURTS,
    HOPT_C_LANGS,
    UkpcFetchError,
    UkpcJudgment,
    enumerate_hopt_c_court,
    fetch_one_hopt_c_judgment,
    getother_url,
    gethoptfiles_c_url,
    parse_getother_response,
    parse_hopt_c_listing,
    save_ukpc_local,
)


class TestConstants:
    def test_ukpc_is_the_hopt_c_court(self):
        assert HOPT_C_COURTS == ("ukpc",)

    def test_hopt_c_langs_matches_bilingual_default(self):
        assert HOPT_C_LANGS == ("en", "tc")


class TestUrls:
    def test_gethoptfiles_c_url_shape(self):
        """URL matches the SPA's live call:
        /api/gethoptfiles?dbcat=C&abbr=ukpc&lang=EN&itemsPerPage=300&page=1&sort=-date
        (lang uppercased — HKLII expects that on this endpoint).
        """
        url = gethoptfiles_c_url("ukpc", "en", 1, 300)
        assert "dbcat=C" in url
        assert "abbr=ukpc" in url
        assert "lang=EN" in url  # HKLII wants uppercase here
        assert "itemsPerPage=300" in url
        assert "page=1" in url
        assert "sort=-date" in url

    def test_getother_url_shape(self):
        """URL matches the SPA's live call when opening one judgment:
        /api/getother?lang=en&abbr=ukpc&year=1997&num=40
        (lang lowercase — different casing than gethoptfiles).
        """
        url = getother_url("ukpc", 1997, 40, "en")
        assert "abbr=ukpc" in url
        assert "lang=en" in url
        assert "year=1997" in url
        assert "num=40" in url


class TestParseListing:
    """gethoptfiles?dbcat=C response → structured entries.

    Real UKPC response has path field "/YYYY/NUM/" (no /en/legis prefix
    that the domestic hopt uses). Our parser recognizes both — this
    test pins the C-family shape.
    """

    def test_parse_ukpc_files_extracts_year_and_num(self):
        body = {
            "totalfiles": 242,
            "files": [
                {"title": "Yuen v. The Royal HK Golf Club",
                 "path": "/1997/40/", "neutral": "[1997] UKPC 40",
                 "date": "1997-07-29"},
                {"title": "CIR v. Cosmotron Manufacturing",
                 "path": "/1997/42/", "neutral": "[1997] UKPC 42",
                 "date": "1997-07-29"},
            ],
        }
        listing = parse_hopt_c_listing(body)
        assert listing.total == 242
        assert len(listing.entries) == 2
        e0 = listing.entries[0]
        assert e0.year == 1997
        assert e0.num == 40
        assert e0.neutral == "[1997] UKPC 40"
        assert e0.date == "1997-07-29"
        assert e0.title == "Yuen v. The Royal HK Golf Club"

    def test_parse_ukpc_skips_unparseable_paths(self):
        """L1: an unexpected path shape logs + skips, doesn't crash."""
        body = {
            "totalfiles": 2,
            "files": [
                {"path": "/1997/40/", "neutral": "[1997] UKPC 40"},
                {"path": "/some-nonsense", "neutral": "junk"},
            ],
        }
        listing = parse_hopt_c_listing(body)
        assert len(listing.entries) == 1
        assert listing.entries[0].num == 40

    def test_parse_empty_response(self):
        listing = parse_hopt_c_listing({"totalfiles": 0, "files": []})
        assert listing.total == 0
        assert listing.entries == []


class TestParseGetother:
    """getother response → UkpcJudgment. Response shape differs from
    case-family getjudgment (title top-level, db as object).
    """

    def _sample(self, content: str = "<p>real judgment body</p>") -> dict:
        return {
            "id": 2582,
            "title": "Yuen v. The Royal Hong Kong Golf Club",
            "neutral": "[1997] UKPC 40",
            "date": "1997-07-29",
            "path": "/1997/40/",
            "db": {"id": 5, "abbr": "ukpc", "lang": "EN"},
            "file_type": 1,
            "content": content,
        }

    def test_parses_real_shape(self):
        j = parse_getother_response("ukpc", 1997, 40, "en", self._sample())
        assert j.abbr == "ukpc"
        assert j.year == 1997
        assert j.num == 40
        assert j.lang == "en"
        assert j.title.startswith("Yuen v.")
        assert j.neutral == "[1997] UKPC 40"
        assert j.date == "1997-07-29"
        assert "<p>real judgment body</p>" in j.content_html

    def test_empty_content_raises(self):
        """Same policy as case_translations — HKLII sometimes returns
        200 OK with an empty body when their pipeline broke for that
        one file. Reject at parse time.
        """
        with pytest.raises(UkpcFetchError, match="empty content"):
            parse_getother_response("ukpc", 1997, 40, "en", self._sample(""))

    def test_missing_title_yields_empty_string(self):
        """Defensive: missing optional field → empty string, not crash."""
        body = self._sample()
        del body["title"]
        j = parse_getother_response("ukpc", 1997, 40, "en", body)
        assert j.title == ""


class TestSaveLocal:
    """Case-family layout: output/ukpc/<year>/ukpc_<year>_<num>[.tc].{html,txt,json}
    so the viewer's render pipeline and cases table code work unchanged.
    """

    def test_en_saves_to_case_family_layout(self, tmp_path: Path):
        j = UkpcJudgment(
            abbr="ukpc", year=1997, num=40, lang="en",
            title="Yuen v. The Royal HK Golf Club",
            neutral="[1997] UKPC 40",
            date="1997-07-29",
            content_html="<p>Cheng Yuen Appellant</p>",
        )
        saved = save_ukpc_local(tmp_path, j)
        # Files landed exactly where the viewer expects
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.html").exists()
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.txt").exists()
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.json").exists()
        assert "ukpc_1997_40.html" in saved

    def test_tc_uses_tc_suffix(self, tmp_path: Path):
        """TC translation follows case-family convention: ``.tc.html``.
        Viewer's bilingual detection treats it like any other TC sibling.
        """
        j = UkpcJudgment(
            abbr="ukpc", year=1997, num=40, lang="tc",
            title="源訴皇家香港哥爾夫球會",
            neutral="[1997] UKPC 40",
            date="1997-07-29",
            content_html="<p>中文譯本</p>",
        )
        save_ukpc_local(tmp_path, j)
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.tc.html").exists()
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.tc.txt").exists()
        assert (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.tc.json").exists()

    def test_json_sidecar_carries_neutral_and_url(self, tmp_path: Path):
        j = UkpcJudgment(
            abbr="ukpc", year=1997, num=40, lang="en",
            title="Yuen v. Golf Club", neutral="[1997] UKPC 40",
            date="1997-07-29", content_html="<p>x</p>",
        )
        save_ukpc_local(tmp_path, j)
        meta = json.loads(
            (tmp_path / "ukpc" / "1997" / "ukpc_1997_40.json").read_text()
        )
        assert meta["neutral_citation"] == "[1997] UKPC 40"
        assert meta["url"] == "https://www.hklii.hk/en/cases/ukpc/1997/40"
        assert meta["year"] == 1997
        assert meta["num"] == 40


class TestEnumerateEndToEnd:
    """Mock the wire so enumeration paginates correctly, stops when
    len(entries) reaches total, and passes through parse."""

    async def test_paginates_and_stops(self, tmp_path: Path):
        """3-page listing (totalfiles=6, 2 entries per page) —
        enumerator walks page=1,2,3 then stops. No page=4 call.
        """
        calls: list[str] = []

        async def _get(url: str, **kw):
            calls.append(url)
            # Break up 6 entries into 3 pages of 2
            page = int(url.split("page=")[1].split("&")[0])
            files_by_page = {
                1: [{"path": "/1997/40/", "neutral": "[1997] UKPC 40",
                     "date": "1997-07-29", "title": "a"},
                    {"path": "/1997/41/", "neutral": "[1997] UKPC 41",
                     "date": "1997-07-29", "title": "b"}],
                2: [{"path": "/1997/42/", "neutral": "[1997] UKPC 42",
                     "date": "1997-07-29", "title": "c"},
                    {"path": "/1997/43/", "neutral": "[1997] UKPC 43",
                     "date": "1997-07-29", "title": "d"}],
                3: [{"path": "/1997/44/", "neutral": "[1997] UKPC 44",
                     "date": "1997-07-29", "title": "e"},
                    {"path": "/1997/45/", "neutral": "[1997] UKPC 45",
                     "date": "1997-07-29", "title": "f"}],
            }
            return httpx.Response(
                200,
                json={"totalfiles": 6, "files": files_by_page.get(page, [])},
                request=httpx.Request("GET", url),
            )

        entries = await enumerate_hopt_c_court(_get, "ukpc", "en", items_per_page=2)
        assert len(entries) == 6
        # Only three API calls — no page=4 waste
        assert len(calls) == 3
        assert "page=1" in calls[0]
        assert "page=2" in calls[1]
        assert "page=3" in calls[2]

    async def test_500_raises(self, tmp_path: Path):
        async def _get(url: str, **kw):
            return httpx.Response(
                500, text="fail", request=httpx.Request("GET", url),
            )

        with pytest.raises(UkpcFetchError, match="HTTP 500"):
            await enumerate_hopt_c_court(_get, "ukpc", "en")


class TestFetchOne:
    async def test_success_returns_parsed_judgment(self, tmp_path: Path):
        async def _get(url: str, **kw):
            return httpx.Response(200, json={
                "id": 2582, "title": "Yuen v. Golf Club",
                "neutral": "[1997] UKPC 40", "date": "1997-07-29",
                "content": "<p>Cheng Yuen Appellant</p>",
            }, request=httpx.Request("GET", url))

        j = await fetch_one_hopt_c_judgment(_get, "ukpc", 1997, 40, "en")
        assert isinstance(j, UkpcJudgment)
        assert j.num == 40
        assert "Cheng Yuen" in j.content_html

    async def test_non_200_raises(self, tmp_path: Path):
        async def _get(url: str, **kw):
            return httpx.Response(
                500, text="err", request=httpx.Request("GET", url),
            )

        with pytest.raises(UkpcFetchError, match="HTTP 500"):
            await fetch_one_hopt_c_judgment(_get, "ukpc", 1997, 40, "en")

    async def test_empty_content_raises(self, tmp_path: Path):
        """Same shape as case_translations empty-body detection."""
        async def _get(url: str, **kw):
            return httpx.Response(200, json={
                "id": 2582, "title": "x", "content": "",
            }, request=httpx.Request("GET", url))

        with pytest.raises(UkpcFetchError, match="empty content"):
            await fetch_one_hopt_c_judgment(_get, "ukpc", 1997, 40, "en")
