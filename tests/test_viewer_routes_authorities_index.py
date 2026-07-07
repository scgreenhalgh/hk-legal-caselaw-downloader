"""Tests for GET /authorities — hub-case leaderboard index (route 31).

Standalone deep hub index, distinct from the case-scoped
``/case/{c}/{y}/{n}/authorities`` HTMX partial owned by
``viewer/routes/citations.py``. Direction here is INBOUND: rows are
the corpus's most-cited cases, ranked by ``inbound_count``.

Design §7 contract:
  * Sourced from ``viewer.db.viewer_hub_cache`` via
    :func:`viewer.graph.hub_cases`
  * Court facet ``?court=<slug>`` narrows to one slug
  * Order: ``inbound_count DESC, case_key ASC`` (stable tiebreak)
  * Empty-cache banner (table absent) surfaces the L1 setup signal —
    distinct from L5 table-present-but-empty
  * Row shape: rank (curial Roman) + case_key + court name +
    inbound_count

Fixture strategy: per-file, following the pattern in
``test_viewer_routes_court.py``. Rule of Three has not yet fired for a
session-scoped fixture across route tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app
from hklii_downloader.viewer.routes.authorities import (
    router as authorities_router,
)

from tests._route_helpers import (
    build_viewer_db,
    drop_hub_cache_table,
    seed_cases,
    seed_hub_cache,
)


_SEED_CASES = [
    ("hkcfa", 2020, 1, "[2020] HKCFA 1", "P1 v Q1", "2020-01-15", "downloaded"),
    ("hkcfa", 2023, 8, "[2023] HKCFA 8", "P2 v Q2", "2023-02-01", "downloaded"),
    ("hkca",  2024, 88, "[2024] HKCA 88", "PP v QQ", "2024-11-01", "downloaded"),
    ("hkdc",  2024, 50, "[2024] HKDC 50", "R v S",   "2024-06-01", "downloaded"),
]


# Ordering-across-courts fixture. hkca/2024/88 has the highest inbound
# (200) but is NOT the apex court — the route must NOT sort by curial
# rank; the design pins inbound_count DESC as the sole primary key.
# hkdc/2024/50 with inbound=1 exists to pin the min_inbound bound —
# if the route silently used the graph default (5), this row would
# vanish and the row-count assertion below would fail.
_SEED_HUB = [
    ("hkcfa/2020/1", 150, "2026-07-06T00:00:00"),
    ("hkca/2024/88", 200, "2026-07-06T00:00:00"),
    ("hkcfa/2023/8", 120, "2026-07-06T00:00:00"),
    ("hkdc/2024/50",   1, "2026-07-06T00:00:00"),
]


def _build_app(
    tmp_path: Path,
    *,
    drop_hub_table: bool = False,
    seed_hub: bool = True,
) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(checkpoint, _SEED_CASES)
    build_viewer_db(viewer)
    if seed_hub:
        seed_hub_cache(viewer, _SEED_HUB)
    if drop_hub_table:
        drop_hub_cache_table(viewer)
    app = create_app(
        checkpoint_db=checkpoint,
        viewer_db=viewer,
        output_root=output_root,
    )
    # app.py is owned by the synthesis stage; the test wires the router
    # here so the route stack is exercised end-to-end without touching
    # the factory.
    app.include_router(authorities_router)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path)


@pytest.fixture
def client_no_hub_cache(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path, drop_hub_table=True)


@pytest.fixture
def client_empty_hub_rows(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path, seed_hub=False)


def test_authorities_index_returns_200_html(client: TestClient) -> None:
    resp = client.get("/authorities")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_authorities_index_orders_by_inbound_desc_then_key(
    client: TestClient,
) -> None:
    """Design §7: ``inbound_count DESC, case_key ASC`` tiebreak.

    Seed:
      hkca/2024/88 → 200
      hkcfa/2020/1 → 150
      hkcfa/2023/8 → 120
      hkdc/2024/50 →   1  (pinned to prove ``min_inbound`` is inclusive)
    """
    resp = client.get("/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=hub-row]")
    keys = [r.get("data-case-key") for r in rows]
    counts = [int(r.get("data-inbound")) for r in rows]
    assert keys == [
        "hkca/2024/88",
        "hkcfa/2020/1",
        "hkcfa/2023/8",
        "hkdc/2024/50",
    ]
    assert counts == [200, 150, 120, 1]


def test_authorities_index_hub_cache_missing_shows_banner(
    client_no_hub_cache: TestClient,
) -> None:
    """viewer_hub_cache table missing → banner + 200 + no rows.

    L1 lens: 'setup was skipped' is distinct from 'setup ran, no data'
    (see the next test). The banner marker must be present and no hub
    rows must render.
    """
    resp = client_no_hub_cache.get("/authorities")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    banner = soup.select_one("[data-testid=hub-cache-missing]")
    assert banner is not None
    # Design §7 exact wording (task-authoritative). If the template
    # drifts to the older 'hklii scrape-noteup' phrasing, this fails.
    assert "hklii viewer index" in banner.get_text()
    assert soup.select("[data-testid=hub-row]") == []
    # The empty-rows marker must NOT double-render here — the two
    # empty states are exclusive.
    assert soup.select_one("[data-testid=empty-hubs]") is None


def test_authorities_index_empty_hub_rows_distinct_from_missing(
    client_empty_hub_rows: TestClient,
) -> None:
    """Table exists but has zero rows → distinct empty state.

    L5 ambiguous-state: the L1 'run indexer' banner must NOT appear
    (that promises setup work that already happened). The row set is
    empty via a separate marker.
    """
    resp = client_empty_hub_rows.get("/authorities")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one("[data-testid=hub-cache-missing]") is None
    assert soup.select_one("[data-testid=empty-hubs]") is not None
    assert soup.select("[data-testid=hub-row]") == []


def test_authorities_index_court_filter_narrows_to_slug(
    client: TestClient,
) -> None:
    """?court=hkcfa → only hkcfa/… case_keys appear.

    L2 semantic-drift: the facet FILTERS the set; it does not just
    re-order it. hkca/2024/88 has the highest inbound but is dropped.
    hkdc/2024/50 has the lowest inbound but is also dropped (not hkcfa).
    Order among the survivors still honours ``inbound_count DESC``.
    """
    resp = client.get("/authorities?court=hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=hub-row]")
    keys = [r.get("data-case-key") for r in rows]
    assert keys == ["hkcfa/2020/1", "hkcfa/2023/8"]


def test_authorities_index_unknown_court_returns_404(
    client: TestClient,
) -> None:
    """?court=hkxyz → 404. Mirrors /court/{slug}'s canonical-slug guard."""
    resp = client.get("/authorities?court=hkxyz")
    assert resp.status_code == 404


