"""Tests for CLI — Click group with download and scrape subcommands."""
from __future__ import annotations

from click.testing import CliRunner

from hklii_downloader.cli import ALL_COURTS, main


class TestAllCourts:
    def test_ukpc_removed_from_all_courts(self):
        """UKPC (UK Privy Council) is a UK court that heard HK appeals until
        1997. HKLII's `ukpc` slug is currently EMPTY — `getmetacase(ukpc,en)`
        returns count=0 and `getmetacase(ukpc,tc)` returns HTTP 500. Local
        DB has 0 cases + 0 citations referencing it. Fetching it every daily
        was pure waste (and the tc bucket 500-aborted the scrape step).

        UKPC judgments live at BAILII (https://www.bailii.org/uk/cases/UKPC/)
        and jcpc.uk — foreign jurisdiction from HK's perspective. Not our
        corpus.

        If HKLII ever populates ukpc, this test is the reversal point:
        delete the assertion and re-add the slug.
        """
        assert "ukpc" not in ALL_COURTS

    def test_all_courts_length_is_12(self):
        """Pin the count. Any silent add/remove flips this test."""
        assert len(ALL_COURTS) == 12


class TestCLIGroup:
    def test_main_is_group(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "download" in result.output
        assert "Commands" in result.output or "Usage" in result.output

    def test_no_subcommand_shows_help(self):
        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "download" in result.output

    def test_scrape_in_group_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "scrape" in result.output


class TestDownloadSubcommand:
    def test_download_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["download", "--help"])
        assert result.exit_code == 0
        assert "--proxy" in result.output
        assert "--direct" in result.output

    def test_download_requires_proxy_or_direct(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "download", "https://www.hklii.hk/en/cases/hkcfi/2023/1",
        ])
        assert result.exit_code != 0
        assert "proxy" in result.output.lower() or "direct" in result.output.lower()

    def test_download_proxy_and_direct_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "download",
            "https://www.hklii.hk/en/cases/hkcfi/2023/1",
            "--proxy", "http://localhost:8888",
            "--direct",
        ])
        assert result.exit_code != 0

    def test_download_accepts_direct_flag(self):
        runner = CliRunner()
        result = runner.invoke(main, ["download", "--direct", "--help"])
        assert result.exit_code == 0

    def test_download_accepts_proxy_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["download", "--proxy", "http://localhost:8888", "--help"])
        assert result.exit_code == 0

    def test_download_format_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["download", "--help"])
        assert "--format" in result.output or "-f" in result.output


