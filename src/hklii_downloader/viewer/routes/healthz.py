"""GET /healthz — DB open + schema-version check (Phase 4 route 10).

Machine-readable liveness probe. Consumers are supervisor processes
(uptime-kuma, launchd's KeepAlive, systemd's ExecStartPre), not
browsers — so the response is ``application/json``.

Broad exception handling here is intentional (L1: normally verboten,
but the whole point of ``/healthz`` is to *expose* failure states to
the outside). Every unhealthy branch names WHICH probe failed so an
operator can act.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hklii_downloader.viewer.db import open_readonly


_REQUIRED_VIEWER_TABLES: tuple[str, ...] = (
    "fts_cases", "case_bodies", "fts_body",
)


def _probe_checkpoint(path: str | Path) -> str:
    """Open checkpoint.db read-only and probe the ``cases`` table.

    Returns ``"ok"`` on success, or a short human-readable reason on
    failure that names the failing action (open vs query).
    """
    try:
        conn = open_readonly(path)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        return f"open failed: {exc.__class__.__name__}: {exc}"
    try:
        try:
            conn.execute("SELECT 1 FROM cases LIMIT 1").fetchone()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"query failed: {exc.__class__.__name__}: {exc}"
    finally:
        conn.close()


def _probe_viewer(path: str | Path) -> str:
    """Open viewer.db read-only and confirm required tables exist.

    We check schema presence rather than running FTS5 queries — the
    FTS5 virtual table is expensive to instantiate, and the cheaper
    ``sqlite_master`` check is sufficient for a liveness probe.
    """
    try:
        conn = open_readonly(path)
    except Exception as exc:  # noqa: BLE001
        return f"open failed: {exc.__class__.__name__}: {exc}"
    try:
        for name in _REQUIRED_VIEWER_TABLES:
            try:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name = ?",
                    (name,),
                ).fetchone()
            except Exception as exc:  # noqa: BLE001
                return f"query failed: {exc.__class__.__name__}: {exc}"
            if row is None:
                return f"missing table: {name}"
        return "ok"
    finally:
        conn.close()


router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    checkpoint_status = _probe_checkpoint(request.app.state.checkpoint_db)
    viewer_status = _probe_viewer(request.app.state.viewer_db)
    all_ok = checkpoint_status == "ok" and viewer_status == "ok"
    body = {
        "status": "ok" if all_ok else "error",
        "checks": {
            "checkpoint_db": checkpoint_status,
            "viewer_db": viewer_status,
        },
    }
    return JSONResponse(body, status_code=200 if all_ok else 503)
