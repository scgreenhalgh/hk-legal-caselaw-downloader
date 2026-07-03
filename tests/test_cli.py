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
