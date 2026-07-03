from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

_log = logging.getLogger("hklii_downloader.scraper")

from .checkpoint import CheckpointDB, CaseRecord
from .client import Judgment, parse_judgment_response, save_judgment_local
from .enrichment import (
    enrich_appeal_history_for_case,
    enrich_summaries_for_case,
)
from .enumerator import enumerate_court
from .parser import HKLIICase
from .proxy_pool import IPLeakError

_PERMANENT_ERRORS = {404, 410}
_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}
_BODY_PREVIEW_LEN = 200


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
        with_summaries: bool = False,
        with_appeal_history: bool = False,
        _backoff_base: float = 1.0,
    ):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._formats = formats if formats is not None else {"html", "txt", "json"}
        self._workers = workers
        self._max_retries = max_retries
        self._limit = limit
        self._with_summaries = with_summaries
        self._with_appeal_history = with_appeal_history
        self._backoff_base = _backoff_base

    async def enumerate(
        self, courts: list[str], langs: tuple[str, ...] = ("en", "tc"),
    ) -> int:
        import random
        import time
        run_ts = int(time.time())
        rng = random.Random()
        seen: set[tuple[str, int, int]] = set()
        for court in courts:
            for lang in langs:
                # itemsPerPage jittered per (court, lang) into the
                # 20-50 range: matches typical "power user browsing"
                # UI defaults across large legal databases, avoids the
                # 10000-per-page scraper tell.
                page_size = rng.randint(20, 50)
                entries = await enumerate_court(
                    court, self._get, lang=lang, items_per_page=page_size,
                )
                for entry in entries:
                    self._checkpoint.upsert_case(
                        entry.court, entry.year, entry.number,
                        entry.neutral, entry.title, entry.date,
                        lang=lang, last_seen_at=run_ts,
                    )
                    seen.add((entry.court, entry.year, entry.number))
        return len(seen)

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

                try:
                    success = await self._download_one(record)
                except Exception:
                    # Belt-and-braces: _download_one catches known errors
                    # already; this guard prevents an unforeseen bug from
                    # cancelling sibling workers via asyncio.gather.
                    success = False
                async with counter_lock:
                    if success:
                        stats["downloaded"] += 1
                    else:
                        stats["failed"] += 1
                    if on_progress is not None:
                        on_progress(stats)

        await asyncio.gather(
            *[worker() for _ in range(self._workers)],
            return_exceptions=True,
        )
        return ScrapeResult(
            downloaded=stats["downloaded"], failed=stats["failed"],
        )

    async def _download_one(self, record: CaseRecord) -> bool:
        try:
            return await self._download_one_impl(record)
        except IPLeakError as e:
            _log.warning(
                "IPLeakError on %s/%s/%s: %s",
                record.court, record.year, record.number, e,
            )
            self._checkpoint.mark_failed(
                record.court, record.year, record.number,
                f"IPLeakError: {e}",
            )
            return False
        except OSError as e:
            _log.error(
                "OSError during save %s/%s/%s: %s",
                record.court, record.year, record.number, e,
            )
            self._checkpoint.mark_failed(
                record.court, record.year, record.number,
                f"OSError during save: {e}",
            )
            return False

    async def _download_one_impl(self, record: CaseRecord) -> bool:
        case = HKLIICase(
            lang=record.lang, court=record.court,
            year=record.year, number=record.number,
        )

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._get(case.api_url)
            except httpx.RequestError as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"{type(e).__name__} after {self._max_retries} retries: {e}",
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
                preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"HTTP {resp.status_code} after {self._max_retries} retries; body: {preview}",
                )
                return False

            try:
                data = resp.json()
            except json.JSONDecodeError:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue
                preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"JSONDecodeError after {self._max_retries} retries; "
                    f"HTTP {resp.status_code}; body: {preview}",
                )
                return False

            judgment = parse_judgment_response(case, data)
            output_dir = self._output_dir / record.court / str(record.year)

            content_ok = bool(judgment.content_html.strip())
            can_try_doc = "doc" in self._formats and judgment.doc_url

            if not content_ok and not can_try_doc:
                doc_hint = f", doc_url={judgment.doc_url}" if judgment.doc_url else ""
                self._checkpoint.mark_failed(
                    record.court, record.year, record.number,
                    f"empty-content{doc_hint}",
                )
                return False

            actually_saved: set[str] = set()
            if content_ok:
                save_judgment_local(judgment, output_dir, self._formats)
                actually_saved = set(self._formats) - {"doc"}

            if can_try_doc:
                output_dir.mkdir(parents=True, exist_ok=True)
                if await self._fetch_doc(judgment, output_dir):
                    actually_saved.add("doc")
                elif not content_ok:
                    # Empty content AND doc fetch failed — nothing on disk
                    self._checkpoint.mark_failed(
                        record.court, record.year, record.number,
                        f"empty-content, doc-fetch-failed, doc_url={judgment.doc_url}",
                    )
                    return False

            self._checkpoint.mark_downloaded(
                record.court, record.year, record.number,
                sorted(actually_saved),
            )

            if self._with_summaries:
                await self._enrich_summaries(record, judgment, output_dir)
            if self._with_appeal_history:
                await self._enrich_appeal_history(record, judgment, output_dir)

            return True

        return False

    async def _fetch_doc(self, judgment: Judgment, output_dir: Path) -> bool:
        from .atomic_write import atomic_write_bytes
        try:
            resp = await self._get(judgment.doc_url)
            if resp.status_code != 200:
                return False
            ext = ".docx" if judgment.doc_url.lower().endswith(".docx") else ".doc"
            path = output_dir / f"{judgment.case.filename_stem}{ext}"
            atomic_write_bytes(path, resp.content)
            return True
        except (httpx.RequestError, OSError):
            return False

    async def _enrich_summaries(
        self, record: CaseRecord, judgment: Judgment, output_dir: Path,
    ) -> None:
        await enrich_summaries_for_case(
            self._get, self._checkpoint,
            record.court, record.year, record.number,
            judgment.case.filename_stem, output_dir, judgment.content_html,
        )

    async def _enrich_appeal_history(
        self, record: CaseRecord, judgment: Judgment, output_dir: Path,
    ) -> None:
        await enrich_appeal_history_for_case(
            self._get, self._checkpoint,
            record.court, record.year, record.number,
            judgment.case.filename_stem, output_dir, judgment.case_number,
        )
