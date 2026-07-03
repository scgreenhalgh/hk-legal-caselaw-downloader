from __future__ import annotations

import asyncio
import json
import math
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
) -> list[CaseEntry]:
    params = urlencode({
        "caseDb": court,
        "lang": lang,
        "itemsPerPage": items_per_page,
        "page": 1,
    })
    data = await _get_json_with_retry(
        get, f"{_BASE_URL}/api/getcasefiles?{params}", max_retries, backoff_base,
    )

    total = data.get("totalfiles", 0)
    if total == 0:
        return []

    total_pages = math.ceil(total / items_per_page)
    entries = [parse_case_entry(j, court) for j in data.get("judgments", [])]

    if on_page:
        on_page(1, total_pages, len(entries))

    for page in range(2, total_pages + 1):
        params = urlencode({
            "caseDb": court,
            "lang": lang,
            "itemsPerPage": items_per_page,
            "page": page,
        })
        page_data = await _get_json_with_retry(
            get, f"{_BASE_URL}/api/getcasefiles?{params}", max_retries, backoff_base,
        )
        page_entries = [parse_case_entry(j, court) for j in page_data.get("judgments", [])]
        entries.extend(page_entries)

        if on_page:
            on_page(page, total_pages, len(page_entries))

    return entries


_PRESS_SUMMARY_RE = re.compile(
    r'<a\s[^>]*href="([^"]+)"[^>]*>\s*Press\s+Summary\s*\((\w+)\)\s*</a>',
    re.DOTALL,
)


def extract_press_summary_url(html: str) -> str | None:
    matches = _PRESS_SUMMARY_RE.findall(html)
    if not matches:
        return None
    for url, lang in matches:
        if lang == "English":
            return url
    return matches[0][0]
