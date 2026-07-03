"""Fetch + save press summaries and appeal history for downloaded judgments.

Press summary URLs come out of the judgment HTML as relative paths on
hklii.hk (e.g. `/doc/judg/html/vetted/other/en/2025/.../ES.htm`). Appeal
history is at `/api/getappealhistory?caseno={caseno}` and returns a JSON
array of related judgments across the appeal chain.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from .atomic_write import atomic_write_text

_BASE_URL = "https://www.hklii.hk"
_VALID_LANGS = ("en", "zh")


@dataclass
class EnrichmentResult:
    processed: int
    failed: int


async def fetch_press_summary(url_or_path: str, get: Callable) -> str:
    if not url_or_path.startswith("http"):
        url_or_path = _BASE_URL + url_or_path
    resp = await get(url_or_path)
    resp.raise_for_status()
    return resp.text


async def fetch_appeal_history(caseno: str, get: Callable) -> list[dict]:
    url = f"{_BASE_URL}/api/getappealhistory?caseno={quote(caseno, safe='')}"
    resp = await get(url)
    resp.raise_for_status()
    return resp.json()


def save_press_summary_local(
    html: str, output_dir: Path, stem: str, lang: str,
) -> Path:
    if lang not in _VALID_LANGS:
        raise ValueError(f"unknown lang {lang!r}; expected one of {_VALID_LANGS}")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stem}.summary_{lang}.html"
    atomic_write_text(path, html)
    return path


def save_appeal_history_local(
    data: list[dict], output_dir: Path, stem: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stem}.appeal_history.json"
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
    return path


async def enrich_summaries_for_case(
    get: Callable, checkpoint,
    court: str, year: int, number: int,
    stem: str, output_dir: Path, content_html: str,
) -> None:
    from .enumerator import extract_press_summary_urls
    urls = extract_press_summary_urls(content_html)
    for lang_label, lang_short in (("English", "en"), ("Chinese", "zh")):
        kind = f"summary_{lang_short}"
        url = urls.get(lang_label)
        if url is None:
            checkpoint.mark_enrichment(court, year, number, kind, "na")
            continue
        try:
            html = await fetch_press_summary(url, get)
            save_press_summary_local(html, output_dir, stem, lang_short)
            checkpoint.mark_enrichment(court, year, number, kind, "downloaded")
        except (httpx.RequestError, httpx.HTTPStatusError, OSError) as e:
            checkpoint.mark_enrichment(
                court, year, number, kind, "failed",
                error=f"{type(e).__name__}: {e}",
            )


async def enrich_appeal_history_for_case(
    get: Callable, checkpoint,
    court: str, year: int, number: int,
    stem: str, output_dir: Path, case_number: str,
) -> None:
    try:
        data = await fetch_appeal_history(case_number, get)
        save_appeal_history_local(data, output_dir, stem)
        checkpoint.mark_enrichment(
            court, year, number, "appeal_history", "downloaded",
        )
    except (httpx.RequestError, httpx.HTTPStatusError,
            json.JSONDecodeError, OSError) as e:
        checkpoint.mark_enrichment(
            court, year, number, "appeal_history", "failed",
            error=f"{type(e).__name__}: {e}",
        )


class EnrichmentRunner:
    """Backfill missing summaries / appeal history for already-downloaded
    judgments by re-reading the on-disk HTML + JSON files.

    Contrast with BulkScraper — this runner never fetches judgments itself;
    it operates on cases already marked status='downloaded' in the checkpoint.
    """

    def __init__(
        self,
        get: Callable,
        checkpoint,
        output_dir: Path,
        do_summaries: bool = True,
        do_appeal_history: bool = True,
        workers: int = 1,
        limit: int | None = None,
    ):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._do_summaries = do_summaries
        self._do_appeal_history = do_appeal_history
        self._workers = workers
        self._limit = limit

    async def enrich_all(
        self, on_progress: Callable[[dict], None] | None = None,
    ) -> EnrichmentResult:
        kinds: list[str] = []
        if self._do_summaries:
            kinds += ["summary_en", "summary_zh"]
        if self._do_appeal_history:
            kinds.append("appeal_history")
        if not kinds:
            return EnrichmentResult(processed=0, failed=0)

        cases = self._checkpoint.pending_any_enrichment(kinds)
        if self._limit is not None:
            cases = cases[: self._limit]

        counter_lock = asyncio.Lock()
        stats = {"processed": 0, "failed": 0}
        idx = {"i": 0}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if idx["i"] >= len(cases):
                        return
                    case = cases[idx["i"]]
                    idx["i"] += 1
                try:
                    await self._enrich_one(case)
                    async with counter_lock:
                        stats["processed"] += 1
                except Exception:
                    async with counter_lock:
                        stats["failed"] += 1
                async with counter_lock:
                    if on_progress is not None:
                        on_progress(stats)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return EnrichmentResult(
            processed=stats["processed"], failed=stats["failed"],
        )

    async def _enrich_one(self, case) -> None:
        court_dir = self._output_dir / case.court / str(case.year)
        stem = f"{case.court}_{case.year}_{case.number}"
        enrich = self._checkpoint.get_enrichment(
            case.court, case.year, case.number,
        )

        if self._do_summaries and (
            enrich["summary_en"] == "pending"
            or enrich["summary_zh"] == "pending"
        ):
            html_path = court_dir / f"{stem}.html"
            if not html_path.exists():
                for kind in ("summary_en", "summary_zh"):
                    if enrich[kind] == "pending":
                        self._checkpoint.mark_enrichment(
                            case.court, case.year, case.number, kind, "failed",
                            error="html file missing on disk",
                        )
                return
            content_html = html_path.read_text(encoding="utf-8")
            await enrich_summaries_for_case(
                self._get, self._checkpoint,
                case.court, case.year, case.number,
                stem, court_dir, content_html,
            )

        if self._do_appeal_history and enrich["appeal_history"] == "pending":
            json_path = court_dir / f"{stem}.json"
            if not json_path.exists():
                self._checkpoint.mark_enrichment(
                    case.court, case.year, case.number,
                    "appeal_history", "failed",
                    error="json sidecar missing on disk",
                )
                return
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            case_number = meta.get("case_number", "")
            if not case_number:
                self._checkpoint.mark_enrichment(
                    case.court, case.year, case.number,
                    "appeal_history", "failed",
                    error="case_number missing in json sidecar",
                )
                return
            await enrich_appeal_history_for_case(
                self._get, self._checkpoint,
                case.court, case.year, case.number,
                stem, court_dir, case_number,
            )
