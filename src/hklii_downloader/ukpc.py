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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

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


# Match the "path" field returned by ``gethoptfiles?dbcat=C``. Two
# shapes are known live (as of 2026-07-08 probe via 20-proxy pool):
#
#   /en/cases/ukpc/1997/40      ← what LIVE gethoptfiles returns
#   /1997/40/                   ← what ``getother``'s path field is
#
# The prior version pinned this to the bare shape only, which caused
# every one of the 242 UKPC entries to be silently skipped during
# ``parse_hopt_c_listing`` — Downloaded=0, Failed=0 with no error.
# Accepting both keeps the parser robust if HKLII ever swaps the two.
_PATH_RE = re.compile(
    r"^(?:/(?:en|tc)/cases/[a-z]+)?/(\d{4})/(\d+)/?$"
)


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


@dataclass
class UkpcRunResult:
    downloaded: int = 0
    failed: int = 0


class UkpcRunner:
    """Single-pass runner: enumerate → fetch → save → dual-write cases row.

    UKPC lives on the hopt-C endpoint family but its judgments belong
    in the case-family cases table so the viewer's court indexer picks
    them up unchanged. Unlike ``HoptRunner`` (which uses a two-phase
    enumerate-then-drain pattern via the ``hopt_documents`` state table),
    UKPC skips the intermediate pending state and inserts rows straight
    at status='downloaded' via
    :meth:`hklii_downloader.checkpoint.CheckpointDB.upsert_downloaded_case`.

    Why single-pass:

    * :meth:`CheckpointDB.claim_pending` is court-unscoped. If a
      UKPC row landed at status='pending', the next ``hklii scrape``
      run would pull it off the queue and hit ``getjudgment`` — WRONG
      endpoint family. Single-pass sidesteps that hazard entirely.
    * The corpus is small (242 records at the current snapshot), so a
      one-shot enumerate + fan-out fetch is cheap; no need for the
      two-phase resume tracking that hopt/legis get from a dedicated
      state table.

    Idempotent resume via :meth:`CheckpointDB.has_downloaded_case`:
    a re-run over the same corpus skips already-downloaded rows without
    re-fetching (unless ``force=True``). TC enum is best-effort — if
    UKPC/TC ever comes online (currently EN-only per /databases), the
    upsert's lang-collapse rule handles the transition.
    """

    def __init__(
        self,
        get: Callable,
        checkpoint,
        output_dir: Path,
        langs: tuple[str, ...] = HOPT_C_LANGS,
        workers: int = 4,
        limit: int | None = None,
        force: bool = False,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._langs = langs
        self._workers = max(1, workers)
        self._limit = limit
        self._force = force

    async def run(
        self,
        on_progress: Callable[[UkpcRunResult], None] | None = None,
    ) -> UkpcRunResult:
        result = UkpcRunResult()
        counter_lock = asyncio.Lock()
        remaining = {"n": self._limit if self._limit is not None else -1}

        pending: list[tuple[int, int, str, UkpcEntry]] = []
        for lang in self._langs:
            try:
                entries = await enumerate_hopt_c_court(
                    get=self._get, abbr="ukpc", lang=lang,
                )
            except UkpcFetchError as exc:
                # TC endpoint historically 500s while EN is fine; log
                # and continue rather than aborting the run.
                _log.warning(
                    "ukpc enum failed for lang=%s: %s", lang, exc,
                )
                continue
            for e in entries:
                if (
                    not self._force
                    and self._checkpoint.has_downloaded_case(
                        "ukpc", e.year, e.num,
                    )
                ):
                    continue
                pending.append((e.year, e.num, lang, e))

        pending_iter = iter(pending)

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    try:
                        year, num, lang, entry = next(pending_iter)
                    except StopIteration:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                try:
                    judgment = await fetch_one_hopt_c_judgment(
                        get=self._get, abbr="ukpc",
                        year=year, num=num, lang=lang,
                    )
                    save_ukpc_local(
                        output_dir=self._output_dir, judgment=judgment,
                    )
                    now = int(time.time())
                    self._checkpoint.upsert_downloaded_case(
                        court="ukpc", year=year, number=num, lang=lang,
                        neutral=judgment.neutral or entry.neutral,
                        title=judgment.title or entry.title,
                        date=judgment.date or entry.date,
                        formats=["html", "json", "txt"],
                        last_seen_at=now,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except UkpcFetchError as exc:
                    _log.warning(
                        "ukpc fetch failed for %s/%s (%s): %s",
                        year, num, lang, exc,
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as exc:
                    _log.warning(
                        "ukpc transport failure %s/%s (%s): %s: %s",
                        year, num, lang, type(exc).__name__, exc,
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(
            *[worker() for _ in range(self._workers)]
        )
        return result
