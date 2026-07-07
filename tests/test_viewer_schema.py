"""Tests for viewer/schema.py — viewer.db shape + FTS5 tokenizer behavior.

Two concerns:

1. Structural: create_schema produces the tables/indexes/triggers documented
   in the design, idempotently, in the right order.

2. Behavioral: the trigram tokenizer supports CJK 3-char match + the
   documented ≥3 char lower bound (Chinese 2-char queries return no
   matches — a design decision the UI surfaces upfront).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hklii_downloader.viewer.schema import (
    ALL_DDL,
    CASE_BODIES_TABLE_DDL,
    FTS_BODY_TABLE_DDL,
    FTS_CASES_TABLE_DDL,
    VIEWER_HUB_CACHE_DDL,
    create_schema,
)


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(tmp_path / "viewer.db"))


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_create_schema_is_idempotent(tmp_path: Path) -> None:
    """Running create_schema twice must not raise or duplicate anything."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    create_schema(conn)  # must not raise
    conn.close()


def test_all_expected_tables_created(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )
    }
    assert "viewer_hub_cache" in tables
    assert "fts_cases" in tables
    assert "case_bodies" in tables
    assert "fts_body" in tables
    conn.close()


def test_fts_body_is_a_trigram_fts5_virtual_table(tmp_path: Path) -> None:
    """fts_body sql references fts5 + trigram tokenizer.

    Design §4 (line 78): trigram is the ONLY workable single-tokenizer for
    the 50/50 EN/TC corpus (unicode61 treats CJK runs as one token; porter
    is EN-only; ICU not shipped by stock CPython sqlite3).
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='fts_body'"
    ).fetchone()
    assert row is not None
    sql = row[0].lower()
    assert "fts5" in sql
    assert "trigram" in sql
    conn.close()


def test_fts_cases_composite_pk_is_case_key_and_lang(tmp_path: Path) -> None:
    """Bilingual keying (L2 fix): PK is (case_key, lang), not case_key
    alone — one row per (case, language) so bilingual bodies don't
    collapse.
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    cols = conn.execute("PRAGMA table_info(fts_cases)").fetchall()
    pk_cols = sorted(c[1] for c in cols if c[5])  # PRAGMA col 5 = pk order
    assert pk_cols == ["case_key", "lang"]
    conn.close()


def test_fts_cases_has_all_documented_columns(tmp_path: Path) -> None:
    """Column list matches design §4 line 74 exactly."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    cols = {
        c[1]
        for c in conn.execute("PRAGMA table_info(fts_cases)").fetchall()
    }
    expected = {
        "case_key", "lang", "court", "year", "number",
        "neutral", "title", "date",
        "body_source", "body_sha256", "indexed_at",
    }
    assert cols == expected
    conn.close()


def test_fts_cases_has_covering_indexes(tmp_path: Path) -> None:
    """(court, year) and (lang, court) covering indexes — browse filter paths."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='index' AND tbl_name='fts_cases'"
        )
    }
    assert "idx_fts_cases_court_year" in indexes
    assert "idx_fts_cases_lang_court" in indexes
    conn.close()


def test_case_bodies_has_integer_rowid_and_unique_case_key_lang(
    tmp_path: Path,
) -> None:
    """case_bodies needs an INTEGER rowid so FTS5 external-content can
    reference it as content_rowid. Composite UNIQUE(case_key, lang)
    prevents double-inserting the same bilingual pair.

    Design §4 line 75 fix — title column INCLUDED so snippet() works
    against the title FTS column.
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    cols = {
        c[1]
        for c in conn.execute("PRAGMA table_info(case_bodies)").fetchall()
    }
    assert cols == {"id", "case_key", "lang", "title", "body"}
    # id is INTEGER PK
    pk_col = next(c for c in conn.execute("PRAGMA table_info(case_bodies)") if c[5])
    assert pk_col[1] == "id"
    assert pk_col[2].upper() == "INTEGER"
    # UNIQUE(case_key, lang) — verify by triggering the constraint
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 'T', 'B')"
    )
    try:
        conn.execute(
            "INSERT INTO case_bodies (case_key, lang, title, body) "
            "VALUES ('hkcfa/2020/32', 'en', 'T2', 'B2')"
        )
        raise AssertionError("expected IntegrityError on duplicate (case_key, lang)")
    except sqlite3.IntegrityError:
        pass
    conn.close()


# ---------------------------------------------------------------------------
# Behavioral — trigger sync between case_bodies and fts_body
# ---------------------------------------------------------------------------


def test_insert_case_body_populates_fts_body_via_trigger(tmp_path: Path) -> None:
    """AFTER INSERT trigger: an insert into case_bodies makes the body
    immediately findable via MATCH on fts_body.
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 'HKSAR v Test', "
        "'The defendant argued a foundation principle')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT c.case_key FROM fts_body b "
        "JOIN case_bodies c ON c.id = b.rowid "
        "WHERE fts_body MATCH ?",
        ["defendant"],
    ).fetchone()
    assert row == ("hkcfa/2020/32",)
    conn.close()


