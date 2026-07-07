"""Tests for viewer/db.py — read-only checkpoint.db opener.

The downloader owns checkpoint.db and grabs a POSIX fcntl write lock via
CheckpointDB. The viewer must open the same file WITHOUT touching that
lock (a concurrent scraper would block us), and without ever writing —
so we can never corrupt the source of truth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hklii_downloader.viewer.db import open_readonly


def _seed_minimal_cases(db_path: Path) -> None:
    """Write a one-row cases table with a writer connection, then close."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE cases (court TEXT, year INTEGER, number INTEGER, title TEXT)"
    )
    conn.execute("INSERT INTO cases VALUES ('hkcfa', 2020, 1, 'Test v Test')")
    conn.commit()
    conn.close()


def test_reads_ok(tmp_path: Path) -> None:
    """Happy path — SELECT on the read-only handle returns real rows."""
    db = tmp_path / "checkpoint.db"
    _seed_minimal_cases(db)
    conn = open_readonly(db)
    try:
        row = conn.execute(
            "SELECT court, year, number, title FROM cases"
        ).fetchone()
        assert row == ("hkcfa", 2020, 1, "Test v Test")
    finally:
        conn.close()


def test_writes_raise(tmp_path: Path) -> None:
    """L1 silent-skip lens: a write attempt must raise, not silently no-op.

    The viewer is READ-ONLY over checkpoint.db. A stray write in viewer code is
    a bug we want to see immediately, not swallow.
    """
    db = tmp_path / "checkpoint.db"
    _seed_minimal_cases(db)
    conn = open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match=r"read.?only"):
            conn.execute("INSERT INTO cases VALUES ('hkca', 2020, 2, 'Other')")
    finally:
        conn.close()


def test_accepts_str_and_pathlib(tmp_path: Path) -> None:
    """Real callers pass pathlib.Path; construction helpers often pass str."""
    db = tmp_path / "checkpoint.db"
    _seed_minimal_cases(db)
    for arg in (str(db), db):
        conn = open_readonly(arg)
        try:
            assert conn.execute("SELECT COUNT(*) FROM cases").fetchone() == (1,)
        finally:
            conn.close()
