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
