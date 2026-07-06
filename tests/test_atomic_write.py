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

    def test_parent_dir_fsync_happens_after_part_fsync_and_after_replace(
        self, tmp_path,
    ):
        """Whole-codebase review (L4): the sibling tests assert the parent
        dir appears in the fsynced list but not its POSITION. The
        durability contract requires:
          1. .part fsync (data reaches disk),
          2. os.replace (rename lands in inode cache),
          3. parent dir fsync (rename survives unclean reboot).
        A refactor that reordered these — say, dir fsync BEFORE replace —
        would leave the crash window open yet keep the position-agnostic
        sibling tests green. Pin the order explicitly.
        """
        import os as _os
        from hklii_downloader.atomic_write import atomic_write_text

        events: list = []
        real_open = _os.open
        real_fsync = _os.fsync
        real_replace = _os.replace

        def spy_open(*a, **kw):
            fd = real_open(*a, **kw)
            path = a[0] if a else kw.get("path")
            events.append(("open", str(path), fd))
            return fd

        def spy_fsync(fd):
            # Look up which path this fd was opened for. FDs are reused
            # after close — walk backwards to find the MOST RECENT open
            # of this fd.
            path = None
            for kind, p, opened_fd in reversed(events):
                if kind == "open" and opened_fd == fd:
                    path = p
                    break
            events.append(("fsync", path, fd))
            return real_fsync(fd)

        def spy_replace(src, dst):
            events.append(("replace", str(src), str(dst)))
            return real_replace(src, dst)

        p = tmp_path / "out.txt"
        from unittest.mock import patch
        with patch("os.open", side_effect=spy_open), \
             patch("os.fsync", side_effect=spy_fsync), \
             patch("os.replace", side_effect=spy_replace):
            atomic_write_text(p, "hello")

        # Extract just the meaningful event tuples for assertions.
        order = [
            (e[0], e[1]) for e in events
            if e[0] in ("fsync", "replace")
        ]
        # Filter to only .part fsync + replace + parent fsync.
        part_fsync_idx = next(
            (i for i, e in enumerate(order)
             if e[0] == "fsync" and e[1] and e[1].endswith(".part")),
            None,
        )
        replace_idx = next(
            (i for i, e in enumerate(order) if e[0] == "replace"),
            None,
        )
        parent_fsync_idx = next(
            (i for i, e in enumerate(order)
             if e[0] == "fsync" and e[1] == str(tmp_path)),
            None,
        )
        assert part_fsync_idx is not None, (
            f"no .part fsync in order: {order}"
        )
        assert replace_idx is not None, f"no os.replace: {order}"
        assert parent_fsync_idx is not None, (
            f"no parent dir fsync: {order}"
        )
        assert part_fsync_idx < replace_idx, (
            f".part fsync must precede replace; order: {order}"
        )
        assert replace_idx < parent_fsync_idx, (
            f"parent dir fsync must FOLLOW replace so the rename "
            f"survives an unclean reboot; order: {order}"
        )
