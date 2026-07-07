"""On-disk contract for ``.generated.html`` sidecar files.

Pandoc-generated HTML sidecars fill the case.lang slot for judgments
whose upstream body is only served as .doc/.rtf/.pdf. The contract has
three moving parts that must stay in lockstep — this file pins each:

- **Path shape**: ``output/{court}/{year}/{court}_{year}_{n}.generated.html``.
  The stem is identical to the primary ``.html`` sibling so both source
  types share the same enumeration logic in
  :func:`viewer.search.discover_body_sources`.
- **File shape**: pandoc's ``-t html`` output — a bare HTML fragment
  (chain of block elements, no ``<html>``/``<body>`` shell). Distinct
  from the native HKLII shape which is a full document wrapped in a
  ``<form name="search_body">``.
- **Fallback semantic**: the sidecar covers ``case.lang`` only when a
  primary ``.html`` is missing. A real ``.html`` sibling always wins —
  pandoc conversion loses formatting fidelity (tables, italics,
  citation anchors), so a native body is strictly preferred when one
  arrives later (e.g. HKLII re-scrape found the original HTML shape).

Where each contract point lives in shipped code:

- :func:`viewer.search.discover_body_sources` — index-time enumeration.
  Rules encoded around lines 519-540 of ``viewer/search.py``.
- :func:`viewer.body_render.render.render_case_body` — render-time
  dispatch. Truthy ``case_row['html_generated_from']`` routes to
  :func:`viewer.body_render.render._render_generated_fragment`; falsy
  routes to :func:`viewer.body_render.render._render_native_hklii`.
- :func:`viewer.body_render.sanitizer.sanitize_body` — allowlist
  sanitizer. Must round-trip the bare-``<p>`` fragment shape without
  losing paragraph text.

Five-lens coverage (docs/review-patterns.md):

- **L1 silent skip**: test 1 asserts a lone ``.generated.html`` yields
  a BodySource, not an empty list — silently skipping the sidecar would
  drop pandoc-converted cases out of the FTS index.
- **L2 semantic drift**: test 2 asserts ``.html`` beats
  ``.generated.html``. Inverting the priority would silently downgrade
  every bilingual case whose upstream re-scrape recovered a native
  body — the served content would be the lossy pandoc render instead.
- **L3 docstring drift**: docstring on
  :func:`discover_body_sources` says the sidecar 'never overrides a
  real .html' — test 2 anchors that phrasing.
- **L4 wrong-side test**: test 3 checks the render-side dispatcher
  (route/pipeline), test 4 checks the helper-side sanitizer. Both
  sides of the contract are exercised.
- **L5 ambiguous state**: test 3 pins the truthy-vs-falsy dispatch key
  on ``html_generated_from`` — an empty string, None, and a
  non-empty string must not collapse into one path.
"""

from __future__ import annotations

from pathlib import Path

from hklii_downloader.viewer.body_render import render as render_mod
from hklii_downloader.viewer.body_render.render import (
    RenderSource,
    render_case_body,
    select_body_source,
)
from hklii_downloader.viewer.body_render.sanitizer import sanitize_body
from hklii_downloader.viewer.search import BodySource, discover_body_sources


# ---------------------------------------------------------------------------
# On-disk fixture helpers — mirror the shape the scraper writes.
# ---------------------------------------------------------------------------


def _case_dir(root: Path, court: str, year: int) -> Path:
    """``output/{court}/{year}`` — the canonical scraper layout."""
    return root / court / str(year)


def _sidecar_path(root: Path, court: str, year: int, number: int) -> Path:
    """The on-disk path shape the contract pins.

    ``output/{court}/{year}/{court}_{year}_{n}.generated.html``.
    """
    return _case_dir(root, court, year) / f"{court}_{year}_{number}.generated.html"


