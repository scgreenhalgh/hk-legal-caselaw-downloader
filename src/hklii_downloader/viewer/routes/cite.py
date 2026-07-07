"""GET /cite/{neutral} — neutral-citation resolver (Phase 4 route 26).

Users clicking a linkified ``<a class="hklii-cite">`` in a rendered body
land here. Three outcomes:

  * **Parse ok, case in cases** — 302 to ``/case/{court}/{year}/{number}``,
    the standard segmented URL. Language selection stays with
    :mod:`case_detail`; ``/cite`` never bakes a language into its
    redirect target.
  * **Parse ok, case not in cases** — 200 renders
    ``cite_unresolved.html``. Design §5 line 132: NEVER a silent 302
    to homepage (L5 ambiguous state — the reader must be told the
    citation didn't resolve). The unresolved page carries an
    ``/search?q=<neutral>`` escape hatch so the user is never dead-
    ended (L1).
  * **Parse returns None** — 404. Distinguishes 'not a citation'
    (probably a broken linkifier upstream) from 'no matching case'
    (a legitimate corpus gap). Collapsing these two states would
    hide meaningful signal.

The path parameter uses FastAPI's ``:path`` converter — tolerant of
whatever the browser lands with (percent-encoded brackets, mixed case).
:func:`parse_neutral_citation` already accepts case-insensitive court
slugs and normalises to the lowercase canonical form.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hklii_downloader.viewer.body_render.cite import parse_neutral_citation
from hklii_downloader.viewer.db import open_readonly


router = APIRouter()


@router.get("/cite/{neutral:path}", response_model=None)
def cite_resolver(
    request: Request, neutral: str
) -> RedirectResponse | HTMLResponse:
    parsed = parse_neutral_citation(neutral)
    if parsed is None:
        raise HTTPException(
            status_code=404,
            detail=f"Not a recognised neutral citation: {neutral!r}",
        )
    court, year, number = parsed

    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        row = conn.execute(
            "SELECT 1 FROM cases WHERE court = ? AND year = ? AND number = ?",
            (court, year, number),
        ).fetchone()
    finally:
        conn.close()

    if row is not None:
        return RedirectResponse(
            f"/case/{court}/{year}/{number}",
            status_code=302,
        )

    templates = request.app.state.templates
    search_href = f"/search?q={quote(neutral, safe='')}"
    return templates.TemplateResponse(
        request,
        "cite_unresolved.html",
        {
            "neutral": neutral,
            "parsed_court": court,
            "parsed_year": year,
            "parsed_number": number,
            "search_href": search_href,
        },
    )
