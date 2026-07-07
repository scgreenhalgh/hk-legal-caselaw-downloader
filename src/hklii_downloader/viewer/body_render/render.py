"""Render-time body pipeline for HKLII judgments.

Contains:
- :class:`RenderSource` — the on-disk body chosen for a (case, lang) request
- :func:`select_body_source` — the discriminator (design §5 lines 104-113)

Later commits in Phase 3 extend this module with the sanitizer,
citation-highlighter, and cache. This file starts with just the
discriminator so route handlers have a stable input contract to build
against.
"""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path

from lxml import html as lxml_html

from hklii_downloader.viewer.body_render.sanitizer import sanitize_body


#: Bump this whenever the sanitizer rules change (or any element of the
#: render pipeline that could produce a different output for the same
#: input bytes). Every cached entry has the version embedded in its key,
#: so a bump invalidates the whole cache on the next request.
_SANITIZER_VERSION: str = "1"


#: Language codes accepted at the route boundary. 'zh' is a legacy value
#: some cp.cases rows may carry (per design §5); it's normalized to 'tc'
#: internally, but the discriminator only exposes 'en' and 'tc' at its
#: return surface.
_VALID_REQUESTED_LANGS: frozenset[str] = frozenset({"en", "tc"})


@dataclass(frozen=True)
class RenderSource:
    """The on-disk body chosen for a render request.

    - lang: the language of the served body ('en' | 'tc')
    - path: the file to read
    - source_kind: 'html' | 'tc.html' | 'generated.html' — needed by
      the (Phase 3.3) render dispatcher to pick the right shape branch
      (native HKLII vs pandoc fragment)
    - upstream_status: passthrough of cases.status so the route can
      render an 'orphaned / retracted' strip when appropriate
    - has_synth_anchors: reserved for v2 (source-provenance strip)
    """

    lang: str
    path: Path
    source_kind: str
    upstream_status: str = "downloaded"
    has_synth_anchors: bool = False


def _normalize_case_lang(lang: str) -> str:
    """Legacy cp.cases.lang can be 'zh'; treat it identical to 'tc'."""
    return "tc" if lang == "zh" else lang


def select_body_source(
    case_row: dict,
    output_root: str | Path,
    requested_lang: str,
) -> RenderSource | None:
    """Pick the on-disk body to serve for ``(case_row, requested_lang)``.

    Returns ``None`` when no file is available in that language — the
    route uses that signal to render a 404 with a formats-on-disk strip
    (design §5). Never raises on missing files; only ``ValueError`` on a
    genuinely wrong ``requested_lang`` (route-layer bug).

    Rules (design §5 lines 104-113):
      - ``requested_lang == 'tc'``:
        - prefer ``{stem}.tc.html`` (paired-bilingual)
        - else if case.lang is 'tc' or 'zh' (TC-only case), fall back
          to bare ``{stem}.html`` (which carries the Chinese content)
        - else if case.lang is TC-only and ``{stem}.generated.html``
          exists, use that
      - ``requested_lang == 'en'``:
        - only if case.lang is NOT TC-only (bilingual or EN-only case)
          → prefer ``{stem}.html``, else ``{stem}.generated.html``
        - a TC-only case yields ``None`` for an EN request
      - ``.generated.html`` is a FALLBACK — never overrides a real
        ``{stem}.html``

    ``upstream_status`` is propagated so the route can render an
    'orphaned' strip; the route also decides whether pending / in-progress
    statuses should surface as 410 Gone.
    """
    if requested_lang not in _VALID_REQUESTED_LANGS:
        raise ValueError(
            f"requested_lang must be 'en' or 'tc', got {requested_lang!r}"
        )

    court = case_row["court"]
    year = case_row["year"]
    number = case_row["number"]
    case_lang = _normalize_case_lang(case_row.get("lang", "en"))
    upstream_status = case_row.get("status", "downloaded")

    is_tc_only_case = case_lang == "tc"

    stem = f"{court}_{year}_{number}"
    d = Path(output_root) / court / str(year)
    html_path = d / f"{stem}.html"
    tc_html_path = d / f"{stem}.tc.html"
    gen_html_path = d / f"{stem}.generated.html"

    if requested_lang == "tc":
        if tc_html_path.exists():
            return RenderSource(
                lang="tc",
                path=tc_html_path,
                source_kind="tc.html",
                upstream_status=upstream_status,
            )
        if is_tc_only_case:
            if html_path.exists():
                return RenderSource(
                    lang="tc",
                    path=html_path,
                    source_kind="html",
                    upstream_status=upstream_status,
                )
            if gen_html_path.exists():
                return RenderSource(
                    lang="tc",
                    path=gen_html_path,
                    source_kind="generated.html",
                    upstream_status=upstream_status,
                )
        return None

    # requested_lang == 'en'
    if is_tc_only_case:
        return None
    if html_path.exists():
        return RenderSource(
            lang="en",
            path=html_path,
            source_kind="html",
            upstream_status=upstream_status,
        )
    if gen_html_path.exists():
        return RenderSource(
            lang="en",
            path=gen_html_path,
            source_kind="generated.html",
            upstream_status=upstream_status,
        )
    return None


