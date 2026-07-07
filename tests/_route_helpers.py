"""Shared setup helpers for viewer HTTP route tests.

Not a test module (underscore prefix means pytest does not collect it).
Each route test file imports the seeders it needs; per-file ``client``
fixtures compose them into a :class:`fastapi.testclient.TestClient`.

Design §10 pins a session-scoped 20-row fixture DB. That has not been
built yet — this module is the seam through which such a fixture would
compose. For now, tests continue to build tmp_path-scoped fresh DBs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hklii_downloader.viewer.schema import create_schema


# Mirror of the columns the route layer actually reads from ``cases``.
# Full schema is in ``hklii_downloader.checkpoint._SCHEMA``.
CASES_DDL = """
CREATE TABLE cases (
    court    TEXT NOT NULL,
    year     INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    neutral  TEXT NOT NULL,
    title    TEXT NOT NULL,
    date     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    formats  TEXT,
    error    TEXT,
    lang     TEXT NOT NULL DEFAULT 'en',
    last_seen_at INTEGER,
    html_generated_from   TEXT,
    PRIMARY KEY (court, year, number)
);
"""


def seed_cases(db_path: Path, rows: list[tuple]) -> None:
    """rows: (court, year, number, neutral, title, date, status)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(CASES_DDL)
        conn.executemany(
            "INSERT INTO cases "
            "(court, year, number, neutral, title, date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


CITATIONS_DDL = """
CREATE TABLE citations (
    from_key   TEXT NOT NULL,
    to_key     TEXT NOT NULL,
    citer_lang TEXT NOT NULL,
    citer_freq INTEGER,
    position   INTEGER,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (from_key, to_key, citer_lang)
) WITHOUT ROWID;
CREATE INDEX idx_cit_to ON citations(to_key);
"""


PARALLEL_CITES_DDL = """
CREATE TABLE case_parallel_cites (
    case_key      TEXT NOT NULL,
    parallel_cite TEXT NOT NULL,
    PRIMARY KEY (case_key, parallel_cite)
) WITHOUT ROWID;
"""


def seed_citations(
    db_path: Path,
    rows: list[tuple[str, str, str, int, int, str]],
) -> None:
    """rows: (from_key, to_key, citer_lang, citer_freq, position, first_seen).

    Creates the ``citations`` table + ``idx_cit_to`` index if absent.
    Idempotent per test since the DDL uses ``IF NOT EXISTS`` at run time
    via ``executescript``; callers passing an already-seeded path get an
    error on re-CREATE — the tests are per-tmp_path so this is fine.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(CITATIONS_DDL)
        conn.executemany(
            "INSERT INTO citations "
            "(from_key, to_key, citer_lang, citer_freq, position, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def seed_parallel_cites(
    db_path: Path,
    rows: list[tuple[str, str]],
) -> None:
    """rows: (case_key, parallel_cite)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(PARALLEL_CITES_DDL)
        conn.executemany(
            "INSERT INTO case_parallel_cites (case_key, parallel_cite) "
            "VALUES (?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def build_viewer_db(path: Path) -> None:
    """Fresh viewer.db via :func:`viewer.schema.create_schema`."""
    conn = sqlite3.connect(str(path))
    try:
        create_schema(conn)
    finally:
        conn.close()


def seed_hub_cache(
    path: Path,
    rows: list[tuple[str, int, str]],
) -> None:
    """rows: (case_key, inbound_count, computed_at)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany(
            "INSERT INTO viewer_hub_cache "
            "(case_key, inbound_count, computed_at) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def drop_hub_cache_table(path: Path) -> None:
    """Drop ``viewer_hub_cache`` so :func:`hub_cases` raises
    :class:`ViewerCacheMissing`. Used by route tests that verify the
    'run the indexer' banner path.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE viewer_hub_cache")
        conn.commit()
    finally:
        conn.close()