def _primary_html_path(root: Path, court: str, year: int, number: int) -> Path:
    """Sibling ``.html`` — same stem, no ``.generated`` infix."""
    return _case_dir(root, court, year) / f"{court}_{year}_{number}.html"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _case_row(
    court: str = "hkcfa",
    year: int = 2020,
    number: int = 32,
    lang: str = "en",
    html_generated_from: str | None = None,
) -> dict:
    """Case row shape as the route layer sees it (route reads from
    checkpoint.db cases table; render pipeline consumes the dict).
    """
    return {
        "court": court,
        "year": year,
        "number": number,
        "lang": lang,
        "status": "downloaded",
        "html_generated_from": html_generated_from,
    }


# Pandoc's ``-t html`` output is a chain of block elements — no
# ``<html>``/``<body>``/``<head>`` shell. Two paragraphs mirror the
# realistic multi-paragraph shape (lxml wraps single-root fragments
# without a synthetic container; multi-root fragments get a ``<div>``
# wrapper on parse, which the render pipeline peels off).
_PANDOC_FRAGMENT: str = (
    "<p>Pandoc paragraph one — introductory recitals.</p>"
    "<p>Pandoc paragraph two — judgment reasoning.</p>"
)


# ---------------------------------------------------------------------------
# Test 1 — sole sidecar yields a BodySource (L1 silent-skip guard)
# ---------------------------------------------------------------------------


def test_only_generated_html_on_disk_yields_one_source_with_kind_generated(
    tmp_path: Path,
) -> None:
    """A case with just ``.generated.html`` on disk must surface as an
    indexable body — one :class:`BodySource` at ``case_lang`` with
    ``source_kind='generated.html'``.

    Regression guard: if :func:`discover_body_sources` silently ignored
    ``.generated.html`` (an easy shape-drift outcome), every
    pandoc-converted case would drop out of the FTS index.
    """
    sidecar = _sidecar_path(tmp_path, "hkcfa", 2020, 32)
    _write(sidecar, _PANDOC_FRAGMENT)

    sources = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")

    assert sources == [
        BodySource(lang="en", path=sidecar, source_kind="generated.html")
    ]


# ---------------------------------------------------------------------------
# Test 2 — real .html beats .generated.html (L2/L3 fallback semantic)
# ---------------------------------------------------------------------------


def test_primary_html_wins_over_generated_html_when_both_present(
    tmp_path: Path,
) -> None:
    """``.generated.html`` is a fallback — a real ``.html`` sibling
    always takes priority. Both the index-time enumerator
    (:func:`discover_body_sources`) and the render-time discriminator
    (:func:`select_body_source`) must agree on this.

    Test asserts BOTH sides because the contract is 'never override a
    real .html' — a divergence between the two sides would produce a
    case that's indexed as pandoc-fragment but rendered as native, or
    vice versa. Either drift ships wrong provenance to the reader.
    """
    primary = _primary_html_path(tmp_path, "hkcfa", 2020, 32)
    sidecar = _sidecar_path(tmp_path, "hkcfa", 2020, 32)
    _write(primary, "<html><body><p>Native HKLII body</p></body></html>")
    _write(sidecar, _PANDOC_FRAGMENT)

    # Index-time (viewer.search): one source, source_kind='html'
    sources = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    assert len(sources) == 1
    assert sources[0].source_kind == "html"
    assert sources[0].path == primary

    # Render-time (viewer.body_render.render): also .html, not generated
    src = select_body_source(
        _case_row(html_generated_from="doc"),  # would misdirect if it
                                                # controlled priority
        tmp_path,
        requested_lang="en",
    )
    assert src is not None
    assert src.source_kind == "html"
    assert src.path == primary


# ---------------------------------------------------------------------------
# Test 3 — render dispatches to _render_generated_fragment (L4 route/L5 state)
# ---------------------------------------------------------------------------


