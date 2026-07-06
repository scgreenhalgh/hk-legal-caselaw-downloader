"""getrelatedcaps scraper — stub. Real impl in the paired feat commit."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RelatedcapsFetchError(RuntimeError):
    pass


@dataclass
class RelatedcapsRunResult:
    downloaded: int = 0
    failed: int = 0


def getrelatedcaps_url(cap_number, abbr, lang):
    raise NotImplementedError


def is_alpha_suffix_cap(cap):
    raise NotImplementedError


def parse_relatedcaps_response(entries, parent_cap, abbr, lang):
    raise NotImplementedError


async def fetch_relatedcaps(get, cap_number, abbr, lang):
    raise NotImplementedError


class RelatedCapsRunner:
    def __init__(self, get, checkpoint, output_dir,
                 cap_range=(1, 1200), abbrs=("ord", "reg"),
                 langs=("en", "tc"), workers=4, limit=None):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._cap_range = cap_range
        self._abbrs = abbrs
        self._langs = langs
        self._workers = workers
        self._limit = limit

    def enumerate_pending(self):
        raise NotImplementedError

    async def fetch_pending(self, on_progress=None):
        raise NotImplementedError
