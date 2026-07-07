"""Text-node walker over HTML.

Yields text nodes in document order, skipping subtrees rooted at
caller-specified tags (default: ``<a>``, ``<code>``, ``<pre>``) plus a
fixed always-skip set of infrastructure tags (``<script>``, ``<style>``,
``<head>``, ``<noscript>``, ``<iframe>``, ``<object>``, ``<embed>``) that
never carry judgment prose.

Used by:
- FTS5 body extractor (viewer/search.py) — assembles case_bodies.body
- Citation highlighter (Phase 3) — walks text nodes to wrap [YYYY] cites

Design decision (§5 line 115): the skipped element's ``.text`` and all
children are excluded, but its ``.tail`` — text after the closing tag,
contributed to the parent — is included. That matches how the DOM
renders and how a lawyer would read the page.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from lxml import html as lxml_html
from lxml.etree import ParserError, _Element


#: Default subtrees whose text is excluded from iteration. Callers may
#: override, in which case the ALWAYS-SKIP set below still applies.
DEFAULT_SKIP_TAGS: frozenset[str] = frozenset({"a", "code", "pre"})

#: Tags whose subtrees are ALWAYS excluded, regardless of the caller's
#: ``skip_tags``. These never carry judgment prose — including them in
#: iterating text would leak <title>, script bodies, or CSS into FTS.
_ALWAYS_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "head", "noscript", "iframe", "object", "embed"}
)


def iter_text_nodes(
    html_content: str | bytes,
    skip_tags: Iterable[str] = DEFAULT_SKIP_TAGS,
) -> Iterator[str]:
    """Yield non-empty text nodes from ``html_content`` in document order.

    Parameters:
      html_content: HTML source. str or utf-8 bytes.
      skip_tags: element tags whose entire subtree is excluded from
        iteration. The element's ``.tail`` is still yielded (it belongs
        to the parent). Passed tags are unioned with ``_ALWAYS_SKIP_TAGS``.

    Yields:
      Text nodes (``.text`` or ``.tail`` strings) with at least one
      non-whitespace character. Purely whitespace nodes are elided.
    """
    # lxml.html.fromstring reads a raw byte stream as Latin-1 for HTML
    # fragments (no <meta charset> hint) — silently corrupting CJK. Decode
    # utf-8 ourselves and pass a str, which lxml processes correctly.
    # Real HKLII files are UTF-8 (verified). Callers with a different
    # encoding must decode themselves.
    if isinstance(html_content, bytes):
        html_content = html_content.decode("utf-8")
    # Empty / whitespace-only input: lxml.html.fromstring raises
    # ParserError('Document is empty'), which would abort a full-corpus
    # build_index on the first 0-byte body file. extract_plaintext's
    # docstring promises empty-body → empty string; honor that here.
    if not html_content.strip():
        return
    # Comment-only, DOCTYPE-only, PI-only content passes the strip check
    # but still parses to nothing → same ParserError. Rare corpus edge
    # cases (pandoc emitting bare DOCTYPE, sidecar retracted-content
    # placeholder). Narrow catch: treat 'Document is empty' as an empty
    # body, but let any other ParserError propagate — malformed real
    # HTML with mid-parse errors should still surface.
    try:
        root = lxml_html.fromstring(html_content)
    except ParserError as e:
        if "Document is empty" in str(e):
            return
        raise
    skip = frozenset(t.lower() for t in skip_tags) | _ALWAYS_SKIP_TAGS
    for text in _walk(root, skip):
        if text.strip():
            yield text


def _walk(element: _Element, skip: frozenset[str]) -> Iterator[str]:
    """Recursive walker.

    Three cases:
      - Comment / ProcessingInstruction / Entity: ``.tag`` is a cyfunction
        (not str). Skip the subtree entirely — comments can leak authoring
        notes or CMS boilerplate into FTS-indexed plaintext. The ``.tail``
        text (after the closing tag, contributed to the parent) is preserved.
      - Kept element (str tag not in skip): yield ``.text``, recurse into
        children, then yield ``.tail``.
      - Skipped element (str tag in skip): yield ONLY ``.tail`` — the
        subtree is dropped by design (e.g., ``<a>`` interior, ``<script>``).
    """
    tag = element.tag
    if not isinstance(tag, str):
        # Comment / ProcessingInstruction / Entity — subtree dropped.
        if element.tail:
            yield element.tail
        return
    if tag.lower() in skip:
        if element.tail:
            yield element.tail
        return
    if element.text:
        yield element.text
    for child in element:
        yield from _walk(child, skip)
    if element.tail:
        yield element.tail
