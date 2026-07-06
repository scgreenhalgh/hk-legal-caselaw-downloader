from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

_log = logging.getLogger("hklii_downloader.scraper")

from .checkpoint import CheckpointDB, CaseRecord
from .client import Judgment, parse_judgment_response, save_judgment_local
from .content_shape import _looks_like_challenge_page
from .enrichment import (
    enrich_appeal_history_for_case,
    enrich_summaries_for_case,
)
from .enumerator import EnumWindow, enumerate_court
from .events import StructuredEventLogger
from .parser import HKLIICase
from .proxy_pool import AllProxiesDeadError, IPLeakError

_PERMANENT_ERRORS = {404, 410}
_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}
_BODY_PREVIEW_LEN = 200

# Magic-byte signatures for Word document bodies. Content-Type is
# unreliable — some proxies/CDNs strip or rewrite it — so we shape-check
# the file's own header instead. Without this guard, an HTTP 200 HTML
# challenge page (~2-8 KB) served by a WAF mid-run would be written
# verbatim to `.docx`, mark the row `downloaded`, and RAG downstream
# would choke on BadZipFile at ingest time. Sibling defenses live in
# content_shape._looks_like_challenge_page (API branch, enrichment).
# Signature → on-disk extension. Judiciary URLs are unreliable indicators
# of the actual format (some `.doc` URLs serve RTF, some 1990s files use
# pre-OLE Word), so we derive the extension from the file body's magic
# bytes. Everything not in this map is rejected.
_DOC_MAGIC_EXTENSIONS: dict[bytes, str] = {
    b"PK\x03\x04": ".docx",          # OOXML — ZIP archive (Word 2007+)
    b"\xd0\xcf\x11\xe0": ".doc",     # OLE compound document (Word 97+)
    b"\xdb\xa5\x2d\x00": ".doc",     # pre-OLE Word 6.0 / Word for Windows 95
                                     # — Judiciary serves these for many 1990s
                                     # judgments (task #64).
    b"{\\rt": ".rtf",                # RTF — Judiciary uses RTF for some
                                     # 1990s-early-2000s files served at .doc
                                     # URLs; content is real judgment text
                                     # (task #67).
}

_DOC_MAGIC_SIGNATURES = tuple(_DOC_MAGIC_EXTENSIONS)


def _has_valid_doc_magic(body: bytes) -> bool:
    """True if the first 4 bytes match a known document signature."""
    if len(body) < 4:
        return False
    head = body[:4]
    return head in _DOC_MAGIC_EXTENSIONS


def _extension_for_body(body: bytes) -> str | None:
    """Return the on-disk extension for a validated document body, or None
    if the magic doesn't match a known format. Callers should have already
    gated on `_has_valid_doc_magic` — this is the sibling that names the
    file. Extension is magic-driven so a mislabelled URL (e.g. RTF served
    at `.doc`) still lands on a filesystem stem that matches the format."""
    if len(body) < 4:
        return None
    return _DOC_MAGIC_EXTENSIONS.get(body[:4])


def _error_class(error: str) -> str:
    """Bucket an error string to a short, greppable class for per-error-class
    analytics — everything up to the first `: ; ,` delimiter. E.g.
    'HTTP 503 after 3 retries; body: ...' -> 'HTTP 503 after 3 retries'."""
    return re.split(r"[:;,]", error, maxsplit=1)[0].strip()[:60]


def _response_headers(resp) -> dict:
    """Best-effort header extraction that works for both httpx.Response
    (tests) and curl_cffi's Response (production)."""
    try:
        return {k: v for k, v in resp.headers.items()}
    except Exception:
        return {}


def _jittered_backoff(base: float, attempt: int) -> float:
    """Exponential backoff with multiplicative uniform jitter in [0.5, 1.5].

    Deterministic base * 2**attempt makes N concurrent workers retry in
    lockstep after a 5xx burst — six identical retry intervals from six
    subnets is a bot signal in access logs. Multiplying by U(0.5, 1.5)
    decorrelates them.
    """
    return base * (2 ** attempt) * random.uniform(0.5, 1.5)


@dataclass
class ScrapeResult:
    downloaded: int
    failed: int


