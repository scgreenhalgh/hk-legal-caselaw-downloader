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


class TestBulkScraperDoc:
    """--allow-doc in bulk mode was a lie — the checkpoint said 'doc'
    was downloaded but no .doc file ever landed. Fix: bulk mode fetches
    doc_url via the pool and writes {stem}.doc[x] when 'doc' is in
    formats and the judgment has a doc_url."""

    async def test_bulk_downloads_doc_when_url_present(self, tmp_path):
        judgment_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }
        calls = []

        async def mock_get(url, **kw):
            calls.append(url)
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=b"docbytes",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"},
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        court_dir = tmp_path / "hkcfi" / "2023"
        assert (court_dir / "hkcfi_2023_1.doc").exists() or \
               (court_dir / "hkcfi_2023_1.docx").exists()

    async def test_bulk_skips_doc_when_no_url_but_no_lie(self, tmp_path):
        """When 'doc' is in formats but judgment.doc_url is None, we don't
        crash and don't record 'doc' as downloaded in the checkpoint."""
        no_doc = {**SAMPLE_JUDGMENT_RESPONSE, "doc": None}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=no_doc,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "doc"},
        )
        await scraper.download_all()
        row = db._conn.execute(
            "SELECT formats FROM cases WHERE court='hkcfi' AND number=1"
        ).fetchone()
        import json as _json
        stored = _json.loads(row[0])
        assert "html" in stored
        assert "doc" not in stored, (
            f"formats should not lie about doc when no doc_url; got {stored}"
        )


class TestBulkScraperWorkerIsolation:
    """A single worker raising an unexpected exception must not cancel
    sibling workers via asyncio.gather. return_exceptions=True (or
    per-worker try/except) contains the crash."""

    async def test_worker_crash_does_not_kill_others(self, tmp_path):
        from unittest.mock import patch

        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=5)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path, workers=3,
        )

        orig = BulkScraper._download_one
        call_count = 0

        async def flaky_download(self, record):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated bug — should not cancel siblings")
            return await orig(self, record)

        with patch.object(BulkScraper, "_download_one", flaky_download):
            result = await scraper.download_all()

        assert result.downloaded >= 4, (
            f"expected >= 4 downloaded despite the crash, "
            f"got downloaded={result.downloaded}, failed={result.failed}"
        )


