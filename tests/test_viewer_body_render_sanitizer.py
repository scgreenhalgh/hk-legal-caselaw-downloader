"""Tests for viewer/body_render/sanitizer.py — render-time HTML cleanup.

Design §5 line 115 + §9 line 261. Allowlist model: any attribute not
explicitly listed is stripped. Rejected tags are removed entirely
(subtree dropped, tail preserved). HKLII semantic tags (parties, coram,
date, representation) are preserved by name. Unknown tags survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hklii_downloader.viewer.body_render.sanitizer import (
    ALLOWED_ATTRS,
    HKLII_SEMANTIC_TAGS,
    REJECT_TAGS,
    sanitize_body,
)


# ---------------------------------------------------------------------------
# Constants pinned so downstream integrations can rely on them
# ---------------------------------------------------------------------------


def test_reject_tags_covers_documented_set() -> None:
    """Design §5 line 115 lists the reject set explicitly."""
    expected = {
        "script", "style", "link", "meta", "iframe",
        "object", "embed", "form", "input", "button", "base",
    }
    assert expected <= REJECT_TAGS


def test_hklii_semantic_tags_include_documented_four() -> None:
    """Design §5 line 115: parties/coram/date/representation preserved by name."""
    expected = {"parties", "coram", "date", "representation"}
    assert expected <= HKLII_SEMANTIC_TAGS


def test_allowed_attrs_includes_design_documented_six() -> None:
    """Design §9 line 261: preserved attributes list."""
    expected = {"align", "width", "valign", "colspan", "rowspan", "href"}
    assert expected <= ALLOWED_ATTRS


# ---------------------------------------------------------------------------
# Reject-tag behavior — subtree dropped, tail preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    # Drop-subtree reject tags — content between open/close carries
    # JS/CSS/binary payloads that must be excised entirely
    ["script", "style", "iframe", "object", "embed"],
)
def test_drop_subtree_reject_tag_and_content_removed(tag: str) -> None:
    """Drop-subtree reject tags are removed along with everything inside."""
    html = f"<div>keep<{tag}>drop this</{tag}>also keep</div>"
    out = sanitize_body(html)
    assert "drop this" not in out
    assert "keep" in out
    assert "also keep" in out
    assert f"<{tag}" not in out


@pytest.mark.parametrize(
    "tag",
    # Unwrap tags — HKLII wraps judgment content in <form name="search_body">;
    # dropping the subtree would strip the entire judgment (verified against
    # hkcfa/2013/hkcfa_2013_11.html: 27KB → 119 chars with the drop-only
    # strategy). Unwrap keeps children, drops only the tag itself.
    ["form", "button"],
)
def test_unwrap_reject_tag_removes_tag_but_preserves_content(
    tag: str,
) -> None:
    """Unwrap-tag content survives; only the tag itself disappears."""
    html = f'<div>before<{tag} name="x">judgment prose</{tag}>after</div>'
    out = sanitize_body(html)
    assert f"<{tag}" not in out
    assert "judgment prose" in out  # content promoted into parent
    assert "before" in out
    assert "after" in out


def test_hklii_form_wrapper_unwraps_body_content(
) -> None:
    """Regression: real HKLII judgments look like <html><body>
    <form name="search_body"><table>...judgment...</table></form>
    </body></html>. Dropping <form> would zero the body; the sanitizer
    must unwrap so downstream sees a sensible body.
    """
    html = (
        "<html><body>"
        '<form name="search_body">'
        "<table><tr><td>FACC No. 8 of 2012</td></tr></table>"
        "<parties>HKSAR v CHAN HOI TAT</parties>"
        "</form>"
        "</body></html>"
    )
    out = sanitize_body(html)
    assert "<form" not in out
    assert "FACC No. 8 of 2012" in out
    assert "<parties>HKSAR v CHAN HOI TAT</parties>" in out


@pytest.mark.parametrize(
    "tag",
    # Void reject tags — the tag itself gets stripped; there's no subtree
    ["link", "meta", "input", "base"],
)
def test_void_reject_tag_removed_from_output(tag: str) -> None:
    """Void reject tags (self-closing) are removed. Their real-world HKLII
    occurrences are attributes-only (<link href='/lrs/x.css'>,
    <meta charset=…>) — the tag itself is what we're stripping.

    lxml treats text between <void>...</void> as sibling text, not
    subtree content, so any surrounding text survives — which is what
    a real render pipeline wants anyway.
    """
    html = f'<div>before<{tag} href="/x"/><span>after</span></div>'
    out = sanitize_body(html)
    assert f"<{tag}" not in out
    assert "before" in out
    assert "after" in out


def test_reject_element_tail_preserved_on_parent(
) -> None:
    """The tail of a rejected element is text AFTER the closing tag —
    belongs to the parent's text stream. Must not be lost with the subtree.
    """
    html = "<p>before<script>eval()</script>after</p>"
    out = sanitize_body(html)
    assert "after" in out
    assert "before" in out
    assert "eval" not in out


def test_style_tag_content_dropped_not_just_the_tag() -> None:
    """A stray <style>body{color:red}</style> in the head strips the CSS
    text too (regression guard — an earlier draft yielded only the tail).
    """
    html = "<div><style>body{color:red}</style>real content</div>"
    out = sanitize_body(html)
    assert "color:red" not in out
    assert "real content" in out


# ---------------------------------------------------------------------------
# HKLII semantic tags — preserved as-is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag", ["parties", "coram", "date", "representation"]
)
def test_hklii_semantic_tag_preserved(tag: str) -> None:
    html = f"<{tag}>Prime v Defendant</{tag}>"
    out = sanitize_body(html)
    assert f"<{tag}>" in out
    assert "Prime v Defendant" in out


# ---------------------------------------------------------------------------
# Attribute allowlist — disallowed stripped, allowed preserved
# ---------------------------------------------------------------------------


def test_disallowed_bgcolor_attr_stripped() -> None:
    html = '<td bgcolor="#FF0000">content</td>'
    out = sanitize_body(html)
    assert "bgcolor" not in out
    assert "FF0000" not in out
    assert "content" in out


def test_disallowed_style_attr_stripped() -> None:
    """style='' carries font-family/font-size that fight our stylesheet;
    strip the whole attr (allowlist model).
    """
    html = '<p style="font-family:Arial;font-size:12pt">judgment prose</p>'
    out = sanitize_body(html)
    assert "font-family" not in out
    assert "font-size" not in out
    assert "Arial" not in out
    assert "judgment prose" in out


def test_disallowed_onclick_attr_stripped() -> None:
    """Inline event handlers must never survive — XSS prevention."""
    html = '<a href="/x" onclick="alert(1)">link</a>'
    out = sanitize_body(html)
    assert "onclick" not in out
    assert "alert" not in out
    # href survives (in the allowlist)
    assert 'href="/x"' in out


@pytest.mark.parametrize(
    "attr,value",
    [
        ("align", "center"),
        ("width", "100%"),
        ("valign", "top"),
        ("colspan", "2"),
        ("rowspan", "3"),
        ("href", "/x"),
    ],
)
def test_allowed_attr_preserved(attr: str, value: str) -> None:
    html = f'<td {attr}="{value}">content</td>'
    out = sanitize_body(html)
    assert f'{attr}="{value}"' in out


def test_class_attr_preserved_for_stylesheet_hooks() -> None:
    """The (Phase 3.5) citation highlighter adds class='hklii-cite'; the
    (Phase 5) stylesheet targets it. class must survive sanitization.
    """
    html = '<a href="/cite/x" class="hklii-cite">[2020] HKCFA 1</a>'
    out = sanitize_body(html)
    assert "hklii-cite" in out


def test_lang_attr_preserved_for_cjk_font_selector() -> None:
    """<article lang='zh-Hant'> is how design §9 line 249 targets the
    CJK font stack via :lang(zh-Hant). lang must survive.
    """
    html = '<article lang="zh-Hant">中文</article>'
    out = sanitize_body(html)
    assert 'lang="zh-Hant"' in out


# ---------------------------------------------------------------------------
# Unknown tags — preserved silently (design §5 line 115)
# ---------------------------------------------------------------------------


def test_unknown_tag_preserved_silently() -> None:
    """Design §5 line 115: 'Unknown tags preserved silently — no WARN log,
    no audit CLI in v1'. A future HKLII tag we don't recognize survives
    rather than gets dropped, so downstream is not surprised.
    """
    html = "<hklii-future-tag>content</hklii-future-tag>"
    out = sanitize_body(html)
    assert "content" in out


# ---------------------------------------------------------------------------
# Edge inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_string() -> None:
    """L5: empty in → empty out, no crash."""
    assert sanitize_body("") == ""
    assert sanitize_body(b"") == ""


def test_whitespace_only_input_returns_empty_string() -> None:
    assert sanitize_body("   \n\t") == ""


def test_parse_error_only_content_returns_empty_string() -> None:
    """Same class as text.iter_text_nodes: comment-only / DOCTYPE-only
    content parses to nothing. Sanitize returns empty.
    """
    assert sanitize_body("<!-- placeholder -->") == ""
    assert sanitize_body("<!DOCTYPE html>") == ""


def test_bytes_input_accepted() -> None:
    """Real files are utf-8 bytes; sanitizer must not force callers to decode."""
    html = "<p>中文判決</p>".encode("utf-8")
    out = sanitize_body(html)
    assert "中文判決" in out


def test_sanitizer_is_idempotent() -> None:
    """Running sanitize twice produces the same output — no accidental
    escaping churn or attr-order drift.
    """
    html = (
        '<div align="center"><p bgcolor="red">'
        "<script>evil()</script>real content"
        "</p></div>"
    )
    once = sanitize_body(html)
    twice = sanitize_body(once)
    assert once == twice
