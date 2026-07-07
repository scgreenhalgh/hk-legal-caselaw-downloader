"""FastAPI app factory for the viewer — stub.

Routes land under ``viewer/routes/`` (Phase 4). The factory pattern
(pass DB paths at boot) keeps the app instance test-friendly: a
TestClient constructs a fresh app per fixture instead of relying on
process-level state.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def create_app(
    *,
    checkpoint_db: Path,
    viewer_db: Path,
    output_root: Path,
) -> FastAPI:
    """Build a FastAPI instance wired to the given corpus + derivative DBs.

    ``checkpoint_db`` is read-only over the downloader's source of truth.
    ``viewer_db`` holds the viewer-owned FTS index + hub cache.
    ``output_root`` is the on-disk corpus root for body-render + appeal_chain.

    Routes are added in subsequent Phase 4 commits; this stub returns an
    empty app so that test collection can proceed and route tests fail at
    assertions (per CLAUDE.md's 'failing test means an assertion executed').
    """
    app = FastAPI()
    app.state.checkpoint_db = checkpoint_db
    app.state.viewer_db = viewer_db
    app.state.output_root = output_root
    return app
