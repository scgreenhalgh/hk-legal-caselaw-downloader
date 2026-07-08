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

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.ukpc import (
    HOPT_C_COURTS,
    HOPT_C_LANGS,
    UkpcFetchError,
    UkpcJudgment,
    UkpcRunner,
    UkpcRunResult,
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

    def test_parse_live_shape_with_en_cases_prefix(self):
        """LIVE gethoptfiles response path shape is
        ``/en/cases/ukpc/YYYY/NUM`` (probed 2026-07-08 via 20-proxy pool).

        The prior implementation pinned _PATH_RE to the bare ``/YYYY/NUM/``
        shape mentioned in the module comment — which turned out to be
        ``getother``'s path field, NOT ``gethoptfiles``'s. Result: every
        one of the 242 UKPC entries silently skipped as unparseable during
        the first live scrape attempt; Downloaded=0, Failed=0.
        """
        body = {
            "totalfiles": 2,
            "files": [
                {"title": "Yuen v. The Royal Hong Kong Golf Club",
                 "path": "/en/cases/ukpc/1997/40",
                 "neutral": "[1997] UKPC 40",
                 "date": "1997-07-29"},
                {"title": "CIR v. Cosmotron",
                 "path": "/en/cases/ukpc/1997/42",
                 "neutral": "[1997] UKPC 42",
                 "date": "1997-07-29"},
            ],
        }
        listing = parse_hopt_c_listing(body)
        assert listing.total == 2
        assert len(listing.entries) == 2, (
            "live-shape path silently dropped — parser _PATH_RE too "
            "strict about the `/YYYY/NUM/` shape."
        )
        assert listing.entries[0].year == 1997
        assert listing.entries[0].num == 40
        assert listing.entries[0].neutral == "[1997] UKPC 40"

    def test_parse_live_shape_tc_prefix(self):
        """TC endpoint (if ever populated) would return
        ``/tc/cases/ukpc/YYYY/NUM`` — accept it too for forward-compat."""
        body = {
            "totalfiles": 1,
            "files": [{"title": "…", "path": "/tc/cases/ukpc/1998/12",
                       "neutral": "[1998] UKPC 12",
                       "date": "1998-03-01"}],
        }
        listing = parse_hopt_c_listing(body)
        assert len(listing.entries) == 1
        assert listing.entries[0].year == 1998
        assert listing.entries[0].num == 12


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


def _wire_stub(entries_by_lang, content="<p>real body</p>"):
    """Return an async _get that answers gethoptfiles + getother.

    ``entries_by_lang`` is ``{lang: [(year, num, neutral, date, title), ...]}``.
    Every getother call returns the same ``content`` HTML.
    """

    async def _get(url, **kw):
        if "gethoptfiles" in url:
            # UPPERCASE per gethoptfiles_c_url; extract to key entries.
            lang_upper = url.split("lang=")[1].split("&")[0]
            lang = lang_upper.lower()
            entries = entries_by_lang.get(lang, [])
            files = [
                {"path": f"/{y}/{n}/", "neutral": neu,
                 "date": d, "title": t}
                for (y, n, neu, d, t) in entries
            ]
            return httpx.Response(200, json={
                "totalfiles": len(files), "files": files,
            }, request=httpx.Request("GET", url))
        if "getother" in url:
            # Parse year/num from URL so the returned neutral/date match
            # the caller's request (so save_ukpc_local hits the right stem).
            year = int(url.split("year=")[1].split("&")[0])
            num = int(url.split("num=")[1].split("&")[0])
            return httpx.Response(200, json={
                "id": year * 100 + num, "title": f"case {year}/{num}",
                "neutral": f"[{year}] UKPC {num}", "date": f"{year}-01-01",
                "content": content,
            }, request=httpx.Request("GET", url))
        raise AssertionError(f"unexpected URL: {url}")

    return _get


class TestUkpcRunnerCasesTable:
    """UkpcRunner writes ``cases`` table rows the viewer / cases-family
    indexer can pick up unchanged. Single-pass runner: enumerate → fetch
    → save → dual-write cases-table row at status='downloaded'.

    Cases-table dual-write is the requirement flagged in the 2026-07-08
    session close: UKPC belongs in the same cases table as hkcfa/hkca/…
    so the viewer's court indexer surfaces it with no code change.

    Rows must NEVER sit at status='pending' in cases — BulkScraper's
    unscoped ``claim_pending()`` would otherwise pick them up and hit
    ``getjudgment`` (WRONG endpoint family) on every subsequent
    ``hklii scrape`` run.
    """

    async def test_single_entry_dual_writes_cases_row(self, tmp_path: Path):
        get = _wire_stub({"en": [(1997, 40, "[1997] UKPC 40",
                                  "1997-07-29", "Yuen v. Golf Club")]})
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            runner = UkpcRunner(
                get=get, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=1,
            )
            result = await runner.run()

            assert isinstance(result, UkpcRunResult)
            assert result.downloaded == 1
            assert result.failed == 0

            # File landed at case-family layout under output/ukpc/YYYY/
            assert (tmp_path / "ukpc" / "1997"
                    / "ukpc_1997_40.html").exists()
            assert (tmp_path / "ukpc" / "1997"
                    / "ukpc_1997_40.json").exists()

            # cases table row: court='ukpc', status='downloaded'
            row = db._conn.execute(
                "SELECT court, year, number, lang, status, neutral, title "
                "FROM cases "
                "WHERE court='ukpc' AND year=1997 AND number=40"
            ).fetchone()
            assert row is not None, "cases row missing for ukpc/1997/40"
            court, year, number, lang, status, neutral, title = row
            assert court == "ukpc"
            assert year == 1997
            assert number == 40
            assert lang == "en"
            assert status == "downloaded", (
                "UKPC rows must land at status='downloaded' — a 'pending' "
                "row would be picked up by BulkScraper.claim_pending() "
                "which uses the wrong endpoint family for ukpc."
            )
            assert neutral == "[1997] UKPC 40"
            assert title.startswith("case ") or title.startswith("Yuen ")
        finally:
            db.close()

    async def test_multi_entry_all_written(self, tmp_path: Path):
        """A 3-entry enum produces 3 cases rows + 3 file sets."""
        get = _wire_stub({"en": [
            (1997, 40, "[1997] UKPC 40", "1997-07-29", "a"),
            (1997, 41, "[1997] UKPC 41", "1997-07-30", "b"),
            (1998, 12, "[1998] UKPC 12", "1998-03-01", "c"),
        ]})
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            runner = UkpcRunner(
                get=get, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=3,
            )
            result = await runner.run()
            assert result.downloaded == 3
            assert result.failed == 0
            rows = db._conn.execute(
                "SELECT year, number FROM cases "
                "WHERE court='ukpc' AND status='downloaded' "
                "ORDER BY year, number"
            ).fetchall()
            assert rows == [(1997, 40), (1997, 41), (1998, 12)]
        finally:
            db.close()

    async def test_resume_skips_already_downloaded(self, tmp_path: Path):
        """Second run over the same corpus is a no-op — no re-fetch."""
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            # First pass: fetch 1997/40 cleanly
            get1 = _wire_stub({"en": [(1997, 40, "[1997] UKPC 40",
                                       "1997-07-29", "a")]})
            r1 = await UkpcRunner(
                get=get1, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=1,
            ).run()
            assert r1.downloaded == 1

            # Second pass: same corpus, tracker counts fetches
            fetch_calls: list[str] = []

            async def get2(url, **kw):
                if "getother" in url:
                    fetch_calls.append(url)
                return await get1(url, **kw)

            r2 = await UkpcRunner(
                get=get2, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=1,
            ).run()
            # No re-fetch — the already-downloaded row was skipped
            assert fetch_calls == []
            # Runner counts skips distinctly from downloads
            assert r2.downloaded == 0
            assert r2.failed == 0
        finally:
            db.close()

    async def test_getother_failure_marks_failed_counter(
        self, tmp_path: Path,
    ):
        """A getother 500 counts as failed, does NOT insert a cases row,
        and does NOT hang the runner."""

        async def get(url, **kw):
            if "gethoptfiles" in url:
                return httpx.Response(200, json={
                    "totalfiles": 1,
                    "files": [{"path": "/1997/40/", "neutral": "n",
                               "date": "1997-07-29", "title": "t"}],
                }, request=httpx.Request("GET", url))
            return httpx.Response(500, text="err",
                                  request=httpx.Request("GET", url))

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            r = await UkpcRunner(
                get=get, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=1,
            ).run()
            assert r.downloaded == 0
            assert r.failed == 1
            # No cases row inserted for a failed fetch — otherwise a
            # BulkScraper.claim_pending() sweep would pick it up.
            row = db._conn.execute(
                "SELECT COUNT(*) FROM cases WHERE court='ukpc'"
            ).fetchone()
            assert row[0] == 0
        finally:
            db.close()

    async def test_limit_caps_run(self, tmp_path: Path):
        """`limit=2` stops the runner after 2 successful fetches even if
        the enumeration returned more entries."""
        get = _wire_stub({"en": [
            (1997, 40, "[1997] UKPC 40", "1997-07-29", "a"),
            (1997, 41, "[1997] UKPC 41", "1997-07-30", "b"),
            (1998, 12, "[1998] UKPC 12", "1998-03-01", "c"),
        ]})
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            r = await UkpcRunner(
                get=get, checkpoint=db, output_dir=tmp_path,
                langs=("en",), workers=1, limit=2,
            ).run()
            assert r.downloaded == 2
            count = db._conn.execute(
                "SELECT COUNT(*) FROM cases WHERE court='ukpc'"
            ).fetchone()[0]
            assert count == 2
        finally:
            db.close()


class TestScrapeUkpcSubcommand:
    """`hklii scrape-ukpc` — CLI entry point, mirrors `scrape-hopt`."""

    def test_subcommand_registered(self):
        from click.testing import CliRunner

        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scrape-ukpc", "--help"])
        assert result.exit_code == 0, result.output
        assert "ukpc" in result.output.lower()

    def test_subcommand_requires_proxy_or_direct(self):
        from click.testing import CliRunner

        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scrape-ukpc"])
        assert result.exit_code != 0
        assert (
            "proxy" in result.output.lower()
            or "--direct" in result.output.lower()
        )