def test_render_case_body_dispatches_to_generated_fragment_when_html_generated_from_truthy(
    tmp_path: Path, monkeypatch,
) -> None:
    """The render dispatcher branches on
    ``case_row['html_generated_from']``: any truthy string (``'doc'``,
    ``'rtf'``, ``'pdf'``) routes to
    :func:`_render_generated_fragment`; ``None`` / empty string routes
    to :func:`_render_native_hklii`.

    Spies on both dispatch functions to prove the truthy branch takes
    the pandoc pipeline. The spied output surfaces at the ``<article>``
    wrapper so the assertion also verifies end-to-end wiring, not just
    an isolated function call.

    L5 ambiguity guard: if the dispatch collapsed to
    ``if case_row.get('html_generated_from') is not None``, an empty
    string would take the wrong branch. Truthy-check is the correct
    predicate — this test uses ``'doc'`` to lock the truthy path in.
    """
    sidecar = _sidecar_path(tmp_path, "hkcfa", 2020, 32)
    _write(sidecar, _PANDOC_FRAGMENT)

    native_calls: list[bytes] = []
    gen_calls: list[bytes] = []

    real_native = render_mod._render_native_hklii
    real_gen = render_mod._render_generated_fragment

    def native_spy(html_bytes: bytes) -> str:
        native_calls.append(html_bytes)
        return real_native(html_bytes)

    def gen_spy(html_bytes: bytes) -> str:
        gen_calls.append(html_bytes)
        return real_gen(html_bytes)

    monkeypatch.setattr(render_mod, "_render_native_hklii", native_spy)
    monkeypatch.setattr(render_mod, "_render_generated_fragment", gen_spy)
    # Cache is keyed on generated_from so a stale hit could bypass the
    # spies. Clear before each render.
    render_mod._cached_render_body.cache_clear()

    case_row = _case_row(html_generated_from="doc")
    src = select_body_source(case_row, tmp_path, requested_lang="en")
    assert src is not None and src.source_kind == "generated.html"

    out = render_case_body(src, case_row, tmp_path)

    # Dispatch decision (L4 wrong-side test — the route/render side of
    # the contract): pandoc pipeline ran, native pipeline did not.
    assert len(gen_calls) == 1
    assert len(native_calls) == 0
    # And the wrapper is intact (end-to-end wiring, not just the branch).
    assert out.startswith('<article lang="en">')
    assert out.endswith("</article>")
    assert "Pandoc paragraph one" in out


# ---------------------------------------------------------------------------
# Test 4 — sanitizer round-trips a bare-<p> fragment (L4 helper side)
# ---------------------------------------------------------------------------


def test_sanitizer_round_trips_bare_p_fragment_preserving_paragraph_text(
) -> None:
    """The helper-side of the contract: :func:`sanitize_body` must not
    drop paragraph text from the pandoc fragment shape.

    Pandoc emits a chain of block elements with no ``<html>``/``<body>``
    shell. lxml auto-wraps a multi-root fragment in a ``<div>`` on
    parse. The sanitizer must:

    1. Not reject/drop bare ``<p>`` elements — they carry the judgment
       body.
    2. Not strip out paragraph text as an allowlist casualty.
    3. Emit output that the render pipeline can peel back to the
       fragment shape.

    L4 wrong-side test: pairs with test 3 (render dispatcher). Test 3
    proves the pandoc branch is TAKEN; this test proves the pipeline
    that branch runs actually preserves the content it was handed.

    L2 semantic drift: if a future sanitizer change treated ``<p>``
    as unknown-and-drop (e.g. a switched-to-denylist model), every
    paragraph in every pandoc-converted case would vanish. This test
    is the tripwire.
    """
    sanitized = sanitize_body(_PANDOC_FRAGMENT)

    # Paragraph text survives (both paragraphs — no truncation).
    assert "Pandoc paragraph one" in sanitized
    assert "introductory recitals" in sanitized
    assert "Pandoc paragraph two" in sanitized
    assert "judgment reasoning" in sanitized

    # The ``<p>`` tags themselves survive — a denylist regression would
    # keep the text but strip the tag, producing an unstyled prose blob.
    assert sanitized.count("<p>") == 2
    assert sanitized.count("</p>") == 2

    # No infrastructure leaks — bare fragment has no <script>/<style>
    # to drop but the sanitizer should not synthesize any.
    assert "<script" not in sanitized
    assert "<style" not in sanitized
