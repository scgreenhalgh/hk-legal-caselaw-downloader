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

import sqlite3

from hklii_downloader.viewer.schema import create_schema
from hklii_downloader.viewer.search import (
    BodySource,
    BuildIndexResult,
    IndexResult,
    atomic_swap,
    body_sha256,
    build_index,
    discover_body_sources,
    extract_plaintext,
    index_case,
)


# Minimal cases-table DDL matching the shipped shape for the columns
# index_case reads. Phase 6 will add a schema-drift contract test.
_CP_CASES_MINIMAL_DDL = """
CREATE TABLE cases (
    court   TEXT NOT NULL,
    year    INTEGER NOT NULL,
    number  INTEGER NOT NULL,
    neutral TEXT NOT NULL,
    title   TEXT NOT NULL,
    date    TEXT NOT NULL,
    lang    TEXT NOT NULL DEFAULT 'en',
    PRIMARY KEY (court, year, number)
);
"""


def _mk_cp(tmp_path: Path) -> sqlite3.Connection:
    """Fresh writer checkpoint.db with the minimal cases table."""
    conn = sqlite3.connect(str(tmp_path / "checkpoint.db"))
    conn.execute(_CP_CASES_MINIMAL_DDL)
    conn.commit()
    return conn


def _mk_vw(tmp_path: Path) -> sqlite3.Connection:
    """Fresh writer viewer.db with the shipped schema."""
    conn = sqlite3.connect(str(tmp_path / "viewer.db"))
    create_schema(conn)
    return conn


def _seed_case(
    cp: sqlite3.Connection,
    case_key: str,
    *,
    title: str = "HKSAR v Test",
    date: str = "2020-01-01",
    lang: str = "en",
) -> None:
    court, year, num = case_key.split("/")
    cp.execute(
        "INSERT INTO cases (court, year, number, neutral, title, date, lang) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [court, int(year), int(num), f"[{year}] TEST {num}", title, date, lang],
    )
    cp.commit()


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


# ---------------------------------------------------------------------------
# extract_plaintext + body_sha256 — index-time text preparation
# ---------------------------------------------------------------------------


def test_extract_plaintext_returns_body_text_only() -> None:
    """extract_plaintext yields prose from a real HKLII HTML sample."""
    html = (
        b"<html><head><script>evil()</script></head>"
        b"<body><p>The defendant argued.</p></body></html>"
    )
    result = extract_plaintext(html)
    assert "defendant argued" in result
    assert "evil()" not in result


def test_extract_plaintext_normalizes_whitespace() -> None:
    """Multiple whitespace runs collapse to single space. Leading/trailing
    stripped. Makes body_sha256 stable against source-format churn.
    """
    html = "<p>  hello\n\n\n   world  </p>"
    assert extract_plaintext(html) == "hello world"


def test_extract_plaintext_preserves_cjk() -> None:
    """UTF-8 bytes decoded correctly (via iter_text_nodes's utf-8 decode)."""
    html = "<p>香港特別行政區 終審法院</p>".encode("utf-8")
    result = extract_plaintext(html)
    assert result == "香港特別行政區 終審法院"


def test_extract_plaintext_accepts_str_and_bytes() -> None:
    html = "<p>hello world</p>"
    assert extract_plaintext(html) == "hello world"
    assert extract_plaintext(html.encode("utf-8")) == "hello world"


def test_extract_plaintext_empty_body_returns_empty_string() -> None:
    """L5: empty body is a legitimate answer (case with no content) —
    downstream code can check for this before writing an empty FTS row.
    """
    assert extract_plaintext("<html><body></body></html>") == ""


def test_body_sha256_returns_64_char_hex() -> None:
    """SHA-256 hex digest is 64 chars."""
    sha = body_sha256("hello world")
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


def test_body_sha256_is_deterministic() -> None:
    """Same input → same digest. Basis of the incremental-diff check."""
    assert body_sha256("hello world") == body_sha256("hello world")


def test_body_sha256_differs_on_content_change() -> None:
    """Different input → different digest. Guards against index staleness."""
    assert body_sha256("hello world") != body_sha256("hello worm")


