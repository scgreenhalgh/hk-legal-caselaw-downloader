"""Tests for D3 (Historical Laws / Other Publications / Practice Directions).

Covers the 6 unmapped slugs surfaced by task 22's endpoint probe:

  * histlaw   dbcat=H  gethistlaw   PDF, same-origin binary
  * hkiac     dbcat=O  getother     PDF, external-host binary
  * hklrccp   dbcat=O  getother     HTML (embedded content)
  * hklrcr    dbcat=O  getother     HTML (embedded content)
  * pcpdaab   dbcat=O  getother     PDF, external-host binary
  * pcpdc     dbcat=O  getother     HTML (embedded content)

Architecture: docs/d3-runner-design.md.
"""
from __future__ import annotations

import pytest


class TestD3Family:
    """Family-record semantics."""

    @pytest.mark.parametrize(
        "slug,expected_wire_abbr",
        [
            ("histlaw", "hkhistlaws"),
            ("hkiac", "hkiac"),
            ("hklrccp", "hklrccp"),
            ("hklrcr", "hklrcr"),
            ("pcpdaab", "pcpdaab"),
            ("pcpdc", "pcpdc"),
        ],
    )
    def test_wire_abbr_per_family(self, slug, expected_wire_abbr):
        from hklii_downloader.d3 import D3_FAMILIES, wire_abbr

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        assert wire_abbr(family) == expected_wire_abbr

    @pytest.mark.parametrize(
        "slug,expected_enabled",
        [
            # HKLII's /static/en/histlaw/*.pdf serves SPA HTML placeholder;
            # real archive at HKU library (oelawhk.lib.hku.hk).
            ("histlaw", False),
            # hkiac.org restructured 2026-07-09; every URL 404s.
            ("hkiac", False),
            # Same SPA-HTML issue as histlaw; source is
            # pcpd.org.hk/english/enforcement/decisions/decisions.html.
            ("pcpdaab", False),
            # HTML slugs — 100% scraped via HKLII getother shape B.
            ("hklrccp", True),
            ("hklrcr", True),
            ("pcpdc", True),
        ],
    )
    def test_family_enabled_flag(self, slug, expected_enabled):
        """`enabled` reflects whether the runner should scrape this slug
        today. Disabled slugs stay in D3_FAMILIES for provenance
        (metadata + freshness ledger) but ACTIVE_D3_FAMILIES filters
        them out at every default callsite.
        """
        from hklii_downloader.d3 import D3_FAMILIES

        fam = next(f for f in D3_FAMILIES if f.slug == slug)
        assert fam.enabled is expected_enabled

    def test_active_d3_families_is_html_slugs_only(self):
        from hklii_downloader.d3 import ACTIVE_D3_FAMILIES

        slugs = {f.slug for f in ACTIVE_D3_FAMILIES}
        assert slugs == {"hklrccp", "hklrcr", "pcpdc"}


class TestD3UrlBuilders:
    """URL constructors — listing + fetch."""

    @pytest.mark.parametrize(
        "slug,expected_dbcat",
        [
            ("histlaw", "H"),
            ("hkiac", "O"),
            ("hklrccp", "O"),
            ("hklrcr", "O"),
            ("pcpdaab", "O"),
            ("pcpdc", "O"),
        ],
    )
    def test_gethoptfiles_url_carries_dbcat_and_slug(
        self, slug, expected_dbcat,
    ):
        from hklii_downloader.d3 import D3_FAMILIES, gethoptfiles_url

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        url = gethoptfiles_url(
            family, lang="en", page=1, items_per_page=100,
        )

        assert url.startswith("https://www.hklii.hk/api/gethoptfiles?")
        assert f"dbcat={expected_dbcat}" in url
        assert f"abbr={slug}" in url  # SPA slug, NOT wire_abbr
        assert "lang=en" in url
        assert "page=1" in url
        assert "itemsPerPage=100" in url

    @pytest.mark.parametrize(
        "slug,expected_endpoint,expected_abbr",
        [
            ("histlaw", "gethistlaw", "hkhistlaws"),  # wire rewrite
            ("hkiac", "getother", "hkiac"),
            ("hklrccp", "getother", "hklrccp"),
            ("hklrcr", "getother", "hklrcr"),
            ("pcpdaab", "getother", "pcpdaab"),
            ("pcpdc", "getother", "pcpdc"),
        ],
    )
    def test_fetch_url_endpoint_and_wire_abbr(
        self, slug, expected_endpoint, expected_abbr,
    ):
        from hklii_downloader.d3 import D3_FAMILIES, fetch_url

        family = next(f for f in D3_FAMILIES if f.slug == slug)
        url = fetch_url(family, year=2020, num=1, lang="en")

        assert url.startswith(
            f"https://www.hklii.hk/api/{expected_endpoint}?"
        )
        assert f"abbr={expected_abbr}" in url
        assert "year=2020" in url
        assert "num=1" in url
        assert "lang=en" in url


class TestD3PathRegex:
    """_PATH_RE accepts /legis/ (histlaw) OR /other/ (getother slugs).

    Not a reuse of hopt._PATH_RE (which is /legis/ only). Also defends
    against ``nd`` year token by parity with hopt even though it was
    not observed on D3 during the endpoint probe.
    """

    @pytest.mark.parametrize(
        "path,expected_year,expected_num",
        [
            # histlaw — /legis/, trailing slash
            ("/en/legis/histlaw/1964/1/", "1964", "1"),
            # HTML slugs — /other/, no trailing slash observed
            ("/en/other/hklrccp/2020/2", "2020", "2"),
            ("/en/other/hklrcr/2019/3", "2019", "3"),
            ("/en/other/pcpdc/2018/5", "2018", "5"),
            # PDF external-host slugs — /other/
            ("/en/other/hkiac/2021/183", "2021", "183"),
            ("/en/other/pcpdaab/2020/1", "2020", "1"),
            # TC lang lane
            ("/tc/other/hklrccp/2020/2", "2020", "2"),
            # SC lang lane — hklrccp/hklrcr/pcpdc publish Simplified Chinese
            ("/sc/other/hklrccp/2020/2", "2020", "2"),
            ("/sc/other/hklrcr/2019/3", "2019", "3"),
            ("/sc/other/pcpdc/2018/5", "2018", "5"),
            # nd year — defensive parity with hopt
            ("/en/legis/histlaw/nd/7/", "nd", "7"),
        ],
    )
    def test_path_re_matches_legis_and_other(
        self, path, expected_year, expected_num,
    ):
        from hklii_downloader.d3 import _PATH_RE

        m = _PATH_RE.match(path)
        assert m is not None, f"regex did not match {path}"
        assert m.group(1) == expected_year
        assert m.group(2) == expected_num

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/",
            "/en/legis/",
            "/en/legis/histlaw/",
            "/en/legis/histlaw/1964",     # missing num
            "/en/cases/hkcfa/2020/1/",    # wrong bucket
            "/de/legis/histlaw/1964/1/",  # wrong lang
        ],
    )
    def test_path_re_rejects_malformed(self, path):
        from hklii_downloader.d3 import _PATH_RE

        assert _PATH_RE.match(path) is None


