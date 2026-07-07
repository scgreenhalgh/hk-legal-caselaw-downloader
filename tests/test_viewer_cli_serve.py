"""Tests for `hklii serve` — Phase 5 viewer boot CLI (design §8).

CLI validation is testable end-to-end via ``CliRunner``. ``uvicorn.run``
is patched everywhere it would block — we assert it was called with the
right host/port/reload rather than actually boot a server (that path is
covered by the ad-hoc dev demo, not the automated suite).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from hklii_downloader.cli import main


# ---------------------------------------------------------------------
# Discovery + help
# ---------------------------------------------------------------------


def test_serve_appears_in_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output


def test_serve_help_lists_documented_options() -> None:
    """Design §8 pins four options: -o/--output, --fts, --port, --dev."""
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    text = result.output
    assert "--output" in text
    assert "--fts" in text
    assert "--port" in text
    assert "--dev" in text


# ---------------------------------------------------------------------
# Startup failure surfaces — design §8 lists four "must name the fix"
# error strings. Each has one test.
# ---------------------------------------------------------------------


def test_serve_missing_output_dir_exit_1_with_fix(tmp_path: Path) -> None:
    """`corpus root missing at {path}. Pass -o /path/to/output.`"""
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "-o", str(tmp_path / "nowhere")])
    assert result.exit_code == 1
    assert "corpus root missing" in result.output.lower()
    assert "-o" in result.output


def test_serve_missing_checkpoint_exit_1_with_fix(tmp_path: Path) -> None:
    r"""`checkpoint DB missing at {path}. Run \`hklii scrape\` first.`"""
    output = tmp_path / "output"
    output.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 1
    assert "checkpoint" in result.output.lower()
    assert "hklii scrape" in result.output.lower()


def test_serve_missing_fts_db_exit_1_with_fix(tmp_path: Path) -> None:
    r"""`FTS index missing at {path}. Run \`hklii viewer index\`.`"""
    output = tmp_path / "output"
    output.mkdir()
    (output / ".checkpoint.db").touch()
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 1
    assert "fts index missing" in result.output.lower()
    assert "hklii viewer index" in result.output.lower()


# ---------------------------------------------------------------------
# Healthy boot — uvicorn.run gets the right args.
# ---------------------------------------------------------------------


def _seed_healthy_corpus(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir()
    (output / ".checkpoint.db").touch()
    (output / "viewer.db").touch()
    return output


def test_serve_binds_localhost_by_default(tmp_path: Path) -> None:
    """127.0.0.1 hardcoded (design §8 'no --host flag in v1')."""
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_serve_port_flag_overrides_default(tmp_path: Path) -> None:
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-o", str(output), "--port", "9999"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["port"] == 9999


def test_serve_default_port_is_8787(tmp_path: Path) -> None:
    """Design §8 pins the default (avoids 8000/8080/3000 collisions)."""
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 0
    assert run.call_args.kwargs["port"] == 8787


def test_serve_default_fts_path_is_output_viewer_db(tmp_path: Path) -> None:
    """Default --fts is <output>/viewer.db (design §8)."""
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        with patch("hklii_downloader.viewer.cli.create_app") as create_app:
            runner = CliRunner()
            result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 0
    kwargs = create_app.call_args.kwargs
    assert kwargs["viewer_db"] == output / "viewer.db"
    assert kwargs["checkpoint_db"] == output / ".checkpoint.db"
    assert kwargs["output_root"] == output


def test_serve_custom_fts_path_used(tmp_path: Path) -> None:
    output = _seed_healthy_corpus(tmp_path)
    custom_fts = tmp_path / "other_fts.db"
    custom_fts.touch()
    with patch("hklii_downloader.viewer.cli.uvicorn.run"):
        with patch("hklii_downloader.viewer.cli.create_app") as create_app:
            runner = CliRunner()
            result = runner.invoke(main, [
                "serve", "-o", str(output), "--fts", str(custom_fts),
            ])
    assert result.exit_code == 0
    assert create_app.call_args.kwargs["viewer_db"] == custom_fts


def test_serve_dev_enables_reload_and_opens_browser(tmp_path: Path) -> None:
    """--dev: uvicorn reload=True + webbrowser.open() (§8 exact spec)."""
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        with patch("hklii_downloader.viewer.cli.webbrowser.open") as open_:
            runner = CliRunner()
            result = runner.invoke(main, ["serve", "-o", str(output), "--dev"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["reload"] is True
    open_.assert_called_once()
    # Design §8 line 244: browser open wrapped in try/except so a non-GUI
    # environment doesn't crash the server.
    assert open_.call_args.args[0].startswith("http://127.0.0.1")


def test_serve_dev_browser_open_error_does_not_crash(tmp_path: Path) -> None:
    """Non-GUI OS: webbrowser.open raises OSError. CLI must swallow it
    (design §8) so the server still boots.
    """
    import webbrowser as wb

    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        with patch(
            "hklii_downloader.viewer.cli.webbrowser.open",
            side_effect=wb.Error("no browser"),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["serve", "-o", str(output), "--dev"])
    assert result.exit_code == 0
    run.assert_called_once()


def test_serve_non_dev_does_not_open_browser(tmp_path: Path) -> None:
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run"):
        with patch("hklii_downloader.viewer.cli.webbrowser.open") as open_:
            runner = CliRunner()
            result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 0
    open_.assert_not_called()


def test_serve_non_dev_reload_is_false(tmp_path: Path) -> None:
    output = _seed_healthy_corpus(tmp_path)
    with patch("hklii_downloader.viewer.cli.uvicorn.run") as run:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-o", str(output)])
    assert result.exit_code == 0
    assert run.call_args.kwargs.get("reload") in (False, None)
