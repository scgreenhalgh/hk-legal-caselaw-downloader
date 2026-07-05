"""Tests for HOPT (Historical / "Other" Publications and Treaties) scraper.

Covers:
  * hopt_documents checkpoint table + accessors
  * URL constructors for gethoptfiles + gettreaty
  * abbr-map (bacpg/bahkg → hktba on the wire)
  * response parsing + on-disk layout
  * HoptRunner enumerate + fetch dispatch through mock async client
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestHoptSchema:
    def test_table_present(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            tables = {
                row[0] for row in
                db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "hopt_documents" in tables
        finally:
            db.close()

    def test_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {
                row[1] for row in
                db._conn.execute(
                    "PRAGMA table_info(hopt_documents)"
                ).fetchall()
            }
            for name in (
                "abbr", "year", "num", "lang", "title",
                "neutral", "doc_date", "status", "formats",
                "error", "last_seen_at",
            ):
                assert name in cols, f"missing {name}"
        finally:
            db.close()


class TestHoptAccessors:
    def test_upsert_hopt_document(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en",
                title="Convention on X", neutral="[2018] HKTS 1",
                doc_date="2018-02-01", last_seen_at=999,
            )
            row = db._conn.execute(
                "SELECT abbr, year, num, lang, title, neutral, "
                "doc_date, status, last_seen_at FROM hopt_documents"
            ).fetchone()
            assert row == (
                "hkts", 2018, 1, "en",
                "Convention on X", "[2018] HKTS 1",
                "2018-02-01", "pending", 999,
            )
        finally:
            db.close()

    def test_claim_pending_hopt(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en", title="X",
                neutral="[2018] HKTS 1", doc_date="2018-02-01",
            )
            rec = db.claim_pending_hopt()
            assert rec is not None
            assert rec.abbr == "hkts"
            assert rec.year == 2018
            assert rec.status == "in_progress"
        finally:
            db.close()

    def test_mark_hopt_downloaded(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en", title="X",
                neutral=None, doc_date=None,
            )
            db.mark_hopt_downloaded(
                abbr="hkts", year=2018, num=1, lang="en",
                formats=["json"],
            )
            row = db._conn.execute(
                "SELECT status, formats FROM hopt_documents"
            ).fetchone()
            assert row[0] == "downloaded"
            assert json.loads(row[1]) == ["json"]
        finally:
            db.close()

    def test_mark_hopt_failed(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en", title="X",
                neutral=None, doc_date=None,
            )
            db.mark_hopt_failed(
                abbr="hkts", year=2018, num=1, lang="en",
                error="HTTP 404",
            )
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents"
            ).fetchone()
            assert row == ("failed", "HTTP 404")
        finally:
            db.close()

    def test_hopt_stats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for i, abbr in enumerate(("hkts", "hkts", "bacpg")):
                db.upsert_hopt_document(
                    abbr=abbr, year=2020, num=i + 1, lang="en",
                    title="T", neutral=None, doc_date=None,
                )
            db.mark_hopt_downloaded(
                abbr="hkts", year=2020, num=1, lang="en",
                formats=["json"],
            )
            stats = db.hopt_stats()
            assert stats["total"] == 3
            assert stats["downloaded"] == 1
            assert stats["pending"] == 2
        finally:
            db.close()

    def test_hopt_stats_by_abbr(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            # Two hkts rows + one bacpg + one bahkg — each with unique
            # (abbr, year, num, lang) so upsert stays insert.
            for i, abbr in enumerate(("hkts", "hkts", "bacpg", "bahkg")):
                db.upsert_hopt_document(
                    abbr=abbr, year=2020, num=i + 1, lang="en",
                    title="T", neutral=None, doc_date=None,
                )
            stats = db.hopt_stats_by_abbr()
            assert stats["hkts"]["total"] == 2
            assert stats["bacpg"]["total"] == 1
            assert stats["bahkg"]["total"] == 1
        finally:
            db.close()


class TestUrlAndAbbrMap:
    def test_gethoptfiles_url(self):
        from hklii_downloader.hopt import gethoptfiles_url

        url = gethoptfiles_url(
            abbr="hkts", lang="en", page=1, items_per_page=100,
        )
        assert "dbcat=other" in url
        assert "abbr=hkts" in url
        assert "lang=en" in url
        assert "page=1" in url

    def test_gettreaty_url(self):
        from hklii_downloader.hopt import gettreaty_url

        url = gettreaty_url(abbr="hkts", year=2018, num=1, lang="en")
        assert "abbr=hkts" in url
        assert "year=2018" in url
        assert "num=1" in url
        assert "lang=en" in url

    def test_wire_abbr_bacpg_maps_to_hktba(self):
        from hklii_downloader.hopt import wire_abbr

        assert wire_abbr("bacpg") == "hktba"
        assert wire_abbr("bahkg") == "hktba"
        assert wire_abbr("hkts") == "hkts"
        assert wire_abbr("hktml") == "hktml"
        assert wire_abbr("hktmc") == "hktmc"

    def test_gettreaty_uses_wire_abbr(self):
        from hklii_downloader.hopt import gettreaty_url

        url = gettreaty_url(abbr="bacpg", year=2015, num=6, lang="en")
        # The SPA-route abbr `bacpg` maps to the wire abbr `hktba`
        assert "abbr=hktba" in url

    def test_gettreaty_url_supports_nd_year(self):
        """No-date treaties (year=nd) must be constructible."""
        from hklii_downloader.hopt import gettreaty_url

        url = gettreaty_url(abbr="hkts", year="nd", num=8, lang="en")
        assert "year=nd" in url
        assert "num=8" in url


class TestParseListingAndPath:
    def test_parse_hopt_files_response(self):
        from hklii_downloader.hopt import parse_hopt_files_response

        body = {
            "totalfiles": 266,
            "files": [
                {
                    "title": "Convention on X",
                    "path": "/en/legis/hkts/2018/1/",
                    "neutral": "[2018] HKTS 1",
                    "date": "2018-02-01",
                },
                {
                    "title": "Agreement Y",
                    "path": "/en/legis/hkts/2019/2/",
                    "neutral": "[2019] HKTS 2",
                    "date": "2019-05-14",
                },
            ],
        }
        parsed = parse_hopt_files_response(body)
        assert parsed.total == 266
        assert parsed.entries[0].year == 2018
        assert parsed.entries[0].num == 1
        assert parsed.entries[0].title == "Convention on X"
        assert parsed.entries[0].neutral == "[2018] HKTS 1"
        assert parsed.entries[1].year == 2019
        assert parsed.entries[1].num == 2

    def test_parse_hopt_files_skips_malformed_paths(self):
        from hklii_downloader.hopt import parse_hopt_files_response

        body = {
            "totalfiles": 1,
            "files": [{"title": "T", "path": "/some/weird/path/",
                       "date": "2020-01-01"}],
        }
        parsed = parse_hopt_files_response(body)
        assert parsed.entries == []

    def test_parse_hopt_files_accepts_nd_year(self):
        """HKLII stores 10 old treaties with year=`nd` (No Date) instead
        of a 4-digit year — /en/legis/hkts/nd/{num}/. Enumeration must
        keep them so we can fetch and back them up."""
        from hklii_downloader.hopt import parse_hopt_files_response

        body = {
            "totalfiles": 1,
            "files": [{
                "title": "AGREEMENT ESTABLISHING ASEAN + 3 …",
                "path": "/en/legis/hkts/nd/8/",
                "neutral": "[ND] HKTS 8",
                "date": None,
            }],
        }
        parsed = parse_hopt_files_response(body)
        assert len(parsed.entries) == 1
        assert parsed.entries[0].year == "nd"
        assert parsed.entries[0].num == 8


class TestSaveHoptLocal:
    def test_save_writes_json(self, tmp_path):
        from hklii_downloader.hopt import save_hopt_local

        doc = {"title": "T", "content": "<p>x</p>"}
        saved = save_hopt_local(
            output_dir=tmp_path,
            abbr="hkts", year=2018, num=1, lang="en",
            doc=doc,
        )
        assert saved == ["json"]
        path = tmp_path / "hopt" / "hkts" / "2018" / "1" / "hkts_2018_1_en.json"
        assert path.exists()
        assert json.loads(path.read_text()) == doc


class TestHoptRunner:
    async def test_enumerate_upserts_all_entries(self, tmp_path):
        from hklii_downloader.hopt import HoptRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            page1 = {
                "totalfiles": 2,
                "files": [
                    {"title": "T1", "path": "/en/legis/hkts/2018/1/",
                     "neutral": "[2018] HKTS 1", "date": "2018-02-01"},
                    {"title": "T2", "path": "/en/legis/hkts/2019/2/",
                     "neutral": "[2019] HKTS 2", "date": "2019-05-14"},
                ],
            }

            async def mock_get(url, **kw):
                return httpx.Response(200, json=page1,
                                      request=httpx.Request("GET", url))

            runner = HoptRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                abbrs=("hkts",), langs=("en",),
            )
            upserted = await runner.enumerate_all()
            assert upserted == 2

            stats = db.hopt_stats()
            assert stats["total"] == 2
            assert stats["pending"] == 2
        finally:
            db.close()

    async def test_fetch_writes_and_marks(self, tmp_path):
        from hklii_downloader.hopt import HoptRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en",
                title="T", neutral="[2018] HKTS 1",
                doc_date="2018-02-01",
            )

            async def mock_get(url, **kw):
                return httpx.Response(
                    200,
                    json={"title": "T", "content": "<p>body</p>",
                          "neutral": "[2018] HKTS 1"},
                    request=httpx.Request("GET", url),
                )

            runner = HoptRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                abbrs=("hkts",), langs=("en",),
            )
            result = await runner.fetch_pending()

            assert result.downloaded == 1
            assert result.failed == 0
            row = db._conn.execute(
                "SELECT status FROM hopt_documents"
            ).fetchone()
            assert row[0] == "downloaded"
            path = (tmp_path / "hopt" / "hkts" / "2018" / "1"
                    / "hkts_2018_1_en.json")
            assert path.exists()
        finally:
            db.close()

    async def test_fetch_500_marks_failed(self, tmp_path):
        from hklii_downloader.hopt import HoptRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkts", year=2018, num=1, lang="en",
                title="T", neutral=None, doc_date=None,
            )

            async def mock_get(url, **kw):
                return httpx.Response(
                    500, text="server", request=httpx.Request("GET", url),
                )

            runner = HoptRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                abbrs=("hkts",), langs=("en",),
            )
            result = await runner.fetch_pending()
            assert result.downloaded == 0
            assert result.failed == 1
        finally:
            db.close()
