"""Tests for the PCPD Administrative Appeals Board resolver.

D3 pcpdaab source: pcpd.org.hk. HKLII's pcpdaab metadata points at
broken /static/ URLs; the real archive lives at pcpd.org.hk with a
per-case index page at `decisions_detail.html` linking to
`files/AAB_*.pdf`.

Architecture: `memory/d3-alt-source-research.md`. Session research
2026-07-09 confirmed 100% HKLII coverage after DOM-based anchor-text
parsing (the anchor text — not the filename — is authoritative).
"""
from __future__ import annotations

import pytest


class TestPcpdaabEntry:
    def test_dataclass_holds_fields(self):
        from hklii_downloader.pcpdaab import PcpdaabEntry

        entry = PcpdaabEntry(
            year=2000,
            num=17,
            filename="AAB_17_2000_e.pdf",
            chinese_only=False,
            anchor_text="AAB 17-2000",
        )

        assert entry.year == 2000
        assert entry.num == 17
        assert entry.filename == "AAB_17_2000_e.pdf"
        assert entry.chinese_only is False
        assert entry.anchor_text == "AAB 17-2000"


class TestParseDecisionsDetail:
    """Anchor-text-driven index parse.

    The anchor TEXT (e.g. "AAB 232-2013") is the authoritative index.
    The filename is opaque payload — do NOT reverse-engineer (year, num)
    from the href.
    """

    def test_single_num_anchor_yields_one_entry(self):
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<tr><td>'
            '<a href="files/AAB_17_2000_e.pdf" rel="external">AAB 17-2000</a>'
            '</td></tr>'
        )

        result = parse_decisions_detail(html)

        assert set(result.keys()) == {(2000, 17)}
        entry = result[(2000, 17)]
        assert entry.filename == "AAB_17_2000_e.pdf"
        assert entry.year == 2000
        assert entry.num == 17
        assert entry.chinese_only is False

    def test_uses_anchor_text_not_filename(self):
        """The three HKLII num-truncation cases (2013/32 → really 232)
        surface here — anchor text says 232 even though filename could
        be anything. Parser must trust the text.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<a href="files/AAB_232_2013.pdf" rel="external">'
            'AAB 232-2013</a>'
        )

        result = parse_decisions_detail(html)

        assert (2013, 232) in result
        # NOT keyed by the naive filename num=232... it MUST match.
        # And NOT keyed by (2013, 32) which is a common wrong parse.
        assert (2013, 32) not in result

    def test_skips_non_aab_pdf_anchors(self):
        """The page has nav links, css, table-of-contents anchors that
        don't point at AAB PDFs. Every one must be dropped so the
        (year, num) dict only carries decision entries.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<a href="#2020">Jump to 2020</a>'
            '<a href="../css/default.css">stylesheet</a>'
            '<a href="casenotes_2.php?id=2020A01">Case notes 2020A01</a>'
            '<a href="files/other_report.pdf">Some other report</a>'
            '<a href="files/AAB_1_2020.pdf">AAB 1-2020</a>'
        )

        result = parse_decisions_detail(html)

        assert set(result.keys()) == {(2020, 1)}

    def test_chinese_only_annotation_captured(self):
        """PCPD marks some cases with "(This decision provides Chinese
        version only)". The parser must set chinese_only=True and still
        derive (year, num) from the AAB prefix.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<a href="files/AAB_232_2013.pdf" rel="external">'
            'AAB 232-2013 (This decision provides Chinese version only)'
            '</a>'
        )

        result = parse_decisions_detail(html)

        assert (2013, 232) in result
        assert result[(2013, 232)].chinese_only is True

    def test_chinese_only_absent_by_default(self):
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = '<a href="files/AAB_1_2020.pdf">AAB 1-2020</a>'

        result = parse_decisions_detail(html)

        assert result[(2020, 1)].chinese_only is False

    def test_ampersand_joined_two_clauses(self):
        """One PDF holds two decisions, anchor text joins them with "&"."""
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<a href="files/AAB_5_6_2021.pdf">AAB 5-2021 & AAB 6-2021</a>'
        )

        result = parse_decisions_detail(html)

        assert set(result.keys()) == {(2021, 5), (2021, 6)}
        assert result[(2021, 5)].filename == "AAB_5_6_2021.pdf"
        assert result[(2021, 6)].filename == "AAB_5_6_2021.pdf"
        # Both entries flag they share a PDF with the OTHER pair.
        assert result[(2021, 5)].shares_pdf_with == ((2021, 6),)
        assert result[(2021, 6)].shares_pdf_with == ((2021, 5),)

    def test_compound_num_list_with_range(self):
        """The real 2024 anchor covers 10 decisions in one PDF."""
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = (
            '<a href="files/AAB_16_17_2024.pdf">'
            'AAB 1, 2, 5, 6, 8-11, 16 & 17/2024'
            '</a>'
        )

        result = parse_decisions_detail(html)

        expected = {
            (2024, 1), (2024, 2), (2024, 5), (2024, 6),
            (2024, 8), (2024, 9), (2024, 10), (2024, 11),
            (2024, 16), (2024, 17),
        }
        assert set(result.keys()) == expected
        # shares_pdf_with lists the OTHER 9 for each entry.
        assert (2024, 1) in result
        assert result[(2024, 1)].shares_pdf_with == tuple(
            sorted(expected - {(2024, 1)})
        )

    def test_range_hyphen_not_confused_with_year_separator(self):
        """Distinguishing "8-11" (range) from "1-2020" (num-year)
        requires clause-scoped parsing. Bad greedy regex would treat
        "5-2021 & AAB 6-2021" as one range spanning 5 to 2021.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = '<a href="files/AAB_5_6_2021.pdf">AAB 5-2021 & AAB 6-2021</a>'

        result = parse_decisions_detail(html)

        assert len(result) == 2  # NOT 2017 pairs from a range explosion

    def test_single_num_shares_pdf_with_is_empty(self):
        from hklii_downloader.pcpdaab import parse_decisions_detail

        html = '<a href="files/AAB_1_2020.pdf">AAB 1-2020</a>'

        result = parse_decisions_detail(html)

        assert result[(2020, 1)].shares_pdf_with == ()


