"""Tests for LegisHistoryRunner — historical-version backfill.

Enumeration reads each row's on-disk versions.json and upserts every
non-latest vid. Fetch phase drains through async workers, saving
{stem}.v{vid}.content.json + marking downloaded/failed.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB


def _bootstrap_downloaded(db, output_dir: Path, abbr, num, lang,
                          versions):
    """Simulate a completed scrape-legis run for one row: DB row +
    versions.json on disk. The runner will read the versions.json and
    upsert every non-latest vid into legis_versions."""
    db.upsert_legis_document(
        abbr=abbr, num=num, lang=lang, title="X",
    )
    db.mark_legis_downloaded(
        abbr=abbr, num=num, lang=lang,
        latest_vid=versions[0]["id"],
        latest_version_date=versions[0].get("date", ""),
        formats=["versions", "content"],
    )
    base = output_dir / "legis" / abbr / num
    base.mkdir(parents=True, exist_ok=True)
    stem = f"{abbr}_{num}_{lang}"
    (base / f"{stem}.versions.json").write_text(
        json.dumps(versions, ensure_ascii=False),
    )


class TestEnumerateHistorical:
    def test_upserts_only_non_latest_vids(self, tmp_path):
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            versions = [
                {"id": 52016, "date": "2025-12-18T00:00:00+08:00"},
                {"id": 50293, "date": "2024-08-18T00:00:00+08:00"},
                {"id": 49871, "date": "2024-03-23T00:00:00+08:00"},
            ]
            _bootstrap_downloaded(db, tmp_path, "ord", "1", "en", versions)

            runner = LegisHistoryRunner(
                get=None, checkpoint=db, output_dir=tmp_path,
            )
            n = runner.enumerate_pending()
            assert n == 2

            vids = {
                r.vid for r in db.pending_legis_versions()
            }
            assert vids == {50293, 49871}
        finally:
            db.close()

    def test_enumerate_skips_already_on_disk(self, tmp_path):
        """If {stem}.v{vid}.content.json already exists on disk, don't
        re-upsert or refetch. Idempotent resume semantics."""
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            versions = [
                {"id": 52016, "date": "2025-12-18T00:00:00+08:00"},
                {"id": 50293, "date": "2024-08-18T00:00:00+08:00"},
                {"id": 49871, "date": "2024-03-23T00:00:00+08:00"},
            ]
            _bootstrap_downloaded(db, tmp_path, "ord", "1", "en", versions)
            # Pre-populate one historical file, simulating a prior run
            (tmp_path / "legis" / "ord" / "1"
             / "ord_1_en.v50293.content.json").write_text(
                json.dumps([{"subpath": "existing"}])
            )

            runner = LegisHistoryRunner(
                get=None, checkpoint=db, output_dir=tmp_path,
            )
            runner.enumerate_pending()

            vids = {r.vid for r in db.pending_legis_versions()}
            assert vids == {49871}
        finally:
            db.close()

    def test_enumerate_ignores_missing_versions_file(self, tmp_path):
        """A row in legis_documents whose versions.json isn't on disk
        (e.g. mid-migration corpus) is skipped rather than raising."""
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_legis_document(
                abbr="ord", num="99", lang="en", title="X",
            )
            db.mark_legis_downloaded(
                abbr="ord", num="99", lang="en",
                latest_vid=1, latest_version_date="2020-01-01",
                formats=["versions", "content"],
            )
            # No versions.json on disk

            runner = LegisHistoryRunner(
                get=None, checkpoint=db, output_dir=tmp_path,
            )
            n = runner.enumerate_pending()
            assert n == 0
        finally:
            db.close()


class TestFetchHistorical:
    async def test_writes_sidecar_and_marks_downloaded(self, tmp_path):
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            versions = [
                {"id": 52016, "date": "2025-12-18T00:00:00+08:00"},
                {"id": 50293, "date": "2024-08-18T00:00:00+08:00"},
            ]
            _bootstrap_downloaded(db, tmp_path, "ord", "1", "en", versions)

            async def mock_get(url, **kw):
                return httpx.Response(
                    200,
                    json=[{"subpath": "s1", "content": "<p>historical</p>"}],
                    request=httpx.Request("GET", url),
                )

            runner = LegisHistoryRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
            )
            runner.enumerate_pending()
            result = await runner.fetch_pending()

            assert result.downloaded == 1
            assert result.failed == 0
            sidecar = (
                tmp_path / "legis" / "ord" / "1"
                / "ord_1_en.v50293.content.json"
            )
            assert sidecar.exists()
            data = json.loads(sidecar.read_text())
            assert data == [{"subpath": "s1", "content": "<p>historical</p>"}]

            row = db._conn.execute(
                "SELECT status FROM legis_versions WHERE vid=50293"
            ).fetchone()
            assert row[0] == "downloaded"
        finally:
            db.close()

    async def test_500_marks_failed(self, tmp_path):
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            versions = [
                {"id": 52016, "date": "2025-12-18T00:00:00+08:00"},
                {"id": 50293, "date": "2024-08-18T00:00:00+08:00"},
            ]
            _bootstrap_downloaded(db, tmp_path, "ord", "1", "en", versions)

            async def mock_get(url, **kw):
                return httpx.Response(
                    500, text="server error",
                    request=httpx.Request("GET", url),
                )

            runner = LegisHistoryRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
            )
            runner.enumerate_pending()
            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM legis_versions WHERE vid=50293"
            ).fetchone()
            assert row[0] == "failed"
            assert "500" in row[1] or "HTTP" in row[1]
        finally:
            db.close()

    async def test_limit_caps_fetches(self, tmp_path):
        from hklii_downloader.legis import LegisHistoryRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            versions = [
                {"id": 52016, "date": "2025-12-18"},
                {"id": 50293, "date": "2024-08-18"},
                {"id": 49871, "date": "2024-03-23"},
                {"id": 47826, "date": "2022-07-01"},
            ]
            _bootstrap_downloaded(db, tmp_path, "ord", "1", "en", versions)

            called = {"n": 0}
            async def mock_get(url, **kw):
                called["n"] += 1
                return httpx.Response(
                    200, json=[],
                    request=httpx.Request("GET", url),
                )

            runner = LegisHistoryRunner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                limit=2,
            )
            runner.enumerate_pending()
            result = await runner.fetch_pending()

            assert result.downloaded == 2
            assert called["n"] == 2
        finally:
            db.close()
