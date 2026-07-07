"""GET / — home: recent judgments + court tiles.

Data sources:
  * Court tile counts: ``SELECT court, COUNT(*) FROM cases GROUP BY court``
    over ``checkpoint.db``. Every canonical court surfaces (0 when empty)
    so a fresh corpus with no rows in a court still shows the court —
    L5 signal that the court exists and is not simply unknown.
  * Recent-cases list: latest ``_RECENT_LIMIT`` by ``date DESC`` with
    stable ``court ASC, number DESC`` tiebreak — no two identical-date
    same-court rows shuffle between renders.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly


_RECENT_LIMIT = 10


def _fetch_court_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {court_slug: row_count} from ``cases``. Missing courts absent."""
    cur = conn.execute("SELECT court, COUNT(*) FROM cases GROUP BY court")
    return {row[0]: row[1] for row in cur.fetchall()}


def _fetch_recent_cases(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Latest ``limit`` cases by ``date DESC``, deterministic tiebreak."""
    cur = conn.execute(
        "SELECT court, year, number, neutral, title, date "
        "FROM cases "
        "ORDER BY date DESC, court ASC, number DESC "
        "LIMIT ?",
        (limit,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        counts = _fetch_court_counts(conn)
        recent = _fetch_recent_cases(conn, _RECENT_LIMIT)
    finally:
        conn.close()

    court_tiles = [
        {"slug": slug, "count": counts.get(slug, 0)}
        for slug in CANONICAL_COURTS
    ]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {"court_tiles": court_tiles, "recent_cases": recent},
    )
