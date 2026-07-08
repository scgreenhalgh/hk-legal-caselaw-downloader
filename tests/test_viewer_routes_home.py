"""Tests for the viewer's HTTP routes — GET / (home).

Home surfaces court tiles + recent cases (RESUME_PROMPT Phase 4 route 1).

Fixture strategy: each test file brings its own tmp_path seeding. Refactor
to shared conftest only when the same seed shape is needed across ≥3 route
files (avoiding speculative fixture design before the routes' shape is
pinned by real assertions).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases


@pytest.fixture
def app_dbs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Seeded checkpoint.db + viewer.db + empty output_root under tmp_path."""
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            # (court, year, number, neutral, title, date, status)
            ("hkcfa", 2024, 15,  "[2024] HKCFA 15",  "HKSAR v Chan",   "2024-11-20", "downloaded"),
            ("hkcfa", 2024, 12,  "[2024] HKCFA 12",  "AA v BB",         "2024-08-05", "downloaded"),
            ("hkca",  2024, 88,  "[2024] HKCA 88",   "CC v DD",         "2024-11-01", "downloaded"),
            ("hkca",  2024, 40,  "[2024] HKCA 40",   "EE Bank v FF",    "2024-06-10", "downloaded"),
            ("hkcfi", 2024, 500, "[2024] HKCFI 500", "Re GG Ltd",       "2024-11-25", "downloaded"),
            ("hkcfi", 2024, 300, "[2024] HKCFI 300", "HH v II",         "2024-09-15", "downloaded"),
            ("hkdc",  2024, 200, "[2024] HKDC 200",  "JJ v KK",         "2024-10-01", "downloaded"),
            ("hkdc",  2024, 100, "[2024] HKDC 100",  "LL v MM",         "2024-05-01", "downloaded"),
        ],
    )
    build_viewer_db(viewer)
    return checkpoint, viewer, output_root


@pytest.fixture
def client(app_dbs: tuple[Path, Path, Path]) -> TestClient:
    checkpoint_db, viewer_db, output_root = app_dbs
    app = create_app(
        checkpoint_db=checkpoint_db,
        viewer_db=viewer_db,
        output_root=output_root,
    )
    return TestClient(app)


def test_home_returns_200_html(client: TestClient) -> None:
    """Route smoke: 200 with an HTML content-type."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_home_shows_court_tiles_for_all_canonical_courts(client: TestClient) -> None:
    """Court tiles include every canonical court slug even when a court has
    zero seeded cases (L5: 0-cases-in-court vs court-doesn't-exist must be
    surfaced distinctly). The four seeded courts must show their real counts.
    """
    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    tiles = soup.select("[data-testid=court-tile]")
    slugs = {t.get("data-court") for t in tiles}
    # All 13 canonical courts surface — 12 from getcasefiles-family +
    # UKPC (hopt-C family). Matches viewer.courts.CANONICAL_COURTS.
    expected = {
        "hkcfa", "hkca", "ukpc", "hkcfi", "hkdc",
        "hkmagc", "hkfc", "hkldt", "hklat",
        "hkct", "hksct", "hkcrc", "hkoat",
    }
    assert expected <= slugs
    counts = {t.get("data-court"): int(t.get("data-count")) for t in tiles}
    assert counts["hkcfa"] == 2
    assert counts["hkca"] == 2
    assert counts["hkcfi"] == 2
    assert counts["hkdc"] == 2
    # Un-seeded courts show count 0 (present-with-zero, not absent).
    assert counts["hkoat"] == 0


def test_home_lists_recent_cases_by_date_desc(client: TestClient) -> None:
    """Recent-cases list orders by date DESC.

    Seeded five latest dates: 2024-11-25 (hkcfi/2024/500),
    2024-11-20 (hkcfa/2024/15), 2024-11-01 (hkca/2024/88),
    2024-10-01 (hkdc/2024/200), 2024-09-15 (hkcfi/2024/300).
    """
    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=recent-case]")
    assert len(rows) >= 5
    dates = [r.get("data-date") for r in rows]
    assert dates == sorted(dates, reverse=True)
    assert rows[0].get("data-case-key") == "hkcfi/2024/500"
    assert rows[1].get("data-case-key") == "hkcfa/2024/15"


def test_home_recent_case_row_shows_neutral_and_title(client: TestClient) -> None:
    """Each recent-case row surfaces the neutral cite and parties title."""
    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=recent-case]")
    text = rows[0].get_text()
    assert "[2024] HKCFI 500" in text
    assert "Re GG Ltd" in text


def test_home_recent_case_links_to_case_detail(client: TestClient) -> None:
    """Each recent-case row links to /case/{court}/{year}/{number} —
    the detail route (Phase 4 route 4). Pinning the URL shape here means
    routes 2/3/4 can't silently drift from this shell's expectations.
    """
    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    first_row = soup.select("[data-testid=recent-case]")[0]
    anchor = first_row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfi/2024/500"


def test_home_court_tile_links_to_court_landing(client: TestClient) -> None:
    """Each court tile is clickable and links to /court/{slug} (Phase 4
    route 2). Same reason as above — this pins the URL shape for route 2.
    """
    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    tiles = soup.select("[data-testid=court-tile]")
    hkcfa_tile = next(t for t in tiles if t.get("data-court") == "hkcfa")
    anchor = hkcfa_tile.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/court/hkcfa"


def test_canonical_courts_covers_all_court_scrape_sources() -> None:
    """L2 drift guard: viewer's ``CANONICAL_COURTS`` must cover every
    court the downloader scrapes from — whether the source is the
    getcasefiles fan-out (``cli.ALL_COURTS``) or the hopt-C endpoint
    family (currently just UKPC). Any new court added to a scrape
    pipeline would otherwise silently fall off the viewer home page
    and every court-facet filter.

    The relationship is CANONICAL_COURTS == ALL_COURTS ∪ HOPT_C_COURTS.
    Direct equality with ALL_COURTS would fail because UKPC is in
    CANONICAL_COURTS but scraped via hopt (see viewer/courts.py comment).
    """
    from hklii_downloader.cli import ALL_COURTS
    from hklii_downloader.viewer.courts import CANONICAL_COURTS

    # UKPC (and any future hopt-C court) is expected to be extra vs
    # ALL_COURTS. Everything else must match.
    _KNOWN_HOPT_C_COURTS = {"ukpc"}

    missing = set(ALL_COURTS) - set(CANONICAL_COURTS)
    extra = set(CANONICAL_COURTS) - set(ALL_COURTS) - _KNOWN_HOPT_C_COURTS
    assert not missing and not extra, (
        f"CANONICAL_COURTS drift — missing from viewer: {missing}, "
        f"unexpected in viewer (not hopt-C): {extra}"
    )


def test_home_empty_corpus_shows_empty_state_not_broken_page(
    tmp_path: Path,
) -> None:
    """L5 ambiguous-state: an empty corpus is a legitimate state (fresh
    install, mid-migration) — the home page must render successfully and
    surface the emptiness, not 500 and not silently render an empty list
    that reads as a scraper failure.
    """
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(checkpoint, [])
    build_viewer_db(viewer)

    app = create_app(
        checkpoint_db=checkpoint,
        viewer_db=viewer,
        output_root=output_root,
    )
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    # All 13 tiles still render — court-doesn't-exist vs no-cases-yet
    # must remain distinct (courts always exist, cases might not).
    tiles = soup.select("[data-testid=court-tile]")
    assert len(tiles) == 13
    # No recent-case rows; an explicit empty-state marker replaces them.
    assert soup.select("[data-testid=recent-case]") == []
    assert soup.select_one(".empty") is not None
