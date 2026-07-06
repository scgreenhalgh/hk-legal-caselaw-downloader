"""Tests for CheckpointDB — SQLite checkpoint with WAL mode."""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from hklii_downloader.checkpoint import CheckpointDB, CaseRecord


class TestHtmlPendingTracker:
    """Track cases captured via doc-fallback (empty content_html at HKLII).
    Motivation: HKLII shows 'Only the Word format is available at the moment'
    for recent-2026 judgments; we still save the .doc/.docx via --allow-doc,
    but we should remember to re-check these cases on later runs to pick
    up the HTML once HKLII processes it."""

    def test_schema_has_html_pending_column(self):
        db = CheckpointDB(":memory:")
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(cases)").fetchall()}
        assert "html_pending_at_hklii" in cols, (
            f"expected html_pending_at_hklii column; got {sorted(cols)}"
        )

    def test_html_pending_is_null_by_default(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2026, 3816, "[2026] HKCFI 3816", "T v T", "2026-07-01")
        row = db._conn.execute(
            "SELECT html_pending_at_hklii FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] is None

    def test_mark_downloaded_with_html_pending_ts_stamps_column(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2026, 3816, "[2026] HKCFI 3816", "T v T", "2026-07-01")
        db.mark_downloaded("hkcfi", 2026, 3816, ["doc"], html_pending_ts=1751600000)
        row = db._conn.execute(
            "SELECT status, formats, html_pending_at_hklii FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] == "downloaded"
        assert row[2] == 1751600000

    def test_mark_downloaded_without_pending_ts_clears_prior_stamp(self):
        """If a later run captures HTML, mark_downloaded is called with
        html_pending_ts=None (the default) and any prior pending stamp
        must be cleared."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2026, 3816, "[2026] HKCFI 3816", "T v T", "2026-07-01")
        db.mark_downloaded("hkcfi", 2026, 3816, ["doc"], html_pending_ts=1751600000)
        # Now HTML shows up on a later run:
        db.mark_downloaded("hkcfi", 2026, 3816, ["html", "txt", "json"])
        row = db._conn.execute(
            "SELECT html_pending_at_hklii FROM cases "
            "WHERE court='hkcfi' AND year=2026 AND number=3816"
        ).fetchone()
        assert row[0] is None, (
            "expected html_pending_at_hklii cleared after HTML capture"
        )

    def test_pending_html_recheck_returns_only_flagged_downloaded_rows(self):
        db = CheckpointDB(":memory:")
        # Row 1: html available on original download (flag is NULL) — should NOT appear
        db.upsert_case("hkcfi", 2025, 100, "[2025] HKCFI 100", "A v B", "2025-06-01")
        db.mark_downloaded("hkcfi", 2025, 100, ["html", "txt", "json"])
        # Row 2: doc-fallback taken (flag stamped) — SHOULD appear
        db.upsert_case("hkcfi", 2026, 3816, "[2026] HKCFI 3816", "T v T", "2026-07-01")
        db.mark_downloaded("hkcfi", 2026, 3816, ["doc"], html_pending_ts=1751600000)
        # Row 3: not yet downloaded — should NOT appear (status='pending')
        db.upsert_case("hkcfi", 2026, 3817, "[2026] HKCFI 3817", "X v Y", "2026-07-02")

        rows = db.pending_html_recheck()
        assert len(rows) == 1
        assert rows[0].court == "hkcfi"
        assert rows[0].year == 2026
        assert rows[0].number == 3816

    def test_pending_html_recheck_respects_limit(self):
        db = CheckpointDB(":memory:")
        for n in range(5):
            db.upsert_case("hkcfi", 2026, 3800 + n, f"[2026] HKCFI {3800 + n}", "T v T", "2026-07-01")
            db.mark_downloaded("hkcfi", 2026, 3800 + n, ["doc"], html_pending_ts=1751600000 + n)
        rows = db.pending_html_recheck(limit=3)
        assert len(rows) == 3


class TestOrphanDetectionAndMarking:
    """`hklii update --profile quarterly` marks rows upstream no longer
    lists as status='orphaned' (never deletes files).

    Detection: after a full-corpus enum bumps last_seen_at on every
    currently-listed row, anything with an older last_seen_at is stale
    and treated as orphaned.
    """

    def test_find_orphans_only_downloaded_returns_stale_downloaded_rows(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2020, 1, "[2020] X 1", "T", "2020-01-01",
                       last_seen_at=100)
        db.mark_downloaded("hkcfi", 2020, 1, ["html"])
        db.upsert_case("hkcfi", 2026, 1, "[2026] X 1", "T", "2026-07-01",
                       last_seen_at=1_000_000)
        db.mark_downloaded("hkcfi", 2026, 1, ["html"])
        orphans = db.find_orphans(as_of_ts=999_999, only_downloaded=True)
        assert len(orphans) == 1
        assert orphans[0].year == 2020

    def test_find_orphans_only_downloaded_excludes_pending(self):
        """Pending / failed rows aren't orphans under the strict filter —
        they just weren't finished. Only downloaded → orphaned."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2020, 1, "[2020] X 1", "T", "2020-01-01",
                       last_seen_at=100)
        # No mark_downloaded — stays pending
        orphans = db.find_orphans(as_of_ts=999_999, only_downloaded=True)
        assert orphans == []

    def test_mark_orphaned_excludes_row_from_downloaded_orphan_scan(self):
        """Observable behavior: after mark_orphaned, find_orphans(
        only_downloaded=True) no longer surfaces the row (it's no longer
        status='downloaded')."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2020, 1, "[2020] X 1", "T", "2020-01-01",
                       last_seen_at=100)
        db.mark_downloaded("hkcfi", 2020, 1, ["html"])
        # Before: findable as a downloaded orphan
        assert db.find_orphans(as_of_ts=999_999, only_downloaded=True)
        db.mark_orphaned("hkcfi", 2020, 1)
        # After: no longer downloaded → not surfaced
        assert db.find_orphans(as_of_ts=999_999, only_downloaded=True) == []

    def test_orphaned_rows_excluded_from_pending_html_recheck(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2020, 1, "[2020] X 1", "T", "2020-01-01",
                       last_seen_at=100)
        db.mark_downloaded("hkcfi", 2020, 1, ["doc"], html_pending_ts=1)
        db.mark_orphaned("hkcfi", 2020, 1)
        # Even though html_pending_at_hklii is stamped, orphaned rows
        # shouldn't appear in the recheck queue.
        assert db.pending_html_recheck() == []

    def test_mark_orphaned_below_ts_batch_flip(self):
        """Single-UPDATE batch variant used by hklii update — avoids the
        N-fsync problem of the per-row loop."""
        db = CheckpointDB(":memory:")
        # Stale downloaded row → orphan candidate
        db.upsert_case("hkcfi", 2020, 1, "[2020] X 1", "T", "2020-01-01",
                       last_seen_at=100)
        db.mark_downloaded("hkcfi", 2020, 1, ["html"])
        # Fresh downloaded row → keep
        db.upsert_case("hkcfi", 2026, 1, "[2026] X 1", "T", "2026-07-01",
                       last_seen_at=1_000_000)
        db.mark_downloaded("hkcfi", 2026, 1, ["html"])
        # Pending row → don't touch
        db.upsert_case("hkcfi", 2020, 2, "[2020] X 2", "T", "2020-01-01",
                       last_seen_at=100)
        n = db.mark_orphaned_below_ts(cutoff_ts=999_999)
        assert n == 1
        assert db.find_orphans(as_of_ts=999_999, only_downloaded=True) == []
        # Idempotent: re-running finds 0 more
        assert db.mark_orphaned_below_ts(cutoff_ts=999_999) == 0


class TestEnumRunGeneration:
    """`enum_runs` table anchors orphan_mark on an explicit 'this enum
    completed cleanly and covered N buckets' marker instead of scanning
    per-bucket last_seen_at timestamps."""

    def test_start_enum_run_returns_monotonic_id(self):
        db = CheckpointDB(":memory:")
        g1 = db.start_enum_run(["hkcfi"], ["en"])
        g2 = db.start_enum_run(["hkca"], ["en"])
        assert g2 > g1

    def test_incomplete_run_hidden_from_latest_completed(self):
        db = CheckpointDB(":memory:")
        db.start_enum_run(["hkcfi", "hkca"], ["en", "tc"])
        # NOT completed
        assert db.latest_completed_enum_run() is None

    def test_completed_run_surfaces_with_courts_and_langs(self):
        db = CheckpointDB(":memory:")
        g = db.start_enum_run(["hkcfi", "hkca"], ["en", "tc"])
        db.complete_enum_run(g)
        latest = db.latest_completed_enum_run()
        assert latest is not None
        assert latest["generation_id"] == g
        assert latest["courts"] == ["hkcfi", "hkca"]
        assert latest["langs"] == ["en", "tc"]
        assert latest["completed_at"] is not None

    def test_latest_returns_most_recent_completed(self):
        db = CheckpointDB(":memory:")
        g1 = db.start_enum_run(["hkcfi"], ["en"])
        db.complete_enum_run(g1)
        # Second sweep starts but doesn't finish → latest is still g1
        db.start_enum_run(["hkca"], ["en"])
        latest = db.latest_completed_enum_run()
        assert latest["generation_id"] == g1
        # Third sweep completes → new latest
        g3 = db.start_enum_run(["hkdc"], ["en"])
        db.complete_enum_run(g3)
        assert db.latest_completed_enum_run()["generation_id"] == g3


class TestEnumRunFullCorpusFiltering:
    """`latest_completed_enum_run()` is orphan_mark's ground truth for
    'a clean full-corpus sweep just finished'. A narrow-window enum
    (daily/weekly/monthly scrape) that touched every court/lang bucket
    but ONLY for rows dated in the last 30/90 days looks byte-identical
    in the pre-fix schema — same courts_json, same langs_json, same
    completed_at — but its `started_at` bound would mass-orphan every
    downloaded row older than the window when a subsequent
    full_reconcile fails and orphan_mark falls back to it.

    Guard: `start_enum_run` records the window boundary strings, and
    `latest_completed_enum_run` filters out any row whose window is
    non-NULL. Only true full-corpus sweeps qualify as orphan_mark's
    reference generation.
    """

    def test_narrow_window_completed_run_not_returned(self):
        db = CheckpointDB(":memory:")
        g = db.start_enum_run(
            ["hkcfi", "hkca"], ["en", "tc"],
            min_date_text="06/06/2026",
            max_date_text="06/07/2026",
        )
        db.complete_enum_run(g)
        # Narrow-window completion must NOT surface as orphan_mark's
        # reference sweep.
        assert db.latest_completed_enum_run() is None

    def test_full_corpus_completed_run_still_returned(self):
        db = CheckpointDB(":memory:")
        g = db.start_enum_run(
            ["hkcfi", "hkca"], ["en", "tc"],
            min_date_text=None, max_date_text=None,
        )
        db.complete_enum_run(g)
        latest = db.latest_completed_enum_run()
        assert latest is not None
        assert latest["generation_id"] == g

    def test_full_corpus_preferred_over_more_recent_narrow(self):
        """A recent narrow scrape must not shadow an older full-corpus
        sweep — the full-corpus row remains the reference for orphan_mark
        even if a daily-narrow ran after it."""
        db = CheckpointDB(":memory:")
        g_full = db.start_enum_run(
            ["hkcfi"], ["en"],
            min_date_text=None, max_date_text=None,
        )
        db.complete_enum_run(g_full)
        # Then a narrow-window sweep completes after it.
        g_narrow = db.start_enum_run(
            ["hkcfi"], ["en"],
            min_date_text="06/06/2026",
            max_date_text="06/07/2026",
        )
        db.complete_enum_run(g_narrow)
        assert db.latest_completed_enum_run()["generation_id"] == g_full

    def test_partial_window_bounds_still_narrow(self):
        """Even a one-sided window (only min or only max) counts as
        narrow — HKLII paginates 'newer than X' or 'older than X' the
        same way, so orphan_mark can't trust either."""
        db = CheckpointDB(":memory:")
        g_min_only = db.start_enum_run(
            ["hkcfi"], ["en"],
            min_date_text="06/06/2026", max_date_text=None,
        )
        db.complete_enum_run(g_min_only)
        assert db.latest_completed_enum_run() is None

        g_max_only = db.start_enum_run(
            ["hkcfi"], ["en"],
            min_date_text=None, max_date_text="06/07/2026",
        )
        db.complete_enum_run(g_max_only)
        assert db.latest_completed_enum_run() is None


class TestEnumRunLegacyMigration:
    """Pre-fix DBs may have enum_runs rows whose provenance can't be
    recovered — the pre-fix `start_enum_run` recorded only (courts,
    langs) and any completed row could have been either a full-corpus
    sweep OR a narrow-window daily/weekly/monthly scrape. When
    `_migrate_enum_runs_window_columns` ALTERs in the new columns,
    SQLite fills them with NULL — indistinguishable from a fresh
    post-fix full-corpus row.

    Guard: on the ADD COLUMN migration, invalidate `completed_at` on
    every pre-existing row so `latest_completed_enum_run()` can't use
    them as orphan_mark's reference. Users on legacy DBs must run one
    fresh full_reconcile before orphan_mark is safe again — a small
    cost for silent-corpus-damage safety.
    """

    def test_legacy_completed_row_invalidated_after_migration(self, tmp_path):
        import json, sqlite3
        db_path = tmp_path / ".checkpoint.db"

        # Materialize the pre-fix enum_runs schema (5 columns, no window).
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            "CREATE TABLE cases (court TEXT NOT NULL, year INTEGER NOT NULL, "
            "number INTEGER NOT NULL, neutral TEXT, title TEXT, date TEXT, "
            "status TEXT DEFAULT 'pending', formats TEXT, error TEXT, "
            "PRIMARY KEY (court, year, number));\n"
            "CREATE TABLE enum_runs ("
            "generation_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "started_at INTEGER NOT NULL, completed_at INTEGER, "
            "courts_json TEXT NOT NULL, langs_json TEXT NOT NULL);"
        )
        # Pre-fix ship's `_run_update_scrape` stamped completed rows with
        # courts=ALL_COURTS, langs=("en","tc") for BOTH narrow (daily)
        # and full-corpus (full_reconcile) sweeps — indistinguishable
        # here without a window field.
        raw.execute(
            "INSERT INTO enum_runs "
            "(started_at, completed_at, courts_json, langs_json) "
            "VALUES (?, ?, ?, ?)",
            (1000, 1300, json.dumps(["hkcfi", "hkca"]), json.dumps(["en", "tc"])),
        )
        raw.commit()
        raw.close()

        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(db_path))
        try:
            # Legacy row's completed_at MUST be nuked so orphan_mark
            # can't consume it — its provenance is ambiguous.
            assert db.latest_completed_enum_run() is None, (
                "legacy enum_run surfaced as full-corpus reference — "
                "orphan_mark would mass-orphan on partial full_reconcile"
            )
        finally:
            db.close()

    def test_post_migration_full_corpus_row_still_returned(self, tmp_path):
        """After the migration, a fresh post-fix full-corpus sweep is
        still consumable — the migration must invalidate ONLY legacy
        rows, not block genuine post-fix references."""
        import json, sqlite3
        db_path = tmp_path / ".checkpoint.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            "CREATE TABLE enum_runs ("
            "generation_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "started_at INTEGER NOT NULL, completed_at INTEGER, "
            "courts_json TEXT NOT NULL, langs_json TEXT NOT NULL);"
        )
        raw.execute(
            "INSERT INTO enum_runs "
            "(started_at, completed_at, courts_json, langs_json) "
            "VALUES (?, ?, ?, ?)",
            (1000, 1300, json.dumps(["hkcfi"]), json.dumps(["en"])),
        )
        raw.commit()
        raw.close()

        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(db_path))
        try:
            # Legacy row invalidated (completed_at now NULL).
            assert db.latest_completed_enum_run() is None
            # Fresh post-fix full-corpus enum → surfaces normally.
            g = db.start_enum_run(
                ["hkcfi"], ["en"],
                min_date_text=None, max_date_text=None,
            )
            db.complete_enum_run(g)
            latest = db.latest_completed_enum_run()
            assert latest is not None
            assert latest["generation_id"] == g
        finally:
            db.close()

    def test_migration_idempotent_on_reopen(self, tmp_path):
        """Reopening a DB that already went through the migration must
        NOT invalidate post-fix rows on every open — the completed_at
        nuke fires only when the columns are actually being ADDED."""
        from hklii_downloader.checkpoint import CheckpointDB
        db_path = tmp_path / ".checkpoint.db"
        # First open creates the schema WITH window columns present.
        db = CheckpointDB(str(db_path))
        try:
            g = db.start_enum_run(
                ["hkcfi"], ["en"],
                min_date_text=None, max_date_text=None,
            )
            db.complete_enum_run(g)
        finally:
            db.close()
        # Second open — migration must be a no-op.
        db2 = CheckpointDB(str(db_path))
        try:
            latest = db2.latest_completed_enum_run()
            assert latest is not None
            assert latest["generation_id"] == g
        finally:
            db2.close()


