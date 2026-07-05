"""Tests for the legis_documents checkpoint table.

Parallel to the cases table but scoped to HK ordinances/regulations/
instruments. Every row is keyed by (abbr, num, lang) where abbr is
`ord`/`reg`/`instrument` (the API's capType), num is the chapter/rule
number as a string (e.g. `1`, `32`, `622C`), and lang is `en` or `tc`.
"""
from __future__ import annotations

import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestLegisSchema:
    def test_legis_documents_table_present(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            tables = {
                row[0] for row in
                db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "legis_documents" in tables
        finally:
            db.close()

    def test_legis_documents_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {
                row[1] for row in
                db._conn.execute(
                    "PRAGMA table_info(legis_documents)"
                ).fetchall()
            }
            for name in (
                "abbr", "num", "lang", "title",
                "latest_vid", "latest_version_date",
                "status", "formats", "error", "last_seen_at",
            ):
                assert name in cols, f"missing {name}"
        finally:
            db.close()


class TestLegisAccessors:
    def test_upsert_legis_document_inserts(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en",
                title="Interpretation and General Clauses Ordinance",
                last_seen_at=1234567890,
            )
            row = db._conn.execute(
                "SELECT abbr, num, lang, title, status, last_seen_at "
                "FROM legis_documents"
            ).fetchone()
            assert row == (
                "ord", "1", "en",
                "Interpretation and General Clauses Ordinance",
                "pending", 1234567890,
            )
        finally:
            db.close()

    def test_upsert_updates_title_and_last_seen_but_not_status(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en", title="Old Title",
                last_seen_at=1000,
            )
            # Manually advance status like a scraper would
            db._conn.execute(
                "UPDATE legis_documents SET status='downloaded' "
                "WHERE abbr='ord' AND num='1' AND lang='en'"
            )
            db._conn.commit()
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en", title="New Title",
                last_seen_at=2000,
            )
            row = db._conn.execute(
                "SELECT title, status, last_seen_at FROM legis_documents"
            ).fetchone()
            assert row == ("New Title", "downloaded", 2000)
        finally:
            db.close()

    def test_claim_pending_legis(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en", title="X",
            )
            db.upsert_legis_document(
                abbr="ord", num="2", lang="en", title="Y",
            )
            rec = db.claim_pending_legis()
            assert rec is not None
            assert rec.abbr == "ord"
            assert rec.status == "in_progress"

            row = db._conn.execute(
                "SELECT status FROM legis_documents "
                "WHERE abbr=? AND num=? AND lang=?",
                (rec.abbr, rec.num, rec.lang),
            ).fetchone()
            assert row[0] == "in_progress"
        finally:
            db.close()

    def test_mark_legis_downloaded_sets_status_and_formats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="1", lang="en", title="X",
            )
            db.mark_legis_downloaded(
                abbr="ord", num="1", lang="en",
                latest_vid=19113, latest_version_date="1997-06-30",
                formats=["versions", "content"],
            )
            row = db._conn.execute(
                "SELECT status, formats, latest_vid, latest_version_date "
                "FROM legis_documents"
            ).fetchone()
            import json
            assert row[0] == "downloaded"
            assert json.loads(row[1]) == ["versions", "content"]
            assert row[2] == 19113
            assert row[3] == "1997-06-30"
        finally:
            db.close()

    def test_mark_legis_failed(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(abbr="ord", num="1", lang="en", title="X")
            db.mark_legis_failed(
                abbr="ord", num="1", lang="en",
                error="HTTP 500 from getcapversions",
            )
            row = db._conn.execute(
                "SELECT status, error FROM legis_documents"
            ).fetchone()
            assert row[0] == "failed"
            assert "HTTP 500" in row[1]
        finally:
            db.close()

    def test_legis_stats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for i in range(3):
                db.upsert_legis_document(
                    abbr="ord", num=str(i), lang="en", title=f"T{i}",
                )
            db.mark_legis_downloaded(
                abbr="ord", num="0", lang="en",
                latest_vid=1, latest_version_date="2020-01-01",
                formats=["versions", "content"],
            )
            db.mark_legis_failed(
                abbr="ord", num="1", lang="en", error="e",
            )
            stats = db.legis_stats()
            assert stats["total"] == 3
            assert stats["downloaded"] == 1
            assert stats["failed"] == 1
            assert stats["pending"] == 1
        finally:
            db.close()

    def test_legis_stats_by_abbr(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(abbr="ord", num="1", lang="en", title="X")
            db.upsert_legis_document(abbr="ord", num="2", lang="en", title="Y")
            db.upsert_legis_document(abbr="reg", num="1", lang="en", title="R")
            stats = db.legis_stats_by_abbr()
            assert stats["ord"]["total"] == 2
            assert stats["reg"]["total"] == 1
        finally:
            db.close()