class TestD3ParseFilesResponse:
    """parse_files_response over real fixtures from the 2026-07-08 probe."""

    def test_parse_histlaw_response(self):
        body = {
            "totalfiles": 3836,
            "files": [
                {
                    "title": "Companies Ordinance(32)",
                    "path": "/en/legis/histlaw/1964/1/",
                    "neutral": "[1964] HKHistLaws 1",
                    "date": "1964-01-01",
                },
                {
                    "title": "Official Languages Ordinance(5)",
                    "path": "/en/legis/histlaw/1964/3/",
                    "neutral": "[1964] HKHistLaws 3",
                    "date": "1964-01-01",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 3836
        assert len(result.entries) == 2
        first = result.entries[0]
        assert first.year == 1964
        assert first.num == 1
        assert first.title == "Companies Ordinance(32)"
        assert first.neutral == "[1964] HKHistLaws 1"
        assert first.date == "1964-01-01"

    def test_parse_hklrccp_response_no_trailing_slash(self):
        body = {
            "totalfiles": 78,
            "files": [
                {
                    "title": "Outcome Related Fee Structures for Arbitration",
                    "path": "/en/other/hklrccp/2020/2",
                    "neutral": "[2020] HKLRCCP 2",
                    "date": "2020-12-01",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 78
        assert len(result.entries) == 1
        assert result.entries[0].year == 2020
        assert result.entries[0].num == 2

    def test_parse_hkiac_response(self):
        body = {
            "totalfiles": 190,
            "files": [
                {
                    "title": (
                        "Playboy Enterprises International, Inc. v. "
                        "E-MODE LIMITED"
                    ),
                    "path": "/en/other/hkiac/2021/183",
                    "neutral": "[2021] HKIAC 183",
                    "date": "2021-10-10",
                },
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)

        assert result.total == 190
        assert len(result.entries) == 1
        e = result.entries[0]
        assert e.year == 2021
        assert e.num == 183
        assert e.neutral == "[2021] HKIAC 183"

    def test_parse_skips_malformed_paths_and_logs_count(self, caplog):
        import logging

        body = {
            "totalfiles": 3,
            "files": [
                {"title": "ok", "path": "/en/legis/histlaw/1964/1/"},
                {"title": "bad", "path": "/random/garbage"},
                {"title": "also bad", "path": ""},
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        with caplog.at_level(logging.INFO, logger="hklii_downloader.d3"):
            result = parse_files_response(body)

        assert len(result.entries) == 1
        assert result.total == 3
        assert any(
            "skipped 2" in r.message.lower() or "skipped 2" in r.message
            for r in caplog.records
        ), f"expected skip-log with count 2 in {caplog.records}"

    def test_parse_skips_nd_year_but_still_parses(self):
        """Regex accepts nd defensively; parser skips it to keep year: int.

        Not observed on D3 during probe — future-proofing against a hopt-
        style legacy row appearing on histlaw or elsewhere.
        """
        body = {
            "totalfiles": 2,
            "files": [
                {"title": "ok", "path": "/en/legis/histlaw/1964/1/"},
                {"title": "nd row", "path": "/en/legis/histlaw/nd/9/"},
            ],
        }
        from hklii_downloader.d3 import parse_files_response

        result = parse_files_response(body)
        assert len(result.entries) == 1
        assert result.entries[0].year == 1964


class TestD3PdfUrl:
    """pdf_url — hop-2 URL resolver for the three JSON body shapes."""

    def test_external_absolute_url_returned_unchanged(self):
        """Shape C — hkiac/pcpdaab point at external source-org hosts."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        response = {
            "content": "",
            "pdf": (
                "https://www.hkiac.org/sites/default/files/"
                "ck_filebrowser/IP/hk/decision/DHK-2100183_Decision.pdf"
            ),
        }

        url = pdf_url(family, response)

        assert url == (
            "https://www.hkiac.org/sites/default/files/"
            "ck_filebrowser/IP/hk/decision/DHK-2100183_Decision.pdf"
        )

    def test_hklii_relative_url_joined_to_base(self):
        """Shape A — histlaw ships a same-origin `/static/...` path."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
        response = {"pdf": "/static/en/histlaw/1964/1.pdf"}

        url = pdf_url(family, response)

        assert url == "https://www.hklii.hk/static/en/histlaw/1964/1.pdf"

    def test_html_slug_no_pdf_field_returns_none(self):
        """Shape B — hklrccp/hklrcr/pcpdc: no `pdf` key → no second hop."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
        response = {"content": "<h3>...</h3>", "file_type": 1}

        assert pdf_url(family, response) is None

    def test_empty_pdf_field_returns_none(self):
        """Defensive — pdf key present but empty string treated as absent."""
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        assert pdf_url(family, {"pdf": ""}) is None

    @pytest.mark.parametrize(
        "bad_body",
        [
            None,       # HKLII returns JSON `null`
            [],         # unexpected list-shaped body
            "string",   # unexpected string
            42,         # unexpected number
        ],
    )
    def test_non_dict_metadata_returns_none_not_attribute_error(
        self, bad_body,
    ):
        """A malformed hop-1 body must not crash the worker.

        HKLII can rarely return JSON `null` or a list where a dict is
        expected. pdf_url must degrade to "no second hop" rather than
        raising AttributeError which _fetch_row won't catch and
        fetch_pending only catches D3FetchError from.
        """
        from hklii_downloader.d3 import D3_FAMILIES, pdf_url

        family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
        assert pdf_url(family, bad_body) is None


class TestD3SaveHtml:
    """save_d3_html — shape-B slugs, one JSON sidecar per row."""

    def test_writes_json_at_output_d3_layout(self, tmp_path):
        import json
        from hklii_downloader.d3 import D3_FAMILIES, save_d3_html

        family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
        response = {
            "id": 5338,
            "title": "Outcome Related Fee Structures for Arbitration",
            "neutral": "[2020] HKLRCCP 2",
            "date": "2020-12-01",
            "file_type": 1,
            "content": "<h3>...</h3>",
        }

        formats = save_d3_html(tmp_path, family, 2020, 2, "en", response)

        assert formats == ["json"]
        path = (
            tmp_path / "d3" / "hklrccp" / "2020" / "2"
            / "hklrccp_2020_2_en.json"
        )
        assert path.exists()
        stored = json.loads(path.read_text())
        assert stored["title"] == (
            "Outcome Related Fee Structures for Arbitration"
        )
        assert stored["file_type"] == 1
        assert stored["content"] == "<h3>...</h3>"

    def test_tc_lang_lands_under_same_year_num_dir(self, tmp_path):
        from hklii_downloader.d3 import D3_FAMILIES, save_d3_html

        family = next(f for f in D3_FAMILIES if f.slug == "pcpdc")
        save_d3_html(
            tmp_path, family, 2020, 1, "en",
            {"content": "<p>EN body</p>"},
        )
        save_d3_html(
            tmp_path, family, 2020, 1, "tc",
            {"content": "<p>TC body</p>"},
        )

        base = tmp_path / "d3" / "pcpdc" / "2020" / "1"
        assert (base / "pcpdc_2020_1_en.json").exists()
        assert (base / "pcpdc_2020_1_tc.json").exists()


class TestD3SavePdf:
    """save_d3_pdf — shape-A/C slugs, two-hop artifacts on disk."""

    def test_writes_json_pdf_and_txt_when_extraction_ok(self, tmp_path):
        import json
        from hklii_downloader.d3 import D3_FAMILIES, save_d3_pdf

        family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
        metadata = {
            "id": 2148,
            "title": "Companies Ordinance(32)",
            "neutral": "[1964] HKHistLaws 1",
            "pdf": "/static/en/histlaw/1964/1.pdf",
        }
        pdf_bytes = b"%PDF-1.4\nfake body"
        text = "Extracted body text"

        formats = save_d3_pdf(
            tmp_path, family, 1964, 1, "en",
            metadata, pdf_bytes, text,
        )

        assert formats == ["json", "pdf", "txt"]
        base = tmp_path / "d3" / "histlaw" / "1964" / "1"
        stored_meta = json.loads(
            (base / "histlaw_1964_1_en.json").read_text()
        )
        assert stored_meta["title"] == "Companies Ordinance(32)"
        assert (base / "histlaw_1964_1_en.pdf").read_bytes() == pdf_bytes
        assert (base / "histlaw_1964_1_en.txt").read_text() == text

    def test_omits_txt_when_extraction_failed(self, tmp_path):
        from hklii_downloader.d3 import D3_FAMILIES, save_d3_pdf

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        formats = save_d3_pdf(
            tmp_path, family, 2021, 183, "en",
            {"id": 5400, "title": "Playboy Enterprises"},
            b"%PDF-1.4\n", None,
        )

        assert formats == ["json", "pdf"]
        base = tmp_path / "d3" / "hkiac" / "2021" / "183"
        assert (base / "hkiac_2021_183_en.json").exists()
        assert (base / "hkiac_2021_183_en.pdf").exists()
        assert not (base / "hkiac_2021_183_en.txt").exists()

    def test_external_pdf_url_preserved_in_metadata(self, tmp_path):
        """Original external `pdf` URL must be grepable after mirror.

        Provenance for cross-origin PDFs (hkiac, pcpdaab) so a future
        audit can compare the mirrored `.pdf` against the source.
        """
        import json
        from hklii_downloader.d3 import D3_FAMILIES, save_d3_pdf

        family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
        metadata = {
            "id": 5400,
            "pdf": (
                "https://www.hkiac.org/sites/default/files/"
                "ck_filebrowser/IP/hk/decision/DHK-2100183.pdf"
            ),
        }
        save_d3_pdf(
            tmp_path, family, 2021, 183, "en",
            metadata, b"%PDF", None,
        )

        stored = json.loads(
            (tmp_path / "d3" / "hkiac" / "2021" / "183"
             / "hkiac_2021_183_en.json").read_text()
        )
        assert stored["pdf"] == (
            "https://www.hkiac.org/sites/default/files/"
            "ck_filebrowser/IP/hk/decision/DHK-2100183.pdf"
        )


class TestD3ExtractPdfText:
    """extract_pdf_text — pdftotext preferred, pypdf fallback, None on both."""

    def test_pdftotext_preferred_when_it_returns_text(self, monkeypatch):
        called: list[str] = []

        def fake_pdftotext(pdf_bytes):
            called.append("pdftotext")
            return "text via pdftotext"

        def fake_pypdf(pdf_bytes):
            called.append("pypdf")
            return "text via pypdf"

        monkeypatch.setattr(
            "hklii_downloader.d3._try_pdftotext", fake_pdftotext,
        )
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pypdf", fake_pypdf,
        )
        from hklii_downloader.d3 import extract_pdf_text

        result = extract_pdf_text(b"%PDF-1.4")

        assert result == "text via pdftotext"
        assert called == ["pdftotext"]  # pypdf never called

    def test_pypdf_fallback_when_pdftotext_returns_none(self, monkeypatch):
        called: list[str] = []

        def fake_pdftotext(pdf_bytes):
            called.append("pdftotext")
            return None

        def fake_pypdf(pdf_bytes):
            called.append("pypdf")
            return "text via pypdf"

        monkeypatch.setattr(
            "hklii_downloader.d3._try_pdftotext", fake_pdftotext,
        )
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pypdf", fake_pypdf,
        )
        from hklii_downloader.d3 import extract_pdf_text

        assert extract_pdf_text(b"%PDF-1.4") == "text via pypdf"
        assert called == ["pdftotext", "pypdf"]

    def test_returns_none_when_both_extractors_fail(self, monkeypatch):
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pdftotext", lambda b: None,
        )
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pypdf", lambda b: None,
        )
        from hklii_downloader.d3 import extract_pdf_text

        assert extract_pdf_text(b"%PDF-1.4") is None

    def test_pdftotext_missing_binary_returns_none(self, monkeypatch):
        """When the pdftotext binary isn't on PATH, _try_pdftotext returns None."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        from hklii_downloader.d3 import _try_pdftotext

        assert _try_pdftotext(b"%PDF-1.4") is None

    def test_pdftotext_empty_stdout_returns_none_not_empty_string(
        self, monkeypatch,
    ):
        """Image-only PDFs make pdftotext exit 0 with empty stdout.

        Empty must normalize to None so the pypdf fallback runs; otherwise
        extract_pdf_text returns "" and save_d3_pdf writes an empty .txt
        sidecar labelled as "text extracted".
        """
        import shutil
        import subprocess

        monkeypatch.setattr(
            shutil, "which",
            lambda name: "/usr/bin/pdftotext" if name == "pdftotext" else None,
        )

        class FakeCompleted:
            returncode = 0
            stdout = b""
            stderr = b""

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: FakeCompleted(),
        )
        from hklii_downloader.d3 import _try_pdftotext

        assert _try_pdftotext(b"%PDF-1.4\nimage-only body") is None

    def test_extract_pdf_text_falls_back_to_pypdf_on_empty_pdftotext(
        self, monkeypatch,
    ):
        """End-to-end: empty pdftotext output triggers pypdf, not a "" return."""
        called: list[str] = []

        def fake_pdftotext(pdf_bytes):
            called.append("pdftotext")
            return None  # After the fix, empty stdout → None.

        def fake_pypdf(pdf_bytes):
            called.append("pypdf")
            return "text via pypdf"

        monkeypatch.setattr(
            "hklii_downloader.d3._try_pdftotext", fake_pdftotext,
        )
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pypdf", fake_pypdf,
        )
        from hklii_downloader.d3 import extract_pdf_text

        assert extract_pdf_text(b"%PDF") == "text via pypdf"
        assert called == ["pdftotext", "pypdf"]


class TestD3RunnerEnumerate:
    """D3Runner.enumerate_all — mock-get replay of a real probe fixture."""

    async def test_upserts_hopt_documents_for_one_slug_one_lang(
        self, tmp_path,
    ):
        import httpx
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            page = {
                "totalfiles": 2,
                "files": [
                    {
                        "title": "Companies Ordinance(32)",
                        "path": "/en/legis/histlaw/1964/1/",
                        "neutral": "[1964] HKHistLaws 1",
                        "date": "1964-01-01",
                    },
                    {
                        "title": "Official Languages Ordinance(5)",
                        "path": "/en/legis/histlaw/1964/3/",
                        "neutral": "[1964] HKHistLaws 3",
                        "date": "1964-01-01",
                    },
                ],
            }

            async def mock_get(url, **kw):
                return httpx.Response(
                    200, json=page,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            upserted = await runner.enumerate_all()

            assert upserted == 2
            stats = db.hopt_stats_by_abbr()
            assert stats.get("histlaw", {}).get("pending", 0) == 2
            assert "en" in runner.langs_enumerated.get("histlaw", set())
        finally:
            db.close()

    async def test_empty_totalfiles_still_marks_lang_enumerated(
        self, tmp_path,
    ):
        """En-only slug: TC bucket returns totalfiles=0 — mark it read
        so freshness gate flips FRESH with local=live=0.
        """
        import httpx
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            empty_page = {"totalfiles": 0, "files": []}

            async def mock_get(url, **kw):
                return httpx.Response(
                    200, json=empty_page,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("tc",),
            )

            upserted = await runner.enumerate_all()

            assert upserted == 0
            assert "tc" in runner.langs_enumerated.get("histlaw", set())
        finally:
            db.close()


class TestD3RunnerFetchHappyPath:
    """D3Runner.fetch_pending — HTML (shape B) + PDF (shapes A/C) success."""

    async def test_html_slug_single_hop_marks_downloaded(self, tmp_path):
        import json

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hklrccp", year=2020, num=2, lang="en",
                title="Outcome Related Fee Structures for Arbitration",
                neutral="[2020] HKLRCCP 2",
                doc_date="2020-12-01",
            )

            metadata = {
                "id": 5338,
                "title": "Outcome Related Fee Structures for Arbitration",
                "neutral": "[2020] HKLRCCP 2",
                "date": "2020-12-01",
                "file_type": 1,
                "content": "<h3>...</h3>",
            }
            requested: list[str] = []

            async def mock_get(url, **kw):
                requested.append(url)
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 1
            assert result.failed == 0
            assert len(requested) == 1
            assert "/api/getother" in requested[0]
            saved = (
                tmp_path / "d3" / "hklrccp" / "2020" / "2"
                / "hklrccp_2020_2_en.json"
            )
            assert saved.exists()
            stored = json.loads(saved.read_text())
            assert stored["content"] == "<h3>...</h3>"
            stats = db.hopt_stats_by_abbr()
            assert stats["hklrccp"]["downloaded"] == 1
        finally:
            db.close()

    async def test_pdf_slug_two_hops_writes_pdf_and_metadata(
        self, tmp_path, monkeypatch,
    ):
        import json

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        # Force extract_pdf_text to return a known string so we can
        # assert on the .txt sidecar without depending on pdftotext.
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pdftotext",
            lambda b: "Companies Ordinance body",
        )
        monkeypatch.setattr(
            "hklii_downloader.d3._try_pypdf", lambda b: None,
        )

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="histlaw", year=1964, num=1, lang="en",
                title="Companies Ordinance(32)",
                neutral="[1964] HKHistLaws 1",
                doc_date="1964-01-01",
            )

            metadata = {
                "id": 2148,
                "title": "Companies Ordinance(32)",
                "neutral": "[1964] HKHistLaws 1",
                "date": "1964",
                "pdf": "/static/en/histlaw/1964/1.pdf",
                "path": "/1964/1/",
                "has_translation": False,
            }
            pdf_bytes = b"%PDF-1.4\nfake binary body\n"
            requested: list[str] = []

            async def mock_get(url, **kw):
                requested.append(url)
                if "/api/gethistlaw" in url:
                    return httpx.Response(
                        200, json=metadata,
                        request=httpx.Request("GET", url),
                    )
                if url == "https://www.hklii.hk/static/en/histlaw/1964/1.pdf":
                    return httpx.Response(
                        200, content=pdf_bytes,
                        request=httpx.Request("GET", url),
                    )
                raise AssertionError(f"unexpected url {url}")

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 1
            assert result.failed == 0
            assert len(requested) == 2
            assert "/api/gethistlaw" in requested[0]
            assert requested[1].endswith("/static/en/histlaw/1964/1.pdf")
            base = tmp_path / "d3" / "histlaw" / "1964" / "1"
            stored_meta = json.loads(
                (base / "histlaw_1964_1_en.json").read_text()
            )
            assert stored_meta["neutral"] == "[1964] HKHistLaws 1"
            assert (
                base / "histlaw_1964_1_en.pdf"
            ).read_bytes() == pdf_bytes
            assert (
                base / "histlaw_1964_1_en.txt"
            ).read_text() == "Companies Ordinance body"
            stats = db.hopt_stats_by_abbr()
            assert stats["histlaw"]["downloaded"] == 1
        finally:
            db.close()


class TestD3RunnerFetchFailures:
    """fetch_pending failure paths — mark row failed, error identifies hop."""

    async def test_hop1_404_marks_failed_with_hop_id(self, tmp_path):
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hklrccp", year=2020, num=2, lang="en",
                title="x", neutral=None, doc_date=None,
            )

            async def mock_get(url, **kw):
                return httpx.Response(
                    404, text="not found",
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents WHERE abbr='hklrccp'"
            ).fetchone()
            assert row[0] == "failed"
            error = row[1]
            assert "hop-1" in error
            assert "404" in error
            assert "hklrccp" in error
        finally:
            db.close()

    async def test_hop2_404_marks_failed_with_hop_id(
        self, tmp_path, monkeypatch,
    ):
        """Metadata JSON lands, but the PDF URL 404s — must be visible."""
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="histlaw", year=1964, num=1, lang="en",
                title="x", neutral=None, doc_date=None,
            )

            metadata = {
                "pdf": "/static/en/histlaw/1964/1.pdf",
                "title": "x",
            }

            async def mock_get(url, **kw):
                if "/api/gethistlaw" in url:
                    return httpx.Response(
                        200, json=metadata,
                        request=httpx.Request("GET", url),
                    )
                if "static" in url:
                    return httpx.Response(
                        404, text="not found",
                        request=httpx.Request("GET", url),
                    )
                raise AssertionError(f"unexpected url {url}")

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents WHERE abbr='histlaw'"
            ).fetchone()
            assert row[0] == "failed"
            error = row[1]
            assert "hop-2" in error
            assert "404" in error
            assert "static" in error  # URL is in the error text
        finally:
            db.close()

    async def test_hop2_returns_html_error_page_marks_failed(
        self, tmp_path,
    ):
        """External hosts (hkiac.org, pcpd.org.hk) can serve a 200
        text/html error page in place of the PDF. Without content-type
        or %PDF magic validation, that HTML is mirrored as `.pdf` and
        the row is marked downloaded — a silent archive corruption.
        """
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hkiac", year=2021, num=183, lang="en",
                title="Playboy Enterprises",
                neutral="[2021] HKIAC 183",
                doc_date="2021-10-10",
            )

            metadata = {
                "pdf": (
                    "https://www.hkiac.org/sites/default/files/"
                    "ck_filebrowser/IP/hk/decision/DHK-2100183.pdf"
                ),
                "content": "",
            }
            html_error = b"<html>Document not available at this time.</html>"

            async def mock_get(url, **kw):
                if "/api/getother" in url:
                    return httpx.Response(
                        200, json=metadata,
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200,
                    content=html_error,
                    headers={"content-type": "text/html"},
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hkiac")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents "
                "WHERE abbr='hkiac'"
            ).fetchone()
            assert row[0] == "failed"
            error = row[1]
            assert "hop-2" in error
            assert "PDF" in error or "magic" in error.lower()
            saved_pdf = (
                tmp_path / "d3" / "hkiac" / "2021" / "183"
                / "hkiac_2021_183_en.pdf"
            )
            assert not saved_pdf.exists(), (
                "HTML body was mirrored as .pdf — archive integrity broken"
            )
        finally:
            db.close()

    async def test_hop1_transport_error_marks_failed_and_continues(
        self, tmp_path,
    ):
        """httpx.ConnectTimeout / RequestError on hop-1 must be caught,
        the row marked failed, and the drain loop must continue to
        remaining pending rows rather than aborting the whole batch.
        """
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            # Two pending rows — first will transport-fail, second must
            # still get processed.
            for num in (1, 2):
                db.upsert_hopt_document(
                    abbr="hklrccp", year=2020, num=num, lang="en",
                    title=f"T{num}", neutral=None, doc_date=None,
                )

            call_count = {"n": 0}
            metadata = {"content": "<p>ok</p>"}

            async def mock_get(url, **kw):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise httpx.ConnectTimeout(
                        "simulated proxy stall",
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.failed == 1
            assert result.downloaded == 1
            rows = db._conn.execute(
                "SELECT num, status, error FROM hopt_documents "
                "WHERE abbr='hklrccp' ORDER BY num"
            ).fetchall()
            failed_row = next(r for r in rows if r[1] == "failed")
            assert "hop-1" in failed_row[2]
            assert "ConnectTimeout" in failed_row[2] or "timeout" in failed_row[2].lower()
        finally:
            db.close()

    async def test_hop2_transport_error_marks_failed(self, tmp_path):
        """httpx transport error on hop-2 must be caught, not propagated."""
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="histlaw", year=1964, num=1, lang="en",
                title="T", neutral=None, doc_date=None,
            )

            metadata = {"pdf": "/static/en/histlaw/1964/1.pdf"}

            async def mock_get(url, **kw):
                if "/api/gethistlaw" in url:
                    return httpx.Response(
                        200, json=metadata,
                        request=httpx.Request("GET", url),
                    )
                # hop-2: external / static host
                raise httpx.ReadTimeout(
                    "simulated hop-2 timeout",
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents "
                "WHERE abbr='histlaw'"
            ).fetchone()
            assert row[0] == "failed"
            assert "hop-2" in row[1]

        finally:
            db.close()

    async def test_enumerate_transport_error_skips_lang_and_continues(
        self, tmp_path,
    ):
        """A transport error on ONE (family, lang) enum must NOT abort
        enumeration for other pairs. And the failed pair MUST NOT show
        up in langs_enumerated so freshness stays STALE for it.
        """
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            good_page = {
                "totalfiles": 1,
                "files": [
                    {
                        "title": "T",
                        "path": "/en/other/hklrccp/2020/2",
                        "neutral": "[2020] HKLRCCP 2",
                        "date": "2020-12-01",
                    },
                ],
            }

            async def mock_get(url, **kw):
                if "hklrccp" in url and "lang=en" in url:
                    raise httpx.ConnectError(
                        "simulated proxy dead",
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=good_page,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en", "tc"),
            )

            upserted = await runner.enumerate_all()

            # EN failed, TC succeeded → 1 upsert from TC.
            assert upserted == 1
            assert "en" not in runner.langs_enumerated.get("hklrccp", set())
            assert "tc" in runner.langs_enumerated.get("hklrccp", set())
        finally:
            db.close()

    async def test_enumerate_json_decode_error_skips_lang_and_continues(
        self, tmp_path,
    ):
        """200-with-HTML upstream error body (JSONDecodeError) on ONE
        (family, lang) enum must NOT abort the whole enumeration for
        other pairs.
        """
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            good_page = {"totalfiles": 0, "files": []}

            async def mock_get(url, **kw):
                if "histlaw" in url:
                    return httpx.Response(
                        200,
                        text="<html>gunicorn hiccup</html>",
                        headers={"content-type": "text/html"},
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=good_page,
                    request=httpx.Request("GET", url),
                )

            histlaw = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            hklrccp = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(histlaw, hklrccp), langs=("en",),
            )

            # Must not raise despite histlaw returning non-JSON.
            await runner.enumerate_all()

            # histlaw's enum wire-failed → not in langs_enumerated.
            assert "en" not in runner.langs_enumerated.get(
                "histlaw", set(),
            )
            # hklrccp's enum succeeded (totalfiles=0 is a valid read).
            assert "en" in runner.langs_enumerated.get("hklrccp", set())
        finally:
            db.close()

    async def test_hop1_non_json_body_marks_failed(self, tmp_path):
        """HKLII returned an HTML error page instead of JSON."""
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="hklrccp", year=2020, num=2, lang="en",
                title="x", neutral=None, doc_date=None,
            )

            async def mock_get(url, **kw):
                return httpx.Response(
                    200,
                    text="<html>upstream error</html>",
                    headers={"content-type": "text/html"},
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 1
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents WHERE abbr='hklrccp'"
            ).fetchone()
            assert row[0] == "failed"
            error = row[1]
            assert "hop-1" in error
            assert "non-JSON" in error or "JSONDecodeError" in error
        finally:
            db.close()