class TestResetRelatedcapFetches:
    """`hklii update --profile quarterly` calls this to force a fresh
    getrelatedcaps diff. Must be idempotent w.r.t. edges (INSERT OR
    IGNORE elsewhere) and safe when the table doesn't exist yet."""

    def test_resets_ok_rows_to_pending(self):
        db = CheckpointDB(":memory:")
        db.upsert_relatedcap_fetch("32", "ord", "en")
        db.mark_relatedcap_ok("32", "ord", "en", edge_count=4, fetched_at="x")
        db.upsert_relatedcap_fetch("32", "reg", "en")
        db.mark_relatedcap_ok("32", "reg", "en", edge_count=0, fetched_at="x")
        n = db.reset_relatedcap_fetches()
        assert n == 2
        stats = db.relatedcap_stats()
        assert stats["pending"] == 2
        assert stats["ok"] == 0

    def test_no_op_when_table_missing(self, tmp_path):
        """A fresh DB with no relatedcap_fetches table yet must not raise."""
        import sqlite3
        db_path = tmp_path / "cp.db"
        # Build a bare cases-only schema without relatedcap_fetches
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE cases (court TEXT, year INT, number INT, "
            "neutral TEXT, title TEXT, date TEXT, status TEXT DEFAULT 'pending', "
            "formats TEXT, error TEXT, lang TEXT DEFAULT 'en', "
            "PRIMARY KEY (court, year, number))"
        )
        conn.commit()
        conn.close()
        # Drop the table CheckpointDB would create on open, to simulate
        # 'never scraped relatedcaps'.
        db = CheckpointDB(str(db_path))
        db._conn.execute("DROP TABLE IF EXISTS relatedcap_fetches")
        db._conn.commit()
        # Must not raise
        assert db.reset_relatedcap_fetches() == 0


