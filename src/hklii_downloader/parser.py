from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?hklii\.hk/(en|tc)/cases/([a-z]+)/(\d{4})/(\d+)/?$"
)

BASE_URL = "https://www.hklii.hk"

_CASE_PATH_PATTERN = re.compile(r"^/(en|tc)/cases/([a-z]+)/(\d{4})(?:/\d+/?)?$")

# Neutral-citation-style caseno used by the /api/getappealhistory endpoint:
# <COURT> <NUM>/<YEAR> e.g. "HKCFA 5/2024". Only this format maps cleanly
# back to the URL court slug (lowercased); other on-wire formats like
# "FACC3/2025" do not reveal the slug and fall through to homepage.
_APPEAL_CASENO_PATTERN = re.compile(r"^([A-Za-z]+)\s+\d+/(\d{4})$")


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


def referer_for(url: str) -> str:
    """Return a plausible SPA Referer for an HKLII request URL.

    Real Chrome sets Referer to the URL that fired the XHR. A hardcoded
    homepage Referer on every /api/* call is a one-line log-analysis signal.
    Falls back to the homepage for anything we can't derive safely.
    """
    parsed = urlparse(url)
    if parsed.netloc != "www.hklii.hk":
        return f"{BASE_URL}/"

    if parsed.path == "/api/getjudgment":
        qs = parse_qs(parsed.query)
        lang = qs.get("lang", [""])[0]
        court = qs.get("abbr", [""])[0]
        year = qs.get("year", [""])[0]
        if lang and court and year:
            return f"{BASE_URL}/{lang}/cases/{court}/{year}/"
        return f"{BASE_URL}/"

    if parsed.path == "/api/getappealhistory":
        qs = parse_qs(parsed.query)
        caseno = qs.get("caseno", [""])[0]
        lang = qs.get("lang", ["en"])[0]
        if lang not in ("en", "tc"):
            lang = "en"
        m = _APPEAL_CASENO_PATTERN.match(caseno)
        if m:
            court, year = m.groups()
            return f"{BASE_URL}/{lang}/cases/{court.lower()}/{year}/"
        return f"{BASE_URL}/"

    if parsed.path == "/api/getcasefiles":
        qs = parse_qs(parsed.query)
        court = qs.get("caseDb", [""])[0]
        lang = qs.get("lang", [""])[0]
        if lang and court:
            return f"{BASE_URL}/{lang}/cases/{court}/"
        return f"{BASE_URL}/"

    m = _CASE_PATH_PATTERN.match(parsed.path)
    if m:
        lang, court, year = m.groups()
        return f"{BASE_URL}/{lang}/cases/{court}/{year}/"

    return f"{BASE_URL}/"


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