class TestScrapeSubcommand:
    def test_scrape_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert result.exit_code == 0
        assert "--proxy" in result.output
        assert "--direct" in result.output
        assert "--limit" in result.output
        assert "--allow-doc" in result.output
        assert "--resume" in result.output
        assert "--courts" in result.output

    def test_scrape_requires_proxy_or_direct(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape"])
        assert result.exit_code != 0
        assert "proxy" in result.output.lower() or "direct" in result.output.lower()

    def test_scrape_direct_requires_yes(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--direct"], input="n\n")
        assert result.exit_code != 0 or "confirm" in result.output.lower() or "abort" in result.output.lower()

    def test_scrape_direct_with_yes_skips_confirmation(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--direct", "--yes", "--help"])
        assert result.exit_code == 0

    def test_scrape_multiple_proxies(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert "--proxy" in result.output

    def test_scrape_help_lists_enrichment_flags(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert "--with-summaries" in result.output
        assert "--with-appeal-history" in result.output

    def test_scrape_help_lists_lang_flag(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert "--lang" in result.output

    def test_scrape_help_lists_retry_failed_flag(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert "--retry-failed" in result.output

    def test_scrape_resume_skips_enumeration_when_pending(self, tmp_path):
        """When there are pending cases and --resume is set, don't
        re-enumerate — just download what's left."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.close()

        enumerate_calls = 0

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        async def counting_enumerate(self, courts, langs=("en", "tc")):
            nonlocal enumerate_calls
            enumerate_calls += 1
            return 0

        async def noop_download_all(self, on_progress=None):
            from hklii_downloader.scraper import ScrapeResult
            return ScrapeResult(downloaded=0, failed=0)

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper.enumerate", counting_enumerate), \
             patch("hklii_downloader.scraper.BulkScraper.download_all",
                   noop_download_all):
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--resume",
            ])
        assert result.exit_code == 0, result.output
        assert enumerate_calls == 0, (
            f"--resume with pending rows should skip enumerate, "
            f"got {enumerate_calls} calls"
        )

    def test_scrape_resume_without_pending_still_enumerates(self, tmp_path):
        """--resume with an empty checkpoint has nothing to resume, so
        still enumerate."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        CheckpointDB(str(out / ".checkpoint.db")).close()

        enumerate_calls = 0

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        async def counting_enumerate(self, courts, langs=("en", "tc")):
            nonlocal enumerate_calls
            enumerate_calls += 1
            return 0

        async def noop_download_all(self, on_progress=None):
            from hklii_downloader.scraper import ScrapeResult
            return ScrapeResult(downloaded=0, failed=0)

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper.enumerate", counting_enumerate), \
             patch("hklii_downloader.scraper.BulkScraper.download_all",
                   noop_download_all):
            runner = CliRunner()
            runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--resume",
            ])
        assert enumerate_calls == 1

    def test_scrape_retry_failed_resets_before_enumeration(self, tmp_path):
        """--retry-failed calls reset_failed_to_pending BEFORE the
        enumerate step, so the retried rows are already pending when
        the download loop starts."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_failed("hkcfi", 2023, 1, "HTTP 403")
        assert db.stats()["failed"] == 1
        db.close()

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        async def noop_enumerate(self, courts, langs=("en", "tc")): return 0
        async def noop_download_all(self, on_progress=None):
            from hklii_downloader.scraper import ScrapeResult
            return ScrapeResult(downloaded=0, failed=0)

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper.enumerate", noop_enumerate), \
             patch("hklii_downloader.scraper.BulkScraper.download_all",
                   noop_download_all):
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--retry-failed",
            ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        stats = db.stats()
        assert stats["pending"] == 1, f"expected 1 pending, got {stats}"
        assert stats["failed"] == 0
        db.close()

    def test_scrape_lang_flag_reaches_enumerate(self, tmp_path):
        """--lang en should skip the tc sweep."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult

        out = tmp_path / "out"
        out.mkdir()
        captured = {}

        def make_capturing_bulkscraper():
            from hklii_downloader import scraper as scraper_mod
            OrigBulkScraper = scraper_mod.BulkScraper

            class CapturingBulkScraper(OrigBulkScraper):
                async def enumerate(self, courts, langs=("en", "tc")):
                    captured["langs"] = tuple(langs)
                    return 0
            return CapturingBulkScraper

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper", make_capturing_bulkscraper()), \
             patch("hklii_downloader.cli.BulkScraper",
                   make_capturing_bulkscraper(), create=True):
            runner = CliRunner()
            runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--lang", "en",
            ])
        assert captured.get("langs") == ("en",)

    def test_scrape_lang_default_is_both(self, tmp_path):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult

        out = tmp_path / "out"
        out.mkdir()
        captured = {}

        def make_capturing_bulkscraper():
            from hklii_downloader import scraper as scraper_mod
            OrigBulkScraper = scraper_mod.BulkScraper

            class CapturingBulkScraper(OrigBulkScraper):
                async def enumerate(self, courts, langs=("en", "tc")):
                    captured["langs"] = tuple(langs)
                    return 0
            return CapturingBulkScraper

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper", make_capturing_bulkscraper()), \
             patch("hklii_downloader.cli.BulkScraper",
                   make_capturing_bulkscraper(), create=True):
            runner = CliRunner()
            runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
            ])
        assert captured.get("langs") == ("en", "tc")

    def test_scrape_default_no_enrichment(self, tmp_path):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.close()

        captured = {}

        real_scraper_init = None

        def make_capturing_bulkscraper():
            from hklii_downloader import scraper as scraper_mod
            OrigBulkScraper = scraper_mod.BulkScraper

            class CapturingBulkScraper(OrigBulkScraper):
                def __init__(self, *args, **kwargs):
                    captured["with_summaries"] = kwargs.get("with_summaries", False)
                    captured["with_appeal_history"] = kwargs.get("with_appeal_history", False)
                    super().__init__(*args, **kwargs)
            return CapturingBulkScraper

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        async def noop_enumerate(self, courts, langs=("en", "tc")): return 0

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper", make_capturing_bulkscraper()), \
             patch("hklii_downloader.cli.BulkScraper",
                   make_capturing_bulkscraper(), create=True):
            runner = CliRunner()
            runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
            ])
        assert captured.get("with_summaries") is False
        assert captured.get("with_appeal_history") is False

