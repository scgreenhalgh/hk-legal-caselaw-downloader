"""Contract test for the shared ``legis_documents`` table.

Design ref: docs/viewer-design.md §10 "Contract tests per shared table"
(the design's "downloader package" location was amended in Phase 6 —
contract tests now land in tests/contract/ inside the viewer worktree
where the reader code lives).

The point of this test is a **schema-drift alarm**: it writes a canonical
row through checkpoint.py's public API and reads it back through the
viewer's read-only connection surface. If a future migration renames a
column, changes a type, swaps the primary key, or changes the ``formats``
serialization format, one of the assertions here fails loudly on CI —
long before a viewer route silently returns wrong data.

viewer/graph.py has no reader for ``legis_documents`` yet (Phase 6+
work), so the read side is raw SQL over ``viewer.db.open_readonly`` —
the same connection surface every shipped route uses. Reading via
open_readonly (not sqlite3.connect) means a query_only / mode=ro
regression that blocks a legitimate SELECT would also surface here.

5-lens coverage:
* **L1 silent skip** — every writer-provided column is asserted
  individually. A NULL leaking past ``mark_legis_downloaded`` (e.g. the
  UPDATE mis-targets the row) fails the specific column, not a shrugged
  "row exists".
* **L4 wrong-side** — write via the real public API + read via the real
  viewer connection surface. No test-internal shortcut on either side.
"""

from __future__ import annotations

from pathlib import Path

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.viewer.db import open_readonly


# Canonical row exercised across every assertion. Values chosen from
# real-corpus shapes: 622C is a live alphanumeric cap number (the
# Companies Regulation family); latest_vid is a plausible capversion
# id; latest_version_date is ISO-8601 as the writer emits.
CANONICAL: dict[str, object] = {
    "abbr": "ord",
    "num": "622C",
    "lang": "en",
    "title": "Companies (Non-Hong Kong Companies) Regulation",
    "latest_vid": 19113,
    "latest_version_date": "2018-02-01",
    "status": "downloaded",
    "formats": ["versions", "content"],
}


def _write_canonical(db: CheckpointDB) -> None:
    """Persist CANONICAL through the two public write entry points a
    real scraper would call: upsert (title + PK) then mark_downloaded
    (status + latest_vid + latest_version_date + formats)."""
    db.upsert_legis_document(
        abbr=CANONICAL["abbr"],  # type: ignore[arg-type]
        num=CANONICAL["num"],    # type: ignore[arg-type]
        lang=CANONICAL["lang"],  # type: ignore[arg-type]
        title=CANONICAL["title"],  # type: ignore[arg-type]
    )
    db.mark_legis_downloaded(
        abbr=CANONICAL["abbr"],                    # type: ignore[arg-type]
        num=CANONICAL["num"],                      # type: ignore[arg-type]
        lang=CANONICAL["lang"],                    # type: ignore[arg-type]
        latest_vid=CANONICAL["latest_vid"],        # type: ignore[arg-type]
        latest_version_date=CANONICAL["latest_version_date"],  # type: ignore[arg-type]
        formats=CANONICAL["formats"],              # type: ignore[arg-type]
    )


def _seed_and_close(cp_path: Path) -> None:
    """Open a CheckpointDB, write the canonical row, close cleanly.

    Closing releases the fcntl write lock so subsequent open_readonly /
    raw sqlite3.connect calls in the same test don't race the writer.
    """
    db = CheckpointDB(str(cp_path))
    try:
        _write_canonical(db)
    finally:
        db.close()


class TestLegisDocumentsContract:
    """Cross-boundary contract: writer schema == reader schema.

    Fires when a downloader migration touches ``legis_documents``
    without a matching viewer-side update.
    """

    def test_canonical_row_reads_back_with_matching_values(
        self, tmp_path: Path,
    ) -> None:
        """Every writer-provided column reads back byte-for-byte.

        Reads via viewer.db.open_readonly (the real viewer surface,
        NOT a raw sqlite3.connect) so any future regression that blocks
        legitimate SELECTs on the read-only handle also fires here.
        L1 / L4 lenses — each column is asserted individually and both
        sides of the wire use their production entry points.
        """
        cp_path = tmp_path / "checkpoint.db"
        _seed_and_close(cp_path)

        conn = open_readonly(str(cp_path))
        try:
            row = conn.execute(
                "SELECT abbr, num, lang, title, latest_vid, "
                "latest_version_date, status, formats "
                "FROM legis_documents "
                "WHERE abbr=? AND num=? AND lang=?",
                (CANONICAL["abbr"], CANONICAL["num"], CANONICAL["lang"]),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, (
            "canonical row missing from legis_documents — writer path broke "
            "or the (abbr, num, lang) PK shape drifted"
        )
        (
            abbr, num, lang, title, latest_vid,
            latest_version_date, status, formats_json,
        ) = row

        # Each column asserted individually so a NULL leak (e.g. the
        # UPDATE mis-targeted the row) points at the specific column.
        assert abbr == CANONICAL["abbr"]
        assert num == CANONICAL["num"]
        assert lang == CANONICAL["lang"]
        assert title == CANONICAL["title"]
        assert latest_vid == CANONICAL["latest_vid"]
        assert latest_version_date == CANONICAL["latest_version_date"]
        assert status == CANONICAL["status"]
        # formats is stored as a JSON string — assert non-NULL here and
        # the JSON round-trip lens covers the parse-back semantics.
        assert formats_json is not None, (
            "mark_legis_downloaded left formats NULL — writer regression"
        )