def test_body_sha256_empty_string_is_deterministic() -> None:
    """The empty-body sha is a stable sentinel — an index row with this
    sha means 'we indexed an empty body' (as opposed to 'never indexed').
    """
    sha_empty = body_sha256("")
    assert (
        sha_empty
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# index_case — the single-case orchestrator
# ---------------------------------------------------------------------------


_FIXED_NOW = "2026-07-07T12:00:00Z"


def test_index_case_returns_no_case_row_when_case_absent(tmp_path: Path) -> None:
    """cp.cases has no row for case_key → skip, no writes to viewer.db."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    result = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert result.action == "no_case_row"
    assert result.langs_indexed == ()
    assert vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone() == (0,)
    cp.close()
    vw.close()


def test_index_case_returns_no_body_when_no_files_on_disk(
    tmp_path: Path,
) -> None:
    """cp.cases has the row, but no HTML files → skip."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32")
    result = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert result.action == "no_body_on_disk"
    assert vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone() == (0,)
    cp.close()
    vw.close()


def test_index_case_indexes_en_only_case_end_to_end(tmp_path: Path) -> None:
    """.html present → row inserted, FTS MATCH finds the body."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>The defendant argued a subtle point.</p>")

    result = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)

    assert result.action == "indexed"
    assert result.langs_indexed == ("en",)
    # fts_cases row exists with the expected shape
    row = vw.execute(
        "SELECT case_key, lang, court, year, number, "
        "body_source, indexed_at, body_sha256 "
        "FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()
    assert row[0] == "hkcfa/2020/32"
    assert row[1] == "en"
    assert row[2] == "hkcfa"
    assert row[3] == 2020
    assert row[4] == 32
    assert row[5] == "html"
    assert row[6] == _FIXED_NOW
    assert len(row[7]) == 64  # sha256 hex
    # case_bodies row exists
    body_row = vw.execute(
        "SELECT title, body FROM case_bodies WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()
    assert body_row is not None
    assert "defendant argued" in body_row[1]
    # FTS MATCH finds it (trigger sync)
    matched = vw.execute(
        "SELECT c.case_key FROM fts_body b "
        "JOIN case_bodies c ON c.id = b.rowid "
        "WHERE fts_body MATCH ?",
        ["defendant"],
    ).fetchone()
    assert matched == ("hkcfa/2020/32",)
    cp.close()
    vw.close()


def test_index_case_indexes_bilingual_case_two_rows(tmp_path: Path) -> None:
    """.html + .tc.html both present → two fts_cases rows (en + tc)."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>English body text</p>")
    _touch(paths["tc.html"], "<p>中文譯本判決書</p>")

    result = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)

    assert result.action == "indexed"
    assert sorted(result.langs_indexed) == ["en", "tc"]
    langs = {
        r[0]
        for r in vw.execute(
            "SELECT lang FROM fts_cases WHERE case_key = ?",
            ["hkcfa/2020/32"],
        )
    }
    assert langs == {"en", "tc"}
    # Body_source tags are per-source
    sources_by_lang = dict(
        vw.execute(
            "SELECT lang, body_source FROM fts_cases WHERE case_key = ?",
            ["hkcfa/2020/32"],
        ).fetchall()
    )
    assert sources_by_lang == {"en": "html", "tc": "tc.html"}
    # TC MATCH finds the Chinese body (3+ chars — trigram lower bound)
    matched = vw.execute(
        "SELECT c.case_key FROM fts_body b "
        "JOIN case_bodies c ON c.id = b.rowid "
        "WHERE fts_body MATCH ?",
        ["中文譯本"],
    ).fetchone()
    assert matched == ("hkcfa/2020/32",)
    cp.close()
    vw.close()


