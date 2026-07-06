"""Tests for EnrichmentRunner — backfill enrichment for already-downloaded cases."""
from __future__ import annotations

import json
from pathlib import Path

import httpx


HTML_WITH_PRESS = (
    '<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/'
    'FACC000003_2025ES.htm">Press Summary (English)</a>'
    '<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/'
    'FACC000003_2025CS.htm">Press Summary (Chinese)</a>'
    "<p>body</p>"
)

APPEAL_HISTORY = [
    {"act": "FACC3/2025", "judgments": [
        {"neutral": "[2026] HKCFA 25", "path": "/en/cases/hkcfa/2026/25",
         "date": "2026-06-17", "lang": "EN", "remarks": ""}]},
]


def _seed_downloaded_case(
    tmp_path: Path, court: str, year: int, number: int,
    content_html: str = "<p>plain</p>",
    case_number: str = "HCA100/2023",
    db=None,
):
    """Set up filesystem + return a CheckpointDB with the case in
    'downloaded' status and all enrichments 'pending'. Pass an existing
    db to reuse (multiple seedings on the same tmp_path)."""
    from hklii_downloader.checkpoint import CheckpointDB
    d = tmp_path / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{court}_{year}_{number}"
    (d / f"{stem}.html").write_text(content_html, encoding="utf-8")
    (d / f"{stem}.json").write_text(json.dumps({
        "title": "T", "case_number": case_number,
        "court": court, "date": f"{year}-01-01",
        "neutral_citation": f"[{year}] X {number}",
        "parallel_citations": [], "doc_url": None,
        "has_translation": False, "url": "https://x",
    }), encoding="utf-8")
    if db is None:
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
    db.upsert_case(court, year, number, f"N{number}", "T", f"{year}-01-01")
    db.claim_pending()
    db.mark_downloaded(court, year, number, ["html", "json"])
    return db


