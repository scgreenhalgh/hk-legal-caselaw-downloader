"""Contract test for the shared ``enum_runs`` table.

Design doc §10 "Contract tests per shared table" — one per table,
covering the writer↔reader agreement between the downloader
(``checkpoint.py``) and the viewer (``viewer.db`` + downstream
consumers). If a future refactor drops a column, renames one, changes
its type, or forgets ``json.dumps``, one of the four assertions here
fails immediately — that's the whole point.

The design originally called for these tests to live in the downloader
package. This one lives in ``tests/contract/`` alongside the viewer
code — the viewer worktree is where writer/reader agreement is
consumed, so the alarm fires where the mismatch would actually hurt.

Writer path: :meth:`hklii_downloader.checkpoint.CheckpointDB.start_enum_run`
and :meth:`~CheckpointDB.complete_enum_run` — the only public API that
touches ``enum_runs``.

Reader path: :func:`hklii_downloader.viewer.db.open_readonly` + raw SQL.
``viewer.graph`` ships no reader for this table (orphan_mark consumes
``latest_completed_enum_run`` inside the writer package), so the raw
SQL SELECT stands in as the reader-side of the contract — still catches
schema drift, which is the point.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.viewer.db import open_readonly


def _select_enum_run(db_path: Path, generation_id: int) -> dict | None:
    """Raw-SQL read of one ``enum_runs`` row through the viewer's
    readonly opener (not through :class:`CheckpointDB`) — proves the
    viewer sees the exact shape the writer committed. Returns None if
    the row is missing.

    Kept as a plain dict rather than sqlite3.Row so a future column
    rename triggers a ``KeyError`` at the assert site, not a silent
    ``None`` from Row's ``__getitem__``.
    """
    conn = open_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT generation_id, started_at, completed_at, "
            "courts_json, langs_json, min_date_text, max_date_text "
            "FROM enum_runs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "generation_id": row[0],
        "started_at": row[1],
        "completed_at": row[2],
        "courts_json": row[3],
        "langs_json": row[4],
        "min_date_text": row[5],
        "max_date_text": row[6],
    }


class TestEnumRunsContract:
    """Four schema-drift alarms. Each method exercises one column-level
    contract; when it fails, the failure message names the exact drift
    (column rename, type coercion, DEFAULT leak, forgotten json.dumps).
    """

    def test_generation_id_autoincrements_across_inserts(
        self, tmp_path: Path,
    ) -> None:
        """L5 ambiguous-state lens: two enum starts must never collide
        on the same ``generation_id``. INTEGER PRIMARY KEY AUTOINCREMENT
        guarantees strictly-larger IDs even after a DELETE — a plain
        INTEGER PRIMARY KEY reuses freed IDs and would let orphan_mark's
        cursor mis-order runs after a truncation.
        """
        db_path = tmp_path / "checkpoint.db"
        db = CheckpointDB(str(db_path))
        try:
            g1 = db.start_enum_run(["hkcfi"], ["en"])
            g2 = db.start_enum_run(["hkca"], ["en"])
        finally:
            db.close()

        assert isinstance(g1, int), (
            f"start_enum_run must return an int generation_id; "
            f"got {type(g1).__name__}"
        )
        assert isinstance(g2, int)
        assert g2 > g1, (
            f"second enum start must get a strictly-larger "
            f"generation_id; got g1={g1} g2={g2}"
        )
        # Reader-side: the ID the writer returned must exist in the DB.
        row1 = _select_enum_run(db_path, g1)
        row2 = _select_enum_run(db_path, g2)
        assert row1 is not None and row1["generation_id"] == g1
        assert row2 is not None and row2["generation_id"] == g2

    def test_completed_at_is_null_until_complete_enum_run_runs(
        self, tmp_path: Path,
    ) -> None:
        """L3 docstring-drift + L5 NULL-vs-0 lens: :meth:`start_enum_run`
        explicitly inserts ``completed_at=NULL``; only
        :meth:`complete_enum_run` fills it. If a future schema change
        added ``completed_at INTEGER DEFAULT (strftime(...))`` or
        auto-stamped it on INSERT, :meth:`latest_completed_enum_run`
        would return every in-flight run — an orphan_mark hazard
        (mass-orphan every row whose enum crashed mid-sweep).

        Also asserts ``started_at`` is populated on INSERT — the pair
        NULL/non-NULL discriminates 'in flight' from 'done'.
        """
        db_path = tmp_path / "checkpoint.db"
        db = CheckpointDB(str(db_path))
        try:
            g = db.start_enum_run(["hkcfi"], ["en"])

            row_open = _select_enum_run(db_path, g)
            assert row_open is not None
            assert row_open["completed_at"] is None, (
                f"completed_at must be NULL after start_enum_run; "
                f"got {row_open['completed_at']!r} — a leaked non-NULL "
                f"here breaks latest_completed_enum_run's 'is it done?' "
                f"check and mass-orphans in-flight enums."
            )
            assert row_open["started_at"] is not None
            assert isinstance(row_open["started_at"], int)

            db.complete_enum_run(g)
            row_done = _select_enum_run(db_path, g)
            assert row_done is not None
            assert row_done["completed_at"] is not None, (
                "completed_at must be non-NULL after complete_enum_run; "
                "still NULL means the UPDATE didn't hit the row."
            )
            assert isinstance(row_done["completed_at"], int)
            # started_at unchanged by complete_enum_run — pins the
            # semantic that only completed_at flips.
            assert row_done["started_at"] == row_open["started_at"]
            assert row_done["completed_at"] >= row_done["started_at"]
        finally:
            db.close()

    def test_courts_and_langs_round_trip_as_json_strings(
        self, tmp_path: Path,
    ) -> None:
        """L2 semantic-drift lens: ``courts_json`` and ``langs_json``
        are TEXT columns holding JSON-encoded lists — not comma-joined
        blobs, not sqlite3-native arrays. ``json.loads`` on the value
        must give back exactly what the writer passed in. If a future
        refactor swapped in ``",".join(courts)`` (a common shortcut) or
        stored a Python ``repr()``, the round-trip breaks here and any
        orphan_mark check that unpacks ``latest["courts"]`` would see a
        malformed list.
        """
        db_path = tmp_path / "checkpoint.db"
        db = CheckpointDB(str(db_path))
        # Multi-court, multi-lang list exercises the JSON encoder more
        # thoroughly than a single-slug run.
        courts = ["hkcfa", "hkca", "hkcfi", "hkdc"]
        langs = ["en", "tc"]
        try:
            g = db.start_enum_run(courts, langs)
        finally:
            db.close()

        row = _select_enum_run(db_path, g)
        assert row is not None
        # Stored form is a str, not bytes or a native list.
        assert isinstance(row["courts_json"], str), (
            f"courts_json must be stored as TEXT; got "
            f"{type(row['courts_json']).__name__}"
        )
        assert isinstance(row["langs_json"], str)
        # Round-trip via json.loads gives back the original lists.
        assert json.loads(row["courts_json"]) == courts, (
            f"courts_json round-trip broke: wrote {courts!r}, "
            f"read {row['courts_json']!r}"
        )
        assert json.loads(row["langs_json"]) == langs

    def test_full_corpus_sweep_persists_window_columns_as_null(
        self, tmp_path: Path,
    ) -> None:
        """L4 wrong-side-test lens: :meth:`latest_completed_enum_run`
        filters to ``min_date_text IS NULL AND max_date_text IS NULL``
        so orphan_mark only trusts full-corpus sweeps as its cutoff
        reference. That reader-side filter is only correct if the
        WRITER side actually persists both columns as NULL when the
        caller passes no window kwargs — an existing bug (see
        ``_migrate_enum_runs_window_columns``) was that legacy rows had
        no window info at all, and the fix nulled them out.

        This test pins the writer half of that contract: a call with no
        ``min_date_text`` / ``max_date_text`` results in NULL columns.
        Also exercises the narrow-window counterfactual — explicit
        dd/mm/yyyy strings must survive round-trip verbatim so the
        reader's ``IS NOT NULL`` branch can filter them out correctly.
        """
        db_path = tmp_path / "checkpoint.db"
        db = CheckpointDB(str(db_path))
        try:
            # Full-corpus sweep: no window kwargs at all — mirrors the
            # `hklii scrape` default caller shape.
            g_full = db.start_enum_run(["hkcfi"], ["en"])
            # Narrow-window sweep: mirrors `hklii update --profile daily`.
            g_narrow = db.start_enum_run(
                ["hkcfi"], ["en"],
                min_date_text="06/06/2026",
                max_date_text="06/07/2026",
            )
        finally:
            db.close()

        row_full = _select_enum_run(db_path, g_full)
        assert row_full is not None
        assert row_full["min_date_text"] is None, (
            f"full-corpus sweep leaked non-NULL min_date_text: "
            f"{row_full['min_date_text']!r} — latest_completed_enum_run "
            f"would exclude a legitimate full-corpus row and orphan_mark "
            f"would lose its reference generation."
        )
        assert row_full["max_date_text"] is None

        row_narrow = _select_enum_run(db_path, g_narrow)
        assert row_narrow is not None
        # Verbatim TEXT round-trip — the dd/mm/yyyy shape is HKLII's
        # native format; any coercion (ISO conversion, whitespace strip)
        # would silently change the semantics.
        assert row_narrow["min_date_text"] == "06/06/2026"
        assert row_narrow["max_date_text"] == "06/07/2026"
