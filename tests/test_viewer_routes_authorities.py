"""Tests for GET /case/{c}/{y}/{n}/authorities — HTMX partial (route 6).

Direction: outbound. Uses ``graph.authorities_cited`` — order by cited
court rank ASC (over ``to_key``), first_seen DESC within court. Row
anchors resolve to the CITED case, not the citer.
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
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "citer v respondent",
             "2020-05-05", "downloaded"),
        ],
    )
    # hkcfa/2020/1 CITES four other cases spanning three courts.
    seed_citations(
        checkpoint,
        [
            ("hkcfa/2020/1", "hkcfa/2019/50", "en", 8, 1, "2019-06-01T00:00:00"),
            ("hkcfa/2020/1", "hkca/2018/524", "en", 5, 1, "2020-01-01T00:00:00"),
            ("hkcfa/2020/1", "hkcfi/2021/99", "en", 3, 1, "2021-03-01T00:00:00"),
            ("hkcfa/2020/1", "hkcfi/2020/22", "en", 2, 1, "2020-05-01T00:00:00"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_authorities_partial_returns_200_html(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1/authorities")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_authorities_partial_is_a_fragment_not_full_page(
    client: TestClient,
) -> None:
    resp = client.get("/case/hkcfa/2020/1/authorities")
    text = resp.text.lower()
    assert "<html" not in text
    assert "<body" not in text
    assert "<!doctype" not in text


def test_authorities_orders_by_to_court_rank_then_first_seen_desc(
    client: TestClient,
) -> None:
    """Symmetric to cited-by: to_court rank ASC (hkcfa 0 < hkca 1 <
    hkcfi 2), first_seen DESC as tiebreak within court.
    """
    resp = client.get("/case/hkcfa/2020/1/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=authorities-row]")
    keys = [r.get("data-to-key") for r in rows]
    assert keys == [
        "hkcfa/2019/50",
        "hkca/2018/524",
        "hkcfi/2021/99",
        "hkcfi/2020/22",
    ]


def test_authorities_row_shows_to_court_and_first_seen(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    row = soup.select("[data-testid=authorities-row]")[0]
    assert row.get("data-to-court") == "hkcfa"
    assert row.get("data-first-seen") == "2019-06-01T00:00:00"


def test_authorities_row_anchor_links_to_cited_detail(
    client: TestClient,
) -> None:
    """L2 direction guard: anchor points at the CITED case (to_key), not
    the citer (from_key). Swapping these would break the panel's core
    'jump to this authority' affordance.
    """
    resp = client.get("/case/hkcfa/2020/1/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    row = soup.select("[data-testid=authorities-row]")[0]
    anchor = row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2019/50"


def test_authorities_empty_case_shows_empty_marker(client: TestClient) -> None:
    """A case that cites nothing (or a case_key with no outbound rows)
    — L5 distinct from a not-yet-loaded panel.
    """
    resp = client.get("/case/hkcfa/2020/9999/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=authorities-row]") == []
    assert soup.select_one("[data-testid=empty-authorities]") is not None


def test_authorities_load_more_absent_when_short_page(
    client: TestClient,
) -> None:
    resp = client.get("/case/hkcfa/2020/1/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one("[data-testid=load-more]") is None
