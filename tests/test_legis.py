"""Tests for the legislation scraper.

Covers the pure helpers: URL construction, response parsing, on-disk
layout. Wire-level orchestration is exercised via mock async clients
in TestLegisRunner.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest


class TestLegisLangs:
    """``LEGIS_LANGS`` is the source of truth for which lang variants
    the legis scraper enumerates and fetches. HKLII serves EN, TC AND
    SC for the three legis-native slugs (ord / reg / instrument),
    confirmed live 2026-07-08 via ``getlegisfiles?lang=sc`` +
    ``getcapversions?lang=sc`` + ``getcapversiontoc``. Prior state
    was EN+TC only — SC was flagged as a D3 punt but the endpoints
    Just Work, so pull SC on the same cadence as TC."""

    def test_includes_sc(self):
        from hklii_downloader.legis import LEGIS_LANGS
        assert "sc" in LEGIS_LANGS, (
            "LEGIS_LANGS omits SC; scrape-legis will never pull the "
            "trilingual legis slugs' SC variants, and every SC bucket "
            "in db_freshness will report a permanent 0/N delta."
        )

    def test_includes_en_and_tc(self):
        """Sanity — the tightening must not accidentally drop EN/TC."""
        from hklii_downloader.legis import LEGIS_LANGS
        assert "en" in LEGIS_LANGS
        assert "tc" in LEGIS_LANGS


class TestUrlConstruction:
    def test_getlegisfiles_url(self):
        from hklii_downloader.legis import getlegisfiles_url

        url = getlegisfiles_url(
            cap_type="ord", lang="en", page=1, items_per_page=200,
        )
        # Matches shape observed in chunk-c.js:
        # {lang, capType, capno, title, firstLetter, numRange,
        #  itemsPerPage, page, sort}
        assert "capType=ord" in url
        assert "lang=en" in url
        assert "itemsPerPage=200" in url
        assert "page=1" in url

    def test_getcapversions_url(self):
        from hklii_downloader.legis import getcapversions_url

        url = getcapversions_url(cap="1", lang="en")
        assert url == "https://www.hklii.hk/api/getcapversions?lang=en&cap=1"

    def test_getcapversiontoc_url(self):
        from hklii_downloader.legis import getcapversiontoc_url

        url = getcapversiontoc_url(vid=19113)
        assert url == "https://www.hklii.hk/api/getcapversiontoc?id=19113"


class TestParseListing:
    def test_parse_files_response(self):
        from hklii_downloader.legis import parse_files_response

        body = {
            "totalfiles": 838,
            "files": [
                {"num": "1", "title": "Interpretation and General Clauses Ordinance"},
                {"num": "32", "title": "Companies (Winding Up and Miscellaneous Provisions) Ordinance"},
            ],
        }
        parsed = parse_files_response(body)
        assert parsed.total == 838
        assert [f.num for f in parsed.entries] == ["1", "32"]
        assert parsed.entries[0].title.startswith("Interpretation")

    def test_parse_files_response_empty(self):
        from hklii_downloader.legis import parse_files_response

        parsed = parse_files_response({"totalfiles": 0, "files": []})
        assert parsed.total == 0
        assert parsed.entries == []


class TestPickLatestVersion:
    def test_picks_first_by_default(self):
        """HKLII's getcapversions returns newest first — the first entry
        is the currently-in-force version."""
        from hklii_downloader.legis import pick_latest_version

        versions = [
            {"id": 52016, "date": "2025-12-18T00:00:00+08:00"},
            {"id": 51000, "date": "2024-01-01T00:00:00+08:00"},
            {"id": 19113, "date": "1997-06-30T00:00:00+08:00"},
        ]
        latest = pick_latest_version(versions)
        assert latest["id"] == 52016
        assert latest["date"].startswith("2025-12-18")

    def test_empty_versions_raises(self):
        from hklii_downloader.legis import pick_latest_version, LegisFetchError

        with pytest.raises(LegisFetchError):
            pick_latest_version([])


class TestSaveLocal:
    def test_save_versions_and_content_json(self, tmp_path):
        from hklii_downloader.legis import save_legis_local

        versions = [{"id": 52016, "title": "T", "date": "2025-12-18T00:00:00+08:00"}]
        content = [
            {"subpath": "longTitle", "title": "Long Title", "content": "<p>hi</p>"},
        ]
        saved = save_legis_local(
            output_dir=tmp_path,
            abbr="ord", num="1", lang="en",
            versions=versions, content=content,
        )
        assert set(saved) == {"versions", "content"}

        base = tmp_path / "legis" / "ord" / "1"
        vp = base / "ord_1_en.versions.json"
        cp = base / "ord_1_en.content.json"
        assert vp.exists()
        assert cp.exists()
        assert json.loads(vp.read_text()) == versions
        assert json.loads(cp.read_text()) == content


class TestFetchLegisDocument:
    """End-to-end fetch for one ordinance — mock the async client so no
    network hits, but exercise the real dispatch through getcapversions
    → pick_latest → getcapversiontoc."""

    async def test_happy_path(self, tmp_path):
        from hklii_downloader.legis import fetch_legis_document

        # Prepare canned responses per URL
        versions = [
            {"id": 52016, "title": "T", "date": "2025-12-18T00:00:00+08:00"},
            {"id": 19113, "title": "T", "date": "1997-06-30T00:00:00+08:00"},
        ]
        content = [
            {"subpath": "longTitle", "title": "Long Title",
             "content": "<p>x</p>"},
        ]

        async def mock_get(url, **kw):
            if "getcapversions" in url:
                data = versions
            elif "getcapversiontoc" in url:
                data = content
            else:
                raise AssertionError(f"unexpected url {url}")
            return httpx.Response(
                200, json=data, request=httpx.Request("GET", url),
            )

        doc = await fetch_legis_document(
            get=mock_get, abbr="ord", num="1", lang="en",
        )
        assert doc.latest_vid == 52016
        assert doc.latest_version_date.startswith("2025-12-18")
        assert doc.versions == versions
        assert doc.content == content

    async def test_versions_endpoint_500_raises(self):
        from hklii_downloader.legis import fetch_legis_document, LegisFetchError

        async def mock_get(url, **kw):
            return httpx.Response(
                500, text="server error",
                request=httpx.Request("GET", url),
            )

        with pytest.raises(LegisFetchError):
            await fetch_legis_document(
                get=mock_get, abbr="ord", num="1", lang="en",
            )


class TestLegisRunnerIntegration:
    """Whole-codebase review (L4): test_legis.py covered only pure
    helpers — URL construction, response parsing, save_local,
    fetch_legis_document. LegisRunner.enumerate_all + fetch_pending
    (the primary current-in-force legis scraper's orchestration) had
    NO tests. Regressions in the worker orchestration, upsert
    integration, or error paths would pass all tests. These add
    end-to-end coverage with mocked HTTP."""

    async def test_enumerate_all_upserts_every_discovered_chapter(
        self, tmp_path,
    ):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.legis import LegisRunner

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            async def mock_get(url, **kw):
                # getlegisfiles single-page response for ord/en.
                return httpx.Response(
                    200,
                    json={
                        "totalfiles": 2,
                        "files": [
                            {"num": "1", "title": "Cap. 1"},
                            {"num": "32", "title": "Cap. 32"},
                        ],
                    },
                    request=httpx.Request("GET", url),
                )

            runner = LegisRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                cap_types=("ord",), langs=("en",),
            )
            n = await runner.enumerate_all()
            assert n == 2
            stats = db.legis_stats()
            assert stats["pending"] == 2, stats

        finally:
            db.close()

    async def test_fetch_pending_downloads_and_marks_ok(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.legis import LegisRunner

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en", title="Cap. 1",
            )

            versions = [
                {"id": 52016, "title": "T",
                 "date": "2025-12-18T00:00:00+08:00"},
            ]
            content = [
                {"subpath": "longTitle", "title": "Long Title",
                 "content": "<p>body</p>"},
            ]

            async def mock_get(url, **kw):
                if "getcapversions" in url:
                    return httpx.Response(200, json=versions,
                                          request=httpx.Request("GET", url))
                if "getcapversiontoc" in url:
                    return httpx.Response(200, json=content,
                                          request=httpx.Request("GET", url))
                raise AssertionError(f"unexpected url {url}")

            runner = LegisRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                cap_types=("ord",), langs=("en",),
            )
            result = await runner.fetch_pending()
            assert result.downloaded == 1
            assert result.failed == 0

            stats = db.legis_stats()
            assert stats.get("downloaded", 0) == 1

            # Sidecar files must exist.
            base = tmp_path / "legis" / "ord" / "1"
            assert (base / "ord_1_en.versions.json").exists()
            assert (base / "ord_1_en.content.json").exists()
        finally:
            db.close()

    async def test_fetch_pending_marks_row_failed_on_http_error(
        self, tmp_path,
    ):
        """One row's 500 error must not crash the run — must mark the
        row failed with an error trail, siblings still process."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.legis import LegisRunner

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            db.upsert_legis_document("ord", "1", "en", "Cap. 1")
            db.upsert_legis_document("ord", "32", "en", "Cap. 32")

            async def mock_get(url, **kw):
                if "num=32" in url or "num%3D32" in url or "/32/" in url:
                    return httpx.Response(
                        500, text="err",
                        request=httpx.Request("GET", url),
                    )
                if "getcapversions" in url:
                    return httpx.Response(
                        200,
                        json=[{"id": 1, "title": "T",
                               "date": "2025-01-01T00:00:00+08:00"}],
                        request=httpx.Request("GET", url),
                    )
                if "getcapversiontoc" in url:
                    return httpx.Response(
                        200,
                        json=[{"subpath": "x", "title": "X",
                               "content": "<p>y</p>"}],
                        request=httpx.Request("GET", url),
                    )
                raise AssertionError(f"unexpected url {url}")

            runner = LegisRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                cap_types=("ord",), langs=("en",), workers=1,
            )
            result = await runner.fetch_pending()
            # Both rows reach a terminal state.
            assert result.downloaded + result.failed == 2, (
                f"one 500 terminated the whole run: {result}"
            )
        finally:
            db.close()