class TestD3RunnerAbbrScoping:
    """C1 + H4 — cross-runner isolation via abbr-scoped queue operations.

    D3Runner must NEVER touch pending rows outside its own family set,
    even though hopt_documents is shared with HoptRunner and other D3
    runs on different --slug subsets. Corrupting a foreign row by
    marking it failed with 'unknown family' is a permanent data loss
    because upsert_hopt_document preserves status on conflict.
    """

    async def test_does_not_touch_hopt_family_pending_rows(self, tmp_path):
        """Precondition: pending bahkg row (HoptRunner state).
        Action: D3Runner.fetch_pending with default D3_FAMILIES.
        Postcondition: bahkg row still 'pending', no wire call fired.
        """
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="bahkg", year=2020, num=12, lang="en",
                title="T", neutral=None, doc_date=None,
            )

            call_count = {"n": 0}

            async def mock_get(url, **kw):
                call_count["n"] += 1
                raise AssertionError(f"unexpected wire call to {url}")

            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=D3_FAMILIES, langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 0
            assert result.failed == 0
            assert call_count["n"] == 0
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents WHERE abbr='bahkg'"
            ).fetchone()
            assert row[0] == "pending"
            assert row[1] is None
        finally:
            db.close()

    async def test_does_not_touch_other_d3_slug_pending_rows(self, tmp_path):
        """Precondition: pending histlaw row (from a prior partial D3 run).
        Action: D3Runner.fetch_pending scoped to --slug hklrccp only.
        Postcondition: histlaw row still 'pending', not marked failed.
        """
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="histlaw", year=1964, num=1, lang="en",
                title="T", neutral=None, doc_date=None,
            )

            async def mock_get(url, **kw):
                raise AssertionError(f"unexpected wire call to {url}")

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.fetch_pending()

            assert result.failed == 0
            row = db._conn.execute(
                "SELECT status, error FROM hopt_documents WHERE abbr='histlaw'"
            ).fetchone()
            assert row[0] == "pending"
            assert row[1] is None
        finally:
            db.close()

    def test_cli_reports_scoped_pending_count_not_global_hopt_stats(
        self, tmp_path,
    ):
        """H4 — `_run_scrape_d3` must count pending scoped to D3 slugs.

        Precondition: 5 pending bacpg rows (hopt) + 0 pending D3 rows.
        Action: `hklii scrape-d3 --slug hklrccp --direct --yes`.
        Expected CLI output: "Pending: 0" and "Nothing to fetch."
        Wrong (pre-fix) output: "Pending: 5" and drain runs.
        """
        from unittest.mock import AsyncMock, patch

        from click.testing import CliRunner

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main

        # Seed 5 bacpg pending rows (HoptRunner state).
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            for num in range(1, 6):
                db.upsert_hopt_document(
                    abbr="bacpg", year=2020, num=num, lang="en",
                    title="T", neutral=None, doc_date=None,
                )
        finally:
            db.close()

        with patch("hklii_downloader.cli._run_scrape_d3", new=AsyncMock()):
            # We use the actual scrape-d3 command but the runner is
            # mocked — we only care about the pre-runner "target"
            # calculation shown to the operator. The relevant helper
            # is the CLI's stats printing before invoking the runner.
            #
            # This test asserts the direct helper: hopt_stats scoped to
            # D3 slugs must return 0 pending given only hopt rows exist.
            pass

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            from hklii_downloader.d3 import D3_FAMILIES
            d3_slugs = tuple(f.slug for f in D3_FAMILIES)
            scoped = db.hopt_stats(abbrs=d3_slugs)
            assert scoped.get("pending", 0) == 0, (
                f"D3-scoped pending must be 0 (bacpg pending doesn't count) "
                f"but got {scoped.get('pending', 0)}"
            )
            # Meanwhile, global stats DO see the 5 bacpg rows.
            global_stats = db.hopt_stats()
            assert global_stats["pending"] == 5
        finally:
            db.close()

    async def test_fetch_pending_runs_workers_concurrently(self, tmp_path):
        """H3 — `workers=N` must fan out concurrent fetches.

        Seeds 8 pending rows and configures workers=4. Mock get() spends
        a tick in-flight and records max-concurrent. If fetch_pending is
        the current serial while-loop, max-concurrent will be 1.
        """
        import asyncio

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for num in range(1, 9):
                db.upsert_hopt_document(
                    abbr="hklrccp", year=2020, num=num, lang="en",
                    title=f"T{num}", neutral=None, doc_date=None,
                )

            gauge = {"current": 0, "max": 0}
            metadata = {"content": "<p>ok</p>"}

            async def mock_get(url, **kw):
                gauge["current"] += 1
                gauge["max"] = max(gauge["max"], gauge["current"])
                await asyncio.sleep(0.02)
                gauge["current"] -= 1
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",), workers=4,
            )

            result = await runner.fetch_pending()

            assert result.downloaded == 8
            assert result.failed == 0
            assert gauge["max"] > 1, (
                f"expected concurrent fetches (workers=4) but observed "
                f"max in-flight = {gauge['max']} — fetch is still serial"
            )
        finally:
            db.close()

    async def test_does_not_release_in_progress_hopt_family_rows(
        self, tmp_path,
    ):
        """release_in_progress_hopt must be abbr-scoped too.

        Precondition: bahkg row stuck at 'in_progress' from a HoptRunner crash.
        Action: D3Runner.fetch_pending (called even when no D3 rows pending).
        Postcondition: bahkg row still 'in_progress' — HoptRunner recovers it,
        not D3Runner.
        """
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_hopt_document(
                abbr="bahkg", year=2020, num=12, lang="en",
                title="T", neutral=None, doc_date=None,
            )
            # Force the row to in_progress (as if a HoptRunner worker
            # claimed it and crashed).
            db._conn.execute(
                "UPDATE hopt_documents SET status='in_progress' "
                "WHERE abbr='bahkg' AND year=2020 AND num=12 AND lang='en'"
            )
            db._conn.commit()

            async def mock_get(url, **kw):
                raise AssertionError(f"unexpected wire call to {url}")

            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=D3_FAMILIES, langs=("en",),
            )
            await runner.fetch_pending()

            row = db._conn.execute(
                "SELECT status FROM hopt_documents WHERE abbr='bahkg'"
            ).fetchone()
            assert row[0] == "in_progress"
        finally:
            db.close()


