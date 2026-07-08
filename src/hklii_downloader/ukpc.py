"""UKPC scraper — HKLII "hopt-C" family.

UKPC (UK Privy Council) judgments live under a different HKLII endpoint
family than the 12 case-family courts in ``cli.ALL_COURTS``:

  enumerate:  gethoptfiles?dbcat=C&abbr=ukpc&lang=<en|tc>
  fetch one:  getother?lang=<en|tc>&abbr=ukpc&year=<Y>&num=<N>
  storage:    output/ukpc/<year>/ukpc_<year>_<num>.{html,txt,json}
              (case-family layout so the viewer's render pipeline
               and cases-table code work unchanged)

Called by ``hklii update`` alongside the case-family scrape, or
standalone via ``hklii scrape-ukpc``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from .atomic_write import atomic_write_text
from .parser import html_to_text

_log = logging.getLogger("hklii_downloader.ukpc")

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 300

#: Slugs served by the ``dbcat=C`` hopt endpoint family. Currently just
#: UKPC; keep as a tuple so a future addition drops in without a
#: signature change.
HOPT_C_COURTS: tuple[str, ...] = ("ukpc",)
HOPT_C_LANGS: tuple[str, ...] = ("en", "tc")


class UkpcFetchError(RuntimeError):
    """Wire failure — non-200, non-JSON body, or empty content."""


@dataclass
class UkpcEntry:
    """One row from the gethoptfiles listing."""
    year: int
    num: int
    neutral: str
    date: str
    title: str


@dataclass
class UkpcListing:
    total: int
    entries: list[UkpcEntry] = field(default_factory=list)


@dataclass
class UkpcJudgment:
    """Parsed getother response."""
    abbr: str
    year: int
    num: int
    lang: str
    title: str
    neutral: str
    date: str
    content_html: str


# Match /<year>/<num>/ from the "path" field in gethoptfiles.
# getother's path field is e.g. "/1997/40/" (no lang or abbr prefix).
_PATH_RE = re.compile(r"^/(\d{4})/(\d+)/?$")


def gethoptfiles_c_url(
    abbr: str, lang: str, page: int, items_per_page: int,
    sort: str = "-date",
) -> str:
    """Build the enumeration URL for a hopt-C court."""
    qs = urlencode({
        "dbcat": "C",
        "abbr": abbr,
        "lang": lang.upper(),  # HKLII expects EN / TC uppercase here
        "itemsPerPage": items_per_page,
        "page": page,
        "sort": sort,
    })
    return f"{_BASE_URL}/api/gethoptfiles?{qs}"


def getother_url(abbr: str, year: int, num: int, lang: str) -> str:
    """Build the individual-fetch URL for a hopt-C judgment."""
    qs = urlencode({
        "lang": lang,
        "abbr": abbr,
        "year": year,
        "num": num,
    })
    return f"{_BASE_URL}/api/getother?{qs}"


def parse_hopt_c_listing(body: dict) -> UkpcListing:
    """Parse a gethoptfiles?dbcat=C response into structured entries.

    The response shape mirrors gethoptfiles?dbcat=other but the ``path``
    field is bare ``/<year>/<num>/`` rather than the ``/en/legis/...``
    form the hopt.py parser expects.
    """
    total = int(body.get("totalfiles", 0))
    entries: list[UkpcEntry] = []
    for f in body.get("files", []):
        raw_path = f.get("path", "") or ""
        m = _PATH_RE.match(raw_path)
        if m is None:
            _log.debug("skip unparseable hopt-C entry path: %r", raw_path)
            continue
        entries.append(UkpcEntry(
            year=int(m.group(1)),
            num=int(m.group(2)),
            neutral=f.get("neutral", ""),
            date=f.get("date", ""),
            title=f.get("title", ""),
        ))
    return UkpcListing(total=total, entries=entries)


def parse_getother_response(
    abbr: str, year: int, num: int, lang: str, body: dict,
) -> UkpcJudgment:
    """Parse a getother JSON body into a UkpcJudgment.

    Response shape (from a UKPC probe):
      {"id": 2582, "title": "...", "neutral": "[1997] UKPC 40",
       "date": "1997-07-29", "path": "/1997/40/",
       "db": {"id":5, "abbr":"ukpc", ...}, "content": "<html>..."}

    Different from the case-family ``getjudgment`` in three places:
      * ``title`` is top-level, not nested under ``cases[0]``
      * ``db`` is an object, not a string
      * No ``cases``, ``parallel_citation``, ``has_translation`` fields

    Empty ``content`` is treated as a fetch failure (upstream data bug)
    rather than a valid empty judgment — HKLII sometimes returns 200 OK
    with an empty body when their internal pipeline broke for one file.
    """
    content = body.get("content", "") or ""
    if not content.strip():
        raise UkpcFetchError(
            f"empty content for {abbr}/{year}/{num}/{lang}"
        )
    return UkpcJudgment(
        abbr=abbr,
        year=year,
        num=num,
        lang=lang,
        title=body.get("title", ""),
        neutral=body.get("neutral", ""),
        date=body.get("date", ""),
        content_html=content,
    )


def save_ukpc_local(
    output_dir: Path,
    judgment: UkpcJudgment,
    formats: set[str] | None = None,
) -> list[str]:
    """Write UKPC judgment files at the case-family layout.

    Path: ``output/ukpc/<year>/ukpc_<year>_<num>.{html,txt,json}``
    (matches the viewer's ``select_body_source`` expectations).

    A TC translation is saved with the ``.tc.`` suffix
    (``ukpc_<year>_<num>.tc.html``) so the viewer's bilingual detection
    treats UKPC identically to case-family bilingual judgments.
    """
    formats = formats or {"html", "txt", "json"}
    base = Path(output_dir) / "ukpc" / str(judgment.year)
    base.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    tc_suffix = ".tc" if judgment.lang == "tc" else ""
    stem = f"ukpc_{judgment.year}_{judgment.num}{tc_suffix}"

    if "html" in formats:
        atomic_write_text(base / f"{stem}.html", judgment.content_html)
        saved.append(f"{stem}.html")

    if "txt" in formats:
        atomic_write_text(
            base / f"{stem}.txt",
            html_to_text(judgment.content_html),
        )
        saved.append(f"{stem}.txt")

    if "json" in formats:
        meta = {
            "title": judgment.title,
            "neutral_citation": judgment.neutral,
            "date": judgment.date,
            "abbr": judgment.abbr,
            "year": judgment.year,
            "num": judgment.num,
            "lang": judgment.lang,
            "url": f"{_BASE_URL}/{judgment.lang}/cases/{judgment.abbr}/{judgment.year}/{judgment.num}",
        }
        atomic_write_text(
            base / f"{stem}.json",
            json.dumps(meta, ensure_ascii=False, indent=2),
        )
        saved.append(f"{stem}.json")

    return saved


async def enumerate_hopt_c_court(
    get: Callable,
    abbr: str,
    lang: str,
    items_per_page: int = _DEFAULT_PAGE_SIZE,
) -> list[UkpcEntry]:
    """Walk every page of a hopt-C court's listing, return every entry."""
    all_entries: list[UkpcEntry] = []
    page = 1
    while True:
        url = gethoptfiles_c_url(abbr, lang, page, items_per_page)
        resp = await get(url)
        if resp.status_code != 200:
            raise UkpcFetchError(
                f"gethoptfiles/{abbr}/{lang} page={page} → HTTP {resp.status_code}"
            )
        listing = parse_hopt_c_listing(resp.json())
        all_entries.extend(listing.entries)
        if len(all_entries) >= listing.total or not listing.entries:
            break
        page += 1
    return all_entries


async def fetch_one_hopt_c_judgment(
    get: Callable,
    abbr: str,
    year: int,
    num: int,
    lang: str,
) -> UkpcJudgment:
    """Fetch a single hopt-C judgment via ``getother`` and parse it."""
    url = getother_url(abbr, year, num, lang)
    resp = await get(url)
    if resp.status_code != 200:
        raise UkpcFetchError(
            f"getother/{abbr}/{year}/{num}/{lang} → HTTP {resp.status_code}"
        )
    return parse_getother_response(abbr, year, num, lang, resp.json())
