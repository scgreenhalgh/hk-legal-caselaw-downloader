"""Citations graph scraper — pulls getcasenoteup for every downloaded
case and stores forward edges in the citations table.

Design source: docs/citation-graph-design.md.

Wire contract:

  GET /api/getcasenoteup?abbr={court}&year={Y}&num={N}
    → JSON array. Each entry has {neutral, path, db, date,
       citation_frequency, parallel, cases[]}.
    → path shape: /{en|tc}/cases/{court}/{year}/{num}
    → Returns [] for zero-citation cases AND nonexistent cases AND bad
       params (see membership guard note below).
    → The `lang` query param is silently IGNORED — one call returns
       citers from both corpora. Do not send lang.

Membership guard: only enumerate downloaded rows. Never call
getcasenoteup for a case we can't validate, so [] is unambiguously
"zero citations" not "typo in params".

On disk:
  output/{court}/{year}/{stem}.noteup.json    raw response
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from .atomic_write import atomic_write_text

_log = logging.getLogger("hklii_downloader.citations")

_BASE_URL = "https://www.hklii.hk"
_PATH_RE = re.compile(r"^/(en|tc)/cases/([a-z]+)/(\d{4})/(\d+)/?")


class NoteupFetchError(RuntimeError):
    """Wire failure (non-200, non-JSON body, unexpected shape)."""


@dataclass
class NoteupParsed:
    edges: list[tuple[str, str, str, int | None, int]] = field(
        default_factory=list
    )
    parallel_cites: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class NoteupRunResult:
    downloaded: int = 0
    failed: int = 0


def getcasenoteup_url(court: str, year: int, num: int) -> str:
    """Build getcasenoteup URL. Deliberately omits `lang` param — HKLII
    ignores it and returns mixed-lang citers regardless (verified via
    MD5-identical bodies during API probe, 2026-07-06)."""
    return (
        f"{_BASE_URL}/api/getcasenoteup"
        f"?abbr={court}&year={year}&num={num}"
    )


def _case_key(court: str, year: int, num: int) -> str:
    return f"{court}/{year}/{num}"


def parse_noteup_response(
    entries: list[dict], target: str,
) -> NoteupParsed:
    """Turn HKLII's getcasenoteup array into (from_key, to_key,
    citer_lang, citer_freq, position) edge tuples + a list of
    (case_key, parallel_cite) pairs.

    target is the case_key of whichever case we queried — appears as
    to_key on every edge.
    """
    parsed = NoteupParsed()
    for position, entry in enumerate(entries):
        # `.get("path", "")` returns None (not "") when path is present
        # but null — HKLII's contract is stable today but a null-path
        # entry would raise TypeError inside _PATH_RE.match. Belt-and-
        # braces coerce to empty string.
        if not isinstance(entry, dict):
            continue
        path = entry.get("path") or ""
        m = _PATH_RE.match(path)
        if not m:
            continue
        citer_lang = m.group(1)
        citer_court = m.group(2)
        citer_year = int(m.group(3))
        citer_num = int(m.group(4))
        from_key = _case_key(citer_court, citer_year, citer_num)
        citer_freq = entry.get("citation_frequency")
        parsed.edges.append(
            (from_key, target, citer_lang, citer_freq, position)
        )
        for pc in entry.get("parallel", []) or []:
            if pc:
                parsed.parallel_cites.append((from_key, pc))
    return parsed


def save_noteup_local(
    output_dir: Path,
    court: str, year: int, num: int, raw: list,
) -> None:
    """Persist the raw response as {stem}.noteup.json for auditability +
    to allow rebuilding the SQLite state from disk without re-hitting
    the API."""
    stem = f"{court}_{year}_{num}"
    d = Path(output_dir) / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        d / f"{stem}.noteup.json",
        json.dumps(raw, ensure_ascii=False),
    )


async def fetch_noteup_for_case(
    get: Callable, court: str, year: int, num: int,
) -> tuple[list[tuple], list[tuple[str, str]], list]:
    """Fetch one case's noteup response. Returns (edges, parallel_cites,
    raw_json). Raises NoteupFetchError on non-200 or malformed body."""
    url = getcasenoteup_url(court=court, year=year, num=num)
    resp = await get(url)
    if resp.status_code != 200:
        raise NoteupFetchError(
            f"getcasenoteup HTTP {resp.status_code} for {court}/{year}/{num}"
        )
    try:
        raw = resp.json()
    except Exception as e:
        raise NoteupFetchError(
            f"getcasenoteup non-JSON body for {court}/{year}/{num}: "
            f"{type(e).__name__}: {e}"
        ) from e
    if not isinstance(raw, list):
        raise NoteupFetchError(
            f"getcasenoteup returned {type(raw).__name__}, expected list, "
            f"for {court}/{year}/{num}"
        )
    target = _case_key(court, year, num)
    parsed = parse_noteup_response(raw, target=target)
    return parsed.edges, parsed.parallel_cites, raw


class NoteupRunner:
    """Two-phase runner:
      enumerate_pending — upsert one noteup_fetches row per downloaded case
      fetch_pending    — drain via N async workers; save sidecar + insert
                          edges + mark ok/failed
    """

    def __init__(
        self,
        get: Callable | None,
        checkpoint,
        output_dir: Path,
        workers: int = 4,
        limit: int | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._workers = max(1, workers)
        self._limit = limit

    def enumerate_pending(self) -> int:
        """Upsert a noteup_fetches row per status='downloaded' case.
        Idempotent — INSERT OR IGNORE. Returns count seen."""
        rows = self._checkpoint._conn.execute(
            "SELECT court, year, number FROM cases WHERE status='downloaded'"
        ).fetchall()
        for court, year, num in rows:
            self._checkpoint.upsert_noteup_fetch(court, year, num)
        return len(rows)

    async def fetch_pending(
        self,
        on_progress: Callable[[NoteupRunResult], None] | None = None,
    ) -> NoteupRunResult:
        # Recover rows stuck at 'in_progress' from a prior worker crash.
        self._checkpoint.release_in_progress_noteup()
        result = NoteupRunResult()
        counter_lock = asyncio.Lock()
        remaining = {"n": self._limit if self._limit is not None else -1}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    rec = self._checkpoint.claim_pending_noteup()
                    if rec is None:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                try:
                    edges, parallels, raw = await fetch_noteup_for_case(
                        get=self._get,
                        court=rec.court, year=rec.year, num=rec.number,
                    )
                    save_noteup_local(
                        output_dir=self._output_dir,
                        court=rec.court, year=rec.year, num=rec.number,
                        raw=raw,
                    )
                    now = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    if edges:
                        self._checkpoint.insert_citation_edges(
                            edges, first_seen=now,
                        )
                    if parallels:
                        # Group parallels by from_key for bulk insert per key
                        by_key: dict[str, list[str]] = {}
                        for from_key, pc in parallels:
                            by_key.setdefault(from_key, []).append(pc)
                        for k, cites in by_key.items():
                            self._checkpoint.insert_parallel_cites(k, cites)
                    self._checkpoint.mark_noteup_ok(
                        court=rec.court, year=rec.year, number=rec.number,
                        edge_count=len(edges), fetched_at=now,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except NoteupFetchError as e:
                    _log.warning(
                        "noteup fetch failed for %s/%s/%s: %s",
                        rec.court, rec.year, rec.number, e,
                    )
                    self._checkpoint.mark_noteup_failed(
                        court=rec.court, year=rec.year, number=rec.number,
                        error=str(e),
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as e:
                    _log.warning(
                        "noteup transport failure %s/%s/%s: %s: %s",
                        rec.court, rec.year, rec.number,
                        type(e).__name__, e,
                    )
                    self._checkpoint.mark_noteup_failed(
                        court=rec.court, year=rec.year, number=rec.number,
                        error=f"{type(e).__name__}: {e}",
                    )
                    async with counter_lock:
                        result.failed += 1
                except Exception as e:  # noqa: BLE001
                    # Catches sqlite3.Error / IntegrityError from the
                    # local DB inserts (insert_citation_edges,
                    # insert_parallel_cites, mark_noteup_ok) so one bad
                    # row can't propagate out of asyncio.gather and
                    # terminate the whole scrape. Every sibling row
                    # still gets processed.
                    _log.warning(
                        "noteup worker failure %s/%s/%s: %s: %s",
                        rec.court, rec.year, rec.number,
                        type(e).__name__, e,
                    )
                    try:
                        self._checkpoint.mark_noteup_failed(
                            court=rec.court, year=rec.year, number=rec.number,
                            error=f"{type(e).__name__}: {e}",
                        )
                    except Exception:  # noqa: BLE001
                        pass  # best-effort — don't nested-crash
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return result
