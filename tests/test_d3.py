"""Tests for D3 (Historical Laws / Other Publications / Practice Directions).

Covers the 6 unmapped slugs surfaced by task 22's endpoint probe:

  * histlaw   dbcat=H  gethistlaw   PDF, same-origin binary
  * hkiac     dbcat=O  getother     PDF, external-host binary
  * hklrccp   dbcat=O  getother     HTML (embedded content)
  * hklrcr    dbcat=O  getother     HTML (embedded content)
  * pcpdaab   dbcat=O  getother     PDF, external-host binary
  * pcpdc     dbcat=O  getother     HTML (embedded content)

Architecture: docs/d3-runner-design.md.
"""
from __future__ import annotations

import pytest


class TestD3Family:
    """Family-record semantics."""

    @pytest.mark.parametrize(
        "slug,expected_wire_abbr",
        [
            ("histlaw", "hkhistlaws"),
            ("hkiac", "hkiac"),
            ("hklrccp", "hklrccp"),
            ("hklrcr", "hklrcr"),
            ("pcpdaab", "pcpdaab"),
            ("pcpdc", "pcpdc"),
        ],
    )
    def test_wire_abbr_per_family(self, slug, expected_wire_abbr):
        from hklii_downloader.d3 import D3_FAMILIES, wire_abbr

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        assert wire_abbr(family) == expected_wire_abbr


class TestD3UrlBuilders:
    """URL constructors — listing + fetch."""

    @pytest.mark.parametrize(
        "slug,expected_dbcat",
        [
            ("histlaw", "H"),
            ("hkiac", "O"),
            ("hklrccp", "O"),
            ("hklrcr", "O"),
            ("pcpdaab", "O"),
            ("pcpdc", "O"),
        ],
    )
    def test_gethoptfiles_url_carries_dbcat_and_slug(
        self, slug, expected_dbcat,
    ):
        from hklii_downloader.d3 import D3_FAMILIES, gethoptfiles_url

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        url = gethoptfiles_url(
            family, lang="en", page=1, items_per_page=100,
        )

        assert url.startswith("https://www.hklii.hk/api/gethoptfiles?")
        assert f"dbcat={expected_dbcat}" in url
        assert f"abbr={slug}" in url  # SPA slug, NOT wire_abbr
        assert "lang=en" in url
        assert "page=1" in url
        assert "itemsPerPage=100" in url
