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
