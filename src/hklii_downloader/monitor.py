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

# Alert thresholds — see scratchpad/REVIEW_VERDICT.md §hour-4. The failed-status
# error-prefix breakdown is the widest-drift-catching signal; the rate band
# tracks the ~7000/hr production target.
_ERR_PREFIX_CRITICAL = 100      # a single prefix over this → critical
_ERR_PREFIX_WARN = 20           # a single prefix in [20, 100] → warn
_IN_PROGRESS_WORKER_MULT = 4    # in_progress over this x workers → critical (B6)
_RATE_CRITICAL = 4000           # sustained rate under this → critical
_RATE_WARN = 6000               # rate in [4000, 6000] → warn (target ~7000)
_RATE_MIN_RUNTIME_H = 1.0       # rate alerts need >1h of data to be meaningful


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

        # A missing .checkpoint.db is itself critical — we cannot assess
        # health — but still surface whatever events/log exist.
        if checkpoint is None:
            return {
                "severity": "CRITICAL",
                "banner": f"hklii scrape @ {self._output_dir} — checkpoint DB not found",
                "runtime_hours": None,
                "checkpoint": None,
                "events": events,
                "log": log,
                "alerts": [{
                    "level": "CRITICAL",
                    "reason": f"checkpoint DB not found at {self._db_path}",
                    "detail": "cannot assess scrape health without .checkpoint.db",
                }],
            }

        runtime_hours = checkpoint["runtime_hours"]
        alerts = self.evaluate_alerts(checkpoint, events, runtime_hours)
        return {
            "severity": self.severity_for(alerts),
            "banner": self._banner(checkpoint),
            "runtime_hours": runtime_hours,
            "checkpoint": checkpoint,
            "events": events,
            "log": log,
            "alerts": alerts,
        }

    def _banner(self, checkpoint: dict[str, Any]) -> str:
        total = checkpoint["total"]
        downloaded = checkpoint["downloaded"]
        pct = (downloaded / total * 100) if total else 0.0
        h = checkpoint["runtime_hours"]
        hour = f"{h:.1f}" if h is not None else "?"
        return (
            f"hklii scrape @ {self._output_dir} — hour {hour}, "
            f"{downloaded}/{total} ({pct:.1f}%)"
        )

    @staticmethod
    def severity_for(alerts: list[dict[str, str]]) -> str:
        """Collapse a list of alerts to a single severity by precedence."""
        levels = {a["level"] for a in alerts}
        if "CRITICAL" in levels:
            return "CRITICAL"
        if "WARN" in levels:
            return "WARN"
        return "HEALTHY"

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
        """Proxies whose in-window failed+challenge count sits >3σ above
        the pool mean — the individual-IP-ban signal.

        Counts both `request_failed` (5xx/timeout etc.) AND
        `challenge_detected` (200 with a WAF interstitial). The
        challenge-page case is the exact 'WAF flags a specific IP with
        a 200 challenge page' pattern this analysis exists to catch;
        omitting it silently suppressed the loudest hotspot signal
        (memory / doc:review-patterns L2).

        Pool is every proxy that served a request in the window (a
        clean proxy counts as 0 failures), so one IP burning while
        its peers stay healthy stands out.
        """
        pool: dict[str, int] = {}
        for row in rows:
            kind = row.get("kind")
            if kind not in (
                "request_success", "request_failed", "challenge_detected",
            ):
                continue
            proxy = row.get("proxy_url")
            if proxy is None:
                continue
            pool.setdefault(proxy, 0)
            if kind in ("request_failed", "challenge_detected"):
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
        """Tail the last 200 lines of scrape.log and keep the last 5 WARN/
        ERROR/CRITICAL records, reformatted as `[HH:MM:SS] message`. Returns
        recent_warnings=None when there is no log file."""
        if not self._log_path.exists():
            return {"recent_warnings": None}
        warnings = []
        for line in self._tail_lines(self._log_path, 200):
            parsed = self._parse_log_warning(line)
            if parsed is not None:
                warnings.append(parsed)
        return {"recent_warnings": warnings[-5:]}

    @staticmethod
    def _tail_lines(path: Path, n: int) -> list[str]:
        block = 65536
        data = b""
        with path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            while pos > 0 and data.count(b"\n") <= n:
                size = min(block, pos)
                pos -= size
                fh.seek(pos)
                data = fh.read(size) + data
        return data.decode("utf-8", "replace").splitlines()[-n:]

    @staticmethod
    def _parse_log_warning(line: str) -> str | None:
        """`<date> <time,ms> <LEVEL> <name>: <msg>` → `[HH:MM:SS] <msg>` for
        WARNING/ERROR/CRITICAL records; None otherwise (INFO, tracebacks)."""
        if ": " not in line:
            return None
        prefix, msg = line.split(": ", 1)
        parts = prefix.split()
        if len(parts) < 3 or parts[2] not in ("WARNING", "ERROR", "CRITICAL"):
            return None
        hhmmss = parts[1].split(",")[0]
        return f"[{hhmmss}] {msg.strip()}"

    def evaluate_alerts(
        self,
        checkpoint: dict[str, Any],
        events: dict[str, Any] | None,
        runtime_hours: float | None,
    ) -> list[dict[str, str]]:
        """Apply the hour-4 alert rules; return a list of alert dicts. Event
        rules are skipped when `events` is None (a --no-events run) — the
        checkpoint rules still fire."""
        alerts: list[dict[str, str]] = []

        for ep in checkpoint.get("top_error_prefixes", []):
            count, prefix = ep["count"], ep["prefix"]
            if count > _ERR_PREFIX_CRITICAL:
                alerts.append({
                    "level": "CRITICAL",
                    "reason": f"error-prefix {prefix!r} has {count} hits",
                    "detail": f"failed-status prefix over {_ERR_PREFIX_CRITICAL}",
                })
            elif count >= _ERR_PREFIX_WARN:
                alerts.append({
                    "level": "WARN",
                    "reason": f"error-prefix {prefix!r} has {count} hits",
                    "detail": (
                        f"failed-status prefix in "
                        f"{_ERR_PREFIX_WARN}-{_ERR_PREFIX_CRITICAL}"
                    ),
                })

        in_progress = checkpoint.get("in_progress", 0)
        ip_threshold = _IN_PROGRESS_WORKER_MULT * self._workers
        if in_progress > ip_threshold:
            alerts.append({
                "level": "CRITICAL",
                "reason": (
                    f"in_progress {in_progress} > "
                    f"{_IN_PROGRESS_WORKER_MULT}x workers ({self._workers})"
                ),
                "detail": "workers stranding rows in in_progress (B6 symptom)",
            })

        rate = checkpoint.get("downloaded_per_hour")
        if (
            rate is not None
            and runtime_hours is not None
            and runtime_hours > _RATE_MIN_RUNTIME_H
        ):
            if rate < _RATE_CRITICAL:
                alerts.append({
                    "level": "CRITICAL",
                    "reason": f"downloaded rate {rate:.0f}/hr < {_RATE_CRITICAL}/hr",
                    "detail": "sustained low throughput",
                })
            elif rate <= _RATE_WARN:
                alerts.append({
                    "level": "WARN",
                    "reason": f"downloaded rate {rate:.0f}/hr below 7000/hr target",
                    "detail": f"rate in {_RATE_CRITICAL}-{_RATE_WARN}/hr",
                })

        if events is not None:
            counts = events.get("counts_by_kind", {})
            window = events.get("window_min")
            degraded = counts.get("degraded", 0)
            if degraded > 0:
                alerts.append({
                    "level": "WARN",
                    "reason": f"{degraded} degraded event(s) in last {window}min",
                    "detail": "B3 IP-check swallow — potential leak window",
                })
            pool_exhausted = counts.get("pool_exhausted", 0)
            if pool_exhausted > 0:
                alerts.append({
                    "level": "WARN",
                    "reason": (
                        f"{pool_exhausted} pool_exhausted event(s) "
                        f"in last {window}min"
                    ),
                    "detail": "B6 pool blackout",
                })
            for hotspot in events.get("proxy_hotspots", []):
                alerts.append({
                    "level": "WARN",
                    "reason": (
                        f"proxy {hotspot['proxy_url']} failures "
                        f"{hotspot['failed']} > 3σ above mean"
                    ),
                    "detail": "probable individual-IP ban",
                })

        return alerts

    def render_json(self, summary: dict[str, Any]) -> str:
        return json.dumps(summary, indent=2, ensure_ascii=False)

    def render_text(self, summary: dict[str, Any]) -> str:
        rule = "─" * 62
        lines = [self._headline(summary), rule]

        checkpoint = summary["checkpoint"]
        if checkpoint is None:
            lines.append(summary["alerts"][0]["reason"])
            return "\n".join(lines)

        lines.append(f"{'status':<14} {'count':>7}")
        lines.append(f"{'─' * 14} {'─' * 7}")
        for key in ("downloaded", "in_progress", "failed", "pending"):
            lines.append(f"{key:<14} {checkpoint[key]:>7}")
        lines.append(rule)

        lines.append("top error prefixes")
        if checkpoint["top_error_prefixes"]:
            for ep in checkpoint["top_error_prefixes"]:
                lines.append(f"  {ep['prefix']:<40} {ep['count']:>5}")
        else:
            lines.append("  (none)")
        lines.append(rule)

        events = summary["events"]
        lines.append(f"recent events (last {self._window_min} min)")
        if events is None:
            lines.append("  N/A (events.jsonl not found — --no-events run?)")
        else:
            counts = events["counts_by_kind"]
            for kind in _TRACKED_KINDS:
                lines.append(f"  {kind:<22} {counts.get(kind, 0):>6}")
        lines.append(rule)

        lines.append("per-proxy failure hotspots (>3σ above mean)")
        if events is None:
            lines.append("  N/A")
        elif events["proxy_hotspots"]:
            for hotspot in events["proxy_hotspots"]:
                lines.append(
                    f"  {hotspot['proxy_url']:<30} failed={hotspot['failed']}"
                )
        else:
            lines.append("  (none)")
        lines.append(rule)

        lines.append("last 5 log warnings")
        warns = summary["log"]["recent_warnings"]
        if warns is None:
            lines.append("  (no log file)")
        elif warns:
            lines.extend(f"  {w}" for w in warns)
        else:
            lines.append("  (none)")

        return "\n".join(lines)

    def _headline(self, summary: dict[str, Any]) -> str:
        """Top line: `[SEVERITY]` then either the most-severe alert reason
        (when anything fired) or the scrape banner + rate/ETA (when healthy)."""
        severity = summary["severity"]
        alerts = summary["alerts"]
        if alerts:
            critical = [a for a in alerts if a["level"] == "CRITICAL"]
            top = critical[0] if critical else alerts[0]
            return f"[{severity}] {top['reason']}"

        banner = summary["banner"]
        checkpoint = summary["checkpoint"]
        extra = ""
        if checkpoint and checkpoint.get("downloaded_per_hour") is not None:
            extra = f", ~{checkpoint['downloaded_per_hour']:.0f}/hr"
            if checkpoint.get("eta_hours") is not None:
                extra += f", ETA ~{checkpoint['eta_hours']:.1f}h"
        return f"[{severity}] {banner}{extra}"
