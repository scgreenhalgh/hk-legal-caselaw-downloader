"""Tests for viewer/body_render/text.py — text-node walker.

The walker is shared between the FTS5 indexer (extracts plaintext from
sanitized HTML) and the future citation-highlighter (walks text nodes
to wrap citation patterns in <a>). See docs/viewer-design.md §5.
"""

from __future__ import annotations

from hklii_downloader.viewer.body_render.text import (
    DEFAULT_SKIP_TAGS,
    iter_text_nodes,
)


def test_yields_simple_body_text() -> None:
    html = "<html><body><p>hello world</p></body></html>"
    assert list(iter_text_nodes(html)) == ["hello world"]


def test_default_skip_includes_a_code_pre() -> None:
    """DEFAULT_SKIP_TAGS is exactly {a, code, pre} per design §5 line 115."""
    assert DEFAULT_SKIP_TAGS == frozenset({"a", "code", "pre"})


def test_skips_subtree_of_default_tags() -> None:
    """<a>, <code>, <pre> subtree text is NOT yielded, but the tail IS.

    Tail of an inline element is text AFTER the closing tag, contributed
    to the parent — visible in the rendered page even when the element
    itself is skipped.
    """
    html = '<p>outer<a href="/x">skipped-anchor</a>after-a'
    html += '<code>skipped-code</code>after-code'
    html += '<pre>skipped-pre</pre>after-pre</p>'
    result = list(iter_text_nodes(html))
    assert "skipped-anchor" not in result
    assert "skipped-code" not in result
    assert "skipped-pre" not in result
    # Tail texts + p.text preserved
    assert "outer" in result
    assert "after-a" in result
    assert "after-code" in result
    assert "after-pre" in result


def test_always_skips_script_style_head_regardless_of_skip_tags() -> None:
    """Head/script/style are ALWAYS skipped even with skip_tags=(). Guards
    against callers who pass an empty override and accidentally leak
    script bodies + <title> into FTS.
    """
    html = """
    <html>
    <head>
        <title>should-not-appear</title>
        <script>evil-js()</script>
        <style>body{color:red}</style>
    </head>
    <body><p>real-content</p></body>
    </html>
    """
    result = list(iter_text_nodes(html, skip_tags=()))
    joined = " ".join(result)
    assert "real-content" in joined
    assert "should-not-appear" not in joined
    assert "evil-js" not in joined
    assert "color:red" not in joined


def test_preserves_tail_after_skipped_default_tag() -> None:
    """The .tail of a skipped element is text AFTER </tag>, contributed to
    the parent — must still appear in output.
    """
    html = '<p>before<a href="/x">skipped</a>after</p>'
    assert "after" in list(iter_text_nodes(html))


def test_bytes_input_accepted() -> None:
    """Real HKLII files are bytes (utf-8). Callers shouldn't have to decode."""
    html_bytes = "<p>中文判決</p>".encode("utf-8")
    assert list(iter_text_nodes(html_bytes)) == ["中文判決"]


def test_whitespace_only_nodes_elided() -> None:
    """Text nodes containing only whitespace are not yielded — they are
    zero-signal noise from HTML source-formatting indentation.
    """
    html = "<div>real<span></span>   <p>content</p>   </div>"
    result = list(iter_text_nodes(html))
    assert "real" in result
    assert "content" in result
    # No purely-whitespace strings survive
    assert not any(s.strip() == "" for s in result)


def test_nested_skipped_tag_within_kept_tag() -> None:
    """<div><p><a>skip</a>keep</p></div> — walker enters div and p, encounters
    <a> (skipped subtree), yields a.tail = 'keep'.
    """
    html = '<div><p><a href="/x">skip</a>keep</p></div>'
    result = list(iter_text_nodes(html))
    assert result == ["keep"]


def test_root_that_is_skipped_yields_nothing() -> None:
    """If the entire input is a single skipped element, nothing yields."""
    html = '<a href="/x">just an anchor</a>'
    assert list(iter_text_nodes(html)) == []


def test_document_order_preserved() -> None:
    """Text yielded in document order — important for FTS snippet coherence."""
    html = "<div><p>first</p><p>second</p><p>third</p></div>"
    result = list(iter_text_nodes(html))
    assert result == ["first", "second", "third"]


def test_custom_skip_tags_extend_defaults_or_replace() -> None:
    """Callers can supply their own skip_tags. Empty tuple means 'no OPTIONAL
    skip' — always-skip (script/style/head/…) still applies.
    """
    html = '<p>keep<a href="/x">via-a</a>tail</p>'
    # With default: <a> skipped, so 'via-a' absent
    assert "via-a" not in list(iter_text_nodes(html))
    # With empty skip_tags: <a>'s subtree text now included
    result = list(iter_text_nodes(html, skip_tags=()))
    assert "via-a" in result


def test_hklii_semantic_tags_pass_through() -> None:
    """HKLII wraps parties/coram/etc in non-standard tags. Those must be
    descended into like any other element — they contain judgment prose.
    """
    html = "<parties><p>plaintiff v defendant</p></parties>"
    result = list(iter_text_nodes(html))
    assert result == ["plaintiff v defendant"]


def test_empty_bytes_input_returns_empty_iter_without_raising() -> None:
    """Regression: lxml.html.fromstring('') raises ParserError 'Document
    is empty'. A 0-byte body file on disk was aborting the full-corpus
    build_index. Guard: iter_text_nodes returns [] for empty/whitespace-only.

    extract_plaintext's docstring promises 'Empty body → empty string' —
    the walker underlying it must not raise.
    """
    assert list(iter_text_nodes(b"")) == []
    assert list(iter_text_nodes("")) == []


def test_whitespace_only_input_returns_empty_iter() -> None:
    """Same as empty-bytes case: fromstring raises on whitespace-only too."""
    assert list(iter_text_nodes(b"   \n\t  ")) == []
    assert list(iter_text_nodes("   \n\t  ")) == []


def test_html_comment_content_not_yielded() -> None:
    """Regression: lxml.html.fromstring preserves HTML comments as Comment
    nodes whose .tag is a cyfunction (not a str). Previous _walk guard
    `if isinstance(tag, str)` set tag=None for those, and the skip-check
    short-circuited, so element.text + .tail leaked into extract_plaintext
    → FTS-indexed as body text.

    Fix: non-str tags are treated as skip-subtrees. Their .tail belongs
    to the parent (visible between the comment and the next tag), so
    the tail is preserved.
    """
    html = "<p>before<!-- SECRET NOTE -->after</p>"
    result = list(iter_text_nodes(html))
    assert "SECRET" not in " ".join(result)
    assert "NOTE" not in " ".join(result)
    # But tail 'after' is preserved (parent's text between the comment
    # and end of <p>).
    assert "after" in result
    assert "before" in result


def test_ie_conditional_comment_not_yielded() -> None:
    """IE conditional comments in legacy HKLII HTML must not leak."""
    html = (
        "<body>"
        "<!--[if lt IE 9]>legacy stuff<![endif]-->"
        "<p>real prose</p>"
        "</body>"
    )
    result = " ".join(iter_text_nodes(html))
    assert "real prose" in result
    assert "legacy stuff" not in result
    assert "IE" not in result


def test_processing_instruction_content_not_yielded() -> None:
    """<?xml-stylesheet ...?> — ProcessingInstruction nodes also have
    non-str .tag; same skip-subtree treatment.
    """
    html = '<?xml-stylesheet href="/x.css" type="text/css"?><p>body</p>'
    result = list(iter_text_nodes(html))
    assert "xml-stylesheet" not in " ".join(result)
    assert "body" in result