class TestEnrichmentRunner:
    async def test_enriches_summaries_and_appeal_history(self, tmp_path):
        from hklii_downloader.enrichment import EnrichmentRunner

        db = _seed_downloaded_case(
            tmp_path, "hkcfa", 2026, 25,
            content_html=HTML_WITH_PRESS,
            case_number="FACC3/2025",
        )

        async def mock_get(url, **kw):
            if "getappealhistory" in url:
                return httpx.Response(200, json=APPEAL_HISTORY,
                                      request=httpx.Request("GET", url))
            if "ES.htm" in url:
                return httpx.Response(200, text="<html>EN body</html>",
                                      request=httpx.Request("GET", url))
            if "CS.htm" in url:
                return httpx.Response(200, text="<html>ZH 摘要</html>",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=True, do_appeal_history=True,
        )
        result = await runner.enrich_all()

        assert result.processed == 1
        court_dir = tmp_path / "hkcfa" / "2026"
        assert (court_dir / "hkcfa_2026_25.summary_en.html").exists()
        assert (court_dir / "hkcfa_2026_25.summary_zh.html").exists()
        assert (court_dir / "hkcfa_2026_25.appeal_history.json").exists()
        enrich = db.get_enrichment("hkcfa", 2026, 25)
        assert enrich["summary_en"] == "downloaded"
        assert enrich["summary_zh"] == "downloaded"
        assert enrich["appeal_history"] == "downloaded"

    async def test_skips_when_no_pending_enrichment(self, tmp_path):
        from hklii_downloader.enrichment import EnrichmentRunner

        db = _seed_downloaded_case(tmp_path, "hkcfi", 2023, 1)
        # Pre-mark all as "na" so nothing is pending
        for kind in ("summary_en", "summary_zh", "appeal_history"):
            db.mark_enrichment("hkcfi", 2023, 1, kind, "na")

        called = []
        async def mock_get(url, **kw):
            called.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
        )
        result = await runner.enrich_all()
        assert result.processed == 0
        assert called == []

    async def test_do_summaries_false_leaves_summary_status_alone(self, tmp_path):
        from hklii_downloader.enrichment import EnrichmentRunner

        db = _seed_downloaded_case(
            tmp_path, "hkcfa", 2026, 25,
            content_html=HTML_WITH_PRESS,
            case_number="FACC3/2025",
        )

        async def mock_get(url, **kw):
            if "getappealhistory" in url:
                return httpx.Response(200, json=APPEAL_HISTORY,
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=False, do_appeal_history=True,
        )
        await runner.enrich_all()
        enrich = db.get_enrichment("hkcfa", 2026, 25)
        assert enrich["summary_en"] == "pending"
        assert enrich["summary_zh"] == "pending"
        assert enrich["appeal_history"] == "downloaded"

    async def test_marks_na_when_html_missing_press_summary(self, tmp_path):
        from hklii_downloader.enrichment import EnrichmentRunner

        db = _seed_downloaded_case(
            tmp_path, "hkcfi", 2023, 1,
            content_html="<p>Ordinary judgment</p>",
        )

        async def mock_get(url, **kw):
            return httpx.Response(200, json=[],
                                  request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=True, do_appeal_history=False,
        )
        await runner.enrich_all()
        enrich = db.get_enrichment("hkcfi", 2023, 1)
        assert enrich["summary_en"] == "na"
        assert enrich["summary_zh"] == "na"

    async def test_html_missing_still_processes_appeal_history(self, tmp_path):
        """Whole-codebase review (L1 silent skip): pre-fix, if the base
        .html was missing AND do_summaries=True, _enrich_one marked the
        summaries failed and RETURNED — skipping the appeal_history
        block entirely. But appeal_history reads .json (not .html) and
        is functionally independent. On the next run, the row is picked
        up again, hits the same missing-.html early return, and
        appeal_history stays 'pending' forever."""
        from pathlib import Path
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.enrichment import EnrichmentRunner

        # Seed a downloaded case with .json present but .html DELETED.
        db = _seed_downloaded_case(
            tmp_path, "hkcfi", 2023, 1,
            content_html=HTML_WITH_PRESS,
        )
        html_path = tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.html"
        html_path.unlink()

        appeal_calls = []
        async def mock_get(url, **kw):
            if "getappealhistory" in url:
                appeal_calls.append(url)
                return httpx.Response(200, json=APPEAL_HISTORY,
                                      request=httpx.Request("GET", url))
            return httpx.Response(200, text="<html>x</html>",
                                  request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=True, do_appeal_history=True,
        )
        await runner.enrich_all()

        enrich = db.get_enrichment("hkcfi", 2023, 1)
        # summaries fail because base .html is missing — expected
        assert enrich["summary_en"] == "failed"
        assert enrich["summary_zh"] == "failed"
        # appeal_history reads .json which IS present — must not skip
        assert enrich["appeal_history"] == "downloaded", (
            f"appeal_history stuck at {enrich['appeal_history']!r} — "
            "it reads .json and is independent of .html; the summaries "
            "early return should not skip it"
        )
        assert appeal_calls, "appeal_history endpoint was never called"

    async def test_worker_exception_marks_db_failed_not_just_stats(
        self, tmp_path,
    ):
        """Whole-codebase review (L1 silent skip): pre-fix, the worker's
        `except Exception:` incremented stats['failed'] but left the DB
        row's status unchanged (still 'pending'). Next run picks up the
        same row, hits the same exception, loops forever with no error
        trail in the DB.

        Guard: on worker exception, mark every pending enrichment kind
        for the case as 'failed' with the exception text as the error."""
        from hklii_downloader.enrichment import EnrichmentRunner

        db = _seed_downloaded_case(
            tmp_path, "hkcfi", 2023, 1,
            content_html=HTML_WITH_PRESS,
        )

        # Corrupt the .json so appeal_history's json.loads raises.
        json_path = tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.json"
        json_path.write_text("not valid json")

        async def mock_get(url, **kw):
            return httpx.Response(200, text="<html>x</html>",
                                  request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=False, do_appeal_history=True,
        )
        result = await runner.enrich_all()
        assert result.failed == 1

        enrich = db.get_enrichment("hkcfi", 2023, 1)
        assert enrich["appeal_history"] == "failed", (
            f"appeal_history still {enrich['appeal_history']!r} after "
            "worker crashed; the row will be re-picked and re-crash "
            "indefinitely with no error trail"
        )

    async def test_limit_stops_after_n(self, tmp_path):
        from hklii_downloader.enrichment import EnrichmentRunner

        db = None
        for i in range(1, 6):
            db = _seed_downloaded_case(
                tmp_path, "hkcfi", 2023, i, HTML_WITH_PRESS, "HCA/2023",
                db=db,
            )

        async def mock_get(url, **kw):
            if "getappealhistory" in url:
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))
            return httpx.Response(200, text="<html>x</html>", request=httpx.Request("GET", url))

        runner = EnrichmentRunner(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            do_summaries=True, do_appeal_history=True, limit=2,
        )
        result = await runner.enrich_all()
        assert result.processed == 2
