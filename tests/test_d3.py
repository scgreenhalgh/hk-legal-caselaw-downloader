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


class TestD3ParseFilesResponse:
    """parse_files_response over real fixtures from the 2026-07-08 probe."""

    def test_parse_histlaw_response(self):
        body = {
            "totalfiles": 3836,
            "files": [
                {
                    "title": "Companies Ordinance(32)",
                    "path": "/en/legis/histlaw/1964/1/",
                    "neutral": "[1964] HKHistLaws 1",
                    "date": "1964-01-01",
                },
                {
                    "title": "Official Languages Ordinance(5)",
                    "path": "/en/legis/histlaw/1964/3/",
                    "neutral": "[1964] HKHistLaws 3",
                    "date": "1964-01-01",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 3836
        assert len(result.entries) == 2
        first = result.entries[0]
        assert first.year == 1964
        assert first.num == 1
        assert first.title == "Companies Ordinance(32)"
        assert first.neutral == "[1964] HKHistLaws 1"
        assert first.date == "1964-01-01"

    def test_parse_hklrccp_response_no_trailing_slash(self):
        body = {
            "totalfiles": 78,
            "files": [
                {
                    "title": "Outcome Related Fee Structures for Arbitration",
                    "path": "/en/other/hklrccp/2020/2",
                    "neutral": "[2020] HKLRCCP 2",
                    "date": "2020-12-01",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 78
        assert len(result.entries) == 1
        assert result.entries[0].year == 2020
        assert result.entries[0].num == 2

    def test_parse_hkiac_response(self):
        body = {
            "totalfiles": 190,
            "files": [
                {
                    "title": (
                        "Playboy Enterprises International, Inc. v. "
                        "E-MODE LIMITED"
                    ),
                    "path": "/en/other/hkiac/2021/183",
                    "neutral": "[2021] HKIAC 183",
                    "date": "2021-10-10",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 190
        assert len(result.entries) == 1
        e = result.entries[0]
        assert e.year == 2021
        assert e.num == 183
        assert e.neutral == "[2021] HKIAC 183"

    def test_parse_skips_malformed_paths_and_logs_count(self, caplog):
        import logging

        body = {
            "totalfiles": 3,
            "files": [
                {"title": "ok", "path": "/en/legis/histlaw/1964/1/"},
                {"title": "bad", "path": "/random/garbage"},
                {"title": "also bad", "path": ""},
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        with caplog.at_level(logging.INFO, logger="hklii_downloader.d3"):
            result = parse_files_response(body)

        assert len(result.entries) == 1
        assert result.total == 3
        assert any(
            "skipped 2" in r.message.lower() or "skipped 2" in r.message
            for r in caplog.records
        ), f"expected skip-log with count 2 in {caplog.records}"

    def test_parse_skips_nd_year_but_still_parses(self):
        """Regex accepts nd defensively; parser skips it to keep year: int.

        Not observed on D3 during probe — future-proofing against a hopt-
        style legacy row appearing on histlaw or elsewhere.
        """
        body = {
            "totalfiles": 2,
            "files": [
                {"title": "ok", "path": "/en/legis/histlaw/1964/1/"},
                {"title": "nd row", "path": "/en/legis/histlaw/nd/9/"},
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)
        assert len(result.entries) == 1
        assert result.entries[0].year == 1964


class TestD3PdfUrl:
    """pdf_url — hop-2 URL resolver for the three JSON body shapes."""

    def test_external_absolute_url_returned_unchanged(self):
        """Shape C — hkiac/pcpdaab point at external source-org hosts."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        response = {
            "content": "",
            "pdf": (
                "https://www.hkiac.org/sites/default/files/"
                "ck_filebrowser/IP/hk/decision/DHK-2100183_Decision.pdf"
            ),
        }

        url = pdf_url(family, response)

        assert url == (
            "https://www.hkiac.org/sites/default/files/"
            "ck_filebrowser/IP/hk/decision/DHK-2100183_Decision.pdf"
        )

    def test_hklii_relative_url_joined_to_base(self):
        """Shape A — histlaw ships a same-origin `/static/...` path."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
        response = {"pdf": "/static/en/histlaw/1964/1.pdf"}

        url = pdf_url(family, response)

        assert url == "https://www.hklii.hk/static/en/histlaw/1964/1.pdf"

    def test_html_slug_no_pdf_field_returns_none(self):
        """Shape B — hklrccp/hklrcr/pcpdc: no `pdf` key → no second hop."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
        response = {"content": "<h3>...</h3>", "file_type": 1}

        assert pdf_url(family, response) is None

    def test_empty_pdf_field_returns_none(self):
        """Defensive — pdf key present but empty string treated as absent."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        assert pdf_url(family, {"pdf": ""}) is None
