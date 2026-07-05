"""Tests for html_generated_from / html_generated_error checkpoint columns.

These columns track which rows have had their doc-family file converted
to a `.generated.html` sidecar (task #76). Sits alongside the existing
per-enrichment-kind columns without disturbing them.
"""
from __future__ import annotations

from hklii_downloader.checkpoint import CheckpointDB


def _seed(db: CheckpointDB, court, year, number, formats):
    db.upsert_case(court, year, number, f"[{year}] X {number}",
                   "T", "2023-01-01")
    db.mark_downloaded(court, year, number, formats)


class TestHtmlGeneratedColumns:
    def test_columns_present_on_fresh_db(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {
                row[1] for row in
                db._conn.execute("PRAGMA table_info(cases)").fetchall()
            }
            assert "html_generated_from" in cols
            assert "html_generated_error" in cols
        finally:
            db.close()

    def test_columns_added_by_migration_on_legacy_db(self, tmp_path):
        """A DB whose schema was created before the migration must gain
        the new columns via _migrate_enrichment_columns on open."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE cases (
                court TEXT NOT NULL,
                year INTEGER NOT NULL,
                number INTEGER NOT NULL,
                neutral TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                formats TEXT,
                error TEXT,
                PRIMARY KEY (court, year, number)
            )
        """)
        conn.commit()
        conn.close()

        db = CheckpointDB(str(db_path))
        try:
            cols = {
                row[1] for row in
                db._conn.execute("PRAGMA table_info(cases)").fetchall()
            }
            assert "html_generated_from" in cols
            assert "html_generated_error" in cols
        finally:
            db.close()

    def test_mark_html_generated_writes_source_ext(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            db.mark_html_generated("hkcfi", 2026, 1, source_ext=".docx")

            row = db._conn.execute(
                "SELECT html_generated_from, html_generated_error "
                "FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] == ".docx"
            assert row[1] is None
        finally:
            db.close()

    def test_mark_html_generation_failed_records_error(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            db.mark_html_generation_failed(
                "hkcfi", 2026, 1,
                error="pandoc cannot read OLE .doc",
            )

            row = db._conn.execute(
                "SELECT html_generated_from, html_generated_error "
                "FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] is None
            assert row[1] == "pandoc cannot read OLE .doc"
        finally:
            db.close()

    def test_mark_html_generated_clears_prior_error(self, tmp_path):
        """After a retry succeeds, the error field must be nulled so a
        third run doesn't misclassify the row."""
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            db.mark_html_generation_failed(
                "hkcfi", 2026, 1, error="first attempt failed",
            )
            db.mark_html_generated("hkcfi", 2026, 1, source_ext=".doc")

            row = db._conn.execute(
                "SELECT html_generated_from, html_generated_error "
                "FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] == ".doc"
            assert row[1] is None
        finally:
            db.close()


class TestPendingHtmlGeneration:
    def test_returns_only_doc_only_rows(self, tmp_path):
        """Rows targeted for html generation: formats=[\"doc\"] only, not
        yet processed. Rows with any of html/txt/json don't qualify."""
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            _seed(db, "hkcfi", 2026, 2, ["doc", "html", "json", "txt"])
            _seed(db, "hkcfi", 2026, 3, ["html", "json", "txt"])

            pending = db.pending_html_generation()
            keys = {(r.court, r.year, r.number) for r in pending}
            assert keys == {("hkcfi", 2026, 1)}
        finally:
            db.close()

    def test_excludes_already_generated_rows(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            _seed(db, "hkcfi", 2026, 2, ["doc"])
            db.mark_html_generated("hkcfi", 2026, 1, source_ext=".docx")

            pending = db.pending_html_generation()
            keys = {(r.court, r.year, r.number) for r in pending}
            assert keys == {("hkcfi", 2026, 2)}
        finally:
            db.close()

    def test_excludes_error_rows_by_default(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            _seed(db, "hkcfi", 2026, 2, ["doc"])
            db.mark_html_generation_failed("hkcfi", 2026, 1, error="unsupported")

            pending = db.pending_html_generation()
            keys = {(r.court, r.year, r.number) for r in pending}
            assert keys == {("hkcfi", 2026, 2)}
        finally:
            db.close()

    def test_include_failed_lets_retry_error_rows(self, tmp_path):
        """With include_failed=True (--force at the CLI), rows previously
        marked errored are re-selected for a retry pass."""
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            db.mark_html_generation_failed("hkcfi", 2026, 1, error="was broken")

            pending = db.pending_html_generation(include_failed=True)
            keys = {(r.court, r.year, r.number) for r in pending}
            assert keys == {("hkcfi", 2026, 1)}
        finally:
            db.close()


class TestHtmlGenerationStats:
    def test_reports_status_breakdown(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed(db, "hkcfi", 2026, 1, ["doc"])
            _seed(db, "hkcfi", 2026, 2, ["doc"])
            _seed(db, "hkcfi", 2026, 3, ["doc"])
            _seed(db, "hkcfi", 2026, 4, ["doc"])
            db.mark_html_generated("hkcfi", 2026, 1, source_ext=".docx")
            db.mark_html_generated("hkcfi", 2026, 2, source_ext=".doc")
            db.mark_html_generation_failed("hkcfi", 2026, 3, error="e")
            # #4 is pending

            stats = db.html_generation_stats()
            assert stats["generated"] == 2
            assert stats["failed"] == 1
            assert stats["pending"] == 1
            assert stats["by_source_ext"][".docx"] == 1
            assert stats["by_source_ext"][".doc"] == 1
        finally:
            db.close()
