from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .checkpoint import CheckpointDB


@dataclass
class ScrapeResult:
    downloaded: int
    failed: int


class BulkScraper:
    def __init__(
        self,
        get: Callable,
        checkpoint: CheckpointDB,
        output_dir: Path,
        formats: set[str] | None = None,
        workers: int = 1,
        max_retries: int = 3,
        limit: int | None = None,
        _backoff_base: float = 1.0,
    ):
        raise NotImplementedError

    async def enumerate(self, courts: list[str]) -> int:
        raise NotImplementedError

    async def download_all(self) -> ScrapeResult:
        raise NotImplementedError
