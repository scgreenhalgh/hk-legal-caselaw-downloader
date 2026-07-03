"""Integration test — full pipeline: enumerate → download → checkpoint → resume."""
from __future__ import annotations

import json

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.scraper import BulkScraper


def _make_getcasefiles(court, entries):
    return {
        "totalfiles": len(entries),
        "judgments": [
            {
                "neutral": f"[{e['year']}] {court.upper()} {e['num']}",
                "path": f"/en/cases/{court}/{e['year']}/{e['num']}",
                "date": f"{e['year']}-01-01",
                "parallel": [],
                "cases": [{"title": e.get("title", "Test Case"), "act": f"CASE{e['num']}/{e['year']}"}],
            }
            for e in entries
        ],
    }


def _make_judgment(court, year, num, content="<p>Judgment body.</p>"):
    return {
        "cases": [{"title": f"Case {num}", "act": f"CASE{num}/{year}"}],
        "db": court,
        "date": f"{year}-06-15",
        "neutral": f"[{year}] {court.upper()} {num}",
        "parallel_citation": [],
        "content": content,
        "doc": None,
        "has_translation": False,
    }


class TestFullPipeline:
    async def test_enumerate_download_checkpoint(self, tmp_path):
        """Full pipeline: enumerate 3 cases, download all, verify checkpoint."""
        cases = [
            {"year": 2023, "num": 1},
            {"year": 2023, "num": 2},
            {"year": 2023, "num": 3},
        ]
        enum_data = _make_getcasefiles("hkcfi", cases)

        async def mock_get(url, **kw):
            if "getcasefiles" in url:
                return httpx.Response(200, json=enum_data)
            if "getjudgment" in url:
                for c in cases:
                    if f"num={c['num']}" in url:
                        return httpx.Response(200, json=_make_judgment("hkcfi", c["year"], c["num"]))
            return httpx.Response(404)

        db = CheckpointDB(":memory:")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        total = await scraper.enumerate(["hkcfi"])
        assert total == 3
        assert db.stats()["pending"] == 3

        result = await scraper.download_all()
        assert result.downloaded == 3
        assert result.failed == 0

        stats = db.stats()
        assert stats["downloaded"] == 3
        assert stats["pending"] == 0

        for c in cases:
            path = tmp_path / "hkcfi" / str(c["year"]) / f"hkcfi_{c['year']}_{c['num']}.html"
            assert path.exists()

    async def test_resume_after_partial_download(self, tmp_path):
        """Simulate interruption: download 1 of 3, then resume and finish."""
        cases = [
            {"year": 2023, "num": 1},
            {"year": 2023, "num": 2},
            {"year": 2023, "num": 3},
        ]
        enum_data = _make_getcasefiles("hkcfi", cases)

        async def mock_get(url, **kw):
            if "getcasefiles" in url:
                return httpx.Response(200, json=enum_data)
            if "getjudgment" in url:
                for c in cases:
                    if f"num={c['num']}" in url:
                        return httpx.Response(200, json=_make_judgment("hkcfi", c["year"], c["num"]))
            return httpx.Response(404)

        db = CheckpointDB(":memory:")

        scraper1 = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path, limit=1,
        )
        await scraper1.enumerate(["hkcfi"])
        result1 = await scraper1.download_all()
        assert result1.downloaded == 1
        assert db.stats()["pending"] == 2

        scraper2 = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result2 = await scraper2.download_all()
        assert result2.downloaded == 2
        assert db.stats()["downloaded"] == 3
        assert db.stats()["pending"] == 0

    async def test_failed_cases_not_retried(self, tmp_path):
        """Cases marked failed stay failed on resume."""
        cases = [{"year": 2023, "num": 1}, {"year": 2023, "num": 2}]
        enum_data = _make_getcasefiles("hkcfi", cases)

        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            if "getcasefiles" in url:
                return httpx.Response(200, json=enum_data)
            if "getjudgment" in url:
                call_count += 1
                if "num=1" in url:
                    return httpx.Response(404)
                return httpx.Response(200, json=_make_judgment("hkcfi", 2023, 2))
            return httpx.Response(404)

        db = CheckpointDB(":memory:")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi"])
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert result.failed == 1
        assert db.stats()["failed"] == 1

        call_count = 0
        scraper2 = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result2 = await scraper2.download_all()
        assert result2.downloaded == 0
        assert call_count == 0

    async def test_multiple_courts(self, tmp_path):
        """Enumerate and download across multiple courts."""
        court_data = {
            "hkcfi": _make_getcasefiles("hkcfi", [{"year": 2023, "num": 1}]),
            "hkca": _make_getcasefiles("hkca", [{"year": 2023, "num": 1}]),
        }

        async def mock_get(url, **kw):
            if "getcasefiles" in url:
                for court, data in court_data.items():
                    if f"caseDb={court}" in url:
                        return httpx.Response(200, json=data)
            if "getjudgment" in url:
                if "abbr=hkcfi" in url:
                    return httpx.Response(200, json=_make_judgment("hkcfi", 2023, 1))
                if "abbr=hkca" in url:
                    return httpx.Response(200, json=_make_judgment("hkca", 2023, 1))
            return httpx.Response(404)

        db = CheckpointDB(":memory:")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi", "hkca"])
        result = await scraper.download_all()
        assert result.downloaded == 2
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.html").exists()
        assert (tmp_path / "hkca" / "2023" / "hkca_2023_1.html").exists()

    async def test_re_enumerate_upserts(self, tmp_path):
        """Re-enumerating upserts new cases without duplicating existing ones.

        Enumeration sweeps both lang=en and lang=tc; this mock returns the
        real payload only for the en sweep and an empty listing for tc so
        the assertions match the en-only test intent.
        """
        initial = _make_getcasefiles("hkcfi", [{"year": 2023, "num": 1}])
        expanded = _make_getcasefiles("hkcfi", [
            {"year": 2023, "num": 1},
            {"year": 2023, "num": 2},
        ])
        empty = {"totalfiles": 0, "judgments": []}

        en_calls = 0

        async def mock_get(url, **kw):
            nonlocal en_calls
            if "getcasefiles" in url:
                if "lang=tc" in url:
                    return httpx.Response(200, json=empty)
                en_calls += 1
                return httpx.Response(200, json=initial if en_calls == 1 else expanded)
            if "getjudgment" in url:
                return httpx.Response(200, json=_make_judgment("hkcfi", 2023, 1))
            return httpx.Response(404)

        db = CheckpointDB(":memory:")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        await scraper.enumerate(["hkcfi"])
        assert db.stats()["total"] == 1

        await scraper.enumerate(["hkcfi"])
        assert db.stats()["total"] == 2