class TestPendingHtmlRecheckMaxAge:
    """`hklii update daily` bounds recheck-html by CASE DATE (not stamp) so we
    don't waste calls on ancient rows where HKLII has permanently declined to
    render HTML. The stamp (`html_pending_at_hklii`) itself bumps forward on
    every re-poll, so it's not a stable 'give up trying this row' signal —
    the case's own `date` column is."""

    _TODAY = "2026-07-06"

    def _seed(self, db, court, year, num, case_date, pending_ts):
        db.upsert_case(court, year, num, f"[{year}] X {num}", "T v T", case_date)
        db.mark_downloaded(court, year, num, ["doc"], html_pending_ts=pending_ts)

    def test_max_age_none_returns_all(self):
        """Back-compat: default kwarg absent → identical to today's behaviour."""
        db = CheckpointDB(":memory:")
        self._seed(db, "hkcfi", 2026, 1, "2026-07-01", 1)
        self._seed(db, "hkcfi", 2020, 1, "2020-01-01", 2)
        rows = db.pending_html_recheck()
        assert len(rows) == 2

    def test_max_age_zero_returns_all(self):
        """0 = unlimited (used by quarterly profile)."""
        db = CheckpointDB(":memory:")
        self._seed(db, "hkcfi", 2026, 1, "2026-07-01", 1)
        self._seed(db, "hkcfi", 2020, 1, "2020-01-01", 2)
        rows = db.pending_html_recheck(max_age_days=0, _today_iso=self._TODAY)
        assert len(rows) == 2

    def test_max_age_30_excludes_older_case_dates(self):
        db = CheckpointDB(":memory:")
        self._seed(db, "hkcfi", 2026, 1, "2026-07-01", 1)   # 5 days ago — include
        self._seed(db, "hkcfi", 2026, 2, "2026-05-01", 2)   # ~66 days ago — exclude
        self._seed(db, "hkcfi", 2020, 1, "2020-01-01", 3)   # ancient — exclude
        rows = db.pending_html_recheck(max_age_days=30, _today_iso=self._TODAY)
        assert len(rows) == 1
        assert rows[0].year == 2026 and rows[0].number == 1

    def test_max_age_30_boundary_includes_exact_cutoff(self):
        """Cases dated exactly (today - N days) should be included."""
        db = CheckpointDB(":memory:")
        self._seed(db, "hkcfi", 2026, 1, "2026-06-06", 1)   # exactly 30 days
        self._seed(db, "hkcfi", 2026, 2, "2026-06-05", 2)   # 31 days — exclude
        rows = db.pending_html_recheck(max_age_days=30, _today_iso=self._TODAY)
        assert {r.number for r in rows} == {1}

    def test_order_by_pending_ts_ascending_within_age_window(self):
        """Age filter narrows the queue; order-by remains oldest-stamp-first."""
        db = CheckpointDB(":memory:")
        self._seed(db, "hkcfi", 2026, 10, "2026-07-01", pending_ts=200)
        self._seed(db, "hkcfi", 2026, 11, "2026-07-02", pending_ts=100)  # older stamp
        self._seed(db, "hkcfi", 2026, 12, "2026-05-01", pending_ts=50)   # out of window
        rows = db.pending_html_recheck(max_age_days=30, _today_iso=self._TODAY)
        assert [r.number for r in rows] == [11, 10]


