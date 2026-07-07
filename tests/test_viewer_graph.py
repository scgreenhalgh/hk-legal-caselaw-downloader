"""Tests for viewer/graph.py — read-only citation graph helpers.

Fixtures mirror the shipped citations schema inline. A schema-drift
contract test in a later phase re-asserts against the real
checkpoint.db shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import cited_by


# Mirror of the shipped citations table (see hklii_downloader.checkpoint._SCHEMA).
_CITATIONS_DDL = """
CREATE TABLE citations (
    from_key   TEXT NOT NULL,
    to_key     TEXT NOT NULL,
    citer_lang TEXT NOT NULL,
    citer_freq INTEGER,
    position   INTEGER,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (from_key, to_key, citer_lang)
) WITHOUT ROWID;
"""
_CITATIONS_INDEX = "CREATE INDEX idx_cit_to ON citations(to_key);"


def _seed_citations(
    db_path: Path,
    rows: list[tuple[str, str, str, int, int, str]],
) -> None:
    """rows: (from_key, to_key, citer_lang, citer_freq, position, first_seen)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_CITATIONS_DDL)
    conn.execute(_CITATIONS_INDEX)
    conn.executemany(
        "INSERT INTO citations "
        "(from_key, to_key, citer_lang, citer_freq, position, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_cited_by_orders_by_curial_precedence_then_first_seen_desc(
    tmp_path: Path,
) -> None:
    """Ordering per design §7: court_rank ASC, first_seen DESC.

    Same target cited by 4 courts across 4 years. Expected order pins the
    CASE-expression court ranks (CFA=0, CA=1, CFI=2) and the within-court
    tiebreak on first_seen DESC.
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkca/2018/524",  "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
            ("hkcfa/2019/50",  "hkcfa/2020/1", "en", 8, 1, "2019-06-01T00:00:00"),
            ("hkcfi/2021/99",  "hkcfa/2020/1", "en", 3, 1, "2021-03-01T00:00:00"),
            ("hkcfi/2020/22",  "hkcfa/2020/1", "en", 2, 1, "2020-05-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = cited_by(conn, "hkcfa/2020/1")
        keys = [r["from_key"] for r in rows]
        assert keys == [
            "hkcfa/2019/50",  # CFA (rank 0)
            "hkca/2018/524",  # CA  (rank 1)
            "hkcfi/2021/99",  # CFI (rank 2), later first_seen
            "hkcfi/2020/22",  # CFI (rank 2), earlier first_seen
        ]
    finally:
        conn.close()


def test_cited_by_court_filter_narrows_to_that_court(tmp_path: Path) -> None:
    """court_filter='hkcfi' returns only CFI citers."""
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkca/2018/524",  "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
            ("hkcfi/2021/99",  "hkcfa/2020/1", "en", 3, 1, "2021-03-01T00:00:00"),
            ("hkcfi/2020/22",  "hkcfa/2020/1", "en", 2, 1, "2020-05-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = cited_by(conn, "hkcfa/2020/1", court_filter="hkcfi")
        keys = [r["from_key"] for r in rows]
        assert keys == ["hkcfi/2021/99", "hkcfi/2020/22"]
    finally:
        conn.close()


def test_cited_by_unknown_case_returns_empty_list(tmp_path: Path) -> None:
    """L5 ambiguous-state: no citations means an empty list, not a raise
    and not None. UI renders 'no incoming citations', distinct from
    'cache not populated' (that's a hub_cases concern).
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(db, [])
    conn = open_readonly(db)
    try:
        assert cited_by(conn, "hkcfa/9999/999") == []
    finally:
        conn.close()


def test_cited_by_paginates_deterministically(tmp_path: Path) -> None:
    """page + per_page slice without reshuffling the sort."""
    db = tmp_path / "checkpoint.db"
    rows = [
        (f"hkcfi/2020/{n}", "hkcfa/2020/1", "en", 1, 1, f"2020-01-0{n}T00:00:00")
        for n in range(1, 6)
    ]
    _seed_citations(db, rows)
    conn = open_readonly(db)
    try:
        page1 = cited_by(conn, "hkcfa/2020/1", page=1, per_page=2)
        page2 = cited_by(conn, "hkcfa/2020/1", page=2, per_page=2)
        page3 = cited_by(conn, "hkcfa/2020/1", page=3, per_page=2)
        assert [r["from_key"] for r in page1] == ["hkcfi/2020/5", "hkcfi/2020/4"]
        assert [r["from_key"] for r in page2] == ["hkcfi/2020/3", "hkcfi/2020/2"]
        assert [r["from_key"] for r in page3] == ["hkcfi/2020/1"]
    finally:
        conn.close()


def test_cited_by_dedupes_bilingual_citer_lang(tmp_path: Path) -> None:
    """L2 semantic-drift: bilingual (en+tc) citer rows must collapse to one.

    The citations table PK is (from_key, to_key, citer_lang) so bilingual
    citers are two physical rows. The UI expects one row per citing case,
    matching hub_cases' COUNT(DISTINCT from_key) contract in design §7.
    The returned 'langs' column preserves both language codes.
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkca/2018/524", "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
            ("hkca/2018/524", "hkcfa/2020/1", "tc", 5, 1, "2020-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = cited_by(conn, "hkcfa/2020/1")
        assert len(rows) == 1
        assert rows[0]["from_key"] == "hkca/2018/524"
        assert set(rows[0]["langs"].split(",")) == {"en", "tc"}
    finally:
        conn.close()


def test_cited_by_returns_derived_from_court_column(tmp_path: Path) -> None:
    """The returned row shape includes from_court (SQL-derived via substr).

    Documented shape avoids per-call substring parsing in caller code;
    Option 3 scope (no from_court column added to shipped schema).
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkcfa/2019/50", "hkcfa/2020/1", "en", 8, 1, "2019-06-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = cited_by(conn, "hkcfa/2020/1")
        assert rows[0]["from_court"] == "hkcfa"
    finally:
        conn.close()
