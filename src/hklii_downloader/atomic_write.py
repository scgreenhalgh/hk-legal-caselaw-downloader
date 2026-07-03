"""Atomic file writes: write to {path}.part → fsync → os.replace.

WAL commits land before Path.write_text's bytes reach disk, so a
crash between them makes the checkpoint claim a file exists that
doesn't. These helpers close that window.
"""
from __future__ import annotations

import os
from pathlib import Path


def _fsync_and_replace(part: Path, dest: Path) -> None:
    fd = os.open(part, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, dest)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    part = path.with_suffix(path.suffix + ".part")
    try:
        part.write_text(content, encoding=encoding)
        _fsync_and_replace(part, path)
    except BaseException:
        # Clean up the partial file on any error, then re-raise
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    part = path.with_suffix(path.suffix + ".part")
    try:
        part.write_bytes(data)
        _fsync_and_replace(part, path)
    except BaseException:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise
