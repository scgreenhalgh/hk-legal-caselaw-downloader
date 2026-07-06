"""HOPT scraper — HKLII "Historical / Other Publications and Treaties".

Covers 5 databases whose entries are single-fetch documents (no
versions, no TOC — one gettreaty call yields the full text):

  bacpg   Basic Law Consultation Papers        23 docs
  bahkg   Basic Law HK Gazette                 218
  hktmc   HK Treaties — Marine Codes           8
  hktml   HK Treaties — Multilateral           61
  hkts    HK Treaty Series                     266

Wire flow:

  gethoptfiles?dbcat=other&abbr=<abbr>&lang=<en|tc>&itemsPerPage=&page=&sort=
    → {totalfiles, files: [{title, path, neutral, date}]}
    path shape: /en/legis/{spa_route}/{year}/{num}/

  gettreaty?lang=<en|tc>&abbr=<wire_abbr>&year=<Y>&num=<N>
    → full document JSON: {db, title, date, neutral, category,
                            body, content (HTML), has_translation,
                            inforce}

The `bacpg` and `bahkg` SPA routes both use the wire abbr `hktba` on
the backend (they're the same DB split into two SPA views). Other
abbrs pass through unchanged. See wire_abbr().

On disk:
  output/hopt/{abbr}/{year}/{num}/{stem}.json
where stem = {abbr}_{year}_{num}_{lang}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode

import httpx

from .atomic_write import atomic_write_text

_log = logging.getLogger("hklii_downloader.hopt")

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 300

HOPT_ABBRS = ("bacpg", "bahkg", "hktmc", "hktml", "hkts")
HOPT_LANGS = ("en", "tc")

# Both `ba*` SPA routes share the same wire DB (hktba). Other abbrs
# pass through. Derived from chunk-c.js's get_treaty:
#   e = this.$route.name.startsWith("ba") ? "hktba" : this.$route.name
_WIRE_ABBR_MAP = {
    "bacpg": "hktba",
    "bahkg": "hktba",
}

# Match /en|tc/legis/<abbr>/<year>/<num>/... — extracts (year, num).
# HKLII uses `nd` ("No Date") for 10 old treaties whose promulgation
# date isn't tracked (e.g. AGREEMENT ESTABLISHING THE INTER-AMERICAN
# DEVELOPMENT BANK, AS AMENDED). Accept it as a valid year token.
_PATH_RE = re.compile(r"^/(?:en|tc)/legis/[a-z]+/(nd|\d{4})/(\d+)/?")


class HoptFetchError(RuntimeError):
    """Wire failure (non-200, non-JSON body, etc.)."""


@dataclass
class HoptEntry:
    year: str | int   # 4-digit str for normal treaties; "nd" for the
                      # 10 "no date" ones. Callers should treat as str.
    num: int
    title: str
    neutral: str | None = None
    date: str | None = None


@dataclass
class HoptListing:
    total: int
    entries: list[HoptEntry] = field(default_factory=list)


@dataclass
class HoptRunResult:
    downloaded: int = 0
    failed: int = 0


def wire_abbr(abbr: str) -> str:
    """SPA route abbr → wire abbr for gettreaty."""
    return _WIRE_ABBR_MAP.get(abbr, abbr)


def gethoptfiles_url(
    abbr: str, lang: str, page: int, items_per_page: int,
    sort: str = "-date",
) -> str:
    qs = urlencode({
        "dbcat": "other",
        "abbr": abbr,
        "lang": lang,
        "itemsPerPage": items_per_page,
        "page": page,
        "sort": sort,
    })
    return f"{_BASE_URL}/api/gethoptfiles?{qs}"


def gettreaty_url(abbr: str, year: int, num: int, lang: str) -> str:
    qs = urlencode({
        "lang": lang,
        "abbr": wire_abbr(abbr),
        "year": year,
        "num": num,
    })
    return f"{_BASE_URL}/api/gettreaty?{qs}"


def parse_hopt_files_response(body: dict) -> HoptListing:
    total = body.get("totalfiles", 0)
    entries: list[HoptEntry] = []
    for f in body.get("files", []):
        path = f.get("path") or ""
        m = _PATH_RE.match(path)
        if not m:
            continue
        year_raw = m.group(1)
        year: str | int = year_raw if year_raw == "nd" else int(year_raw)
        num = int(m.group(2))
        entries.append(HoptEntry(
            year=year, num=num,
            title=f.get("title", ""),
            neutral=f.get("neutral"),
            date=f.get("date"),
        ))
    return HoptListing(total=total, entries=entries)


def save_hopt_local(
    output_dir: Path,
    abbr: str, year: int, num: int, lang: str, doc: dict,
) -> list[str]:
    stem = f"{abbr}_{year}_{num}_{lang}"
    base = Path(output_dir) / "hopt" / abbr / str(year) / str(num)
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        base / f"{stem}.json",
        json.dumps(doc, ensure_ascii=False, indent=2),
    )
    return ["json"]


async def enumerate_hopt_pages(
    get: Callable, abbr: str, lang: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> Iterable[HoptEntry]:
    page = 1
    seen = 0
    total: int | None = None
    while True:
        url = gethoptfiles_url(
            abbr=abbr, lang=lang, page=page, items_per_page=page_size,
        )
        resp = await get(url)
        if resp.status_code != 200:
            raise HoptFetchError(
                f"gethoptfiles HTTP {resp.status_code} "
                f"for {abbr} lang={lang} page={page}"
            )
        parsed = parse_hopt_files_response(resp.json())
        if total is None:
            total = parsed.total
        for entry in parsed.entries:
            yield entry
            seen += 1
        if not parsed.entries or seen >= total:
            return
        page += 1


async def fetch_hopt_document(
    get: Callable, abbr: str, year: int, num: int, lang: str,
) -> dict:
    url = gettreaty_url(abbr=abbr, year=year, num=num, lang=lang)
    resp = await get(url)
    if resp.status_code != 200:
        raise HoptFetchError(
            f"gettreaty HTTP {resp.status_code} for "
            f"{abbr}/{year}/{num} ({lang})"
        )
    try:
        return resp.json()
    except Exception as e:
        raise HoptFetchError(
            f"gettreaty non-JSON body for {abbr}/{year}/{num} "
            f"({lang}): {type(e).__name__}: {e}"
        ) from e


class HoptRunner:
    """Two-phase runner: enumerate_all upserts pending rows,
    fetch_pending drains them through N async workers."""

    def __init__(
        self,
        get: Callable,
        checkpoint,
        output_dir: Path,
        abbrs: tuple[str, ...] = HOPT_ABBRS,
        langs: tuple[str, ...] = HOPT_LANGS,
        workers: int = 4,
        limit: int | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._abbrs = abbrs
        self._langs = langs
        self._workers = max(1, workers)
        self._limit = limit

    async def enumerate_all(self) -> int:
        upserted = 0
        now = int(time.time())
        for abbr in self._abbrs:
            for lang in self._langs:
                _log.info("enumerating hopt abbr=%s lang=%s", abbr, lang)
                async for entry in enumerate_hopt_pages(
                    get=self._get, abbr=abbr, lang=lang,
                ):
                    self._checkpoint.upsert_hopt_document(
                        abbr=abbr, year=entry.year, num=entry.num,
                        lang=lang, title=entry.title,
                        neutral=entry.neutral, doc_date=entry.date,
                        last_seen_at=now,
                    )
                    upserted += 1
        return upserted

    async def fetch_pending(
        self,
        on_progress: Callable[[HoptRunResult], None] | None = None,
    ) -> HoptRunResult:
        # Recover any rows stuck at 'in_progress' from a prior worker
        # crash — otherwise they stay permanently unclaimable.
        self._checkpoint.release_in_progress_hopt()
        result = HoptRunResult(downloaded=0, failed=0)
        counter_lock = asyncio.Lock()
        remaining = {"n": self._limit if self._limit is not None else -1}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    rec = self._checkpoint.claim_pending_hopt()
                    if rec is None:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                try:
                    doc = await fetch_hopt_document(
                        get=self._get,
                        abbr=rec.abbr, year=rec.year, num=rec.num,
                        lang=rec.lang,
                    )
                    formats = save_hopt_local(
                        output_dir=self._output_dir,
                        abbr=rec.abbr, year=rec.year, num=rec.num,
                        lang=rec.lang, doc=doc,
                    )
                    self._checkpoint.mark_hopt_downloaded(
                        abbr=rec.abbr, year=rec.year, num=rec.num,
                        lang=rec.lang, formats=formats,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except HoptFetchError as e:
                    _log.warning(
                        "hopt fetch failed for %s/%s/%s (%s): %s",
                        rec.abbr, rec.year, rec.num, rec.lang, e,
                    )
                    self._checkpoint.mark_hopt_failed(
                        abbr=rec.abbr, year=rec.year, num=rec.num,
                        lang=rec.lang, error=str(e),
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as e:
                    _log.warning(
                        "hopt transport failure %s/%s/%s (%s): %s: %s",
                        rec.abbr, rec.year, rec.num, rec.lang,
                        type(e).__name__, e,
                    )
                    self._checkpoint.mark_hopt_failed(
                        abbr=rec.abbr, year=rec.year, num=rec.num,
                        lang=rec.lang,
                        error=f"{type(e).__name__}: {e}",
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return result
