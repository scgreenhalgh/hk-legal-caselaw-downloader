"""GET /court/{slug}/{year} — paginated case listing.

Design §6:
  * 5-column table (neutral, parties, date, formats, inbound_count).
    Phase 4 lands columns 1-3; formats/inbound decorators land later
    when app.state.format_flags + hub_counts are wired (design §6
    'no viewer_meta.db').
  * Sort: date_desc default (Phase 4 pins default only; ``?sort=…``
    branches land alongside browse in a later phase).
  * Pagination: ``?page=N``, size fixed at 50.
  * Empty year on a known court renders '0 cases …' — NOT 404.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly


_PAGE_SIZE = 50


def _count_cases(conn: sqlite3.Connection, court: str, year: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM cases WHERE court = ? AND year = ?",
        (court, year),
    )
    return cur.fetchone()[0]


def _fetch_page(
    conn: sqlite3.Connection,
    court: str,
    year: int,
    limit: int,
    offset: int,
) -> list[dict]:
    """Latest page of cases for (court, year), ordered date DESC with a
    stable ``number DESC`` tiebreak so identical-date rows don't shuffle.
    """
    cur = conn.execute(
        "SELECT court, year, number, neutral, title, date "
        "FROM cases WHERE court = ? AND year = ? "
        "ORDER BY date DESC, number DESC "
        "LIMIT ? OFFSET ?",
        (court, year, limit, offset),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


router = APIRouter()


@router.get("/court/{slug}/{year}", response_class=HTMLResponse)
def year_page(
    request: Request,
    slug: str,
    year: int,
    page: int = 1,
) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")
    if page < 1:
        raise HTTPException(status_code=404, detail="Invalid page")

    offset = (page - 1) * _PAGE_SIZE
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        total = _count_cases(conn, slug, year)
        cases = _fetch_page(conn, slug, year, _PAGE_SIZE, offset)
    finally:
        conn.close()

    total_pages = max(1, -(-total // _PAGE_SIZE))  # ceil-div

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "year.html",
        {
            "court_slug": slug,
            "year": year,
            "cases": cases,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )
