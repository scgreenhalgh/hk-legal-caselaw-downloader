"""Tests for the case translation backfill.

Scope: EN-scraped judgments with has_translation=True whose TC
counterpart was never downloaded (~590 of 118,188 EN judgments,
per 2026-07-06 audit). Pure on-disk state — no new DB columns.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


def _write_en_judgment(out, court, year, num, has_translation):
    stem = f"{court}_{year}_{num}"
    d = out / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.json").write_text(json.dumps({
        "title": "T", "case_number": f"HCA{num}/{year}",
        "court": "Court of First Instance",
        "date": f"{year}-01-01",
        "neutral_citation": f"[{year}] HK{court.upper()} {num}",
        "parallel_citations": [],
        "doc_url": None,
        "has_translation": has_translation,
        "url": f"https://www.hklii.hk/en/cases/{court}/{year}/{num}",
    }))
    (d / f"{stem}.html").write_text("<p>english body</p>")
    (d / f"{stem}.txt").write_text("english body")
    return d, stem


class TestEnumerate:
    def test_finds_has_translation_true_without_tc_sidecar(self, tmp_path):
        from hklii_downloader.case_translations import find_translation_targets

        out = tmp_path / "output"
        _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=True)
        _write_en_judgment(out, "hkcfi", 2024, 2, has_translation=False)

        targets = list(find_translation_targets(out))
        assert len(targets) == 1
        assert targets[0].court == "hkcfi"
        assert targets[0].year == 2024
        assert targets[0].number == 1

    def test_skips_when_tc_sidecar_already_exists(self, tmp_path):
        from hklii_downloader.case_translations import find_translation_targets

        out = tmp_path / "output"
        d, stem = _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=True)
        (d / f"{stem}.tc.html").write_text("<p>chinese</p>")

        targets = list(find_translation_targets(out))
        assert targets == []

    def test_ignores_summary_and_appeal_sidecar_json(self, tmp_path):
        """{stem}.appeal_history.json and {stem}.summary_*.html are not
        primary judgment metadata files — enumeration must not open them."""
        from hklii_downloader.case_translations import find_translation_targets

        out = tmp_path / "output"
        d, stem = _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=False)
        (d / f"{stem}.appeal_history.json").write_text("[]")
        (d / f"{stem}.summary_en.html").write_text("<p>x</p>")
        # Only the primary .json says has_translation=False → 0 targets.

        assert list(find_translation_targets(out)) == []

    def test_skips_malformed_json(self, tmp_path):
        from hklii_downloader.case_translations import find_translation_targets

        out = tmp_path / "output"
        d = out / "hkcfi" / "2024"
        d.mkdir(parents=True)
        (d / "hkcfi_2024_1.json").write_text("{not valid json")

        # Robust — no exception, just skip
        assert list(find_translation_targets(out)) == []


class TestSaveTranslation:
    def test_writes_tc_sidecars(self, tmp_path):
        from hklii_downloader.case_translations import save_translation_local
        from hklii_downloader.client import Judgment
        from hklii_downloader.parser import HKLIICase

        case = HKLIICase(lang="tc", court="hkcfi", year=2024, number=1)
        judgment = Judgment(
            case=case, title="TC Title",
            case_number="HCA1/2024",
            court_name="Court of First Instance",
            date="2024-01-01",
            neutral_citation="[2024] HKCFI 1",
            parallel_citations=[],
            content_html="<p>Chinese text</p>",
            doc_url=None,
            has_translation=True,
        )
        out = tmp_path / "output" / "hkcfi" / "2024"
        out.mkdir(parents=True)

        saved = save_translation_local(judgment, out)
        assert set(saved) == {".tc.html", ".tc.txt", ".tc.json"}
        assert (out / "hkcfi_2024_1.tc.html").exists()
        assert (out / "hkcfi_2024_1.tc.txt").exists()
        assert (out / "hkcfi_2024_1.tc.json").exists()
        assert (out / "hkcfi_2024_1.tc.html").read_text() == "<p>Chinese text</p>"


class TestRunner:
    async def test_fetch_writes_sidecars(self, tmp_path):
        from hklii_downloader.case_translations import CaseTranslationRunner

        out = tmp_path / "output"
        _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=True)

        async def mock_get(url, **kw):
            assert "lang=tc" in url
            return httpx.Response(
                200,
                json={
                    "cases": [{"title": "TC Title", "act": "HCA1/2024"}],
                    "db": "Court of First Instance",
                    "date": "2024-01-01",
                    "neutral": "[2024] HKCFI 1",
                    "parallel_citation": [],
                    "content": "<p>Chinese</p>",
                    "doc": None,
                    "has_translation": True,
                },
                request=httpx.Request("GET", url),
            )

        runner = CaseTranslationRunner(get=mock_get, output_dir=out)
        result = await runner.run()

        assert result.downloaded == 1
        assert result.failed == 0
        assert (out / "hkcfi" / "2024" / "hkcfi_2024_1.tc.html").exists()

    async def test_challenge_page_marks_failed(self, tmp_path):
        from hklii_downloader.case_translations import CaseTranslationRunner

        out = tmp_path / "output"
        _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=True)

        async def mock_get(url, **kw):
            return httpx.Response(
                200,
                json={"content": "<title>Just a moment...</title>",
                      "cases": [], "db": "", "date": "",
                      "neutral": "", "parallel_citation": [], "doc": None,
                      "has_translation": True},
                request=httpx.Request("GET", url),
            )

        runner = CaseTranslationRunner(get=mock_get, output_dir=out)
        result = await runner.run()

        assert result.downloaded == 0
        assert result.failed == 1
        assert not (out / "hkcfi" / "2024" / "hkcfi_2024_1.tc.html").exists()

    async def test_500_marks_failed(self, tmp_path):
        from hklii_downloader.case_translations import CaseTranslationRunner

        out = tmp_path / "output"
        _write_en_judgment(out, "hkcfi", 2024, 1, has_translation=True)

        async def mock_get(url, **kw):
            return httpx.Response(
                500, text="err", request=httpx.Request("GET", url),
            )

        runner = CaseTranslationRunner(get=mock_get, output_dir=out)
        result = await runner.run()
        assert result.downloaded == 0
        assert result.failed == 1

    async def test_limit_caps_fetches(self, tmp_path):
        from hklii_downloader.case_translations import CaseTranslationRunner

        out = tmp_path / "output"
        for i in range(5):
            _write_en_judgment(out, "hkcfi", 2024, i + 1,
                               has_translation=True)

        called = {"n": 0}
        async def mock_get(url, **kw):
            called["n"] += 1
            return httpx.Response(
                200,
                json={"cases": [{"title": "T", "act": ""}], "db": "",
                      "date": "", "neutral": "", "parallel_citation": [],
                      "content": "<p>x</p>", "doc": None,
                      "has_translation": True},
                request=httpx.Request("GET", url),
            )

        runner = CaseTranslationRunner(
            get=mock_get, output_dir=out, limit=2,
        )
        result = await runner.run()
        assert result.downloaded == 2
        assert called["n"] == 2


class TestValidatorPeelsTcSuffixes:
    """After the backfill lands sidecars, hklii validate must not flag
    them as orphans."""

    def test_tc_html_not_flagged_as_orphan(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.validate import Validator

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2024, 1, "[2024] HKCFI 1",
                       "T", "2024-01-01")
        db.mark_downloaded("hkcfi", 2024, 1, ["html"])
        d = out / "hkcfi" / "2024"
        d.mkdir(parents=True)
        (d / "hkcfi_2024_1.html").write_text("<p>en</p>")
        (d / "hkcfi_2024_1.tc.html").write_text("<p>tc</p>")
        (d / "hkcfi_2024_1.tc.txt").write_text("tc")
        (d / "hkcfi_2024_1.tc.json").write_text("{}")

        report = Validator(db, out, checks=["orphans"]).run()
        db.close()

        orphans = [d for d in report.discrepancies if d.check == "orphans"]
        assert orphans == [], f"got orphans: {orphans}"
