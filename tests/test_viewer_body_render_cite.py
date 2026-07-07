"""Tests for viewer/body_render/cite.py — citation regex + linkifier.

Wraps neutral-citation strings ([YYYY] COURT N) in <a href="/cite/{court}/
{year}/{num}" class="hklii-cite">. The (Phase 4) route resolves the href
by looking up the case; if not found, it renders a 200 'unresolved cite'
page — never a silent 302 to homepage (design §5 line 119).
"""

from __future__ import annotations

import pytest

from hklii_downloader.viewer.body_render.cite import (
    NEUTRAL_CITATION_RE,
    linkify_citations,
    parse_neutral_citation,
)


# ---------------------------------------------------------------------------
# parse_neutral_citation — pure regex parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[2020] HKCFA 1", ("hkcfa", 2020, 1)),
        ("[2013] HKCA 533", ("hkca", 2013, 533)),
        ("[2013] HKCFI 100", ("hkcfi", 2013, 100)),
        ("[2020] HKDC 42", ("hkdc", 2020, 42)),
        ("[1995] UKPC 12", ("ukpc", 1995, 12)),
        # Case-insensitive court + inner whitespace tolerance
        ("[2020] hkcfa 1", ("hkcfa", 2020, 1)),
        ("[2020]   HKCFA   1", ("hkcfa", 2020, 1)),
    ],
)
def test_parse_neutral_citation_recognizes_documented_courts(
    text: str, expected: tuple[str, int, int]
) -> None:
    result = parse_neutral_citation(text)
    assert result == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no citation here",
        "[2020]",              # missing court + number
        "[2020] HKCFA",        # missing number
        "HKCFA 1",             # missing year
        "(2020) 23 HKCFAR 100",  # parallel citation, not neutral format
        "[2020] FOO 1",        # unknown court
    ],
)
def test_parse_neutral_citation_returns_none_on_non_match(text: str) -> None:
    assert parse_neutral_citation(text) is None


def test_neutral_citation_regex_covers_all_13_court_slugs() -> None:
    """The regex must recognize the same 13 slugs as _COURT_RANK in
    viewer/graph.py — otherwise a UKPC or hkoat citation lands as
    unresolved.
    """
    for court in [
        "HKCFA", "HKCA", "HKCFI", "HKDC", "HKMAGC", "HKFC",
        "HKLDT", "HKLAT", "HKCT", "HKSCT", "HKCRC", "HKOAT", "UKPC",
    ]:
        text = f"[2020] {court} 1"
        assert NEUTRAL_CITATION_RE.search(text) is not None, court


# ---------------------------------------------------------------------------
# linkify_citations — DOM walker
# ---------------------------------------------------------------------------


def test_linkify_wraps_single_citation_in_anchor() -> None:
    html = "<p>See [2020] HKCFA 1 for the rule.</p>"
    out = linkify_citations(html)
    assert '<a href="/cite/hkcfa/2020/1" class="hklii-cite">' in out
    assert "[2020] HKCFA 1</a>" in out
    # Surrounding text preserved
    assert "See " in out
    assert " for the rule." in out


def test_linkify_wraps_multiple_citations_in_one_text_node() -> None:
    """Two consecutive citations in the same paragraph."""
    html = "<p>Compare [2020] HKCFA 1 and [2019] HKCA 5.</p>"
    out = linkify_citations(html)
    assert '<a href="/cite/hkcfa/2020/1"' in out
    assert '<a href="/cite/hkca/2019/5"' in out
    # Order preserved
    assert out.index("hkcfa/2020/1") < out.index("hkca/2019/5")


def test_linkify_skips_citations_inside_anchor_tags() -> None:
    """Design §5 line 119: skip <a> subtrees. A citation already inside
    an <a> (from a prior linkify pass or manual markup) is not re-wrapped.
    """
    html = '<p><a href="/x">[2020] HKCFA 1</a></p>'
    out = linkify_citations(html)
    # The outer <a href="/x"> survives — inner text is NOT re-wrapped
    assert 'href="/x"' in out
    assert 'href="/cite/hkcfa' not in out


@pytest.mark.parametrize("tag", ["code", "pre"])
def test_linkify_skips_citations_inside_code_and_pre(tag: str) -> None:
    """Design §5 line 119: <code> and <pre> content preserved verbatim."""
    html = f"<p>See <{tag}>[2020] HKCFA 1</{tag}> here.</p>"
    out = linkify_citations(html)
    assert 'href="/cite/hkcfa' not in out
    # Original content preserved as-is
    assert f"<{tag}>[2020] HKCFA 1</{tag}>" in out


def test_linkify_wraps_citation_in_element_tail() -> None:
    """The .tail of an inline element is text after the closing tag —
    citations there must also get wrapped.
    """
    html = "<p><b>Notes:</b> refer to [2020] HKCFA 1 for guidance.</p>"
    out = linkify_citations(html)
    assert '<a href="/cite/hkcfa/2020/1"' in out
    # Sibling text before and after also preserved
    assert "refer to " in out
    assert " for guidance." in out


def test_linkify_preserves_document_when_no_citations() -> None:
    """No citation → output byte-identical to input's sanitized form."""
    html = "<p>Just prose without any citations here.</p>"
    out = linkify_citations(html)
    assert "Just prose without any citations here." in out
    assert '<a href="/cite/' not in out


def test_linkify_is_idempotent() -> None:
    """A second pass over linkified HTML must not double-wrap. Depends on
    the design's skip-<a> rule: after the first pass, citations sit inside
    <a> and are skipped on the second.
    """
    html = "<p>See [2020] HKCFA 1 today.</p>"
    once = linkify_citations(html)
    twice = linkify_citations(once)
    # Only one <a class="hklii-cite"> in either
    assert once.count('class="hklii-cite"') == 1
    assert twice.count('class="hklii-cite"') == 1
    assert once == twice


def test_linkify_accepts_bytes_input() -> None:
    """Callers with raw file bytes shouldn't have to decode."""
    html = "<p>See [2020] HKCFA 1</p>".encode("utf-8")
    out = linkify_citations(html)
    assert '<a href="/cite/hkcfa/2020/1"' in out


def test_linkify_empty_input_returns_empty_string() -> None:
    """L5: matching the sanitizer/text.iter_text_nodes empty contract."""
    assert linkify_citations("") == ""
    assert linkify_citations(b"") == ""
    assert linkify_citations("   ") == ""


def test_linkify_preserves_cjk_around_citations() -> None:
    """Citation regex is ASCII-only; CJK text around it must survive."""
    html = "<p>參見 [2020] HKCFA 1 判決。</p>"
    out = linkify_citations(html)
    assert "參見" in out
    assert "判決" in out
    assert '<a href="/cite/hkcfa/2020/1"' in out
