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
