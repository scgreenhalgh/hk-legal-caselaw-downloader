"""Tests for viewer static asset serving.

Static assets (Pico.css, custom app.css) live under ``viewer/static/``
and are served under ``/static/*`` by FastAPI's ``StaticFiles`` mount.

We don't test styling — visual regression is Phase 6+ territory (design
§10 declines Playwright). These tests just prove the mount works and
the vendored file is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [("hkcfa", 2020, 1, "[2020] HKCFA 1", "P v Q",
          "2020-05-05", "downloaded")],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_pico_css_served_at_static_path(client: TestClient) -> None:
    """Vendored Pico.css v2 available at /static/pico.classless.min.css."""
    resp = client.get("/static/pico.classless.min.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    # The Pico banner comment names the framework — proves we shipped it
    # and haven't accidentally replaced the file with an empty stub.
    assert "Pico CSS" in resp.text


def test_app_css_served_at_static_path(client: TestClient) -> None:
    """Custom app.css layered on top of Pico for viewer-specific bits."""
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_home_page_links_pico_and_app_css(client: TestClient) -> None:
    """base.html <head> must reference both stylesheets. This pins the
    integration so a template rewrite that drops the link tag fails
    loudly rather than silently returning to unstyled HTML.
    """
    from bs4 import BeautifulSoup

    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("link", rel="stylesheet")
    hrefs = {link.get("href") for link in links}
    assert "/static/pico.classless.min.css" in hrefs
    assert "/static/app.css" in hrefs


def test_home_page_declares_color_scheme_meta(client: TestClient) -> None:
    """Pico's dark mode uses ``prefers-color-scheme``. The meta tag
    ``<meta name="color-scheme" content="light dark">`` unlocks form
    control theming (design §9 dark-mode note). Without it, form
    controls stay light-themed on dark OS.
    """
    from bs4 import BeautifulSoup

    resp = client.get("/")
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "color-scheme"})
    assert meta is not None
    assert "light" in meta.get("content", "")
    assert "dark" in meta.get("content", "")
