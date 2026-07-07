"""FastAPI app factory for the viewer.

The factory (pass DB paths at boot) keeps the app instance test-friendly:
a TestClient constructs a fresh app per fixture instead of relying on
process-level state. Each route module exposes an ``APIRouter`` that
:func:`create_app` mounts.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hklii_downloader.viewer.routes.case_detail import router as case_detail_router
from hklii_downloader.viewer.routes.citations import router as citations_router
from hklii_downloader.viewer.routes.court import router as court_router
from hklii_downloader.viewer.routes.healthz import router as healthz_router
from hklii_downloader.viewer.routes.home import router as home_router
from hklii_downloader.viewer.routes.search import router as search_router
from hklii_downloader.viewer.routes.year import router as year_router


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


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
    """
    app = FastAPI()
    app.state.checkpoint_db = checkpoint_db
    app.state.viewer_db = viewer_db
    app.state.output_root = output_root
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(home_router)
    app.include_router(court_router)
    app.include_router(year_router)
    app.include_router(case_detail_router)
    app.include_router(citations_router)
    app.include_router(search_router)
    app.include_router(healthz_router)
    return app
