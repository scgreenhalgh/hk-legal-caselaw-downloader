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

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

_log = logging.getLogger("hklii_downloader.d3")

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 300

_PATH_RE = re.compile(
    r"^/(?:en|tc)/(?:legis|other)/[a-z]+/(nd|\d{4})/(\d+)/?"
)


@dataclass(frozen=True)
class D3Family:
    slug: str
    dbcat: str
    fetch_endpoint: str
    wire_abbr: str
    content_format: str


D3_FAMILIES: tuple[D3Family, ...] = (
    D3Family("histlaw", "H", "gethistlaw", "hkhistlaws", "pdf"),
    D3Family("hkiac", "O", "getother", "hkiac", "pdf"),
    D3Family("hklrccp", "O", "getother", "hklrccp", "html"),
    D3Family("hklrcr", "O", "getother", "hklrcr", "html"),
    D3Family("pcpdaab", "O", "getother", "pcpdaab", "pdf"),
    D3Family("pcpdc", "O", "getother", "pcpdc", "html"),
)

D3_LANGS: tuple[str, ...] = ("en", "tc", "sc")


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


def pdf_url(family: D3Family, response: dict) -> str | None:
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
    """
    raw = response.get("pdf")
    if not raw:
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
