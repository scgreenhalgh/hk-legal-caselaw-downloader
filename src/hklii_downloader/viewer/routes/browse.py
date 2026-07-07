"""GET /browse — corpus-wide list with mandatory court prefilter (route 30).

Design §6:
  * ``/browse?court=<slug>[&year=YYYY][&page=N][&sort=<mode>]``
  * UI *requires* at least one court prefilter. A bare ``GET /browse``
    would ask the caller to render 162k rows unfiltered — instead we
    reject with 400 and a rendered "pick a court" landing so the user
    sees a hint, not a raw error.
  * Court facet is single-select (design §7 verdict-YAGNI: multi-select
    removed).
  * Sort modes: ``date_desc`` (default) | ``date_asc`` | ``neutral_asc``.
    An unknown value falls back to the default (L1 silent-skip: we do
    NOT honour a garbage sort key).
  * Pagination: 50 rows/page.
  * Row shape mirrors the year page (neutral, parties, date).

The fetch pattern is a deliberate mirror of ``year.py``:
  * Per-request read-only ``open_readonly(checkpoint_db)`` connection.
  * ``COUNT(*)`` + ``LIMIT/OFFSET`` from ``cases``.
  * Court validated against :data:`CANONICAL_COURTS`.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly


_PAGE_SIZE = 50

#: Allowed ``?sort=`` values. Kept as a tuple (not a set) so error
#: messages and the "pick a sort" affordance render in a stable order.
_SORT_MODES: tuple[str, ...] = ("date_desc", "date_asc", "neutral_asc")

#: SQL ``ORDER BY`` fragments keyed by sort mode. Static strings only —
#: the sort value never flows into the SQL text via interpolation of
#: user input, only via a whitelist lookup. Every clause pairs the
#: primary key with ``number`` as a stable tiebreak so identical
#: dates / neutrals don't shuffle between requests.
_SORT_SQL: dict[str, str] = {
    "date_desc":   "ORDER BY date DESC, number DESC",
    "date_asc":    "ORDER BY date ASC, number ASC",
    "neutral_asc": "ORDER BY neutral ASC, number ASC",
}


def _resolve_sort(sort: str | None) -> str:
    """Return a whitelisted sort mode; unknown values fall back to default.

    L1 silent-skip lens: an unknown ``?sort=`` MUST NOT drift into
    "whatever SQLite decides" ordering. Fall-back is the documented
    default (``date_desc``), matching the year page's implicit contract.
    """
    if sort is None or sort not in _SORT_MODES:
        return "date_desc"
    return sort


def _count(
    conn: sqlite3.Connection, court: str, year: int | None
) -> int:
    """Row count for ``court`` (and optional ``year``)."""
    if year is None:
        cur = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE court = ?",
            (court,),
        )
    else:
        cur = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE court = ? AND year = ?",
            (court, year),
        )
    return cur.fetchone()[0]


def _fetch_page(
    conn: sqlite3.Connection,
    court: str,
    year: int | None,
    order_by: str,
    limit: int,
    offset: int,
) -> list[dict]:
    """One page of ``(court, year?)`` rows with the given ``ORDER BY``.

    ``order_by`` is a whitelisted static string from :data:`_SORT_SQL`
    (see :func:`_resolve_sort`); the (court, year, limit, offset) values
    are parameterised — so the SQL is a fixed shape per sort mode with
    no injection surface.
    """
    if year is None:
        cur = conn.execute(
            "SELECT court, year, number, neutral, title, date "
            f"FROM cases WHERE court = ? {order_by} LIMIT ? OFFSET ?",
            (court, limit, offset),
        )
    else:
        cur = conn.execute(
            "SELECT court, year, number, neutral, title, date "
            "FROM cases WHERE court = ? AND year = ? "
            f"{order_by} LIMIT ? OFFSET ?",
            (court, year, limit, offset),
        )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


router = APIRouter()


@router.get("/browse", response_class=HTMLResponse)
def browse_page(
    request: Request,
    court: str | None = None,
    year: int | None = None,
    page: int = 1,
    sort: str | None = None,
) -> HTMLResponse:
    templates = request.app.state.templates

    # Bare /browse — no court facet. Render a "pick a court" landing
    # with status 400. Design §6 forbids an unfiltered corpus render.
    if court is None:
        return templates.TemplateResponse(
            request,
            "browse.html",
            {
                "court_slug": None,
                "year": None,
                "cases": [],
                "page": 1,
                "total_pages": 0,
                "total": 0,
                "sort": "date_desc",
                "sort_modes": _SORT_MODES,
                "courts": CANONICAL_COURTS,
                "missing_court": True,
            },
            status_code=400,
        )

    if court not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")
    if page < 1:
        raise HTTPException(status_code=404, detail="Invalid page")

    resolved_sort = _resolve_sort(sort)
    order_by = _SORT_SQL[resolved_sort]
    offset = (page - 1) * _PAGE_SIZE

    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        total = _count(conn, court, year)
        cases = _fetch_page(conn, court, year, order_by, _PAGE_SIZE, offset)
    finally:
        conn.close()

    total_pages = max(1, -(-total // _PAGE_SIZE))  # ceil-div

    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "court_slug": court,
            "year": year,
            "cases": cases,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "sort": resolved_sort,
            "sort_modes": _SORT_MODES,
            "courts": CANONICAL_COURTS,
            "missing_court": False,
        },
    )
