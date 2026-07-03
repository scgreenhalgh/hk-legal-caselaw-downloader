"""Tests for CLI — Click group with download and scrape subcommands."""
from __future__ import annotations

from click.testing import CliRunner

from hklii_downloader.cli import main


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

    def test_scrape_proxy_and_direct_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "scrape",
            "--proxy", "http://localhost:8888",
            "--direct",
        ])
        assert result.exit_code != 0

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

        async def noop_enumerate(self, courts): return 0

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

        async def noop_enumerate(self, courts): return 0

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

        async def noop_enumerate(self, courts):
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

        async def noop_enumerate(self, courts):
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
