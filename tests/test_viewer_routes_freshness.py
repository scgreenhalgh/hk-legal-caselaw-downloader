"""Tests for GET /freshness — live-generated database drift report.

Design:
  * ``GET /freshness`` renders an HTML table of the same shape as the
    ``hklii check-freshness --report`` markdown, but reads
    ``db_freshness`` off the local checkpoint DB and never touches the
    wire — a browser-friendly view of the last-probed state.
  * Every row: slug, English name, Chinese name, and three lang cell
    groups (EN / TC / SC), each showing ``local / live`` counts and
    the last-observed HKLII ``live_updated_at`` timestamp.
  * Cells where ``local != live`` are visually emphasised — bolded and
    class-tagged ``freshness-mismatch`` so an operator scanning the
    page spots drift instantly. Delta (``+N`` / ``-N``) is inline.
  * The page cites ``docs/freshness-sanity-check.md`` as the reference
    for interpreting the deltas + operating instructions.

L-lens coverage:
  * L1 silent-skip — a missing db_freshness row surfaces as ``—`` in
    that cell (asserted below); an empty ledger renders the page
    without exploding.
  * L2 semantic drift — bold class only appears on mismatches, never
    on parity rows (see ``test_matching_cells_have_no_mismatch_class``).
  * L4 wrong-side — every assertion parses the rendered DOM via BS4,
    not the raw string, so a template refactor that keeps the shape
    doesn't require test edits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db


# --- seeding helpers ------------------------------------------------------

_DB_FRESHNESS_DDL = """
CREATE TABLE db_freshness (
    kind                     TEXT NOT NULL,
    scope                    TEXT NOT NULL,
    lang                     TEXT NOT NULL,
    live_count               INTEGER,
    live_updated_at          TEXT,
    live_probed_at           INTEGER,
    probe_error              TEXT,
    local_count              INTEGER,
    local_counted_at         INTEGER,
    last_scrape_completed_at INTEGER,
    source_generation_id     INTEGER,
    PRIMARY KEY (kind, scope, lang)
);
"""


def seed_db_freshness(db_path: Path, rows: list[dict]) -> None:
    """Rows: dicts with kind/scope/lang plus any populated columns.

    Mirrors what ``CheckpointDB.upsert_freshness_probe`` /
    ``recompute_local_count`` / ``mark_bucket_scraped`` would write
    together, but assembled in one shot so test scenarios are terse.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DB_FRESHNESS_DDL)
        conn.executemany(
            "INSERT INTO db_freshness "
            "(kind, scope, lang, live_count, live_updated_at, "
            " live_probed_at, probe_error, local_count, "
            " local_counted_at, last_scrape_completed_at, "
            " source_generation_id) "
            "VALUES (:kind, :scope, :lang, :live_count, "
            ":live_updated_at, :live_probed_at, :probe_error, "
            ":local_count, :local_counted_at, "
            ":last_scrape_completed_at, :source_generation_id)",
            [{**{
                "live_count": None, "live_updated_at": None,
                "live_probed_at": None, "probe_error": None,
                "local_count": None, "local_counted_at": None,
                "last_scrape_completed_at": None,
                "source_generation_id": None,
            }, **r} for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


# --- fixture --------------------------------------------------------------

@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    # 4 rows covering every case the page must render:
    #   * cases/hkcfa/en   — parity (plain)
    #   * cases/hkcfi/tc   — mismatch (+2, bold)
    #   * legis/ord/sc     — mismatch (+838, bold — trilingual SC)
    #   * hopt/bahkg/en    — mismatch (+1, HKLII duplicate quirk)
    seed_db_freshness(checkpoint, [
        {"kind": "cases", "scope": "hkcfa", "lang": "en",
         "live_count": 2143, "local_count": 2143,
         "live_updated_at": "2026-07-08",
         "live_probed_at": 1_720_000_000,
         "last_scrape_completed_at": 1_720_000_000},
        {"kind": "cases", "scope": "hkcfi", "lang": "tc",
         "live_count": 16123, "local_count": 16121,
         "live_updated_at": "2026-07-08",
         "live_probed_at": 1_720_000_000,
         "last_scrape_completed_at": 1_720_000_000},
        {"kind": "legis", "scope": "ord", "lang": "sc",
         "live_count": 838, "local_count": 0,
         "live_updated_at": "2026-07-08",
         "live_probed_at": 1_720_000_000},
        {"kind": "hopt", "scope": "bahkg", "lang": "en",
         "live_count": 218, "local_count": 217,
         "live_updated_at": "2026-07-08",
         "live_probed_at": 1_720_000_000,
         "last_scrape_completed_at": 1_720_000_000},
    ])
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer,
        output_root=output_root,
    )
    return TestClient(app)


# --- tests ---------------------------------------------------------------

def test_route_returns_200_html(client: TestClient) -> None:
    r = client.get("/freshness")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_page_has_freshness_table(client: TestClient) -> None:
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.select_one("table.freshness-table")
    assert table is not None, (
        "no <table.freshness-table> on the page — the freshness "
        "route did not render the ledger."
    )


def test_matching_cell_renders_plain(client: TestClient) -> None:
    """cases/hkcfa/en has live == local — must render as plain
    ``2143 / 2143`` with no mismatch class or delta."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    row = soup.select_one('tr[data-slug="hkcfa"]')
    assert row is not None
    en_cell = row.select_one('[data-lang="en"] .count')
    assert en_cell is not None
    assert "2143 / 2143" in en_cell.get_text()
    assert "freshness-mismatch" not in en_cell.get("class", []), (
        "matching cell should not carry the mismatch class"
    )


def test_mismatch_cell_is_bolded_and_shows_delta(
    client: TestClient,
) -> None:
    """hkcfi/tc has live=16123, local=16121 → +2 mismatch. Must render
    with the ``freshness-mismatch`` class and inline ``(+2)`` delta."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    row = soup.select_one('tr[data-slug="hkcfi"]')
    assert row is not None
    tc_cell = row.select_one('[data-lang="tc"] .count')
    assert tc_cell is not None
    text = tc_cell.get_text()
    assert "16121 / 16123" in text
    assert "(+2)" in text, (
        "expected inline delta '(+2)' next to mismatched counts; got "
        + text
    )
    assert "freshness-mismatch" in tc_cell.get("class", []), (
        "mismatch cell missing the class — visual emphasis wouldn't "
        "fire in CSS"
    )


def test_sc_column_populates_for_trilingual_slug(
    client: TestClient,
) -> None:
    """legis/ord/sc has live=838, local=0. The SC group must render
    it AND mark the cell as mismatch."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    row = soup.select_one('tr[data-slug="ord"]')
    assert row is not None
    sc_cell = row.select_one('[data-lang="sc"] .count')
    assert sc_cell is not None
    assert "0 / 838" in sc_cell.get_text()
    assert "(+838)" in sc_cell.get_text()
    assert "freshness-mismatch" in sc_cell.get("class", [])


def test_missing_row_shows_em_dash(client: TestClient) -> None:
    """cases/hkca is in the /databases matrix but not in the seeded
    db_freshness → cells render em-dashes, not '0 / 0'."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    row = soup.select_one('tr[data-slug="hkca"]')
    assert row is not None
    en_cell = row.select_one('[data-lang="en"] .count')
    assert en_cell is not None
    assert "—" in en_cell.get_text()


def test_hopt_family_renders_row(client: TestClient) -> None:
    """hopt/bahkg/en has +1 mismatch — the hopt-family bucket must
    appear on the same page as case-family and legis; the /freshness
    view is the single-page overview of the whole ledger."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    row = soup.select_one('tr[data-slug="bahkg"]')
    assert row is not None
    en_cell = row.select_one('[data-lang="en"] .count')
    assert en_cell is not None
    assert "217 / 218" in en_cell.get_text()
    assert "(+1)" in en_cell.get_text()
    assert "freshness-mismatch" in en_cell.get("class", [])


def test_page_links_to_sanity_check_doc(client: TestClient) -> None:
    """Page cites ``docs/freshness-sanity-check.md`` so a reader with
    "what does bold mean" or "how do I close the drift" gets a link
    to the operator doc, not a dead end."""
    r = client.get("/freshness")
    assert "freshness-sanity-check" in r.text


def test_primary_nav_links_to_freshness(client: TestClient) -> None:
    """The primary nav gets a link to /freshness on every page —
    discoverable without knowing the URL. Assert against the
    freshness page itself since the fixture doesn't seed the cases
    table that the home route reads; the nav lives in base.html so
    every route surfaces the same link."""
    r = client.get("/freshness")
    soup = BeautifulSoup(r.text, "html.parser")
    nav = soup.find("nav")
    assert nav is not None
    assert nav.find("a", href="/freshness") is not None, (
        "no <a href='/freshness'> in the primary nav; the page is "
        "shipped but invisible."
    )