def test_authorities_index_row_anchor_url_shape(client: TestClient) -> None:
    """Row anchor href resolves to ``/case/{case_key}``.

    Pins the URL shape at the leaderboard's dominant navigation
    affordance — a case-detail-route rename must break this test, not
    silently dead-link every leaderboard row.
    """
    resp = client.get("/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    first_row = soup.select_one("[data-testid=hub-row]")
    assert first_row is not None
    anchor = first_row.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkca/2024/88"


def test_authorities_index_row_shape_has_rank_and_court_name(
    client: TestClient,
) -> None:
    """Row surfaces curial Roman rank + court name.

    Design §7 row shape: rank (curial Roman) + case_key + inbound_count
    + court name. case_key and inbound are pinned via data-* attrs in
    other tests; here we confirm the two humanised elements render into
    the row's visible text.
    """
    resp = client.get("/authorities")
    soup = BeautifulSoup(resp.text, "html.parser")
    hkca_row = soup.select_one(
        "[data-testid=hub-row][data-case-key='hkca/2024/88']"
    )
    assert hkca_row is not None
    row_text = hkca_row.get_text()
    # hkca curial rank is Ⅱ (Unicode Roman numeral U+2161).
    assert "Ⅱ" in row_text
    assert "Court of Appeal" in row_text
