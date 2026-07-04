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
