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

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from .atomic_write import atomic_write_bytes, atomic_write_text

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
    return result.stdout.decode("utf-8", errors="replace")


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
