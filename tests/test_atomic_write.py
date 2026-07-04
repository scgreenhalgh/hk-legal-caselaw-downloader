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


class TestParentDirFsync:
    """The rename in os.replace is not durable until the parent dir is fsynced.
    Missing this step means an unclean reboot can drop the rename even though
    the .part file was fsynced. Real risk over a 20-40h run.
    """

    def _run_with_fsync_spy(self, tmp_path, invoke):
        fsynced_paths: list[str] = []
        fd_to_path: dict[int, str] = {}
        real_open = os.open
        real_fsync = os.fsync

        def spy_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            fd_to_path[fd] = str(path)
            return fd

        def spy_fsync(fd):
            fsynced_paths.append(fd_to_path.get(fd, f"unknown-fd:{fd}"))
            return real_fsync(fd)

        with patch("os.open", side_effect=spy_open), patch("os.fsync", side_effect=spy_fsync):
            invoke()
        return fsynced_paths

    def test_atomic_write_text_fsyncs_parent_dir_after_replace(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_text

        p = tmp_path / "out.txt"
        fsynced = self._run_with_fsync_spy(tmp_path, lambda: atomic_write_text(p, "hello"))

        assert p.read_text() == "hello"
        assert any(fp.endswith(".part") for fp in fsynced), (
            f"regression: .part file was not fsynced. paths={fsynced}"
        )
        assert str(tmp_path) in fsynced, (
            f"expected parent dir {tmp_path} to be fsynced after os.replace; "
            f"actually fsynced: {fsynced}"
        )

    def test_atomic_write_bytes_fsyncs_parent_dir_after_replace(self, tmp_path):
        from hklii_downloader.atomic_write import atomic_write_bytes

        p = tmp_path / "out.bin"
        fsynced = self._run_with_fsync_spy(tmp_path, lambda: atomic_write_bytes(p, b"\x00\x01\x02"))

        assert p.read_bytes() == b"\x00\x01\x02"
        assert str(tmp_path) in fsynced, (
            f"expected parent dir {tmp_path} to be fsynced after os.replace; "
            f"actually fsynced: {fsynced}"
        )
