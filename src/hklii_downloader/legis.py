"""Legislation scraper — HK ordinances, regulations, and instruments.

Parallel to scraper.py but scoped to legislation. HKLII's data model
for a piece of legislation is:

  chapter (cap)     → getcap(lang, cap, abbr) → metadata dict
    versions[]      → getcapversions(lang, cap) → newest-first list
      TOC (sections)→ getcapversiontoc(id=vid) → sections with content
                       inline as HTML

Each version's TOC is self-contained — no follow-up per-section fetch
is required. Since a real ordinance can span 100+ sections, one TOC
response can be 100s of KB. We save two artifacts per (abbr, num, lang):

  {stem}.versions.json  → the full versions list from getcapversions
  {stem}.content.json   → the TOC (with inline section HTML) for the
                          newest version — the "currently in force"
                          text

Stem: {abbr}_{num}_{lang}, e.g. `ord_1_en`. On disk:
  output/legis/{abbr}/{num}/{stem}.{versions,content}.json
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode

import httpx

from .atomic_write import atomic_write_text

_log = logging.getLogger("hklii_downloader.legis")

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 500

# Non-empty capTypes per the metadata probe (2026-07-05). The three
# with real content — bacpg/bahkg/hktml/hkts/hktmc are HOPT and use
# gethoptfiles, not getlegisfiles; those live in a follow-up.
LEGIS_CAP_TYPES = ("ord", "reg", "instrument")
LEGIS_LANGS = ("en", "tc")


class LegisFetchError(RuntimeError):
    """Wire failure (500, empty response, missing versions, etc.)."""


@dataclass
class LegisEntry:
    num: str
    title: str


@dataclass
class LegisListing:
    total: int
    entries: list[LegisEntry]


@dataclass
class LegisDocument:
    abbr: str
    num: str
    lang: str
    latest_vid: int
    latest_version_date: str
    versions: list[dict]
    content: list[dict]


def getlegisfiles_url(
    cap_type: str, lang: str, page: int, items_per_page: int,
    sort: str = "capNum",
) -> str:
    """Listing endpoint — one page of chapters for a capType.

    Params mirror the SPA's get_files() invocation from chunk-c.js:
      lang, capType, capno, title, firstLetter, numRange,
      itemsPerPage, page, sort
    We pass only the required ones for a bulk enumeration (no search
    filters).
    """
    qs = urlencode({
        "lang": lang,
        "capType": cap_type,
        "itemsPerPage": items_per_page,
        "page": page,
        "sort": sort,
    })
    return f"{_BASE_URL}/api/getlegisfiles?{qs}"


def getcapversions_url(cap: str, lang: str) -> str:
    qs = urlencode({"lang": lang, "cap": cap})
    return f"{_BASE_URL}/api/getcapversions?{qs}"


def getcapversiontoc_url(vid: int) -> str:
    return f"{_BASE_URL}/api/getcapversiontoc?id={vid}"


def parse_files_response(body: dict) -> LegisListing:
    total = body.get("totalfiles", 0)
    entries = [
        LegisEntry(num=f["num"], title=f.get("title", ""))
        for f in body.get("files", [])
    ]
    return LegisListing(total=total, entries=entries)


def pick_latest_version(versions: list[dict]) -> dict:
    """HKLII's getcapversions returns newest-first; the first entry is
    the "currently in force" one we want to capture. Raise if the
    list is empty (means the API returned no versions for a chapter,
    which we treat as a fetch error)."""
    if not versions:
        raise LegisFetchError("no versions returned by getcapversions")
    return versions[0]


def save_legis_local(
    output_dir: Path,
    abbr: str, num: str, lang: str,
    versions: list[dict],
    content: list[dict],
) -> list[str]:
    """Write the two JSON artifacts under output/legis/{abbr}/{num}/
    and return the list of format tags written."""
    stem = f"{abbr}_{num}_{lang}"
    base = Path(output_dir) / "legis" / abbr / num
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        base / f"{stem}.versions.json",
        json.dumps(versions, ensure_ascii=False, indent=2),
    )
    atomic_write_text(
        base / f"{stem}.content.json",
        json.dumps(content, ensure_ascii=False, indent=2),
    )
    return ["versions", "content"]


async def fetch_legis_document(
    get: Callable, abbr: str, num: str, lang: str,
) -> LegisDocument:
    """Fetch versions + latest TOC for one chapter through the async
    pool `get`. Any non-200 or empty-versions response raises
    LegisFetchError with a descriptive message."""
    v_resp = await get(getcapversions_url(cap=num, lang=lang))
    if v_resp.status_code != 200:
        raise LegisFetchError(
            f"getcapversions HTTP {v_resp.status_code} "
            f"for {abbr} cap {num} ({lang})"
        )
    try:
        versions = v_resp.json()
    except Exception as e:
        raise LegisFetchError(
            f"getcapversions non-JSON body for {abbr} cap {num} "
            f"({lang}): {type(e).__name__}: {e}"
        ) from e
    if not isinstance(versions, list):
        raise LegisFetchError(
            f"getcapversions returned {type(versions).__name__}, "
            f"expected list, for {abbr} cap {num} ({lang})"
        )

    latest = pick_latest_version(versions)
    vid = int(latest["id"])
    version_date = latest.get("date", "")

    toc_resp = await get(getcapversiontoc_url(vid=vid))
    if toc_resp.status_code != 200:
        raise LegisFetchError(
            f"getcapversiontoc HTTP {toc_resp.status_code} "
            f"for {abbr} cap {num} ({lang}), vid={vid}"
        )
    try:
        content = toc_resp.json()
    except Exception as e:
        raise LegisFetchError(
            f"getcapversiontoc non-JSON body for {abbr} cap {num} "
            f"({lang}), vid={vid}: {type(e).__name__}: {e}"
        ) from e

    return LegisDocument(
        abbr=abbr, num=num, lang=lang,
        latest_vid=vid, latest_version_date=version_date,
        versions=versions, content=content,
    )


async def enumerate_legis_pages(
    get: Callable, cap_type: str, lang: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> Iterable[LegisEntry]:
    """Yield every LegisEntry for (cap_type, lang) by paging through
    getlegisfiles until we've seen `totalfiles` entries."""
    page = 1
    seen = 0
    total: int | None = None
    while True:
        url = getlegisfiles_url(
            cap_type=cap_type, lang=lang, page=page,
            items_per_page=page_size,
        )
        resp = await get(url)
        if resp.status_code != 200:
            raise LegisFetchError(
                f"getlegisfiles HTTP {resp.status_code} "
                f"for capType={cap_type} lang={lang} page={page}"
            )
        parsed = parse_files_response(resp.json())
        if total is None:
            total = parsed.total
        for entry in parsed.entries:
            yield entry
            seen += 1
        if not parsed.entries or seen >= total:
            return
        page += 1