class TestVerifySubcommand:
    def test_verify_in_group_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "verify" in result.output

    def test_verify_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["verify", "--help"])
        assert result.exit_code == 0
        assert "--output" in result.output or "-o" in result.output

    def test_verify_flips_broken_rows(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.upsert_case("hkcfi", 2023, 1, "N", "T", "2023-01-01")
        db.claim_pending()
        db.mark_downloaded("hkcfi", 2023, 1, ["html"])
        db.close()
        # No files exist under out/hkcfi/2023/

        runner = CliRunner()
        result = runner.invoke(main, ["verify", "-o", str(out)])
        assert result.exit_code == 0
        assert "1" in result.output  # broken count somewhere

        db = CheckpointDB(str(out / ".checkpoint.db"))
        assert db.stats()["pending"] == 1
        db.close()


class TestEnrichSubcommand:
    def test_enrich_in_group_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "enrich" in result.output

    def test_enrich_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["enrich", "--help"])
        assert result.exit_code == 0
        assert "--proxy" in result.output
        assert "--summaries" in result.output or "--no-summaries" in result.output
        assert "--appeal-history" in result.output or "--no-appeal-history" in result.output

    def test_enrich_requires_proxy_or_direct(self):
        runner = CliRunner()
        result = runner.invoke(main, ["enrich"])
        assert result.exit_code != 0

    def _real_enrich(self, tmp_path, extra_args=None):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        # enrich requires the DB to exist
        CheckpointDB(str(out / ".checkpoint.db")).close()

        captured = {}

        def make_capturing_runner():
            from hklii_downloader import enrichment as mod
            OrigRunner = mod.EnrichmentRunner

            class Capture(OrigRunner):
                def __init__(self, *args, **kwargs):
                    captured["do_summaries"] = kwargs.get("do_summaries", True)
                    captured["do_appeal_history"] = kwargs.get("do_appeal_history", True)
                    super().__init__(*args, **kwargs)

                async def enrich_all(self, on_progress=None):
                    from hklii_downloader.enrichment import EnrichmentResult
                    return EnrichmentResult(processed=0, failed=0)
            return Capture

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.enrichment.EnrichmentRunner",
                   make_capturing_runner()), \
             patch("hklii_downloader.cli.EnrichmentRunner",
                   make_capturing_runner(), create=True):
            runner = CliRunner()
            args = ["enrich", "-p", "http://localhost:8888", "-o", str(out)]
            if extra_args:
                args += extra_args
            result = runner.invoke(main, args)
        return captured, result

    def test_enrich_default_flags(self, tmp_path):
        captured, result = self._real_enrich(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured.get("do_summaries") is True
        assert captured.get("do_appeal_history") is True

    def test_enrich_no_summaries(self, tmp_path):
        captured, result = self._real_enrich(tmp_path, ["--no-summaries"])
        assert captured.get("do_summaries") is False
        assert captured.get("do_appeal_history") is True

    def test_enrich_no_appeal_history(self, tmp_path):
        captured, result = self._real_enrich(tmp_path, ["--no-appeal-history"])
        assert captured.get("do_summaries") is True
        assert captured.get("do_appeal_history") is False


    def test_scrape_flags_reach_scraper(self, tmp_path):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        db.close()

        captured = {}

        def make_capturing_bulkscraper():
            from hklii_downloader import scraper as scraper_mod
            OrigBulkScraper = scraper_mod.BulkScraper

            class CapturingBulkScraper(OrigBulkScraper):
                def __init__(self, *args, **kwargs):
                    captured["with_summaries"] = kwargs.get("with_summaries", False)
                    captured["with_appeal_history"] = kwargs.get("with_appeal_history", False)
                    super().__init__(*args, **kwargs)
            return CapturingBulkScraper

        async def ok_preflight(self):
            return PreflightResult(home_ip="203.0.113.1",
                                    healthy_proxies=["http://localhost:8888"])

        async def noop_enumerate(self, courts, langs=("en", "tc")): return 0

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper", make_capturing_bulkscraper()), \
             patch("hklii_downloader.cli.BulkScraper",
                   make_capturing_bulkscraper(), create=True):
            runner = CliRunner()
            runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--with-summaries",
                "--with-appeal-history",
            ])
        assert captured.get("with_summaries") is True
        assert captured.get("with_appeal_history") is True

    def test_scrape_releases_in_progress_before_reporting_pending(self, tmp_path):
        """Pending count printed before download must include recovered
        in_progress records so the Rich progress bar's target isn't short
        by N after a Ctrl-C-then-resume (finding #4)."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db_path = out / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        for i in range(5):
            db.upsert_case("hkcfi", 2023, i + 1, f"[2023] HKCFI {i+1}",
                           f"Case {i+1}", "2023-01-01")
        db.claim_pending()
        db.claim_pending()
        assert db.stats()["in_progress"] == 2
        assert db.stats()["pending"] == 3
        db.close()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
                leaked_proxies=[],
                failed_proxies=[],
            )

        async def noop_enumerate(self, courts, langs=("en", "tc")):
            return 0  # skip enumeration, use existing DB rows

        async def noop_download_all(self, on_progress=None):
            from hklii_downloader.scraper import ScrapeResult
            return ScrapeResult(downloaded=0, failed=0)

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper.enumerate", noop_enumerate), \
             patch("hklii_downloader.scraper.BulkScraper.download_all",
                   noop_download_all):
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
            ])

        assert "Pending: 5" in result.output, (
            f"expected Pending count to include released in_progress rows, "
            f"got:\n{result.output}"
        )

    def test_scrape_target_clamps_limit_to_pending(self, tmp_path):
        """When --limit exceeds pending, target must clamp to pending so
        the Rich progress bar total matches what download_all can actually
        do (finding #8)."""
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        for i in range(3):
            db.upsert_case("hkcfi", 2023, i + 1, f"[2023] HKCFI {i+1}",
                           f"Case {i+1}", "2023-01-01")
        db.close()

        captured = {}

        async def capture_download_with_progress(scraper, target):
            from hklii_downloader.scraper import ScrapeResult
            captured["target"] = target
            return ScrapeResult(downloaded=0, failed=0)

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_enumerate(self, courts, langs=("en", "tc")):
            return 0

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight), \
             patch("hklii_downloader.scraper.BulkScraper.enumerate", noop_enumerate), \
             patch("hklii_downloader.cli._download_with_progress",
                   capture_download_with_progress):
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--limit", "100",
            ])

        assert captured.get("target") == 3, (
            f"target should clamp to pending=3 when --limit=100, "
            f"got target={captured.get('target')}. Output:\n{result.output}"
        )

    def test_scrape_exits_cleanly_when_all_proxies_dead(self, tmp_path):
        """If preflight kills every proxy (all leaked or unreachable), the
        CLI must exit with a clear UsageError instead of proceeding with
        workers=1 and crashing later inside enumerate on AllProxiesDeadError.
        """
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult

        async def dead_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=[],
                leaked_proxies=["http://localhost:8888 returned home IP"],
                failed_proxies=[],
            )

        with patch(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", dead_preflight,
        ):
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(tmp_path / "out"),
            ])

        assert result.exit_code != 0, (
            f"expected non-zero exit, got 0 with output:\n{result.output}"
        )
        msg = (result.output or "").lower()
        assert "no healthy proxies" in msg or "no healthy proxy" in msg, (
            f"expected message about no healthy proxies, got:\n{result.output}"
        )


