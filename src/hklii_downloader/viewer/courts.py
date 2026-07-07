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
    "hkcfa", "hkca",
    "hkcfi", "hkdc", "hkmagc", "hkfc",
    "hkldt", "hklat", "hkct", "hksct", "hkcrc", "hkoat",
)
# UKPC (UK Privy Council) removed 2026-07-08 in coordination with
# cli.ALL_COURTS. HKLII's ukpc slug is currently empty, no rows or
# citations reference it. See cli.py for the fuller explanation.


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


#: BCP-47 mapping from internal lang codes to the tags emitted on
#: ``<html>`` / ``<article>``. Design §9 line 262 pins ``'tc' → 'zh-Hant'``
#: because the CJK font stack in ``app.css`` is targeted by the
#: ``:lang(zh-Hant)`` selector — the script tag, not the region tag.
#: Design §5 lines 120-121 treats legacy ``'zh'`` as an alias for
#: ``'tc'`` (the checkpoint's pre-rename Traditional-Chinese label), so
#: both codes must produce the same script tag.
_BCP47_MAP: dict[str, str] = {
    "en": "en",
    "tc": "zh-Hant",
    "zh": "zh-Hant",  # legacy alias — see §5 discriminator rules
}


def bcp47(lang: str) -> str:
    """Return the BCP-47 tag for internal lang code ``lang``.

    ``'en' → 'en'``, ``'tc' → 'zh-Hant'``, ``'zh' → 'zh-Hant'`` (legacy).
    Any other input (including ``''``, region tags like ``'zh-CN'``, or
    an unexpected checkpoint value) falls back to ``'en'`` so the
    template always emits a valid ``lang`` attribute. See design §9
    line 262 + §5 lines 120-121.

    The fallback is a defence: HTML with an invalid ``lang=""`` fails
    validators and confuses screen readers. In production the
    discriminator only ever passes canonical codes; the fallback is
    load-bearing only under drift.
    """
    return _BCP47_MAP.get(lang, "en")


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
