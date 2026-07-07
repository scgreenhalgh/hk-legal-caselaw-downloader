"""Tests for GET /healthz — DB open + schema-version check (Phase 4 route 10).

Returns JSON, not HTML. The intended consumer is a shell script or
supervisor process (systemd/launchd/uptime probe) — machine-readable
status wins over a pretty page.

Semantics:
  * 200 + {"status": "ok"} when both DBs open and required tables
    exist
  * 503 + {"status": "error", "checks": {...}} otherwise, with the
    per-check reasons so an operator can see WHICH side failed
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases


def _make_healthy_app(tmp_path: Path) -> TestClient:
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


def _make_broken_viewer_app(tmp_path: Path) -> TestClient:
    """Healthy checkpoint.db, viewer.db exists but has NO viewer schema."""
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [("hkcfa", 2020, 1, "[2020] HKCFA 1", "P v Q",
          "2020-05-05", "downloaded")],
    )
    # Touch viewer.db but do NOT run create_schema.
    conn = sqlite3.connect(str(viewer))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def _make_missing_checkpoint_app(tmp_path: Path) -> TestClient:
    """Viewer.db healthy but checkpoint.db is not on disk."""
    checkpoint = tmp_path / "does_not_exist.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_healthz_ok_when_both_dbs_healthy(tmp_path: Path) -> None:
    client = _make_healthy_app(tmp_path)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["checkpoint_db"] == "ok"
    assert body["checks"]["viewer_db"] == "ok"


def test_healthz_returns_json(tmp_path: Path) -> None:
    """Machine-readable. HTML would be a wrong-tool answer to 'is this
    process healthy' probes.
    """
    client = _make_healthy_app(tmp_path)
    resp = client.get("/healthz")
    assert resp.headers["content-type"].startswith("application/json")


def test_healthz_503_when_viewer_schema_missing(tmp_path: Path) -> None:
    """viewer.db has no fts_body — health probe must FAIL loudly (L1
    'setup not done' vs L5 'empty index' signal).
    """
    client = _make_broken_viewer_app(tmp_path)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["checks"]["checkpoint_db"] == "ok"
    assert body["checks"]["viewer_db"] != "ok"


def test_healthz_503_when_checkpoint_missing(tmp_path: Path) -> None:
    """checkpoint.db not on disk — probe fails."""
    client = _make_missing_checkpoint_app(tmp_path)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["checks"]["checkpoint_db"] != "ok"
