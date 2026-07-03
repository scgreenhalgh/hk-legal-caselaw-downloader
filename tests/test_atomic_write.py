"""Tests for atomic file writing helper."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_text
        p = tmp_path / "out.txt"
        atomic_write_text(p, "hello world")
        assert p.read_text() == "hello world"

    def test_no_partfile_left_behind_on_success(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_text
        p = tmp_path / "out.txt"
        atomic_write_text(p, "content")
        remaining = list(tmp_path.iterdir())
        assert remaining == [p], (
            f"expected only final path, got {[r.name for r in remaining]}"
        )

    def test_leaves_no_partfile_on_write_error(self, tmp_path):
        """If write_text raises, .part must not remain — atomic semantics."""
        from hklii_downloader.atomic_write import atomic_write_text
        p = tmp_path / "out.txt"

        real_write = Path.write_text

        def failing_write(self, content, **kw):
            if str(self).endswith(".part"):
                raise OSError("simulated ENOSPC")
            return real_write(self, content, **kw)

        with patch.object(Path, "write_text", failing_write):
            raised = None
            try:
                atomic_write_text(p, "content")
            except OSError as e:
                raised = e
        assert raised is not None
        # No .part file should remain
        parts = [f for f in tmp_path.iterdir() if str(f).endswith(".part")]
        assert parts == [], f"expected no .part file, got {parts}"

    def test_existing_file_replaced_atomically(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_text
        p = tmp_path / "out.txt"
        p.write_text("old")
        atomic_write_text(p, "new")
        assert p.read_text() == "new"


class TestAtomicWriteBytes:
    def test_writes_bytes(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_bytes
        p = tmp_path / "out.bin"
        atomic_write_bytes(p, b"\x00\x01\x02")
        assert p.read_bytes() == b"\x00\x01\x02"
