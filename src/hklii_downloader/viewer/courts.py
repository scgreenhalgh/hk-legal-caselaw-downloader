"""Canonical HKLII court slugs — the viewer's copy.

Duplicated from ``hklii_downloader.cli.ALL_COURTS`` with a drift-guard
test in ``tests/test_viewer_routes_home.py``. The viewer intentionally
does not import from ``cli`` at module import time so that opening the
viewer package doesn't drag in click and the full CLI surface.

Order matters — this drives the court-tile grid order on the home page
and the court-facet order on browse/search pages: apex first, then
CFI, then lower/tribunal courts.
"""

from __future__ import annotations


CANONICAL_COURTS: tuple[str, ...] = (
    "hkcfa", "hkca", "ukpc",
    "hkcfi", "hkdc", "hkmagc", "hkfc",
    "hkldt", "hklat", "hkct", "hksct", "hkcrc", "hkoat",
)


#: Curial-precedence rank rendered as a Unicode Roman numeral.
#: Mirrors ``graph._COURT_RANK_WHEN_ELSE`` (rank 0 → Ⅰ, 1 → Ⅱ, …). HKCA and
#: UKPC are tied at rank 1 in the graph; both render as Ⅱ here — a legal-
#: minded reader reads that as 'same tier of authority', which is the
#: intended semantics (pre-1997 UKPC ≈ CFA-adjacent authority weight).
#: Unicode Roman numerals (U+2160–216B) — screen readers vocalize them,
#: and every rank column has the same character-cell width.
CURIAL_ROMAN: dict[str, str] = {
    "hkcfa":  "Ⅰ",  # Ⅰ
    "hkca":   "Ⅱ",  # Ⅱ
    "ukpc":   "Ⅱ",  # Ⅱ (tied with HKCA)
    "hkcfi":  "Ⅲ",  # Ⅲ
    "hkdc":   "Ⅳ",  # Ⅳ
    "hkmagc": "Ⅴ",  # Ⅴ
    "hkfc":   "Ⅵ",  # Ⅵ
    "hkldt":  "Ⅶ",  # Ⅶ
    "hklat":  "Ⅷ",  # Ⅷ
    "hkct":   "Ⅸ",  # Ⅸ
    "hksct":  "Ⅹ",  # Ⅹ
    "hkcrc":  "Ⅺ",  # Ⅺ
    "hkoat":  "Ⅻ",  # Ⅻ
}


#: Human-readable court names — reference materials use the abbreviations
#: (HKCFA, HKCA, HKCFI…) but the display layer benefits from the full
#: name once, then the abbreviation carries the rest of the interface.
COURT_DISPLAY_NAMES: dict[str, str] = {
    "hkcfa":  "Court of Final Appeal",
    "hkca":   "Court of Appeal",
    "ukpc":   "UK Privy Council",
    "hkcfi":  "Court of First Instance",
    "hkdc":   "District Court",
    "hkmagc": "Magistrates' Courts",
    "hkfc":   "Family Court",
    "hkldt":  "Lands Tribunal",
    "hklat":  "Labour Tribunal",
    "hkct":   "Competition Tribunal",
    "hksct":  "Small Claims Tribunal",
    "hkcrc":  "Coroner's Court",
    "hkoat":  "Obscene Articles Tribunal",
}


def curial_roman(slug: str) -> str:
    """Return the Roman-numeral rank for ``slug`` or '·' if unknown."""
    return CURIAL_ROMAN.get(slug, "·")


def court_name(slug: str) -> str:
    """Return the human-readable court name or the uppercased slug."""
    return COURT_DISPLAY_NAMES.get(slug, slug.upper())


def thousands(value: int | float | str | None) -> str:
    """Format ``value`` with comma thousands separators.

    ``None`` and empty inputs yield ``'0'`` so templates don't need
    guards. Non-numeric strings pass through unchanged — a defence
    against accidental double-formatting.
    """
    if value is None or value == "":
        return "0"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)
