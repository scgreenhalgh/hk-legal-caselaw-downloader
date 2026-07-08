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

from dataclasses import dataclass
from urllib.parse import urlencode

_BASE_URL = "https://www.hklii.hk"
_DEFAULT_PAGE_SIZE = 300


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