class TestBulkScraperRobustExcept:
    """The audit found only (ConnectError, TimeoutException) were caught in
    _download_one. Real proxy failures also raise ReadError, WriteError,
    RemoteProtocolError, ProxyError — narrow catch escapes and kills the
    scrape. Fix: broaden to httpx.RequestError. Also catch OSError (disk)
    and IPLeakError (from pool.get) to mark_failed cleanly."""

    async def test_read_error_is_retried_then_marked_failed(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            raise httpx.ReadError("connection reset by peer")

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            max_retries=2, _backoff_base=0.0,
        )
        result = await scraper.download_all()
        # 1 initial + 2 retries = 3 calls
        assert call_count == 3, (
            f"ReadError should retry, got {call_count} calls "
            f"(broad httpx.RequestError not caught?)"
        )
        assert result.downloaded == 0
        assert result.failed == 1

    async def test_remote_protocol_error_is_retried(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.RemoteProtocolError("server closed mid-header")
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert call_count == 2

    async def test_ip_leak_error_marks_failed_not_crashes(self, tmp_path):
        from hklii_downloader.proxy_pool import IPLeakError

        async def mock_get(url, **kw):
            raise IPLeakError("proxy leaked home IP")

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.failed == 1, (
            f"IPLeakError should be caught + marked failed, "
            f"got downloaded={result.downloaded}, failed={result.failed}"
        )

    async def test_oserror_during_save_marks_failed(self, tmp_path):
        """Simulate disk-full: patching Path.write_text to raise OSError
        must land as mark_failed, not an escaped traceback."""
        from unittest.mock import patch

        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        with patch("pathlib.Path.write_text",
                   side_effect=OSError("[Errno 28] No space left on device")):
            result = await scraper.download_all()
        assert result.failed == 1
        assert result.downloaded == 0


class TestBulkScraperRetryPolicy:
    """403 (WAF), 429 (rate limit), 5xx and JSONDecodeError must retry with
    backoff. Failure reasons include the HTTP status and a body preview
    so the operator can distinguish 'Cloudflare block page' from real
    JSON malformation."""

    async def test_403_is_retried(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(403, text="<html>Cloudflare</html>",
                                     request=httpx.Request("GET", url))
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert call_count == 2
        assert result.downloaded == 1

    async def test_json_decode_error_is_retried(self, tmp_path):
        call_count = 0

        async def mock_get(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, text="<html>Not JSON</html>",
                                      request=httpx.Request("GET", url))
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert call_count == 2, (
            f"JSONDecodeError should retry, got {call_count} calls"
        )
        assert result.downloaded == 1

    async def test_failure_reason_includes_status_and_body_preview(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(403, text="<html>Access denied</html>",
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            max_retries=1, _backoff_base=0.0,
        )
        await scraper.download_all()
        row = db._conn.execute(
            "SELECT error FROM cases WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row is not None
        error = row[0]
        assert "403" in error, f"expected status 403 in error, got: {error}"
        assert "Access denied" in error or "Cloudflare" in error or "denied" in error, (
            f"expected body preview in error, got: {error}"
        )


class TestBulkScraperDocRetry:
    """Doc URLs go to legalref.judiciary.hk — a different host with its own
    reachability characteristics. A single proxy hiccup shouldn't sink the
    fetch; _fetch_doc must retry through fresh sessions the same way the
    main getjudgment call does."""

    async def test_doc_fetch_retries_on_transient_error(self, tmp_path):
        judgment = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "doc": "https://legalref.judiciary.hk/x/foo.docx",
        }
        doc_calls = 0

        async def mock_get(url, **kw):
            nonlocal doc_calls
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                doc_calls += 1
                if doc_calls == 1:
                    raise httpx.TimeoutException("first proxy timed out")
                return httpx.Response(200, content=b"docx bytes",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"}, _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert doc_calls == 2, (
            f"expected doc fetch to retry after transient, "
            f"got {doc_calls} attempts"
        )
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.docx").exists()


class TestBulkScraperEmptyContentWithDoc:
    """HCAL / HCCC cases often ship as doc-only: the API returns
    content='' but doc_url points at the actual judgment. With
    --allow-doc + doc_url, we should try the doc before mark_failed."""

    async def test_empty_content_with_doc_url_saves_doc(self, tmp_path):
        judgment_empty_html_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_empty_html_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=b"real docx bytes",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"},
        )
        result = await scraper.download_all()

        assert result.downloaded == 1, (
            f"expected doc-only case to succeed with --allow-doc, "
            f"got downloaded={result.downloaded}, failed={result.failed}"
        )
        court_dir = tmp_path / "hkcfi" / "2023"
        doc = court_dir / "hkcfi_2023_1.docx"
        assert doc.exists(), "expected docx to land on disk"
        assert not (court_dir / "hkcfi_2023_1.html").exists(), (
            "no empty HTML should be written"
        )

    async def test_empty_content_no_allow_doc_still_marks_failed(self, tmp_path):
        judgment_empty_html_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=judgment_empty_html_with_doc,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        # No 'doc' in formats — empty content should still fail
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "txt", "json"},
        )
        result = await scraper.download_all()
        assert result.downloaded == 0
        assert result.failed == 1

    async def test_empty_content_with_allow_doc_but_doc_fetch_fails(self, tmp_path):
        judgment_empty_html_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_empty_html_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(500, text="doc unreachable",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"},
        )
        result = await scraper.download_all()
        assert result.failed == 1
        assert result.downloaded == 0


