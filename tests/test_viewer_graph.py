"""Tests for viewer/graph.py — read-only citation graph helpers.

Fixtures mirror the shipped citations schema inline. A schema-drift
contract test in a later phase re-asserts against the real
checkpoint.db shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import json

from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import (
    ViewerCacheMissing,
    appeal_chain,
    authorities_cited,
    cited_by,
    hub_cases,
    inbound_counts,
    parallel_cites,
)
from hklii_downloader.viewer.schema import VIEWER_HUB_CACHE_DDL


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

# Mirror of the shipped case_parallel_cites table.
_PARALLEL_CITES_DDL = """
CREATE TABLE case_parallel_cites (
    case_key      TEXT NOT NULL,
    parallel_cite TEXT NOT NULL,
    PRIMARY KEY (case_key, parallel_cite)
) WITHOUT ROWID;
"""


def _seed_parallel_cites(
    db_path: Path,
    rows: list[tuple[str, str]],
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PARALLEL_CITES_DDL)
    conn.executemany(
        "INSERT INTO case_parallel_cites (case_key, parallel_cite) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


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


def test_cited_by_and_authorities_cited_use_symmetric_min_position(
    tmp_path: Path,
) -> None:
    """L3 promise/impl drift: authorities_cited's docstring says "symmetric
    to :func:`cited_by`" but cited_by aggregated MAX(position) while
    authorities_cited aggregated MIN(position) for the same (from, to,
    en/tc) bilingual pair. The same edge reported opposite ends of HKLII's
    noteup ordinal from either side.

    Fix pins MIN on both — position=1 means 'top of the noteup list',
    so MIN represents the more meaningful signal 'closest to the top'.
    """
    db = tmp_path / "checkpoint.db"
    # Same bilingual citation edge, en at position=1, tc at position=5
    _seed_citations(
        db,
        [
            ("hkcfi/2023/155", "hkcfa/2019/50", "en", 8, 1, "2023-01-01T00:00:00"),
            ("hkcfi/2023/155", "hkcfa/2019/50", "tc", 8, 5, "2023-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        cb_rows = cited_by(conn, "hkcfa/2019/50")
        ac_rows = authorities_cited(conn, "hkcfi/2023/155")
        # Both must agree — bilingual aggregation collapses to MIN
        assert cb_rows[0]["position"] == 1
        assert ac_rows[0]["position"] == 1
        assert cb_rows[0]["position"] == ac_rows[0]["position"]
    finally:
        conn.close()


def test_cited_by_ranks_ukpc_as_near_apex_not_unknown(tmp_path: Path) -> None:
    """Design promise (§7 line 203): all 13 court slugs are ranked.

    ukpc is the canonical downloader slug for UK Privy Council decisions
    (cli.py ALL_COURTS). Pre-1997 UKPC was the ultimate appellate court
    for HK — cited-by rows from a UKPC citer must not collapse into the
    ELSE 99 bucket alongside 'unknown court' anomalies.

    Fix places ukpc at rank 1 (tied with hkca, reflecting its historical
    near-apex role in HK case law).
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkcfi/1995/50", "hkcfa/2020/1", "en", 3, 1, "1995-06-01T00:00:00"),
            ("ukpc/1990/5",   "hkcfa/2020/1", "en", 8, 1, "1990-01-01T00:00:00"),
            ("hkcfa/2019/50", "hkcfa/2020/1", "en", 8, 1, "2019-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = cited_by(conn, "hkcfa/2020/1")
        keys = [r["from_key"] for r in rows]
        # hkcfa (rank 0), ukpc (rank 1, near-apex), hkcfi (rank 3)
        # ukpc must precede hkcfi despite being older
        assert keys.index("hkcfa/2019/50") < keys.index("ukpc/1990/5")
        assert keys.index("ukpc/1990/5") < keys.index("hkcfi/1995/50")
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


# ---------------------------------------------------------------------------
# authorities_cited — symmetric to cited_by; WHERE from_key=? GROUP BY to_key
# ---------------------------------------------------------------------------


def test_authorities_cited_orders_by_cited_court_precedence(tmp_path: Path) -> None:
    """authorities_cited orders cited (to_key) courts by curial precedence.

    Same citer (hkcfi/2023/155) cites 4 cases across 3 courts. Expected order:
    CFA, then CA, then CFI, with first_seen DESC within same court.
    """
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkcfi/2023/155", "hkca/2018/524",  "en", 5, 1, "2023-01-01T00:00:00"),
            ("hkcfi/2023/155", "hkcfa/2019/50",  "en", 8, 2, "2023-02-01T00:00:00"),
            ("hkcfi/2023/155", "hkcfi/2020/22",  "en", 3, 3, "2023-03-01T00:00:00"),
            ("hkcfi/2023/155", "hkcfi/2021/99",  "en", 2, 4, "2023-04-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = authorities_cited(conn, "hkcfi/2023/155")
        keys = [r["to_key"] for r in rows]
        assert keys == [
            "hkcfa/2019/50",   # CFA (rank 0)
            "hkca/2018/524",   # CA  (rank 1)
            "hkcfi/2021/99",   # CFI (rank 2), later first_seen
            "hkcfi/2020/22",   # CFI (rank 2), earlier first_seen
        ]
    finally:
        conn.close()


def test_authorities_cited_dedupes_bilingual_citer_lang(tmp_path: Path) -> None:
    """Bilingual (en+tc) citation of the same target collapses to one row."""
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkcfi/2023/155", "hkcfa/2019/50", "en", 8, 1, "2023-01-01T00:00:00"),
            ("hkcfi/2023/155", "hkcfa/2019/50", "tc", 8, 1, "2023-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = authorities_cited(conn, "hkcfi/2023/155")
        assert len(rows) == 1
        assert rows[0]["to_key"] == "hkcfa/2019/50"
        assert set(rows[0]["langs"].split(",")) == {"en", "tc"}
    finally:
        conn.close()


def test_authorities_cited_unknown_case_returns_empty_list(tmp_path: Path) -> None:
    db = tmp_path / "checkpoint.db"
    _seed_citations(db, [])
    conn = open_readonly(db)
    try:
        assert authorities_cited(conn, "hkcfa/9999/999") == []
    finally:
        conn.close()


def test_authorities_cited_returns_derived_to_court_column(tmp_path: Path) -> None:
    """Row shape includes to_court (SQL-derived via substr on to_key)."""
    db = tmp_path / "checkpoint.db"
    _seed_citations(
        db,
        [
            ("hkcfi/2023/155", "hkcfa/2019/50", "en", 8, 1, "2023-01-01T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = authorities_cited(conn, "hkcfi/2023/155")
        assert rows[0]["to_court"] == "hkcfa"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# parallel_cites — SELECT parallel_cite FROM case_parallel_cites WHERE case_key=?
# ---------------------------------------------------------------------------


def test_parallel_cites_returns_sorted_list_of_strings(tmp_path: Path) -> None:
    """List of parallel citation strings, sorted ASC for stable rendering."""
    db = tmp_path / "checkpoint.db"
    _seed_parallel_cites(
        db,
        [
            ("hkcfa/2020/1", "[2021] 6 HKC 46"),
            ("hkcfa/2020/1", "(2020) 23 HKCFAR 100"),
            ("hkcfa/2020/1", "[2020] HKCFA 32"),
        ],
    )
    conn = open_readonly(db)
    try:
        cites = parallel_cites(conn, "hkcfa/2020/1")
        assert cites == [
            "(2020) 23 HKCFAR 100",
            "[2020] HKCFA 32",
            "[2021] 6 HKC 46",
        ]
    finally:
        conn.close()


def test_parallel_cites_unknown_case_returns_empty_list(tmp_path: Path) -> None:
    """L5: missing case is not an error — just no parallel cites."""
    db = tmp_path / "checkpoint.db"
    _seed_parallel_cites(db, [])
    conn = open_readonly(db)
    try:
        assert parallel_cites(conn, "hkcfa/9999/999") == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# hub_cases + inbound_counts — read viewer.db, not checkpoint.db
# ---------------------------------------------------------------------------


def _seed_viewer_hub_cache(
    db_path: Path,
    rows: list[tuple[str, int, str]],
) -> None:
    """rows: (case_key, inbound_count, computed_at)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(VIEWER_HUB_CACHE_DDL)
    conn.executemany(
        "INSERT INTO viewer_hub_cache (case_key, inbound_count, computed_at) "
        "VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_hub_cases_orders_by_inbound_count_desc(tmp_path: Path) -> None:
    """Top hubs first. case_key ASC as stable tiebreak."""
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            ("hkca/2018/524",  11450, "2026-07-07T00:00:00"),
            ("hkca/2012/502",   6468, "2026-07-07T00:00:00"),
            ("hkcfa/1999/17",   4595, "2026-07-07T00:00:00"),
            ("hkcfa/1999/72",   4595, "2026-07-07T00:00:00"),  # tie
        ],
    )
    conn = open_readonly(db)
    try:
        rows = hub_cases(conn)
        keys = [r["case_key"] for r in rows]
        assert keys == [
            "hkca/2018/524",
            "hkca/2012/502",
            "hkcfa/1999/17",  # tie w/ 72; case_key ASC → 17 before 72
            "hkcfa/1999/72",
        ]
    finally:
        conn.close()


def test_hub_cases_court_filter_narrows(tmp_path: Path) -> None:
    """court='hkcfa' returns only CFA hubs (derived from case_key prefix)."""
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            ("hkca/2018/524",  11450, "2026-07-07T00:00:00"),
            ("hkcfa/1999/17",   4595, "2026-07-07T00:00:00"),
            ("hkcfa/2020/32",   200,  "2026-07-07T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        rows = hub_cases(conn, court="hkcfa")
        assert [r["case_key"] for r in rows] == [
            "hkcfa/1999/17",
            "hkcfa/2020/32",
        ]
    finally:
        conn.close()


def test_hub_cases_min_inbound_filters_low_count_rows(tmp_path: Path) -> None:
    """Default min_inbound=5 filters out tiny counts — the design's rationale
    is that a case with 2 inbound cites isn't a 'hub' by any reasonable UI."""
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            ("hkca/2018/524", 11450, "2026-07-07T00:00:00"),
            ("hkcfa/2020/32",     2, "2026-07-07T00:00:00"),  # below default
        ],
    )
    conn = open_readonly(db)
    try:
        rows = hub_cases(conn)
        assert [r["case_key"] for r in rows] == ["hkca/2018/524"]
    finally:
        conn.close()


def test_hub_cases_limit_truncates(tmp_path: Path) -> None:
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            (f"hkcfa/2020/{n}", 100 - n, "2026-07-07T00:00:00")
            for n in range(1, 6)
        ],
    )
    conn = open_readonly(db)
    try:
        rows = hub_cases(conn, limit=2)
        assert len(rows) == 2
        assert rows[0]["case_key"] == "hkcfa/2020/1"  # highest count
    finally:
        conn.close()


def test_hub_cases_empty_cache_returns_empty_list(tmp_path: Path) -> None:
    """L5: table exists, 0 rows — a legitimate 'nothing cached yet' answer.
    Distinct from the missing-table state below.
    """
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(db, [])
    conn = open_readonly(db)
    try:
        assert hub_cases(conn) == []
    finally:
        conn.close()


def test_hub_cases_missing_table_raises_viewer_cache_missing(tmp_path: Path) -> None:
    """L1: missing viewer_hub_cache table raises — never silently returns [].

    Route handler catches this to render the 'run `hklii viewer index`' banner,
    which is a distinct UX state from 'cache empty'.
    """
    db = tmp_path / "viewer.db"
    # Create an empty DB file WITHOUT the table
    sqlite3.connect(str(db)).close()
    conn = open_readonly(db)
    try:
        with pytest.raises(ViewerCacheMissing):
            hub_cases(conn)
    finally:
        conn.close()


def test_inbound_counts_returns_dict_keyed_on_case_key(tmp_path: Path) -> None:
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            ("hkca/2018/524", 11450, "2026-07-07T00:00:00"),
            ("hkcfa/2020/32",    32, "2026-07-07T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        result = inbound_counts(conn, ["hkca/2018/524", "hkcfa/2020/32"])
        assert result == {"hkca/2018/524": 11450, "hkcfa/2020/32": 32}
    finally:
        conn.close()


def test_inbound_counts_absent_key_is_missing_from_dict(tmp_path: Path) -> None:
    """L5 ambiguous-state: a case_key not in the cache is ABSENT from the dict,
    not present with value 0. The caller can distinguish:
      - present, value 0 → cached, no inbound
      - absent from dict → never computed (or fell out of cache)
    """
    db = tmp_path / "viewer.db"
    _seed_viewer_hub_cache(
        db,
        [
            ("hkca/2018/524", 11450, "2026-07-07T00:00:00"),
        ],
    )
    conn = open_readonly(db)
    try:
        result = inbound_counts(
            conn, ["hkca/2018/524", "hkcfa/9999/999"]
        )
        assert result == {"hkca/2018/524": 11450}
        assert "hkcfa/9999/999" not in result
    finally:
        conn.close()


def test_inbound_counts_missing_table_raises(tmp_path: Path) -> None:
    """L1: missing table raises. Consistent with hub_cases behavior."""
    db = tmp_path / "viewer.db"
    sqlite3.connect(str(db)).close()
    conn = open_readonly(db)
    try:
        with pytest.raises(ViewerCacheMissing):
            inbound_counts(conn, ["hkca/2018/524"])
    finally:
        conn.close()


def test_inbound_counts_empty_input_returns_empty_dict(tmp_path: Path) -> None:
    """Empty input is not an error and does not require the table to exist.

    Guards against a route that receives 0 case_keys firing a spurious
    missing-table raise before it can render an empty page.
    """
    db = tmp_path / "viewer.db"
    sqlite3.connect(str(db)).close()  # no table
    conn = open_readonly(db)
    try:
        assert inbound_counts(conn, []) == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# appeal_chain — reads output/{court}/{year}/{stem}.appeal_history.json
# ---------------------------------------------------------------------------


_SAMPLE_CHAIN = [
    {
        "act": "CACC124/2013",
        "judgments": [
            {
                "neutral": "[2013] HKCA 533",
                "date": "2013-10-07",
                "remarks": "",
                "path": "/en/cases/hkca/2013/533",
                "lang": "EN",
            }
        ],
    },
    {
        "act": "DCCC860/2012",
        "judgments": [
            {
                "neutral": "[2013] HKDC 352",
                "date": "2013-03-12",
                "remarks": "",
                "path": "/en/cases/hkdc/2013/352",
                "lang": "EN",
            }
        ],
    },
]


def _seed_appeal_sidecar(
    output_root: Path,
    case_key: str,
    chain: list[dict],
) -> Path:
    """Write output/{court}/{year}/{court}_{year}_{num}.appeal_history.json."""
    court, year, num = case_key.split("/", 2)
    dst = output_root / court / year / f"{court}_{year}_{num}.appeal_history.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(chain), encoding="utf-8")
    return dst


def test_appeal_chain_reads_expected_path_and_returns_parsed_json(
    tmp_path: Path,
) -> None:
    """Path shape: output_root/{court}/{year}/{court}_{year}_{n}.appeal_history.json.
    Returned value is the parsed JSON (list of acts) as-is.
    """
    _seed_appeal_sidecar(tmp_path, "hkdc/2013/352", _SAMPLE_CHAIN)
    chain = appeal_chain(tmp_path, "hkdc/2013/352")
    assert chain == _SAMPLE_CHAIN
    assert len(chain) == 2
    assert chain[0]["act"] == "CACC124/2013"


def test_appeal_chain_no_sidecar_returns_empty_list(tmp_path: Path) -> None:
    """Most cases in the corpus don't have appeal chains. File absence is
    a legitimate 'no chain' signal, NOT a silent-skip (L1 nuance): the
    caller intends to render an empty appeal-strip, not surface an error.
    """
    # tmp_path exists but no sidecar is written
    assert appeal_chain(tmp_path, "hkcfa/2020/1") == []


def test_appeal_chain_malformed_json_raises(tmp_path: Path) -> None:
    """L1 strict lens: JSON parse errors must raise. A malformed sidecar
    is a real data issue (partial write, disk corruption) — the viewer
    surfaces it rather than silently pretending the case has no chain.
    """
    court, year, num = "hkcfa", "2020", "1"
    dst = tmp_path / court / year / f"{court}_{year}_{num}.appeal_history.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("not valid json {", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        appeal_chain(tmp_path, "hkcfa/2020/1")


def test_appeal_chain_accepts_str_and_pathlib_output_root(tmp_path: Path) -> None:
    _seed_appeal_sidecar(tmp_path, "hkdc/2013/352", _SAMPLE_CHAIN)
    for arg in (str(tmp_path), tmp_path):
        assert appeal_chain(arg, "hkdc/2013/352") == _SAMPLE_CHAIN


def test_appeal_chain_malformed_case_key_raises_value_error(
    tmp_path: Path,
) -> None:
    """A case_key without two slashes cannot resolve a path — raise, don't
    silently return [].
    """
    with pytest.raises(ValueError):
        appeal_chain(tmp_path, "just-a-string")
    with pytest.raises(ValueError):
        appeal_chain(tmp_path, "onlyone/slash")
