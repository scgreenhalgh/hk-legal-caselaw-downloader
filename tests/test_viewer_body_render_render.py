"""Tests for viewer/body_render/render.py — render-time discriminator.

select_body_source picks ONE on-disk file for a (case, requested_lang)
pair. Distinct from viewer.search.discover_body_sources which enumerates
every language present at index time. Design doc §5 line 104-113.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hklii_downloader.viewer.body_render.render import (
    RenderSource,
    render_case_body,
    select_body_source,
)


def _touch(path: Path, content: str = "<html></html>") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mk_paths(root: Path, case_key: str) -> dict[str, Path]:
    court, year, num = case_key.split("/")
    stem = f"{court}_{year}_{num}"
    d = root / court / year
    return {
        "html": d / f"{stem}.html",
        "tc.html": d / f"{stem}.tc.html",
        "generated.html": d / f"{stem}.generated.html",
    }


def _case_row(
    court: str = "hkcfa",
    year: int = 2020,
    number: int = 32,
    lang: str = "en",
    status: str = "downloaded",
    html_generated_from: str | None = None,
) -> dict:
    return {
        "court": court,
        "year": year,
        "number": number,
        "lang": lang,
        "status": status,
        "html_generated_from": html_generated_from,
    }


# ---------------------------------------------------------------------------
# Bilingual case (case.lang='en', both .html and .tc.html present)
# ---------------------------------------------------------------------------


def test_bilingual_case_en_request_serves_html_at_en(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    _touch(paths["tc.html"])
    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    assert src is not None
    assert src.lang == "en"
    assert src.path == paths["html"]
    assert src.source_kind == "html"


def test_bilingual_case_tc_request_serves_tc_html_at_tc(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    _touch(paths["tc.html"])
    src = select_body_source(_case_row(), tmp_path, requested_lang="tc")
    assert src is not None
    assert src.lang == "tc"
    assert src.path == paths["tc.html"]
    assert src.source_kind == "tc.html"


# ---------------------------------------------------------------------------
# EN-only case (only .html, no .tc.html)
# ---------------------------------------------------------------------------


def test_en_only_case_en_request_serves_html(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    assert src is not None
    assert src.lang == "en"
    assert src.source_kind == "html"


def test_en_only_case_tc_request_returns_none(tmp_path: Path) -> None:
    """No .tc.html sibling and case_lang != tc → no TC body available."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    assert select_body_source(_case_row(), tmp_path, requested_lang="tc") is None


# ---------------------------------------------------------------------------
# TC-only case (case.lang='tc' or 'zh', only bare .html present with TC content)
# ---------------------------------------------------------------------------


def test_tc_only_case_tc_request_serves_bare_html_at_tc(tmp_path: Path) -> None:
    """Design §5: TC-only court cases have Chinese content in the bare
    .html (no .tc.html sibling). discriminator must recognize this.
    """
    paths = _mk_paths(tmp_path, "hkmagc/2014/6")
    _touch(paths["html"])
    src = select_body_source(
        _case_row(court="hkmagc", year=2014, number=6, lang="tc"),
        tmp_path,
        requested_lang="tc",
    )
    assert src is not None
    assert src.lang == "tc"
    assert src.source_kind == "html"


def test_tc_only_case_zh_legacy_lang_value_treated_as_tc(tmp_path: Path) -> None:
    """Design §5: case.lang='zh' is a legacy TC value. Discriminator
    treats 'zh' and 'tc' identically.
    """
    paths = _mk_paths(tmp_path, "hkmagc/2014/6")
    _touch(paths["html"])
    src = select_body_source(
        _case_row(court="hkmagc", year=2014, number=6, lang="zh"),
        tmp_path,
        requested_lang="tc",
    )
    assert src is not None
    assert src.lang == "tc"


