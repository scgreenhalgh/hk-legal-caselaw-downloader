"""Walk cases previously captured via doc-fallback and re-check whether
HKLII has now processed the HTML.

Motivation: HKLII shows "Only the Word format is available at the moment"
for very recent judgments (getjudgment returns content:"" + doc URL).
The scraper's --allow-doc path captures the .doc/.docx anyway and stamps
html_pending_at_hklii so this pass can find those rows later.

For each pending row:
- Fetch getjudgment
- If content_html is now non-empty AND not a challenge page:
    save_judgment_local (html/txt/json — never overwrite the prior doc),
    mark_downloaded with the union of prior formats and newly saved ones,
    html_pending_ts=None so the pending flag clears.
- If content_html is still empty: bump html_pending_at_hklii to now so
    the next pass picks it up again in FIFO order.
- If the response looks like a challenge page: leave the row unchanged
    and count as failed for reporting.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .checkpoint import CaseRecord, CheckpointDB
from .client import parse_judgment_response, save_judgment_local
from .content_shape import _looks_like_challenge_page
from .parser import HKLIICase


@dataclass
class RecheckResult:
    newly_captured: int
    still_pending: int
    failed: int


class HtmlRecheckRunner:
    def __init__(
        self,
        get: Callable,
        checkpoint: CheckpointDB,
        output_dir: Path,
        formats: set[str] | None = None,
        workers: int = 1,
        limit: int | None = None,
        events=None,
    ):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        # Doc is captured on the original scrape — don't try to overwrite
        # or duplicate it here. Only re-check the HTML-derived formats.
        default = {"html", "txt", "json"}
        self._formats = (formats & default) if formats else default
        self._workers = max(1, workers)
        self._limit = limit
        # events wiring for task #38 lands in the paired impl commit; the
        # attribute exists here so tests can pass events= without a
        # TypeError while assertions still fail on the missing emits.
        self._events = events

    async def recheck_all(self) -> dict[str, int]:
        pending = self._checkpoint.pending_html_recheck(limit=self._limit)
        if not pending:
            return {"newly_captured": 0, "still_pending": 0, "failed": 0}

        counts = {"newly_captured": 0, "still_pending": 0, "failed": 0}
        semaphore = asyncio.Semaphore(self._workers)

        async def worker(record: CaseRecord) -> None:
            async with semaphore:
                outcome = await self._recheck_one(record)
                counts[outcome] += 1

        await asyncio.gather(*(worker(r) for r in pending))
        return counts

    async def _recheck_one(self, record: CaseRecord) -> str:
        case = HKLIICase(
            lang=record.lang, court=record.court,
            year=record.year, number=record.number,
        )
        try:
            resp = await self._get(case.api_url)
        except httpx.RequestError:
            return "failed"

        if resp.status_code != 200:
            return "failed"

        try:
            data = resp.json()
        except Exception:
            return "failed"

        judgment = parse_judgment_response(case, data)

        if _looks_like_challenge_page(judgment.content_html):
            return "failed"

        if not judgment.content_html.strip():
            # Still not extracted at HKLII. Bump the timestamp so this row
            # gets rechecked in later passes and moves toward the back of
            # the FIFO order.
            self._checkpoint.bump_html_pending_ts(
                record.court, record.year, record.number, int(time.time()),
            )
            return "still_pending"

        # HTML available — save it (without touching the prior doc).
        output_dir = self._output_dir / record.court / str(record.year)
        output_dir.mkdir(parents=True, exist_ok=True)
        save_judgment_local(judgment, output_dir, self._formats)

        existing = set(
            self._checkpoint.get_formats(
                record.court, record.year, record.number
            ) or []
        )
        new_formats = sorted(existing | self._formats)
        self._checkpoint.mark_downloaded(
            record.court, record.year, record.number, new_formats,
            html_pending_ts=None,
        )
        return "newly_captured"
