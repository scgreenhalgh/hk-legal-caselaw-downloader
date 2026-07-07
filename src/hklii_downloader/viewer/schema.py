"""viewer.db schema DDL.

viewer.db is the viewer-owned derivative store: FTS index over case bodies,
precomputed hub cache, any other precomputation the viewer needs. The
downloader-owned checkpoint.db is read-only from the viewer's perspective
(see docs/viewer-design.md §0 Option 3 scope).

Adding a new viewer-side table:
1. Add its DDL constant here
2. Append to ``ALL_DDL`` (in dependency order — case_bodies before fts_body)
3. Callers of ``create_schema`` pick it up automatically
"""

from __future__ import annotations

import sqlite3


VIEWER_HUB_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS viewer_hub_cache (
    case_key      TEXT NOT NULL PRIMARY KEY,
    inbound_count INTEGER NOT NULL,
    computed_at   TEXT NOT NULL
) WITHOUT ROWID;
""".strip()


# fts_cases: filter-only metadata mirror. Bilingual keying (§4 line 82,
# L2 fix) — composite PK (case_key, lang), one row per (case, language).
# Body text is NOT here (case_bodies owns it); this table exists so the
# route can add filter columns without rebuilding the FTS index.
FTS_CASES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fts_cases (
    case_key      TEXT NOT NULL,
    lang          TEXT NOT NULL,
    court         TEXT NOT NULL,
    year          INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    neutral       TEXT NOT NULL,
    title         TEXT NOT NULL,
    date          TEXT NOT NULL,
    body_source   TEXT NOT NULL,
    body_sha256   TEXT NOT NULL,
    indexed_at    TEXT NOT NULL,
    PRIMARY KEY (case_key, lang)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_fts_cases_court_year ON fts_cases(court, year);
CREATE INDEX IF NOT EXISTS idx_fts_cases_lang_court ON fts_cases(lang, court);
""".strip()


# case_bodies: the plaintext content table backing fts_body's external
# content. Requires an INTEGER PRIMARY KEY (used as content_rowid). The
# design's line 75 fix: title INCLUDED as its own column so snippet()
# against fts_body's title column has a source. UNIQUE(case_key, lang)
# prevents double-inserting the same bilingual half.
CASE_BODIES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS case_bodies (
    id       INTEGER PRIMARY KEY,
    case_key TEXT NOT NULL,
    lang     TEXT NOT NULL,
    title    TEXT NOT NULL,
    body     TEXT NOT NULL,
    UNIQUE (case_key, lang)
);
""".strip()


# fts_body: FTS5 virtual table over case_bodies (external content).
# Trigram tokenizer — the ONLY workable single-tokenizer choice for the
# 50/50 EN/TC corpus (design §4 line 78): unicode61 treats a run of Han
# chars as ONE token; porter is EN-only; ICU isn't in stock CPython
# sqlite3. Trigram indexes overlapping 3-char sequences → both EN
# substrings and CJK 3+ char queries match; 2-char CJK queries yield
# no rows (documented lower bound, UI validates upfront).
FTS_BODY_TABLE_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_body USING fts5(
    title,
    body,
    content='case_bodies',
    content_rowid='id',
    tokenize='trigram case_sensitive 0'
);
""".strip()


# External-content sync triggers. Standard FTS5 pattern (see SQLite docs
# 'External Content Tables'). Insert on case_bodies mirrors to fts_body;
# delete/update use the special `INSERT INTO fts_body(fts_body, ...)`
# meta-command to remove old tokens.
FTS_BODY_TRIGGERS_DDL = """
CREATE TRIGGER IF NOT EXISTS case_bodies_ai AFTER INSERT ON case_bodies BEGIN
    INSERT INTO fts_body(rowid, title, body)
        VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS case_bodies_ad AFTER DELETE ON case_bodies BEGIN
    INSERT INTO fts_body(fts_body, rowid, title, body)
        VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS case_bodies_au AFTER UPDATE ON case_bodies BEGIN
    INSERT INTO fts_body(fts_body, rowid, title, body)
        VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO fts_body(rowid, title, body)
        VALUES (new.id, new.title, new.body);
END;
""".strip()


#: DDL execution order. case_bodies MUST land before fts_body (fts_body
#: references it as content). Triggers must land after both.
ALL_DDL: list[str] = [
    VIEWER_HUB_CACHE_DDL,
    FTS_CASES_TABLE_DDL,
    CASE_BODIES_TABLE_DDL,
    FTS_BODY_TABLE_DDL,
    FTS_BODY_TRIGGERS_DDL,
]


def create_schema(conn: sqlite3.Connection) -> None:
    """Execute every DDL block via ``executescript`` for multi-statement
    support. Idempotent (every CREATE uses IF NOT EXISTS).

    Also sets ``PRAGMA journal_mode=WAL`` (design §4 line 107). WAL is a
    persistent per-DB property: once set the first time, it survives
    connection close and reopen. Setting it here means every fresh
    viewer.db picks it up automatically at creation, and a subsequent
    ``hklii serve`` reader coexists with a ``hklii viewer index --incremental``
    writer without either blocking the other.

    Note: ``executescript`` issues an implicit BEGIN/COMMIT — do NOT wrap
    the caller in a transaction, or nested-transaction errors will fire.
    """
    conn.execute("PRAGMA journal_mode = WAL")
    for ddl in ALL_DDL:
        conn.executescript(ddl)
