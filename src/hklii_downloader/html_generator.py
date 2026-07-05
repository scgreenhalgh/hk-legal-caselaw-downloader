"""HtmlGenerator — stub. Full impl in the feat pair."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GenerationResult:
    candidates: int = 0
    generated: int = 0
    failed: int = 0


def convert_to_html(path):
    """Re-exported here so tests can patch this module's binding
    (patch('hklii_downloader.html_generator.convert_to_html', ...))."""
    from .doc_convert import convert_to_html as _real
    return _real(path)


class HtmlGenerator:
    def __init__(self, checkpoint, output_dir, *,
                 limit=None, include_failed=False, dry_run=False):
        self._checkpoint = checkpoint
        self._output_dir = output_dir
        self._limit = limit
        self._include_failed = include_failed
        self._dry_run = dry_run

    def generate_all(self):
        raise NotImplementedError
