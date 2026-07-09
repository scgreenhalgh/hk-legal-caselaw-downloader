"""PCPD Administrative Appeals Board — resolver for HKLII pcpdaab.

HKLII's `pcpdaab` metadata points at `/static/en/others/pcpdaab/*.pdf`
URLs that serve the SPA HTML placeholder (2.7 KB `<!DOCTYPE html>...`)
instead of real PDFs. The real archive is at
`https://www.pcpd.org.hk/english/enforcement/decisions/decisions_detail.html`
which links to `files/AAB_*.pdf` via anchor tags whose TEXT is the
authoritative (year, num) index.

## Why not regex the filename

Filenames come in at least six flavors:
    AAB_{n}_{year}.pdf              # standard
    AAB_0{n}_{year}.pdf             # zero-padded (some years)
    AAB_{n1}_{n2}_{year}.pdf        # multi-appeal combined
    AAB_Decision_{n}_{year}_OCR.pdf # older OCR scans
    AAB_{n}_{year}_{e|E}.pdf        # e-suffix (case sensitive)
And multi-appeal PDFs cover 10+ cases (`AAB_16_17_2024.pdf` actually
holds AAB 1, 2, 5, 6, 8-11, 16 & 17 of 2024). Reading the anchor text
sidesteps every flavor and gives 100% HKLII coverage — the site's own
DOM already carries the mapping we need. See
`memory/d3-alt-source-research.md`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import httpx

from .atomic_write import atomic_write_bytes, atomic_write_text


PCPD_DECISIONS_URL = (
    "https://www.pcpd.org.hk/english/enforcement/decisions/decisions_detail.html"
)
PCPD_FILES_URL_TEMPLATE = (
    "https://www.pcpd.org.hk/{lang_prefix}/enforcement/decisions/files/{filename}"
)
# HKLII's `en` / `tc` naming → PCPD's URL path prefix. PCPD only
# publishes /english/ and /tc_chi/ — SC is not on their site.
_PCPD_LANG_URL_PREFIX = {"en": "english", "tc": "tc_chi"}


class PcpdaabFetchError(RuntimeError):
    """Any wire-shape failure fetching PCPD's decisions index."""


@dataclass(frozen=True)
class PcpdaabEntry:
    year: int
    num: int
    filename: str
    chinese_only: bool
    anchor_text: str
    shares_pdf_with: tuple[tuple[int, int], ...] = ()


