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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

from .atomic_write import atomic_write_text


_BASE_URL = "https://www.hklii.hk"


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
