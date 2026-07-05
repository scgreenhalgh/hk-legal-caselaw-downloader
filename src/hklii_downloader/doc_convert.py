"""doc → html conversion — stub. Real impl in the feat pair."""
from __future__ import annotations

import subprocess
from pathlib import Path


class ConversionError(RuntimeError):
    pass


class UnsupportedSourceError(RuntimeError):
    pass


def _find_soffice() -> str | None:
    return None


def convert_to_html(path: Path) -> str:
    raise NotImplementedError
