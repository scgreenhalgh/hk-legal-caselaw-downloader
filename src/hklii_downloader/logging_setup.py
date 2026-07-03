"""Logging bootstrap for long-run diagnostics.

Writes to <output>/<subcommand>.log so a multi-day scrape leaves
grep-able evidence when things go sideways under nohup.
"""
from __future__ import annotations

import logging
from pathlib import Path

_ROOT_LOGGER_NAME = "hklii_downloader"


def setup_logging(output_dir: Path, subcommand: str) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{subcommand}.log"

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(logging.INFO)

    # Clear pre-existing handlers so re-invocation doesn't duplicate lines
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    return log_path


def get_logger(name: str = _ROOT_LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)
