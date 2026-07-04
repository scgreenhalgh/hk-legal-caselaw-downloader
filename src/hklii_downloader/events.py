"""Structured event logging for post-run scrape triage.

`events.jsonl` carries one JSON object per line — a machine-readable
companion to the human `scrape.log`. A 15-20h / ~228K-request run needs
`jq`-able per-proxy / per-error / hourly analytics that grepping a text
log cannot give (see research/13-observability.md).

Durability + concurrency contract:

- **Atomic append.** Each row is written as one full line (payload + "\n")
  in a single `os.write` to an `O_APPEND` fd. POSIX makes concurrent
  `O_APPEND` writes non-interleaving, and a whole-line write means a
  SIGKILL lands *between* lines, never mid-line — so `jq -c` never chokes
  on a torn final row.
- **Backpressure-safe.** `emit()` is a non-blocking enqueue onto a bounded
  queue drained by a single background writer coroutine; a slow disk stalls
  the writer, not the scraper's worker coroutines. On overflow the row is
  dropped (count-only, throttled WARN) rather than blocking the event loop.
- **Optional everywhere.** Callers hold `EventLogger | None`; None is a
  valid no-op state, so unit tests never need to wire this.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_write import atomic_write_bytes, atomic_write_text

_log = logging.getLogger("hklii_downloader.events")

_EVENTS_FILENAME = "events.jsonl"
_SAMPLES_DIRNAME = "failure_samples"

# Global bucket key for the challenge-page sample budget (distinct from the
# per-error-prefix budgets, which key on the caller's signature).
_CHALLENGE_BUCKET = "\x00challenge"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Sentinel pushed onto the queue by aclose() so the writer drains every
# already-queued row before exiting.
_STOP = object()

# Ordered so the JSONL columns read left-to-right the way an operator scans
# them; `ts` and `kind` always lead.
_FIELD_ORDER = (
    "court", "year", "num", "proxy_url", "url", "http_status",
    "elapsed_ms", "error_class", "error_msg", "response_len",
    "retry_attempt", "extra",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_signature(signature: str) -> str:
    """Collapse anything that is not filesystem-safe into single underscores so
    an error/URL-shaped signature becomes a valid, readable filename stem."""
    safe = _UNSAFE_FILENAME_CHARS.sub("_", signature).strip("._")
    return (safe or "sample")[:120]


class StructuredEventLogger:
    def __init__(
        self,
        output_dir: Path | str,
        *,
        max_queue: int = 10_000,
        challenge_sample_cap: int = 20,
        per_prefix_sample_cap: int = 5,
        max_sample_bytes: int = 200 * 1024,
    ):
        self._output_dir = Path(output_dir)
        self._events_path = self._output_dir / _EVENTS_FILENAME
        self._samples_dir = self._output_dir / _SAMPLES_DIRNAME
        self._max_queue = max_queue

        self._challenge_sample_cap = challenge_sample_cap
        self._per_prefix_sample_cap = per_prefix_sample_cap
        self._max_sample_bytes = max_sample_bytes
        self._sample_counts: dict[str, int] = {}

        self._queue: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None
        self._fd: int | None = None
        self._dropped = 0

    # ------------------------------------------------------------------ emit

    def emit(
        self,
        kind: str,
        *,
        court: str | None = None,
        year: int | None = None,
        num: int | None = None,
        proxy_url: str | None = None,
        url: str | None = None,
        http_status: int | None = None,
        elapsed_ms: int | None = None,
        error_class: str | None = None,
        error_msg: str | None = None,
        response_len: int | None = None,
        retry_attempt: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record one structured event. Non-blocking and never raises —
        event logging must never take down the scrape it observes."""
        row: dict[str, Any] = {"ts": _now_iso(), "kind": kind}
        values = {
            "court": court, "year": year, "num": num, "proxy_url": proxy_url,
            "url": url, "http_status": http_status, "elapsed_ms": elapsed_ms,
            "error_class": error_class, "error_msg": error_msg,
            "response_len": response_len, "retry_attempt": retry_attempt,
            "extra": extra,
        }
        for key in _FIELD_ORDER:
            v = values[key]
            if v is not None:
                row[key] = v

        try:
            line = json.dumps(row, ensure_ascii=False)
        except (TypeError, ValueError):
            # A non-serializable `extra` must not crash the caller; drop the
            # extra and re-encode the skeleton so the event still lands.
            row.pop("extra", None)
            line = json.dumps(row, ensure_ascii=False)

        if self._writer_task is not None and not self._writer_task.done():
            assert self._queue is not None
            try:
                self._queue.put_nowait(line)
            except asyncio.QueueFull:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 1000 == 0:
                    _log.warning(
                        "events queue full — dropped %d event row(s); "
                        "disk cannot keep up with emit rate",
                        self._dropped,
                    )
        else:
            # No running writer (pre-start, post-close, or unit test): a
            # direct synchronous append is still atomic on an O_APPEND fd.
            self._write_line(line)

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._ensure_fd()
        self._queue = asyncio.Queue(maxsize=self._max_queue)
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def aclose(self) -> None:
        if self._writer_task is not None and self._queue is not None:
            await self._queue.put(_STOP)
            await self._writer_task
            self._writer_task = None
        self._fsync()
        self._close_fd()

    async def __aenter__(self) -> "StructuredEventLogger":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _writer_loop(self) -> None:
        assert self._queue is not None
        while True:
            line = await self._queue.get()
            try:
                if line is _STOP:
                    return
                self._write_line(line)
                # Coalesced durability: fsync only when we've caught up, so a
                # burst costs one fsync instead of one-per-row.
                if self._queue.empty():
                    self._fsync()
            finally:
                self._queue.task_done()

    # ---------------------------------------------------------- failure samples

    def sample_failure(
        self,
        signature: str,
        body: str | bytes,
        headers: dict | None = None,
        *,
        is_challenge: bool = False,
    ) -> bool:
        """Persist the raw body + headers of a failure/challenge response to
        `failure_samples/` for post-run WAF signature analysis.

        Caps: `challenge_sample_cap` total challenge hits, then
        `per_prefix_sample_cap` per distinct error `signature`. Bodies are
        truncated to `max_sample_bytes`. Beyond a cap the call is count-only
        and returns False. Never raises — a sampling failure must not take
        down the scrape."""
        bucket = _CHALLENGE_BUCKET if is_challenge else signature
        cap = (
            self._challenge_sample_cap if is_challenge
            else self._per_prefix_sample_cap
        )
        if self._sample_counts.get(bucket, 0) >= cap:
            return False

        try:
            self._samples_dir.mkdir(parents=True, exist_ok=True)
            base = self._unique_sample_base(_sanitize_signature(signature))

            raw = body.encode("utf-8", "replace") if isinstance(body, str) else body
            truncated = len(raw) > self._max_sample_bytes
            if truncated:
                raw = raw[: self._max_sample_bytes]
            atomic_write_bytes(self._samples_dir / f"{base}.html", raw)

            meta = {
                "signature": signature,
                "captured_at": _now_iso(),
                "is_challenge": is_challenge,
                "truncated": truncated,
                "body_bytes": len(raw),
                "headers": dict(headers) if headers else {},
            }
            atomic_write_text(
                self._samples_dir / f"{base}.headers.json",
                json.dumps(meta, indent=2, ensure_ascii=False),
            )
        except OSError as exc:
            _log.warning("failed to write failure sample %r: %s", signature, exc)
            return False

        self._sample_counts[bucket] = self._sample_counts.get(bucket, 0) + 1
        return True

    def _unique_sample_base(self, base: str) -> str:
        candidate = base
        n = 1
        while (self._samples_dir / f"{candidate}.html").exists():
            candidate = f"{base}_{n}"
            n += 1
        return candidate

    # -------------------------------------------------------------------- I/O

    def _ensure_fd(self) -> None:
        if self._fd is not None:
            return
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(
            self._events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        # Make the file's directory entry durable so the log survives an
        # unclean reboot even before the first fsync of its contents.
        self._fsync_dir()

    def _write_line(self, line: str) -> None:
        try:
            self._ensure_fd()
            assert self._fd is not None
            data = (line + "\n").encode("utf-8")
            # Loop in case os.write is short (rare on regular files) so we
            # never emit a partial line.
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
        except OSError as exc:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                _log.warning(
                    "failed to write event row (%s); dropped %d so far",
                    exc, self._dropped,
                )

    def _fsync(self) -> None:
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            except OSError:
                pass

    def _fsync_dir(self) -> None:
        try:
            dir_fd = os.open(self._output_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    @property
    def dropped(self) -> int:
        return self._dropped
