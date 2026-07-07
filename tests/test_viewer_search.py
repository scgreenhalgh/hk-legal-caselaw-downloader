"""Tests for viewer/search.py — index-build helpers.

Phase 2.4: discover_body_sources implements the bilingual sibling probe
per design §4 line 82. Rules (index-time enumeration, distinct from the
render-time discriminator in §5):

- ``{stem}.tc.html`` is unambiguously TC (regardless of case.lang)
- ``{stem}.html`` is EN when a .tc.html sibling exists (bilingual pair);
  otherwise it reflects case.lang
- ``{stem}.generated.html`` is a fallback for case.lang when the primary
  source is missing — never overrides a real .html

The result is one BodySource per (case, language) present on disk.
An FTS row gets built for each element the list returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hklii_downloader.viewer.search import BodySource, discover_body_sources


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


def test_bilingual_pair_yields_en_and_tc(tmp_path: Path) -> None:
    """{stem}.html + {stem}.tc.html both present → two BodySources.

    Order: TC first (from .tc.html), then EN (from .html). The order
    itself doesn't matter for downstream — the FTS indexer iterates the
    list — but a stable order helps test determinism.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    _touch(paths["tc.html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    langs = sorted(s.lang for s in result)
    assert langs == ["en", "tc"]
    en = next(s for s in result if s.lang == "en")
    tc = next(s for s in result if s.lang == "tc")
    assert en.source_kind == "html" and en.path == paths["html"]
    assert tc.source_kind == "tc.html" and tc.path == paths["tc.html"]


def test_en_only_case_yields_single_en_source(tmp_path: Path) -> None:
    """Case with just .html and case_lang='en' → one BodySource(en, html)."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    assert result == [
        BodySource(lang="en", path=paths["html"], source_kind="html")
    ]


def test_tc_only_case_yields_single_tc_source_from_bare_html(
    tmp_path: Path,
) -> None:
    """TC-only court (e.g. hkmagc): case_lang='tc' + only .html present.

    L2 semantic-drift fix (§4 line 82): the sibling probe checks the
    filesystem, not case.lang, to determine bilingual-ness. But when
    the only file is bare .html AND case.lang='tc', that .html body
    IS the TC content.
    """
    paths = _mk_paths(tmp_path, "hkmagc/2014/6")
    _touch(paths["html"])
    result = discover_body_sources(tmp_path, "hkmagc/2014/6", case_lang="tc")
    assert result == [
        BodySource(lang="tc", path=paths["html"], source_kind="html")
    ]


def test_only_tc_html_yields_single_tc_source(tmp_path: Path) -> None:
    """Unusual (but possible): only .tc.html present, no .html. Case
    lang could still be 'en' — the sibling probe reports what disk has.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["tc.html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    assert result == [
        BodySource(lang="tc", path=paths["tc.html"], source_kind="tc.html")
    ]


def test_generated_html_fallback_when_no_html(tmp_path: Path) -> None:
    """No .html and no .tc.html; .generated.html present → indexed as case_lang."""
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["generated.html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    assert result == [
        BodySource(
            lang="en",
            path=paths["generated.html"],
            source_kind="generated.html",
        )
    ]


def test_generated_html_ignored_when_html_present(tmp_path: Path) -> None:
    """.generated.html is a fallback — never overrides a real .html body.

    Design decision: the LibreOffice-rendered fallback is lower fidelity
    than the original HKLII HTML. If both exist, prefer the real one.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    _touch(paths["generated.html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    assert len(result) == 1
    assert result[0].source_kind == "html"


def test_generated_html_covers_missing_lang_in_bilingual_scenario(
    tmp_path: Path,
) -> None:
    """.generated.html only covers the case_lang position. If a bilingual
    sibling (.tc.html) exists but no .html, the .generated.html covers EN
    (case_lang='en') while .tc.html covers TC — two sources.
    """
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["tc.html"])
    _touch(paths["generated.html"])
    result = discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en")
    langs = sorted(s.lang for s in result)
    assert langs == ["en", "tc"]
    en = next(s for s in result if s.lang == "en")
    assert en.source_kind == "generated.html"


def test_nothing_on_disk_returns_empty(tmp_path: Path) -> None:
    """L5: no files → empty list. Distinct from 'file missing' failure —
    the case simply has no body to index yet (e.g. failed scrape).
    """
    assert discover_body_sources(tmp_path, "hkcfa/2020/32", case_lang="en") == []


def test_malformed_case_key_raises(tmp_path: Path) -> None:
    """Consistent with viewer/graph.appeal_chain."""
    with pytest.raises(ValueError):
        discover_body_sources(tmp_path, "onlyone/slash", case_lang="en")
    with pytest.raises(ValueError):
        discover_body_sources(tmp_path, "no-slashes", case_lang="en")


def test_accepts_str_and_pathlib_output_root(tmp_path: Path) -> None:
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"])
    for arg in (str(tmp_path), tmp_path):
        result = discover_body_sources(arg, "hkcfa/2020/32", case_lang="en")
        assert len(result) == 1