class TestFullFixture:
    """Pin the live 2026-07-09 PCPD decisions_detail.html contents.

    The fixture was fetched via the ProxyPool in the session that added
    this module. It documents the parser's expected behavior against
    every naming variant in the wild — a smoke test that a refactor
    of the parsing regexes cannot silently regress.
    """

    @pytest.fixture
    def fixture_html(self) -> str:
        from pathlib import Path
        return Path(
            "tests/fixtures/pcpd_decisions_detail.html"
        ).read_text()

    def test_extracts_at_least_400_entries(self, fixture_html):
        from hklii_downloader.pcpdaab import parse_decisions_detail

        result = parse_decisions_detail(fixture_html)

        # Session research 2026-07-09 measured 429 unique (year, num)
        # pairs. Assert ≥400 to leave headroom for PCPD adding a few
        # cases without breaking the pin.
        assert len(result) >= 400, f"only got {len(result)}"

    def test_covers_the_12_previously_missing_hklii_entries(
        self, fixture_html,
    ):
        """These 12 (year, num) pairs were invisible to the filename-
        regex approach earlier; anchor-text parsing must find all of them.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        result = parse_decisions_detail(fixture_html)

        expected_now_covered = [
            (2000, 17),   # AAB_17_2000_e.pdf — E-suffix variant
            (2005, 61),   # AAB_61_2005.pdf — not linked via my old regex
            (2013, 25),   # AAB_Decision_25_2013_OCR.pdf — OCR variant
            (2013, 26),   # AAB_26_2013_e.pdf — e-suffix
            (2013, 232),  # HKLII pcpdaab/2013/32 → truly 232
            (2013, 233),  # HKLII pcpdaab/2013/33 → truly 233
            (2013, 234),  # HKLII pcpdaab/2013/34 → truly 234
            (2014, 17),   # AAB_Decision_17_2014_OCR.pdf
            (2014, 23),   # AAB_Decision_23_2014_OCR.pdf
            (2014, 46),   # AAB_Decision_46_2014_OCR.pdf
            (2015, 1),    # AAB_Decision_1_2015_OCR.pdf
            (2016, 25),   # AAB_25_2016_E.pdf — capital-E variant
        ]
        for key in expected_now_covered:
            assert key in result, f"{key} should be resolvable but is missing"

    def test_multi_num_2024_shared_pdf_expands_to_10_entries(
        self, fixture_html,
    ):
        """AAB_16_17_2024.pdf covers "1, 2, 5, 6, 8-11, 16 & 17/2024" —
        10 HKLII rows must all resolve to that filename.
        """
        from hklii_downloader.pcpdaab import parse_decisions_detail

        result = parse_decisions_detail(fixture_html)

        expected_pairs = {
            (2024, 1), (2024, 2), (2024, 5), (2024, 6),
            (2024, 8), (2024, 9), (2024, 10), (2024, 11),
            (2024, 16), (2024, 17),
        }
        for k in expected_pairs:
            assert k in result, f"{k} missing"
            assert result[k].filename == "AAB_16_17_2024.pdf"
            # each should list all 9 others via shares_pdf_with
            assert set(result[k].shares_pdf_with) == expected_pairs - {k}


class TestFetchDiscovery:
    """Async fetch wrapper — delegates HTTP to a caller-provided `get`
    (satisfied by ``ProxyPool.get``), not to a hard-coded httpx client.

    That lets the pool's VPN routing / preflight / throttling flow
    through unchanged. Every wire probe against pcpd.org.hk still
    routes through the 20-proxy pool per the standing rule.
    """

    async def test_fetches_via_provided_get_and_parses(self):
        from pathlib import Path

        import httpx

        from hklii_downloader.pcpdaab import (
            PCPD_DECISIONS_URL,
            fetch_discovery,
        )

        fixture = Path(
            "tests/fixtures/pcpd_decisions_detail.html"
        ).read_bytes()
        requested: list[str] = []

        async def mock_get(url, **kw):
            requested.append(url)
            return httpx.Response(
                200,
                content=fixture,
                headers={"content-type": "text/html; charset=UTF-8"},
                request=httpx.Request("GET", url),
            )

        result = await fetch_discovery(mock_get)

        assert requested == [PCPD_DECISIONS_URL]
        assert len(result) >= 400

    async def test_non_200_raises_pcpdaab_fetch_error(self):
        import httpx

        from hklii_downloader.pcpdaab import (
            PcpdaabFetchError,
            fetch_discovery,
        )

        async def mock_get(url, **kw):
            return httpx.Response(
                503, text="upstream unavailable",
                request=httpx.Request("GET", url),
            )

        with pytest.raises(PcpdaabFetchError) as exc:
            await fetch_discovery(mock_get)

        assert "503" in str(exc.value)
        assert "decisions_detail" in str(exc.value).lower()

    async def test_transport_error_wrapped_as_pcpdaab_fetch_error(self):
        """httpx.RequestError from the pool must be converted so the
        caller only has to except PcpdaabFetchError.
        """
        import httpx

        from hklii_downloader.pcpdaab import (
            PcpdaabFetchError,
            fetch_discovery,
        )

        async def mock_get(url, **kw):
            raise httpx.ConnectTimeout(
                "simulated timeout",
                request=httpx.Request("GET", url),
            )

        with pytest.raises(PcpdaabFetchError) as exc:
            await fetch_discovery(mock_get)

        assert "timeout" in str(exc.value).lower() or "connecttimeout" in str(exc.value).lower()
