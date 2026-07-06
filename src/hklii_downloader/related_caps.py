"""getrelatedcaps scraper — HKLII ord → reg mapping.

Design source: docs/citation-graph-design.md §3.2.

Wire contract:

  GET /api/getrelatedcaps?num_int={N}&lang={en|tc}&abbr={ord|reg}
    → JSON array. Each entry has {title, num, path}.
    → abbr=ord returns ONE record — the ordinance itself (self-lookup).
    → abbr=reg returns all subsidiary regs with `N{letter}` naming.
    → num_int MUST be a pure integer — alpha-suffix (32A) → HTTP 500.
    → Nonexistent cap → [].

On disk:
  output/legis/{abbr}/{cap}/relatedcaps_{lang}.json    raw response
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

from .atomic_write import atomic_write_text

_log = logging.getLogger("hklii_downloader.related_caps")

_BASE_URL = "https://www.hklii.hk"
_ALPHA_SUFFIX_RE = re.compile(r"^\d+[A-Z]+$", re.IGNORECASE)


class RelatedcapsFetchError(RuntimeError):
    """Wire failure (non-200 / non-JSON / unexpected shape)."""


@dataclass
class RelatedcapsRunResult:
    downloaded: int = 0
    failed: int = 0


def getrelatedcaps_url(cap_number: str, abbr: str, lang: str) -> str:
    qs = urlencode({
        "num_int": cap_number,
        "lang": lang,
        "abbr": abbr,
    })
    return f"{_BASE_URL}/api/getrelatedcaps?{qs}"


def is_alpha_suffix_cap(cap: str) -> bool:
    """True if cap looks like `32A`, `622J`, etc. — HKLII's num_int
    parameter can't handle these and returns a raw 500."""
    return bool(_ALPHA_SUFFIX_RE.match(cap))


def parse_relatedcaps_response(
    entries: list[dict], parent_cap: str, abbr: str, lang: str,
) -> list[tuple[str, str, str, str]]:
    """Turn the API's [{title, num, path}] array into
    (parent_cap, child_cap, lang, title) edge tuples.

    abbr='ord' is a degenerate self-lookup — the one returned record IS
    the ordinance being queried, so no true edges. Return empty list.
    """
    if abbr == "ord":
        return []
    edges = []
    for e in entries:
        num = e.get("num") or ""
        title = e.get("title") or ""
        if not num:
            continue
        edges.append((parent_cap, num, lang, title))
    return edges


def _save_local(
    output_dir: Path, cap_number: str, abbr: str, lang: str, raw: list,
) -> None:
    d = Path(output_dir) / "legis" / abbr / cap_number
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        d / f"relatedcaps_{lang}.json",
        json.dumps(raw, ensure_ascii=False),
    )


async def fetch_relatedcaps(
    get: Callable, cap_number: str, abbr: str, lang: str,
) -> tuple[list[tuple[str, str, str, str]], list]:
    """Fetch one (cap, abbr, lang). Returns (edges, raw_json).
    Raises RelatedcapsFetchError on non-200 or malformed body."""
    url = getrelatedcaps_url(cap_number=cap_number, abbr=abbr, lang=lang)
    resp = await get(url)
    if resp.status_code != 200:
        raise RelatedcapsFetchError(
            f"getrelatedcaps HTTP {resp.status_code} "
            f"for cap={cap_number} abbr={abbr} lang={lang}"
        )
    try:
        raw = resp.json()
    except Exception as e:
        raise RelatedcapsFetchError(
            f"getrelatedcaps non-JSON body for cap={cap_number} "
            f"abbr={abbr} lang={lang}: {type(e).__name__}: {e}"
        ) from e
    if not isinstance(raw, list):
        raise RelatedcapsFetchError(
            f"getrelatedcaps returned {type(raw).__name__}, expected list"
        )
    edges = parse_relatedcaps_response(
        raw, parent_cap=cap_number, abbr=abbr, lang=lang,
    )
    return edges, raw


