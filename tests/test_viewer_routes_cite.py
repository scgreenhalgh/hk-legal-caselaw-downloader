"""Tests for GET /cite/{neutral} — neutral-citation resolver (Phase 4 route 26).

Path resolves a URL-encoded neutral like ``/cite/[2020]%20HKCFA%2015``:
  * Hit (parse ok, case in cases) → 302 → /case/{court}/{year}/{number}
  * Miss (parse ok, case absent) → 200 renders ``cite_unresolved.html``
    with the neutral echoed + a /search fallback link
  * Malformed (parse returns None) → 404

Design §5 line 132 + docs/viewer-design.md §11 line 371 pin the
200-on-miss shape: NEVER a silent 302 to the homepage (L5 ambiguous
state — the reader must be told the citation didn't resolve).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app
from hklii_downloader.viewer.routes.cite import router as cite_router

from tests._route_helpers import build_viewer_db, seed_cases


def _cite_url(neutral: str) -> str:
    """Percent-encode a neutral citation into a ``/cite/{neutral}`` URL.

    ``safe=''`` escapes brackets + spaces so the wire form matches what
    a browser would produce when linkified anchors are clicked.
    """
    return f"/cite/{quote(neutral, safe='')}"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            ("hkcfa", 2020, 15, "[2020] HKCFA 15", "A v B",
             "2020-11-11", "downloaded"),
        ],
    )
    # A second row with lang='tc' to simulate a bilingual case. cases.PK
    # is (court, year, number), so bilingual halves cannot coexist as
    # separate rows — the language column varies per row, not the tuple.
    # The cite resolver must still redirect to the plain /case URL
    # regardless of the case's ``lang`` marker.
    conn = sqlite3.connect(str(checkpoint))
    try:
        conn.execute(
            "INSERT INTO cases (court, year, number, neutral, title, "
            "date, status, lang) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("hkca", 2019, 7, "[2019] HKCA 7", "香港特別行政區 v X",
             "2019-04-04", "downloaded", "tc"),
        )
        conn.commit()
    finally:
        conn.close()
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    # Route module is not mounted in the shipped ``create_app`` yet —
    # synthesis stage will wire it. The test fixture attaches the
    # router locally so we can pin behaviour before that lands.
    app.include_router(cite_router)
    return TestClient(app)


def test_cite_hit_302_redirects_to_case_detail(client: TestClient) -> None:
    """L4 wire-side: Location header pins /case/{court}/{year}/{number}
    with the parsed lowercase court slug. Status = 302 (temporary) so
    the browser's back button still lands on the referring page rather
    than skipping past ``/cite/``.
    """
    resp = client.get(_cite_url("[2020] HKCFA 15"), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/case/hkcfa/2020/15"


def test_cite_miss_returns_200_unresolved(client: TestClient) -> None:
    """L5 ambiguous state: parse succeeded, case not in DB → 200 with
    the ``cite-unresolved`` marker. Design §5 line 132: NEVER a silent
    302 to the homepage — reader must be told the citation didn't
    resolve.
    """
    resp = client.get(_cite_url("[1999] HKCFA 99"), follow_redirects=False)
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one("[data-testid=cite-unresolved]") is not None


def test_cite_miss_echoes_neutral_text(client: TestClient) -> None:
    """User sees the exact citation they clicked as an orientation cue —
    'we understood what you asked for; we just don't have that case'.
    """
    resp = client.get(_cite_url("[1999] HKCFA 99"), follow_redirects=False)
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.select_one("[data-testid=cite-unresolved]")
    assert body is not None
    assert "[1999] HKCFA 99" in body.get_text()


def test_cite_miss_offers_search_link(client: TestClient) -> None:
    """Unresolved page carries a /search?q=<neutral> escape hatch —
    never a dead end (L1 silent-skip lens: the user's action must lead
    somewhere they can act on).
    """
    resp = client.get(_cite_url("[1999] HKCFA 99"), follow_redirects=False)
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.select("[data-testid=cite-unresolved] a[href]")
    search_hrefs = [
        a["href"] for a in anchors
        if a["href"].startswith("/search")
    ]
    assert len(search_hrefs) >= 1
    href = search_hrefs[0]
    # Query string carries the citation parts so the search actually
    # runs against something meaningful (not an empty ``?q=``).
    assert "1999" in href
    assert "HKCFA" in href
    assert "99" in href


def test_cite_malformed_returns_404(client: TestClient) -> None:
    """L2 semantic drift: parse returns None → 404, NOT the 200
    unresolved page. Distinguishes 'not a citation' (bad input) from
    'no matching case' (good input, empty corpus). Collapsing these
    two states would hide the difference between a broken linkifier
    upstream and a legitimate corpus gap.
    """
    resp = client.get(_cite_url("not a citation"), follow_redirects=False)
    assert resp.status_code == 404


def test_cite_bilingual_case_resolves_to_plain_case_url(
    client: TestClient,
) -> None:
    """L4 wire-side: A case row with ``lang='tc'`` still resolves to the
    plain /case/{court}/{year}/{number} URL — no lang suffix, no query
    string. The case_detail route owns lang selection separately, and
    /cite must never bake a language into its redirect target.
    """
    resp = client.get(_cite_url("[2019] HKCA 7"), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/case/hkca/2019/7"
