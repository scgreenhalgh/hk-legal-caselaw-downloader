"""Tests for HtmlGenerator + hklii generate-html CLI subcommand."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hklii_downloader.checkpoint import CheckpointDB


def _make_doc_only(db, court, year, number):
    db.upsert_case(court, year, number, f"[{year}] X {number}",
                   "T", "2023-01-01")
    db.mark_downloaded(court, year, number, ["doc"])


def _write_doc(out: Path, court, year, stem, ext, body: bytes) -> Path:
    d = out / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}{ext}"
    p.write_bytes(body)
    return p


class TestHtmlGenerator:
    def test_writes_sidecar_and_updates_db_on_success(self, tmp_path):
        from hklii_downloader.html_generator import HtmlGenerator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_doc_only(db, "hkcfi", 2026, 1)
        _write_doc(out, "hkcfi", 2026, "hkcfi_2026_1", ".docx",
                   b"PK\x03\x04payload")

        with patch(
            "hklii_downloader.html_generator.convert_to_html",
            return_value="<p>ok</p>",
        ):
            result = HtmlGenerator(db, out).generate_all()

        try:
            assert result.generated == 1
            assert result.failed == 0
            sidecar = out / "hkcfi" / "2026" / "hkcfi_2026_1.generated.html"
            assert sidecar.exists()
            assert sidecar.read_text() == "<p>ok</p>"
            row = db._conn.execute(
                "SELECT html_generated_from, html_generated_error "
                "FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] == ".docx"
            assert row[1] is None
        finally:
            db.close()

    def test_records_error_on_unsupported_source(self, tmp_path):
        from hklii_downloader.html_generator import HtmlGenerator
        from hklii_downloader.doc_convert import UnsupportedSourceError

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_doc_only(db, "hkcfi", 2026, 1)
        _write_doc(out, "hkcfi", 2026, "hkcfi_2026_1", ".doc",
                   b"\xd0\xcf\x11\xe0raw ole body bytes")

        with patch(
            "hklii_downloader.html_generator.convert_to_html",
            side_effect=UnsupportedSourceError("no soffice"),
        ):
            result = HtmlGenerator(db, out).generate_all()

        try:
            assert result.generated == 0
            assert result.failed == 1
            # No sidecar written on failure
            assert not (
                out / "hkcfi" / "2026" / "hkcfi_2026_1.generated.html"
            ).exists()
            row = db._conn.execute(
                "SELECT html_generated_from, html_generated_error "
                "FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] is None
            assert "no soffice" in row[1]
        finally:
            db.close()

    def test_respects_limit(self, tmp_path):
        from hklii_downloader.html_generator import HtmlGenerator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        for i in range(1, 6):
            _make_doc_only(db, "hkcfi", 2026, i)
            _write_doc(out, "hkcfi", 2026, f"hkcfi_2026_{i}", ".docx",
                       b"PK\x03\x04payload")

        with patch(
            "hklii_downloader.html_generator.convert_to_html",
            return_value="<p>ok</p>",
        ):
            result = HtmlGenerator(db, out, limit=2).generate_all()

        try:
            assert result.generated == 2
        finally:
            db.close()

    def test_skips_row_with_no_doc_file_on_disk(self, tmp_path):
        """Row in DB but no doc-family file on disk — record as failure
        rather than crashing. This shouldn't happen post-scrape but the
        generator must not blow up if it does."""
        from hklii_downloader.html_generator import HtmlGenerator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_doc_only(db, "hkcfi", 2026, 1)
        # No file written

        result = HtmlGenerator(db, out).generate_all()

        try:
            assert result.generated == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT html_generated_error FROM cases "
                "WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert "no doc-family file" in row[0].lower() or "not found" in row[0].lower()
        finally:
            db.close()

    def test_dry_run_writes_nothing_and_leaves_db_alone(self, tmp_path):
        from hklii_downloader.html_generator import HtmlGenerator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_doc_only(db, "hkcfi", 2026, 1)
        _write_doc(out, "hkcfi", 2026, "hkcfi_2026_1", ".docx",
                   b"PK\x03\x04payload")

        with patch("hklii_downloader.html_generator.convert_to_html"):
            result = HtmlGenerator(db, out, dry_run=True).generate_all()

        try:
            assert result.generated == 0
            assert result.failed == 0
            # nb: report the row as a candidate
            assert result.candidates == 1
            assert not (
                out / "hkcfi" / "2026" / "hkcfi_2026_1.generated.html"
            ).exists()
            row = db._conn.execute(
                "SELECT html_generated_from FROM cases "
                "WHERE court='hkcfi' AND year=2026 AND number=1"
            ).fetchone()
            assert row[0] is None
        finally:
            db.close()


class TestGenerateHtmlSubcommand:
    def test_generate_html_in_group_help(self):
        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "generate-html" in result.output

    def test_generate_html_help_lists_flags(self):
        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["generate-html", "--help"])
        assert result.exit_code == 0
        for flag in ("--limit", "--dry-run", "--force"):
            assert flag in result.output

    def test_generate_html_missing_db_exits_nonzero(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "empty"
        out.mkdir()
        runner = CliRunner()
        result = runner.invoke(main, ["generate-html", "-o", str(out)])
        assert result.exit_code != 0

    def test_generate_html_processes_pending(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_doc_only(db, "hkcfi", 2026, 1)
        _write_doc(out, "hkcfi", 2026, "hkcfi_2026_1", ".docx",
                   b"PK\x03\x04payload")
        db.close()

        runner = CliRunner()
        with patch(
            "hklii_downloader.html_generator.convert_to_html",
            return_value="<p>ok</p>",
        ):
            result = runner.invoke(main, ["generate-html", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert (
            out / "hkcfi" / "2026" / "hkcfi_2026_1.generated.html"
        ).exists()
