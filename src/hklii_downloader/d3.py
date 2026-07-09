"""D3 scraper — HKLII Historical / "Other" / Practice Directions.

Covers the six databases that D2 enumerator probes surfaced as
populated but unmapped by any runner (task 22).

The shape mirrors :mod:`hklii_downloader.hopt`: one `gethoptfiles`
listing per (slug, lang) → per-row metadata JSON via a family-specific
fetch endpoint. Divergence from hopt: three of the six slugs return
metadata with a ``pdf`` pointer instead of embedded ``content``, and
one of those PDF pointers is external to hklii.hk. See
``docs/d3-runner-design.md`` for the two-hop fetch rationale and
wire-response shapes.

``wire_abbr`` addresses a single-slug rename: HKLII's SPA route uses
``histlaw`` while the ``gethistlaw`` endpoint expects ``hkhistlaws``.
The mapping is stored on the family record rather than a lookup table
because only histlaw needs a rewrite — a table would be single-key
and unmotivated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import urlencode

import httpx

from .atomic_write import atomic_write_bytes, atomic_write_text

_log = logging.getLogger("hklii_downloader.d3")

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 300

_PATH_RE = re.compile(
    r"^/(?:en|tc|sc)/(?:legis|other)/[a-z]+/(nd|\d{4})/(\d+)/?"
)


@dataclass(frozen=True)
class D3Family:
    slug: str
    dbcat: str
    fetch_endpoint: str
    wire_abbr: str
    content_format: str
    enabled: bool = True
    # 'hklii' (default) → two-hop fetch via HKLII's getother/gethistlaw.
    # 'pcpd'  → route through the pcpdaab resolver which fetches direct
    # PDFs from pcpd.org.hk (HKLII's /static/ URLs serve SPA HTML for
    # pcpdaab). See :mod:`hklii_downloader.pcpdaab`.
    resolver_kind: str = "hklii"


# PDF slugs disabled because HKLII's `pdf` URLs are broken:
#
# * histlaw   — /static/en/histlaw/*.pdf serves the SPA HTML placeholder
#               (200 text/html, ~2.7 kB). Real archive at HKU library
#               (oelawhk.lib.hku.hk). Data-model mismatch (edition-based
#               vs year-num); needs a resolver we haven't built.
# * hkiac     — hkiac.org restructured 2026-07-09; every URL in HKLII's
#               metadata (2001-2021 UDRP decisions) 404s, category page
#               also 404s. Provenance only unless HKIAC republishes.
#
# pcpdaab was in that bucket until 2026-07-09 when the resolver landed:
# pcpd.org.hk publishes direct PDFs at
# /english/enforcement/decisions/files/AAB_*.pdf. Discovery scrapes
# decisions_detail.html once per run; per-row fetch is a single hop.
#
# The disabled families stay in D3_FAMILIES for provenance (freshness
# ledger continues to probe and track the STALE state). ACTIVE_D3_FAMILIES
# is the runtime default at every callsite. See
# `memory/d3-live-wire-findings.md` and `d3-alt-source-research.md`.
D3_FAMILIES: tuple[D3Family, ...] = (
    D3Family("histlaw", "H", "gethistlaw", "hkhistlaws", "pdf", enabled=False),
    D3Family("hkiac", "O", "getother", "hkiac", "pdf", enabled=False),
    D3Family("hklrccp", "O", "getother", "hklrccp", "html"),
    D3Family("hklrcr", "O", "getother", "hklrcr", "html"),
    D3Family(
        "pcpdaab", "O", "getother", "pcpdaab", "pdf",
        resolver_kind="pcpd",
    ),
    D3Family("pcpdc", "O", "getother", "pcpdc", "html"),
)

ACTIVE_D3_FAMILIES: tuple[D3Family, ...] = tuple(
    f for f in D3_FAMILIES if f.enabled
)

D3_LANGS: tuple[str, ...] = ("en", "tc", "sc")


class D3FetchError(RuntimeError):
    """Wire failure (non-200, non-JSON body, etc.)."""


def wire_abbr(family: D3Family) -> str:
    return family.wire_abbr


def gethoptfiles_url(
    family: D3Family, lang: str, page: int, items_per_page: int,
    sort: str = "-date",
) -> str:
    qs = urlencode({
        "dbcat": family.dbcat,
        "abbr": family.slug,
        "lang": lang,
        "itemsPerPage": items_per_page,
        "page": page,
        "sort": sort,
    })
    return f"{_BASE_URL}/api/gethoptfiles?{qs}"


def fetch_url(family: D3Family, year: int, num: int, lang: str) -> str:
    qs = urlencode({
        "lang": lang,
        "abbr": wire_abbr(family),
        "year": year,
        "num": num,
    })
    return f"{_BASE_URL}/api/{family.fetch_endpoint}?{qs}"


@dataclass
class D3Entry:
    year: int
    num: int
    title: str
    neutral: str | None = None
    date: str | None = None


@dataclass
class D3Listing:
    total: int
    entries: list[D3Entry] = field(default_factory=list)


@dataclass
class D3RunResult:
    downloaded: int = 0
    failed: int = 0
    langs_enumerated: dict[str, set[str]] = field(default_factory=dict)


def _row_dir(output_dir: Path, family: D3Family, year: int, num: int) -> Path:
    base = Path(output_dir) / "d3" / family.slug / str(year) / str(num)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _stem(family: D3Family, year: int, num: int, lang: str) -> str:
    return f"{family.slug}_{year}_{num}_{lang}"


def save_d3_html(
    output_dir: Path, family: D3Family,
    year: int, num: int, lang: str,
    response: dict,
) -> list[str]:
    """Shape-B save — one JSON sidecar with embedded ``content`` field.

    Returns the list of formats landed. HTML slugs always produce
    ``["json"]``; the JSON body carries the full HKLII response
    verbatim so downstream tooling can grep ``id`` / ``neutral`` /
    ``date`` without re-parsing HTML.
    """
    base = _row_dir(output_dir, family, year, num)
    atomic_write_text(
        base / f"{_stem(family, year, num, lang)}.json",
        json.dumps(response, ensure_ascii=False, indent=2),
    )
    return ["json"]


_PDFTOTEXT_TIMEOUT_SEC = 30


def _try_pdftotext(pdf_bytes: bytes) -> str | None:
    """Return extracted text via the poppler `pdftotext` binary, or None.

    Fails soft on: missing binary, non-zero exit, timeout, non-UTF-8
    output that can't be re-decoded with replacement. Only positive
    outcome is a genuinely decoded string.
    """
    if shutil.which("pdftotext") is None:
        return None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            timeout=_PDFTOTEXT_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.info("d3._try_pdftotext: subprocess failed (%s)", exc)
        return None
    if result.returncode != 0:
        _log.info(
            "d3._try_pdftotext: pdftotext exited %d (%s)",
            result.returncode,
            result.stderr[:200] if result.stderr else "",
        )
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return text or None


def _try_pypdf(pdf_bytes: bytes) -> str | None:
    """Return extracted text via pypdf, or None if pypdf unavailable / fails."""
    try:
        import pypdf
    except ImportError:
        return None
    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        pages = [p.extract_text() or "" for p in reader.pages]
        text = "\n".join(pages).strip()
        return text or None
    except Exception as exc:
        _log.info("d3._try_pypdf: extraction failed (%s)", exc)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Best-effort text extraction: pdftotext preferred, pypdf fallback.

    Row status does NOT depend on this — a None return leaves the row
    `downloaded` with `formats=["json","pdf"]` and no `.txt` sidecar.
    A backfill CLI can regenerate `.txt` later once the extractor
    changes.
    """
    text = _try_pdftotext(pdf_bytes)
    if text is not None:
        return text
    return _try_pypdf(pdf_bytes)