def test_tc_only_case_en_request_returns_none(tmp_path: Path) -> None:
    """TC-only case has no EN body — a request for EN yields None
    (route renders 404 with formats-on-disk strip per design §5).
    """
    paths = _mk_paths(tmp_path, "hkmagc/2014/6")
    _touch(paths["html"])
    assert (
        select_body_source(
            _case_row(court="hkmagc", year=2014, number=6, lang="tc"),
            tmp_path,
            requested_lang="en",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Generated (pandoc-rendered) fallback
# ---------------------------------------------------------------------------


def test_generated_html_fallback_when_no_primary_body(tmp_path: Path) -> None:
    """.generated.html covers case.lang when no .html is present."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["generated.html"])
    src = select_body_source(
        _case_row(html_generated_from="doc"),
        tmp_path,
        requested_lang="en",
    )
    assert src is not None
    assert src.lang == "en"
    assert src.source_kind == "generated.html"


def test_generated_html_does_not_override_primary_html(tmp_path: Path) -> None:
    """Design §5: `.generated.html` is a fallback — never override .html."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    _touch(paths["generated.html"])
    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    assert src is not None
    assert src.source_kind == "html"  # not 'generated.html'


# ---------------------------------------------------------------------------
# Orphaned / status pass-through
# ---------------------------------------------------------------------------


def test_orphaned_upstream_status_propagates_to_render_source(
    tmp_path: Path,
) -> None:
    """upstream_status from cases.status is carried in RenderSource so
    the route can render the 'retracted from upstream' strip.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    src = select_body_source(
        _case_row(status="orphaned"),
        tmp_path,
        requested_lang="en",
    )
    assert src is not None
    assert src.upstream_status == "orphaned"


def test_downloaded_status_is_the_default_propagation(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    src = select_body_source(
        _case_row(status="downloaded"),
        tmp_path,
        requested_lang="en",
    )
    assert src is not None
    assert src.upstream_status == "downloaded"


# ---------------------------------------------------------------------------
# Empty disk / error cases
# ---------------------------------------------------------------------------


def test_no_body_files_on_disk_returns_none(tmp_path: Path) -> None:
    """L5: no on-disk files → None (route renders 404). Distinct from a
    raise, which would be a real config error.
    """
    assert (
        select_body_source(_case_row(), tmp_path, requested_lang="en") is None
    )


def test_invalid_requested_lang_raises(tmp_path: Path) -> None:
    """L1 loud-failure: unknown requested_lang is a route-layer bug, not
    silent None.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    with pytest.raises(ValueError, match="requested_lang"):
        select_body_source(_case_row(), tmp_path, requested_lang="fr")


def test_accepts_str_and_pathlib_output_root(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    for arg in (str(tmp_path), tmp_path):
        src = select_body_source(_case_row(), arg, requested_lang="en")
        assert src is not None


# ---------------------------------------------------------------------------
# render_case_body dispatch (native HKLII vs pandoc fragment)
# ---------------------------------------------------------------------------


def test_render_case_body_wraps_output_in_article_with_bcp47_lang(
    tmp_path: Path,
) -> None:
    """Design §9 line 249: <article lang="{{ body_lang | bcp47 }}"> wraps
    every rendered body. English maps to 'en'; TC maps to 'zh-Hant'.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<html><body><p>judgment prose</p></body></html>")

    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    out = render_case_body(src, _case_row())

    assert out.startswith('<article lang="en">')
    assert out.endswith("</article>")
    assert "judgment prose" in out


def test_render_case_body_tc_gets_zh_hant_lang(tmp_path: Path) -> None:
    """Bilingual TC body → article lang='zh-Hant' (BCP-47)."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["tc.html"], "<html><body><p>中文判決</p></body></html>")

    src = select_body_source(_case_row(), tmp_path, requested_lang="tc")
    out = render_case_body(src, _case_row())

    assert out.startswith('<article lang="zh-Hant">')
    assert "中文判決" in out


def test_render_case_body_native_path_when_html_generated_from_is_none(
    tmp_path: Path,
) -> None:
    """case_row.html_generated_from == None → native HKLII dispatch.
    Sanitizes a full-document HTML shape (has outer <html>/<body>/<form>).
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(
        paths["html"],
        # Real HKLII shape: form-wrapped body content
        "<html><body>"
        '<form name="search_body">'
        "<parties>HKSAR v CHAN</parties>"
        "<p>judgment prose here</p>"
        "</form>"
        "</body></html>",
    )
    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    out = render_case_body(src, _case_row(html_generated_from=None))

    assert "<form" not in out  # unwrapped by sanitizer
    assert "HKSAR v CHAN" in out
    assert "judgment prose" in out


def test_render_case_body_generated_path_when_html_generated_from_is_doc(
    tmp_path: Path,
) -> None:
    """case_row.html_generated_from == 'doc' → generated-fragment dispatch.
    Pandoc emits a bare fragment (no <html> shell); the renderer wraps it
    in <article> the same way.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(
        paths["generated.html"],
        # Pandoc fragment: bare paragraphs
        "<p>Pandoc-derived paragraph 1</p><p>Paragraph 2</p>",
    )
    row = _case_row(html_generated_from="doc")
    src = select_body_source(row, tmp_path, requested_lang="en")
    out = render_case_body(src, row)

    assert '<article lang="en">' in out
    assert "Paragraph 1" in out
    assert "Paragraph 2" in out


def test_render_case_body_none_source_returns_empty_article(
    tmp_path: Path,
) -> None:
    """No RenderSource (route hit 404 shape) → empty <article>."""
    out = render_case_body(None, _case_row())
    assert out == '<article lang="en"></article>' or out == ""


def test_render_case_body_empty_file_yields_empty_article(
    tmp_path: Path,
) -> None:
    """0-byte body file (matches iter_text_nodes empty guard chain) →
    the render pipeline must not crash; template gets an empty article.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "")

    src = select_body_source(_case_row(), tmp_path, requested_lang="en")
    out = render_case_body(src, _case_row())
    assert out == '<article lang="en"></article>'
