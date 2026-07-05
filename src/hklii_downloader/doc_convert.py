"""doc → html conversion for the 234 empty-content-at-HKLII rows.

Dispatches by magic bytes on the input file — the same magic-driven
routing scraper._extension_for_body uses for write-time extension
choice. pandoc handles OOXML (.docx) and RTF (.rtf) directly. Plain
OLE .doc (Word 97+) isn't in pandoc's input formats, so we trampoline
via `soffice --headless --convert-to docx` and hand the resulting
docx back to pandoc; if libreoffice isn't installed we raise
UnsupportedSourceError with a clear install hint rather than silently
skipping.

Sits alongside scraper (scraper.py:67-75) as the read-time counterpart
of the write-time extension chooser — same magic table, different
direction. Same conservative default: if the magic isn't in our table
we refuse rather than guess.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class ConversionError(RuntimeError):
    """The conversion tool ran but failed to produce html."""


class UnsupportedSourceError(RuntimeError):
    """The input file's magic bytes don't match a supported format
    (or the format needs a helper tool we don't have)."""


# Magic → (pandoc -f value, path/None). None means "needs soffice trampoline".
_MAGIC_TO_PANDOC_FORMAT: dict[bytes, str | None] = {
    b"PK\x03\x04": "docx",
    b"{\\rt": "rtf",
    b"\xd0\xcf\x11\xe0": None,           # OLE .doc — via soffice
    b"\xdb\xa5\x2d\x00": None,           # pre-OLE Word 6.0 / 95 — via soffice
}


def _find_soffice() -> str | None:
    """Return an absolute path to soffice / libreoffice, or None if
    neither is on PATH. Isolated so tests can monkeypatch."""
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _read_magic(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(4)


def convert_to_html(path: Path) -> str:
    """Convert the file at `path` to an html string.

    Extension is ignored — dispatch is purely on the first 4 magic
    bytes. Raises UnsupportedSourceError if we don't know how to handle
    the magic (or if the needed helper tool isn't installed), and
    ConversionError if the tool ran but failed.
    """
    magic = _read_magic(path)
    if magic not in _MAGIC_TO_PANDOC_FORMAT:
        raise UnsupportedSourceError(
            f"unknown doc-family magic {magic.hex()!r} for {path}; "
            "supported: docx (PK\\x03\\x04), rtf ({\\rt), "
            "OLE .doc (\\xd0\\xcf\\x11\\xe0)"
        )

    pandoc_format = _MAGIC_TO_PANDOC_FORMAT[magic]
    if pandoc_format is not None:
        return _convert_via_pandoc(path, pandoc_format)

    # OLE .doc trampoline: soffice → docx → pandoc.
    soffice = _find_soffice()
    if soffice is None:
        raise UnsupportedSourceError(
            f"{path} is OLE .doc but neither `soffice` nor "
            "`libreoffice` is on PATH; install LibreOffice "
            "(brew install --cask libreoffice) to enable this format"
        )
    with tempfile.TemporaryDirectory() as td:
        outdir = Path(td)
        result = subprocess.run(
            [
                soffice, "--headless", "--convert-to", "docx",
                "--outdir", str(outdir), str(path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise ConversionError(
                f"soffice failed on {path}: {result.stderr.strip()}"
            )
        trampolined = outdir / f"{path.stem}.docx"
        if not trampolined.exists():
            raise ConversionError(
                f"soffice succeeded but no docx at {trampolined} "
                f"(soffice stdout: {result.stdout.strip()!r})"
            )
        return _convert_via_pandoc(trampolined, "docx")


def _convert_via_pandoc(path: Path, input_format: str) -> str:
    """Run `pandoc -f {input_format} -t html {path}` and return stdout."""
    result = subprocess.run(
        ["pandoc", "-f", input_format, "-t", "html", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise ConversionError(
            f"pandoc failed on {path}: {result.stderr.strip()}"
        )
    return result.stdout
