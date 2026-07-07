"""Tests for GET /court/{slug}/{year} — paginated case list (Phase 4 route 3).

Design §6:
  * Fixed 5-column table (neutral, parties, date, formats, inbound_count)
  * Default sort ``date_desc``
  * Pagination: ``?page=N`` only, size=50 fixed
  * Empty year renders '0 cases in {court} {year}' + years sidebar,
    NOT 404 (court exists, year just has no rows)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases


def _make_year_cases(court: str, year: int, count: int) -> list[tuple]:
    """Fabricate ``count`` cases in ``court/year`` with distinct descending dates.

    Case n gets date ``{year}-12-{31 - n:02d}`` (approx; wraps into
    earlier months at high n) so ORDER BY date DESC yields n=1, 2, 3, …
    """
    rows: list[tuple] = []
    for n in range(1, count + 1):
        day = 31 - ((n - 1) % 28)  # keep in 4..31, no month-overflow risk
        month = 12 - ((n - 1) // 28)
        rows.append(
            (
                court,
                year,
                n,
                f"[{year}] HKCFA {n}",
                f"Party{n} v Other{n}",
                f"{year}-{month:02d}-{day:02d}",
                "downloaded",
            )
        )
    return rows


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            *_make_year_cases("hkcfa", 2023, 55),
            # hkcfa 2024 — control, must not appear on /court/hkcfa/2023
            ("hkcfa", 2024, 100, "[2024] HKCFA 100", "AA v BB",
             "2024-01-01", "downloaded"),
            # hkca 2023 — control, different court
            ("hkca", 2023, 5, "[2023] HKCA 5", "CC v DD",
             "2023-05-05", "downloaded"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_year_page_returns_200_html(client: TestClient) -> None:
    resp = client.get("/court/hkcfa/2023")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_year_page_404_on_unknown_court(client: TestClient) -> None:
    resp = client.get("/court/hkxyz/2023")
    assert resp.status_code == 404


def test_year_page_empty_year_shows_empty_state_not_404(client: TestClient) -> None:
    """Design §6: 'Court exists / 0 rows in year → 0 cases … NOT 404'."""
    resp = client.get("/court/hkcfa/1990")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=case-row]") == []
    assert soup.select_one("[data-testid=empty-year]") is not None


def test_year_page_row_shows_neutral_title_date(client: TestClient) -> None:
    """Each row surfaces the neutral cite, parties title, and date."""
    resp = client.get("/court/hkcfa/2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=case-row]")
    assert len(rows) > 0
    first_text = rows[0].get_text()
    assert "[2023] HKCFA 1" in first_text
    assert "Party1 v Other1" in first_text
    assert "2023-12-31" in first_text


def test_year_page_default_sort_is_date_desc(client: TestClient) -> None:
    """Design §6: 'Sort: date_desc (default) | date_asc | neutral_asc'."""
    resp = client.get("/court/hkcfa/2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    dates = [r.get("data-date") for r in soup.select("[data-testid=case-row]")]
    assert dates == sorted(dates, reverse=True)


def test_year_page_row_links_to_detail_route(client: TestClient) -> None:
    """Row anchor pins route 4's URL shape: /case/{court}/{year}/{number}."""
    resp = client.get("/court/hkcfa/2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    first_row = soup.select_one("[data-testid=case-row]")
    anchor = first_row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2023/1"


def test_year_page_page_1_holds_50_rows(client: TestClient) -> None:
    """Design §6: 'size=50 hardcoded'. Page 1 must hold exactly 50."""
    resp = client.get("/court/hkcfa/2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert len(soup.select("[data-testid=case-row]")) == 50


def test_year_page_page_2_holds_the_remainder(client: TestClient) -> None:
    """55 seeded → page 1 has 50, page 2 has 5 (no gap, no overlap)."""
    resp = client.get("/court/hkcfa/2023?page=2")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=case-row]")
    assert len(rows) == 5


def test_year_page_only_lists_this_court_year(client: TestClient) -> None:
    """L2 semantic drift: hkcfa 2024 and hkca 2023 are seeded controls;
    neither may appear on /court/hkcfa/2023.
    """
    resp = client.get("/court/hkcfa/2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    keys = {
        r.get("data-case-key")
        for r in soup.select("[data-testid=case-row]")
    }
    assert "hkcfa/2024/100" not in keys
    assert "hkca/2023/5" not in keys


def test_year_page_pagination_marker_reflects_current_page(
    client: TestClient,
) -> None:
    """Page marker exposes current page + total pages so template drift is
    caught (L4 wrong-side test: the pagination widget IS the observable
    contract, not the internal offset math).
    """
    resp = client.get("/court/hkcfa/2023?page=2")
    soup = BeautifulSoup(resp.text, "html.parser")
    pager = soup.select_one("[data-testid=pager]")
    assert pager is not None
    assert pager.get("data-page") == "2"
    assert pager.get("data-total-pages") == "2"
