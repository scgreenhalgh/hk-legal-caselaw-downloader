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

import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class PcpdaabEntry:
    year: int
    num: int
    filename: str
    chinese_only: bool
    anchor_text: str


_AAB_PDF_HREF_RE = re.compile(r"^files/AAB.*\.pdf$", re.IGNORECASE)
_ANCHOR_INDEX_RE = re.compile(
    r"\s*AAB\s+(?P<num>\d+)\s*[-/]\s*(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)


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


def parse_decisions_detail(html: str) -> dict[tuple[int, int], PcpdaabEntry]:
    """Parse PCPD's ``decisions_detail.html`` into ``(year, num) → entry``.

    Uses anchor text as the authoritative index (not the filename).
    """
    collector = _LinkCollector()
    collector.feed(html)

    entries: dict[tuple[int, int], PcpdaabEntry] = {}
    for href, text in collector.pairs:
        stripped = _strip_annotation(text)
        match = _ANCHOR_INDEX_RE.match(stripped)
        if match is None:
            continue
        year = int(match.group("year"))
        num = int(match.group("num"))
        filename = href.split("/")[-1]
        entries[(year, num)] = PcpdaabEntry(
            year=year,
            num=num,
            filename=filename,
            chinese_only="Chinese version only" in text,
            anchor_text=text,
        )
    return entries
