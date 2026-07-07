"""GET /court/{slug} — court landing (year buckets + top hub cases).

Design §6: year buckets + top hub cases. §7: hub cases sourced from
``viewer.db.viewer_hub_cache`` via :func:`viewer.graph.hub_cases`; when
the table is missing (setup skipped), the route still renders — year
buckets don't depend on the cache — with an L1 banner in place of the
hub panel.

Unknown court → 404 (design §6). The canonical slug list is
:data:`viewer.courts.CANONICAL_COURTS`.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import ViewerCacheMissing, hub_cases


# Hub panel bounds — kept modest for the landing page. `/authorities`
# (a later route) is the deep hub index; this is a teaser.
_HUB_LIMIT = 20
_HUB_MIN_INBOUND = 5


def _fetch_year_buckets(
    conn: sqlite3.Connection, court: str
) -> list[dict]:
    """Return ``[{year, count}]`` for ``court``, ordered year DESC."""
    cur = conn.execute(
        "SELECT year, COUNT(*) FROM cases "
        "WHERE court = ? GROUP BY year ORDER BY year DESC",
        (court,),
    )
    return [{"year": row[0], "count": row[1]} for row in cur.fetchall()]


router = APIRouter()


@router.get("/court/{slug}", response_class=HTMLResponse)
def court_landing(request: Request, slug: str) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")

    cp_conn = open_readonly(request.app.state.checkpoint_db)
    try:
        year_buckets = _fetch_year_buckets(cp_conn, slug)
    finally:
        cp_conn.close()

    hub_result: list[dict] = []
    hub_cache_missing = False
    vw_conn = open_readonly(request.app.state.viewer_db)
    try:
        try:
            hub_result = hub_cases(
                vw_conn,
                court=slug,
                min_inbound=_HUB_MIN_INBOUND,
                limit=_HUB_LIMIT,
            )
        except ViewerCacheMissing:
            hub_cache_missing = True
    finally:
        vw_conn.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "court.html",
        {
            "court_slug": slug,
            "year_buckets": year_buckets,
            "hub_cases": hub_result,
            "hub_cache_missing": hub_cache_missing,
        },
    )
