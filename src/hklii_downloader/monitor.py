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
        return {
            "severity": "HEALTHY",
            "banner": "",
            "runtime_hours": None,
            "checkpoint": {
                "downloaded": 0,
                "in_progress": 0,
                "failed": 0,
                "pending": 0,
                "total": 0,
                "downloaded_per_hour": None,
                "eta_hours": None,
                "top_error_prefixes": [],
            },
            "events": {
                "window_min": self._window_min,
                "counts_by_kind": {},
                "proxy_hotspots": [],
                "recent_challenges": [],
            },
            "log": {"recent_warnings": []},
            "alerts": [],
        }

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
