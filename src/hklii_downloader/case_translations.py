"""Case translation backfill — fetch TC counterparts for EN judgments
whose has_translation=True but whose {stem}.tc.html sidecar was never
downloaded.

Original scrape used --lang both with EN-wins-for-bilingual semantics,
so ~590 of 118,188 EN-scraped cases lost their TC translation. This
module walks disk, reads each JSON's has_translation flag, and fills
the gap by calling getjudgment?lang=tc directly.

Idempotent — the sidecar-existence check makes re-runs skip what's
already there. No DB migration; state lives on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx

from .atomic_write import atomic_write_text
from .client import Judgment, parse_judgment_response
from .content_shape import _looks_like_challenge_page
from .parser import HKLIICase, html_to_text

_log = logging.getLogger("hklii_downloader.case_translations")

# Match {court}/{year}/{stem}.json where stem is {court}_{year}_{number}
# — the primary judgment metadata file. Sidecar suffixes must be excluded
# (.appeal_history.json, .summary_*.html, .tc.json).
_PRIMARY_JSON = re.compile(r"^([a-z]+)_(\d{4})_(\d+)\.json$")
_SLUG_DIR = re.compile(r"^hk[a-z]+$")


@dataclass
class TranslationTarget:
    court: str
    year: int
    number: int


@dataclass
class TranslationResult:
    downloaded: int = 0
    failed: int = 0


def find_translation_targets(
    output_dir: Path,
) -> Iterable[TranslationTarget]:
    """Yield (court, year, number) for every EN-scraped judgment whose
    JSON says has_translation=True and whose {stem}.tc.html sidecar
    doesn't exist yet.

    Skips malformed JSON files, non-slug directories, and sidecar JSONs
    (.appeal_history.json, .tc.json)."""
    root = Path(output_dir)
    if not root.exists():
        return
    for slug in root.iterdir():
        if not slug.is_dir() or not _SLUG_DIR.match(slug.name):
            continue
        for year_dir in slug.iterdir():
            if not year_dir.is_dir():
                continue
            for f in year_dir.iterdir():
                if not f.is_file():
                    continue
                m = _PRIMARY_JSON.match(f.name)
                if not m:
                    continue
                court, year, num = m.group(1), int(m.group(2)), int(m.group(3))
                try:
                    doc = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if not doc.get("has_translation"):
                    continue
                stem = f"{court}_{year}_{num}"
                if (year_dir / f"{stem}.tc.html").exists():
                    continue
                yield TranslationTarget(court=court, year=year, number=num)


def save_translation_local(
    judgment: Judgment, output_dir: Path,
) -> list[str]:
    """Write {stem}.tc.html, .tc.txt, .tc.json for a TC judgment. Returns
    the list of suffixes saved."""
    stem = judgment.case.filename_stem
    d = Path(output_dir)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / f"{stem}.tc.html", judgment.content_html)
    atomic_write_text(d / f"{stem}.tc.txt", html_to_text(judgment.content_html))
    meta = {
        "title": judgment.title,
        "case_number": judgment.case_number,
        "court": judgment.court_name,
        "date": judgment.date,
        "neutral_citation": judgment.neutral_citation,
        "parallel_citations": judgment.parallel_citations,
        "doc_url": judgment.doc_url,
        "has_translation": judgment.has_translation,
        "url": (
            f"https://www.hklii.hk/tc/cases/"
            f"{judgment.case.court}/{judgment.case.year}/"
            f"{judgment.case.number}"
        ),
    }
    atomic_write_text(
        d / f"{stem}.tc.json",
        json.dumps(meta, indent=2, ensure_ascii=False),
    )
    return [".tc.html", ".tc.txt", ".tc.json"]


class CaseTranslationRunner:
    """Enumerates has_translation=True judgments then fetches each
    through the async pool, saving TC sidecars alongside the EN
    files."""

    def __init__(
        self,
        get: Callable,
        output_dir: Path,
        workers: int = 4,
        limit: int | None = None,
    ) -> None:
        self._get = get
        self._output_dir = Path(output_dir)
        self._workers = max(1, workers)
        self._limit = limit

    async def run(
        self,
        on_progress: Callable[[TranslationResult], None] | None = None,
    ) -> TranslationResult:
        targets = list(find_translation_targets(self._output_dir))
        if self._limit is not None:
            targets = targets[: self._limit]

        result = TranslationResult()
        counter_lock = asyncio.Lock()
        queue = asyncio.Queue()
        for t in targets:
            queue.put_nowait(t)

        async def worker() -> None:
            while True:
                try:
                    target = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._fetch_one(target)
                    async with counter_lock:
                        result.downloaded += 1
                except _TranslationError as e:
                    _log.warning(
                        "translation fetch failed for %s/%s/%s: %s",
                        target.court, target.year, target.number, e,
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as e:
                    _log.warning(
                        "translation transport failure for %s/%s/%s: "
                        "%s: %s",
                        target.court, target.year, target.number,
                        type(e).__name__, e,
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return result

    async def _fetch_one(self, target: TranslationTarget) -> None:
        case = HKLIICase(
            lang="tc", court=target.court,
            year=target.year, number=target.number,
        )
        resp = await self._get(case.api_url)
        if resp.status_code != 200:
            raise _TranslationError(
                f"getjudgment HTTP {resp.status_code}"
            )
        try:
            data = resp.json()
        except Exception as e:
            raise _TranslationError(
                f"non-JSON body: {type(e).__name__}: {e}"
            ) from e
        judgment = parse_judgment_response(case, data)
        if _looks_like_challenge_page(judgment.content_html):
            raise _TranslationError("challenge-page detected in TC content")
        if not judgment.content_html.strip():
            raise _TranslationError("empty TC content")
        case_dir = self._output_dir / target.court / str(target.year)
        save_translation_local(judgment, case_dir)


class _TranslationError(RuntimeError):
    """Internal — recoverable per-row failure classification."""
