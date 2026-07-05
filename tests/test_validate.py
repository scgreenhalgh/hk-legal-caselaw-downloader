"""Corpus validator tests — hklii validate.

Covers the 14 checks laid out in scratchpad/VALIDATOR_SPEC.md §6.
Library-level checks live here as pytest classes; CLI wiring and --fix
remediation land in TestValidateSubcommand / TestValidateFix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hklii_downloader.checkpoint import CheckpointDB


def _make_case(
    db: CheckpointDB,
    court: str,
    year: int,
    number: int,
    neutral: str,
    formats: list[str] | None = None,
    se_status: str = "na",
    sz_status: str = "na",
    ah_status: str = "na",
) -> None:
    """Insert a case + mark downloaded (if formats given) + set enrichment.

    mark_downloaded doesn't require pending → in_progress → downloaded
    transitions; it updates by PK, so we skip claim_pending to keep the
    fixture atomic per (court, year, number).
    """
    db.upsert_case(court, year, number, neutral, f"Title {number}", "2023-01-01")
    if formats is not None:
        db.mark_downloaded(court, year, number, formats)
    for kind, status in [
        ("summary_en", se_status),
        ("summary_zh", sz_status),
        ("appeal_history", ah_status),
    ]:
        db.mark_enrichment(court, year, number, kind, status)


def _write(out: Path, court: str, year: int, name: str, body) -> Path:
    d = out / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if isinstance(body, (bytes, bytearray)):
        p.write_bytes(body)
    else:
        p.write_text(body)
    return p


class TestPresenceCheck:
    def test_presence_flags_missing_file(self, tmp_path):
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "json", "txt"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "<p>[2023] HKCFI 1</p>")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        # deliberately omit hkcfi_2023_1.txt

        report = Validator(db, out, checks=["presence"]).run()
        db.close()

        fatal = [d for d in report.discrepancies if d.severity == "fatal"]
        presence = [d for d in fatal if d.check == "presence"]
        assert len(presence) == 1
        assert presence[0].court == "hkcfi"
        assert presence[0].year == 2023
        assert presence[0].number == 1

    def test_presence_ignores_docx_when_doc_in_formats(self, tmp_path):
        """A `.docx` sibling of a `formats=[..., 'doc', ...]` row must not
        trip presence — magic-driven extension resolution means the on-disk
        file can be any of .doc/.docx/.rtf and still satisfy the 'doc'
        formats-list entry. Mirrors verify_downloaded_against_files
        docx-fallback semantics (checkpoint.py:278-303)."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "json", "txt", "doc"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "<p>[2023] HKCFI 1</p>")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.txt", "[2023] HKCFI 1")
        # magic-picked .docx — no .doc file on disk
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.docx", b"PK\x03\x04payload")

        report = Validator(db, out, checks=["presence"]).run()
        db.close()

        assert report.counts["discrepancies_by_severity"]["fatal"] == 0


class TestMagicCheck:
    def test_magic_flags_rtf_at_doc_extension(self, tmp_path):
        """RTF bytes at a `.doc` filename — the same class of drift that
        motivated task #67. Extension must be magic-driven; validator
        surfaces the drift as a fatal magic discrepancy."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["doc"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.doc", b"{\\rtf1 body")

        report = Validator(db, out, checks=["magic"]).run()
        db.close()

        magic_fatals = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "magic"
        ]
        assert len(magic_fatals) == 1
        assert magic_fatals[0].observed == ".rtf"
        assert magic_fatals[0].expected == ".doc"

    def test_magic_accepts_pre_ole_word(self, tmp_path):
        """Word 6.0 / 95 magic (0xdba52d00) at .doc must pass — this is
        the format Judiciary serves for many 1990s judgments (task #64)."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 1998, 42, "[1998] HKCFI 42",
            formats=["doc"],
        )
        _write(out, "hkcfi", 1998, "hkcfi_1998_42.doc",
               b"\xdb\xa5\x2d\x00rest of body")

        report = Validator(db, out, checks=["magic"]).run()
        db.close()

        magic_fatals = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "magic"
        ]
        assert magic_fatals == []


