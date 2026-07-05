"""Case translation backfill — stub. Full impl in the feat commit."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class TranslationTarget:
    court: str
    year: int
    number: int


@dataclass
class TranslationResult:
    downloaded: int = 0
    failed: int = 0


def find_translation_targets(output_dir):
    raise NotImplementedError


def save_translation_local(judgment, output_dir):
    raise NotImplementedError


class CaseTranslationRunner:
    def __init__(self, get, output_dir, workers=4, limit=None):
        self._get = get
        self._output_dir = Path(output_dir)
        self._workers = workers
        self._limit = limit

    async def run(self):
        raise NotImplementedError
