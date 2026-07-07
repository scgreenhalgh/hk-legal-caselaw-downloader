"""HTMX citation-panel partials.

Three routes live here — cited-by, authorities-cited, parallel — all
returning HTML fragments meant for ``hx-swap="innerHTML"`` into the
matching ``#panel-*`` container on the case-detail page.

Routes 6 and 7 are wired alongside route 5 in a single module because
they share the same page-size constant, the same 404 shape, and the
same connection-open pattern; keeping them together lets a reviewer
verify all three follow the design's ordering / court-filter /
pagination contract from one place.

Design §7 constraints:
  * cited-by ranking: court_rank ASC, first_seen DESC
  * Single-select court facet ``?court=<slug>``
  * 50/page with 'Load next 50' button and ``hx-swap="beforeend"``
  * parallel_cites is small (11k total) — no pagination, sort ASC
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import (
    authorities_cited,
    cited_by,
    parallel_cites,
)


_PAGE_SIZE = 50


router = APIRouter()


@router.get(
    "/case/{slug}/{year}/{number}/cited-by",
    response_class=HTMLResponse,
)
def cited_by_partial(
    request: Request,
    slug: str,
    year: int,
    number: int,
    court: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")
    if page < 1:
        raise HTTPException(status_code=404, detail="Invalid page")

    case_key = f"{slug}/{year}/{number}"
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        rows = cited_by(
            conn,
            case_key,
            court_filter=court,
            page=page,
            per_page=_PAGE_SIZE,
        )
    finally:
        conn.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/cited_by.html",
        {
            "case_key": case_key,
            "rows": rows,
            "page": page,
            "court_filter": court,
            "page_size": _PAGE_SIZE,
        },
    )


@router.get(
    "/case/{slug}/{year}/{number}/authorities",
    response_class=HTMLResponse,
)
def authorities_partial(
    request: Request,
    slug: str,
    year: int,
    number: int,
    page: int = 1,
) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")
    if page < 1:
        raise HTTPException(status_code=404, detail="Invalid page")

    case_key = f"{slug}/{year}/{number}"
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        rows = authorities_cited(
            conn, case_key, page=page, per_page=_PAGE_SIZE
        )
    finally:
        conn.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/authorities.html",
        {
            "case_key": case_key,
            "rows": rows,
            "page": page,
            "page_size": _PAGE_SIZE,
        },
    )


@router.get(
    "/case/{slug}/{year}/{number}/parallel",
    response_class=HTMLResponse,
)
def parallel_partial(
    request: Request,
    slug: str,
    year: int,
    number: int,
) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")

    case_key = f"{slug}/{year}/{number}"
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        cites = parallel_cites(conn, case_key)
    finally:
        conn.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/parallel.html",
        {"case_key": case_key, "cites": cites},
    )
