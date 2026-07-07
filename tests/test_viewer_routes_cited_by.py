"""Tests for GET /case/{slug}/{year}/{number}/cited-by — HTMX partial (route 5).

Design §7:
  * Ranking: court_rank ASC, first_seen DESC (all 12 court slugs)
  * Fragment response — meant to swap into #panel-cited-by via
    hx-swap="innerHTML"
  * Pagination: 50/page, "Load next 50" with hx-swap="beforeend"
  * Court facet: single-select ``?court=hkcfa``
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import (
    build_viewer_db,
    seed_cases,
    seed_citations,
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "target v X",
             "2020-05-05", "downloaded"),
        ],
    )
    seed_citations(
        checkpoint,
        [
            # (from_key, to_key, citer_lang, citer_freq, position, first_seen)
            ("hkca/2018/524",  "hkcfa/2020/1", "en", 5, 1, "2020-01-01T00:00:00"),
            ("hkcfa/2019/50",  "hkcfa/2020/1", "en", 8, 1, "2019-06-01T00:00:00"),
            ("hkcfi/2021/99",  "hkcfa/2020/1", "en", 3, 1, "2021-03-01T00:00:00"),
            ("hkcfi/2020/22",  "hkcfa/2020/1", "en", 2, 1, "2020-05-01T00:00:00"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_cited_by_partial_returns_200_html(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_cited_by_partial_is_a_fragment_not_full_page(client: TestClient) -> None:
    """L4 wrong-side test: hx-swap='innerHTML' requires a fragment.
    A leading <html> or full base.html shell means it will insert an
    entire nested document into #panel-cited-by. Fail loudly here.
    """
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    text = resp.text.lower()
    assert "<html" not in text
    assert "<body" not in text
    assert "<!doctype" not in text


def test_cited_by_partial_orders_by_court_rank_then_first_seen_desc(
    client: TestClient,
) -> None:
    """Design §7: 'court_rank ASC, first_seen DESC'.
    hkcfa (rank 0) < hkca (rank 1) < hkcfi (rank 2). Within hkcfi, the
    2021-03-01 citation beats 2020-05-01.
    """
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=cited-by-row]")
    keys = [r.get("data-from-key") for r in rows]
    assert keys == [
        "hkcfa/2019/50",
        "hkca/2018/524",
        "hkcfi/2021/99",
        "hkcfi/2020/22",
    ]


def test_cited_by_row_shows_from_court_and_first_seen(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    soup = BeautifulSoup(resp.text, "html.parser")
    row = soup.select("[data-testid=cited-by-row]")[0]
    assert row.get("data-from-court") == "hkcfa"
    assert row.get("data-first-seen") == "2019-06-01T00:00:00"


def test_cited_by_row_anchor_links_to_citer_detail(client: TestClient) -> None:
    """Row's anchor href pins the /case/{key} shape."""
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    soup = BeautifulSoup(resp.text, "html.parser")
    row = soup.select("[data-testid=cited-by-row]")[0]
    anchor = row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2019/50"


def test_cited_by_court_filter_narrows_to_that_court(client: TestClient) -> None:
    """?court=hkcfi returns only hkcfi citers (single-select per design §7)."""
    resp = client.get("/case/hkcfa/2020/1/cited-by?court=hkcfi")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=cited-by-row]")
    keys = [r.get("data-from-key") for r in rows]
    assert keys == ["hkcfi/2021/99", "hkcfi/2020/22"]


def test_cited_by_empty_case_shows_empty_marker(client: TestClient) -> None:
    """A case with zero inbound citations — L5 distinct from 'not fetched'.
    Empty list rendering must not silently look like a busy indicator.
    """
    resp = client.get("/case/hkcfa/2020/1/cited-by?court=hkoat")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=cited-by-row]") == []
    assert soup.select_one("[data-testid=empty-cited-by]") is not None


def test_cited_by_load_more_absent_when_short_page(client: TestClient) -> None:
    """4 rows < page size (50). No 'load next' button — pagination end."""
    resp = client.get("/case/hkcfa/2020/1/cited-by")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one("[data-testid=load-more]") is None
