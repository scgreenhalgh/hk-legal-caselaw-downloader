from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .checkpoint import CheckpointDB, CaseRecord
from .client import parse_judgment_response, save_judgment_local
from .enumerator import enumerate_court
from .parser import HKLIICase

_PERMANENT_ERRORS = {404, 410}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class ScrapeResult:
    downloaded: int
    failed: int


class BulkScraper:
    def __init__(
        self,
        get: Callable,
        checkpoint: CheckpointDB,
        output_dir: Path,
        formats: set[str] | None = None,
        workers: int = 1,
        max_retries: int = 3,
        limit: int | None = None,
        _backoff_base: float = 1.0,
    ):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._formats = formats if formats is not None else {"html", "txt", "json"}
        self._workers = workers
        self._max_retries = max_retries
        self._limit = limit
        self._backoff_base = _backoff_base

    async def enumerate(self, courts: list[str]) -> int:
        total = 0
        for court in courts:
            entries = await enumerate_court(court, self._get)
            for entry in entries:
                self._checkpoint.upsert_case(
                    entry.court, entry.year, entry.number,
                    entry.neutral, entry.title, entry.date,
                )
            total += len(entries)
        return total

    async def download_all(
        self,
        on_progress: Callable[[dict], None] | None = None,
    ) -> ScrapeResult:
        self._checkpoint.release_in_progress()

        counter_lock = asyncio.Lock()
        stats = {"downloaded": 0, "failed": 0, "dispatched": 0}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if (self._limit is not None
                            and stats["dispatched"] >= self._limit):
                        return
                    record = self._checkpoint.claim_pending()
                    if record is None:
                        return
                    stats["dispatched"] += 1

                success = await self._download_one(record)
                async with counter_lock:
                    if success:
                        stats["downloaded"] += 1
                    else:
                        stats["failed"] += 1
                    if on_progress is not None:
                        on_progress(stats)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return ScrapeResult(
            downloaded=stats["downloaded"], failed=stats["failed"],
        )

    async def _download_one(self, record: CaseRecord) -> bool:
        case = HKLIICase(
            lang="en", court=record.court,
            year=record.year, number=record.number,
        )

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._get(case.api_url)
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    "Connection error after retries",
                )
                return False

            if resp.status_code in _PERMANENT_ERRORS:
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"HTTP {resp.status_code}",
                )
                return False

            if resp.status_code in _RETRYABLE_STATUSES:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"HTTP {resp.status_code} after {self._max_retries} retries",
                )
                return False

            try:
                data = resp.json()
            except json.JSONDecodeError:
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    "JSONDecodeError",
                )
                return False

            judgment = parse_judgment_response(case, data)
            output_dir = self._output_dir / record.court / str(record.year)
            save_judgment_local(judgment, output_dir, self._formats)

            self._checkpoint.mark_downloaded(
                record.court, record.year, record.number,
                list(self._formats),
            )
            return True

        return False