class RelatedCapsRunner:
    """Two-phase runner. Enumeration iterates over the cap range × abbr ×
    lang product, upserting one relatedcap_fetches row per combination.
    Fetch drains via async workers."""

    def __init__(
        self,
        get: Callable | None,
        checkpoint,
        output_dir: Path,
        cap_range: tuple[int, int] = (1, 1200),
        abbrs: tuple[str, ...] = ("ord", "reg"),
        langs: tuple[str, ...] = ("en", "tc"),
        workers: int = 4,
        limit: int | None = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._cap_range = cap_range
        self._abbrs = abbrs
        self._langs = langs
        self._workers = max(1, workers)
        self._limit = limit

    def enumerate_pending(self) -> int:
        """Upsert one relatedcap_fetches row per (cap, abbr, lang) in the
        configured product. Skips alpha-suffix cap numbers because the
        API returns 500 on them."""
        lo, hi = self._cap_range
        upserted = 0
        for cap in range(lo, hi + 1):
            cap_str = str(cap)
            if is_alpha_suffix_cap(cap_str):
                continue
            for abbr in self._abbrs:
                for lang in self._langs:
                    self._checkpoint.upsert_relatedcap_fetch(
                        cap_str, abbr, lang,
                    )
                    upserted += 1
        return upserted

    async def fetch_pending(
        self,
        on_progress: Callable[[RelatedcapsRunResult], None] | None = None,
    ) -> RelatedcapsRunResult:
        # Recover rows stuck at 'in_progress' from a prior worker crash.
        self._checkpoint.release_in_progress_relatedcap()
        result = RelatedcapsRunResult()
        counter_lock = asyncio.Lock()
        remaining = {"n": self._limit if self._limit is not None else -1}

        async def worker() -> None:
            while True:
                async with counter_lock:
                    if remaining["n"] == 0:
                        return
                    rec = self._checkpoint.claim_pending_relatedcap()
                    if rec is None:
                        return
                    if remaining["n"] > 0:
                        remaining["n"] -= 1

                try:
                    edges, raw = await fetch_relatedcaps(
                        get=self._get, cap_number=rec.cap_number,
                        abbr=rec.abbr, lang=rec.lang,
                    )
                    _save_local(
                        self._output_dir, rec.cap_number,
                        rec.abbr, rec.lang, raw,
                    )
                    now = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    if edges:
                        self._checkpoint.insert_ord_reg_edges(
                            edges, first_seen=now,
                        )
                    self._checkpoint.mark_relatedcap_ok(
                        cap_number=rec.cap_number, abbr=rec.abbr,
                        lang=rec.lang,
                        edge_count=len(edges), fetched_at=now,
                    )
                    async with counter_lock:
                        result.downloaded += 1
                except RelatedcapsFetchError as e:
                    _log.warning(
                        "relatedcaps fetch failed for cap=%s abbr=%s "
                        "lang=%s: %s",
                        rec.cap_number, rec.abbr, rec.lang, e,
                    )
                    self._checkpoint.mark_relatedcap_failed(
                        cap_number=rec.cap_number, abbr=rec.abbr,
                        lang=rec.lang, error=str(e),
                    )
                    async with counter_lock:
                        result.failed += 1
                except (httpx.RequestError, OSError) as e:
                    _log.warning(
                        "relatedcaps transport failure cap=%s: %s: %s",
                        rec.cap_number, type(e).__name__, e,
                    )
                    self._checkpoint.mark_relatedcap_failed(
                        cap_number=rec.cap_number, abbr=rec.abbr,
                        lang=rec.lang,
                        error=f"{type(e).__name__}: {e}",
                    )
                    async with counter_lock:
                        result.failed += 1

                if on_progress is not None:
                    on_progress(result)

        await asyncio.gather(*[worker() for _ in range(self._workers)])
        return result
