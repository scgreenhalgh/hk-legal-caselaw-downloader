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

    @pytest.mark.parametrize(
        "slug,expected_endpoint,expected_abbr",
        [
            ("histlaw", "gethistlaw", "hkhistlaws"),  # wire rewrite
            ("hkiac", "getother", "hkiac"),
            ("hklrccp", "getother", "hklrccp"),
            ("hklrcr", "getother", "hklrcr"),
            ("pcpdaab", "getother", "pcpdaab"),
            ("pcpdc", "getother", "pcpdc"),
        ],
    )
    def test_fetch_url_endpoint_and_wire_abbr(
        self, slug, expected_endpoint, expected_abbr,
    ):
        from hklii_downloader.d3 import D3_FAMILIES, fetch_url

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        url = fetch_url(family, year=2020, num=1, lang="en")

        assert url.startswith(
            f"https://www.hklii.hk/api/{expected_endpoint}?"
        )
        assert f"abbr={expected_abbr}" in url
        assert "year=2020" in url
        assert "num=1" in url
        assert "lang=en" in url


class TestD3PathRegex:
    """_PATH_RE accepts /legis/ (histlaw) OR /other/ (getother slugs).

    Not a reuse of hopt._PATH_RE (which is /legis/ only). Also defends
    against ``nd`` year token by parity with hopt even though it was
    not observed on D3 during the endpoint probe.
    """

    @pytest.mark.parametrize(
        "path,expected_year,expected_num",
        [
            # histlaw — /legis/, trailing slash
            ("/en/legis/histlaw/1964/1/", "1964", "1"),
            # HTML slugs — /other/, no trailing slash observed
            ("/en/other/hklrccp/2020/2", "2020", "2"),
            ("/en/other/hklrcr/2019/3", "2019", "3"),
            ("/en/other/pcpdc/2018/5", "2018", "5"),
            # PDF external-host slugs — /other/
            ("/en/other/hkiac/2021/183", "2021", "183"),
            ("/en/other/pcpdaab/2020/1", "2020", "1"),
            # TC lang lane
            ("/tc/other/hklrccp/2020/2", "2020", "2"),
            # nd year — defensive parity with hopt
            ("/en/legis/histlaw/nd/7/", "nd", "7"),
        ],
    )
    def test_path_re_matches_legis_and_other(
        self, path, expected_year, expected_num,
    ):
        from hklii_downloader.d3 import _PATH_RE

        m = _PATH_RE.match(path)
        assert m is not None, f"regex did not match {path}"
        assert m.group(1) == expected_year
        assert m.group(2) == expected_num

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/",
            "/en/legis/",
            "/en/legis/histlaw/",
            "/en/legis/histlaw/1964",     # missing num
            "/en/cases/hkcfa/2020/1/",    # wrong bucket
            "/de/legis/histlaw/1964/1/",  # wrong lang
        ],
    )
    def test_path_re_rejects_malformed(self, path):
        from hklii_downloader.d3 import _PATH_RE

        assert _PATH_RE.match(path) is None
