"""viewer.db schema DDL.

viewer.db is the viewer-owned derivative store: FTS index over case bodies,
precomputed hub cache, any other precomputation the viewer needs. The
downloader-owned checkpoint.db is read-only from the viewer's perspective —
see docs/viewer-design.md §0 (Option 3 scope).

Adding a new viewer-side table means:
1. Append its DDL string here
2. Add it to ``ALL_DDL``
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


ALL_DDL: list[str] = [VIEWER_HUB_CACHE_DDL]


def create_schema(conn: sqlite3.Connection) -> None:
    """Execute every DDL in a single transaction. Idempotent (IF NOT EXISTS)."""
    with conn:
        for ddl in ALL_DDL:
            conn.execute(ddl)