class TestChallengePageDetection:
    """Unit tests for _looks_like_challenge_page.

    A WAF or origin-side error can return HTTP 200 + a valid JSON envelope
    whose `content` field is an HTML challenge/interstitial page. The existing
    empty-check does not catch this — content_html is non-empty, just wrong.
    S-1 rejects it before the row is marked downloaded.
    """

    def test_english_cloudflare_challenge_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = "<html><body><h1>Just a moment...</h1><p>cloudflare</p></body></html>"
        assert _looks_like_challenge_page(html)

    def test_english_verify_human_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = "<html><body><p>Please verify you are human</p></body></html>"
        assert _looks_like_challenge_page(html)

    def test_english_access_denied_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = "<html><body><h1>Access Denied</h1></body></html>"
        assert _looks_like_challenge_page(html)

    def test_traditional_chinese_wait_challenge_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        # HKLII is bilingual EN + TC. A Chinese-language challenge page would
        # slip past an English-only denylist (completeness gap #9).
        html = "<html><body><p>請稍候，正在驗證您的請求</p></body></html>"
        assert _looks_like_challenge_page(html)

    def test_traditional_chinese_verify_human_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = "<html><body>驗證您是人類</body></html>"
        assert _looks_like_challenge_page(html)

    def test_traditional_chinese_access_restricted_detected(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = "<html><body>訪問受限</body></html>"
        assert _looks_like_challenge_page(html)

    def test_real_judgment_html_not_flagged(self):
        from hklii_downloader.scraper import _looks_like_challenge_page
        html = ("<html><body><h1>[2024] HKCFI 1234</h1>"
                "<p>Between Plaintiff and Defendant</p>"
                "<p>Judgment date: 2024-01-15</p>"
                "<p>The court finds in favour of the plaintiff.</p></body></html>")
        assert not _looks_like_challenge_page(html)

    def test_empty_content_not_flagged_as_challenge(self):
        # Empty content is handled by the existing empty-content branch,
        # not by challenge detection — otherwise recent 2026 judgments
        # (which HKLII serves as content:"" with a doc URL) would be
        # misclassified.
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page("")


class TestBulkScraperChallengePage:
    """A challenge page returned as content_html must mark the row failed with
    a distinctive reason so it can be identified in the checkpoint DB and
    retried, not silently persisted as `downloaded`."""

    async def test_english_challenge_page_content_marks_failed(self, tmp_path):
        challenge_response = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "<html><body>Just a moment... cloudflare</body></html>",
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=challenge_response,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()

        assert result.downloaded == 0
        assert result.failed == 1
        html_files = list(tmp_path.rglob("*.html"))
        assert html_files == [], f"expected no HTML written, got {html_files}"

        row = db._conn.execute(
            "SELECT error FROM cases WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row is not None
        error = (row[0] or "").lower()
        assert "challenge" in error, f"expected 'challenge' in error, got {row[0]!r}"

    async def test_chinese_challenge_page_content_marks_failed(self, tmp_path):
        challenge_response = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "<html><body>請稍候</body></html>",
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=challenge_response,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()

        assert result.failed == 1
        row = db._conn.execute(
            "SELECT error FROM cases WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row is not None
        error = (row[0] or "").lower()
        assert "challenge" in error, f"expected 'challenge' in error, got {row[0]!r}"


class TestBulkScraperEmptyContent:
    """A 200 response whose content field is empty must NOT be saved and
    marked downloaded — that produces 0-byte HTML files that poison RAG.
    Instead the case is marked failed with a distinctive reason."""

    async def test_empty_content_marks_failed_not_downloaded(self, tmp_path):
        empty_response = {**SAMPLE_JUDGMENT_RESPONSE, "content": ""}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=empty_response,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()

        assert result.downloaded == 0
        assert result.failed == 1
        # No 0-byte HTML on disk
        html_files = list(tmp_path.rglob("*.html"))
        assert html_files == [], f"expected no HTML written, got {html_files}"

    async def test_whitespace_only_content_marks_failed(self, tmp_path):
        empty_response = {**SAMPLE_JUDGMENT_RESPONSE, "content": "   \n\t  "}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=empty_response,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()

        assert result.downloaded == 0
        assert result.failed == 1

    async def test_normal_content_still_saved(self, tmp_path):
        async def mock_get(url, **kw):
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        result = await scraper.download_all()
        assert result.downloaded == 1
        assert result.failed == 0


class TestBulkScraperDownloadLang:
    async def test_download_uses_record_lang_for_tc_case(self, tmp_path):
        """A record with lang='tc' must hit getjudgment?lang=tc."""
        called_urls = []

        async def mock_get(url, **kw):
            called_urls.append(url)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkdc", 2026, 5, "N", "T", "2026-01-01", lang="tc")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.download_all()

        judgment_calls = [u for u in called_urls if "getjudgment" in u]
        assert judgment_calls, "no getjudgment call was made"
        assert "lang=tc" in judgment_calls[0], (
            f"expected lang=tc in URL, got: {judgment_calls[0]}"
        )

    async def test_download_uses_en_for_en_case(self, tmp_path):
        called_urls = []

        async def mock_get(url, **kw):
            called_urls.append(url)
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01", lang="en")
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.download_all()

        judgment_calls = [u for u in called_urls if "getjudgment" in u]
        assert "lang=en" in judgment_calls[0]


class TestBulkScraperEnumFreshness:
    async def test_skips_recent_enumeration(self, tmp_path):
        """When enum_max_age_hours>0 and last_seen_at is within window,
        skip the API call for that (court, lang) sweep."""
        import time
        call_urls = []

        async def mock_get(url, **kw):
            call_urls.append(url)
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []},
                                  request=httpx.Request("GET", url))

        db = _make_db()
        # Seed one case with a very recent last_seen_at
        recent = int(time.time()) - 3600  # 1 hour ago
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       lang="en", last_seen_at=recent)

        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            enum_max_age_hours=24,
        )
        await scraper.enumerate(["hkcfi"], langs=("en",))
        assert call_urls == [], (
            f"expected no getcasefiles calls with fresh cache, "
            f"got {len(call_urls)}"
        )

    async def test_reenumerates_when_cache_stale(self, tmp_path):
        """If last enumeration was OLDER than the window, do enumerate."""
        import time
        call_urls = []

        async def mock_get(url, **kw):
            call_urls.append(url)
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []},
                                  request=httpx.Request("GET", url))

        db = _make_db()
        stale = int(time.time()) - (48 * 3600)  # 48 hours ago
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       lang="en", last_seen_at=stale)

        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            enum_max_age_hours=24,
        )
        await scraper.enumerate(["hkcfi"], langs=("en",))
        assert len(call_urls) >= 1, "expected at least one API call"

    async def test_default_max_age_zero_always_enumerates(self, tmp_path):
        """Default (0) preserves old behavior — always re-enumerate."""
        import time
        call_urls = []

        async def mock_get(url, **kw):
            call_urls.append(url)
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []},
                                  request=httpx.Request("GET", url))

        db = _make_db()
        # Even a very recent enumeration should not be skipped
        recent = int(time.time()) - 60
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       lang="en", last_seen_at=recent)

        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi"], langs=("en",))
        assert len(call_urls) >= 1


