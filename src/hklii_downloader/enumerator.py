from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


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
        return ""


def parse_case_entry(data: dict, court: str) -> CaseEntry:
    return CaseEntry(court=court, year=0, number=0, neutral="", title="", date="")


async def enumerate_court(
    court: str,
    get,
    lang: str = "en",
    items_per_page: int = 10_000,
    on_page=None,
) -> list[CaseEntry]:
    return []
