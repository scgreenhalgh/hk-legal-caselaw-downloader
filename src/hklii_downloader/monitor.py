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

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_CHECKPOINT_FILENAME = ".checkpoint.db"
_EVENTS_FILENAME = "events.jsonl"
_LOG_FILENAME = "scrape.log"

# Event kinds surfaced (and 0-filled) in the recent-events table, in display
# order. Any other kind seen in the window is still counted, appended after.
_TRACKED_KINDS = (
    "request_success", "request_failed", "warmup",
    "challenge_detected", "pool_exhausted", "degraded",
)


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
        # Capture mtime before opening the connection so a read cannot
        # perturb the fallback run-start signal.
        db_mtime = self._db_path.stat().st_mtime
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
            min_seen = conn.execute(
                "SELECT MIN(last_seen_at) FROM cases"
            ).fetchone()[0]
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

        # Run-start: prefer the earliest enumeration timestamp (a real per-row
        # signal). Fall back to the checkpoint mtime when no row carries one,
        # flagging that the rate/ETA are approximate.
        warning = None
        if min_seen is not None:
            run_start = datetime.fromtimestamp(min_seen, tz=timezone.utc)
            run_start_source = "min_last_seen_at"
        else:
            run_start = datetime.fromtimestamp(db_mtime, tz=timezone.utc)
            run_start_source = "checkpoint_mtime"
            warning = (
                "run-start derived from .checkpoint.db mtime (no per-row "
                "enumeration timestamp); rate/ETA are approximate."
            )

        runtime_hours = (self._now - run_start).total_seconds() / 3600.0
        downloaded_per_hour = (
            downloaded / runtime_hours if runtime_hours > 0 else None
        )
        remaining = pending + in_progress
        eta_hours = (
            remaining / downloaded_per_hour
            if downloaded_per_hour and downloaded_per_hour > 0
            else None
        )

        return {
            "downloaded": downloaded,
            "in_progress": in_progress,
            "failed": failed,
            "pending": pending,
            "total": sum(counts.values()),
            "downloaded_per_hour": downloaded_per_hour,
            "eta_hours": eta_hours,
            "runtime_hours": runtime_hours,
            "run_start": run_start.isoformat(),
            "run_start_source": run_start_source,
            "warning": warning,
            "top_error_prefixes": top_error_prefixes,
        }

    # ----------------------------------------------------------------- events

    def _read_events(self) -> dict[str, Any] | None:
        """Count events by kind within the look-back window. Returns None
        when events.jsonl is absent (a --no-events run)."""
        if not self._events_path.exists():
            return None
        cutoff = self._now - timedelta(minutes=self._window_min)
        rows = self._events_in_window(cutoff)

        counts = {k: 0 for k in _TRACKED_KINDS}
        for row in rows:
            kind = row.get("kind")
            if kind is None:
                continue
            counts[kind] = counts.get(kind, 0) + 1

        return {
            "window_min": self._window_min,
            "counts_by_kind": counts,
            "proxy_hotspots": self._proxy_hotspots(rows),
            "recent_challenges": self._recent_challenges(rows),
        }

    @staticmethod
    def _proxy_hotspots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Proxies whose in-window failed count sits >3σ above the pool mean —
        the individual-IP-ban signal. The pool is every proxy that served a
        request in the window (a clean proxy counts as 0 failures), so one IP
        burning while its peers stay healthy stands out."""
        pool: dict[str, int] = {}
        for row in rows:
            kind = row.get("kind")
            if kind not in ("request_success", "request_failed"):
                continue
            proxy = row.get("proxy_url")
            if proxy is None:
                continue
            pool.setdefault(proxy, 0)
            if kind == "request_failed":
                pool[proxy] += 1

        if len(pool) < 2:
            return []
        failed_counts = list(pool.values())
        mean = statistics.fmean(failed_counts)
        sigma = statistics.pstdev(failed_counts)
        if sigma <= 0:
            return []
        threshold = mean + 3.0 * sigma
        hotspots = [
            {
                "proxy_url": proxy,
                "failed": count,
                "mean": round(mean, 1),
                "threshold": round(threshold, 1),
            }
            for proxy, count in pool.items()
            if count > threshold
        ]
        hotspots.sort(key=lambda h: h["failed"], reverse=True)
        return hotspots

    @staticmethod
    def _recent_challenges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Up to 5 most-recent challenge_detected URLs + proxies for eyeball
        WAF inspection. `rows` is newest-first, so slice the first 5."""
        out = []
        for row in rows:
            if row.get("kind") != "challenge_detected":
                continue
            out.append({
                "url": row.get("url"),
                "proxy_url": row.get("proxy_url"),
            })
            if len(out) == 5:
                break
        return out

    def _events_in_window(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Rows with `ts` >= cutoff, newest-first. Reads backward from EOF in
        blocks and stops at the first row older than the window, so a 100MB
        append-only log costs a couple of blocks, not a full scan."""
        rows: list[dict[str, Any]] = []
        block = 65536
        with self._events_path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            carry = b""  # partial head-of-line continuing into an earlier block
            stop = False
            while pos > 0 and not stop:
                size = min(block, pos)
                pos -= size
                fh.seek(pos)
                data = fh.read(size) + carry
                parts = data.split(b"\n")
                carry = parts[0]
                for raw in reversed(parts[1:]):
                    if self._consume_event_line(raw, cutoff, rows):
                        stop = True
                        break
            if not stop:
                self._consume_event_line(carry, cutoff, rows)
        return rows

    @staticmethod
    def _consume_event_line(
        raw: bytes, cutoff: datetime, rows: list[dict[str, Any]],
    ) -> bool:
        """Append a parsed in-window row; return True once a row older than
        cutoff is seen so the backward scan can stop."""
        raw = raw.strip()
        if not raw:
            return False
        try:
            row = json.loads(raw)
        except ValueError:
            return False
        ts = row.get("ts")
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return False
        if dt >= cutoff:
            rows.append(row)
            return False
        return True

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
