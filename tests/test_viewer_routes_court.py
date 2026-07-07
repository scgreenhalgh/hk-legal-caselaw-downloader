"""Tests for GET /court/{slug} — court landing (Phase 4 route 2).

Landing shape (design §6):
  * Year buckets with counts, DESC
  * Top hub cases in this court (when viewer_hub_cache populated)
  * "Cache not built" banner when viewer_hub_cache table missing —
    L1 signal that the indexer never ran, distinct from L5 populated-
    but-no-hubs-in-court (empty list, no banner)

Fixture strategy: still per-file until Route 3 lands (Rule of Three).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import (
    build_viewer_db,
    drop_hub_cache_table,
    seed_cases,
    seed_hub_cache,
)


_SEED_CASES = [
    # hkcfa: 3 year buckets
    ("hkcfa", 2024, 15, "[2024] HKCFA 15", "P1 v Q1", "2024-11-20", "downloaded"),
    ("hkcfa", 2024, 10, "[2024] HKCFA 10", "P2 v Q2", "2024-05-05", "downloaded"),
    ("hkcfa", 2023, 30, "[2023] HKCFA 30", "P3 v Q3", "2023-10-01", "downloaded"),
    ("hkcfa", 2023, 22, "[2023] HKCFA 22", "P4 v Q4", "2023-06-01", "downloaded"),
    ("hkcfa", 2023, 8,  "[2023] HKCFA 8",  "P5 v Q5", "2023-02-01", "downloaded"),
    ("hkcfa", 2020, 1,  "[2020] HKCFA 1",  "P6 v Q6", "2020-01-15", "downloaded"),
    # hkca control — must NOT leak into /court/hkcfa
    ("hkca",  2024, 88, "[2024] HKCA 88",  "PP v QQ", "2024-11-01", "downloaded"),
]


_SEED_HUB = [
    ("hkcfa/2020/1", 150, "2026-07-06T00:00:00"),
    ("hkcfa/2023/8", 120, "2026-07-06T00:00:00"),
    ("hkcfa/2024/10", 80, "2026-07-06T00:00:00"),
    # hkca control — must NOT leak into hkcfa's hub panel
    ("hkca/2024/88", 200, "2026-07-06T00:00:00"),
]


def _build_app(tmp_path: Path, drop_hub_table: bool = False) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(checkpoint, _SEED_CASES)
    build_viewer_db(viewer)
    seed_hub_cache(viewer, _SEED_HUB)
    if drop_hub_table:
        drop_hub_cache_table(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path)


@pytest.fixture
def client_no_hub_cache(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path, drop_hub_table=True)


def test_court_landing_returns_200_html(client: TestClient) -> None:
    resp = client.get("/court/hkcfa")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_court_landing_404_on_unknown_court(client: TestClient) -> None:
    """Design §6: 'Unknown court → 404'."""
    resp = client.get("/court/hkxyz")
    assert resp.status_code == 404


def test_court_landing_shows_year_buckets_desc(client: TestClient) -> None:
    """Year buckets grouped and ordered by year DESC with correct counts."""
    resp = client.get("/court/hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    buckets = soup.select("[data-testid=year-bucket]")
    years = [int(b.get("data-year")) for b in buckets]
    counts = [int(b.get("data-count")) for b in buckets]
    assert years == [2024, 2023, 2020]
    assert counts == [2, 3, 1]


def test_year_bucket_links_to_court_year_route(client: TestClient) -> None:
    """Bucket anchor href pins route 3's URL shape: /court/{slug}/{year}."""
    resp = client.get("/court/hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    bucket_2024 = next(
        b for b in soup.select("[data-testid=year-bucket]")
        if b.get("data-year") == "2024"
    )
    anchor = bucket_2024.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/court/hkcfa/2024"


def test_court_landing_shows_hub_cases_when_cache_populated(
    client: TestClient,
) -> None:
    """Hub cases panel lists this court's hubs, ordered by inbound_count DESC."""
    resp = client.get("/court/hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    hubs = soup.select("[data-testid=hub-case]")
    keys = [h.get("data-case-key") for h in hubs]
    assert keys == ["hkcfa/2020/1", "hkcfa/2023/8", "hkcfa/2024/10"]
    counts = [int(h.get("data-inbound")) for h in hubs]
    assert counts == [150, 120, 80]


def test_court_landing_hub_panel_isolated_to_this_court(client: TestClient) -> None:
    """hkca/2024/88 has 200 inbound (higher than any hkcfa hub) but MUST NOT
    appear on /court/hkcfa — L2 semantic-drift: the panel filters by court,
    not just orders by rank.
    """
    resp = client.get("/court/hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    keys = {
        h.get("data-case-key")
        for h in soup.select("[data-testid=hub-case]")
    }
    assert "hkca/2024/88" not in keys


def test_court_landing_empty_court_renders_without_year_buckets(
    client: TestClient,
) -> None:
    """A canonical court with no seeded cases (hkoat) returns 200 with an
    'empty' marker in place of year buckets. L5: empty-court vs unknown-
    court must be distinguishable — this is empty, the /court/hkxyz test
    covers the unknown case.
    """
    resp = client.get("/court/hkoat")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select("[data-testid=year-bucket]") == []
    assert soup.select_one("[data-testid=empty-years]") is not None


def test_court_landing_hub_cache_missing_shows_banner(
    client_no_hub_cache: TestClient,
) -> None:
    """viewer_hub_cache table missing → banner surfaces the L1 setup signal,
    year buckets still render (the browse angle doesn't depend on the cache).
    """
    resp = client_no_hub_cache.get("/court/hkcfa")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    # Year buckets still render — browse works without hub cache.
    assert len(soup.select("[data-testid=year-bucket]")) == 3
    # Hub panel replaced by banner (or omitted, but marker present).
    assert soup.select_one("[data-testid=hub-cache-missing]") is not None
    # No hub-case rows.
    assert soup.select("[data-testid=hub-case]") == []