class TestChallengeHtmlCheck:
    def test_challenge_page_detected_in_html(self, tmp_path):
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html"],
        )
        _write(
            out, "hkcfi", 2023, "hkcfi_2023_1.html",
            "<html><title>Just a moment...</title><body>cloudflare</body></html>",
        )

        report = Validator(db, out, checks=["challenge_html"]).run()
        db.close()

        challenge = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "challenge_html"
        ]
        assert len(challenge) == 1


class TestStemCoordsCheck:
    def test_stem_mismatch_with_parent_dir(self, tmp_path):
        """File hkcfi_2023_155.html placed under hkca/2023/ — the exact
        drift the spec calls out (§2 check 4)."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(db, "hkcfi", 2023, 155, "[2023] HKCFI 155", formats=["html"])
        _make_case(db, "hkca", 2023, 500, "[2023] HKCA 500", formats=["html"])
        _write(out, "hkca", 2023, "hkca_2023_500.html", "legit")
        _write(out, "hkcfi", 2023, "hkcfi_2023_155.html", "legit")
        # The drift: hkcfi stem under hkca dir
        _write(out, "hkca", 2023, "hkcfi_2023_155.html", "misplaced")

        report = Validator(db, out, checks=["stem_coords"]).run()
        db.close()

        stem_fatals = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "stem_coords"
        ]
        assert len(stem_fatals) == 1


class TestNeutralInBodyCheck:
    def test_neutral_missing_from_body_is_warn(self, tmp_path):
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 155, "[2023] HKCFI 155",
            formats=["html", "txt", "json"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_155.html", "unrelated body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_155.txt", "unrelated body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_155.json", "{}")

        report = Validator(db, out, checks=["neutral_in_body"]).run()
        db.close()

        warns = [
            d for d in report.discrepancies
            if d.severity == "warn" and d.check == "neutral_in_body"
        ]
        assert len(warns) == 1

    def test_neutral_present_with_nbsp_is_ok(self, tmp_path):
        """Encoding drift edge case from spec §4: NBSP inside the citation
        must not cause a false warn. Normalisation folds all whitespace."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 155, "[2023] HKCFI 155",
            formats=["txt"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_155.txt",
               "body containing HKCFI\xa0155 citation")

        report = Validator(db, out, checks=["neutral_in_body"]).run()
        db.close()

        warns = [
            d for d in report.discrepancies
            if d.check == "neutral_in_body"
        ]
        assert warns == []


class TestEnrichmentCheck:
    def test_summary_en_missing_when_status_downloaded(self, tmp_path):
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html"], se_status="downloaded",
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "body")
        # no summary_en.html — contradicts status='downloaded'

        report = Validator(db, out, checks=["enrichment"]).run()
        db.close()

        enr = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "enrichment"
        ]
        assert len(enr) == 1

    def test_summary_zh_extra_file_when_status_na(self, tmp_path):
        """Sidecar file exists but status='na' — stale artifact from an
        aborted run. Fatal per spec §2 check 6 (fatal both ways)."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html"], sz_status="na",
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.summary_zh.html",
               "stale sidecar")

        report = Validator(db, out, checks=["enrichment"]).run()
        db.close()

        enr = [
            d for d in report.discrepancies
            if d.severity == "fatal" and d.check == "enrichment"
        ]
        assert len(enr) == 1

    def test_pending_enrichment_with_no_sidecar_is_ok(self, tmp_path):
        """The 4 manually-grabbed rows have pending enrichment statuses
        and no sidecars — expected state, must not flag."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html"],
            se_status="pending", sz_status="pending", ah_status="pending",
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "body")

        report = Validator(db, out, checks=["enrichment"]).run()
        db.close()

        enr = [d for d in report.discrepancies if d.check == "enrichment"]
        assert enr == []


class TestOrphansCheck:
    def test_orphan_file_reported(self, tmp_path):
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        # No DB row for hkcfi_9999_1
        _write(out, "hkcfi", 9999, "hkcfi_9999_1.html", "orphan body")

        report = Validator(db, out, checks=["orphans"]).run()
        db.close()

        orphans = [
            d for d in report.discrepancies
            if d.severity == "warn" and d.check == "orphans"
        ]
        assert len(orphans) == 1

    def test_orphans_ignores_dotfiles_and_non_slug_dirs(self, tmp_path):
        """The output directory contains `.enum_cache/` and
        `failure_samples/`; walking those as if they were court slugs
        would produce garbage orphan warnings."""
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        # sibling dirs that must be skipped
        (out / ".enum_cache").mkdir()
        (out / ".enum_cache" / "stale.json").write_text("{}")
        (out / "failure_samples").mkdir()
        (out / "failure_samples" / "sample_1.html").write_text("junk")

        report = Validator(db, out, checks=["orphans"]).run()
        db.close()

        orphans = [d for d in report.discrepancies if d.check == "orphans"]
        assert orphans == []


