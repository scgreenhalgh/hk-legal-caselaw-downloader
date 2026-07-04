"""One-shot health snapshot for a running (or finished) bulk scrape.

`hklii monitor -o <dir>` is a pure, read-only reader over the three
artifacts a scrape leaves behind — `.checkpoint.db`, `events.jsonl`, and
`scrape.log`. It prints a compact severity-coded summary and exits
0 (healthy) / 1 (warn) / 2 (critical) so a cron job or `/loop` wrapper can
escalate during the 15-20h production run. It never writes to any of the
artifacts it reads.

This module is the skeleton; behaviour is filled in test-first across the
`hklii monitor` commit pairs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CHECKPOINT_FILENAME = ".checkpoint.db"
_EVENTS_FILENAME = "events.jsonl"
_LOG_FILENAME = "scrape.log"


class MonitorRunner:
    def __init__(
        self,
        output_dir: Path | str,
        *,
        window_min: int = 30,
        workers: int = 20,
        now: datetime | None = None,
    ):
        self._output_dir = Path(output_dir)
        self._db_path = self._output_dir / _CHECKPOINT_FILENAME
        self._events_path = self._output_dir / _EVENTS_FILENAME
        self._log_path = self._output_dir / _LOG_FILENAME
        self._window_min = window_min
        self._workers = workers
        self._now = now or datetime.now(timezone.utc)

    def run(self) -> dict[str, Any]:
        """Read all artifacts and assemble the severity-coded summary."""
        checkpoint = self._read_checkpoint()
        events = self._read_events()
        log = self._read_log()
        return {
            "severity": "HEALTHY",
            "banner": "",
            "runtime_hours": checkpoint["runtime_hours"] if checkpoint else None,
            "checkpoint": checkpoint,
            "events": events,
            "log": log,
            "alerts": [],
        }

    # ------------------------------------------------------------- checkpoint

    def _read_checkpoint(self) -> dict[str, Any] | None:
        """Read status counts + top failed-error prefixes from a read-only
        connection to `.checkpoint.db`. Returns None if the DB is absent."""
        if not self._db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            counts = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM cases GROUP BY status"
                ).fetchall()
            }
            error_rows = conn.execute(
                "SELECT SUBSTR(error, 1, 40) AS err, COUNT(*) FROM cases "
                "WHERE status='failed' GROUP BY err ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()

        downloaded = counts.get("downloaded", 0)
        in_progress = counts.get("in_progress", 0)
        failed = counts.get("failed", 0)
        pending = counts.get("pending", 0)
        top_error_prefixes = [
            {"prefix": row[0], "count": row[1]}
            for row in error_rows
            if row[0] is not None
        ]
        return {
            "downloaded": downloaded,
            "in_progress": in_progress,
            "failed": failed,
            "pending": pending,
            "total": sum(counts.values()),
            "downloaded_per_hour": None,
            "eta_hours": None,
            "runtime_hours": None,
            "top_error_prefixes": top_error_prefixes,
        }

    # ----------------------------------------------------------------- events

    def _read_events(self) -> dict[str, Any] | None:
        return {
            "window_min": self._window_min,
            "counts_by_kind": {},
            "proxy_hotspots": [],
            "recent_challenges": [],
        }

    # -------------------------------------------------------------------- log

    def _read_log(self) -> dict[str, Any]:
        return {"recent_warnings": []}

    def evaluate_alerts(
        self,
        checkpoint: dict[str, Any],
        events: dict[str, Any] | None,
        runtime_hours: float | None,
    ) -> list[dict[str, str]]:
        """Apply the hour-4 alert rules; return a list of alert dicts."""
        return []

    def render_text(self, summary: dict[str, Any]) -> str:
        return ""

    def render_json(self, summary: dict[str, Any]) -> str:
        return ""