@dataclass
class LegisRunResult:
    downloaded: int
    failed: int


class LegisRunner:
    """Enumerate + fetch + persist for one or more capType/lang scopes.

    Enumeration phase upserts every discovered chapter into
    legis_documents (status=pending). Fetch phase drains
    claim_pending_legis() through N async workers, writing artifacts
    to disk and flipping rows to downloaded/failed.
    """

    def __init__(
        self,
        get: Callable,
        checkpoint,
        output_dir: Path,
        cap_types: tuple[str, ...] = LEGIS_CAP_TYPES,
        langs: tuple[str, ...] = LEGIS_LANGS,
        workers: int = 4,
        limit: int | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._cap_types = cap_types
        self._langs = langs
        self._workers = max(1, workers)
        self._limit = limit

    async def enumerate_all(self) -> int:
        """Upsert every discovered chapter into legis_documents. Returns
        the number of rows upserted this pass."""
        upserted = 0
        now = int(time.time())
        for cap_type in self._cap_types:
            for lang in self._langs:
                _log.info(
                    "enumerating legis capType=%s lang=%s", cap_type, lang,
                )
                async for entry in enumerate_legis_pages(
                    get=self._get, cap_type=cap_type, lang=lang,
                ):
                    self._checkpoint.upsert_legis_document(
                        abbr=cap_type, num=entry.num, lang=lang,
                        title=entry.title, last_seen_at=now,
                    )
                    upserted += 1
        return upserted

    async def fetch_pending(
        self,
        on_progress: Callable[[LegisRunResult], None] | None = None,
    ) -> LegisRunResult:
        result = LegisRunResult(downloaded=0, failed=0)
        counter_lock = asyncio.Lock()
        remaining = {"n": self._limit if self._limit is not None else -1}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    rec = self._checkpoint.claim_pending_legis()
                    if rec is None:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                try:
                    doc = await fetch_legis_document(
                        get=self._get,
                        abbr=rec.abbr, num=rec.num, lang=rec.lang,
                    )
                    formats = save_legis_local(
                        output_dir=self._output_dir,
                        abbr=rec.abbr, num=rec.num, lang=rec.lang,
                        versions=doc.versions, content=doc.content,
                    )
                    self._checkpoint.mark_legis_downloaded(
                        abbr=rec.abbr, num=rec.num, lang=rec.lang,
                        latest_vid=doc.latest_vid,
                        latest_version_date=doc.latest_version_date,
                        formats=formats,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except LegisFetchError as e:
                    _log.warning(
                        "legis fetch failed for %s cap %s (%s): %s",
                        rec.abbr, rec.num, rec.lang, e,
                    )
                    self._checkpoint.mark_legis_failed(
                        abbr=rec.abbr, num=rec.num, lang=rec.lang,
                        error=str(e),
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as e:
                    _log.warning(
                        "legis transport failure for %s cap %s (%s): "
                        "%s: %s", rec.abbr, rec.num, rec.lang,
                        type(e).__name__, e,
                    )
                    self._checkpoint.mark_legis_failed(
                        abbr=rec.abbr, num=rec.num, lang=rec.lang,
                        error=f"{type(e).__name__}: {e}",
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return result


class LegisHistoryRunner:
    """Stub — full impl lands in the paired feat commit (task #87)."""

    def __init__(
        self, get, checkpoint, output_dir,
        workers: int = 4, limit: int | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._workers = max(1, workers)
        self._limit = limit

    def enumerate_pending(self) -> int:
        raise NotImplementedError

    async def fetch_pending(
        self, on_progress=None,
    ) -> LegisRunResult:
        raise NotImplementedError