class TestValidateReportSchema:
    def test_json_report_schema_stable(self, tmp_path):
        from hklii_downloader.validate import Validator, SCHEMA_VERSION

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))

        report = Validator(db, out).run()
        db.close()

        d = report.to_dict()
        assert d["schema_version"] == 1
        assert SCHEMA_VERSION == 1
        for key in (
            "schema_version",
            "output_dir",
            "generated_at",
            "counts",
            "discrepancies",
            "enrichment_stats",
            "checkpoint_stats",
        ):
            assert key in d, f"missing top-level key {key!r}"
        for k in (
            "rows_examined",
            "files_examined",
            "checks_run",
            "discrepancies_by_severity",
            "sampled",
        ):
            assert k in d["counts"], f"missing counts.{k}"
        for sev in ("fatal", "warn", "info"):
            assert sev in d["counts"]["discrepancies_by_severity"]


class TestValidateSubcommand:
    def test_validate_in_group_help(self):
        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "validate" in result.output

    def test_validate_help_lists_flags(self):
        from hklii_downloader.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["validate", "--help"])
        assert result.exit_code == 0
        for flag in ("--sample", "--seed", "--checks", "--fix", "--report"):
            assert flag in result.output, f"missing {flag} in help output"

    def test_validate_missing_db_exits_3(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "empty"
        out.mkdir()
        runner = CliRunner()
        result = runner.invoke(main, ["validate", "-o", str(out), "--json"])
        assert result.exit_code == 3, result.output

    def test_sample_mode_limits_row_scan(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        for i in range(1, 101):
            _make_case(db, "hkcfi", 2023, i, f"[2023] HKCFI {i}", formats=[])
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out),
            "--sample", "10", "--seed", "0", "--json",
        ])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["counts"]["rows_examined"] == 10
        assert report["counts"]["sampled"] is True

    def test_exit_code_2_on_fatal(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(db, "hkcfi", 2023, 1, "[2023] HKCFI 1", formats=["doc"])
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.doc", b"{\\rtf1 body")
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--checks", "magic", "--json",
        ])
        assert result.exit_code == 2, result.output

    def test_exit_code_1_on_warn_only(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        # Orphan file — no DB row → warn discrepancy
        _write(out, "hkcfi", 9999, "hkcfi_9999_1.html", "orphan body")
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--checks", "orphans", "--json",
        ])
        assert result.exit_code == 1, result.output

    def test_exit_code_0_on_clean(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()

        runner = CliRunner()
        result = runner.invoke(main, ["validate", "-o", str(out), "--json"])
        assert result.exit_code == 0, result.output

    def test_report_flag_writes_json_file(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()
        report_path = tmp_path / "report.json"

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out),
            "--report", str(report_path), "--json",
        ])
        assert result.exit_code == 0, result.output
        assert report_path.exists()
        d = json.loads(report_path.read_text())
        assert d["schema_version"] == 1

    def test_json_and_text_mutually_exclusive(self, tmp_path):
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--json", "--text",
        ])
        assert result.exit_code != 0

    def test_checks_flag_narrows_scope(self, tmp_path):
        """--checks presence,magic runs those two only; other checks that
        would fire (neutral_in_body warn on missing citation) don't."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "txt", "json"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "unrelated body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.txt", "unrelated body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out),
            "--checks", "presence,magic", "--json",
        ])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["counts"]["checks_run"] == ["presence", "magic"]
        assert report["counts"]["discrepancies_by_severity"]["warn"] == 0

    def test_text_output_has_sections_and_summary(self, tmp_path):
        """Text writer emits a section per firing check and a totals table.
        Spec §3: first 20 discrepancies + `... N more` tail."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(db, "hkcfi", 2023, 1, "[2023] HKCFI 1", formats=["doc"])
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.doc", b"{\\rtf1 body")
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out),
            "--checks", "magic", "--text",
        ])
        assert result.exit_code == 2, result.output
        assert "magic" in result.output
        assert "fatal" in result.output


