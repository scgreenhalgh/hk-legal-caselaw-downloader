"""Read-only checkpoint.db connection helper.

The downloader owns checkpoint.db and holds a POSIX fcntl write lock via
CheckpointDB. The viewer must never grab that lock (a concurrent scraper
would block us) and must never write (it would corrupt the writer's
source of truth). SQLite's URI ``mode=ro`` gives us both properties in
one call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` read-only. Never grabs an fcntl lock.

    Writes on the returned connection raise :class:`sqlite3.OperationalError`
    — L1 silent-skip lens: the viewer never silently no-ops a write; a
    stray write attempt in viewer code is a bug we want to see loudly.

    ``PRAGMA query_only`` is set as belt-and-suspenders on top of
    ``mode=ro`` — it also blocks operations that ``mode=ro`` allows but
    the viewer never needs (e.g. temporary schema creation).

    Concurrency contract: under WAL journal_mode (which viewer.db and
    checkpoint.db both use), a connection returned by this function
    coexists with a concurrent writer without ``SQLITE_BUSY`` — it sees
    the snapshot as of when its transaction started, and a fresh call
    picks up the latest committed state. This is design §4 line 50 and
    is pinned by ``tests/test_viewer_db_wal_concurrent_reader.py``.
    """
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = 1")
    return conn
