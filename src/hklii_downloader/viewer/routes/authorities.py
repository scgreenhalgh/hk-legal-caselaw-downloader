"""GET /authorities — hub-case leaderboard index (design §7, route 31).

Standalone deep hub index. Distinct from the case-scoped
``/case/{c}/{y}/{n}/authorities`` HTMX partial (owned by
:mod:`viewer.routes.citations`):

  * ``/authorities``                     — INBOUND leaderboard, whole
                                           corpus, sourced from
                                           ``viewer_hub_cache``
  * ``/case/…/authorities``              — OUTBOUND per-case panel,
                                           sourced from ``citations``

Design §7 constraints (task-authoritative):
  * Single-select court facet via ``?court=<slug>``
  * Order = ``inbound_count DESC, case_key ASC`` (delegated to
    :func:`viewer.graph.hub_cases`)
  * Missing ``viewer_hub_cache`` table → banner:
    "Hub cache not yet computed. Run ``hklii viewer index`` first."
    (rendered by the template — L1 signal that the indexer never ran,
    distinct from L5 populated-but-no-rows)
  * NO sortable header, NO pagination in v1
  * Row = curial Roman rank + case_key + court name + inbound count
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import ViewerCacheMissing, hub_cases


# Leaderboard is the deep index — include every cached hub with at
# least one inbound edge (i.e. min_inbound=1, not the graph default
# of 5). ``/court/{slug}`` uses the higher default because that page
# is a teaser; here we want the full ranking.
_MIN_INBOUND = 1

# Scan-friendly page cap. Design §7 explicitly declines pagination in
# v1, so this is the hard upper bound on rendered rows.
_LIMIT = 100


router = APIRouter()


@router.get("/authorities", response_class=HTMLResponse)
def authorities_index(
    request: Request,
    court: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the hub-case leaderboard, optionally narrowed to one court.

    Unknown court → 404 (mirrors the ``/court/{slug}`` guard). Missing
    hub-cache table → 200 + banner (setup signal). Empty result set on
    a present table → 200 + distinct empty marker.
    """
    if court is not None and court not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")

    hub_rows: list[dict] = []
    hub_cache_missing = False
    vw_conn = open_readonly(request.app.state.viewer_db)
    try:
        try:
            hub_rows = hub_cases(
                vw_conn,
                court=court,
                min_inbound=_MIN_INBOUND,
                limit=_LIMIT,
            )
        except ViewerCacheMissing:
            hub_cache_missing = True
    finally:
        vw_conn.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "authorities.html",
        {
            "hub_rows": hub_rows,
            "hub_cache_missing": hub_cache_missing,
            "selected_court": court,
        },
    )
