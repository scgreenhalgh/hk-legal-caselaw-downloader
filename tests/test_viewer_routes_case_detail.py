"""Tests for GET /case/{slug}/{year}/{number} — case detail (Phase 4 route 4).

Shape (design §5 / §7):
  * Metadata: neutral, title, date, court
  * Body: rendered HTML wrapped in ``<article lang="…">`` per §9
  * Three tab shells for cited-by / authorities / parallel — HTMX
    lazy loads to routes 5-7 (this file pins the ``hx-get`` URLs)
  * 404 on unknown (court, year, number)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases


_NATIVE_HKLII_HTML = (
    "<html><head><title>x</title></head>"
    "<body><form name=\"search_body\">"
    "<p>The court held that ABC.</p>"
    "<p>Costs to the respondent.</p>"
    "</form></body></html>"
)


def _write_body(output_root: Path, court: str, year: int, number: int, html: str) -> None:
    d = output_root / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{court}_{year}_{number}.html").write_text(html, encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "P v Q", "2020-05-05", "downloaded"),
        ],
    )
    _write_body(output_root, "hkcfa", 2020, 1, _NATIVE_HKLII_HTML)
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_case_detail_returns_200_html(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_case_detail_404_on_unknown_case(client: TestClient) -> None:
    """Case tuple not in ``cases`` → 404 (design §5)."""
    resp = client.get("/case/hkcfa/2020/999")
    assert resp.status_code == 404


def test_case_detail_404_on_unknown_court_slug(client: TestClient) -> None:
    """Even if the slug isn't in CANONICAL_COURTS, 404 (not 500)."""
    resp = client.get("/case/hkxyz/2020/1")
    assert resp.status_code == 404


def test_case_detail_shows_metadata(client: TestClient) -> None:
    """Neutral, title, court, and date must all surface as visible text."""
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.select_one("[data-testid=case-metadata]")
    assert meta is not None
    text = meta.get_text()
    assert "[2020] HKCFA 1" in text
    assert "P v Q" in text
    assert "2020-05-05" in text
    assert "HKCFA" in text


def test_case_detail_renders_body_content(client: TestClient) -> None:
    """Sanitized body text survives — unwrap form, keep <p>."""
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.select_one("[data-testid=case-body] article")
    assert body is not None
    text = body.get_text()
    assert "The court held that ABC." in text
    assert "Costs to the respondent." in text


def test_case_detail_body_article_carries_bcp47_lang(client: TestClient) -> None:
    """Design §9: ``<article lang="{{ body_lang | bcp47 }}">``. For 'en'
    body_lang, the article's lang attr must be 'en' (not 'zh-Hant').
    """
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    article = soup.select_one("[data-testid=case-body] article")
    assert article is not None
    assert article.get("lang") == "en"


def test_case_detail_cited_by_tab_hx_get_pins_route_5(client: TestClient) -> None:
    """HTMX shell for cited-by lazy load. Pins route 5's URL:
    /case/{c}/{y}/{n}/cited-by. hx-target + hx-swap fill the shell.
    """
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    tab = soup.select_one("[data-testid=cited-by-tab]")
    assert tab is not None
    assert tab.get("hx-get") == "/case/hkcfa/2020/1/cited-by"
    # Panel targeted by hx-target must exist in the DOM with the right id.
    target_sel = tab.get("hx-target")
    assert target_sel is not None
    assert soup.select_one(target_sel) is not None


def test_case_detail_authorities_tab_hx_get_pins_route_6(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    tab = soup.select_one("[data-testid=authorities-tab]")
    assert tab is not None
    assert tab.get("hx-get") == "/case/hkcfa/2020/1/authorities"
    target_sel = tab.get("hx-target")
    assert soup.select_one(target_sel) is not None


def test_case_detail_parallel_tab_hx_get_pins_route_7(client: TestClient) -> None:
    resp = client.get("/case/hkcfa/2020/1")
    soup = BeautifulSoup(resp.text, "html.parser")
    tab = soup.select_one("[data-testid=parallel-tab]")
    assert tab is not None
    assert tab.get("hx-get") == "/case/hkcfa/2020/1/parallel"
    target_sel = tab.get("hx-target")
    assert soup.select_one(target_sel) is not None


def test_case_detail_missing_body_file_still_renders(tmp_path: Path) -> None:
    """L5 ambiguous state: case row exists in DB but no body on disk
    (mid-download, orphaned, or scrape gap). Route must still render
    metadata + shell + tabs rather than 404 — the case ROW exists.
    Design §5 line 121: missing render_source → empty article shell.
    """
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            ("hkcfa", 2020, 42, "[2020] HKCFA 42", "no body v yet",
             "2020-01-01", "orphaned"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    client = TestClient(app)
    resp = client.get("/case/hkcfa/2020/42")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    # Body shell present but empty.
    article = soup.select_one("[data-testid=case-body] article")
    assert article is not None
    # Metadata still visible.
    meta = soup.select_one("[data-testid=case-metadata]")
    assert meta is not None
    assert "[2020] HKCFA 42" in meta.get_text()
