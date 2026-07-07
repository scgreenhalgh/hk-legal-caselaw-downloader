"""Render-time HTML sanitizer for HKLII judgments.

Allowlist model (design §5 line 115 + §9 line 261):
- REJECT_TAGS: element subtree is dropped entirely; the tail
  (text after the closing tag, part of the parent's stream) is preserved
- HKLII_SEMANTIC_TAGS: preserved as-is; non-standard but the design
  wants them in the served HTML for CSS + reader orientation
- Attribute strip: any attribute NOT in ALLOWED_ATTRS is removed. New
  HKLII inline attrs are dropped by default rather than silently letting
  them through — a fresh HKLII feature would fail loud in a golden fixture
  test rather than silently take over the rendered look
- Unknown tags survive silently: preserving them costs nothing, browser
  renders them as anonymous inline elements (unstyled since <link> tags
  and inline styles are stripped)
"""

from __future__ import annotations

from lxml import html as lxml_html
from lxml.etree import ParserError, _Element


#: Tags whose subtree is DROPPED entirely — content is JS/CSS/binary.
_DROP_SUBTREE_TAGS: frozenset[str] = frozenset({
    "script", "style", "iframe", "object", "embed",
})

#: Void reject tags — self-closing, so no subtree to consider; the tag
#: itself is removed (link/meta/base are head decoration; input is form UI).
_VOID_DROP_TAGS: frozenset[str] = frozenset({
    "link", "meta", "input", "base",
})

#: Tags that are UNWRAPPED — the tag itself disappears but children survive.
#: HKLII wraps every judgment body in <form name="search_body">; dropping
#: the subtree would zero the rendered body. Verified against
#: hkcfa/2013/hkcfa_2013_11.html (27KB raw → 119 chars with the drop-only
#: strategy, vs the full body preserved after this split).
_UNWRAP_TAGS: frozenset[str] = frozenset({"form", "button"})

#: Public constant covering all tag categories treated by the sanitizer.
#: Design §5 line 115 enumerated these as one set; internally we split by
#: behavior (drop-subtree vs void-drop vs unwrap).
REJECT_TAGS: frozenset[str] = (
    _DROP_SUBTREE_TAGS | _VOID_DROP_TAGS | _UNWRAP_TAGS
)


#: HKLII's non-standard semantic tags — kept as-is so downstream CSS
#: and reader orientation still work. Design §5 line 115.
HKLII_SEMANTIC_TAGS: frozenset[str] = frozenset({
    "parties", "coram", "date", "representation",
})


#: Attribute allowlist. Anything else is stripped (allowlist model).
#:  - align/width/valign/colspan/rowspan: HKLII table & text layout
#:    (design §9 line 261).
#:  - href: hyperlinks (including our own /cite/{...} rewrites).
#:  - id/name: anchor targets — legacy HKLII uses <a name="...">.
#:  - class: CSS + our citation linkifier's 'hklii-cite' hook.
#:  - lang: :lang(zh-Hant) selector target (design §9 line 249).
#:  - title: accessible tooltips.
#:  - src/alt: images.
ALLOWED_ATTRS: frozenset[str] = frozenset({
    "align", "width", "valign", "colspan", "rowspan",
    "href", "id", "name", "class", "lang", "title",
    "src", "alt",
})


def sanitize_body(html_content: str | bytes) -> str:
    """Return sanitized HTML string, ready for the Jinja template.

    Empty / whitespace / parse-to-nothing input → empty string. Any
    other parse error raises (real HTML corruption is not silenced).
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

    _drop_rejected_subtrees(root)
    _unwrap_form_and_button(root)
    _strip_disallowed_attrs(root)

    return lxml_html.tostring(root, encoding="unicode", method="html")


def _drop_rejected_subtrees(root: _Element) -> None:
    """Remove every element whose tag is in _DROP_SUBTREE_TAGS or
    _VOID_DROP_TAGS.

    The rejected element's tail text (after its closing tag) belongs to
    the parent's text stream — preserved by grafting it onto the previous
    sibling's tail or the parent's text.

    Collects victims first, then removes, so no live-iterator invalidation
    over a mutating tree.
    """
    drop_set = _DROP_SUBTREE_TAGS | _VOID_DROP_TAGS
    victims = [
        e
        for e in root.iter()
        if isinstance(e.tag, str) and e.tag.lower() in drop_set
    ]
    for e in victims:
        parent = e.getparent()
        if parent is None:
            # Root itself is a reject tag — replace its contents with nothing.
            root.clear()
            return
        tail = e.tail
        if tail:
            prev = e.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail
        parent.remove(e)


def _unwrap_form_and_button(root: _Element) -> None:
    """Unwrap every ``<form>`` / ``<button>`` element — remove the tag
    itself but promote its children (and text) into the parent.

    HKLII wraps every judgment body in ``<form name="search_body">``;
    treating <form> as a subtree-drop zero'd the output. Unwrap keeps
    the content while stripping the meaningless form scaffold.

    Uses lxml.html.HtmlElement.drop_tag() which handles text/tail
    plumbing correctly (unlike a naive parent.remove + reinsert).
    """
    victims = [
        e
        for e in root.iter()
        if isinstance(e.tag, str) and e.tag.lower() in _UNWRAP_TAGS
    ]
    for e in victims:
        # Only HtmlElement has drop_tag; iter() returns _Element in general
        # but real HTML input yields HtmlElements, which subclass _Element.
        if hasattr(e, "drop_tag"):
            e.drop_tag()


def _strip_disallowed_attrs(root: _Element) -> None:
    """Drop any attribute not in :data:`ALLOWED_ATTRS`.

    Skips Comment / ProcessingInstruction nodes (their tag is a
    cyfunction) — they have no attrib to strip anyway.
    """
    for e in root.iter():
        if not isinstance(e.tag, str):
            continue
        for attr in list(e.attrib.keys()):
            if attr not in ALLOWED_ATTRS:
                del e.attrib[attr]