# ---------------------------------------------------------------------------
# render_case_body — dispatch on case_row['html_generated_from']
# ---------------------------------------------------------------------------


def _to_bcp47(lang: str) -> str:
    """Map internal lang codes to BCP-47 for HTML lang= attribute.

    Design §9 line 249: <article lang="{{ body_lang | bcp47 }}">.
    'en' → 'en'. 'tc' → 'zh-Hant' (Traditional Chinese script tag,
    which is what our :lang(zh-Hant) CSS selector targets).
    """
    return "zh-Hant" if lang == "tc" else lang


def _extract_body_inner(sanitized_html: str) -> str:
    """Return the inner HTML of ``<body>``.

    ``sanitize_body`` always emits a full document (lxml.html.fromstring
    auto-wraps fragments). We strip the outer shell here so the render
    output is just the case content, ready for the <article> wrapper.
    """
    if not sanitized_html:
        return ""
    tree = lxml_html.fromstring(sanitized_html)
    body = tree.find(".//body")
    target = body if body is not None else tree
    parts: list[str] = []
    if target.text:
        parts.append(target.text)
    for child in target:
        parts.append(
            lxml_html.tostring(child, encoding="unicode", method="html")
        )
    return "".join(parts)


def _render_native_hklii(html_bytes: bytes) -> str:
    """Native HKLII shape: full <html><body> with <form> wrapper, inline
    styles, <link> stylesheets. Sanitizer handles all of that; we just
    peel off the outer shell.
    """
    return _extract_body_inner(sanitize_body(html_bytes))


def _render_generated_fragment(html_bytes: bytes) -> str:
    """Pandoc-generated fragment: bare <p>...</p> chain. lxml auto-wraps
    in <html><body> during parse; we then peel the shell same as native.
    """
    return _extract_body_inner(sanitize_body(html_bytes))


def _compute_format_digest(output_root: str | Path, case_row: dict) -> str:
    """Hash of ``(has_html, has_tc_html, has_generated_html)`` for the case.

    Invalidates the render cache when a new sibling arrives — even if
    the currently-chosen source is byte-unchanged. Design §5 line 117:
    'render `.generated.html`-only case → cache → create `.html` sibling
    → re-request must re-render to native.'
    """
    court = case_row["court"]
    year = case_row["year"]
    number = case_row["number"]
    stem = f"{court}_{year}_{number}"
    d = Path(output_root) / court / str(year)
    flags = (
        (d / f"{stem}.html").exists(),
        (d / f"{stem}.tc.html").exists(),
        (d / f"{stem}.generated.html").exists(),
    )
    return hashlib.sha256(repr(flags).encode()).hexdigest()[:16]


@functools.lru_cache(maxsize=256)
def _cached_render_body(
    path_str: str,
    mtime_ns: int,
    format_digest: str,
    sanitizer_version: str,
    lang: str,
    generated_from: str,
) -> str:
    """Actual render pipeline — LRU-cached.

    Key components (design §5 line 117):
      - path_str + mtime_ns: source-file identity
      - format_digest: sibling availability (invalidates on new .html
        arriving next to a chosen .generated.html)
      - sanitizer_version: pipeline-code invariant
      - lang: passthrough (part of the wrapped output)
      - generated_from: dispatch choice (native vs pandoc-fragment)

    ``generated_from`` is a string not None|str so it hashes cleanly
    ('' means native path).
    """
    with open(path_str, "rb") as f:
        html_bytes = f.read()
    if generated_from:
        body_inner = _render_generated_fragment(html_bytes)
    else:
        body_inner = _render_native_hklii(html_bytes)
    lang_attr = _to_bcp47(lang)
    return f'<article lang="{lang_attr}">{body_inner}</article>'


def render_case_body(
    render_source: RenderSource | None,
    case_row: dict,
    output_root: str | Path,
) -> str:
    """Render a case body as sanitized HTML wrapped in ``<article>``.

    Dispatch (design §5 line 121):
      - ``case_row['html_generated_from']`` is truthy (any of 'doc',
        'rtf', 'pdf') → ``_render_generated_fragment``
      - else (native HKLII HTML) → ``_render_native_hklii``

    Cached (design §5 line 117): the underlying
    :func:`_cached_render_body` is ``functools.lru_cache(maxsize=256)``
    keyed on ``(sanitizer_version, format_digest, source_path,
    source_mtime, lang, generated_from)``. Format digest hashes the
    presence of all three potential body files, so a new sibling
    invalidates the cache even when the currently-chosen source is
    byte-unchanged.

    Wraps in ``<article lang="{bcp47}">`` per design §9 line 249.
    Missing ``render_source`` (route couldn't find a body → 404-shape)
    yields ``<article lang="en"></article>`` so the template still has
    a shell to render around.
    """
    if render_source is None:
        return '<article lang="en"></article>'

    format_digest = _compute_format_digest(output_root, case_row)
    mtime_ns = render_source.path.stat().st_mtime_ns
    generated_from = case_row.get("html_generated_from") or ""
    return _cached_render_body(
        path_str=str(render_source.path),
        mtime_ns=mtime_ns,
        format_digest=format_digest,
        sanitizer_version=_SANITIZER_VERSION,
        lang=render_source.lang,
        generated_from=generated_from,
    )