class TestMonitorSubcommand:
    def _healthy_db(self, tmp_path):
        import time
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        seen = int(time.time()) - 60  # 1 min ago → runtime <1h, no rate alert
        for i in range(1, 6):
            db.upsert_case("hkcfi", 2024, i, f"N{i}", f"T{i}",
                           "2024-01-01", last_seen_at=seen)
        for i in range(1, 4):
            db.mark_downloaded("hkcfi", 2024, i, ["html"])
        db.close()
        return out

    def test_monitor_in_group_help(self):
        result = CliRunner().invoke(main, ["--help"])
        assert "monitor" in result.output

    def test_monitor_help_lists_flags(self):
        result = CliRunner().invoke(main, ["monitor", "--help"])
        assert result.exit_code == 0
        for flag in ("--output", "--window-min", "--workers", "--json", "--quiet"):
            assert flag in result.output, f"{flag} missing from monitor --help"

    def test_healthy_exits_0(self, tmp_path):
        out = self._healthy_db(tmp_path)
        result = CliRunner().invoke(main, ["monitor", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert "HEALTHY" in result.output

    def test_missing_db_exits_2(self, tmp_path):
        out = tmp_path / "empty"
        out.mkdir()
        result = CliRunner().invoke(main, ["monitor", "-o", str(out)])
        assert result.exit_code == 2
        assert "checkpoint DB not found" in result.output

    def test_workers_flag_changes_severity(self, tmp_path):
        import time
        from hklii_downloader.checkpoint import CheckpointDB
        out = tmp_path / "out"
        out.mkdir()
        db = CheckpointDB(str(out / ".checkpoint.db"))
        seen = int(time.time()) - 60
        for i in range(1, 13):
            db.upsert_case("hkcfi", 2024, i, f"N{i}", f"T{i}",
                           "2024-01-01", last_seen_at=seen)
        for _ in range(10):  # 10 in_progress
            db.claim_pending()
        db.close()
        # workers=2 → in_progress alert threshold 8 → 10 > 8 → critical
        crit = CliRunner().invoke(main, ["monitor", "-o", str(out), "--workers", "2"])
        assert crit.exit_code == 2, crit.output
        # workers=20 → threshold 80 → not critical
        ok = CliRunner().invoke(main, ["monitor", "-o", str(out), "--workers", "20"])
        assert ok.exit_code == 0, ok.output

    def test_json_flag_emits_json(self, tmp_path):
        import json as _json
        out = self._healthy_db(tmp_path)
        result = CliRunner().invoke(main, ["monitor", "-o", str(out), "--json"])
        assert result.exit_code == 0
        obj = _json.loads(result.output)
        assert obj["severity"] == "HEALTHY"

    def test_quiet_suppresses_output(self, tmp_path):
        out = self._healthy_db(tmp_path)
        result = CliRunner().invoke(main, ["monitor", "-o", str(out), "--quiet"])
        assert result.exit_code == 0
        assert result.output.strip() == ""


class TestEventsWiring:
    """The observability layer is opt-out: `scrape` / `enrich` / `recheck-html`
    construct a StructuredEventLogger from -o by default (creating
    <output>/events.jsonl), and `--no-events` skips it for storage-constrained
    runs."""

    def _patches(self):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.scraper import ScrapeResult

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_enumerate(self, courts, langs=("en", "tc")):
            return 0

        async def noop_download(self, on_progress=None):
            return ScrapeResult(downloaded=0, failed=0)

        return (
            patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight),
            patch("hklii_downloader.scraper.BulkScraper.enumerate", noop_enumerate),
            patch("hklii_downloader.scraper.BulkScraper.download_all", noop_download),
        )

    def test_scrape_creates_events_jsonl_by_default(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            result = CliRunner().invoke(main, [
                "scrape", "-p", "http://localhost:8888", "-o", str(out),
            ])
        assert result.exit_code == 0, result.output
        assert (out / "events.jsonl").exists(), (
            "scrape should construct an EventLogger from -o and create events.jsonl"
        )

    def test_scrape_no_events_flag_skips_events_jsonl(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            result = CliRunner().invoke(main, [
                "scrape", "-p", "http://localhost:8888", "-o", str(out),
                "--no-events",
            ])
        assert result.exit_code == 0, result.output
        assert not (out / "events.jsonl").exists(), (
            "--no-events must skip events.jsonl creation"
        )

    def test_enrich_creates_events_jsonl_by_default(self, tmp_path):
        from unittest.mock import patch
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.checkpoint import CheckpointDB

        out = tmp_path / "out"
        out.mkdir()
        # enrich requires an existing checkpoint DB.
        CheckpointDB(str(out / ".checkpoint.db")).close()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        with patch("hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight):
            result = CliRunner().invoke(main, [
                "enrich", "-p", "http://localhost:8888", "-o", str(out),
            ])
        assert result.exit_code == 0, result.output
        assert (out / "events.jsonl").exists(), (
            "enrich should construct an EventLogger from -o and create events.jsonl"
        )

    def test_all_three_commands_expose_no_events_flag(self):
        runner = CliRunner()
        for cmd in ("scrape", "enrich", "recheck-html"):
            out = runner.invoke(main, [cmd, "--help"]).output
            assert "--no-events" in out, (
                f"`{cmd} --help` must document --no-events; got:\n{out}"
            )


class TestCliProxyDirectMutex:
    """The --proxy/--direct mutex must fire BEFORE the callback runs, for
    every subcommand that accepts both flags.

    Round 4 review found a bypass: `MutuallyExclusiveOption` at cli.py:18
    checked `opts["proxy"]`, but scrape/enrich/recheck-html declare their
    dest as `"proxies"` (multiple=True). Additionally, enrich and
    recheck-html didn't apply the class at all. A live repro
    (`hklii scrape -p http://127.0.0.1:9999 --direct -y`) hit hklii.hk
    from the home IP and got 155555 cases back.

    Tests use `-y` so the --direct confirm prompt cannot short-circuit
    the mutex and mask a broken check. Tests mock ProxyPool.preflight so
    a broken mutex fails fast (with a "no healthy proxies" UsageError
    that has the *wrong* message, so the "mutually exclusive" assertion
    catches the bypass) instead of hanging on real network I/O.
    """

    def _patch_asyncio_run_noop(self):
        """Patch asyncio.run inside the cli module to close the coroutine
        and return None, so a broken mutex fails fast with exit_code 0
        (wrong signal for the 'mutually exclusive' assertion) instead of
        hanging on real network I/O. A working mutex fires during option
        parsing, well before asyncio.run is even reached — so the mock
        is never called in the passing case."""
        from unittest.mock import patch

        def noop_run(coro):
            coro.close()
            return None

        return patch("hklii_downloader.cli.asyncio.run", noop_run)

    def test_scrape_proxy_and_direct_are_mutually_exclusive(self, tmp_path):
        with self._patch_asyncio_run_noop():
            runner = CliRunner()
            result = runner.invoke(main, [
                "scrape",
                "-p", "http://127.0.0.1:9999",
                "--direct",
                "-y",
                "-o", str(tmp_path / "out"),
            ])
        assert result.exit_code == 2, (
            f"expected UsageError exit code 2, got {result.exit_code}. "
            f"Output:\n{result.output}"
        )
        assert "mutually exclusive" in result.output.lower(), (
            f"expected 'mutually exclusive' in output, got:\n{result.output}"
        )

    def test_enrich_proxy_and_direct_are_mutually_exclusive(self, tmp_path):
        with self._patch_asyncio_run_noop():
            runner = CliRunner()
            result = runner.invoke(main, [
                "enrich",
                "-p", "http://127.0.0.1:9999",
                "--direct",
                "-y",
                "-o", str(tmp_path / "out"),
            ])
        assert result.exit_code == 2, (
            f"expected UsageError exit code 2, got {result.exit_code}. "
            f"Output:\n{result.output}"
        )
        assert "mutually exclusive" in result.output.lower(), (
            f"expected 'mutually exclusive' in output, got:\n{result.output}"
        )

    def test_recheck_html_proxy_and_direct_are_mutually_exclusive(self, tmp_path):
        with self._patch_asyncio_run_noop():
            runner = CliRunner()
            result = runner.invoke(main, [
                "recheck-html",
                "-p", "http://127.0.0.1:9999",
                "--direct",
                "-y",
                "-o", str(tmp_path / "out"),
            ])
        assert result.exit_code == 2, (
            f"expected UsageError exit code 2, got {result.exit_code}. "
            f"Output:\n{result.output}"
        )
        assert "mutually exclusive" in result.output.lower(), (
            f"expected 'mutually exclusive' in output, got:\n{result.output}"
        )
