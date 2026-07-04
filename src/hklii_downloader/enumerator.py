from __future__ import annotations

import asyncio
import json
import math
import random
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

import httpx

_BASE_URL = "https://www.hklii.hk"
_PATH_RE = re.compile(r"/(?:en|tc)/cases/([a-z]+)/(\d{4})/(\d+)")
_PERMANENT_STATUSES = {404, 410}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class CaseEntry:
    court: str
    year: int
    number: int
    neutral: str
    title: str
    date: str

    @property
    def api_url(self) -> str:
        params = urlencode({
            "lang": "en",
            "abbr": self.court,
            "year": self.year,
            "num": self.number,
        })
        return f"{_BASE_URL}/api/getjudgment?{params}"


def parse_case_entry(data: dict, court: str) -> CaseEntry:
    path = data.get("path", "")
    m = _PATH_RE.search(path)
    year = int(m.group(2)) if m else 0
    number = int(m.group(3)) if m else 0

    cases_list = data.get("cases", [])
    title = cases_list[0].get("title", "") if cases_list else ""

    return CaseEntry(
        court=court,
        year=year,
        number=number,
        neutral=data.get("neutral", ""),
        title=title,
        date=data.get("date", ""),
    )


def _jittered_backoff(base: float, attempt: int) -> float:
    # Stub — real jitter lands in the next commit.
    return base * (2 ** attempt)


async def _get_json_with_retry(
    get: Callable,
    url: str,
    max_retries: int,
    backoff_base: float,
) -> dict:
    for attempt in range(max_retries + 1):
        try:
            resp = await get(url)
        except httpx.RequestError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(backoff_base * (2 ** attempt))
            continue

        status = resp.status_code
        if status in _PERMANENT_STATUSES:
            resp.raise_for_status()
        if status in _RETRYABLE_STATUSES or status >= 500:
            if attempt >= max_retries:
                resp.raise_for_status()
            await asyncio.sleep(backoff_base * (2 ** attempt))
            continue

        try:
            return resp.json()
        except json.JSONDecodeError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(backoff_base * (2 ** attempt))
            continue


async def enumerate_court(
    court: str,
    get: Callable,
    lang: str = "en",
    items_per_page: int = 10_000,
    on_page: Callable | None = None,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    save_response_to=None,
) -> list[CaseEntry]:
    from pathlib import Path
    import time
    save_dir: Path | None = None
    ts = int(time.time())
    if save_response_to is not None:
        save_dir = Path(save_response_to) / f"{court}_{lang}"

    async def _fetch_and_maybe_save(page_num: int) -> dict:
        params = urlencode({
            "caseDb": court,
            "lang": lang,
            "itemsPerPage": items_per_page,
            "page": page_num,
        })
        data = await _get_json_with_retry(
            get, f"{_BASE_URL}/api/getcasefiles?{params}",
            max_retries, backoff_base,
        )
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            out = save_dir / f"{ts}_page{page_num:04d}.json"
            out.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8",
            )
        return data

    data = await _fetch_and_maybe_save(1)

    total = data.get("totalfiles", 0)
    if total == 0:
        return []

    total_pages = math.ceil(total / items_per_page)
    entries = [parse_case_entry(j, court) for j in data.get("judgments", [])]

    if on_page:
        on_page(1, total_pages, len(entries))

    for page in range(2, total_pages + 1):
        page_data = await _fetch_and_maybe_save(page)
        page_entries = [parse_case_entry(j, court) for j in page_data.get("judgments", [])]
        entries.extend(page_entries)

        if on_page:
            on_page(page, total_pages, len(page_entries))

    return entries


_PRESS_SUMMARY_TEXT_RE = re.compile(
    r"press\s+summary\s*\(([^)]+)\)", re.IGNORECASE,
)

_LANG_CANON = {
    "english": "English", "en": "English",
    "chinese": "Chinese", "zh": "Chinese",
    "zh-hant": "Chinese", "zh-hans": "Chinese",
    "traditional chinese": "Chinese", "simplified chinese": "Chinese",
}


def extract_press_summary_urls(html: str) -> dict[str, str]:
    """Return {lang: url} for every Press Summary anchor found.

    Uses BeautifulSoup so wrapping tags, single-quoted hrefs, case
    variations, and extra attributes don't silently break extraction.
    """
    if not html:
        return {}
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    result: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        m = _PRESS_SUMMARY_TEXT_RE.search(text)
        if not m:
            continue
        lang = m.group(1).strip()
        canonical = _LANG_CANON.get(lang.lower(), lang)
        result.setdefault(canonical, a["href"])
    return result


def extract_press_summary_url(html: str) -> str | None:
    """Return one URL, preferring English. Kept for backwards-compat."""
    urls = extract_press_summary_urls(html)
    return urls.get("English") or next(iter(urls.values()), None)
