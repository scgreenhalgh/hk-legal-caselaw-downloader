from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

_BASE_URL = "https://www.hklii.hk"
_PATH_RE = re.compile(r"/(?:en|tc)/cases/([a-z]+)/(\d{4})/(\d+)")


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


async def enumerate_court(
    court: str,
    get: Callable,
    lang: str = "en",
    items_per_page: int = 10_000,
    on_page: Callable | None = None,
) -> list[CaseEntry]:
    params = urlencode({
        "caseDb": court,
        "lang": lang,
        "itemsPerPage": items_per_page,
        "page": 1,
    })
    resp = await get(f"{_BASE_URL}/api/getcasefiles?{params}")
    data = resp.json()

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
        resp = await get(f"{_BASE_URL}/api/getcasefiles?{params}")
        page_data = resp.json()
        page_entries = [parse_case_entry(j, court) for j in page_data.get("judgments", [])]
        entries.extend(page_entries)

        if on_page:
            on_page(page, total_pages, len(page_entries))

    return entries


def extract_press_summary_url(html: str) -> str | None:
    raise NotImplementedError
