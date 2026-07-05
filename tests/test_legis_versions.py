"""Tests for the legis_versions checkpoint table.

Historical-version backfill layer — one row per (abbr, num, lang, vid).
The current-in-force version already lives in legis_documents; this
table tracks the non-latest vids that get_capversiontoc will re-fetch
for full historical corpus.
"""
from __future__ import annotations

import pytest

from hklii_downloader.checkpoint import CheckpointDB


def _seed_doc(db, abbr="ord", num="1", lang="en"):
    db.upsert_legis_document(
        abbr=abbr, num=num, lang=lang, title="X", last_seen_at=0,
    )
    db.mark_legis_downloaded(
        abbr=abbr, num=num, lang=lang,
        latest_vid=52016, latest_version_date="2025-12-18",
        formats=["versions", "content"],
    )


class TestLegisVersionsSchema:
    def test_table_present(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            tables = {
                row[0] for row in
                db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "legis_versions" in tables
        finally:
            db.close()

    def test_columns(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            cols = {
                row[1] for row in
                db._conn.execute(
                    "PRAGMA table_info(legis_versions)"
                ).fetchall()
            }
            for name in (
                "abbr", "num", "lang", "vid",
                "version_date", "status", "error", "last_seen_at",
            ):
                assert name in cols, f"missing {name}"
        finally:
            db.close()


class TestLegisVersionsAccessors:
    def test_upsert_inserts_pending(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
                last_seen_at=1000,
            )
            row = db._conn.execute(
                "SELECT abbr, num, lang, vid, version_date, "
                "status, last_seen_at FROM legis_versions"
            ).fetchone()
            assert row == ("ord", "1", "en", 50293, "2024-08-18",
                           "pending", 1000)
        finally:
            db.close()

    def test_upsert_is_idempotent(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            for _ in range(3):
                db.upsert_legis_version(
                    abbr="ord", num="1", lang="en",
                    vid=50293, version_date="2024-08-18",
                    last_seen_at=2000,
                )
            n = db._conn.execute(
                "SELECT COUNT(*) FROM legis_versions"
            ).fetchone()[0]
            assert n == 1
            # last_seen_at updated
            ts = db._conn.execute(
                "SELECT last_seen_at FROM legis_versions"
            ).fetchone()[0]
            assert ts == 2000
        finally:
            db.close()

    def test_upsert_does_not_touch_status(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
            )
            db._conn.execute(
                "UPDATE legis_versions SET status='downloaded' "
                "WHERE abbr='ord' AND num='1' AND lang='en' AND vid=50293"
            )
            db._conn.commit()
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
                last_seen_at=3000,
            )
            row = db._conn.execute(
                "SELECT status, last_seen_at FROM legis_versions"
            ).fetchone()
            assert row == ("downloaded", 3000)
        finally:
            db.close()

    def test_claim_pending_flips_to_in_progress(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
            )
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=49871, version_date="2024-03-23",
            )
            rec = db.claim_pending_legis_version()
            assert rec is not None
            assert rec.abbr == "ord" and rec.num == "1" and rec.lang == "en"

            row = db._conn.execute(
                "SELECT status FROM legis_versions "
                "WHERE abbr=? AND num=? AND lang=? AND vid=?",
                (rec.abbr, rec.num, rec.lang, rec.vid),
            ).fetchone()
            assert row[0] == "in_progress"
        finally:
            db.close()

    def test_claim_pending_returns_none_when_empty(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            assert db.claim_pending_legis_version() is None
        finally:
            db.close()

    def test_mark_downloaded(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
            )
            db.mark_legis_version_downloaded(
                abbr="ord", num="1", lang="en", vid=50293,
            )
            row = db._conn.execute(
                "SELECT status, error FROM legis_versions"
            ).fetchone()
            assert row == ("downloaded", None)
        finally:
            db.close()

    def test_mark_failed(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            db.upsert_legis_version(
                abbr="ord", num="1", lang="en",
                vid=50293, version_date="2024-08-18",
            )
            db.mark_legis_version_failed(
                abbr="ord", num="1", lang="en", vid=50293,
                error="HTTP 500",
            )
            row = db._conn.execute(
                "SELECT status, error FROM legis_versions"
            ).fetchone()
            assert row == ("failed", "HTTP 500")
        finally:
            db.close()

    def test_stats(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            for vid, date in [(50293, "2024-08-18"), (49871, "2024-03-23"),
                              (47826, "2022-07-01")]:
                db.upsert_legis_version(
                    abbr="ord", num="1", lang="en",
                    vid=vid, version_date=date,
                )
            db.mark_legis_version_downloaded(
                abbr="ord", num="1", lang="en", vid=50293,
            )
            db.mark_legis_version_failed(
                abbr="ord", num="1", lang="en", vid=49871, error="e",
            )
            stats = db.legis_version_stats()
            assert stats["total"] == 3
            assert stats["downloaded"] == 1
            assert stats["failed"] == 1
            assert stats["pending"] == 1
        finally:
            db.close()

    def test_pending_legis_versions_lists_only_pending(self, tmp_path):
        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            _seed_doc(db)
            for vid in (50293, 49871, 47826):
                db.upsert_legis_version(
                    abbr="ord", num="1", lang="en",
                    vid=vid, version_date="x",
                )
            db.mark_legis_version_downloaded(
                abbr="ord", num="1", lang="en", vid=50293,
            )
            pending = db.pending_legis_versions()
            vids = {rec.vid for rec in pending}
            assert vids == {49871, 47826}
        finally:
            db.close()