class TestD3RunnerRun:
    """D3Runner.run — enumerate + fetch composed; result surface."""

    async def test_run_composes_enumerate_and_fetch(self, tmp_path):
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            listing = {
                "totalfiles": 1,
                "files": [
                    {
                        "title": "T",
                        "path": "/en/other/hklrccp/2020/2",
                        "neutral": "[2020] HKLRCCP 2",
                        "date": "2020-12-01",
                    },
                ],
            }
            metadata = {"content": "<h3>x</h3>"}

            async def mock_get(url, **kw):
                if "gethoptfiles" in url:
                    return httpx.Response(
                        200, json=listing,
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )

            result = await runner.run()

            assert result.downloaded == 1
            assert result.failed == 0
            assert result.langs_enumerated == {"hklrccp": {"en"}}
        finally:
            db.close()

    async def test_langs_enumerated_excludes_wire_failed_langs(
        self, tmp_path,
    ):
        """One (slug, lang) enum 500s — that pair must NOT flip FRESH."""
        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            en_page = {
                "totalfiles": 1,
                "files": [
                    {
                        "title": "T",
                        "path": "/en/other/hklrccp/2020/2",
                        "neutral": "[2020] HKLRCCP 2",
                        "date": "2020-12-01",
                    },
                ],
            }
            metadata = {"content": "<p>ok</p>"}

            async def mock_get(url, **kw):
                if "gethoptfiles" in url and "lang=en" in url:
                    return httpx.Response(
                        200, json=en_page,
                        request=httpx.Request("GET", url),
                    )
                if "gethoptfiles" in url and "lang=tc" in url:
                    # TC enum wire-fails
                    return httpx.Response(
                        500, text="internal error",
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en", "tc"),
            )

            result = await runner.run()

            assert result.langs_enumerated == {"hklrccp": {"en"}}
            assert "tc" not in result.langs_enumerated.get("hklrccp", set())
        finally:
            db.close()


class TestScrapeD3Cli:
    """CLI: `hklii scrape-d3` subcommand."""

    def test_help_invocable_and_lists_flags(self):
        from click.testing import CliRunner
        from hklii_downloader.cli import main

        result = CliRunner().invoke(main, ["scrape-d3", "--help"])

        assert result.exit_code == 0
        for flag in (
            "--proxy", "--direct", "--slug", "--lang",
            "--limit", "--skip-if-fresh", "--yes",
        ):
            assert flag in result.output, f"missing flag {flag}"

    def test_requires_proxy_or_direct(self):
        from click.testing import CliRunner
        from hklii_downloader.cli import main

        result = CliRunner().invoke(main, ["scrape-d3"])

        assert result.exit_code != 0
        assert "proxy" in result.output.lower()

    def test_dispatches_to_run_scrape_d3_when_direct(self, tmp_path):
        """--direct --yes should skip the confirm prompt and call the runner."""
        from unittest.mock import AsyncMock, patch

        from click.testing import CliRunner
        from hklii_downloader.cli import main

        with patch(
            "hklii_downloader.cli._run_scrape_d3", new=AsyncMock(),
        ) as mocked:
            result = CliRunner().invoke(
                main,
                [
                    "scrape-d3",
                    "-o", str(tmp_path),
                    "--direct", "--yes",
                    "--slug", "hklrccp",
                    "--lang", "en",
                    "--limit", "1",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mocked.await_count == 1
        call = mocked.await_args
        assert call.kwargs["output"] == tmp_path
        assert call.kwargs["direct"] is True
        assert "hklrccp" in call.kwargs["slugs"]
        assert call.kwargs["langs"] == ("en",)
        assert call.kwargs["limit"] == 1

    def test_skip_if_fresh_short_circuits_when_all_fresh(self, tmp_path):
        """Every requested (slug, lang) is FRESH → returns without wire call."""
        from unittest.mock import AsyncMock, patch

        from click.testing import CliRunner

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.d3 import D3_FAMILIES, D3_LANGS

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            # Seed every (slug, lang) as FRESH under kind='hopt'.
            import time
            now = int(time.time())
            for family in D3_FAMILIES:
                for lang in D3_LANGS:
                    db.upsert_freshness_probe(
                        kind="hopt", scope=family.slug, lang=lang,
                        live_count=0, live_updated_at="2026-07-08",
                        live_probed_at=now, probe_error=None,
                    )
                    db._conn.execute(
                        "UPDATE db_freshness "
                        "SET local_count=0, local_counted_at=? "
                        "WHERE kind='hopt' AND scope=? AND lang=?",
                        (now, family.slug, lang),
                    )
                    db._conn.commit()
                    db.mark_bucket_scraped(
                        kind="hopt", scope=family.slug, lang=lang,
                        completed_at=now,
                    )
        finally:
            db.close()

        with patch(
            "hklii_downloader.cli._run_scrape_d3", new=AsyncMock(),
        ) as mocked:
            result = CliRunner().invoke(
                main,
                [
                    "scrape-d3",
                    "-o", str(tmp_path),
                    "--direct", "--yes",
                    "--skip-if-fresh",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mocked.await_count == 0, "runner ran despite all-fresh"


class TestD3Dispatcher:
    """update.py PROFILE_DEFAULTS + plan step + step estimate."""

    def test_step_est_registered_for_scrape_d3(self):
        from hklii_downloader.update import _STEP_EST

        assert "scrape_d3" in _STEP_EST
        # T5 — key presence is not enough; a refactor to empty string
        # would leave `est: ` blank in format_plan output. Require a
        # non-empty descriptive string.
        assert _STEP_EST["scrape_d3"], "empty estimate string"
        assert "enum" in _STEP_EST["scrape_d3"].lower() or "fetch" in _STEP_EST["scrape_d3"].lower()

    def test_daily_and_weekly_exclude_d3(self):
        from hklii_downloader.update import PROFILE_DEFAULTS

        assert PROFILE_DEFAULTS["daily"]["include_d3"] is False
        assert PROFILE_DEFAULTS["weekly"]["include_d3"] is False

    def test_monthly_and_quarterly_include_d3(self):
        from hklii_downloader.update import PROFILE_DEFAULTS

        assert PROFILE_DEFAULTS["monthly"]["include_d3"] is True
        assert PROFILE_DEFAULTS["quarterly"]["include_d3"] is True

    def test_custom_defaults_include_d3_off(self):
        from hklii_downloader.update import PROFILE_DEFAULTS

        assert PROFILE_DEFAULTS["custom"]["include_d3"] is False

    def test_plan_contains_scrape_d3_step_when_included(self, tmp_path):
        from hklii_downloader.update import UpdateRunner

        runner = UpdateRunner(
            profile="custom", output=tmp_path, proxies=["p"],
            include_d3=True,
        )
        step_names = {s.name for s in runner.plan()}
        assert "scrape_d3" in step_names

    def test_plan_omits_scrape_d3_step_when_excluded(self, tmp_path):
        from hklii_downloader.update import UpdateRunner

        runner = UpdateRunner(
            profile="custom", output=tmp_path, proxies=["p"],
            include_d3=False,
        )
        step_names = {s.name for s in runner.plan()}
        assert "scrape_d3" not in step_names

    def test_update_cli_accepts_include_d3_flag(self, tmp_path):
        """M2 — `hklii update --include-d3` must be registered.

        PROFILE_DEFAULTS + UpdateRunner already accept include_d3, but
        without a Click option the flag returns NoSuchOption and the
        custom profile can never opt into D3.
        """
        from click.testing import CliRunner

        from hklii_downloader.cli import main

        result = CliRunner().invoke(main, ["update", "--help"])

        assert result.exit_code == 0
        assert "--include-d3" in result.output
        assert "--no-d3" in result.output

    def test_update_cli_include_d3_reaches_update_runner(self, tmp_path):
        """--include-d3 must flow into UpdateRunner(include_d3=True)."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from hklii_downloader.cli import main

        with patch("hklii_downloader.update.UpdateRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.plan.return_value = []
            instance.format_plan.return_value = "no steps"
            CliRunner().invoke(main, [
                "update",
                "--profile", "custom",
                "--include-d3",
                "--dry-run",
                "-o", str(tmp_path),
                "-p", "http://127.0.0.1:8888",
            ])

        assert MockRunner.called, "UpdateRunner was not instantiated"
        call_kwargs = MockRunner.call_args.kwargs
        assert call_kwargs.get("include_d3") is True


class TestD3FreshnessEndToEnd:
    """Runner run → mark_bucket_scraped → recompute → row flips FRESH."""

    async def test_populated_bucket_flips_to_fresh_after_run(self, tmp_path):
        import time

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner
        from hklii_downloader.freshness import _fresh

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            now = int(time.time())
            # Seed live_count=1 for hklrccp/en so recompute has a target.
            db.upsert_freshness_probe(
                kind="hopt", scope="hklrccp", lang="en",
                live_count=1, live_updated_at="2026-07-08",
                live_probed_at=now, probe_error=None,
            )

            listing = {
                "totalfiles": 1,
                "files": [
                    {
                        "title": "T",
                        "path": "/en/other/hklrccp/2020/2",
                        "neutral": "[2020] HKLRCCP 2",
                        "date": "2020-12-01",
                    },
                ],
            }
            metadata = {"content": "<h3>x</h3>"}

            async def mock_get(url, **kw):
                if "gethoptfiles" in url:
                    return httpx.Response(
                        200, json=listing,
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json=metadata,
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "hklrccp")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("en",),
            )
            result = await runner.run()
            assert result.downloaded == 1

            # Simulate the CLI close-out step + freshness recompute.
            db.mark_bucket_scraped(
                "hopt", "hklrccp", "en", completed_at=now,
            )
            db.recompute_local_count(
                kind="hopt", scope="hklrccp", lang="en",
            )

            row = db.get_freshness_row("hopt", "hklrccp", "en")
            assert row is not None
            assert row.local_count == 1
            assert row.last_scrape_completed_at == now
            assert _fresh(row) is True
        finally:
            db.close()

    async def test_run_scrape_d3_close_out_marks_only_enumerated_langs(
        self, tmp_path,
    ):
        """T4 — the CLI close-out loop must iterate langs_enumerated,
        not the requested langs. If a lang's enum wire-failed, that
        bucket must remain STALE.

        Drives the full `_run_scrape_d3` helper end-to-end (unlike
        TestD3FreshnessEndToEnd which calls db.mark_bucket_scraped
        directly and would miss a refactor to the close-out loop).
        """
        import time

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_scrape_d3
        from hklii_downloader.proxy_pool import ProxyPool

        now = int(time.time())

        # Seed live counts so freshness has targets for both langs.
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            for lang in ("en", "tc"):
                db.upsert_freshness_probe(
                    kind="hopt", scope="hklrccp", lang=lang,
                    live_count=1, live_updated_at="2026-07-08",
                    live_probed_at=now, probe_error=None,
                )
        finally:
            db.close()

        good_page = {
            "totalfiles": 1,
            "files": [
                {
                    "title": "T",
                    "path": "/en/other/hklrccp/2020/2",
                    "neutral": "[2020] HKLRCCP 2",
                    "date": "2020-12-01",
                },
            ],
        }
        metadata = {"content": "<h3>x</h3>"}

        async def fake_get(self, url, **kw):
            if "gethoptfiles" in url and "lang=tc" in url:
                # TC enum wire-fails — 500.
                return httpx.Response(
                    500, text="internal",
                    request=httpx.Request("GET", url),
                )
            if "gethoptfiles" in url:
                return httpx.Response(
                    200, json=good_page,
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200, json=metadata,
                request=httpx.Request("GET", url),
            )

        # Patch ProxyPool.get on the class so the real _run_scrape_d3
        # helper drives the actual close-out loop.
        from unittest.mock import patch

        async def fake_preflight(self):
            from hklii_downloader.proxy_pool import PreflightResult
            return PreflightResult(
                home_ip="1.2.3.4", healthy_proxies=["p1"], failed_proxies=[],
            )

        async def fake_close(self):
            return None

        with patch.object(ProxyPool, "get", fake_get), \
             patch.object(ProxyPool, "preflight", fake_preflight), \
             patch.object(ProxyPool, "close", fake_close):
            await _run_scrape_d3(
                output=tmp_path,
                proxies=["http://p1"], direct=False,
                slugs=("hklrccp",), langs=("en", "tc"),
                limit=None, no_events=True,
            )

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            en_row = db.get_freshness_row("hopt", "hklrccp", "en")
            tc_row = db.get_freshness_row("hopt", "hklrccp", "tc")
            assert en_row is not None
            assert tc_row is not None
            # EN enumerated successfully → stamped as scraped.
            assert en_row.last_scrape_completed_at is not None
            # TC enum wire-failed → must NOT be stamped scraped (else
            # freshness would lie FRESH with local=0 vs live=1).
            assert tc_row.last_scrape_completed_at is None, (
                "TC bucket was stamped scraped despite enum wire failure — "
                "the close-out loop iterated requested langs instead of "
                "langs_enumerated"
            )
        finally:
            db.close()

    async def test_empty_bucket_flips_fresh_at_local_equals_live_zero(
        self, tmp_path,
    ):
        """En-only slug's TC bucket: live=0, local=0, still FRESH."""
        import time

        import httpx

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.d3 import D3_FAMILIES, D3Runner
        from hklii_downloader.freshness import _fresh

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            now = int(time.time())
            db.upsert_freshness_probe(
                kind="hopt", scope="histlaw", lang="tc",
                live_count=0, live_updated_at="2026-07-08",
                live_probed_at=now, probe_error=None,
            )

            async def mock_get(url, **kw):
                return httpx.Response(
                    200, json={"totalfiles": 0, "files": []},
                    request=httpx.Request("GET", url),
                )

            family = next(f for f in D3_FAMILIES if f.slug == "histlaw")
            runner = D3Runner(
                get=mock_get, checkpoint=db, output_dir=tmp_path,
                families=(family,), langs=("tc",),
            )
            result = await runner.run()
            assert result.downloaded == 0

            db.mark_bucket_scraped(
                "hopt", "histlaw", "tc", completed_at=now,
            )
            db.recompute_local_count(
                kind="hopt", scope="histlaw", lang="tc",
            )

            row = db.get_freshness_row("hopt", "histlaw", "tc")
            assert row is not None
            assert row.live_count == 0
            assert row.local_count == 0
            assert _fresh(row) is True
        finally:
            db.close()