class TestValidateFix:
    def test_fix_flips_broken_rows_to_pending(self, tmp_path):
        """Presence fatal (missing .txt) → --fix flips the row to pending
        with formats=NULL, mirroring verify_downloaded_against_files."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "json", "txt"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        # No .txt — presence fatal
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--fix", "--yes", "--json",
        ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            stats = db.stats()
            assert stats["pending"] == 1
            assert stats["downloaded"] == 0
            # formats cleared so a resume scrape re-picks the row cleanly
            row = db._conn.execute(
                "SELECT formats FROM cases WHERE court='hkcfi' "
                "AND year=2023 AND number=1"
            ).fetchone()
            assert row[0] is None
        finally:
            db.close()

    def test_fix_deletes_challenge_html_and_flips_row(self, tmp_path):
        """Challenge HTML fatal → --fix deletes the bad file and flips
        the row to pending."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(db, "hkcfi", 2023, 1, "[2023] HKCFI 1", formats=["html"])
        html_path = _write(
            out, "hkcfi", 2023, "hkcfi_2023_1.html",
            "<title>Just a moment...</title>",
        )
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--fix", "--yes", "--json",
        ])
        assert result.exit_code == 0, result.output
        assert not html_path.exists(), "--fix must delete the challenge page"

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            assert db.stats()["pending"] == 1
            assert db.stats()["downloaded"] == 0
        finally:
            db.close()

    def test_fix_deletes_magic_mismatch_and_flips_row(self, tmp_path):
        """Magic mismatch fatal → --fix deletes the bad file, flips row."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(db, "hkcfi", 2023, 1, "[2023] HKCFI 1", formats=["doc"])
        # PK magic at .doc — mismatch (should be .docx)
        doc_path = _write(
            out, "hkcfi", 2023, "hkcfi_2023_1.doc", b"PK\x03\x04body",
        )
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--fix", "--yes", "--json",
        ])
        assert result.exit_code == 0, result.output
        assert not doc_path.exists()

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            assert db.stats()["pending"] == 1
        finally:
            db.close()

    def test_fix_deletes_orphans_older_than_run_start(self, tmp_path):
        """Orphans older than run start are safe to delete — --fix (a)."""
        from hklii_downloader.cli import main
        import os
        import time

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        orphan = _write(out, "hkcfi", 9999, "hkcfi_9999_1.html", "orphan")
        db.close()

        old = time.time() - 3600
        os.utime(orphan, (old, old))

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--fix", "--yes", "--json",
        ])
        assert result.exit_code == 0, result.output
        assert not orphan.exists()

    def test_fix_preserves_body_when_citation_missing(self, tmp_path):
        """neutral_in_body is warn — never auto-fixed. Body file must
        stay put, row stays 'downloaded'. Spec §5(d) forbids auto-repair
        of body content because it needs human judgment."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "txt", "json"],
        )
        html_path = _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "unrelated")
        txt_path = _write(out, "hkcfi", 2023, "hkcfi_2023_1.txt", "unrelated")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, [
            "validate", "-o", str(out), "--fix", "--yes", "--json",
        ])
        # Post-fix: warn-only (neutral_in_body untouched)
        assert result.exit_code == 1, result.output
        assert html_path.exists()
        assert txt_path.exists()

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            assert db.stats()["downloaded"] == 1
        finally:
            db.close()

    def test_fix_no_yes_prompts_and_aborts_on_no(self, tmp_path):
        """Without --yes, --fix asks; feeding 'n' via CliRunner input
        aborts before any mutation."""
        from hklii_downloader.cli import main

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        _make_case(
            db, "hkcfi", 2023, 1, "[2023] HKCFI 1",
            formats=["html", "json", "txt"],
        )
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.html", "body")
        _write(out, "hkcfi", 2023, "hkcfi_2023_1.json", "{}")
        db.close()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["validate", "-o", str(out), "--fix"],
            input="n\n",
        )
        # Aborted → non-zero exit; row still marked downloaded, no mutation
        assert result.exit_code != 0

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            assert db.stats()["downloaded"] == 1
        finally:
            db.close()
