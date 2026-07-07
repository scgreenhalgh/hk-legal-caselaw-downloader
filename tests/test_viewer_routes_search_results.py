"""Tests for GET /search/results — HTMX search partial (Phase 4 route 9).

Same BM25 query as /search, but returns a fragment for HTMX swap into
the results container. Empty query returns a minimal empty fragment
(the partial is a slave endpoint — the master /search page owns the
form + validation).
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
    seed_search_index,
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
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "Contract dispute",
             "2020-05-05", "downloaded"),
        ],
    )
    build_viewer_db(viewer)
    seed_search_index(
        viewer,
        [
            {
                "case_key": "hkcfa/2020/1", "lang": "en",
                "court": "hkcfa", "year": 2020, "number": 1,
                "neutral": "[2020] HKCFA 1", "title": "Contract dispute",
                "date": "2020-05-05",
                "body": (
                    "The court considered breach of contract and "
                    "damages for late delivery of goods."
                ),
            },
        ],
    )
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_search_results_returns_200_html(client: TestClient) -> None:
    resp = client.get("/search/results", params={"q": "contract"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_search_results_is_a_fragment_not_full_page(client: TestClient) -> None:
    """L4 wrong-side: hx-swap can only target a fragment."""
    resp = client.get("/search/results", params={"q": "contract"})
    text = resp.text.lower()
    assert "<html" not in text
    assert "<body" not in text
    assert "<!doctype" not in text


def test_search_results_empty_query_returns_empty_fragment(
    client: TestClient,
) -> None:
    """No q — return an empty fragment (not 400). The partial is a slave
    endpoint: the parent page owns validation.
    """
    resp = client.get("/search/results")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=search-result]") == []


def test_search_results_hits_render_result_rows(client: TestClient) -> None:
    resp = client.get("/search/results", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=search-result]")
    assert len(rows) == 1
    assert rows[0].get("data-case-key") == "hkcfa/2020/1"


def test_search_results_no_hits_shows_empty_marker(client: TestClient) -> None:
    resp = client.get("/search/results", params={"q": "asteroid"})
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=search-result]") == []
    assert soup.select_one("[data-testid=empty-search]") is not None


def test_search_results_row_has_snippet_and_anchor(client: TestClient) -> None:
    resp = client.get("/search/results", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    row = soup.select_one("[data-testid=search-result]")
    anchor = row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2020/1"
    snippet = row.select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.find("mark") is not None
