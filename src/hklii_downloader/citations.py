"""Citations graph scraper — stub. Full impl in the feat commit."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class NoteupFetchError(RuntimeError):
    pass


@dataclass
class NoteupParsed:
    edges: list[tuple] = field(default_factory=list)
    parallel_cites: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class NoteupRunResult:
    downloaded: int = 0
    failed: int = 0


def getcasenoteup_url(court, year, num):
    raise NotImplementedError


def parse_noteup_response(entries, target):
    raise NotImplementedError


def save_noteup_local(output_dir, court, year, num, raw):
    raise NotImplementedError


async def fetch_noteup_for_case(get, court, year, num):
    raise NotImplementedError


class NoteupRunner:
    def __init__(self, get, checkpoint, output_dir,
                 workers=4, limit=None):
        self._get = get
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._workers = workers
        self._limit = limit

    def enumerate_pending(self):
        raise NotImplementedError

    async def fetch_pending(self, on_progress=None):
        raise NotImplementedError
