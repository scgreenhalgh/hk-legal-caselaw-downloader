"""Tests for BulkScraper — asyncio.Queue dispatch with retry logic."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.scraper import BulkScraper, ScrapeResult


SAMPLE_JUDGMENT_RESPONSE = {
    "cases": [{"title": "HKSAR v. Test", "act": "HCCC1/2023"}],
    "db": "hkcfi",
    "date": "2023-06-15",
    "neutral": "[2023] HKCFI 1",
    "parallel_citation": [],
    "content": "<p>Judgment text.</p>",
    "doc": None,
    "has_translation": False,
}

SAMPLE_GETCASEFILES_RESPONSE = {
    "totalfiles": 2,
    "judgments": [
        {
            "neutral": "[2023] HKCFI 1",
            "path": "/en/cases/hkcfi/2023/1",
            "date": "2023-01-01",
            "parallel": [],
            "cases": [{"title": "A v B", "act": "HCCC1/2023"}],
        },
        {
            "neutral": "[2023] HKCFI 2",
            "path": "/en/cases/hkcfi/2023/2",
            "date": "2023-01-02",
            "parallel": [],
            "cases": [{"title": "C v D", "act": "HCCC2/2023"}],
        },
    ],
}


def _make_db() -> CheckpointDB:
    return CheckpointDB(":memory:")


def _seed_db(db: CheckpointDB, count: int = 1, court: str = "hkcfi") -> None:
    for i in range(1, count + 1):
        db.upsert_case(court, 2023, i, f"[2023] HKCFI {i}", f"Case {i}", "2023-01-01")


class TestBulkScraperEnumerate:
    @pytest.mark.asyncio
    async def test_enumerate_populates_checkpoint(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_GETCASEFILES_RESPONSE)

        db = _make_db()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
        )
        count = await scraper.enumerate(["hkcfi"])
        assert count == 2
        assert db.stats()["pending"] == 2

    @pytest.mark.asyncio
    async def test_enumerate_multiple_courts(self, tmp_path):
        court_data = {
            "hkcfi": {
                "totalfiles": 1,
                "judgments": [{
                    "neutral": "[2023] HKCFI 1", "path": "/en/cases/hkcfi/2023/1",
                    "date": "2023-01-01", "parallel": [],
                    "cases": [{"title": "A", "act": "1"}],
                }],
            },
            "hkca": {
                "totalfiles": 1,
                "judgments": [{
                    "neutral": "[2023] HKCA 1", "path": "/en/cases/hkca/2023/1",
                    "date": "2023-01-01", "parallel": [],
                    "cases": [{"title": "B", "act": "2"}],
                }],
            },
        }

        async def mock_get(url, **kw):
            for court, data in court_data.items():
                if f"caseDb={court}" in url:
                    return httpx.Response(200, json=data)
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []})

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        count = await scraper.enumerate(["hkcfi", "hkca"])
        assert count == 2
        assert db.stats()["pending"] == 2


class TestBulkScraperDownload:
    @pytest.mark.asyncio
    async def test_downloads_pending_cases(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=2)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 2
        assert result.failed == 0
        assert db.stats()["downloaded"] == 2

    @pytest.mark.asyncio
    async def test_saves_files_in_court_year_dirs(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.download_all()
        court_dir = tmp_path / "hkcfi" / "2023"
        assert court_dir.exists()
        assert (court_dir / "hkcfi_2023_1.html").exists()

    @pytest.mark.asyncio
    async def test_respects_format_selection(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json"},
        )
        await scraper.download_all()
        court_dir = tmp_path / "hkcfi" / "2023"
        assert (court_dir / "hkcfi_2023_1.html").exists()
        assert (court_dir / "hkcfi_2023_1.json").exists()
        assert not (court_dir / "hkcfi_2023_1.txt").exists()

    @pytest.mark.asyncio
    async def test_limit_stops_after_n(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=5)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path, limit=2,
        )
        result = await scraper.download_all()
        assert result.downloaded == 2
        assert db.stats()["pending"] == 3

    @pytest.mark.asyncio
    async def test_mark_failed_on_404(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(404)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 0
        assert result.failed == 1
        assert db.stats()["failed"] == 1

    @pytest.mark.asyncio
    async def test_mark_failed_on_json_decode_error(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, text="<html>Error page</html>")

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 0
        assert result.failed == 1

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(503)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_mark_failed_after_retry_exhaustion(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(500)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            max_retries=2, _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 0
        assert result.failed == 1

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_releases_in_progress_on_start(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=2)
        db.claim_pending()
        assert db.stats()["in_progress"] == 1
        assert db.stats()["pending"] == 1

        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 2

    @pytest.mark.asyncio
    async def test_scrape_result_fields(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert isinstance(result, ScrapeResult)
        assert result.downloaded == 0
        assert result.failed == 0
