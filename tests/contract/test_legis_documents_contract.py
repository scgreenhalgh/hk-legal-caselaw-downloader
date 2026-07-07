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
* **L2 semantic drift** — ``formats`` is JSON-encoded in the shipped
  writer; the parity check against ``cases.formats`` catches a future
  drift where one table stays JSON and the other moves to CSV/repr().
* **L4 wrong-side** — write via the real public API + read via the real
  viewer connection surface. No test-internal shortcut on either side.
* **L5 ambiguous state** — the PK-rejection test uses a raw INSERT (not
  the checkpoint upsert's ``ON CONFLICT DO UPDATE``) so we distinguish
  "schema PK enforced" from "API absorbs conflicts". Two different
  guarantees; conflating them would hide a schema-drift bug.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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
        # exhaustively round-trip it in the dedicated test below.
        assert formats_json is not None, (
            "mark_legis_downloaded left formats NULL — writer regression"
        )

    def test_primary_key_rejects_duplicate_raw_insert(
        self, tmp_path: Path,
    ) -> None:
        """Schema PK (abbr, num, lang) is enforced at the SQLite layer.

        Uses raw INSERT — NOT the checkpoint upsert, which has
        ``ON CONFLICT ... DO UPDATE`` and would absorb the collision.
        The alarm this test exists for: a migration that widens the PK
        (e.g. adds ``latest_vid`` to it, or drops ``lang``) would let
        two rows with the same (abbr, num, lang) coexist and silently
        corrupt every viewer query that assumes uniqueness.
        L5 lens — distinguish "schema enforces PK" from "API absorbs
        conflicts"; two different guarantees, don't conflate.
        """
        cp_path = tmp_path / "checkpoint.db"
        _seed_and_close(cp_path)

        # Raw writable connection — bypass CheckpointDB's ON CONFLICT
        # UPDATE and hit the schema constraint directly. Not open_readonly
        # because that would block writes before we could probe the PK.
        conn = sqlite3.connect(str(cp_path))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO legis_documents "
                    "(abbr, num, lang, title, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        CANONICAL["abbr"],
                        CANONICAL["num"],
                        CANONICAL["lang"],
                        "Any Other Title",
                        "pending",
                    ),
                )
        finally:
            conn.close()

    def test_formats_column_json_round_trips_and_matches_cases_shape(
        self, tmp_path: Path,
    ) -> None:
        """``formats`` is a JSON-encoded list-of-str, same shape as
        ``cases.formats``.

        Two guarantees pinned in one test:
        1. Writer serializes to JSON → reader parses back to the
           identical Python list (values + order + element types).
        2. Serialization format is CONSISTENT with ``cases.formats``,
           the sibling table that ``mark_downloaded`` writes with the
           same JSON convention. A future refactor that moves one table
           to CSV/repr() while the other stays JSON fails this test —
           L2 semantic-drift lens.
        """
        cp_path = tmp_path / "checkpoint.db"
        _seed_and_close(cp_path)

        conn = open_readonly(str(cp_path))
        try:
            legis_formats_json = conn.execute(
                "SELECT formats FROM legis_documents "
                "WHERE abbr=? AND num=? AND lang=?",
                (CANONICAL["abbr"], CANONICAL["num"], CANONICAL["lang"]),
            ).fetchone()[0]
        finally:
            conn.close()

        # Guarantee 1: JSON round-trip preserves list-of-str exactly.
        assert legis_formats_json is not None
        parsed = json.loads(legis_formats_json)
        assert parsed == CANONICAL["formats"]
        assert isinstance(parsed, list)
        assert all(isinstance(x, str) for x in parsed)

        # Guarantee 2: cases.formats writes the same JSON shape. If a
        # future migration diverges the two encodings, this fires.
        cases_cp = tmp_path / "cases_cp.db"
        db2 = CheckpointDB(str(cases_cp))
        try:
            db2.upsert_case(
                court="hkcfa", year=2020, number=32,
                neutral="[2020] HKCFA 32",
                title="Contract-Parity Sentinel",
                date="2020-06-30",
            )
            db2.mark_downloaded(
                court="hkcfa", year=2020, number=32,
                formats=CANONICAL["formats"],  # type: ignore[arg-type]
            )
        finally:
            db2.close()

        conn2 = open_readonly(str(cases_cp))
        try:
            cases_formats_json = conn2.execute(
                "SELECT formats FROM cases "
                "WHERE court=? AND year=? AND number=?",
                ("hkcfa", 2020, 32),
            ).fetchone()[0]
        finally:
            conn2.close()

        # Compare parsed shapes (encoders may differ on whitespace but
        # the semantic content must match) — this is the drift alarm.
        assert json.loads(cases_formats_json) == parsed
