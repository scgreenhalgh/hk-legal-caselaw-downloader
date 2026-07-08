"""GET /freshness — dashboard of the ``db_freshness`` ledger.

Server-side render of the same table ``hklii check-freshness --report``
prints as markdown, but wire-free — every visit reads the last-probed
state from ``checkpoint.db``. Mismatched cells are visually emphasised
so an operator scanning the page spots drift at a glance.

Data sources
------------
* ``checkpoint.db → db_freshness`` for per-bucket wire + local counts +
  timestamps.
* :func:`hklii_downloader.discovery.load_default_matrix` for the
  authoritative slug × lang matrix (order of rows, presence of SC
  column per slug).
* :data:`hklii_downloader.freshness.DB_DISPLAY_NAMES` for the English
  + Traditional Chinese label pair.

Refresh cadence
---------------
Never triggers a wire probe itself — the data is only as fresh as the
last ``hklii check-freshness`` invocation. The page footer surfaces
that timestamp so a reader knows how stale the numbers are.

See ``docs/freshness-sanity-check.md`` for interpretation and
operating instructions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from hklii_downloader.discovery import load_default_matrix
from hklii_downloader.freshness import DB_DISPLAY_NAMES
from hklii_downloader.viewer.db import open_readonly


_LANGS = ("en", "tc", "sc")
_MATRIX_BUCKETS = ("cases", "legis", "other")
HKT = timezone(timedelta(hours=8))


def _fetch_freshness_rows(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str], dict]:
    """Return ``{(kind, scope, lang): row_dict}`` for every db_freshness
    row present.

    Returns an empty dict when the table doesn't exist (checkpoint DB
    predates the D2 migration) so the page still renders — every cell
    shows ``—`` for freshness-unaware corpora.
    """
    try:
        cur = conn.execute(
            "SELECT kind, scope, lang, live_count, local_count, "
            "       live_updated_at, live_probed_at, "
            "       last_scrape_completed_at, probe_error "
            "FROM db_freshness"
        )
    except sqlite3.OperationalError:
        return {}
    out: dict[tuple[str, str, str], dict] = {}
    for row in cur.fetchall():
        (kind, scope, lang, live, local, live_upd,
         probed_at, scrape_at, probe_err) = row
        out[(kind, scope, lang)] = {
            "live": live, "local": local,
            "live_updated_at": live_upd,
            "live_probed_at": probed_at,
            "last_scrape_completed_at": scrape_at,
            "probe_error": probe_err,
        }
    return out


def _kinds_for_lookup(bucket: str, slug: str) -> tuple[str, ...]:
    """Return the ordered list of ``kind`` values to probe for a given
    ``(bucket, slug)`` pair.

    The freshness ledger stores UKPC under ``kind='cases'`` (its rows
    live in the cases table) even though it comes from the /databases
    ``cases`` bucket. Every "other"-bucket slug that's mapped in
    dispatch_url is tracked under ``kind='hopt'``. Trying all three
    kinds in this order keeps the lookup zero-cost when the row is
    present and sensibly falls back when not.
    """
    return ("cases", "legis", "hopt")


def _build_cell(rows: dict, bucket: str, slug: str, lang: str) -> dict:
    """Assemble one cell dict for the template.

    Cell shape:
      * ``local``, ``live`` — either int or ``None`` (renders as ``—``)
      * ``delta`` — signed ``int`` when both counts known and differ,
        else ``None``
      * ``updated`` — HKLII's returned timestamp string or ``None``
      * ``is_mismatch`` — True iff both counts are populated and unequal
    """
    for kind in _kinds_for_lookup(bucket, slug):
        rec = rows.get((kind, slug, lang))
        if rec is not None:
            live = rec["live"]
            local = rec["local"]
            delta = (
                (live - local)
                if isinstance(live, int) and isinstance(local, int)
                else None
            )
            is_mismatch = (
                delta is not None and delta != 0
                or rec.get("probe_error") is not None
            )
            return {
                "local": local,
                "live": live,
                "delta": delta,
                "updated": rec.get("live_updated_at"),
                "is_mismatch": is_mismatch,
                "probe_error": rec.get("probe_error"),
                "available": True,
            }
    return {
        "local": None, "live": None, "delta": None, "updated": None,
        "is_mismatch": False, "probe_error": None, "available": False,
    }


def _build_row(
    rows: dict, bucket: str, slug: str, langs: Iterable[str],
) -> dict:
    en_name, zh_name = DB_DISPLAY_NAMES.get(slug, (slug, ""))
    langs_available = set(langs)
    cells = []
    for lang in _LANGS:
        if lang not in langs_available:
            # Slug doesn't advertise this lang on /databases — em-dash
            # the cell so the column stays aligned across trilingual
            # neighbours without falsely suggesting a probe.
            cells.append({
                "lang": lang, "local": None, "live": None,
                "delta": None, "updated": None,
                "is_mismatch": False, "probe_error": None,
                "available": False, "not_applicable": True,
            })
            continue
        cell = _build_cell(rows, bucket, slug, lang)
        cell["lang"] = lang
        cell["not_applicable"] = False
        cells.append(cell)
    return {
        "slug": slug,
        "en_name": en_name,
        "zh_name": zh_name,
        "cells": cells,
    }


def _latest_probed_at(rows: dict) -> str | None:
    """Return the newest ``live_probed_at`` across all rows, formatted
    as an ISO string in HKT. Used for the "last probed" footer."""
    tss = [
        r["live_probed_at"] for r in rows.values()
        if r.get("live_probed_at") is not None
    ]
    if not tss:
        return None
    ts = max(tss)
    return datetime.fromtimestamp(ts, HKT).isoformat(timespec="seconds")


router = APIRouter()


@router.get("/freshness", response_class=HTMLResponse)
def freshness(request: Request) -> HTMLResponse:
    conn = open_readonly(request.app.state.checkpoint_db)
    try:
        rows = _fetch_freshness_rows(conn)
    finally:
        conn.close()

    matrix = load_default_matrix()
    sections: list[dict] = []
    for bucket_name in _MATRIX_BUCKETS:
        bucket = getattr(matrix, bucket_name, {}) or {}
        rendered = [
            _build_row(rows, bucket_name, slug, matrix_langs)
            for slug, matrix_langs in bucket.items()
        ]
        sections.append({"name": bucket_name, "rows": rendered})

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "freshness.html",
        {
            "sections": sections,
            "langs": _LANGS,
            "last_probed_at": _latest_probed_at(rows),
            "row_count": len(rows),
        },
    )