class TestLockFallbackWarning:
    """S-4: silently swallowing an OSError when creating the .lock file
    means two concurrent scrape processes race with no warning. If the
    filesystem can't host the lock (e.g. NFS without lockd, some FUSE
    mounts, read-only mounts), we must at least tell the operator."""

    def test_oserror_creating_lock_file_logs_warning(self, tmp_path, caplog):
        db_path = str(tmp_path / "checkpoint.db")
        real_open = __import__("os").open

        def fake_open(path, flags, *a, **kw):
            if str(path).endswith(".lock"):
                raise OSError(30, "Read-only file system")
            return real_open(path, flags, *a, **kw)

        with caplog.at_level(logging.WARNING, logger="hklii_downloader.checkpoint"):
            with patch("hklii_downloader.checkpoint.os.open", side_effect=fake_open):
                db = CheckpointDB(db_path)

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "lock" in r.getMessage().lower()
        ]
        assert warnings, (
            f"expected a WARNING log about the lock fallback; got records: "
            f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        db.close()


class TestCheckpointDB:
    def test_upsert_and_stats(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1234, "[2023] HKCFI 1234", "Test v Test", "2023-06-15")
        stats = db.stats()
        assert stats["total"] == 1
        assert stats["pending"] == 1

    def test_upsert_is_idempotent(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        assert db.stats()["total"] == 1

    def test_claim_pending_returns_case(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        record = db.claim_pending()
        assert record is not None
        assert record.court == "hkcfi"
        assert record.year == 2023
        assert record.number == 1
        assert record.status == "in_progress"

    def test_claim_pending_atomic(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        first = db.claim_pending()
        second = db.claim_pending()
        assert first is not None
        assert second is None

    def test_claim_pending_filters_by_court(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A", "2023-01-01")
        db.upsert_case("hkca", 2023, 1, "[2023] HKCA 1", "B", "2023-01-01")
        record = db.claim_pending(court="hkca")
        assert record is not None
        assert record.court == "hkca"

    def test_mark_downloaded(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html", "txt"])
        stats = db.stats()
        assert stats["downloaded"] == 1
        assert stats["pending"] == 0

    def test_mark_failed(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 1, "404 Not Found")
        stats = db.stats()
        assert stats["failed"] == 1

    def test_release_in_progress(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A v B", "2023-01-01")
        db.claim_pending()
        assert db.stats()["in_progress"] == 1
        db.release_in_progress()
        assert db.stats()["in_progress"] == 0
        assert db.stats()["pending"] == 1

    def test_pending_cases(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "[2023] HKCFI 1", "A", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 2, "[2023] HKCFI 2", "B", "2023-01-02")
        db.upsert_case("hkca", 2023, 1, "[2023] HKCA 1", "C", "2023-01-03")
        cases = db.pending_cases()
        assert len(cases) == 3
        hkcfi_cases = db.pending_cases(courts=["hkcfi"])
        assert len(hkcfi_cases) == 2

    def test_wal_mode_enabled(self):
        db = CheckpointDB(":memory:")
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal" or mode == "memory"

    def test_case_record_fields(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 42, "[2023] HKCFI 42", "Test Case", "2023-06-15")
        record = db.claim_pending()
        assert record.neutral == "[2023] HKCFI 42"
        assert record.title == "Test Case"
        assert record.date == "2023-06-15"

    def test_stats_all_statuses(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "N1", "T1", "2023-01-01")
        db.upsert_case("hkcfi", 2023, 2, "N2", "T2", "2023-01-02")
        db.upsert_case("hkcfi", 2023, 3, "N3", "T3", "2023-01-03")
        db.upsert_case("hkcfi", 2023, 4, "N4", "T4", "2023-01-04")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html"])
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 2, "error")
        db.claim_pending()
        stats = db.stats()
        assert stats == {"total": 4, "pending": 1, "in_progress": 1, "downloaded": 1, "failed": 1}

    def test_close(self):
        db = CheckpointDB(":memory:")
        db.close()


class TestLastEnumerationTs:
    def test_returns_none_when_never_enumerated(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        assert db.last_enumeration_ts("hkcfi", "en") is None

    def test_returns_max_last_seen_at_for_court(self, tmp_path):
        """Spec change (2026-07-07 whole-codebase review): last_enum-
        eration_ts now returns MAX across every lang for the court,
        regardless of the `lang` parameter. upsert_case collapses
        bilingual cases to lang='en'; a strict per-lang filter would
        return None for tc queries on bilingual-only courts and
        silently disable the scraper's enum-cache skip for the tc pass.
        """
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       lang="en", last_seen_at=1000)
        db.upsert_case("hkcfi", 2023, 2, "N", "T", "2023-01-01",
                       lang="en", last_seen_at=2000)
        db.upsert_case("hkcfi", 2023, 3, "N", "T", "2023-01-01",
                       lang="tc", last_seen_at=500)
        # Both queries return the court-wide MAX now.
        assert db.last_enumeration_ts("hkcfi", "en") == 2000
        assert db.last_enumeration_ts("hkcfi", "tc") == 2000

    def test_returns_none_when_all_rows_null(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        # last_seen_at defaults to None
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01", lang="en")
        assert db.last_enumeration_ts("hkcfi", "en") is None


class TestFreshnessAndOrphans:
    def test_upsert_sets_last_seen_at(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       last_seen_at=1700000000)
        row = db._conn.execute(
            "SELECT last_seen_at FROM cases WHERE court='hkcfi' AND number=1"
        ).fetchone()
        assert row[0] == 1700000000

    def test_reupsert_updates_last_seen_at(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       last_seen_at=1000)
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       last_seen_at=2000)
        row = db._conn.execute(
            "SELECT last_seen_at FROM cases WHERE court='hkcfi' AND number=1"
        ).fetchone()
        assert row[0] == 2000

    def test_find_orphans_returns_rows_older_than_ts(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / "cp.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01",
                       last_seen_at=1000)
        db.upsert_case("hkcfi", 2023, 2, "N", "T", "2023-01-01",
                       last_seen_at=2000)
        orphans = db.find_orphans(as_of_ts=1500)
        assert len(orphans) == 1
        assert orphans[0].number == 1

    def test_find_orphans_migration_treats_missing_ts_as_orphan(self, tmp_path):
        """Rows migrated from an old DB (no last_seen_at) should surface
        as orphans on the first freshness check so they get re-enumerated."""
        import sqlite3
        db_path = tmp_path / "cp.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE cases (
            court TEXT NOT NULL, year INTEGER NOT NULL, number INTEGER NOT NULL,
            neutral TEXT NOT NULL, title TEXT NOT NULL, date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            formats TEXT, error TEXT,
            PRIMARY KEY (court, year, number))""")
        conn.execute("INSERT INTO cases VALUES ('hkcfi',2023,1,'N','T','2023-01-01','pending',NULL,NULL)")
        conn.commit()
        conn.close()

        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(db_path))
        orphans = db.find_orphans(as_of_ts=1000)
        assert len(orphans) == 1


class TestVerifyDownloaded:
    def test_missing_file_flips_row_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        (out / "hkcfi" / "2023").mkdir(parents=True)

        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html", "txt", "json"])
        # No files actually written
        broken = db.verify_downloaded_against_files(out)
        assert broken == 1
        stats = db.stats()
        assert stats["pending"] == 1
        assert stats["downloaded"] == 0

    def test_zero_byte_file_flips_row_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        d = out / "hkcfi" / "2023"
        d.mkdir(parents=True)
        (d / "hkcfi_2023_1.html").write_text("")  # 0-byte
        (d / "hkcfi_2023_1.txt").write_text("body")
        (d / "hkcfi_2023_1.json").write_text("{}")

        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html", "txt", "json"])

        broken = db.verify_downloaded_against_files(out)
        assert broken == 1
        assert db.stats()["pending"] == 1

    def test_intact_files_left_alone(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        d = out / "hkcfi" / "2023"
        d.mkdir(parents=True)
        (d / "hkcfi_2023_1.html").write_text("body")
        (d / "hkcfi_2023_1.txt").write_text("body")
        (d / "hkcfi_2023_1.json").write_text("{}")

        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html", "txt", "json"])

        broken = db.verify_downloaded_against_files(out)
        assert broken == 0
        assert db.stats()["downloaded"] == 1

    def test_fmt_doc_accepts_rtf_on_disk(self, tmp_path):
        """Regression from whole-codebase review (L2 semantic drift):
        scraper._fetch_doc records fmt='doc' regardless of on-disk
        extension (Judiciary serves RTF at .doc URLs — task #67),
        and validate.py already probes .doc/.docx/.rtf via
        _DOC_FAMILY_EXTS. verify_downloaded_against_files was checking
        only .docx (with .doc fallback), so 19+ production rows
        (hkcfi/1998/78, hkca/2002/232, ...) with .rtf on disk would
        be falsely flipped to pending on `hklii verify`."""
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        d = out / "hkcfi" / "1998"
        d.mkdir(parents=True)
        (d / "hkcfi_1998_78.rtf").write_bytes(
            b"{\\rtf1\\ansi some judgment body}"
        )
        (d / "hkcfi_1998_78.html").write_text("body")
        (d / "hkcfi_1998_78.txt").write_text("body")
        (d / "hkcfi_1998_78.json").write_text("{}")

        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 1998, 78, "N", "T", "1998-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 1998, 78, ["html", "txt", "json", "doc"])

        broken = db.verify_downloaded_against_files(out)
        assert broken == 0, (
            "fmt='doc' with .rtf on disk was falsely flipped to pending "
            "— verify must recognize .doc/.docx/.rtf as valid doc-family "
            "presence signals (matches _DOC_FAMILY_EXTS used by validate.py "
            "and html_generator.py)"
        )
        assert db.stats()["downloaded"] == 1

    def test_fmt_doc_missing_all_three_variants_still_flips(self, tmp_path):
        """Sibling positive test: if none of .doc/.docx/.rtf exist, the
        row IS broken and should flip. Otherwise the fix would silently
        skip every fmt='doc' row regardless of on-disk state."""
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        d = out / "hkcfi" / "2020"
        d.mkdir(parents=True)
        (d / "hkcfi_2020_1.html").write_text("body")
        (d / "hkcfi_2020_1.txt").write_text("body")
        (d / "hkcfi_2020_1.json").write_text("{}")
        # No .doc / .docx / .rtf

        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2020, 1, "N", "T", "2020-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2020, 1, ["html", "txt", "json", "doc"])

        broken = db.verify_downloaded_against_files(out)
        assert broken == 1
        assert db.stats()["pending"] == 1


class TestReleaseInProgressPerDomain:
    """Every runner that flips rows to status='in_progress' via a
    claim_pending_X() atomic transition needs a matching
    release_in_progress_X() so that a worker crash mid-processing
    doesn't permanently orphan the row. Without it, the row stays
    at 'in_progress' forever, no future run picks it up, and the
    scrape silently under-reports coverage."""

    def test_hopt_release_flips_in_progress_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_hopt_document("bacpg", 2020, "1", "en", "Title", "2020-01-01")
        claimed = db.claim_pending_hopt()
        assert claimed is not None
        # Worker crashes → row stays in_progress
        db.release_in_progress_hopt()
        again = db.claim_pending_hopt()
        assert again is not None, (
            "release_in_progress_hopt didn't recover the crashed row"
        )

    def test_noteup_release_flips_in_progress_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.mark_downloaded("hkcfi", 2023, 1, ["html"])
        db.upsert_noteup_fetch("hkcfi", 2023, 1)
        claimed = db.claim_pending_noteup()
        assert claimed is not None
        db.release_in_progress_noteup()
        again = db.claim_pending_noteup()
        assert again is not None

    def test_relatedcap_release_flips_in_progress_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_relatedcap_fetch("32", "ord", "en")
        claimed = db.claim_pending_relatedcap()
        assert claimed is not None
        db.release_in_progress_relatedcap()
        again = db.claim_pending_relatedcap()
        assert again is not None

    def test_legis_release_flips_in_progress_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_legis_document("ord", "1", "en", "Cap 1", "2020-01-01")
        claimed = db.claim_pending_legis()
        assert claimed is not None
        db.release_in_progress_legis()
        again = db.claim_pending_legis()
        assert again is not None

    def test_legis_version_release_flips_in_progress_to_pending(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        db.upsert_legis_document("ord", "1", "en", "Cap 1", "2020-01-01")
        db.upsert_legis_version("ord", "1", "en", 1, "2020-01-01")
        claimed = db.claim_pending_legis_version()
        assert claimed is not None
        db.release_in_progress_legis_version()
        again = db.claim_pending_legis_version()
        assert again is not None


class TestLastEnumerationTsBilingualCollapse:
    """Whole-codebase review (L2 semantic drift): upsert_case collapses
    bilingual cases to lang='en' (checkpoint.py:329-332). last_enumer-
    ation_ts filters WHERE lang=? — so a court whose only cases are
    bilingual returns None for last_enumeration_ts(court, 'tc') even
    though those cases WERE enumerated under the tc pass.

    Scraper.py:194's enum-cache skip is driven by that value, so the
    tc pass never gets its cache hit — every bilingual-only court
    re-enumerates its tc bucket on every run, wasting one full
    enumeration per invocation. Same shape as the coverage_canary
    bilingual-TC finding, hit from the enum-cache side.

    Guard: last_enumeration_ts must consider bilingual cases as having
    been enumerated under BOTH langs — MAX(last_seen_at) across every
    lang for the court is the right shape, since one enumeration bumps
    the same row regardless of which lang pass it came from.
    """

    def test_bilingual_only_court_returns_ts_for_tc_query(self, tmp_path):
        import time
        from hklii_downloader.checkpoint import CheckpointDB

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        ts = int(time.time()) - 3600
        # Bilingual case: upsert en → then tc. UPSERT keeps lang='en'
        # but bumps last_seen_at on the tc pass.
        db.upsert_case("hkcfi", 2026, 1, "N", "T", "2026-01-01",
                       lang="en", last_seen_at=ts)
        db.upsert_case("hkcfi", 2026, 1, "N", "T", "2026-01-01",
                       lang="tc", last_seen_at=ts + 60)

        stored_lang = db._conn.execute(
            "SELECT lang FROM cases WHERE court='hkcfi' AND year=2026 "
            "AND number=1"
        ).fetchone()[0]
        assert stored_lang == "en"  # UPSERT collapse — precondition

        tc_ts = db.last_enumeration_ts("hkcfi", "tc")
        assert tc_ts is not None, (
            "last_enumeration_ts(court, 'tc') returned None even though "
            "the tc enumeration bumped last_seen_at on the bilingual row. "
            "Scraper.py's enum-cache skip driven by this value never "
            "fires for the tc pass on bilingual-only courts."
        )
        assert tc_ts >= ts, tc_ts


class TestIntegrityCheck:
    def test_healthy_db_opens_fine(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db_path = tmp_path / "cp.db"
        db = CheckpointDB(str(db_path))
        db.close()
        # And reopens cleanly
        db2 = CheckpointDB(str(db_path))
        db2.close()

    def test_corrupt_db_raises(self, tmp_path):
        """If _check_integrity finds anything but 'ok', __init__ must
        raise CheckpointCorruptError with the error text."""
        from unittest.mock import patch
        from hklii_downloader.checkpoint import (
            CheckpointDB, CheckpointCorruptError,
        )

        def bad_check(self, path):
            self._conn.close()
            raise CheckpointCorruptError(
                f"integrity_check failed for {path}: corruption in root page 3"
            )

        with patch.object(CheckpointDB, "_check_integrity", bad_check):
            raised = None
            try:
                CheckpointDB(str(tmp_path / "cp.db"))
            except CheckpointCorruptError as e:
                raised = e
        assert raised is not None
        assert "corruption" in str(raised)


class TestProcessLock:
    def test_second_open_on_same_path_raises(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB, CheckpointLockError

        db_path = tmp_path / "cp.db"
        first = CheckpointDB(str(db_path))
        raised = None
        try:
            CheckpointDB(str(db_path))
        except CheckpointLockError as e:
            raised = e
        assert raised is not None, (
            "opening the same checkpoint DB twice must raise CheckpointLockError"
        )
        first.close()

    def test_second_open_after_first_close_ok(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB

        db_path = tmp_path / "cp.db"
        first = CheckpointDB(str(db_path))
        first.close()
        # Now the lock is released
        second = CheckpointDB(str(db_path))
        second.close()

    def test_in_memory_db_does_not_lock(self):
        """`:memory:` DBs are per-process and shouldn't attempt file lock."""
        from hklii_downloader.checkpoint import CheckpointDB
        # Two in-memory instances open independently — no error
        a = CheckpointDB(":memory:")
        b = CheckpointDB(":memory:")
        a.close()
        b.close()


class TestRetryFailed:
    def test_reset_failed_to_pending(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 1, "HTTP 403")
        assert db.stats()["failed"] == 1
        n = db.reset_failed_to_pending()
        assert n == 1
        stats = db.stats()
        assert stats["pending"] == 1
        assert stats["failed"] == 0

    def test_reset_clears_error(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 1, "HTTP 403")
        db.reset_failed_to_pending()
        row = db._conn.execute(
            "SELECT error FROM cases WHERE court='hkcfi' AND year=2023 AND number=1"
        ).fetchone()
        assert row[0] is None

    def test_reset_no_failed_rows_is_noop(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        n = db.reset_failed_to_pending()
        assert n == 0


class TestLangColumn:
    """lang is stored per-case, default 'en'. Enumeration sweeps both
    languages and dedupes by (court, year, number) preferring en."""

    def test_new_case_defaults_to_en(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkdc", 2026, 5, "N", "T", "2026-01-01")
        rec = db.claim_pending()
        assert rec.lang == "en"

    def test_upsert_case_accepts_lang(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkdc", 2026, 5, "N", "T", "2026-01-01", lang="tc")
        rec = db.claim_pending()
        assert rec.lang == "tc"

    def test_en_wins_over_tc_when_both_present(self):
        """Enumerate en first, tc second — en wins."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2026, 100, "N", "T-en", "2026-01-01", lang="en")
        db.upsert_case("hkcfi", 2026, 100, "N", "T-tc", "2026-01-01", lang="tc")
        rec = db.claim_pending()
        assert rec.lang == "en"

    def test_en_wins_over_tc_regardless_of_order(self):
        """Enumerate tc first, en second — en still wins."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfi", 2026, 100, "N", "T-tc", "2026-01-01", lang="tc")
        db.upsert_case("hkcfi", 2026, 100, "N", "T-en", "2026-01-01", lang="en")
        rec = db.claim_pending()
        assert rec.lang == "en"

    def test_tc_only_case_stays_tc(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkdc", 2026, 5, "N", "T", "2026-01-01", lang="tc")
        rec = db.claim_pending()
        assert rec.lang == "tc"

    def test_migration_adds_lang_to_existing_db(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE cases (
            court TEXT NOT NULL, year INTEGER NOT NULL, number INTEGER NOT NULL,
            neutral TEXT NOT NULL, title TEXT NOT NULL, date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            formats TEXT, error TEXT,
            PRIMARY KEY (court, year, number))""")
        conn.execute("INSERT INTO cases VALUES ('hkcfi',2026,1,'N','T','2026-01-01','pending',NULL,NULL)")
        conn.commit()
        conn.close()

        db = CheckpointDB(str(db_path))
        rec = db.claim_pending()
        assert rec.lang == "en"


class TestEnrichmentStatus:
    """summary_en, summary_zh, appeal_history tracked independently."""

    def _seed(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 25, "[2026] HKCFA 25", "HKSAR v X", "2026-06-17")
        return db

    def test_new_case_has_pending_enrichment(self):
        db = self._seed()
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row == {
            "summary_en": "pending",
            "summary_zh": "pending",
            "appeal_history": "pending",
        }

    def test_mark_enrichment_downloaded(self):
        db = self._seed()
        db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "downloaded")
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "downloaded"
        assert row["summary_zh"] == "pending"
        assert row["appeal_history"] == "pending"

    def test_mark_enrichment_na(self):
        db = self._seed()
        db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "na")
        db.mark_enrichment("hkcfa", 2026, 25, "summary_zh", "na")
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["summary_en"] == "na"
        assert row["summary_zh"] == "na"

    def test_mark_enrichment_failed_with_error(self):
        db = self._seed()
        db.mark_enrichment(
            "hkcfa", 2026, 25, "appeal_history", "failed",
            error="ConnectTimeout after 3 retries",
        )
        row = db.get_enrichment("hkcfa", 2026, 25)
        assert row["appeal_history"] == "failed"
        errs = db.get_enrichment_errors("hkcfa", 2026, 25)
        assert "appeal_history" in errs
        assert "ConnectTimeout" in errs["appeal_history"]

    def test_pending_enrichment_iterates_only_pending(self):
        """Enrichment only applies to cases whose judgment is already
        downloaded — you can't extract summary URLs from a file you don't
        have. So pending_enrichment filters by both."""
        db = CheckpointDB(":memory:")
        for i in range(3):
            db.upsert_case("hkcfa", 2026, i+1, f"N{i+1}", f"T{i+1}", "2026-01-01")
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, i+1, ["html"])
        db.mark_enrichment("hkcfa", 2026, 1, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 2, "summary_en", "na")
        pending = db.pending_enrichment("summary_en")
        nums = sorted(r.number for r in pending)
        assert nums == [3]

    def test_pending_enrichment_excludes_undownloaded_cases(self):
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 1, "N1", "T1", "2026-01-01")
        db.upsert_case("hkcfa", 2026, 2, "N2", "T2", "2026-01-02")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        pending = db.pending_enrichment("summary_en")
        assert [r.number for r in pending] == [1]

    def test_enrichment_stats_reports_counts(self):
        db = CheckpointDB(":memory:")
        for i in range(4):
            db.upsert_case("hkcfa", 2026, i+1, f"N{i+1}", f"T{i+1}", "2026-01-01")
        db.mark_enrichment("hkcfa", 2026, 1, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 2, "summary_en", "downloaded")
        db.mark_enrichment("hkcfa", 2026, 3, "summary_en", "na")
        db.mark_enrichment("hkcfa", 2026, 4, "summary_en", "failed")
        stats = db.enrichment_stats()
        assert stats["summary_en"] == {
            "pending": 0, "downloaded": 2, "na": 1, "failed": 1,
        }

    def test_invalid_enrichment_kind_raises(self):
        db = self._seed()
        with pytest.raises(ValueError, match="kind"):
            db.mark_enrichment("hkcfa", 2026, 25, "not_a_kind", "downloaded")

    def test_invalid_enrichment_status_raises(self):
        db = self._seed()
        with pytest.raises(ValueError, match="status"):
            db.mark_enrichment("hkcfa", 2026, 25, "summary_en", "weird")

    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE cases (
            court TEXT NOT NULL, year INTEGER NOT NULL, number INTEGER NOT NULL,
            neutral TEXT NOT NULL, title TEXT NOT NULL, date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            formats TEXT, error TEXT,
            PRIMARY KEY (court, year, number))""")
        conn.execute("INSERT INTO cases VALUES ('hkcfa', 2026, 1, 'N1', 'T1', '2026-01-01', 'downloaded', NULL, NULL)")
        conn.commit()
        conn.close()

        db = CheckpointDB(str(db_path))
        row = db.get_enrichment("hkcfa", 2026, 1)
        assert row == {
            "summary_en": "pending",
            "summary_zh": "pending",
            "appeal_history": "pending",
        }
