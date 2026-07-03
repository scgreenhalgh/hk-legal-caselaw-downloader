"""Tests for BulkScraper — asyncio.Queue dispatch with retry logic."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

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


class TestBulkScraperBilingualEnumerate:
    async def test_enumerate_sweeps_both_langs(self, tmp_path):
        """A tc-only case must be captured by the enumeration sweep even
        when the case is not present in the lang=en listing."""
        en_data = {
            "totalfiles": 1,
            "judgments": [{
                "neutral": "[2026] HKDC 100",
                "path": "/en/cases/hkdc/2026/100",
                "date": "2026-01-01",
                "parallel": [],
                "cases": [{"title": "T-en", "act": "HCA1/2026"}],
            }],
        }
        tc_data = {
            "totalfiles": 2,
            "judgments": [
                {"neutral": "[2026] HKDC 100", "path": "/tc/cases/hkdc/2026/100",
                 "date": "2026-01-01", "parallel": [],
                 "cases": [{"title": "T-tc", "act": "HCA1/2026"}]},
                {"neutral": "[2026] HKDC 5",   "path": "/tc/cases/hkdc/2026/5",
                 "date": "2026-01-01", "parallel": [],
                 "cases": [{"title": "T-tc-only", "act": "HCA5/2026"}]},
            ],
        }

        async def mock_get(url, **kw):
            payload = en_data if "lang=en" in url else tc_data
            return httpx.Response(200, json=payload,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        total = await scraper.enumerate(["hkdc"])
        assert total == 2, f"expected 2 unique cases after dedupe, got {total}"

        # tc-only case must have lang='tc'
        db._conn.execute("UPDATE cases SET status='pending' "
                         "WHERE court='hkdc' AND year=2026 AND number=5")
        db._conn.commit()
        recs = db.pending_cases(courts=["hkdc"])
        by_num = {r.number: r.lang for r in recs}
        assert by_num[5] == "tc"

    async def test_bilingual_case_kept_as_en(self, tmp_path):
        """A case present in BOTH sweeps stays lang='en' (English wins)."""
        en_data = {"totalfiles": 1, "judgments": [
            {"neutral": "[2026] HKCFI 1", "path": "/en/cases/hkcfi/2026/1",
             "date": "2026-01-01", "parallel": [],
             "cases": [{"title": "T-en", "act": "HCA1/2026"}]},
        ]}
        tc_data = {"totalfiles": 1, "judgments": [
            {"neutral": "[2026] HKCFI 1", "path": "/tc/cases/hkcfi/2026/1",
             "date": "2026-01-01", "parallel": [],
             "cases": [{"title": "T-tc", "act": "HCA1/2026"}]},
        ]}

        async def mock_get(url, **kw):
            payload = en_data if "lang=en" in url else tc_data
            return httpx.Response(200, json=payload,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi"])
        recs = db.pending_cases(courts=["hkcfi"])
        assert len(recs) == 1
        assert recs[0].lang == "en"


class TestBulkScraperEnumerate:
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

    async def test_mark_failed_on_json_decode_error(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, text="<html>Error page</html>")

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 0
        assert result.failed == 1

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

    async def test_scrape_result_fields(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert isinstance(result, ScrapeResult)
        assert result.downloaded == 0
        assert result.failed == 0


SAMPLE_JUDGMENT_WITH_PS = {
    "cases": [{"title": "HKSAR v Test", "act": "FACC3/2025"}],
    "db": "hkcfa",
    "date": "2026-06-17",
    "neutral": "[2026] HKCFA 25",
    "parallel_citation": [],
    "content": (
        '<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/'
        'FACC000003_2025ES.htm">Press Summary (English)</a>'
        '<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/'
        'FACC000003_2025CS.htm">Press Summary (Chinese)</a>'
        "<p>Judgment body</p>"
    ),
    "doc": None,
    "has_translation": False,
}

SAMPLE_APPEAL_HISTORY = [
    {"act": "FACC3/2025", "judgments": [
        {"neutral": "[2026] HKCFA 25", "path": "/en/cases/hkcfa/2026/25",
         "date": "2026-06-17", "lang": "EN", "remarks": ""}]},
]


class TestBulkScraperEnrichment:
    async def test_enrichment_disabled_by_default(self, tmp_path):
        calls = []
        async def mock_get(url, **kw):
            calls.append(url)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_WITH_PS,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-06-17")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.download_all()
        # Only the judgment API was called — no summary or appeal history
        assert len(calls) == 1
        assert "getjudgment" in calls[0]

    async def test_enrichment_downloads_press_summaries(self, tmp_path):
        calls = []
        async def mock_get(url, **kw):
            calls.append(url)
            if "getjudgment" in url:
                return httpx.Response(200, json=SAMPLE_JUDGMENT_WITH_PS,
                                      request=httpx.Request("GET", url))
            if "ES.htm" in url:
                return httpx.Response(200, text="<html>EN summary</html>",
                                      request=httpx.Request("GET", url))
            if "CS.htm" in url:
                return httpx.Response(200, text="<html>ZH 摘要</html>",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-06-17")
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            with_summaries=True,
        )
        await scraper.download_all()
        court_dir = tmp_path / "hkcfa" / "2026"
        assert (court_dir / "hkcfa_2026_25.summary_en.html").exists()
        assert (court_dir / "hkcfa_2026_25.summary_zh.html").exists()
        assert "摘要" in (court_dir / "hkcfa_2026_25.summary_zh.html").read_text()
        enrich = db.get_enrichment("hkcfa", 2026, 25)
        assert enrich["summary_en"] == "downloaded"
        assert enrich["summary_zh"] == "downloaded"

    async def test_enrichment_marks_na_when_no_press_summary(self, tmp_path):
        judgment_no_ps = {**SAMPLE_JUDGMENT_WITH_PS,
                          "content": "<p>Ordinary judgment, no summary link</p>"}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=judgment_no_ps,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            with_summaries=True,
        )
        await scraper.download_all()
        enrich = db.get_enrichment("hkcfi", 2023, 1)
        assert enrich["summary_en"] == "na"
        assert enrich["summary_zh"] == "na"

    async def test_enrichment_downloads_appeal_history(self, tmp_path):
        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=SAMPLE_JUDGMENT_WITH_PS,
                                      request=httpx.Request("GET", url))
            if "getappealhistory" in url:
                assert "FACC3%2F2025" in url
                return httpx.Response(200, json=SAMPLE_APPEAL_HISTORY,
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-06-17")
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            with_appeal_history=True,
        )
        await scraper.download_all()
        court_dir = tmp_path / "hkcfa" / "2026"
        path = court_dir / "hkcfa_2026_25.appeal_history.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data[0]["act"] == "FACC3/2025"
        enrich = db.get_enrichment("hkcfa", 2026, 25)
        assert enrich["appeal_history"] == "downloaded"

    async def test_enrichment_failure_does_not_fail_main_download(self, tmp_path):
        """If a press summary fetch fails, the main download is still marked
        downloaded; only the summary's own status flips to failed."""
        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=SAMPLE_JUDGMENT_WITH_PS,
                                      request=httpx.Request("GET", url))
            if "ES.htm" in url:
                return httpx.Response(500, text="",
                                      request=httpx.Request("GET", url))
            if "CS.htm" in url:
                return httpx.Response(200, text="<html>ZH 摘要</html>",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfa", 2026, 25, "N", "T", "2026-06-17")
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            with_summaries=True,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert result.failed == 0
        enrich = db.get_enrichment("hkcfa", 2026, 25)
        assert enrich["summary_en"] == "failed"
        assert enrich["summary_zh"] == "downloaded"


class TestBulkScraperConcurrency:
    async def test_multiple_workers_run_concurrently(self, tmp_path):
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def slow_get(url, **kw):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            async with lock:
                in_flight -= 1
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=6)
        scraper = BulkScraper(
            get=slow_get, checkpoint=db, output_dir=tmp_path,
            workers=3,
        )
        await scraper.download_all()
        assert max_in_flight >= 2, (
            f"expected multiple downloads in flight with workers=3, "
            f"saw max {max_in_flight}"
        )

    async def test_workers_share_limit_correctly(self, tmp_path):
        async def mock_get(url, **kw):
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=20)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            workers=4, limit=5,
        )
        result = await scraper.download_all()
        assert result.downloaded == 5, (
            f"limit=5 exceeded with concurrent workers: {result.downloaded}"
        )

    async def test_on_progress_fires_per_download(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=3)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        events = []
        def on_progress(stats):
            events.append(dict(stats))

        await scraper.download_all(on_progress=on_progress)

        assert len(events) == 3, (
            f"on_progress should fire once per attempt (3), got {len(events)}"
        )
        assert events[-1]["downloaded"] == 3
        assert events[-1]["failed"] == 0
        assert [e["downloaded"] for e in events] == [1, 2, 3]

    async def test_on_progress_reports_failures(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(404)

        db = _make_db()
        _seed_db(db, count=2)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        events = []
        await scraper.download_all(
            on_progress=lambda s: events.append(dict(s)),
        )

        assert len(events) == 2, (
            f"on_progress should fire on failures too, got {len(events)}"
        )
        assert events[-1]["failed"] == 2
        assert events[-1]["downloaded"] == 0

    async def test_single_worker_is_still_sequential(self, tmp_path):
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def slow_get(url, **kw):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE)

        db = _make_db()
        _seed_db(db, count=4)
        scraper = BulkScraper(
            get=slow_get, checkpoint=db, output_dir=tmp_path,
            workers=1,
        )
        await scraper.download_all()
        assert max_in_flight == 1, (
            f"workers=1 should be sequential, saw max {max_in_flight}"
        )
