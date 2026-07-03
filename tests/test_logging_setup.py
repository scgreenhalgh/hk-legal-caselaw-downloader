"""Tests for logging_setup helper."""
from __future__ import annotations

import logging
from pathlib import Path


class TestSetupLogging:
    def test_creates_log_file_in_output_dir(self, tmp_path):
        from hklii_downloader.logging_setup import setup_logging
        out = tmp_path / "out"
        out.mkdir()
        setup_logging(out, subcommand="scrape")
        log = out / "scrape.log"
        logging.getLogger("hklii_downloader").info("hello")
        # Force flush handlers
        for h in logging.getLogger("hklii_downloader").handlers:
            h.flush()
        assert log.exists()
        content = log.read_text()
        assert "hello" in content

    def test_log_lines_include_level_and_timestamp(self, tmp_path):
        from hklii_downloader.logging_setup import setup_logging
        out = tmp_path / "out"
        out.mkdir()
        setup_logging(out, subcommand="scrape")
        logging.getLogger("hklii_downloader").warning("oh no")
        for h in logging.getLogger("hklii_downloader").handlers:
            h.flush()
        content = (out / "scrape.log").read_text()
        assert "WARNING" in content
        assert "oh no" in content
