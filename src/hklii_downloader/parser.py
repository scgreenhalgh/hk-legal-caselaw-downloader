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

# /api/getappealhistory carries the caseno in two shapes:
#   (a) Neutral-citation style: "HKCFA 5/2024" — court prefix is the
#       lowercased URL slug directly.
#   (b) Act-based compact style: "FACC3/2025", "HCMP2265/2025",
#       "CACV45/2024" — court prefix is an act code; we resolve to the
#       URL slug via COURT_PREFIX_MAP. Task #62.
# Both shapes match _APPEAL_CASENO_PATTERN; \s* accepts zero or more
# whitespace so the compact form parses.
_APPEAL_CASENO_PATTERN = re.compile(r"^([A-Za-z]+)\s*\d+/(\d{4})$")

# Act-prefix → HKLII URL slug. Prefixes are the alphabetic segment of
# the caseno; e.g. HCMP001234/2025 → "hcmp" → hkcfi. Uppercase-matched
# for stability across HKLII wire variations.
COURT_PREFIX_MAP: dict[str, str] = {
    # Court of First Instance (High Court, first instance)
    "HCMP": "hkcfi", "HCA": "hkcfi", "HCB": "hkcfi", "HCPI": "hkcfi",
    "HCCC": "hkcfi", "HCAJ": "hkcfi", "HCAL": "hkcfi", "HCCT": "hkcfi",
    "HCCW": "hkcfi", "HCIA": "hkcfi", "HCIP": "hkcfi", "HCMA": "hkcfi",
    "HCMC": "hkcfi", "HCPD": "hkcfi", "HCPT": "hkcfi", "HCSD": "hkcfi",
    "HCZZ": "hkcfi",
    # Court of Appeal
    "CACV": "hkca", "CACC": "hkca", "CAAR": "hkca",
    "CAAG": "hkca", "CAAM": "hkca", "CAAP": "hkca", "CAQL": "hkca",
    "CAMP": "hkca",
    # Court of Final Appeal
    "FACC": "hkcfa", "FACV": "hkcfa", "FAMV": "hkcfa", "FAMP": "hkcfa",
    # District Court
    "DCCC": "hkdc", "DCCJ": "hkdc",
    "DCCV": "hkdc", "DCEC": "hkdc", "DCMP": "hkdc", "DCPI": "hkdc",
    "DCPA": "hkdc", "DCSA": "hkdc", "DCTC": "hkdc", "DCZZ": "hkdc",
}


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
            prefix, year = m.groups()
            # Act-prefix carries the court identity for compact-shape
            # casenos (HCMP2265/2025). Look up first; fall back to using
            # the prefix directly (lowercase) for neutral-citation shape
            # (HKCFA 5/2024 → "hkcfa"). Unknown prefixes with no self-slug
            # semantics fall through to homepage.
            slug = COURT_PREFIX_MAP.get(prefix.upper())
            if slug is None and prefix.lower().startswith("hk"):
                slug = prefix.lower()
            if slug:
                return f"{BASE_URL}/{lang}/cases/{slug}/{year}/"
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