def test_index_case_returns_unchanged_when_sha_matches(tmp_path: Path) -> None:
    """Second call with unchanged body → action='unchanged', no upsert."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>Original body content</p>")

    # First index — writes
    r1 = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso="2026-01-01T00:00:00Z")
    assert r1.action == "indexed"
    # Second index — same content, so sha matches
    r2 = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso="2026-02-01T00:00:00Z")
    assert r2.action == "unchanged"
    assert r2.langs_unchanged == ("en",)
    # indexed_at is not bumped
    stamp = vw.execute(
        "SELECT indexed_at FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()
    assert stamp[0] == "2026-01-01T00:00:00Z"  # first-call stamp preserved
    cp.close()
    vw.close()


# ---------------------------------------------------------------------------
# build_index — iterate cp.cases and index each
# ---------------------------------------------------------------------------


def test_build_index_empty_cases_returns_all_zeros(tmp_path: Path) -> None:
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    result = build_index(vw, cp, tmp_path, now_iso=_FIXED_NOW)
    assert result == BuildIndexResult(
        processed=0, indexed=0, unchanged=0, no_body=0
    )
    cp.close()
    vw.close()


def test_build_index_indexes_all_cases_with_bodies(tmp_path: Path) -> None:
    """3 cases: EN-only, bilingual, and one with no body on disk."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/1", lang="en")
    _seed_case(cp, "hkcfa/2020/2", lang="en")
    _seed_case(cp, "hkcfa/2020/3", lang="en")  # will have no body

    p1 = _mk_paths(tmp_path, "hkcfa/2020/1")
    _touch(p1["html"], "<p>english body</p>")

    p2 = _mk_paths(tmp_path, "hkcfa/2020/2")
    _touch(p2["html"], "<p>english bilingual body</p>")
    _touch(p2["tc.html"], "<p>中文譯本文字</p>")

    result = build_index(vw, cp, tmp_path, now_iso=_FIXED_NOW)
    assert result.processed == 3
    assert result.indexed == 2
    assert result.unchanged == 0
    assert result.no_body == 1

    # Bilingual case yields 2 fts_cases rows; EN-only yields 1; total = 3
    fts_count = vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone()
    assert fts_count == (3,)
    cp.close()
    vw.close()


def test_build_index_second_run_reports_unchanged(tmp_path: Path) -> None:
    """Same corpus, second call → sha matches → unchanged."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/1", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/1")
    _touch(paths["html"], "<p>stable body</p>")

    r1 = build_index(vw, cp, tmp_path, now_iso="2026-01-01T00:00:00Z")
    assert r1.indexed == 1 and r1.unchanged == 0

    r2 = build_index(vw, cp, tmp_path, now_iso="2026-02-01T00:00:00Z")
    assert r2.indexed == 0 and r2.unchanged == 1
    cp.close()
    vw.close()


def test_build_index_empty_courts_list_is_a_noop_not_full_rebuild(
    tmp_path: Path,
) -> None:
    """L5 ambiguous-state: courts=[] and courts=None must not be conflated.

    An empty courts list is a legitimate 'no courts to index' signal
    (e.g. after intersecting a --courts flag with the shipped court list
    and finding no overlap). Falling through to a full-corpus rebuild
    silently kicks off a 20-min rebuild instead of a no-op.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/1")
    _seed_case(cp, "hkca/2020/1")

    result = build_index(vw, cp, tmp_path, courts=[], now_iso=_FIXED_NOW)
    assert result.processed == 0
    assert result.indexed == 0
    # No cases were written
    assert vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone() == (0,)
    cp.close()
    vw.close()


def test_build_index_courts_filter_restricts_processing(
    tmp_path: Path,
) -> None:
    """courts=['hkcfa'] processes ONLY CFA cases (idx_court hits)."""
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/1")
    _seed_case(cp, "hkca/2020/1")
    _seed_case(cp, "hkcfi/2020/1")

    p_cfa = _mk_paths(tmp_path, "hkcfa/2020/1")
    _touch(p_cfa["html"], "<p>cfa body</p>")
    p_ca = _mk_paths(tmp_path, "hkca/2020/1")
    _touch(p_ca["html"], "<p>ca body</p>")

    result = build_index(
        vw, cp, tmp_path, courts=["hkcfa"], now_iso=_FIXED_NOW
    )
    assert result.processed == 1
    assert result.indexed == 1

    courts_indexed = {
        r[0] for r in vw.execute("SELECT court FROM fts_cases")
    }
    assert courts_indexed == {"hkcfa"}
    cp.close()
    vw.close()


# ---------------------------------------------------------------------------
# atomic_swap — os.replace wrapper for viewer.db.new → viewer.db
# ---------------------------------------------------------------------------


