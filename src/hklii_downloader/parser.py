from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from bs4 import BeautifulSoup

_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?hklii\.hk/(en|tc)/cases/([a-z]+)/(\d{4})/(\d+)/?$"
)

BASE_URL = "https://www.hklii.hk"


@dataclass(frozen=True)
class HKLIICase:
    lang: str
    court: str
    year: int
    number: int

    @property
    def api_url(self) -> str:
        params = urlencode({
            "lang": self.lang,
            "abbr": self.court,
            "year": self.year,
            "num": self.number,
        })
        return f"{BASE_URL}/api/getjudgment?{params}"

    @property
    def filename_stem(self) -> str:
        return f"{self.court}_{self.year}_{self.number}"


def parse_hklii_url(url: str) -> HKLIICase:
    m = _URL_PATTERN.match(url)
    if not m:
        raise ValueError(f"Not a valid HKLII case URL: {url}")
    lang, court, year, number = m.groups()
    return HKLIICase(lang=lang, court=court, year=int(year), number=int(number))


_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "table", "thead", "tbody", "tfoot", "td", "th",
}


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "link", "meta"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
