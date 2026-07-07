"""Tests for GET /search — FTS form + BM25 results (Phase 4 route 8).

Full page (form + results embedded). Route 9 (/search/results) will
serve the HTMX partial for pagination without full reload.

FTS5 details:
  * Trigram tokenizer (design §4). Queries < 3 chars yield no rows.
  * ``snippet(fts_body, 1, '<mark>', '</mark>', '…', 32)`` for column 1
    (body). Highlight markers are the CSS contract.
  * BM25 ranking — smaller score is more relevant per SQLite docs.
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
            ("hkca",  2019, 10, "[2019] HKCA 10", "Employment case",
             "2019-08-08", "downloaded"),
            ("hkcfi", 2021, 22, "[2021] HKCFI 22", "Injunction application",
             "2021-11-11", "downloaded"),
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
                    "The court considered breach of contract in "
                    "commercial dealings. The plaintiff sought damages "
                    "for late delivery of goods."
                ),
            },
            {
                "case_key": "hkca/2019/10", "lang": "en",
                "court": "hkca", "year": 2019, "number": 10,
                "neutral": "[2019] HKCA 10", "title": "Employment case",
                "date": "2019-08-08",
                "body": (
                    "The tribunal ruled on wrongful termination and "
                    "the calculation of severance payment."
                ),
            },
            {
                "case_key": "hkcfi/2021/22", "lang": "en",
                "court": "hkcfi", "year": 2021, "number": 22,
                "neutral": "[2021] HKCFI 22", "title": "Injunction application",
                "date": "2021-11-11",
                "body": (
                    "The court granted an interlocutory injunction "
                    "preventing disposition of the disputed goods "
                    "pending trial."
                ),
            },
        ],
    )
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_search_no_query_shows_form_only(client: TestClient) -> None:
    """GET /search renders the search form; no results section yet."""
    resp = client.get("/search")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.select_one("[data-testid=search-form]")
    assert form is not None
    q_input = form.select_one('input[name="q"]')
    assert q_input is not None
    # No results section rendered when no query.
    assert soup.select("[data-testid=search-result]") == []


def test_search_form_action_targets_search_route(client: TestClient) -> None:
    """Form's method + action pin the round-trip. Default GET so query
    lives in the URL (bookmarkable, sharable, browser-back-safe).
    """
    resp = client.get("/search")
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.select_one("[data-testid=search-form]")
    # GET is default; explicit method=get or omitted both fine.
    method = (form.get("method") or "get").lower()
    assert method == "get"
    assert form.get("action") in ("/search", "")


def test_search_query_with_hits_returns_result_rows(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "contract"})
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=search-result]")
    assert len(rows) >= 1
    keys = {r.get("data-case-key") for r in rows}
    assert "hkcfa/2020/1" in keys


def test_search_query_no_hits_shows_empty_state(client: TestClient) -> None:
    """L5 distinct from no-query — user searched, got zero rows."""
    resp = client.get("/search", params={"q": "asteroid"})
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=search-result]") == []
    assert soup.select_one("[data-testid=empty-search]") is not None


def test_search_result_row_shows_metadata(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    row = next(
        r for r in soup.select("[data-testid=search-result]")
        if r.get("data-case-key") == "hkcfa/2020/1"
    )
    text = row.get_text()
    assert "[2020] HKCFA 1" in text
    assert "Contract dispute" in text
    assert "2020-05-05" in text


def test_search_result_snippet_carries_highlight_marks(
    client: TestClient,
) -> None:
    """Design §9: ``FTS_HIGHLIGHT_START = '<mark>'`` — snippet must emit
    the marker around matched substrings. If the snippet has ``<mark>``
    literally in it (rendered NOT escaped), the CSS contract is intact.
    """
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    row = next(
        r for r in soup.select("[data-testid=search-result]")
        if r.get("data-case-key") == "hkcfa/2020/1"
    )
    snippet = row.select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.find("mark") is not None


def test_search_result_row_links_to_detail(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    row = next(
        r for r in soup.select("[data-testid=search-result]")
        if r.get("data-case-key") == "hkcfa/2020/1"
    )
    anchor = row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2020/1"


def test_search_form_preserves_query_after_submission(
    client: TestClient,
) -> None:
    """After a search, the form's input value should carry ``q`` so the
    user sees what they searched for — L5 avoids the 'am I still on the
    results page?' confusion when the box goes blank.
    """
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    q_input = soup.select_one('input[name="q"]')
    assert q_input is not None
    assert q_input.get("value") == "contract"


def test_search_query_with_quote_does_not_500(client: TestClient) -> None:
    """L2 safety: unescaped user input containing a double quote must
    not raise a SQL syntax error — the route escapes or wraps the query.
    """
    resp = client.get("/search", params={"q": 'foo"bar'})
    assert resp.status_code == 200
