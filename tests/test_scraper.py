"""Tests for BulkScraper — asyncio.Queue dispatch with retry logic."""
from __future__ import annotations

import asyncio
import json
import logging
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
                # Real .docx starts with PK\x03\x04 (OOXML ZIP magic).
                # Anything else is rejected by the shape-check in
                # _fetch_doc — see TestBulkScraperDocFallbackMagicByteGuard.
                return httpx.Response(200, content=b"PK\x03\x04docbytes",
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
                return httpx.Response(200, content=b"PK\x03\x04docx bytes",
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
                return httpx.Response(200, content=b"PK\x03\x04real docx bytes",
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


class TestBulkScraperDocFallbackMagicByteGuard:
    """Doc-fallback poisoning defense: if Judiciary F5 flips WAF mid-run,
    an HTTP 200 body may be an HTML challenge page (~2-8 KB), not a docx.
    Sibling paths already guard against this:
      - API branch: _looks_like_challenge_page on content_html (scraper.py:332)
      - Press summary: same check in enrichment.py:88
    _fetch_doc was the last un-guarded path. Writing HTML bytes to a .docx
    file poisons RAG downstream (BadZipFile at ingest time), and stamps
    'downloaded' in the checkpoint so a re-run won't re-fetch. The magic-byte
    check (PK\\x03\\x04 for docx / \\xd0\\xcf\\x11\\xe0 for legacy .doc) is
    cheap and content-driven — Content-Type can be stripped/rewritten by
    proxies/CDNs, magic bytes come from the file itself."""

    async def test_fetch_doc_rejects_html_body_with_invalid_magic(self, tmp_path):
        # Doc-fallback path (content='') + doc URL that returns an HTML
        # challenge page instead of docx bytes.
        judgment_empty_html_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }
        html_challenge_body = (
            b"<html><head><title>Just a moment...</title></head>"
            b"<body>cloudflare challenge</body></html>"
        )

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_empty_html_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                # HTTP 200 but body is HTML (missing PK\x03\x04 / \xd0\xcf\x11\xe0)
                return httpx.Response(
                    200, content=html_challenge_body,
                    headers={"Content-Type": "text/html"},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"}, _backoff_base=0.0,
        )
        result = await scraper.download_all()

        # Row must transition to failed, not downloaded — otherwise a
        # 2-8 KB HTML file sits on disk with a .docx extension.
        assert result.failed == 1, (
            f"expected doc-invalid-magic to fail row; got {result}"
        )
        assert result.downloaded == 0, (
            f"invalid magic must NOT mark row downloaded; got {result}"
        )

        # No poisoned .docx / .doc on disk.
        court_dir = tmp_path / "hkcfi" / "2023"
        assert not (court_dir / "hkcfi_2023_1.docx").exists(), (
            "invalid-magic body must NOT be written as .docx"
        )
        assert not (court_dir / "hkcfi_2023_1.doc").exists(), (
            "invalid-magic body must NOT be written as .doc"
        )

        # Error message must include the distinctive prefix so the monitor's
        # top-error-classes surface WAF-flip signals separately from generic
        # doc-fetch failures.
        row = db._conn.execute(
            "SELECT status, error, formats FROM cases "
            "WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row is not None
        status, error, formats = row
        assert status == "failed", f"expected status=failed, got {status!r}"
        assert error is not None
        assert "doc-invalid-magic" in error, (
            f"expected 'doc-invalid-magic' prefix in error; got {error!r}"
        )
        # 3c = '<' — the first byte of the HTML body.
        assert "3c" in error.lower(), (
            f"expected first-byte hex in error; got {error!r}"
        )
        # No formats should be persisted — nothing landed on disk.
        assert formats in (None, "[]"), (
            f"expected no formats persisted on invalid-magic; got {formats!r}"
        )

    async def test_fetch_doc_rejects_html_body_even_when_content_ok(self, tmp_path):
        # Even when HTML content_html is present (doc is supplementary),
        # a WAF-flip on the doc-fetch endpoint is a signal that must not
        # be silently swallowed. Fail the row so the runbook grep for
        # 'doc-invalid-magic' surfaces it — otherwise a run-wide Judiciary
        # WAF flip would hide behind 'downloaded=N' counters (html saved,
        # doc silently dropped).
        judgment_with_html_and_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_html_and_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=b"<html>challenge</html>",
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"}, _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.failed == 1
        assert result.downloaded == 0

        court_dir = tmp_path / "hkcfi" / "2023"
        assert not (court_dir / "hkcfi_2023_1.docx").exists(), (
            "invalid-magic body must NOT be written as .docx"
        )
        row = db._conn.execute(
            "SELECT status, error FROM cases "
            "WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row[0] == "failed"
        assert "doc-invalid-magic" in (row[1] or ""), (
            f"expected 'doc-invalid-magic' in error; got {row[1]!r}"
        )

    async def test_fetch_doc_accepts_docx_zip_magic(self, tmp_path):
        # Positive control — a real .docx starts with the ZIP magic
        # PK\x03\x04 (docx is an OOXML ZIP archive). Must NOT be rejected.
        judgment_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/foo.docx",
        }
        # Minimal ZIP header — first 4 bytes are the discriminator, remaining
        # bytes are arbitrary since we only shape-check the first 4.
        docx_bytes = b"PK\x03\x04" + b"\x00" * 60

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=docx_bytes,
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
        assert result.failed == 0
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.docx").exists()

    async def test_fetch_doc_accepts_legacy_doc_ole_magic(self, tmp_path):
        # Positive control — legacy .doc uses OLE compound magic
        # \xd0\xcf\x11\xe0. Must NOT be rejected.
        judgment_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/word.doc",
        }
        ole_bytes = b"\xd0\xcf\x11\xe0" + b"\x00" * 60

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=ole_bytes,
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
        assert result.failed == 0
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.doc").exists()

    async def test_fetch_doc_accepts_word_95_magic(self, tmp_path):
        # Positive control — pre-OLE Word 6.0 / Word for Windows 95 uses
        # the \xdb\xa5\x2d\x00 signature. Judiciary serves these for many
        # 1990s judgments (e.g. direct probe of HCCT000064_1996.doc returned
        # 10240 bytes starting `db a5 2d 00 00 00 09 04`). LibreOffice and
        # Word 365 can open them; they belong in the corpus. Real production
        # incident (2026-07-04 run): 11 such rows were mis-marked failed
        # by W1 before the accept list was widened.
        judgment_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/word95.doc",
        }
        word95_bytes = b"\xdb\xa5\x2d\x00" + b"\x00" * 60

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=word95_bytes,
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
        assert result.failed == 0
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.doc").exists()

    async def test_fetch_doc_accepts_rtf_and_writes_dot_rtf(self, tmp_path):
        # Task #67 — RTF files served at `.doc` URLs are complete
        # judgments (verified across HCMA001041_1997, HCMA001055_1997,
        # HCMA000131_1989 — real coram/parties/reasoning). Judiciary chose
        # RTF for some 1990s-early-2000s files. Accept them and write to
        # `.rtf` so the extension matches the format (writing RTF bytes
        # to `.docx` still poisons RAG at ingest time).
        judgment_with_doc = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/legacy.doc",
        }
        rtf_body = b"{\\rtf1\\ansi\\ansicpg936\\uc2 hello judgment}"

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_doc,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=rtf_body,
                                      request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "json", "doc"}, _backoff_base=0.0,
        )
        result = await scraper.download_all()
        assert result.downloaded == 1, (
            f"RTF served at .doc URL must be accepted; got {result}"
        )
        assert result.failed == 0
        # Extension is magic-driven — RTF magic writes to `.rtf`, not `.doc`.
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.rtf").exists(), (
            "RTF body must write to `.rtf` extension"
        )
        assert not (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.doc").exists(), (
            "RTF body must NOT be written to `.doc` — extension must match magic"
        )

    async def test_fetch_doc_magic_drives_extension_not_url(self, tmp_path):
        # Regression guard for #67 — the extension-picker was previously
        # URL-suffix-based (`.docx` if url ends `.docx` else `.doc`). Now
        # it's magic-driven. Locks in that a docx URL returning OLE-Word
        # bytes writes to `.doc` (magic wins), not `.docx` (URL says).
        # Not a scenario Judiciary currently ships, but a real hardening
        # against silent mislabelling.
        judgment_with_docx_url = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/labeled.docx",
        }
        ole_bytes = b"\xd0\xcf\x11\xe0" + b"\x00" * 60  # legacy .doc magic

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=judgment_with_docx_url,
                                      request=httpx.Request("GET", url))
            if "legalref" in url:
                return httpx.Response(200, content=ole_bytes,
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
        # Magic (OLE) drives the extension → `.doc`, even though the URL
        # said `.docx`.
        assert (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.doc").exists()
        assert not (tmp_path / "hkcfi" / "2023" / "hkcfi_2023_1.docx").exists()


class TestRetryBackoffJitter:
    """Deterministic `base * 2**attempt` makes 6 concurrent proxies retry in
    lockstep — itself a bot pattern in access logs (6 identical retry
    intervals from 6 subnets after a 5xx burst). Multiplicative uniform
    jitter in [0.5, 1.5] decorrelates."""

    def test_scraper_jittered_backoff_multiplies_by_random_uniform(self):
        from unittest.mock import patch
        from hklii_downloader.scraper import _jittered_backoff
        with patch(
            "hklii_downloader.scraper.random.uniform", return_value=0.75
        ) as m:
            assert _jittered_backoff(1.0, 3) == 6.0  # 1.0 * 8 * 0.75
            m.assert_called_once_with(0.5, 1.5)

    def test_scraper_jittered_backoff_range(self):
        """Over many draws, delays stay within [0.5*base*2**n, 1.5*base*2**n]
        and DO vary (not always at the max/min)."""
        from hklii_downloader.scraper import _jittered_backoff
        delays = [_jittered_backoff(1.0, 2) for _ in range(50)]
        for d in delays:
            assert 2.0 <= d <= 6.0, f"delay {d} outside [2.0, 6.0]"
        # If jitter is applied, we should see variance across 50 draws.
        assert len(set(delays)) > 5, (
            f"expected varying jittered delays, got {len(set(delays))} unique "
            f"in 50 draws — jitter probably not applied"
        )

    def test_enumerator_jittered_backoff_multiplies_by_random_uniform(self):
        from unittest.mock import patch
        from hklii_downloader.enumerator import _jittered_backoff
        with patch(
            "hklii_downloader.enumerator.random.uniform", return_value=1.25
        ) as m:
            assert _jittered_backoff(2.0, 1) == 5.0  # 2.0 * 2 * 1.25
            m.assert_called_once_with(0.5, 1.5)


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

    def test_bare_access_denied_no_longer_detected(self):
        # Task #66 dropped the "access denied" marker: too common in
        # legal English (medical/custody/premises access being denied)
        # to keep as a bare-phrase substring match. A page reading only
        # "Access Denied" no longer fires the detector; real Cloudflare
        # block pages still fire via the "cloudflare" marker (see the
        # test_real_cloudflare_page_still_detected_via_remaining_markers
        # positive control below). The trade-off is documented in
        # content_shape.py.
        html = "<html><body><h1>Access Denied</h1></body></html>"
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

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

    def test_organic_just_a_moment_in_witness_testimony_not_flagged(self):
        # Real production incident (2026-07-04 run): hkcfi/2023/1197 was
        # mis-marked failed because the judgment quotes witness testimony
        # containing "just a moment". Cloudflare's actual interstitial
        # title is "Just a moment..." with a three-dot ellipsis; the bare
        # phrase is far too common in transcripts.
        html = (
            "<html><body>"
            "<p>Q: Do you know what the situation is in Niger today?</p>"
            "<p>Candy:  No, just a moment.  I'm not asking you what the "
            "situation is in Niger; I'm asking you today, do you...</p>"
            "</body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

    def test_organic_just_a_moment_in_judicial_reasoning_not_flagged(self):
        # hkcfi/2024/620 excerpt: "just a moment of anger" was flagged as
        # a Cloudflare challenge page. Sibling of the previous test — same
        # class, different phrasing.
        html = (
            "<html><body>"
            "<p>The court accepts that this was not a premeditated attempt "
            "to seriously harm or kill A1 or A2 other than just a moment "
            "of anger, that in any event it was a private and personal "
            "dispute...</p>"
            "</body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

    def test_cloudflare_just_a_moment_ellipsis_still_detected(self):
        # After tightening the marker to "just a moment..." (three dots),
        # real Cloudflare interstitials MUST still fire. This is the exact
        # title/H1 CF ships as of the incident window.
        html = "<html><head><title>Just a moment...</title></head></html>"
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert _looks_like_challenge_page(html)

    def test_organic_access_denied_in_bill_of_costs_not_flagged(self):
        # Real production incident (2026-07-05 retry sweep): hkcfi/2011/523
        # (bill of costs on insurance claim) fires on "access denied" —
        # meaning access to medical care being denied, not a WAF page.
        # Same class of FP as #63 but on a different marker.
        html = (
            "<html><body>"
            "<p>Spectacles ordered nearly 3 months after access denied, "
            "expenses not proved to be related to incident.</p>"
            "<p>Swim caps ordered nearly 3 months after access denied, "
            "expenses not proved to be related to the denial of access.</p>"
            "</body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

    def test_organic_access_denied_in_family_court_not_flagged(self):
        # hkfc/2012/56 excerpt (child access dispute in family court):
        # "Access denied" refers to the parent's visitation being blocked.
        html = (
            "<html><body>"
            "<p>(f) N's birthday and Access denied;</p>"
            "<p>(g) Other Access denied dates including school events...</p>"
            "</body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

    def test_organic_too_many_requests_in_testimony_not_flagged(self):
        # hkca/1983/60 excerpt: witness described a defendant's behaviour
        # as "one too many requests for money from TANG Lin". Not a rate-
        # limit page — just the phrase used in cross-examination.
        html = (
            "<html><body>"
            "<p>...will have all arisen out of one too many requests "
            "for money from TANG Lin. And that I think, members of the "
            "jury, is the crux of this case.</p>"
            "</body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert not _looks_like_challenge_page(html)

    def test_real_cloudflare_page_still_detected_via_remaining_markers(self):
        # After dropping "access denied" + "too many requests", real WAF
        # pages must still fire via the remaining markers. A realistic CF
        # "sorry, you have been blocked" page contains the CF brand text
        # multiple times — this test locks in that the marker list is not
        # so narrow that CF pages slip through entirely.
        html = (
            "<html><head><title>Attention Required! | Cloudflare</title></head>"
            "<body><h1>Sorry, you have been blocked</h1>"
            "<p>You are unable to access this site.</p>"
            "<p>This website is using a security service to protect "
            "itself. Please enable JavaScript and reload the page. "
            "Cloudflare Ray ID: xxxxx.</p></body></html>"
        )
        from hklii_downloader.scraper import _looks_like_challenge_page
        assert _looks_like_challenge_page(html)


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


class TestBulkScraperHtmlPendingStamp:
    """When --allow-doc + content='' + doc URL present, scraper should
    stamp html_pending_at_hklii so a later `hklii recheck-html` pass can
    find these rows. When content_html IS available (normal case), the
    column stays NULL / gets cleared."""

    async def test_doc_fallback_stamps_html_pending_at_hklii(self, tmp_path):
        # Empty content_html + doc URL → doc-fallback path.
        response = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "",
            "doc": "https://legalref.judiciary.hk/doc/word.doc",
        }

        async def mock_get(url, **kw):
            if "getjudgment" in url:
                return httpx.Response(200, json=response,
                                      request=httpx.Request("GET", url))
            # Doc fetch — legacy .doc uses OLE compound magic
            # \xd0\xcf\x11\xe0. See TestBulkScraperDocFallbackMagicByteGuard.
            return httpx.Response(200, content=b"\xd0\xcf\x11\xe0docdata",
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "doc"},
        )
        result = await scraper.download_all()
        assert result.downloaded == 1
        row = db._conn.execute(
            "SELECT html_pending_at_hklii, formats FROM cases "
            "WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row[0] is not None, (
            f"expected html_pending_at_hklii stamped on doc-fallback; got NULL"
        )
        assert row[0] > 0, f"expected unix ts, got {row[0]}"
        formats = json.loads(row[1])
        assert "doc" in formats
        assert "html" not in formats

    async def test_html_capture_clears_prior_pending_stamp(self, tmp_path):
        # Seed a row already marked as doc-fallback-pending.
        db = _make_db()
        _seed_db(db, count=1)
        db.mark_downloaded("hkcfi", 2023, 1, ["doc"], html_pending_ts=1751600000)
        # Now content_html is available.
        response = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "<html><body>Real judgment text now available.</body></html>",
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=response,
                                  request=httpx.Request("GET", url))

        # First release the in_progress lock (mark_downloaded doesn't re-open),
        # then re-queue as pending so the scraper picks it up.
        db._conn.execute(
            "UPDATE cases SET status='pending' WHERE court='hkcfi' AND year=2023 AND number=1"
        )
        db._conn.commit()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            formats={"html", "txt", "json"},
        )
        await scraper.download_all()
        row = db._conn.execute(
            "SELECT html_pending_at_hklii FROM cases "
            "WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row[0] is None, (
            f"expected html_pending_at_hklii cleared after HTML capture; "
            f"got {row[0]}"
        )


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


class TestBulkScraperFailureLogging:
    """B2 — the runbook's WAF-detection tripwire (line 367)
    `grep 'FAILED\\|mark_failed\\|challenge-page detected' scrape.log`
    returns empty because 9 mark_failed sites in scraper.py never emit a
    WARNING log — only the DB error column. Over a 15-20h unattended
    scrape that leaves the operator blind to a mid-run WAF ramp."""

    async def test_download_failure_emits_warning_log(self, tmp_path, caplog):
        """A 404 (permanent-error branch) must emit a WARNING record on
        the `hklii_downloader.scraper` logger with 'FAILED' in the message
        and the case_id substring so the runbook grep tripwire fires."""

        async def mock_get(url, **kw):
            return httpx.Response(404, request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)  # hkcfi 2023 1
        scraper = BulkScraper(get=mock_get, checkpoint=db, output_dir=tmp_path)

        with caplog.at_level(logging.WARNING, logger="hklii_downloader.scraper"):
            await scraper.download_all()

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "FAILED" in r.getMessage()
        ]
        assert warnings, (
            f"expected WARNING log with 'FAILED' on mark_failed; got records: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        # Case id must appear so operator can grep back to the failing row.
        msg = warnings[0].getMessage()
        assert "hkcfi" in msg and "2023" in msg and "1" in msg, (
            f"expected case_id (hkcfi/2023/1) in FAILED log; got: {msg!r}"
        )

    async def test_challenge_page_emits_distinct_warning(self, tmp_path, caplog):
        """The runbook grep also looks for literal 'challenge-page detected'
        as a distinct WAF signal. Even though the FAILED log would contain
        it via the err string, emit an additional distinct WARNING so the
        signal is unmistakable in a 15-20h log tail."""

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

        with caplog.at_level(logging.WARNING, logger="hklii_downloader.scraper"):
            await scraper.download_all()

        challenge_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "challenge-page detected" in r.getMessage()
        ]
        assert challenge_warnings, (
            f"expected WARNING with 'challenge-page detected' prefix; "
            f"got records: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )


class TestBulkScraperPoolExhausted:
    """B6 + task #65 — when the pool goes fully dead mid-run, workers must
    NOT drain the pending queue by terminal-failing each row. The 2026-07-04
    production run lost 7,730 legit rows in ~44s that way when a spurious
    session-kill cascade left the pool at zero live sessions. Correct
    behavior: re-queue the row (release in_progress → pending) and sleep
    so the pool has time to revive via cooldown. Only terminal-fail once
    a row has cycled through pool-exhausted more than
    `_pool_exhausted_max_retries` times — that's the safety net for a
    genuinely stuck pool.

    Tests below use aggressively-tuned attributes so the retry loop finishes
    in milliseconds; production defaults give ~2 minutes of retry budget
    per row (60 retries * 2s), enough to survive typical mass-kill events
    where cooldown is ~300s but sessions revive on a stagger."""

    async def test_pool_death_re_queues_row_then_recovers(self, tmp_path):
        """On the first hit, the worker must release the row (in_progress
        → pending) and sleep, NOT mark it failed. When the pool recovers
        on the retry, the row completes cleanly. Simulates the transient
        session-kill storm scenario."""
        from hklii_downloader.proxy_pool import AllProxiesDeadError

        attempts = {"count": 0}

        async def mock_get(url, **kw):
            attempts["count"] += 1
            # First getjudgment call → pool dead. Second call onward → live.
            if attempts["count"] == 1:
                raise AllProxiesDeadError("All proxy sessions are dead")
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        # Tune for tests — production defaults give ~2 minutes of retry.
        scraper._pool_exhausted_max_retries = 5
        scraper._pool_exhausted_sleep = 0.001
        result = await scraper.download_all()

        # Row must complete on the retry — pool-exhausted is transient.
        assert result.downloaded == 1, (
            f"row should be downloaded after pool recovered; got {result}"
        )
        assert result.failed == 0, (
            f"transient pool death must NOT terminal-fail the row; "
            f"got {result}"
        )
        # Confirm the mock was called at least twice — once during the
        # pool-dead state, once after recovery.
        assert attempts["count"] >= 2, (
            f"expected re-queue then retry (>= 2 attempts); "
            f"got {attempts['count']}"
        )

    async def test_pool_death_terminal_fails_only_after_max_retries(self, tmp_path):
        """When the pool stays dead for the full retry budget (2026-07-04
        incident recovered in 44s; production budget covers ~2min), THEN
        the row terminal-fails with a distinctive prefix so --retry-failed
        can pick it up next run.

        Also asserts the row is actually retried at least `max_retries`
        times before giving up — otherwise a regression where
        `max_retries=1` (fail-fast) would pass this test's outcome check."""
        from hklii_downloader.proxy_pool import AllProxiesDeadError

        attempts = {"count": 0}

        async def mock_get(url, **kw):
            attempts["count"] += 1
            raise AllProxiesDeadError("All proxy sessions are dead")

        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        scraper._pool_exhausted_max_retries = 3
        scraper._pool_exhausted_sleep = 0.001
        result = await scraper.download_all()

        assert result.downloaded == 0
        assert result.failed == 1, (
            f"row must terminal-fail after exhausting retries; got {result}"
        )
        # The row must actually be retried max_retries times before giving
        # up — pins down the retry-count guarantee so fail-fast regressions
        # (max_retries=1) can't sneak through.
        assert attempts["count"] >= 3, (
            f"row should attempt at least max_retries (3) times before "
            f"terminal-fail; got {attempts['count']}"
        )
        row = db._conn.execute(
            "SELECT status, error FROM cases "
            "WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row[0] == "failed"
        # A distinctive prefix separates "gave up after N retries" from
        # "got one pool_exhausted event" in the monitor's top-error-classes.
        assert "pool-exhausted" in row[1], (
            f"expected 'pool-exhausted' in error; got {row[1]!r}"
        )

    async def test_pool_death_retry_counter_is_per_row(self, tmp_path):
        """Row A's pool-exhausted retries must not consume row B's retry
        budget. Prevents a single sticky row from short-circuiting other
        rows' recovery paths."""
        from hklii_downloader.proxy_pool import AllProxiesDeadError

        attempts = {"row1_attempts": 0, "row2_attempts": 0}

        async def mock_get(url, **kw):
            # Row 1 pool-dies once, then recovers. Row 2 pool-dies once,
            # then recovers. If the counter is per-run instead of per-row,
            # row 2 might terminal-fail because row 1 already used the budget.
            if "num=1" in url:
                attempts["row1_attempts"] += 1
                if attempts["row1_attempts"] == 1:
                    raise AllProxiesDeadError("all dead (row 1)")
            elif "num=2" in url:
                attempts["row2_attempts"] += 1
                if attempts["row2_attempts"] == 1:
                    raise AllProxiesDeadError("all dead (row 2)")
            return httpx.Response(200, json=SAMPLE_JUDGMENT_RESPONSE,
                                  request=httpx.Request("GET", url))

        db = _make_db()
        _seed_db(db, count=2)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=tmp_path,
            _backoff_base=0.0,
        )
        scraper._pool_exhausted_max_retries = 2
        scraper._pool_exhausted_sleep = 0.001
        result = await scraper.download_all()

        # Both rows recover after their independent single retry.
        assert result.downloaded == 2, (
            f"both rows should recover; got {result} "
            f"(row1={attempts['row1_attempts']}, row2={attempts['row2_attempts']})"
        )
        assert result.failed == 0


def _read_events(out_dir) -> list[dict]:
    p = Path(out_dir) / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


class TestBulkScraperEvents:
    """The scraper feeds the observability layer: case-level terminal
    failures emit `case_failed`, WAF interstitials emit `challenge_detected`
    plus a raw failure sample, and mid-run pool death emits `pool_exhausted`.
    EventLogger is optional — None must remain a valid no-op."""

    async def test_emits_case_failed_on_terminal_failure(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        empty = {**SAMPLE_JUDGMENT_RESPONSE, "content": "", "doc": None}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=empty,
                                  request=httpx.Request("GET", url))

        out = tmp_path / "out"
        db = _make_db()
        _seed_db(db, count=1)
        ev = StructuredEventLogger(out)
        await ev.start()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=out, events=ev,
        )
        await scraper.download_all()
        await ev.aclose()

        rows = _read_events(out)
        failed = [r for r in rows if r["kind"] == "case_failed"]
        assert len(failed) == 1, f"expected 1 case_failed row, got {rows}"
        assert failed[0]["court"] == "hkcfi"
        assert failed[0]["num"] == 1
        assert "empty-content" in failed[0]["error_msg"]
        assert failed[0]["error_class"] == "empty-content", (
            f"error_class should bucket cleanly, got {failed[0]['error_class']!r}"
        )

    async def test_emits_challenge_detected_and_raw_sample(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        challenge = {
            **SAMPLE_JUDGMENT_RESPONSE,
            "content": "<html><body>Just a moment... cloudflare</body></html>",
        }

        async def mock_get(url, **kw):
            return httpx.Response(200, json=challenge,
                                  request=httpx.Request("GET", url))

        out = tmp_path / "out"
        db = _make_db()
        _seed_db(db, count=1)
        ev = StructuredEventLogger(out)
        await ev.start()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=out, events=ev,
        )
        await scraper.download_all()
        await ev.aclose()

        rows = _read_events(out)
        challenges = [r for r in rows if r["kind"] == "challenge_detected"]
        assert len(challenges) == 1, (
            f"expected 1 challenge_detected row, got {rows}"
        )
        assert challenges[0]["court"] == "hkcfi"

        # A raw response sample must be dumped for post-run WAF analysis.
        samples = list((out / "failure_samples").glob("*.html"))
        assert len(samples) == 1, f"expected 1 failure sample, got {samples}"
        assert "Just a moment" in samples[0].read_text()

    async def test_emits_pool_exhausted_on_all_proxies_dead(self, tmp_path):
        # Task #65 changed the spec: pool-exhausted rows re-queue with a
        # per-attempt event, then terminal-fail once the retry budget is
        # exhausted. So a stuck pool now emits `max_retries` re-queue events
        # + 1 terminal event per row, not a single terminal event. The
        # observability contract preserved by this test is:
        #   - pool_exhausted events fire on every retry AND on terminal fail
        #   - error_class stays 'pool-exhausted' throughout so the monitor's
        #     top-error-classes still buckets it consistently
        #   - each row has exactly one terminal event (error_class contains
        #     "after N retries")
        from hklii_downloader.events import StructuredEventLogger
        from hklii_downloader.proxy_pool import AllProxiesDeadError

        async def mock_get(url, **kw):
            raise AllProxiesDeadError("all proxy sessions are dead")

        out = tmp_path / "out"
        db = _make_db()
        _seed_db(db, count=2)
        ev = StructuredEventLogger(out)
        await ev.start()
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=out, events=ev,
            _backoff_base=0.0,
        )
        # Tune down so this test runs in ms, not minutes.
        scraper._pool_exhausted_max_retries = 3
        scraper._pool_exhausted_sleep = 0.001
        await scraper.download_all()
        await ev.aclose()

        rows = _read_events(out)
        exhausted = [r for r in rows if r["kind"] == "pool_exhausted"]
        # With max_retries=3: attempts 1-2 emit re-queue, attempt 3 emits
        # terminal — so 3 events per row × 2 rows == 6 total.
        assert len(exhausted) == 6, (
            f"expected 6 pool_exhausted events "
            f"(2 re-queue + 1 terminal × 2 rows), got {len(exhausted)}: {exhausted}"
        )
        # The class-level bucket is 'pool-exhausted' on every re-queue AND
        # broadens to 'pool-exhausted after N retries' only on terminal.
        assert all(
            r["error_class"].startswith("pool-exhausted") for r in exhausted
        ), (
            f"pool_exhausted rows must bucket under 'pool-exhausted*'; "
            f"got {exhausted}"
        )
        # Exactly one terminal event per row (2 total).
        terminal = [
            r for r in exhausted if "after" in r["error_class"]
        ]
        assert len(terminal) == 2, (
            f"expected 1 terminal event per row (2 total); got {terminal}"
        )

    async def test_events_none_is_a_valid_noop(self, tmp_path):
        empty = {**SAMPLE_JUDGMENT_RESPONSE, "content": "", "doc": None}

        async def mock_get(url, **kw):
            return httpx.Response(200, json=empty,
                                  request=httpx.Request("GET", url))

        out = tmp_path / "out"
        db = _make_db()
        _seed_db(db, count=1)
        scraper = BulkScraper(
            get=mock_get, checkpoint=db, output_dir=out, events=None,
        )
        # Must not raise, and must not create an events.jsonl.
        result = await scraper.download_all()
        assert result.failed == 1
        assert not (out / "events.jsonl").exists()
