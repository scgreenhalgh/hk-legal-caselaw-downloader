"""Tests for citations.py — the getcasenoteup scraper."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB


class TestUrl:
    def test_getcasenoteup_url(self):
        from hklii_downloader.citations import getcasenoteup_url

        url = getcasenoteup_url(court="hkcfa", year=2023, num=32)
        assert url == (
            "https://www.hklii.hk/api/getcasenoteup"
            "?abbr=hkcfa&year=2023&num=32"
        )

    def test_getcasenoteup_url_omits_lang(self):
        """API-probe finding: lang param is silently ignored by
        getcasenoteup. Do not send it — one call returns mixed-lang
        results."""
        from hklii_downloader.citations import getcasenoteup_url

        url = getcasenoteup_url(court="hkcfi", year=2023, num=155)
        assert "lang=" not in url


class TestParseResponse:
    def test_parse_empty_response(self):
        from hklii_downloader.citations import parse_noteup_response

        parsed = parse_noteup_response([], target="hkcfa/2020/32")
        assert parsed.edges == []
        assert parsed.parallel_cites == []

    def test_parse_extracts_from_key_from_path(self):
        """The response entries carry the citer's `path`. Parser extracts
        the (court, year, num) triple and encodes as `court/year/num`."""
        from hklii_downloader.citations import parse_noteup_response

        entries = [
            {
                "neutral": "[2023] HKCFA 40",
                "path": "/en/cases/hkcfa/2023/40",
                "db": "Court of Final Appeal",
                "date": "2023-12-08T00:00:00+08:00",
                "citation_frequency": 1,
                "parallel": [],
                "cases": [{"title": "DAVID SUBOTIC AND OTHERS V. SFC"}],
            },
        ]
        parsed = parse_noteup_response(entries, target="hkcfa/2023/32")
        assert len(parsed.edges) == 1
        from_key, to_key, citer_lang, citer_freq, position = parsed.edges[0]
        assert from_key == "hkcfa/2023/40"
        assert to_key == "hkcfa/2023/32"
        assert citer_lang == "en"
        assert citer_freq == 1
        assert position == 0

    def test_parse_handles_tc_citers_in_same_response(self):
        """One call returns citers from BOTH corpora. TC path → citer_lang='tc'."""
        from hklii_downloader.citations import parse_noteup_response

        entries = [
            {"neutral": "[2023] HKDC 1180",
             "path": "/tc/cases/hkdc/2023/1180",
             "db": "區域法院", "date": "", "citation_frequency": 0,
             "parallel": [], "cases": []},
            {"neutral": "[2024] HKCA 854",
             "path": "/en/cases/hkca/2024/854",
             "db": "Court of Appeal", "date": "", "citation_frequency": 3,
             "parallel": [], "cases": []},
        ]
        parsed = parse_noteup_response(entries, target="hkcfa/2020/32")
        langs = {e[2] for e in parsed.edges}
        assert langs == {"en", "tc"}

    def test_parse_captures_parallel_cites(self):
        from hklii_downloader.citations import parse_noteup_response

        entries = [{
            "neutral": "[2023] HKCFA 40",
            "path": "/en/cases/hkcfa/2023/40",
            "db": "CFA", "date": "", "citation_frequency": 1,
            "parallel": ["[2023] 6 HKC 46", "(2023) 26 HKCFAR 200"],
            "cases": [],
        }]
        parsed = parse_noteup_response(entries, target="hkcfa/2023/32")
        assert set(parsed.parallel_cites) == {
            ("hkcfa/2023/40", "[2023] 6 HKC 46"),
            ("hkcfa/2023/40", "(2023) 26 HKCFAR 200"),
        }

    def test_parse_skips_malformed_paths(self):
        from hklii_downloader.citations import parse_noteup_response

        entries = [
            {"path": "/en/cases/hkcfa/2023/40",
             "citation_frequency": 0, "parallel": [], "cases": []},
            {"path": "/nonsense/path",
             "citation_frequency": 0, "parallel": [], "cases": []},
            {"path": "",
             "citation_frequency": 0, "parallel": [], "cases": []},
        ]
        parsed = parse_noteup_response(entries, target="hkcfa/2023/32")
        assert len(parsed.edges) == 1


class TestFetchOne:
    async def test_happy_path(self, tmp_path):
        from hklii_downloader.citations import fetch_noteup_for_case

        async def mock_get(url, **kw):
            return httpx.Response(
                200,
                json=[
                    {"neutral": "[2023] HKCFA 40",
                     "path": "/en/cases/hkcfa/2023/40",
                     "db": "CFA", "date": "",
                     "citation_frequency": 1, "parallel": [], "cases": []},
                ],
                request=httpx.Request("GET", url),
            )

        edges, parallels, raw = await fetch_noteup_for_case(
            get=mock_get, court="hkcfa", year=2023, num=32,
        )
        assert len(edges) == 1
        assert edges[0][0] == "hkcfa/2023/40"
        assert edges[0][1] == "hkcfa/2023/32"
        assert isinstance(raw, list)

    async def test_500_raises_noteup_fetch_error(self):
        from hklii_downloader.citations import (
            fetch_noteup_for_case, NoteupFetchError,
        )

        async def mock_get(url, **kw):
            return httpx.Response(
                500, text="err", request=httpx.Request("GET", url),
            )
        with pytest.raises(NoteupFetchError):
            await fetch_noteup_for_case(
                get=mock_get, court="hkcfa", year=2023, num=32,
            )


class TestSaveLocal:
    def test_writes_raw_sidecar(self, tmp_path):
        from hklii_downloader.citations import save_noteup_local

        raw = [{"neutral": "[2023] HKCFA 40", "path": "/en/cases/hkcfa/2023/40"}]
        save_noteup_local(
            output_dir=tmp_path,
            court="hkcfa", year=2023, num=32, raw=raw,
        )
        path = tmp_path / "hkcfa" / "2023" / "hkcfa_2023_32.noteup.json"
        assert path.exists()
        assert json.loads(path.read_text()) == raw


class TestRunner:
    async def test_enumerate_targets_downloaded_cases(self, tmp_path):
        """Membership guard: only enumerate rows we actually have on disk
        so we never call getcasenoteup for a case we can't validate."""
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_case("hkcfa", 2023, 32, "[2023] HKCFA 32",
                            "T", "2023-01-01")
            db.mark_downloaded("hkcfa", 2023, 32, ["html"])
            db.upsert_case("hkcfa", 2023, 99, "[2023] HKCFA 99",
                            "T", "2023-01-01")
            # note: 99 is pending, not downloaded

            runner = NoteupRunner(get=None, checkpoint=db,
                                    output_dir=tmp_path)
            n = runner.enumerate_pending()
            assert n == 1

            stats = db.noteup_stats()
            assert stats["pending"] == 1
            assert stats["total"] == 1
        finally:
            db.close()

    async def test_enumerate_is_idempotent(self, tmp_path):
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_case("hkcfa", 2023, 32, "N", "T", "2023-01-01")
            db.mark_downloaded("hkcfa", 2023, 32, ["html"])

            runner = NoteupRunner(get=None, checkpoint=db,
                                    output_dir=tmp_path)
            runner.enumerate_pending()
            runner.enumerate_pending()

            n = db._conn.execute(
                "SELECT COUNT(*) FROM noteup_fetches"
            ).fetchone()[0]
            assert n == 1
        finally:
            db.close()

    async def test_fetch_writes_edges_and_marks_ok(self, tmp_path):
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_case("hkcfa", 2020, 32, "N", "T", "2020-01-01")
            db.mark_downloaded("hkcfa", 2020, 32, ["html"])
            db.upsert_noteup_fetch("hkcfa", 2020, 32)

            async def mock_get(url, **kw):
                return httpx.Response(
                    200,
                    json=[
                        {"neutral": "[2023] HKCFA 40",
                         "path": "/en/cases/hkcfa/2023/40",
                         "db": "CFA", "date": "",
                         "citation_frequency": 1, "parallel": [],
                         "cases": []},
                    ],
                    request=httpx.Request("GET", url),
                )

            runner = NoteupRunner(get=mock_get, checkpoint=db,
                                    output_dir=tmp_path)
            result = await runner.fetch_pending()

            assert result.downloaded == 1
            assert result.failed == 0

            edge_count = db._conn.execute(
                "SELECT COUNT(*) FROM citations"
            ).fetchone()[0]
            assert edge_count == 1

            row = db._conn.execute(
                "SELECT status, edge_count FROM noteup_fetches"
            ).fetchone()
            assert row == ("ok", 1)
        finally:
            db.close()

    async def test_fetch_writes_parallel_cites_grouped_by_key(self, tmp_path):
        """Whole-codebase review (L4): the parallel_cites integration
        path (grouping by from_key + per-key bulk insert at
        citations.py:222-228) had NO test — test_fetch_writes_edges_
        and_marks_ok uses `parallel: []` so the by_key dict-grouping
        code was dead in tests. A regression removing the group-and-
        insert step would leave parallel cites permanently unsaved."""
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_case("hkcfa", 2020, 32, "N", "T", "2020-01-01")
            db.mark_downloaded("hkcfa", 2020, 32, ["html"])
            db.upsert_noteup_fetch("hkcfa", 2020, 32)

            async def mock_get(url, **kw):
                # Two entries, one with two parallel cites, one with
                # one — exercises the by_key grouping + per-key
                # insert_parallel_cites call.
                return httpx.Response(
                    200,
                    json=[
                        {"neutral": "[2023] HKCFA 40",
                         "path": "/en/cases/hkcfa/2023/40",
                         "db": "CFA", "date": "",
                         "citation_frequency": 1,
                         "parallel": ["[2023] 4 HKC 100", "[2023] HKLRD 55"],
                         "cases": []},
                        {"neutral": "[2024] HKCFA 5",
                         "path": "/en/cases/hkcfa/2024/5",
                         "db": "CFA", "date": "",
                         "citation_frequency": 1,
                         "parallel": ["[2024] 2 HKC 200"],
                         "cases": []},
                    ],
                    request=httpx.Request("GET", url),
                )

            runner = NoteupRunner(get=mock_get, checkpoint=db,
                                    output_dir=tmp_path)
            result = await runner.fetch_pending()
            assert result.downloaded == 1
            assert result.failed == 0

            # Three parallel cites total, two distinct from_keys.
            all_pcs = db._conn.execute(
                "SELECT case_key, parallel_cite FROM case_parallel_cites "
                "ORDER BY case_key, parallel_cite"
            ).fetchall()
            assert len(all_pcs) == 3, all_pcs
            from_keys = {row[0] for row in all_pcs}
            assert len(from_keys) == 2, (
                f"expected two distinct from_keys, got {from_keys}"
            )
            # Group-by-from_key check: hkcfa/2023/40 has 2, /2024/5 has 1.
            counts_per_key: dict = {}
            for k, _pc in all_pcs:
                counts_per_key[k] = counts_per_key.get(k, 0) + 1
            assert set(counts_per_key.values()) == {1, 2}, counts_per_key
        finally:
            db.close()

    async def test_500_marks_failed(self, tmp_path):
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            db.upsert_case("hkcfa", 2020, 32, "N", "T", "2020-01-01")
            db.mark_downloaded("hkcfa", 2020, 32, ["html"])
            db.upsert_noteup_fetch("hkcfa", 2020, 32)

            async def mock_get(url, **kw):
                return httpx.Response(
                    500, text="err", request=httpx.Request("GET", url),
                )

            runner = NoteupRunner(get=mock_get, checkpoint=db,
                                    output_dir=tmp_path)
            result = await runner.fetch_pending()
            assert result.downloaded == 0
            assert result.failed == 1

            row = db._conn.execute(
                "SELECT status, error FROM noteup_fetches"
            ).fetchone()
            assert row[0] == "error"
            assert "500" in row[1] or "HTTP" in row[1]
        finally:
            db.close()

    async def test_sqlite_error_in_worker_does_not_terminate_run(self, tmp_path):
        """Whole-codebase review (L1 silent skip): pre-fix, a SQLite
        error inside the fetch worker's try block (from
        insert_citation_edges / insert_parallel_cites / mark_noteup_ok)
        wasn't caught by the two except clauses (NoteupFetchError,
        RequestError/OSError). It propagated out of asyncio.gather and
        terminated the whole run — one bad row killed every subsequent
        one.

        Guard: the worker must catch broader errors, mark_noteup_failed
        with the error, and continue processing sibling rows."""
        import sqlite3
        from unittest.mock import patch
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for n in (32, 33, 34):
                db.upsert_case("hkcfa", 2020, n, f"N{n}", "T", "2020-01-01")
                db.mark_downloaded("hkcfa", 2020, n, ["html"])
                db.upsert_noteup_fetch("hkcfa", 2020, n)

            async def mock_get(url, **kw):
                return httpx.Response(
                    200, json=[
                        {"neutral": "[2023] HKCFA 40",
                         "path": "/en/cases/hkcfa/2023/40",
                         "db": "CFA", "date": "", "citation_frequency": 1,
                         "parallel": [], "cases": []},
                    ],
                    request=httpx.Request("GET", url),
                )

            call_count = {"n": 0}
            real_insert = db.insert_citation_edges
            def fail_first(edges, first_seen):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise sqlite3.OperationalError("simulated DB failure")
                return real_insert(edges, first_seen)

            runner = NoteupRunner(get=mock_get, checkpoint=db,
                                    output_dir=tmp_path)
            with patch.object(db, "insert_citation_edges", side_effect=fail_first):
                result = await runner.fetch_pending()

            # Must NOT terminate — the remaining 2 rows still process
            # (order is non-deterministic across workers, but total
            # processed + failed should account for all 3).
            assert result.downloaded + result.failed == 3, (
                f"one SQLite failure terminated the run — "
                f"only {result.downloaded + result.failed} of 3 rows "
                "reached a terminal state"
            )
            # The row that hit the sqlite error must be marked failed.
            rows = db._conn.execute(
                "SELECT status, error FROM noteup_fetches WHERE status='error'"
            ).fetchall()
            assert len(rows) == 1
            assert "simulated DB failure" in rows[0][1]
        finally:
            db.close()

    async def test_limit_caps_fetches(self, tmp_path):
        from hklii_downloader.citations import NoteupRunner

        db = CheckpointDB(str(tmp_path / "cp.db"))
        try:
            for i in range(5):
                db.upsert_case("hkcfa", 2023, i + 1, "N", "T", "2023-01-01")
                db.mark_downloaded("hkcfa", 2023, i + 1, ["html"])

            calls = {"n": 0}
            async def mock_get(url, **kw):
                calls["n"] += 1
                return httpx.Response(
                    200, json=[], request=httpx.Request("GET", url),
                )

            runner = NoteupRunner(get=mock_get, checkpoint=db,
                                    output_dir=tmp_path, limit=2)
            runner.enumerate_pending()
            result = await runner.fetch_pending()

            assert result.downloaded == 2
            assert calls["n"] == 2
        finally:
            db.close()