def test_atomic_swap_replaces_existing_dst(tmp_path: Path) -> None:
    """atomic_swap(src, dst) → dst has src's content, src is gone."""
    src = tmp_path / "viewer.db.new"
    dst = tmp_path / "viewer.db"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    atomic_swap(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


def test_atomic_swap_creates_dst_when_absent(tmp_path: Path) -> None:
    """atomic_swap(src, dst) → dst created if it didn't exist."""
    src = tmp_path / "viewer.db.new"
    dst = tmp_path / "viewer.db"
    src.write_bytes(b"new")
    atomic_swap(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


def test_atomic_swap_missing_src_raises(tmp_path: Path) -> None:
    """L1 loud-failure: nonexistent src is a real error, not silent no-op."""
    src = tmp_path / "does-not-exist"
    dst = tmp_path / "viewer.db"
    with pytest.raises(FileNotFoundError):
        atomic_swap(src, dst)


def test_atomic_swap_accepts_str_and_pathlib(tmp_path: Path) -> None:
    src = tmp_path / "viewer.db.new"
    dst = tmp_path / "viewer.db"
    src.write_bytes(b"data")
    atomic_swap(str(src), str(dst))
    assert dst.read_bytes() == b"data"


def test_index_case_prunes_bilingual_half_when_file_disappears(
    tmp_path: Path,
) -> None:
    """Regression: bilingual case had both .html + .tc.html on the first
    run. TC file is later removed (upstream retracted, scraper cleanup,
    manual delete). Second index_case must delete the stale TC row from
    fts_cases + case_bodies, and the AFTER DELETE trigger must clean up
    fts_body. FTS MATCH against the old TC body must return 0.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>english body</p>")
    _touch(paths["tc.html"], "<p>中文譯本 uniquechinese</p>")

    # First run — both langs indexed
    r1 = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert r1.action == "indexed"
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (2,)

    # TC file deleted upstream
    paths["tc.html"].unlink()

    # Second run — stale TC row must be pruned
    r2 = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert r2.action == "indexed"
    assert r2.langs_pruned == ("tc",)

    remaining = {
        r[0]
        for r in vw.execute(
            "SELECT lang FROM fts_cases WHERE case_key = ?",
            ["hkcfa/2020/32"],
        )
    }
    assert remaining == {"en"}

    bodies = vw.execute(
        "SELECT COUNT(*) FROM case_bodies WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()
    assert bodies == (1,)

    # FTS MATCH for the pruned TC body returns 0 (trigger cleanup)
    hits = vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["uniquechinese"],
    ).fetchone()
    assert hits == (0,)
    cp.close()
    vw.close()


def test_index_case_prunes_all_rows_when_case_row_disappears_from_cp(
    tmp_path: Path,
) -> None:
    """Case was in cp.cases + indexed. Case is later removed from cp.cases
    (scraper marks it dropped, or a --clean pass wipes it). Next
    index_case must return 'indexed' with langs_pruned covering what
    was in viewer.db — not 'no_case_row' with no cleanup, which would
    leave orphan FTS rows that keep matching search queries.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>orphaned body uniqueorphan</p>")

    # First run — indexed
    index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone() == (1,)

    # Remove the case from cp.cases
    cp.execute("DELETE FROM cases WHERE court='hkcfa' AND year=2020 AND number=32")
    cp.commit()

    # Second run — must prune
    r = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)
    assert r.action == "indexed"
    assert r.langs_pruned == ("en",)

    assert vw.execute("SELECT COUNT(*) FROM fts_cases").fetchone() == (0,)
    assert vw.execute("SELECT COUNT(*) FROM case_bodies").fetchone() == (0,)
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["uniqueorphan"],
    ).fetchone() == (0,)
    cp.close()
    vw.close()


def test_index_case_replaces_row_when_body_changes(tmp_path: Path) -> None:
    """Same case_key, changed content → sha differs → row replaced. FTS
    reflects the NEW body; old body no longer matches.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/32", lang="en")
    paths = _mk_paths(tmp_path, "hkcfa/2020/32")
    _touch(paths["html"], "<p>oldbodycontent unique1</p>")
    index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso="2026-01-01T00:00:00Z")

    # Change body on disk
    _touch(paths["html"], "<p>newbodycontent unique2</p>")
    r2 = index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso="2026-02-01T00:00:00Z")

    assert r2.action == "indexed"
    assert r2.langs_indexed == ("en",)
    # Only one row (not two)
    count = vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()
    assert count == (1,)
    # New sha != old
    sha = vw.execute(
        "SELECT body_sha256 FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone()[0]
    assert sha == body_sha256("newbodycontent unique2")
    # FTS: old marker gone, new marker present
    old_hits = vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["unique1"],
    ).fetchone()
    new_hits = vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["unique2"],
    ).fetchone()
    assert old_hits == (0,)
    assert new_hits == (1,)
    cp.close()
    vw.close()
