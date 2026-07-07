"""Tests for GET /case/{c}/{y}/{n}/parallel — HTMX partial (route 7).

Parallel-cite panel — small (11k corpus-wide), unpaginated, sorted ASC.
No court facet. Row content is a plain reporter cite string, not a
case_key.
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
    seed_parallel_cites,
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
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "P v Q",
             "2020-05-05", "downloaded"),
        ],
    )
    seed_parallel_cites(
        checkpoint,
        [
            ("hkcfa/2020/1", "[2020] 6 HKC 46"),
            ("hkcfa/2020/1", "(2020) 23 HKCFAR 15"),
            ("hkcfa/2020/1", "[2020] HKLRD 300"),
            # Control: parallel cite for a DIFFERENT case must not leak.
            ("hkca/2020/88", "[2020] 4 HKC 100"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_parallel_partial_returns_200_html(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1/parallel")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_parallel_partial_is_a_fragment_not_full_page(
    client: TestClient,
) -> None:
    resp = client.get("/case/hkcfa/2020/1/parallel")
    text = resp.text.lower()
    assert "<html" not in text
    assert "<body" not in text
    assert "<!doctype" not in text


def test_parallel_lists_cites_ascending(client: TestClient) -> None:
    """graph.parallel_cites returns ASC-sorted strings. The partial
    must render in the same order — L3 docstring pin.
    """
    resp = client.get("/case/hkcfa/2020/1/parallel")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=parallel-row]")
    texts = [r.get_text(strip=True) for r in rows]
    assert texts == sorted(texts)
    assert texts == [
        "(2020) 23 HKCFAR 15",
        "[2020] 6 HKC 46",
        "[2020] HKLRD 300",
    ]


def test_parallel_case_isolation(client: TestClient) -> None:
    """L2 semantic-drift: hkca/2020/88 has a parallel cite in the fixture
    but must NOT appear on hkcfa/2020/1's panel.
    """
    resp = client.get("/case/hkcfa/2020/1/parallel")
    soup = BeautifulSoup(resp.text, "html.parser")
    text = resp.text
    assert "[2020] 4 HKC 100" not in text


def test_parallel_empty_case_shows_empty_marker(client: TestClient) -> None:
    """Case_key with no parallel-cite rows returns an empty-parallel
    marker — L5 vs 'not yet loaded' distinguishable.
    """
    resp = client.get("/case/hkcfa/2020/9999/parallel")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=parallel-row]") == []
    assert soup.select_one("[data-testid=empty-parallel]") is not None


def test_parallel_no_load_more_button(client: TestClient) -> None:
    """Parallel is unpaginated by design (§7 'also cited as'). Even with
    3 rows, no 'Load next 50' button — its presence would be a copy-
    paste-from-cited-by regression.
    """
    resp = client.get("/case/hkcfa/2020/1/parallel")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one("[data-testid=load-more]") is None
