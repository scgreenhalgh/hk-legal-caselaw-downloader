"""Tests for StructuredEventLogger — atomic JSONL event log + failure samples.

The observability layer feeds post-run triage of a 15-20h / ~228K-request
production scrape. events.jsonl carries one JSON object per line; a slow disk
must never stall the async scraper, and a SIGKILL mid-run must never leave a
half-written line that breaks `jq`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


class TestStructuredEventLoggerAppend:
    async def test_emit_appends_one_jsonl_row_per_event(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        ev.emit("request_success", court="hkcfi", year=2023, num=1,
                http_status=200)
        ev.emit("request_failed", court="hkcfi", year=2023, num=2,
                error_class="HTTP 503", error_msg="HTTP 503 after 3 retries")
        await ev.aclose()

        path = tmp_path / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, f"expected 2 rows, got {lines}"

        rows = [json.loads(line) for line in lines]
        assert rows[0]["kind"] == "request_success"
        assert rows[0]["court"] == "hkcfi"
        assert rows[0]["year"] == 2023
        assert rows[0]["num"] == 1
        assert rows[0]["http_status"] == 200
        assert rows[1]["kind"] == "request_failed"
        assert rows[1]["error_class"] == "HTTP 503"

    async def test_every_row_carries_ts_and_kind(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        ev.emit("warmup", proxy_url="http://localhost:8888")
        await ev.aclose()

        row = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert "ts" in row and row["ts"], "every row must carry a truthy ts"
        assert row["kind"] == "warmup"
        # ISO-8601 with a 'T' date/time separator so `jq` can sort lexically.
        assert "T" in row["ts"], f"ts should be ISO-8601, got {row['ts']!r}"

    async def test_none_fields_are_omitted_not_serialized_as_null(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        # Only court supplied; everything else defaults to None.
        ev.emit("ip_echo", proxy_url="http://localhost:8888")
        await ev.aclose()

        row = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert "court" not in row, f"None fields must be omitted, got {row}"
        assert "http_status" not in row
        assert "error_msg" not in row
        assert row["proxy_url"] == "http://localhost:8888"

    async def test_extra_dict_is_nested_under_extra_key(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        ev.emit("ip_echo", proxy_url="http://localhost:8888",
                extra={"observed_ip": "1.2.3.4", "echo_url": "https://ipinfo.io/json"})
        await ev.aclose()

        row = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert row["extra"]["observed_ip"] == "1.2.3.4"

    async def test_every_line_is_independently_parseable_json(self, tmp_path):
        """A half-written line would break `jq -c` post-run. Each emitted line
        must be a complete JSON object terminated by exactly one newline."""
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        for i in range(50):
            ev.emit("request_success", court="hkcfi", year=2023, num=i)
        await ev.aclose()

        raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert raw.endswith("\n"), "file must end with a trailing newline"
        lines = raw.splitlines()
        assert len(lines) == 50
        for line in lines:
            json.loads(line)  # raises if any line is truncated/corrupt

    async def test_concurrent_emits_do_not_interleave(self, tmp_path):
        """Many worker coroutines emitting at once must not corrupt lines —
        the scraper runs ~20 workers against one events.jsonl."""
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        await ev.start()

        async def worker(wid: int) -> None:
            for i in range(100):
                ev.emit("request_success", court="hkcfi", year=2023,
                        num=wid * 1000 + i)
                await asyncio.sleep(0)

        await asyncio.gather(*[worker(w) for w in range(10)])
        await ev.aclose()

        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        assert len(lines) == 1000, f"expected 1000 rows, got {len(lines)}"
        nums = sorted(json.loads(line)["num"] for line in lines)
        assert nums == sorted(w * 1000 + i for w in range(10) for i in range(100))

    async def test_emit_without_started_writer_falls_back_to_sync_append(
        self, tmp_path,
    ):
        """Unit tests (and any pre-start emit) must still persist the row via a
        direct synchronous append, so EventLogger=None-vs-unstarted never
        silently drops data."""
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        # Note: no await ev.start()
        ev.emit("degraded", proxy_url="http://localhost:8888",
                error_msg="runtime IP check degraded")

        row = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert row["kind"] == "degraded"
        assert row["proxy_url"] == "http://localhost:8888"


class TestStructuredEventLoggerQueueOverflow:
    """Whole-codebase review (L4): the queue-full drop path in emit()
    had zero test coverage. The `dropped` counter, the warning throttle
    at N=1 and every 1000, and the sync-fallback after aclose were all
    unverified. Pin the observable behaviour so regressions can't
    silently break the drop signal."""

    async def test_queue_full_increments_dropped_counter(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        # maxsize=1 with the writer NOT running: the writer would drain,
        # so directly manipulate queue state to guarantee overflow.
        logger = StructuredEventLogger(tmp_path / "e.jsonl", max_queue=1)
        await logger.start()
        # Stop the writer's drain so our next put_nowait overflows.
        # Fill the queue past capacity with sentinel objects.
        logger._queue.put_nowait("preload-blocker")
        # The writer will pop this; wait a moment to let it happen.
        # Actually, easier: patch the queue to full.
        import asyncio as _asyncio

        # Save the writer task, stop it cleanly, then force overflow.
        writer = logger._writer_task
        # Emit while queue is full. First, fill queue to maxsize.
        # Since maxsize=1 and writer is active, one emit fills, next drains.
        # Reliably fill: pause the writer by grabbing all its capacity.
        # Simpler approach: monkeypatch put_nowait to raise QueueFull.

        raised = {"n": 0}
        original_put_nowait = logger._queue.put_nowait

        def always_full(item):
            raised["n"] += 1
            raise _asyncio.QueueFull()

        logger._queue.put_nowait = always_full  # type: ignore[method-assign]

        logger.emit("request_success", proxy_url="x", url="y")
        logger.emit("request_success", proxy_url="x", url="y")
        logger.emit("request_success", proxy_url="x", url="y")

        assert logger._dropped == 3, (
            f"expected dropped=3, got {logger._dropped}"
        )

        # Restore + close cleanly
        logger._queue.put_nowait = original_put_nowait  # type: ignore[method-assign]
        await logger.aclose()

    async def test_queue_full_warning_throttled(self, tmp_path, caplog):
        """First drop + every 1000th drop must WARN — pre-fix, unverified."""
        import asyncio as _asyncio
        import logging
        from hklii_downloader.events import StructuredEventLogger

        logger = StructuredEventLogger(tmp_path / "e.jsonl", max_queue=1)
        await logger.start()
        original_put_nowait = logger._queue.put_nowait
        logger._queue.put_nowait = lambda x: (_ for _ in ()).throw(
            _asyncio.QueueFull()
        )  # type: ignore[method-assign]

        try:
            with caplog.at_level(
                logging.WARNING, logger="hklii_downloader.events"
            ):
                logger.emit("k", proxy_url="x", url="y")  # #1 → warn
                for _ in range(998):
                    logger.emit("k", proxy_url="x", url="y")  # #2..999 → silent
                logger.emit("k", proxy_url="x", url="y")  # #1000 → warn

            warn_messages = [
                r.message for r in caplog.records
                if r.levelname == "WARNING"
                and "queue full" in r.message
            ]
            assert len(warn_messages) == 2, (
                f"expected 2 warnings (drop #1 + drop #1000), got "
                f"{len(warn_messages)}: {warn_messages}"
            )
        finally:
            # Restore before aclose so the stop sentinel can enqueue.
            logger._queue.put_nowait = original_put_nowait  # type: ignore[method-assign]
            await logger.aclose()


class TestFailureSampleDumper:
    """The dumper saves the raw body + headers of challenge-page / failure
    responses to <output>/failure_samples/ for post-run WAF signature
    analysis. Hard caps stop a WAF loop from writing 228K sample files."""

    def test_challenge_samples_capped_at_20_total(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        returns = [
            ev.sample_failure(
                f"challenge_hkcfi_2023_{i}",
                f"<html>Just a moment {i}... cloudflare</html>",
                {"Server": "cloudflare"},
                is_challenge=True,
            )
            for i in range(25)
        ]
        assert sum(returns) == 20, (
            f"exactly 20 challenge samples should be written, got {sum(returns)}"
        )
        assert returns[19] is True and returns[20] is False, (
            "the 21st challenge hit must be count-only (return False)"
        )
        htmls = sorted((tmp_path / "failure_samples").glob("*.html"))
        assert len(htmls) == 20, f"expected 20 .html samples, got {len(htmls)}"

    def test_per_error_prefix_capped_at_5(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        returns = [
            ev.sample_failure(
                "HTTP 503",
                f"<html>gateway error {i}</html>",
                {"Server": "nginx"},
            )
            for i in range(7)
        ]
        assert sum(returns) == 5, (
            f"per-prefix cap is 5, got {sum(returns)} writes"
        )

    def test_distinct_prefixes_have_independent_budgets(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        for i in range(5):
            assert ev.sample_failure("HTTP 503", f"a{i}", {})
        for i in range(5):
            assert ev.sample_failure("HTTP 429", f"b{i}", {})
        # 6th of each prefix is over budget.
        assert ev.sample_failure("HTTP 503", "a5", {}) is False
        assert ev.sample_failure("HTTP 429", "b5", {}) is False
        htmls = list((tmp_path / "failure_samples").glob("*.html"))
        assert len(htmls) == 10, (
            f"two prefixes x 5 each = 10 samples, got {len(htmls)}"
        )

    def test_body_truncated_to_200kb(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        big = "x" * (300 * 1024)  # 300KB
        ok = ev.sample_failure("challenge_big", big, {"Server": "x"},
                               is_challenge=True)
        assert ok
        htmls = list((tmp_path / "failure_samples").glob("*.html"))
        assert len(htmls) == 1
        saved = htmls[0].read_bytes()
        assert len(saved) <= 200 * 1024, (
            f"body must be truncated to 200KB, got {len(saved)} bytes"
        )

    def test_headers_persisted_as_json_for_waf_fingerprinting(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        ev.sample_failure(
            "challenge_hkcfi_2023_1",
            "<html>Just a moment... cloudflare</html>",
            {"Server": "cloudflare", "CF-Ray": "abc123", "Set-Cookie": "cf_clearance=x"},
            is_challenge=True,
        )
        header_files = list((tmp_path / "failure_samples").glob("*.headers.json"))
        assert len(header_files) == 1
        doc = json.loads(header_files[0].read_text())
        # Headers live under a 'headers' key so operators can jq
        # `.headers.Server` / `.headers["CF-Ray"]` across samples.
        assert doc["headers"]["Server"] == "cloudflare"
        assert doc["headers"]["CF-Ray"] == "abc123"

    def test_signature_with_unsafe_chars_is_sanitized(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        ok = ev.sample_failure(
            "HTTP 503; path=/api/getjudgment?x=1",
            "<html>err</html>",
            {},
        )
        assert ok
        htmls = list((tmp_path / "failure_samples").glob("*.html"))
        assert len(htmls) == 1
        # No path separators / query chars leak into the filename.
        name = htmls[0].name
        assert "/" not in name and "?" not in name and "=" not in name

    def test_missing_headers_still_writes_body(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        ev = StructuredEventLogger(tmp_path)
        ok = ev.sample_failure(
            "challenge_summary_hkcfa_2026_25_en",
            "<html>Just a moment... please enable JavaScript</html>",
            None,  # enrichment path has only the body text, no response headers
            is_challenge=True,
        )
        assert ok
        htmls = list((tmp_path / "failure_samples").glob("*.html"))
        assert len(htmls) == 1
        assert "Just a moment" in htmls[0].read_text()
