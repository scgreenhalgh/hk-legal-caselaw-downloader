"""Contract test for the shared `cases` table (design §10).

Writes canonical rows via ``CheckpointDB``'s public API, reads them back
via raw SQL, and asserts every column of interest round-trips with
matching values. This is a schema-drift alarm — if a column is renamed,
moved, or if ``upsert_case`` / ``mark_downloaded`` / ``mark_orphaned`` /
``mark_html_generated`` semantics change, the writer↔reader contract
breaks LOUDLY at CI time rather than silently at query time.

``viewer/graph.py`` has no ``cases`` reader — its readers target
``citations``, ``case_parallel_cites``, and ``viewer_hub_cache``. Per
design §10 the fallback is raw SQL over the checkpoint connection: the
point of a contract test is the shared-table schema promise, not the
shape of any specific reader.

Columns exercised (per Phase 6 spec): court, year, number, neutral,
title, date, status, formats, lang, html_generated_from.

Five-lens coverage:
  L1  silent skip     — direct value assertions, no bare-except swallow
  L2  semantic drift  — writer inputs must equal reader outputs
  L3  docstring drift — status transition test names the documented flow
  L4  wrong-side test — writer (upsert_case, mark_downloaded, ...) and
                        reader (raw SELECT) are both exercised
  L5  ambiguous state — a fresh row's formats/html_generated_from is
                        NULL (unset), distinct from an empty JSON list
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hklii_downloader.checkpoint import CheckpointDB

# Column indices for the shared SELECT, kept in one place so a rename
# in the SELECT string doesn't drift from the index constants below.
_SELECT_COLS = (
    "court, year, number, neutral, title, date, "
    "status, formats, lang, html_generated_from"
)
COL_COURT, COL_YEAR, COL_NUMBER = 0, 1, 2
COL_NEUTRAL, COL_TITLE, COL_DATE = 3, 4, 5
COL_STATUS, COL_FORMATS, COL_LANG, COL_HTML_GENERATED_FROM = 6, 7, 8, 9


def _read_row(db: CheckpointDB, court: str, year: int, number: int):
    """Read every column-of-interest for one row via raw SQL."""
    return db._conn.execute(
        f"SELECT {_SELECT_COLS} FROM cases "
        "WHERE court=? AND year=? AND number=?",
        (court, year, number),
    ).fetchone()


class TestCasesTableContract:
    """Contract: what ``CheckpointDB`` writes to ``cases``, callers read back."""

    def test_upsert_case_writes_land_in_canonical_columns(self):
        """upsert_case's inputs land in the eponymous columns.

        A fresh row has status='pending' (DDL default), formats=NULL and
        html_generated_from=NULL — the writer never touches those columns
        on insert. This is the canonical write-then-read pair for the
        schema-drift alarm.
        """
        db = CheckpointDB(":memory:")
        db.upsert_case(
            court="hkcfa",
            year=2020,
            number=32,
            neutral="[2020] HKCFA 32",
            title="A v B",
            date="2020-09-15",
            lang="en",
        )
        row = _read_row(db, "hkcfa", 2020, 32)
        assert row is not None, "row not found after upsert_case"
        assert row[COL_COURT] == "hkcfa"
        assert row[COL_YEAR] == 2020
        assert row[COL_NUMBER] == 32
        assert row[COL_NEUTRAL] == "[2020] HKCFA 32"
        assert row[COL_TITLE] == "A v B"
        assert row[COL_DATE] == "2020-09-15"
        assert row[COL_STATUS] == "pending"
        assert row[COL_FORMATS] is None
        assert row[COL_LANG] == "en"
        assert row[COL_HTML_GENERATED_FROM] is None

    def test_status_transitions_pending_downloaded_orphaned(self):
        """Documented status flow, as named by the CheckpointDB API:
        DDL default 'pending' → mark_downloaded → mark_orphaned.

        Each transition lands the expected status column via the exact
        method the docstring says owns that transition — a rename or
        semantic swap of any one method surfaces here.
        """
        db = CheckpointDB(":memory:")
        db.upsert_case(
            "hkcfi", 2023, 155,
            "[2023] HKCFI 155", "X v Y", "2023-02-01",
        )
        assert _read_row(db, "hkcfi", 2023, 155)[COL_STATUS] == "pending"

        db.mark_downloaded("hkcfi", 2023, 155, ["html", "txt", "json"])
        assert _read_row(db, "hkcfi", 2023, 155)[COL_STATUS] == "downloaded"

        db.mark_orphaned("hkcfi", 2023, 155)
        assert _read_row(db, "hkcfi", 2023, 155)[COL_STATUS] == "orphaned"

    def test_formats_json_list_survives_roundtrip(self):
        """mark_downloaded serialises formats as JSON text; a caller
        reading the raw column and running json.loads recovers the exact
        list. The shipped read helper ``get_formats`` matches.

        Both the raw-SQL path and the public reader path are asserted —
        L4: writer↔reader agreement must hold via either entry point.
        """
        db = CheckpointDB(":memory:")
        db.upsert_case(
            "hkca", 2024, 8, "[2024] HKCA 8", "P v Q", "2024-01-30",
        )
        original = ["html", "txt", "json", "doc"]
        db.mark_downloaded("hkca", 2024, 8, original)

        raw = _read_row(db, "hkca", 2024, 8)[COL_FORMATS]
        assert raw is not None, "formats column NULL after mark_downloaded"
        assert json.loads(raw) == original

        # Same value via the shipped read helper.
        assert db.get_formats("hkca", 2024, 8) == original

    def test_html_generated_from_records_source_extension(self):
        """mark_html_generated records the on-disk extension used for
        provenance. ``html_generated_from`` is the canonical column
        name — a rename to e.g. ``html_source_ext`` would fail here
        instead of silently dropping the value in downstream stats.
        """
        db = CheckpointDB(":memory:")
        db.upsert_case(
            "hkcfi", 2026, 100, "[2026] HKCFI 100", "R v S", "2026-01-05",
        )
        db.mark_downloaded("hkcfi", 2026, 100, ["doc"])
        db.mark_html_generated("hkcfi", 2026, 100, source_ext=".docx")

        assert _read_row(db, "hkcfi", 2026, 100)[COL_HTML_GENERATED_FROM] == ".docx"

    def test_bilingual_upsert_collapses_to_single_row(self):
        """(court, year, number) is the composite PK. A bilingual case
        (en on disk with a tc sibling) reaches ``upsert_case`` twice —
        once per language pass — but MUST land as a single row, not two.

        Two independent contract checks:
          (a) After the en-then-tc upsert pair, exactly one row exists
              for that PK triple (upsert_case's ON CONFLICT DO UPDATE
              collapses the pair). The lang column promotes to 'en'
              per the documented CASE — an en pass beats a tc pass.
          (b) A raw INSERT bypassing ON CONFLICT raises
              sqlite3.IntegrityError — the PK contract at the SQL layer,
              independent of upsert_case's collapse policy. If someone
              later drops the composite PK the raw INSERT will silently
              succeed and this assertion flips red.
        """
        db = CheckpointDB(":memory:")
        db.upsert_case(
            "hkcfa", 2020, 32,
            "[2020] HKCFA 32", "A v B", "2020-09-15",
            lang="en",
        )
        db.upsert_case(
            "hkcfa", 2020, 32,
            "[2020] HKCFA 32", "甲 對 乙", "2020-09-15",
            lang="tc",
        )

        # (a) PK collapse — one row survives the pair.
        count = db._conn.execute(
            "SELECT COUNT(*) FROM cases "
            "WHERE court=? AND year=? AND number=?",
            ("hkcfa", 2020, 32),
        ).fetchone()[0]
        assert count == 1, f"expected 1 row after bilingual upsert, got {count}"

        # ``upsert_case``'s CASE expression promotes lang to 'en' if
        # either write was 'en'. This is documented behaviour and
        # exercised here to catch a swap of the CASE branches.
        assert _read_row(db, "hkcfa", 2020, 32)[COL_LANG] == "en"

        # (b) Raw INSERT bypassing ON CONFLICT raises — the PK guard is
        # in the schema, not just in upsert_case.
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO cases "
                "(court, year, number, neutral, title, date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("hkcfa", 2020, 32, "n", "t", "d"),
            )
