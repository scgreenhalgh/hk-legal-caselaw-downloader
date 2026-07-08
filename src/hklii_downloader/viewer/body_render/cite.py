"""Neutral-citation parser + DOM linkifier.

Wraps ``[YYYY] COURT N`` citations in ``<a href="/cite/{court}/{year}/{n}"
class="hklii-cite">`` on the render path. Design §5 line 119. The
resolver at ``/cite/{...}`` (Phase 4 route) looks up the case and 302s
to canonical on hit / 200 unresolved on miss — never a silent 302 to
homepage (L5).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from lxml import html as lxml_html
from lxml.etree import ParserError, _Element


#: Court slugs recognized in neutral citations. 13 total: the 12
#: getcasefiles-family courts + UKPC (which lives under the hopt-C
#: endpoint family — see viewer/courts.py comment).
_COURTS = "|".join([
    "HKCFA", "HKCA", "HKCFI", "HKDC", "HKMAGC", "HKFC",
    "HKLDT", "HKLAT", "HKCT", "HKSCT", "HKCRC", "HKOAT", "UKPC",
])

#: Neutral citation regex: ``[YYYY] COURT N`` with case-insensitive court
#: and inner-whitespace tolerance. Not anchored — callers use ``search`` /
#: ``finditer`` inside text nodes.
NEUTRAL_CITATION_RE: re.Pattern[str] = re.compile(
    rf"\[(\d{{4}})\]\s+({_COURTS})\s+(\d+)",
    re.IGNORECASE,
)


#: Subtrees whose text is NEVER linkified. Same set as
#: viewer/body_render/text.DEFAULT_SKIP_TAGS + docs/viewer-design.md §5
#: line 119: <a> avoids double-wrapping, <code>/<pre> preserve verbatim
#: content.
_SKIP_TAGS: frozenset[str] = frozenset({"a", "code", "pre"})


def parse_neutral_citation(text: str) -> tuple[str, int, int] | None:
    """Parse a neutral-citation string. Returns ``(court, year, number)``
    with court lower-cased ('hkcfa', 'hkcfi', ...) or ``None`` when no
    citation is found in ``text``.
    """
    m = NEUTRAL_CITATION_RE.search(text)
    if m is None:
        return None
    year, court, number = m.group(1), m.group(2), m.group(3)
    return court.lower(), int(year), int(number)


def linkify_citations(html_content: str | bytes) -> str:
    """Wrap neutral citations in ``<a class="hklii-cite">``.

    Skips text inside ``<a>``, ``<code>``, and ``<pre>`` subtrees so
    existing anchors aren't double-wrapped and preformatted code is
    preserved verbatim. Empty / whitespace / parse-to-nothing → ``""``
    (matches the sanitizer contract).
    """
    if isinstance(html_content, bytes):
        html_content = html_content.decode("utf-8")
    if not html_content.strip():
        return ""
    try:
        root = lxml_html.fromstring(html_content)
    except ParserError as e:
        if "Document is empty" in str(e):
            return ""
        raise
    _linkify_node(root)
    return lxml_html.tostring(root, encoding="unicode", method="html")


def _linkify_node(elem: _Element) -> None:
    """Recursively rewrite text nodes to inject citation anchors.

    Two text carriers per element: ``.text`` (before first child) and
    each child's ``.tail`` (after that child's closing tag). Skip
    subtrees rooted at :data:`_SKIP_TAGS`.
    """
    tag = elem.tag if isinstance(elem.tag, str) else None
    if tag is not None and tag.lower() in _SKIP_TAGS:
        return

    # Rewrite elem.text (before the first child)
    if elem.text:
        leading, extras = _split_by_citations(elem.text)
        if extras:
            elem.text = leading
            for i, (anchor, tail_text) in enumerate(extras):
                anchor.tail = tail_text
                elem.insert(i, anchor)

    # Recurse into and rewrite tails of children. Snapshot list first
    # (we may insert new siblings).
    for child in list(elem):
        _linkify_node(child)
        if child.tail:
            leading, extras = _split_by_citations(child.tail)
            if extras:
                child.tail = leading
                # Walk forward through the parent as we insert
                cursor = child
                for anchor, tail_text in extras:
                    anchor.tail = tail_text
                    cursor.addnext(anchor)
                    cursor = anchor


def _split_by_citations(
    text: str,
) -> tuple[str, list[tuple[_Element, str]]]:
    """Split ``text`` at each citation match.

    Returns ``(leading_text, [(anchor_element, tail_text), ...])``. If no
    matches, returns ``(text, [])`` and callers can no-op cheaply.

    ``leading_text`` is what belongs immediately BEFORE the first anchor
    (i.e., stays as the ``.text`` / ``.tail`` of the containing element).
    Each pair's ``tail_text`` is the text BETWEEN this anchor and the next
    (or between the last anchor and the end of the string).
    """
    matches = list(NEUTRAL_CITATION_RE.finditer(text))
    if not matches:
        return text, []

    leading = text[: matches[0].start()]
    pairs: list[tuple[_Element, str]] = []
    for i, m in enumerate(matches):
        anchor = _make_cite_anchor(m)
        if i + 1 < len(matches):
            tail = text[m.end() : matches[i + 1].start()]
        else:
            tail = text[m.end() :]
        pairs.append((anchor, tail))
    return leading, pairs


def _make_cite_anchor(match: re.Match) -> _Element:
    """Build the ``<a class="hklii-cite" href="/cite/{...}">`` element."""
    year, court, num = match.group(1), match.group(2).lower(), match.group(3)
    href = f"/cite/{court}/{year}/{num}"
    a = lxml_html.Element("a", href=href)
    a.set("class", "hklii-cite")
    a.text = match.group(0)  # the raw citation string, verbatim
    return a
