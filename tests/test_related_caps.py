"""Tests for related_caps.py — the getrelatedcaps scraper."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestUrl:
    def test_getrelatedcaps_url_ord(self):
        from hklii_downloader.related_caps import getrelatedcaps_url

        url = getrelatedcaps_url(cap_number="32", abbr="ord", lang="en")
        assert "num_int=32" in url
        assert "abbr=ord" in url
        assert "lang=en" in url

    def test_getrelatedcaps_url_reg(self):
        from hklii_downloader.related_caps import getrelatedcaps_url

        url = getrelatedcaps_url(cap_number="622", abbr="reg", lang="tc")
        assert "num_int=622" in url
        assert "abbr=reg" in url
        assert "lang=tc" in url


class TestAlphaSuffixGuard:
    def test_is_alpha_suffix_cap(self):
        from hklii_downloader.related_caps import is_alpha_suffix_cap

        assert is_alpha_suffix_cap("32A") is True
        assert is_alpha_suffix_cap("622J") is True
        assert is_alpha_suffix_cap("32") is False
        assert is_alpha_suffix_cap("622") is False
        assert is_alpha_suffix_cap("1") is False


class TestParseResponse:
    def test_parse_reg_response(self):
        from hklii_downloader.related_caps import parse_relatedcaps_response

        entries = [
            {"title": "Companies (Winding Up) Rules",
             "num": "32A",
             "path": "/en/legis/reg/32A/"},
            {"title": "Companies (Forms) Regulations",
             "num": "32B",
             "path": "/en/legis/reg/32B/"},
        ]
        edges = parse_relatedcaps_response(
            entries, parent_cap="32", abbr="reg", lang="en",
        )
        assert len(edges) == 2
        # (parent_cap, child_cap, lang, title)
        assert edges[0] == ("32", "32A", "en",
                             "Companies (Winding Up) Rules")

    def test_parse_ord_self_lookup_returns_empty_edges(self):
        """abbr=ord is a degenerate self-lookup — the single returned
        record IS the queried ordinance. No graph edges to extract."""
        from hklii_downloader.related_caps import parse_relatedcaps_response

        entries = [
            {"title": "Companies Ordinance",
             "num": "32",
             "path": "/en/legis/ord/32/"},
        ]
        edges = parse_relatedcaps_response(
            entries, parent_cap="32", abbr="ord", lang="en",
        )
        assert edges == []

    def test_parse_empty_response(self):
        from hklii_downloader.related_caps import parse_relatedcaps_response

        edges = parse_relatedcaps_response(
            [], parent_cap="9999", abbr="reg", lang="en",
        )
        assert edges == []


class TestFetchOne:
    async def test_happy_path(self):
        from hklii_downloader.related_caps import fetch_relatedcaps

        async def mock_get(url, **kw):
            return httpx.Response(
                200,
                json=[
                    {"title": "Companies (Forms) Regulations",
                     "num": "32B", "path": "/en/legis/reg/32B/"},
                ],
                request=httpx.Request("GET", url),
            )
        edges, raw = await fetch_relatedcaps(
            get=mock_get, cap_number="32", abbr="reg", lang="en",
        )
        assert len(edges) == 1
        assert edges[0][1] == "32B"

    async def test_500_raises(self):
        from hklii_downloader.related_caps import (
            fetch_relatedcaps, RelatedcapsFetchError,
        )

        async def mock_get(url, **kw):
            return httpx.Response(
                500, text="err", request=httpx.Request("GET", url),
            )
        with pytest.raises(RelatedcapsFetchError):
            await fetch_relatedcaps(
                get=mock_get, cap_number="32", abbr="reg", lang="en",
            )


class TestRunner:
    async def test_enumerate_pending_only_covers_numeric_caps(self, tmp_path):
        """Alpha-suffix caps (32A, 622J) are excluded — API returns 500
        for them. Cover only base numeric caps in the range."""
        from hklii_downloader.related_caps import RelatedCapsRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            runner = RelatedCapsRunner(
                get=None, checkpoint=db, output_dir=tmp_path,
                cap_range=(1, 3),   # inclusive: 1, 2, 3
                langs=("en",),
            )
            n = runner.enumerate_pending()
            # 3 caps × {ord, reg} × 1 lang = 6 rows
            assert n == 6

            stats = db.relatedcap_stats()
            assert stats["pending"] == 6
        finally:
            db.close()

    async def test_fetch_writes_edges_and_marks_ok(self, tmp_path):
        from hklii_downloader.related_caps import RelatedCapsRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32", "reg", "en")

            async def mock_get(url, **kw):
                return httpx.Response(
                    200,
                    json=[
                        {"title": "Companies (Forms) Regulations",
                         "num": "32A", "path": "/en/legis/reg/32A/"},
                    ],
                    request=httpx.Request("GET", url),
                )
            runner = RelatedCapsRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                cap_range=(32, 32), langs=("en",),
            )
            result = await runner.fetch_pending()
            assert result.downloaded == 1
            assert result.failed == 0

            edge_count = db._conn.execute(
                "SELECT COUNT(*) FROM ord_reg_edges"
            ).fetchone()[0]
            assert edge_count == 1
        finally:
            db.close()

    async def test_500_marks_failed(self, tmp_path):
        from hklii_downloader.related_caps import RelatedCapsRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_relatedcap_fetch("32", "reg", "en")

            async def mock_get(url, **kw):
                return httpx.Response(
                    500, text="err", request=httpx.Request("GET", url),
                )
            runner = RelatedCapsRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                cap_range=(32, 32), langs=("en",),
            )
            result = await runner.fetch_pending()
            assert result.downloaded == 0
            assert result.failed == 1
        finally:
            db.close()