def test_delete_case_body_removes_from_fts_body_via_trigger(
    tmp_path: Path,
) -> None:
    """AFTER DELETE trigger: the deleted body is no longer findable."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 't', 'unique-body-marker')"
    )
    conn.execute("DELETE FROM case_bodies WHERE case_key='hkcfa/2020/32'")
    conn.commit()
    r = conn.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["unique-body-marker"],
    ).fetchone()
    assert r == (0,)
    conn.close()


def test_update_case_body_replaces_fts_body_row_via_trigger(
    tmp_path: Path,
) -> None:
    """AFTER UPDATE trigger: old body no longer matches, new body does."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 't', 'old-body-marker')"
    )
    conn.execute(
        "UPDATE case_bodies SET body='new-body-marker' "
        "WHERE case_key='hkcfa/2020/32'"
    )
    conn.commit()
    r_old = conn.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["old-body-marker"],
    ).fetchone()
    r_new = conn.execute(
        "SELECT c.case_key FROM fts_body b JOIN case_bodies c ON c.id = b.rowid "
        "WHERE fts_body MATCH ?",
        ["new-body-marker"],
    ).fetchone()
    assert r_old == (0,)
    assert r_new == ("hkcfa/2020/32",)
    conn.close()


# ---------------------------------------------------------------------------
# Behavioral — trigram tokenizer semantics
# ---------------------------------------------------------------------------


def test_trigram_matches_english_substring(tmp_path: Path) -> None:
    """Trigram is char-based — 3+ char substrings anywhere in a word match."""
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 't', "
        "'The foundation of the doctrine was clearly established')"
    )
    conn.commit()
    for term in ("foundation", "oundat", "trine"):
        r = conn.execute(
            "SELECT c.case_key FROM fts_body b "
            "JOIN case_bodies c ON c.id = b.rowid "
            "WHERE fts_body MATCH ?",
            [term],
        ).fetchone()
        assert r == ("hkcfa/2020/32",), f"MATCH failed for {term!r}"
    conn.close()


def test_trigram_matches_cjk_three_or_more_characters(tmp_path: Path) -> None:
    """CJK 3+ char queries match. This is the whole reason we picked trigram
    over unicode61 (which treats a run of Han chars as ONE token).
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'tc', '香港特別行政區', "
        "'終審法院判決 香港特別行政區 上訴案件')"
    )
    conn.commit()
    for term in ("香港特別行政區", "特別行政區", "香港特別", "終審法院"):
        r = conn.execute(
            "SELECT c.case_key FROM fts_body b "
            "JOIN case_bodies c ON c.id = b.rowid "
            "WHERE fts_body MATCH ?",
            [term],
        ).fetchone()
        assert r == ("hkcfa/2020/32",), f"CJK MATCH failed for {term!r}"
    conn.close()


def test_trigram_2char_cjk_query_returns_no_matches(tmp_path: Path) -> None:
    """Trigram documented lower bound: 3 chars minimum. 2-char CJK queries
    yield no rows — the UI validates upfront rather than surfacing a
    silent-empty result (design §4 line 78).
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'tc', '香港特別行政區', "
        "'終審法院判決 香港特別行政區 上訴案件')"
    )
    conn.commit()
    for term in ("香港", "特別", "行政"):
        r = conn.execute(
            "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
            [term],
        ).fetchone()
        assert r == (0,), f"expected 0 hits for 2-char {term!r}"
    conn.close()


def test_trigram_snippet_wraps_matches_in_mark_tags(tmp_path: Path) -> None:
    """snippet(fts_body, 1, '<mark>', '</mark>', '…', 32) wraps highlighted
    text — design §4 line 86 fixes the highlight tokens as a contract with
    the styling layer.
    """
    conn = _fresh_db(tmp_path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO case_bodies (case_key, lang, title, body) "
        "VALUES ('hkcfa/2020/32', 'en', 't', "
        "'The foundation of the doctrine established here')"
    )
    conn.commit()
    r = conn.execute(
        "SELECT snippet(fts_body, 1, '<mark>', '</mark>', '…', 8) "
        "FROM fts_body WHERE fts_body MATCH ?",
        ["foundation"],
    ).fetchone()
    assert r is not None
    assert "<mark>" in r[0] and "</mark>" in r[0]
    # The highlighted word should be inside the tags
    assert "foundation" in r[0].lower()
    conn.close()