def save_d3_pdf(
    output_dir: Path, family: D3Family,
    year: int, num: int, lang: str,
    metadata: dict, pdf_bytes: bytes,
    extracted_text: str | None,
) -> list[str]:
    """Shape-A/C save — metadata JSON + PDF binary + optional .txt sidecar.

    The metadata JSON is written verbatim so a future audit can compare
    the mirrored ``.pdf`` against the ``pdf`` field's original URL
    (important for cross-origin hkiac / pcpdaab rows). Extracted text
    is best-effort; a missing ``.txt`` sidecar does not degrade the
    row's ``downloaded`` status because the binary IS the source of
    truth.
    """
    base = _row_dir(output_dir, family, year, num)
    stem = _stem(family, year, num, lang)

    atomic_write_text(
        base / f"{stem}.json",
        json.dumps(metadata, ensure_ascii=False, indent=2),
    )
    atomic_write_bytes(base / f"{stem}.pdf", pdf_bytes)

    formats = ["json", "pdf"]
    if extracted_text is not None:
        atomic_write_text(base / f"{stem}.txt", extracted_text)
        formats.append("txt")
    return formats


async def enumerate_pages(
    get: Callable, family: D3Family, lang: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> AsyncIterator[D3Entry]:
    """Iterate over ``gethoptfiles`` pages for one (family, lang) pair.

    Yields each :class:`D3Entry` in order. Raises :class:`D3FetchError`
    on any non-200, transport error, or non-JSON body; malformed paths
    are dropped by :func:`parse_files_response` with a visible skip-log.

    Transport errors (``httpx.RequestError``, ``OSError``) and
    ``JSONDecodeError`` are wrapped so the caller's
    ``except D3FetchError`` catches every wire-shape failure — else
    a transient proxy stall or gunicorn 200-with-HTML body would abort
    the whole multi-slug enumeration.
    """
    page = 1
    seen = 0
    total: int | None = None
    while True:
        url = gethoptfiles_url(
            family, lang=lang, page=page, items_per_page=page_size,
        )
        try:
            resp = await get(url)
        except (httpx.RequestError, OSError) as exc:
            raise D3FetchError(
                f"gethoptfiles transport error for {family.slug} "
                f"lang={lang} page={page}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise D3FetchError(
                f"gethoptfiles HTTP {resp.status_code} for "
                f"{family.slug} lang={lang} page={page}"
            )
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise D3FetchError(
                f"gethoptfiles non-JSON body for {family.slug} "
                f"lang={lang} page={page}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        parsed = parse_files_response(body)
        if total is None:
            total = parsed.total
        for entry in parsed.entries:
            yield entry
            seen += 1
        if not parsed.entries or seen >= total:
            return
        page += 1


class D3Runner:
    """Two-phase runner: ``enumerate_all`` → ``fetch_pending``.

    Rows land in ``hopt_documents`` keyed by ``family.slug`` so the
    D2 freshness ledger (kind='hopt', scope=slug) auto-inherits with
    zero schema change.

    ``langs_enumerated`` records which (slug, lang) pairs completed
    enumeration without a wire error, including buckets that returned
    ``totalfiles=0`` (empty-but-read). The CLI reads this to scope
    ``mark_bucket_scraped`` so an en-only slug's TC bucket flips
    FRESH with ``local=live=0``.
    """

    def __init__(
        self,
        get: Callable,
        checkpoint,
        output_dir: Path,
        families: tuple[D3Family, ...] = ACTIVE_D3_FAMILIES,
        langs: tuple[str, ...] = D3_LANGS,
        workers: int = 4,
        limit: int | None = None,
        pcpdaab_map: dict | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._families = families
        self._langs = langs
        self._workers = max(1, workers)
        self._limit = limit
        # Discovery output for resolver_kind='pcpd' families. The CLI /
        # dispatcher populates this via `pcpdaab.fetch_discovery` before
        # calling run(). None means no pcpdaab in scope; empty dict is
        # a legitimate "no entries available" state.
        self._pcpdaab_map = pcpdaab_map or {}
        self.langs_enumerated: dict[str, set[str]] = {}

    async def enumerate_all(self) -> int:
        upserted = 0
        now = int(time.time())
        for family in self._families:
            for lang in self._langs:
                _log.info(
                    "enumerating d3 slug=%s lang=%s",
                    family.slug, lang,
                )
                try:
                    async for entry in enumerate_pages(
                        self._get, family, lang,
                    ):
                        self._checkpoint.upsert_hopt_document(
                            abbr=family.slug,
                            year=entry.year,
                            num=entry.num,
                            lang=lang,
                            title=entry.title,
                            neutral=entry.neutral,
                            doc_date=entry.date,
                            last_seen_at=now,
                        )
                        upserted += 1
                except D3FetchError as exc:
                    _log.error(
                        "d3.enumerate_all: %s/%s failed — %s",
                        family.slug, lang, exc,
                    )
                    continue
                self.langs_enumerated.setdefault(
                    family.slug, set(),
                ).add(lang)
        return upserted

    async def _process_pcpdaab_row(self, row) -> str:
        """Fetch + save one pcpdaab row via the pcpd.org.hk resolver.

        Returns ``"downloaded"`` or ``"failed"``. Marks the row in
        the checkpoint DB accordingly. Does not update the shared
        counters — the caller handles that under its lock.

        HKLII bug workaround — three entries (2013/32, 33, 34) store a
        truncated num in the path; the title reads the real num (232,
        233, 234). If ``(year, path_num)`` misses the resolver map, we
        try to parse a real num from the row's title before failing.
        """
        from .pcpdaab import (
            PcpdaabFetchError,
            fetch_pcpdaab_pdf,
            save_pcpdaab_local,
        )
        entry = self._pcpdaab_map.get((row.year, row.num))
        if entry is None:
            title_num = _parse_pcpdaab_title_num(
                getattr(row, "title", None), row.year,
            )
            if title_num is not None:
                entry = self._pcpdaab_map.get((row.year, title_num))
        if entry is None:
            error = (
                f"pcpdaab resolver map has no entry for "
                f"{row.year}/{row.num}"
            )
            _log.warning("d3 pcpdaab miss %s: %s", row, error)
            self._checkpoint.mark_hopt_failed(
                row.abbr, row.year, row.num, row.lang, error=error,
            )
            return "failed"
        try:
            pdf_bytes = await fetch_pcpdaab_pdf(self._get, entry.filename)
        except PcpdaabFetchError as exc:
            _log.warning(
                "d3 pcpdaab fetch failed for %s/%s (%s): %s",
                row.year, row.num, row.lang, exc,
            )
            self._checkpoint.mark_hopt_failed(
                row.abbr, row.year, row.num, row.lang, str(exc),
            )
            return "failed"
        hklii_metadata = {
            "title": getattr(row, "title", None),
            "neutral": getattr(row, "neutral", None),
            "date": getattr(row, "doc_date", None),
        }
        # Match save_d3_pdf: best-effort text extraction. A missing
        # sidecar does not degrade row status; the PDF is truth.
        extracted_text = extract_pdf_text(pdf_bytes)
        formats = save_pcpdaab_local(
            self._output_dir, row.year, row.num, row.lang,
            entry, hklii_metadata, pdf_bytes,
            extracted_text=extracted_text,
        )
        self._checkpoint.mark_hopt_downloaded(
            row.abbr, row.year, row.num, row.lang, formats,
        )
        return "downloaded"

    async def run(self) -> D3RunResult:
        """Enumerate all (family, lang) pairs then drain pending rows.

        Convenience for callers that don't want to compose the two
        phases themselves. ``limit`` respects the value passed to
        ``__init__``.
        """
        await self.enumerate_all()
        return await self.fetch_pending(limit=self._limit)

    async def fetch_pending(
        self,
        limit: int | None = None,
        on_progress: Callable[["D3RunResult"], None] | None = None,
    ) -> D3RunResult:
        """Drain pending rows for this runner's family set, concurrently.

        Both :meth:`CheckpointDB.release_in_progress_hopt` and
        :meth:`claim_pending_hopt` are called with an abbr filter so a
        D3 run NEVER touches HoptRunner-owned rows (bacpg / bahkg /
        hktmc / hktml / hkts) or D3 rows for slugs outside this run's
        ``--slug`` scope. Without this filter, foreign rows would be
        marked failed via the unknown-family path — a permanent
        data-loss corruption because ``upsert_hopt_document``
        preserves status on conflict.

        ``self._workers`` workers run in parallel via
        :func:`asyncio.gather`; ordering across workers is not
        preserved but every claimed row is fetched exactly once.
        """
        family_by_slug = {f.slug: f for f in self._families}
        abbr_scope = tuple(family_by_slug.keys())
        self._checkpoint.release_in_progress_hopt(abbrs=abbr_scope)
        result = D3RunResult(
            langs_enumerated={
                slug: set(langs)
                for slug, langs in self.langs_enumerated.items()
            },
        )
        # Prefer the per-call ``limit`` when passed, else fall back to
        # the runner-level default so ``run()`` still respects it.
        effective_limit = limit if limit is not None else self._limit
        remaining = {"n": effective_limit if effective_limit is not None else -1}
        counter_lock = asyncio.Lock()

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    row = self._checkpoint.claim_pending_hopt(
                        abbrs=abbr_scope,
                    )
                    if row is None:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                family = family_by_slug.get(row.abbr)
                if family is None:
                    # Defensive: the SQL scope should prevent this branch,
                    # but fail-close if it fires (schema change, race).
                    self._checkpoint.mark_hopt_failed(
                        row.abbr, row.year, row.num, row.lang,
                        error=f"unknown family for abbr={row.abbr}",
                    )
                    async with counter_lock:
                        result.failed += 1
                    continue

                if family.resolver_kind == "pcpd":
                    outcome = await self._process_pcpdaab_row(row)
                    async with counter_lock:
                        if outcome == "downloaded":
                            result.downloaded += 1
                        else:
                            result.failed += 1
                    if on_progress is not None:
                        on_progress(result)
                    continue

                try:
                    metadata, pdf_bytes = await _fetch_row(
                        self._get, family, row.year, row.num, row.lang,
                    )
                    if pdf_bytes is None:
                        formats = save_d3_html(
                            self._output_dir, family,
                            row.year, row.num, row.lang, metadata,
                        )
                    else:
                        text = extract_pdf_text(pdf_bytes)
                        formats = save_d3_pdf(
                            self._output_dir, family,
                            row.year, row.num, row.lang,
                            metadata, pdf_bytes, text,
                        )
                    self._checkpoint.mark_hopt_downloaded(
                        row.abbr, row.year, row.num, row.lang, formats,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except D3FetchError as exc:
                    _log.warning(
                        "d3 fetch failed for %s/%s/%s (%s): %s",
                        row.abbr, row.year, row.num, row.lang, exc,
                    )
                    self._checkpoint.mark_hopt_failed(
                        row.abbr, row.year, row.num, row.lang, str(exc),
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(
            *[worker() for _ in range(self._workers)]
        )
        return result


_PCPDAAB_TITLE_NUM_RE = re.compile(
    r"AAB\s+(\d+)[-_/](\d{4})", re.IGNORECASE,
)


def _parse_pcpdaab_title_num(title: str | None, year: int) -> int | None:
    """Extract the real num from a HKLII pcpdaab row's title.

    Used only to work around the three HKLII rows (2013/32, 33, 34)
    whose path num is a truncation of the AAB title num (232, 233, 234).
    Returns None if the title is missing, unparseable, or the parsed
    year doesn't match the row's path year (belt-and-suspenders).
    """
    if not title:
        return None
    match = _PCPDAAB_TITLE_NUM_RE.search(title)
    if match is None:
        return None
    if int(match.group(2)) != year:
        return None
    return int(match.group(1))


async def _fetch_row(
    get: Callable, family: D3Family,
    year: int, num: int, lang: str,
) -> tuple[dict, bytes | None]:
    """Two-hop fetch: metadata JSON, then (if shape A/C) PDF binary.

    Returns ``(metadata, None)`` for HTML slugs. Every wire-shape
    failure (transport error, non-200, non-JSON, non-PDF body) is
    converted to :class:`D3FetchError` so the caller's row-scoped
    ``except D3FetchError`` catches it and continues to the next row
    instead of aborting the whole batch.
    """
    meta_url = fetch_url(family, year, num, lang)
    try:
        resp = await get(meta_url)
    except (httpx.RequestError, OSError) as exc:
        raise D3FetchError(
            f"hop-1 transport error for {meta_url}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise D3FetchError(
            f"hop-1 HTTP {resp.status_code} for {meta_url}"
        )
    try:
        metadata = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise D3FetchError(
            f"hop-1 non-JSON body for {meta_url}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    binary_url = pdf_url(family, metadata)
    if binary_url is None:
        return metadata, None
    try:
        resp = await get(binary_url)
    except (httpx.RequestError, OSError) as exc:
        raise D3FetchError(
            f"hop-2 transport error for {binary_url}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise D3FetchError(
            f"hop-2 HTTP {resp.status_code} for {binary_url}"
        )
    body = resp.content
    if not body[:4] == b"%PDF":
        raise D3FetchError(
            f"hop-2 body missing %PDF magic for {binary_url} "
            f"(first 4 bytes: {body[:4]!r}, "
            f"content-type: {resp.headers.get('content-type', '?')})"
        )
    return metadata, body


def pdf_url(family: D3Family, response: object) -> str | None:
    """Resolve the ``pdf`` field in a fetch response into a hop-2 URL.

    - Shape A (histlaw): ``/static/en/histlaw/1964/1.pdf`` → joined to
      the HKLII base.
    - Shape C (hkiac / pcpdaab): already an absolute URL to an
      external source-org host — returned unchanged.
    - Shape B (hklrccp / hklrcr / pcpdc): no ``pdf`` field, or an
      empty string — no second hop, return ``None``.

    ``family`` is accepted for symmetry with other builders but is
    not currently needed to route — the response body carries the
    discriminator.

    Malformed response bodies (JSON ``null``, list, string, number)
    degrade to ``None`` rather than raise — a rare HKLII shape
    surprise must not crash the worker.
    """
    if not isinstance(response, dict):
        return None
    raw = response.get("pdf")
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/"):
        return f"{_BASE_URL}{raw}"
    return f"{_BASE_URL}/{raw}"


def parse_files_response(body: dict) -> D3Listing:
    """Parse a ``gethoptfiles`` JSON response into a :class:`D3Listing`.

    Malformed paths are counted and reported at INFO — the count-visible
    skip pattern from ``review-patterns`` (silent skips break coverage
    audits). ``nd`` year rows are counted the same way; the regex
    accepts them defensively but the parser drops them so
    :attr:`D3Entry.year` stays ``int``.
    """
    total = body.get("totalfiles", 0)
    entries: list[D3Entry] = []
    skipped = 0
    for f in body.get("files", []):
        path = f.get("path") or ""
        m = _PATH_RE.match(path)
        if not m:
            skipped += 1
            continue
        year_raw = m.group(1)
        if year_raw == "nd":
            skipped += 1
            continue
        entries.append(D3Entry(
            year=int(year_raw),
            num=int(m.group(2)),
            title=f.get("title", ""),
            neutral=f.get("neutral"),
            date=f.get("date"),
        ))
    if skipped:
        _log.info(
            "d3.parse_files_response: skipped %d entry/entries with "
            "malformed or unsupported path",
            skipped,
        )
    return D3Listing(total=total, entries=entries)
