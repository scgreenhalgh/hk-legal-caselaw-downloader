"""HOPT scraper — stub. Full impl in the feat pair (task #89)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class HoptFetchError(RuntimeError):
    pass


@dataclass
class HoptEntry:
    year: int
    num: int
    title: str
    neutral: str | None = None
    date: str | None = None


@dataclass
class HoptListing:
    total: int
    entries: list[HoptEntry] = field(default_factory=list)


@dataclass
class HoptRunResult:
    downloaded: int = 0
    failed: int = 0


def gethoptfiles_url(abbr, lang, page, items_per_page, sort="-date"):
    raise NotImplementedError


def gettreaty_url(abbr, year, num, lang):
    raise NotImplementedError


def wire_abbr(abbr):
    raise NotImplementedError


def parse_hopt_files_response(body):
    raise NotImplementedError


def save_hopt_local(output_dir, abbr, year, num, lang, doc):
    raise NotImplementedError


class HoptRunner:
    def __init__(self, get, checkpoint, output_dir, abbrs, langs,
                 workers=4, limit=None):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._abbrs = abbrs
        self._langs = langs
        self._workers = workers
        self._limit = limit

    async def enumerate_all(self):
        raise NotImplementedError

    async def fetch_pending(self, on_progress=None):
        raise NotImplementedError
