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

from dataclasses import dataclass
from pathlib import Path


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
