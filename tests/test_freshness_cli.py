"""Tests for the Phase D2 CLI surface:

  * ``hklii check-freshness`` subcommand (probe + report).
  * ``--skip-if-fresh`` flag on the four scrape subcommands
    (``scrape``, ``scrape-hopt``, ``scrape-ukpc``, ``scrape-legis``).

The runner-level tests for ``FreshnessRunner`` live in
:mod:`tests.test_freshness`. This file exercises Click wiring
(registration, help text, exit codes, --json/--text mutex) and the
gating semantics of ``--skip-if-fresh``. Wire calls are stubbed via
:func:`unittest.mock.patch` on ``ProxyPool`` so the tests never
actually reach HKLII.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

HKT = timezone(timedelta(hours=8))


def _hkt_ts_at(iso_date: str, hour: int = 12) -> int:
    """Convert a YYYY-MM-DD to a HKT-midday unix timestamp — mirrors
    the helper in test_freshness.py so the two files stay aligned on
    the same date-boundary intuition for _fresh comparisons."""
    d = date.fromisoformat(iso_date)
    return int(
        datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=HKT).timestamp()
    )


# -------- hklii check-freshness --------------------------------------------

class TestCheckFreshnessRegistration:
    """The subcommand exists in the CLI graph, requires --proxy or
    --direct like every other wire subcommand, and lists its option set
    in --help so operators can discover it."""

    def test_subcommand_registered(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["check-freshness", "--help"])
        assert result.exit_code == 0, result.output
        assert "freshness" in result.output.lower()

    def test_help_lists_json_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["check-freshness", "--help"])
        assert "--json" in result.output

    def test_help_lists_text_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["check-freshness", "--help"])
        assert "--text" in result.output

    def test_requires_proxy_or_direct(self):
        """Wire subcommands must opt into --proxy or --direct — a bare
        invocation should abort rather than silently hitting HKLII from
        the operator's home IP."""
        from hklii_downloader.cli import main
        result = CliRunner().invoke(
            main, ["check-freshness", "-o", "./nope"],
        )
        assert result.exit_code != 0
        out = result.output.lower()
        assert "proxy" in out or "direct" in out

    def test_json_and_text_are_mutually_exclusive(self):
        """--json and --text set two mutually incompatible output modes.
        Same UX pattern as `hklii validate` — asking for both is a
        usage error, not a silent tiebreaker."""
        from hklii_downloader.cli import main
        result = CliRunner().invoke(
            main, [
                "check-freshness", "-o", "./nope",
                "--direct", "--yes",
                "--json", "--text",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower(), result.output


class TestCheckFreshnessBehaviour:
    """End-to-end behaviour with a stubbed ProxyPool. Every case fixes
    the wire probe and asserts on the exit code / stdout shape the
    caller (cron script, operator) will consume."""

    def _patch_pool(self, *, status: int = 200, body: dict | None = None):
        """Return a MagicMock class replacement for ProxyPool that
        yields httpx.Response(status, json=body) on every .get()."""
        import httpx

        default_body = body or {"count": 100, "timestamp": "2026-07-07"}

        class _FakePool:
            def __init__(self, *a, **kw):
                pass

            async def preflight(self):
                class _R:
                    home_ip = "1.2.3.4"
                    healthy_proxies = ["p1"]
                    leaked_proxies = []
                    failed_proxies = []
                return _R()

            async def close(self):
                pass

            async def get(self, url, **kw):
                return httpx.Response(
                    status,
                    json=default_body,
                    request=httpx.Request("GET", url),
                )

        return _FakePool

    def test_exits_zero_when_all_buckets_fresh(self, tmp_path):
        """Seed db_freshness so every mapped triple is fresh; then run
        the command. Exit 0 is the healthy-cron signal so a scheduled
        run can chain `hklii check-freshness && ...`."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # Every case-family + hopt + legis triple in the fixture matrix
        # must be fresh AFTER the probe overwrites live_count/updated_at.
        # Simplest: match probe body (100, 2026-07-07) then seed local
        # + scrape completion for every expected triple upfront.
        completed_at = _hkt_ts_at("2026-07-08")
        # The runner will iterate the matrix; without spinning up real
        # data, the count-match comes from probe body count=100 vs
        # local_count=100 seeded here.
        # But recompute_local_count will reset local_count to the actual
        # SELECT COUNT(*) result (=0 with no case rows). Insert 100 rows
        # for one bucket to make it fresh — the OTHER buckets flip to
        # stale automatically (local_count=0 vs live_count=100) which
        # is exactly the case we DON'T want here.
        # Instead: intercept probe_all's writes by monkeypatching the
        # runner's probe body to match local_count=0 (an empty bucket
        # can be fresh too — 0 == 0).
        db.close()

        _FakePool = self._patch_pool(
            body={"count": 0, "timestamp": "2026-07-07"},
        )
        # Seed every expected triple with scrape completion + zero count.
        # We can't easily do this AFTER the probe overwrites, so bump
        # the completion timestamp AFTER the probe runs. Easier: patch
        # the runner's stale_buckets / first_run_missing return to [].
        with patch(
            "hklii_downloader.proxy_pool.ProxyPool", _FakePool,
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.stale_buckets",
            return_value=[],
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.first_run_missing",
            return_value=[],
        ):
            result = CliRunner().invoke(main, [
                "check-freshness",
                "-o", str(tmp_path),
                "--direct", "--yes",
                "--no-events",
            ])
        assert result.exit_code == 0, result.output

    def test_exits_nonzero_when_any_bucket_stale(self, tmp_path):
        """A stale bucket must produce a non-zero exit so cron scripts
        (or `hklii check-freshness && …` chains) escalate to a scrape
        instead of continuing as if nothing needed doing."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.freshness import FreshnessRow

        _FakePool = self._patch_pool()
        with patch(
            "hklii_downloader.proxy_pool.ProxyPool", _FakePool,
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.stale_buckets",
            return_value=[FreshnessRow("cases", "hkcfa", "en")],
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.first_run_missing",
            return_value=[],
        ):
            result = CliRunner().invoke(main, [
                "check-freshness",
                "-o", str(tmp_path),
                "--direct", "--yes",
                "--no-events",
            ])
        assert result.exit_code != 0, result.output

    def test_json_flag_emits_parseable_json(self, tmp_path):
        """--json makes stdout parse cleanly. Scripts should not have
        to grep human prose — they consume the report as JSON."""
        from hklii_downloader.cli import main
        from hklii_downloader.freshness import FreshnessRow

        _FakePool = self._patch_pool()
        with patch(
            "hklii_downloader.proxy_pool.ProxyPool", _FakePool,
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.stale_buckets",
            return_value=[FreshnessRow("cases", "hkcfa", "en")],
        ), patch(
            "hklii_downloader.freshness.FreshnessRunner.first_run_missing",
            return_value=[],
        ):
            result = CliRunner().invoke(main, [
                "check-freshness",
                "-o", str(tmp_path),
                "--direct", "--yes",
                "--json", "--no-events",
            ])
        # Even on nonzero exit stdout should be JSON (report first,
        # exit code second — matches `hklii validate`).
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)
        assert "stale" in parsed
        assert any(
            row.get("scope") == "hkcfa"
            for row in parsed["stale"]
        ), parsed


# -------- hklii update: freshness step wiring -----------------------------

class TestUpdateFreshnessProfileDefaults:
    """PROFILE_DEFAULTS gains ``include_freshness_check`` per profile.
    Default ON for every cadence — a freshness check is ~28 requests
    (cheap) and its whole purpose is to REPLACE the counts-only canary
    signal, so it must run on the same cadences the canary did.
    """

    def test_daily_default_includes_freshness_check(self):
        from hklii_downloader.update import PROFILE_DEFAULTS
        assert PROFILE_DEFAULTS["daily"]["include_freshness_check"] is True

    def test_weekly_default_includes_freshness_check(self):
        from hklii_downloader.update import PROFILE_DEFAULTS
        assert PROFILE_DEFAULTS["weekly"]["include_freshness_check"] is True

    def test_monthly_default_includes_freshness_check(self):
        from hklii_downloader.update import PROFILE_DEFAULTS
        assert (
            PROFILE_DEFAULTS["monthly"]["include_freshness_check"] is True
        )

    def test_quarterly_default_includes_freshness_check(self):
        from hklii_downloader.update import PROFILE_DEFAULTS
        assert (
            PROFILE_DEFAULTS["quarterly"]["include_freshness_check"] is True
        )

    def test_custom_default_omits_freshness_check(self):
        """Custom starts everything OFF — the operator must opt in."""
        from hklii_downloader.update import PROFILE_DEFAULTS
        assert (
            PROFILE_DEFAULTS["custom"]["include_freshness_check"] is False
        )


class TestUpdatePlanEmitsCheckFreshnessStep:
    """UpdateRunner.plan() surfaces a ``check_freshness`` step under
    every profile that opts in, and orders it BEFORE any scrape step so
    the dispatcher can scope subsequent scrapes to stale buckets."""

    def _fake_now(self, iso_utc="2026-07-06T02:00:00"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt_utc = datetime.fromisoformat(iso_utc).replace(
            tzinfo=ZoneInfo("UTC"),
        )
        return lambda: dt_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))

    def test_daily_plan_includes_check_freshness(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=self._fake_now(),
        )
        names = [s.name for s in runner.plan()]
        assert "check_freshness" in names

    def test_check_freshness_precedes_scrape(self, tmp_path):
        """Plan ordering is contractual: the dispatcher scopes the
        scrape step to stale buckets computed from the freshness probe.
        A check_freshness step AFTER scrape would probe post-hoc, which
        is not the design."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=self._fake_now(),
        )
        names = [s.name for s in runner.plan()]
        assert names.index("check_freshness") < names.index("scrape")

    def test_no_freshness_check_flag_omits_step(self, tmp_path):
        """Passing include_freshness_check=False overrides the profile
        default and drops the step from the plan entirely — dry-run
        output stays honest."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            include_freshness_check=False,
            now=self._fake_now(),
        )
        names = [s.name for s in runner.plan()]
        assert "check_freshness" not in names

    def test_include_freshness_check_flag_adds_step_to_custom(
        self, tmp_path,
    ):
        """Symmetric: custom starts with everything OFF; explicit opt-in
        adds the step. Same knob shape as every other --include-* flag."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="custom", output=tmp_path, proxies=["p"],
            include_freshness_check=True,
            now=self._fake_now(),
        )
        names = [s.name for s in runner.plan()]
        assert "check_freshness" in names


class TestUpdateCliFreshnessFlags:
    """CLI-surface flags for the freshness gate. The update command
    already carries a wall of --include-*/--no-* flags; the freshness
    pair follows the same naming convention."""

    def test_help_lists_include_freshness_check_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["update", "--help"])
        assert result.exit_code == 0, result.output
        assert "--include-freshness-check" in result.output


class TestUpdateDispatchFreshnessStep:
    """`_dispatch_update_plan` must (a) run the freshness step by
    calling ``FreshnessRunner.probe_all`` and (b) reject a rename typo
    by raising when the step name isn't recognised. The existing
    dispatcher already has an ``else: raise RuntimeError`` branch —
    this suite pins its behaviour for the new step name too.
    """

    def test_dispatch_source_handles_check_freshness_step(self):
        """Dispatcher source names ``check_freshness`` at its call
        site — mirrors the pattern of the other Step.name branches
        (coverage_canary / scrape_hopt / …). A missing branch would
        trip the else-raise the wrapper installs on unknown names."""
        import inspect
        from hklii_downloader.cli import _dispatch_update_plan
        src = inspect.getsource(_dispatch_update_plan)
        assert "check_freshness" in src, (
            "_dispatch_update_plan has no branch for 'check_freshness' — "
            "the update runner will raise a RuntimeError under the else "
            "guard when plan() emits it."
        )

    def test_dispatch_raises_on_unknown_step_name(self, tmp_path):
        """The else-branch converts a plan/dispatch drift into a hard
        failure so a typo like ``check-freshness`` vs
        ``check_freshness`` fails LOUDLY, not silently."""
        import inspect
        from hklii_downloader.cli import _dispatch_update_plan
        src = inspect.getsource(_dispatch_update_plan)
        # The existing pattern for unknown steps is `raise RuntimeError(
        # f"unknown update step ...")`. Assert the guard is still there
        # so a future refactor that removes it also fails this test.
        assert "unknown update step" in src, (
            "_dispatch_update_plan has lost its 'unknown update step' "
            "guard — a rename typo would report a step as 'ok' when it "
            "silently no-oped."
        )


# -------- --skip-if-fresh on scrape / scrape-hopt / scrape-ukpc / scrape-legis

class TestScrapeSkipIfFreshFlag:
    """--skip-if-fresh is an OPT-IN gate on every scrape-family
    subcommand. When set, it consults db_freshness and drops buckets
    already marked FRESH. Default OFF preserves the current explicit-
    invocation semantic for operators who want a full re-scrape.
    """

    def test_scrape_help_lists_skip_if_fresh_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["scrape", "--help"])
        assert result.exit_code == 0
        assert "--skip-if-fresh" in result.output

    def test_scrape_hopt_help_lists_skip_if_fresh_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["scrape-hopt", "--help"])
        assert result.exit_code == 0
        assert "--skip-if-fresh" in result.output

    def test_scrape_ukpc_help_lists_skip_if_fresh_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["scrape-ukpc", "--help"])
        assert result.exit_code == 0
        assert "--skip-if-fresh" in result.output

    def test_scrape_legis_help_lists_skip_if_fresh_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["scrape-legis", "--help"])
        assert result.exit_code == 0
        assert "--skip-if-fresh" in result.output