class BulkScraper:
    # Task #65 — pool-exhausted (all sessions dead simultaneously) is a
    # recoverable state. Instead of terminal-failing rows in the drain,
    # we re-queue and sleep so the pool has time to revive via cooldown.
    # Terminal-fail only after `max_retries` re-queue cycles; this gives
    # each row a ~2-minute budget (60 * 2s) to ride out a mass-kill event.
    # Both are class attrs so tests can tune them down for millisecond
    # runtimes. See memory/incident-2026-07-05-pool-storm.md.
    _pool_exhausted_max_retries: int = 60
    _pool_exhausted_sleep: float = 2.0

    def __init__(
        self,
        get: Callable,
        checkpoint: CheckpointDB,
        output_dir: Path,
        formats: set[str] | None = None,
        workers: int = 1,
        max_retries: int = 3,
        limit: int | None = None,
        with_summaries: bool = False,
        with_appeal_history: bool = False,
        enum_max_age_hours: int = 0,
        save_enum_responses: bool = False,
        events: StructuredEventLogger | None = None,
        _backoff_base: float = 1.0,
        items_per_page: int = 10_000,
        min_date_text: str | None = None,
        max_date_text: str | None = None,
        sort: str | None = None,
        window: "EnumWindow | None" = None,
    ):
        # EnumWindow, if provided, wins over the individual kwargs.
        # This lets callers thread ONE object down the CLI stack rather
        # than four coupled scalars.
        if window is not None:
            items_per_page = window.items_per_page
            min_date_text = window.min_date_text
            max_date_text = window.max_date_text
            sort = window.sort
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._formats = formats if formats is not None else {"html", "txt", "json"}
        self._workers = workers
        self._max_retries = max_retries
        self._limit = limit
        self._with_summaries = with_summaries
        self._with_appeal_history = with_appeal_history
        self._enum_max_age_hours = enum_max_age_hours
        self._save_enum_responses = save_enum_responses
        self._events = events
        self._backoff_base = _backoff_base
        self._items_per_page = items_per_page
        self._min_date_text = min_date_text
        self._max_date_text = max_date_text
        self._sort = sort

    def _emit(self, kind: str, **fields) -> None:
        if self._events is not None:
            self._events.emit(kind, **fields)

    async def enumerate(
        self, courts: list[str], langs: tuple[str, ...] = ("en", "tc"),
    ) -> int:
        import time
        run_ts = int(time.time())
        seen: set[tuple[str, int, int]] = set()
        # Register this enumeration attempt so orphan_mark (and any future
        # consumer needing "was there a clean full-corpus sweep") can key
        # off a concrete generation marker instead of scanning per-bucket
        # last_seen_at heuristics. completed_at stays NULL if we raise.
        # min_date_text / max_date_text are stored on the row so
        # orphan_mark can filter out narrow-window rows and only fall
        # back to a genuine full-corpus reference.
        generation_id = self._checkpoint.start_enum_run(
            courts, langs,
            min_date_text=self._min_date_text,
            max_date_text=self._max_date_text,
        )
        for court in courts:
            for lang in langs:
                if self._enum_max_age_hours > 0:
                    last_ts = self._checkpoint.last_enumeration_ts(court, lang)
                    if last_ts is not None and (run_ts - last_ts) < self._enum_max_age_hours * 3600:
                        age_h = (run_ts - last_ts) / 3600
                        _log.info(
                            "skip enumerate court=%s lang=%s (last %.1fh ago, cache window %dh)",
                            court, lang, age_h, self._enum_max_age_hours,
                        )
                        continue

                _log.info(
                    "enumerate court=%s lang=%s via %s",
                    court, lang, self._get_path_label(),
                )
                # itemsPerPage=10000 — 13 total enumeration calls across
                # the whole corpus. Trades on-wire pattern realism for
                # speed + durability: the smaller values I tried earlier
                # (20-50) turned each court into 2500+ sequential API
                # calls, which pushed enumeration to 40+ min per court
                # and any single mid-enum timeout wiped everything since
                # entries only land in the DB after enumerate_court
                # returns. Bulk enumeration is inherently scraper-shaped
                # no matter what page size we pick.
                entries = await enumerate_court(
                    court, self._get, lang=lang,
                    items_per_page=self._items_per_page,
                    save_response_to=(
                        self._output_dir / ".enum_cache"
                        if self._save_enum_responses else None
                    ),
                    min_date_text=self._min_date_text,
                    max_date_text=self._max_date_text,
                    sort=self._sort,
                )
                for entry in entries:
                    self._checkpoint.upsert_case(
                        entry.court, entry.year, entry.number,
                        entry.neutral, entry.title, entry.date,
                        lang=lang, last_seen_at=run_ts,
                    )
                    seen.add((entry.court, entry.year, entry.number))
        # All (court, lang) pairs succeeded → stamp completion.
        self._checkpoint.complete_enum_run(generation_id)
        return len(seen)

    def _get_path_label(self) -> str:
        """Human-readable label for whichever get() this scraper is using —
        proves at log time that enumeration is routed through the pool."""
        get = self._get
        if hasattr(get, "__self__"):
            owner = type(get.__self__).__name__
            method = getattr(get, "__name__", "?")
            return f"{owner}.{method}"
        return getattr(get, "__qualname__", repr(get))

    async def download_all(
        self,
        on_progress: Callable[[dict], None] | None = None,
    ) -> ScrapeResult:
        self._checkpoint.release_in_progress()

        counter_lock = asyncio.Lock()
        stats = {"downloaded": 0, "failed": 0, "dispatched": 0}
        # Per-row pool-exhausted retry counter (task #65). Keyed by
        # (court, year, number). Only one worker holds a given row at a
        # time (claim_pending is atomic), so per-row increments do not
        # race across workers.
        pool_exhausted_retries: dict[tuple[str, int, int], int] = {}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if (self._limit is not None
                            and stats["dispatched"] >= self._limit):
                        return
                    record = self._checkpoint.claim_pending()
                    if record is None:
                        return
                    stats["dispatched"] += 1

                try:
                    success = await self._download_one(record)
                except AllProxiesDeadError as exc:
                    # Task #65 — pool went dead mid-run (e.g. a burst of
                    # target-side timeouts pushed every ProxySession past
                    # its failure counter in the same second). Terminal-
                    # failing here drains the queue at SQLite speed
                    # (7,730 rows in 44s during the 2026-07-04 incident).
                    # Re-queue instead: release the row (in_progress →
                    # pending) and sleep so cooldowns can elapse and
                    # _revive_cooled_down_sessions can bring sessions back.
                    # Only terminal-fail once we've exhausted the retry
                    # budget (default: 60 * 2s = 2 minutes per row, plenty
                    # to survive typical mass-kill events).
                    key = (record.court, record.year, record.number)
                    pool_exhausted_retries[key] = (
                        pool_exhausted_retries.get(key, 0) + 1
                    )
                    attempt = pool_exhausted_retries[key]
                    if attempt >= self._pool_exhausted_max_retries:
                        # Pool never came back for this row within budget —
                        # terminal-fail with a distinctive prefix so
                        # --retry-failed can reclaim it next run.
                        self._fail(
                            record.court, record.year, record.number,
                            f"pool-exhausted after {attempt} retries: {exc}",
                            event_kind="pool_exhausted",
                        )
                        success = False
                    else:
                        # Re-queue and back off. The `dispatched` counter
                        # is decremented so --limit budgets attempts, not
                        # retries. The row goes back to pending; the next
                        # claim (this worker's next iteration, or another
                        # worker's) will pick it up once the pool revives.
                        self._checkpoint.release_row(
                            record.court, record.year, record.number,
                        )
                        self._emit(
                            "pool_exhausted",
                            court=record.court,
                            year=record.year,
                            num=record.number,
                            error_class="pool-exhausted",
                            error_msg=(
                                f"re-queued (attempt {attempt}/"
                                f"{self._pool_exhausted_max_retries}): "
                                f"{exc}"
                            ),
                        )
                        async with counter_lock:
                            stats["dispatched"] -= 1
                        await asyncio.sleep(self._pool_exhausted_sleep)
                        continue
                except Exception:
                    # Belt-and-braces: _download_one catches known errors
                    # already; this guard prevents an unforeseen bug from
                    # cancelling sibling workers via asyncio.gather.
                    success = False
                async with counter_lock:
                    if success:
                        stats["downloaded"] += 1
                    else:
                        stats["failed"] += 1
                    if on_progress is not None:
                        on_progress(stats)

        await asyncio.gather(
            *[worker() for _ in range(self._workers)],
            return_exceptions=True,
        )
        return ScrapeResult(
            downloaded=stats["downloaded"], failed=stats["failed"],
        )

    def _fail(
        self, court: str, year: int, number: int, error: str,
        *, event_kind: str = "case_failed", **event_fields,
    ) -> None:
        """Mark a row failed AND emit a WARNING log — the runbook's
        WAF tripwire greps for 'FAILED' in scrape.log
        (see docs/RUNBOOK.md line 367). Writing to the DB error column
        without logging leaves the operator blind over a 15-20h scrape.

        Also emits one structured event (`event_kind`, default
        `case_failed`) so events.jsonl carries the same failure with a
        greppable `error_class`. The challenge / pool-exhausted sites pass
        their own `event_kind` so they bucket separately in analytics."""
        self._checkpoint.mark_failed(court, year, number, error)
        _log.warning("FAILED %s/%s/%s: %s", court, year, number, error)
        self._emit(
            event_kind, court=court, year=year, num=number,
            error_class=_error_class(error), error_msg=error, **event_fields,
        )

    async def _download_one(self, record: CaseRecord) -> bool:
        try:
            return await self._download_one_impl(record)
        except IPLeakError as e:
            self._fail(
                record.court, record.year, record.number,
                f"IPLeakError: {e}",
            )
            return False
        except OSError as e:
            _log.error(
                "OSError during save %s/%s/%s: %s",
                record.court, record.year, record.number, e,
            )
            self._fail(
                record.court, record.year, record.number,
                f"OSError during save: {e}",
            )
            return False

    async def _download_one_impl(self, record: CaseRecord) -> bool:
        case = HKLIICase(
            lang=record.lang, court=record.court,
            year=record.year, number=record.number,
        )

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._get(case.api_url)
            except httpx.RequestError as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                    continue
                self._fail(
                    record.court, record.year, record.number,
                    f"{type(e).__name__} after {self._max_retries} retries: {e}",
                    url=case.api_url, retry_attempt=attempt,
                )
                return False

            if resp.status_code in _PERMANENT_ERRORS:
                self._fail(
                    record.court, record.year, record.number,
                    f"HTTP {resp.status_code}",
                    url=case.api_url, http_status=resp.status_code,
                    retry_attempt=attempt,
                )
                return False

            if resp.status_code in _RETRYABLE_STATUSES:
                if attempt < self._max_retries:
                    await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                    continue
                preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
                if self._events is not None:
                    self._events.sample_failure(
                        f"HTTP_{resp.status_code}",
                        resp.text, _response_headers(resp),
                    )
                self._fail(
                    record.court, record.year, record.number,
                    f"HTTP {resp.status_code} after {self._max_retries} retries; body: {preview}",
                    url=case.api_url, http_status=resp.status_code,
                    retry_attempt=attempt,
                )
                return False

            try:
                data = resp.json()
            except json.JSONDecodeError:
                if attempt < self._max_retries:
                    await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                    continue
                preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
                if self._events is not None:
                    self._events.sample_failure(
                        f"JSONDecodeError_HTTP_{resp.status_code}",
                        resp.text, _response_headers(resp),
                    )
                self._fail(
                    record.court, record.year, record.number,
                    f"JSONDecodeError after {self._max_retries} retries; "
                    f"HTTP {resp.status_code}; body: {preview}",
                    url=case.api_url, http_status=resp.status_code,
                    retry_attempt=attempt,
                )
                return False

            judgment = parse_judgment_response(case, data)
            output_dir = self._output_dir / record.court / str(record.year)

            if _looks_like_challenge_page(judgment.content_html):
                # Distinct WARNING so the runbook's grep for the literal
                # 'challenge-page detected' string (line 367) surfaces the
                # WAF signal even if only 1-2 rows out of thousands hit it.
                _log.warning(
                    "challenge-page detected on %s/%s/%s",
                    record.court, record.year, record.number,
                )
                # Dump the raw response body + headers for post-run WAF
                # fingerprint analysis (capped at 20 challenge samples/run).
                if self._events is not None:
                    self._events.sample_failure(
                        f"challenge_{record.court}_{record.year}_{record.number}",
                        resp.text, _response_headers(resp), is_challenge=True,
                    )
                self._fail(
                    record.court, record.year, record.number,
                    "challenge-page detected in content_html",
                    event_kind="challenge_detected",
                    proxy_url=getattr(resp, "hklii_proxy_url", None),
                    url=case.api_url, http_status=resp.status_code,
                    response_len=len(judgment.content_html),
                )
                return False

            content_ok = bool(judgment.content_html.strip())
            can_try_doc = "doc" in self._formats and judgment.doc_url

            if not content_ok and not can_try_doc:
                doc_hint = f", doc_url={judgment.doc_url}" if judgment.doc_url else ""
                self._fail(
                    record.court, record.year, record.number,
                    f"empty-content{doc_hint}",
                )
                return False

            actually_saved: set[str] = set()
            if content_ok:
                save_judgment_local(judgment, output_dir, self._formats)
                actually_saved = set(self._formats) - {"doc"}

            if can_try_doc:
                output_dir.mkdir(parents=True, exist_ok=True)
                doc_ok, doc_err = await self._fetch_doc(judgment, output_dir)
                if doc_ok:
                    actually_saved.add("doc")
                elif doc_err is not None:
                    # Hard failure with a specific reason (e.g. invalid
                    # magic bytes — a WAF-flip signal). Route through
                    # `_fail` directly so the DB carries the specific
                    # error class and events.jsonl buckets it, instead
                    # of collapsing into the generic 'doc-fetch-failed'
                    # message. This also fires when content_ok=True —
                    # a run-wide Judiciary WAF flip must not hide behind
                    # 'downloaded=N' counters with silently-dropped docs.
                    self._fail(
                        record.court, record.year, record.number, doc_err,
                        event_kind="doc_invalid_magic",
                        url=judgment.doc_url,
                    )
                    return False
                elif not content_ok:
                    # Empty content AND doc fetch failed — nothing on disk
                    self._fail(
                        record.court, record.year, record.number,
                        f"empty-content, doc-fetch-failed, doc_url={judgment.doc_url}",
                    )
                    return False

            # If content_html was empty and we saved via doc-fallback, stamp
            # html_pending_at_hklii so a later `hklii recheck-html` pass
            # can find these rows. When content_html was present, the
            # kwarg stays None which clears any prior stamp.
            html_pending_ts = (
                int(time.time())
                if not content_ok and "doc" in actually_saved
                else None
            )
            self._checkpoint.mark_downloaded(
                record.court, record.year, record.number,
                sorted(actually_saved),
                html_pending_ts=html_pending_ts,
            )

            if self._with_summaries:
                await self._enrich_summaries(record, judgment, output_dir)
            if self._with_appeal_history:
                await self._enrich_appeal_history(record, judgment, output_dir)

            return True

        return False

    async def _fetch_doc(
        self, judgment: Judgment, output_dir: Path,
    ) -> tuple[bool, str | None]:
        """Fetch and persist the doc-fallback body.

        Returns (success, hard_error). `hard_error` is a specific failure
        reason (e.g. 'doc-invalid-magic: 0x3c68746d') that the caller
        must surface via `_fail` — bypassing the generic 'doc-fetch-failed'
        message. `None` means transient (network / retryable HTTP), which
        the caller may either ignore (content_ok=True) or generic-fail
        (content_ok=False).

        The magic-byte shape-check exists because HTTP 200 alone doesn't
        prove the body is a docx — a WAF flip on Judiciary F5 mid-run
        can return a 200 HTML challenge page (~2-8 KB) that we'd otherwise
        write verbatim as `.docx`, stamp `downloaded`, and poison RAG.
        Sibling defenses guard the API + press-summary paths already.
        """
        from .atomic_write import atomic_write_bytes
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._get(judgment.doc_url)
            except httpx.RequestError:
                if attempt >= self._max_retries:
                    return False, None
                await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                continue
            if resp.status_code != 200:
                if attempt < self._max_retries and resp.status_code >= 500:
                    await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                    continue
                return False, None
            if not _has_valid_doc_magic(resp.content):
                magic_hex = resp.content[:4].hex() if resp.content else "empty"
                # Distinct prefix so the monitor's top-error-classes bucket
                # WAF-flip signals separately from generic 'doc-fetch-failed'.
                return False, (
                    f"doc-invalid-magic: 0x{magic_hex}, "
                    f"doc_url={judgment.doc_url}"
                )
            # Extension is magic-driven, not URL-driven — Judiciary is not
            # a reliable indicator of format (some .doc URLs serve RTF, task
            # #67). `_has_valid_doc_magic` already gated so `_extension_for_body`
            # is guaranteed non-None here.
            ext = _extension_for_body(resp.content)
            path = output_dir / f"{judgment.case.filename_stem}{ext}"
            try:
                atomic_write_bytes(path, resp.content)
                return True, None
            except OSError:
                return False, None
        return False, None

    async def _enrich_summaries(
        self, record: CaseRecord, judgment: Judgment, output_dir: Path,
    ) -> None:
        await enrich_summaries_for_case(
            self._get, self._checkpoint,
            record.court, record.year, record.number,
            judgment.case.filename_stem, output_dir, judgment.content_html,
            events=self._events,
        )

    async def _enrich_appeal_history(
        self, record: CaseRecord, judgment: Judgment, output_dir: Path,
    ) -> None:
        await enrich_appeal_history_for_case(
            self._get, self._checkpoint,
            record.court, record.year, record.number,
            judgment.case.filename_stem, output_dir, judgment.case_number,
        )