class TestBulkScraperEnumResponseCache:
    async def test_save_enum_responses_writes_to_output_enum_cache(self, tmp_path):
        response_data = {
            "totalfiles": 1,
            "judgments": [{
                "neutral": "[2023] X 1", "path": "/en/cases/hkcfi/2023/1",
                "date": "2023-01-01", "parallel": [],
                "cases": [{"title": "T", "act": "HCA1/2023"}],
            }],
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=response_data,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            save_enum_responses=True,
        )
        await scraper.enumerate(["hkcfi"], langs=("en",))
        cache_dir = tmp_path / ".enum_cache" / "hkcfi_en"
        assert cache_dir.exists()
        files = list(cache_dir.glob("*.json"))
        assert len(files) == 1

    async def test_no_enum_cache_when_flag_false(self, tmp_path):
        response_data = {"totalfiles": 0, "judgments": []}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=response_data,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi"], langs=("en",))
        # .enum_cache dir shouldn't exist
        assert not (tmp_path / ".enum_cache").exists()


class TestBulkScraperEnumerationUsesPool:
    async def test_enumeration_calls_pool_get_not_direct(self, tmp_path):
        """Enumeration must go through the scraper's injected get() (the
        proxy pool in production), not any direct client. Regression guard
        against enumeration accidentally leaking home IP."""
        call_urls = []

        async def mock_get(url, **kw):
            call_urls.append(url)
            return httpx.Response(200, json={"totalfiles": 0, "judgments": []},
                                  request=httpx.Request("GET", url))

        db = _make_db()
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)
        await scraper.enumerate(["hkcfi"], langs=("en",))

        assert call_urls, "no calls made through injected get"
        # every enumeration URL must be the getcasefiles endpoint
        for u in call_urls:
            assert "getcasefiles" in u


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
