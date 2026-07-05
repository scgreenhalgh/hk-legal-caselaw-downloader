"""HtmlGenerator — populate .generated.html sidecars for the 234
empty-content-at-HKLII rows.

Reads `pending_html_generation` from the checkpoint, locates each
row's doc-family file on disk, hands it to doc_convert.convert_to_html,
writes the result at `{stem}.generated.html`, and updates the DB
provenance columns. Failures are recorded per-row (via
mark_html_generation_failed) so a subsequent run doesn't re-hit them
unless --force is set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .atomic_write import atomic_write_text
from .doc_convert import (
    ConversionError,
    UnsupportedSourceError,
)

_log = logging.getLogger("hklii_downloader.html_generator")


def convert_to_html(path):
    """Module-level re-export so tests can patch on this module rather
    than the source module (mirrors the pattern in enrichment.py)."""
    from .doc_convert import convert_to_html as _real
    return _real(path)


@dataclass
class GenerationResult:
    candidates: int = 0
    generated: int = 0
    failed: int = 0


_DOC_FAMILY_EXTS = (".doc", ".docx", ".rtf")


class HtmlGenerator:
    """Populate {stem}.generated.html for rows targeted by
    checkpoint.pending_html_generation.

    Idempotent: successful rows record their source ext in
    html_generated_from; failed rows record the error in
    html_generated_error. Neither state is re-visited on the next run
    unless --force (include_failed=True) is passed.
    """

    def __init__(
        self,
        checkpoint,
        output_dir,
        *,
        limit: int | None = None,
        include_failed: bool = False,
        dry_run: bool = False,
    ) -> None:
        self._checkpoint = checkpoint
        self._output_dir = Path(output_dir)
        self._limit = limit
        self._include_failed = include_failed
        self._dry_run = dry_run

    def generate_all(
        self, on_progress: Callable[[GenerationResult], None] | None = None,
    ) -> GenerationResult:
        rows = self._checkpoint.pending_html_generation(
            limit=self._limit, include_failed=self._include_failed,
        )
        result = GenerationResult(candidates=len(rows))

        if self._dry_run:
            return result

        for row in rows:
            try:
                source_ext = self._process_row(row)
                self._checkpoint.mark_html_generated(
                    row.court, row.year, row.number, source_ext=source_ext,
                )
                result.generated += 1
            except (UnsupportedSourceError, ConversionError, OSError) as e:
                _log.warning(
                    "html generation failed for %s/%s/%s: %s",
                    row.court, row.year, row.number, e,
                )
                self._checkpoint.mark_html_generation_failed(
                    row.court, row.year, row.number, error=str(e),
                )
                result.failed += 1
            except Exception as e:
                # Unexpected: still record so we don't spin on it, but
                # don't swallow silently.
                _log.exception(
                    "unexpected error generating html for %s/%s/%s",
                    row.court, row.year, row.number,
                )
                self._checkpoint.mark_html_generation_failed(
                    row.court, row.year, row.number,
                    error=f"{type(e).__name__}: {e}",
                )
                result.failed += 1
            if on_progress is not None:
                on_progress(result)

        return result

    def _process_row(self, row) -> str:
        """Convert the row's doc-family file, write sidecar, return source ext.

        Raises OSError / UnsupportedSourceError / ConversionError which
        the caller records as a per-row failure.
        """
        stem = f"{row.court}_{row.year}_{row.number}"
        case_dir = self._output_dir / row.court / str(row.year)
        source_path = self._locate_source(case_dir, stem)
        if source_path is None:
            raise OSError(
                f"no doc-family file found for {row.court}/{row.year}/"
                f"{row.number} at {case_dir} (looked for {stem}.doc/.docx/.rtf)"
            )
        html = convert_to_html(source_path)
        sidecar = case_dir / f"{stem}.generated.html"
        atomic_write_text(sidecar, html)
        return source_path.suffix

    def _locate_source(self, case_dir: Path, stem: str) -> Path | None:
        for ext in _DOC_FAMILY_EXTS:
            p = case_dir / f"{stem}{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None