_AAB_PDF_HREF_RE = re.compile(r"^files/AAB.*\.pdf$", re.IGNORECASE)
# Split points between multiple "AAB N-YEAR" clauses (lookahead for "AAB"
# preserves the "AAB" prefix on each side).
_CLAUSE_SPLIT_RE = re.compile(r"\s*&\s*(?=AAB\b)", re.IGNORECASE)
# One clause: "AAB {num-list}[-/]YYYY".
_CLAUSE_RE = re.compile(
    r"\s*AAB\s+(?P<nums>.*?)\s*[-/]\s*(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)
_NUM_TOKEN_SPLIT_RE = re.compile(r"[,&]")
_NUM_RANGE_RE = re.compile(r"^(?P<lo>\d+)\s*-\s*(?P<hi>\d+)$")
_NUM_SINGLE_RE = re.compile(r"^\d+$")


class _LinkCollector(HTMLParser):
    """Collect ``(href, anchor_text)`` pairs for AAB-PDF links only.

    Ignores every anchor whose href doesn't match ``files/AAB*.pdf`` —
    the page has non-decision links (nav, css, index anchors) we don't
    care about. Text is joined verbatim so the caller can inspect
    trailing markers like "(This decision provides Chinese version only)".
    """

    def __init__(self) -> None:
        super().__init__()
        self._active_href: str | None = None
        self._chunks: list[str] = []
        self.pairs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if _AAB_PDF_HREF_RE.match(href):
            self._active_href = href
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href is not None:
            self.pairs.append((self._active_href, "".join(self._chunks).strip()))
            self._active_href = None


def _strip_annotation(text: str) -> str:
    """Remove parenthetical annotations (e.g. Chinese-only marker) for parsing."""
    return re.sub(r"\([^)]*\)", "", text).strip()


def _parse_num_list(num_part: str) -> list[int]:
    """Parse "1, 2, 5, 6, 8-11, 16 & 17" into [1, 2, 5, 6, 8, 9, 10, 11, 16, 17]."""
    nums: list[int] = []
    for token in _NUM_TOKEN_SPLIT_RE.split(num_part):
        token = token.strip()
        if not token:
            continue
        range_m = _NUM_RANGE_RE.match(token)
        if range_m is not None:
            lo, hi = int(range_m.group("lo")), int(range_m.group("hi"))
            nums.extend(range(lo, hi + 1))
            continue
        if _NUM_SINGLE_RE.match(token):
            nums.append(int(token))
    return nums


def _pairs_from_anchor_text(text: str) -> list[tuple[int, int]]:
    """Return every (year, num) covered by this anchor.

    Handles single "AAB 232-2013", "&"-joined "AAB 5-2021 & AAB 6-2021",
    and compound lists "AAB 1, 2, 5, 6, 8-11, 16 & 17/2024" uniformly.
    Range hyphens (``8-11``) are disambiguated from the num-year
    separator by clause-scoped parsing — the ``& AAB``-lookahead split
    prevents ``5-2021 & AAB 6-2021`` from being consumed as a
    range-spanning-to-2021.
    """
    stripped = _strip_annotation(text)
    pairs: list[tuple[int, int]] = []
    for clause in _CLAUSE_SPLIT_RE.split(stripped):
        match = _CLAUSE_RE.match(clause)
        if match is None:
            continue
        year = int(match.group("year"))
        for num in _parse_num_list(match.group("nums")):
            pairs.append((year, num))
    return pairs


# Individual AAB decisions can be 30-70 MB (they're scanned image PDFs
# for older years). The pool's default 30s timeout would abort those
# mid-download. Give the PDF fetch a longer ceiling.
_PDF_FETCH_TIMEOUT_SEC = 180


async def _fetch_pcpdaab_pdf_one(
    get: Callable, url: str, filename: str,
) -> bytes:
    """One-shot GET + %PDF magic validation. Wraps transport errors."""
    try:
        resp = await get(url, timeout=_PDF_FETCH_TIMEOUT_SEC)
    except (httpx.RequestError, OSError) as exc:
        raise PcpdaabFetchError(
            f"transport error fetching {filename}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise PcpdaabFetchError(
            f"HTTP {resp.status_code} for {filename}"
        )
    body = resp.content
    if body[:4] != b"%PDF":
        raise PcpdaabFetchError(
            f"body missing %PDF magic for {filename} "
            f"(first 4 bytes: {body[:4]!r}, "
            f"content-type: {resp.headers.get('content-type', '?')})"
        )
    return body


async def fetch_pcpdaab_pdf(
    get: Callable, filename: str, lang: str = "en",
) -> bytes:
    """Fetch a single PCPD PDF via the pool.

    ``lang`` selects the URL path prefix (``/english/`` vs ``/tc_chi/``).
    PCPD serves the same AAB PDFs under both paths — bytes are usually
    identical but not guaranteed, so we fetch both lanes when the
    HKLII corpus asks for both.

    A subset of filenames (the ``_e`` / ``_E`` suffix variants —
    empirically ~3 of 429) are only published under ``/english/``.
    When the TC lane 404s, we transparently fall back to ``/english/``
    because the PDF is bilingual regardless of URL path. The EN lane
    never falls back (a 404 there is a genuine "no such filename").

    Validates the %PDF magic prefix (same defensive posture as the C3
    fix in :mod:`hklii_downloader.d3`). Non-200 or non-PDF bodies are
    wrapped in :class:`PcpdaabFetchError`.
    """
    lang_prefix = _PCPD_LANG_URL_PREFIX.get(lang, "english")
    primary_url = PCPD_FILES_URL_TEMPLATE.format(
        lang_prefix=lang_prefix, filename=filename,
    )
    try:
        return await _fetch_pcpdaab_pdf_one(get, primary_url, filename)
    except PcpdaabFetchError as exc:
        # Only fall back for /tc_chi/ 404s — the PDF is bilingual so
        # /english/ carries the same content.
        if lang_prefix != "tc_chi" or " 404 " not in f" {exc} ":
            raise
    fallback_url = PCPD_FILES_URL_TEMPLATE.format(
        lang_prefix="english", filename=filename,
    )
    return await _fetch_pcpdaab_pdf_one(get, fallback_url, filename)


def save_pcpdaab_local(
    output_dir: Path,
    year: int,
    num: int,
    lang: str,
    entry: PcpdaabEntry,
    hklii_metadata: dict,
    pdf_bytes: bytes,
    extracted_text: str | None = None,
) -> list[str]:
    """Write PDF binary + merged metadata JSON + optional text sidecar.

    Layout: ``output/d3/pcpdaab/{year}/{num}/pcpdaab_{year}_{num}_{lang}.{pdf,json[,txt]}``.

    ``year`` / ``num`` come from the HKLII row (not the entry) so the
    on-disk layout stays addressable by HKLII's identifiers even when
    the PCPD entry uses different numbers (the three 2013 truncation
    cases where HKLII path=32 but the real PCPD case is 232).

    ``extracted_text`` is written to ``{stem}.txt`` when non-None,
    matching :func:`hklii_downloader.d3.save_d3_pdf`'s contract so
    downstream FTS/RAG can grep the whole corpus uniformly. Row still
    counts as ``downloaded`` if extraction failed (PDF is source of
    truth).
    """
    base = (
        Path(output_dir) / "d3" / "pcpdaab" / str(year) / str(num)
    )
    base.mkdir(parents=True, exist_ok=True)
    stem = f"pcpdaab_{year}_{num}_{lang}"

    merged = {
        "hklii": hklii_metadata,
        "pcpd": {
            "filename": entry.filename,
            "anchor_text": entry.anchor_text,
            "chinese_only": entry.chinese_only,
            "shares_pdf_with": [list(p) for p in entry.shares_pdf_with],
            "resolved_year": entry.year,
            "resolved_num": entry.num,
        },
    }
    atomic_write_text(
        base / f"{stem}.json",
        json.dumps(merged, ensure_ascii=False, indent=2),
    )
    atomic_write_bytes(base / f"{stem}.pdf", pdf_bytes)
    formats = ["json", "pdf"]
    if extracted_text is not None:
        atomic_write_text(base / f"{stem}.txt", extracted_text)
        formats.append("txt")
    return formats


async def fetch_discovery(
    get: Callable,
) -> dict[tuple[int, int], PcpdaabEntry]:
    """Fetch PCPD's ``decisions_detail.html`` via ``get`` and parse it.

    ``get`` is the ProxyPool's ``async get(url) -> httpx.Response`` —
    that keeps every wire request routed through the 20-proxy VPN pool
    per the standing rule (never direct curl against pcpd.org.hk).

    Transport errors and non-200 responses are wrapped in
    :class:`PcpdaabFetchError` so the caller only has to except one
    exception type.
    """
    try:
        resp = await get(PCPD_DECISIONS_URL)
    except (httpx.RequestError, OSError) as exc:
        raise PcpdaabFetchError(
            f"transport error fetching decisions_detail: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise PcpdaabFetchError(
            f"decisions_detail HTTP {resp.status_code}"
        )
    return parse_decisions_detail(resp.text)


def parse_decisions_detail(html: str) -> dict[tuple[int, int], PcpdaabEntry]:
    """Parse PCPD's ``decisions_detail.html`` into ``(year, num) → entry``.

    Uses anchor text as the authoritative index (not the filename).
    Multi-num anchors expand into one entry per (year, num); each entry
    records the other partners via :attr:`PcpdaabEntry.shares_pdf_with`
    so downstream can dedupe wire fetches (all partners share the PDF).
    """
    collector = _LinkCollector()
    collector.feed(html)

    entries: dict[tuple[int, int], PcpdaabEntry] = {}
    for href, text in collector.pairs:
        pairs = _pairs_from_anchor_text(text)
        if not pairs:
            continue
        filename = href.split("/")[-1]
        chinese_only = "Chinese version only" in text
        for year, num in pairs:
            partners = tuple(sorted(p for p in pairs if p != (year, num)))
            entries[(year, num)] = PcpdaabEntry(
                year=year,
                num=num,
                filename=filename,
                chinese_only=chinese_only,
                anchor_text=text,
                shares_pdf_with=partners,
            )
    return entries
