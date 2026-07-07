"""GET /search — FTS form + BM25 results (Phase 4 route 8).

Full page rendering both the search form and (when ``?q=…``) the results
list. Route 9 (/search/results) will serve the HTMX partial for
pagination without a full reload.

FTS5 shape:
  * Trigram tokenizer (design §4). Queries <3 chars naturally yield
    zero rows.
  * ``bm25(fts_body)`` ranks — smaller score = more relevant.
  * ``snippet(fts_body, col=1, start='<mark>', end='</mark>', ellip='…',
    tokens=32)`` wraps matched tokens for the CSS contract in design §9.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.viewer.db import open_readonly


_PAGE_SIZE = 50

#: CSS contract (design §9): the sanitizer-safe snippet markers.
FTS_HIGHLIGHT_START: str = "<mark>"
FTS_HIGHLIGHT_END: str = "</mark>"


def _escape_fts_query(q: str) -> str:
    """Wrap the raw user query as an FTS5 quoted phrase.

    Doubling ``"`` inside the value is the FTS5 escape for a literal
    double-quote inside a phrase. Wrapping the whole value in quotes
    means FTS5 treats it as a phrase literal — none of the syntax
    operators (``AND``, ``OR``, ``NEAR``, ``*`` prefix, ``-``
    exclusion) are active. This is the smallest safe surface for
    Phase 4; a query-parser that surfaces operators can land later.
    """
    return '"' + q.replace('"', '""') + '"'


def _search_bm25(
    vw_conn: sqlite3.Connection,
    raw_query: str,
    page: int,
    per_page: int,
) -> tuple[list[dict], int]:
    """Return ``(rows, total_count)`` for a BM25-ranked query."""
    escaped = _escape_fts_query(raw_query)
    offset = (page - 1) * per_page

    total = vw_conn.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        (escaped,),
    ).fetchone()[0]

    cur = vw_conn.execute(
        f"""
        SELECT fc.case_key, fc.court, fc.year, fc.number,
               fc.neutral, fc.title, fc.date, fc.lang,
               snippet(fts_body, 1,
                       '{FTS_HIGHLIGHT_START}',
                       '{FTS_HIGHLIGHT_END}',
                       '…', 32) AS snippet,
               bm25(fts_body) AS score
        FROM fts_body
        JOIN case_bodies cb ON cb.id = fts_body.rowid
        JOIN fts_cases fc ON fc.case_key = cb.case_key AND fc.lang = cb.lang
        WHERE fts_body MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?
        """,
        (escaped, per_page, offset),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()], total


router = APIRouter()


def _run_search(request: Request, q: str | None, page: int) -> tuple[list[dict], int]:
    """Query the FTS5 index if ``q`` is non-empty, else return empty results."""
    if not q:
        return [], 0
    conn = open_readonly(request.app.state.viewer_db)
    try:
        return _search_bm25(conn, q, page, _PAGE_SIZE)
    finally:
        conn.close()


@router.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    rows, total = _run_search(request, q, page)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": q or "",
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": _PAGE_SIZE,
        },
    )


@router.get("/search/results", response_class=HTMLResponse)
def search_results_partial(
    request: Request,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    """HTMX fragment version of the search results.

    Rendered by ``partials/search_results.html`` — the same file
    /search embeds via ``{% include %}``. Both routes therefore
    render identical result markup and can't drift.
    """
    rows, total = _run_search(request, q, page)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {
            "query": q or "",
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": _PAGE_SIZE,
        },
    )
