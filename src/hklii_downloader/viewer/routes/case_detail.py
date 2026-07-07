"""GET /case/{slug}/{year}/{number} — case detail page.

Layout (design §5 body / §7 citation panels):
  * Metadata header: neutral, title, court, year, date
  * Sanitized body wrapped in ``<article lang="…">`` per §9
  * Three HTMX-lazy citation tabs (routes 5/6/7). Panels stay empty
    on first paint — the tab clicks fetch and swap into their panels.

404 shape:
  * Unknown court slug → 404
  * (court, year, number) tuple not in ``cases`` → 404
  * Case row exists but body missing on disk → 200 with empty article
    shell (L5: the case IS in the corpus, we just don't have its body
    yet — orphan / mid-scrape / pending). Body render dispatcher's own
    contract already returns an empty ``<article>`` in that case.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.body_render.render import (
    render_case_body,
    select_body_source,
)
from hklii_downloader.viewer.courts import CANONICAL_COURTS
from hklii_downloader.viewer.db import open_readonly


def _fetch_case_row(
    conn: sqlite3.Connection, court: str, year: int, number: int
) -> dict | None:
    cur = conn.execute(
        "SELECT court, year, number, neutral, title, date, status, "
        "formats, lang, html_generated_from "
        "FROM cases "
        "WHERE court = ? AND year = ? AND number = ?",
        (court, year, number),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


router = APIRouter()


@router.get("/case/{slug}/{year}/{number}", response_class=HTMLResponse)
def case_detail(
    request: Request,
    slug: str,
    year: int,
    number: int,
    lang: str = "en",
) -> HTMLResponse:
    if slug not in CANONICAL_COURTS:
        raise HTTPException(status_code=404, detail="Unknown court")

    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        case_row = _fetch_case_row(conn, slug, year, number)
    finally:
        conn.close()

    if case_row is None:
        raise HTTPException(status_code=404, detail="Case not found")

    output_root = request.app.state.output_root
    source = select_body_source(case_row, output_root, lang)
    body_html = render_case_body(source, case_row, output_root)

    case_key = f"{case_row['court']}/{case_row['year']}/{case_row['number']}"
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "case_detail.html",
        {
            "case": case_row,
            "case_key": case_key,
            "body_html": body_html,
        },
    )
