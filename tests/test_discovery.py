"""Tests for the Phase D1 discovery module.

Discovery walks the HKLII ``/databases`` page (a Vue SPA — must be
rendered before parsing) and extracts the ``slug × lang`` matrix
by category (cases / legis / other). Ships with a checked-in
rendered-HTML fixture and a drift-guard test that asserts the
matrix matches our hardcoded ``ALL_COURTS`` and ``HOPT_C_COURTS``.

D2 (freshness via count / last-updated) and D3 (remove all
hardcoded court lists) are deliberately out of scope — see
2026-07-08 session close for the roadmap.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hklii_downloader.discovery import (
    DatabaseMatrix,
    parse_databases_matrix,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures" / "databases_page_rendered_2026-07-08.html"
)


class TestParserSyntheticInput:
    """Small hand-authored HTML — pins the parser's shape independent
    of the frozen 2026-07-08 fixture. Any HTML restructure at HKLII
    will fail these + the fixture drift-guard together, but keeping
    small inputs here means diagnosing which is which is fast."""

    def _mk(self, links: list[str]) -> str:
        body = "".join(f'<a href="{h}">x</a>' for h in links)
        return f"<html><body>{body}</body></html>"

    def test_case_slug_extracted(self):
        html = self._mk(["/en/cases/hkcfa/", "/tc/cases/hkcfa/"])
        matrix = parse_databases_matrix(html)
        assert isinstance(matrix, DatabaseMatrix)
        assert matrix.cases == {"hkcfa": ("en", "tc")}
        assert matrix.legis == {}
        assert matrix.other == {}

    def test_ukpc_en_only(self):
        """UKPC has no TC anchor on /databases — parser must not
        fabricate a TC entry, even when EN/TC entries exist for
        peer slugs."""
        html = self._mk([
            "/en/cases/hkcfa/", "/tc/cases/hkcfa/",
            "/en/cases/ukpc/",  # no /tc/cases/ukpc/
        ])
        matrix = parse_databases_matrix(html)
        assert matrix.cases["hkcfa"] == ("en", "tc")
        assert matrix.cases["ukpc"] == ("en",)

    def test_legis_category_separated(self):
        html = self._mk([
            "/en/legis/ord/", "/tc/legis/ord/", "/sc/legis/ord/",
        ])
        matrix = parse_databases_matrix(html)
        assert matrix.legis == {"ord": ("en", "sc", "tc")}
        assert matrix.cases == {}

    def test_other_category_separated(self):
        html = self._mk(["/en/other/pd/", "/tc/other/pd/"])
        matrix = parse_databases_matrix(html)
        assert matrix.other == {"pd": ("en", "tc")}

    def test_dedup_across_multiple_anchors_same_lang(self):
        """Same lang linked twice → one entry."""
        html = self._mk([
            "/en/cases/hkcfa/",
            "/en/cases/hkcfa/",
            "/en/cases/hkcfa/",
        ])
        matrix = parse_databases_matrix(html)
        assert matrix.cases == {"hkcfa": ("en",)}

    def test_unrelated_anchors_skipped(self):
        """Header/footer nav — /about, mailto:, external, /404 —
        must not clutter the matrix."""
        html = self._mk([
            "/en/cases/hkcfa/",
            "/about",
            "mailto:info@hklii.hk",
            "https://example.com",
            "/404",
            "#footer",
        ])
        matrix = parse_databases_matrix(html)
        assert matrix.cases == {"hkcfa": ("en",)}
        assert matrix.legis == {}
        assert matrix.other == {}

    def test_trailing_segments_ignored(self):
        """Anchor may include a deeper path like /en/cases/hkcfa/2020/…
        — only the (lang, category, slug) triple counts."""
        html = self._mk([
            "/en/cases/hkcfa/2020/1",
            "/en/cases/hkcfa/",
        ])
        matrix = parse_databases_matrix(html)
        assert matrix.cases == {"hkcfa": ("en",)}


class TestFixtureDriftGuard:
    """The 2026-07-08 fixture is the frozen ground truth for
    ``/databases``. This test compares the parsed matrix against
    ``ALL_COURTS`` (getcasefiles case-family, 12 slugs) plus
    ``HOPT_C_COURTS`` (hopt-C, currently just ukpc). Any drift between
    HKLII's live list and our hardcoded fan-out lists surfaces here —
    the fix is either updating the fixture (if HKLII genuinely added
    a slug) or updating the hardcoded lists (if we missed one).

    Legis + other buckets are intentionally NOT asserted against
    hardcoded lists — LEGIS_CAP_TYPES only covers ord/reg/instrument
    (not histlaw/hkts/…) because the other legis abbrs are handled
    by scrape-hopt, not scrape-legis. This test focuses on the
    case-family universe which is the primary drift risk.
    """

    def test_fixture_parses_nonempty(self):
        """Sanity: fixture is on disk and produces some rows."""
        html = FIXTURE.read_text()
        matrix = parse_databases_matrix(html)
        assert len(matrix.cases) > 0
        assert len(matrix.legis) > 0
        assert len(matrix.other) > 0

    def test_cases_bucket_equals_all_courts_plus_hopt_c(self):
        """The heart of the drift guard: matrix.cases's slug set
        exactly matches ``ALL_COURTS`` (12) + ``HOPT_C_COURTS`` (1) —
        13 total. If HKLII adds a case-family court, this fails.
        If we forget to add a slug to either ALL_COURTS or
        HOPT_C_COURTS, this fails.
        """
        from hklii_downloader.cli import ALL_COURTS
        from hklii_downloader.ukpc import HOPT_C_COURTS

        html = FIXTURE.read_text()
        matrix = parse_databases_matrix(html)
        parsed = set(matrix.cases.keys())
        expected = set(ALL_COURTS) | set(HOPT_C_COURTS)
        assert parsed == expected, (
            f"cases-bucket drift detected!\n"
            f"  parsed only: {sorted(parsed - expected)}\n"
            f"  expected only: {sorted(expected - parsed)}"
        )

    def test_ukpc_is_en_only_in_fixture(self):
        """/databases confirms UKPC has no TC counterpart on HKLII.
        Independent of the hopt-C fetch flow — this is the frontend
        source of truth."""
        html = FIXTURE.read_text()
        matrix = parse_databases_matrix(html)
        assert matrix.cases["ukpc"] == ("en",)

    def test_other_case_family_courts_are_bilingual(self):
        """Every case-family court apart from UKPC has both EN and TC
        on the /databases page."""
        from hklii_downloader.cli import ALL_COURTS
        html = FIXTURE.read_text()
        matrix = parse_databases_matrix(html)
        for court in ALL_COURTS:
            assert matrix.cases[court] == ("en", "tc"), (
                f"{court} lang set is {matrix.cases[court]}, expected en+tc"
            )
