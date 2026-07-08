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


class TestUpdateScrapeConsumesFreshnessResult:
    """Regression pins for adversarial D2 finding #2:
    ``_run_update_check_freshness`` populates db_freshness, but
    ``_run_update_scrape`` hardcoded ``court_list=ALL_COURTS`` and
    ignored what the freshness step wrote. Net effect on every
    ``hklii update -p daily`` etc: ~28 wire probes wasted, then the
    scrape rescans every court × en/tc regardless.

    The fix reads db_freshness right before the scrape step and
    filters (court, lang) buckets whose ``_fresh`` returns True.
    Verified two ways: (a) an all-fresh state skips ``_run_scrape``
    entirely, (b) a partly-stale state passes a REDUCED court_list to
    ``_run_scrape``.
    """

    def _seed_fresh(self, db, kind, scope, lang):
        """Same _seed_fresh as TestSkipIfFreshFilterHelpers — puts
        the (kind, scope, lang) triple into a state _fresh() accepts."""
        db.upsert_freshness_probe(
            kind, scope, lang,
            live_count=100, live_updated_at="2026-07-07",
            live_probed_at=1_720_000_000, probe_error=None,
        )
        db._conn.execute(
            "UPDATE db_freshness SET local_count=100, local_counted_at=? "
            "WHERE kind=? AND scope=? AND lang=?",
            (1_720_000_100, kind, scope, lang),
        )
        db._conn.commit()
        db.mark_bucket_scraped(
            kind, scope, lang,
            completed_at=_hkt_ts_at("2026-07-08"),
        )

    def test_update_scrape_skips_run_when_every_case_bucket_is_fresh(
        self, tmp_path,
    ):
        """If db_freshness marks every ALL_COURTS × en/tc pair FRESH,
        ``_run_update_scrape`` must NOT invoke ``_run_scrape``. The
        prior implementation wasted every probe by scraping anyway."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import ALL_COURTS, _run_update_scrape
        from hklii_downloader.update import Step, UpdateRunner

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        for court in ALL_COURTS:
            for lang in ("en", "tc"):
                self._seed_fresh(db, "cases", court, lang)
        db.close()

        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
        )
        step = Step(name="scrape", kwargs={
            "recent_days": 30, "items_per_page": 500,
            "min_date": None, "max_date": None, "sort": None,
            "allow_doc": True,
            "with_summaries": True, "with_appeal_history": True,
        })

        with patch(
            "hklii_downloader.cli._run_scrape",
            new=AsyncMock(),
        ) as m_run_scrape:
            asyncio.run(_run_update_scrape(
                runner, step, no_events=True,
            ))

        assert m_run_scrape.await_count == 0, (
            "_run_update_scrape invoked _run_scrape even though every "
            "case bucket in db_freshness is FRESH — the update run "
            "burned probe cost on check_freshness for nothing. See "
            "finding #2."
        )

    def test_update_scrape_narrows_court_list_when_some_are_fresh(
        self, tmp_path,
    ):
        """Half the courts fresh, half stale → ``_run_scrape`` runs
        but with a court_list that DROPS the fresh ones. Pins the
        wire between db_freshness and the ScrapeConfig."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import ALL_COURTS, _run_update_scrape
        from hklii_downloader.update import Step, UpdateRunner

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # Mark the first 6 courts fresh; leave the rest first-run so
        # they'll be treated as stale by _filter_fresh_case_buckets.
        fresh_courts = ALL_COURTS[:6]
        stale_courts = ALL_COURTS[6:]
        for court in fresh_courts:
            for lang in ("en", "tc"):
                self._seed_fresh(db, "cases", court, lang)
        db.close()

        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
        )
        step = Step(name="scrape", kwargs={
            "recent_days": 30, "items_per_page": 500,
            "min_date": None, "max_date": None, "sort": None,
            "allow_doc": True,
            "with_summaries": True, "with_appeal_history": True,
        })

        with patch(
            "hklii_downloader.cli._run_scrape",
            new=AsyncMock(),
        ) as m_run_scrape:
            asyncio.run(_run_update_scrape(
                runner, step, no_events=True,
            ))

        assert m_run_scrape.await_count == 1, (
            "_run_update_scrape should have invoked _run_scrape once "
            "for the stale-bucket half"
        )
        cfg = m_run_scrape.await_args.args[0]
        assert set(cfg.court_list) == set(stale_courts), (
            "court_list passed to _run_scrape includes fresh courts — "
            "the freshness-based scoping did not land. Got: "
            f"{sorted(cfg.court_list)}. Expected: {sorted(stale_courts)}."
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


class TestSkipIfFreshFilterHelpers:
    """Behavioural tests for the two db_freshness → surviving-buckets
    filter helpers. Pins the semantic that a FRESH bucket is one
    where :func:`_fresh` returns True AND the row exists —
    first-run and probe-error rows must always pass through
    (fail-safe = scrape).
    """

    def _seed_fresh(self, db, kind, scope, lang):
        """Match test_freshness._seed_fresh — probe body, local count,
        scrape completion all consistent with ``_fresh_row()`` fixture."""
        db.upsert_freshness_probe(
            kind, scope, lang,
            live_count=100, live_updated_at="2026-07-07",
            live_probed_at=1_720_000_000, probe_error=None,
        )
        db._conn.execute(
            "UPDATE db_freshness SET local_count=100, local_counted_at=? "
            "WHERE kind=? AND scope=? AND lang=?",
            (1_720_000_100, kind, scope, lang),
        )
        db._conn.commit()
        db.mark_bucket_scraped(
            kind, scope, lang,
            completed_at=_hkt_ts_at("2026-07-08"),
        )

    def test_case_filter_drops_fresh_court_when_all_langs_fresh(
        self, tmp_path,
    ):
        """A court whose en AND tc are both fresh drops out of the
        scrape scope entirely — no wasted enum for that court."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _filter_fresh_case_buckets

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        self._seed_fresh(db, "cases", "hkcfa", "en")
        self._seed_fresh(db, "cases", "hkcfa", "tc")
        db.close()

        courts, langs = _filter_fresh_case_buckets(
            tmp_path, ["hkcfa", "hkca"], ("en", "tc"),
        )
        assert courts == ["hkca"]  # hkcfa dropped, hkca kept.
        assert langs == ("en", "tc")

    def test_case_filter_keeps_court_when_any_lang_stale(self, tmp_path):
        """If en is fresh but tc has never probed, the court must
        stay in the scrape scope — otherwise tc-lang buckets
        silently drift."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _filter_fresh_case_buckets

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        self._seed_fresh(db, "cases", "hkcfa", "en")
        # tc has no db_freshness row → first-run → NOT fresh.
        db.close()

        courts, _ = _filter_fresh_case_buckets(
            tmp_path, ["hkcfa"], ("en", "tc"),
        )
        assert courts == ["hkcfa"], (
            "hkcfa/tc has no ledger row (first-run) — must pass "
            "through the filter as stale"
        )

    def test_case_filter_keeps_probe_error_bucket(self, tmp_path):
        """A probe_error row must pass the filter as stale. Encodes
        fresh_definition rule (b): can't confirm freshness →
        conservatively scrape."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _filter_fresh_case_buckets

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        self._seed_fresh(db, "cases", "hkcfa", "en")
        # Poison the row with a probe_error → STALE per _fresh rule (b).
        db._conn.execute(
            "UPDATE db_freshness SET probe_error='HTTP 500' "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        )
        db._conn.commit()
        db.close()

        courts, _ = _filter_fresh_case_buckets(
            tmp_path, ["hkcfa"], ("en",),
        )
        assert courts == ["hkcfa"]

    def test_case_filter_returns_all_courts_when_no_db(self, tmp_path):
        """No checkpoint DB (first-ever run) → no filter possible;
        every bucket passes through as stale. Matches the fail-safe
        posture — scrape rather than silently no-op."""
        from hklii_downloader.cli import _filter_fresh_case_buckets

        courts, langs = _filter_fresh_case_buckets(
            tmp_path, ["hkcfa", "hkca"], ("en", "tc"),
        )
        assert courts == ["hkcfa", "hkca"]
        assert langs == ("en", "tc")

    def test_hopt_filter_drops_abbr_when_all_langs_fresh(self, tmp_path):
        """kind='hopt' dispatch: bacpg with all langs fresh drops out."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _filter_fresh_hopt_buckets

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        self._seed_fresh(db, "hopt", "bacpg", "en")
        self._seed_fresh(db, "hopt", "bacpg", "tc")
        db.close()

        abbrs, _ = _filter_fresh_hopt_buckets(
            tmp_path, ("bacpg", "hkts"), ("en", "tc"), kind="hopt",
        )
        assert abbrs == ("hkts",)

    def test_legis_filter_kind_reads_legis_rows_not_hopt(self, tmp_path):
        """kind='legis' must dispatch to freshness rows under kind='legis',
        not kind='hopt'. Regression pin — the initial helper switch
        gets a kind param and the caller MUST pass 'legis' from
        scrape-legis."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _filter_fresh_hopt_buckets

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # Seed ord as fresh under kind='legis'; if the helper read
        # kind='hopt' instead, the row wouldn't be found and ord
        # would stay in the scope.
        self._seed_fresh(db, "legis", "ord", "en")
        self._seed_fresh(db, "legis", "ord", "tc")
        # And a SEPARATE 'hopt' bucket for the SAME slug shape — if
        # the helper dispatched to hopt by mistake, it might match
        # this row by coincidence.
        self._seed_fresh(db, "hopt", "ord", "en")
        db.close()

        abbrs, _ = _filter_fresh_hopt_buckets(
            tmp_path, ("ord", "reg"), ("en", "tc"), kind="legis",
        )
        assert abbrs == ("reg",), (
            "kind='legis' filter should read legis rows only — "
            "ord is fresh under legis and MUST drop out"
        )


class TestLoadDefaultMatrix:
    """`discovery.load_default_matrix` is dev-mode scaffolding until
    D3 lands. Pin its contract so a future packaging change doesn't
    silently return an empty matrix (which would let the freshness
    step no-op)."""

    def test_returns_nonempty_matrix(self):
        from hklii_downloader.discovery import load_default_matrix
        matrix = load_default_matrix()
        assert len(matrix.cases) > 0, "matrix.cases is empty"

    def test_includes_hkcfa_bilingual(self):
        """Sanity: hkcfa exists in the fixture with both en+tc so the
        freshness runner has real triples to iterate."""
        from hklii_downloader.discovery import load_default_matrix
        matrix = load_default_matrix()
        assert "hkcfa" in matrix.cases
        assert set(matrix.cases["hkcfa"]) >= {"en", "tc"}

    def test_matrix_matches_databases_parser(self):
        """The default matrix must equal the direct-parse of the same
        fixture — no accidental filtering or transformation between
        the two entry points."""
        from pathlib import Path
        from hklii_downloader.discovery import (
            load_default_matrix,
            parse_databases_matrix,
        )
        here = Path(__file__).resolve().parent
        fixture = here / "fixtures" / "databases_page_rendered_2026-07-08.html"
        direct = parse_databases_matrix(fixture.read_text())
        loaded = load_default_matrix()
        assert direct.cases == loaded.cases
        assert direct.legis == loaded.legis
        assert direct.other == loaded.other


class TestPackagedMatrixFixture:
    """Regression pins for adversarial D2 finding #5: load_default_matrix()
    must not depend on tests/fixtures/ because ``tests/`` is not part of
    the built wheel (only ``src/hklii_downloader`` is — see pyproject.toml
    ``[tool.hatch.build.targets.wheel] packages``). Any operator who ran
    ``pip install .`` (non-editable) or installed a wheel from PyPI got a
    broken ``hklii check-freshness`` and a broken ``hklii update`` with
    the default freshness step ON — both crash on FileNotFoundError
    inside the freshness step handler, aborting the entire update run
    because ``check_freshness`` is emitted at plan index 0.
    """

    def test_packaged_matrix_fixture_ships_with_wheel(self):
        """The fixture is inside the installed package tree so it ships
        with the wheel — checked via importlib.resources.files, the
        wheel-safe lookup API. If this fails on a clean install, the
        wheel does not include the fixture and load_default_matrix()
        cannot resolve it.
        """
        from importlib.resources import files
        pkg_data = files("hklii_downloader") / "data" / "databases_matrix.html"
        assert pkg_data.is_file(), (
            f"Packaged fixture missing at {pkg_data}. Move the fixture "
            "into src/hklii_downloader/data/databases_matrix.html and "
            "list the data/ subtree under "
            "[tool.hatch.build.targets.wheel.force-include] so the wheel "
            "includes it."
        )

    def test_load_default_matrix_works_without_tests_fixture(
        self, monkeypatch,
    ):
        """Simulate a wheel install: patch the tests/fixtures/ path
        lookup so it always misses (as it would beyond an editable
        checkout). ``load_default_matrix()`` must still return a
        populated matrix by reading the packaged copy under
        ``src/hklii_downloader/data/``.
        """
        from pathlib import Path as _RealPath

        real_is_file = _RealPath.is_file

        def _blocked_is_file(self):
            # Any lookup path that traverses tests/fixtures/... is treated
            # as if it doesn't exist — models the wheel-install condition
            # where the tests directory is not shipped.
            if "tests/fixtures/databases_page_rendered" in str(self):
                return False
            return real_is_file(self)

        monkeypatch.setattr(_RealPath, "is_file", _blocked_is_file)
        from hklii_downloader.discovery import load_default_matrix
        matrix = load_default_matrix()
        assert len(matrix.cases) > 0, (
            "load_default_matrix returned an empty matrix when the "
            "tests/fixtures/ path was unreachable — it must fall back "
            "to the packaged data/ copy under src/hklii_downloader/."
        )


class TestCheckFreshnessFixtureIntegration:
    """End-to-end: the check-freshness subcommand goes through the
    real DatabaseMatrix (from the fixture). Wire the ProxyPool +
    HTTP get stub so we exercise probe_all against a realistic
    matrix without hitting HKLII."""

    def _patch_healthy_pool(
        self, count: int = 100, timestamp: str = "2026-07-07",
    ):
        import httpx

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
                    200,
                    json={"count": count, "timestamp": timestamp},
                    request=httpx.Request("GET", url),
                )

        return _FakePool

    def test_first_run_produces_stale_report_and_nonzero_exit(
        self, tmp_path,
    ):
        """Empty DB + healthy wire → every triple probes cleanly BUT
        has no ``last_scrape_completed_at`` yet, so ``_fresh`` returns
        False under rule (e). The command must exit nonzero and the
        stale list must include the never-scraped triples — that's the
        primary cron-boot signal: a fresh checkout must not silently
        pass its freshness gate.
        """
        from hklii_downloader.cli import main

        _FakePool = self._patch_healthy_pool()
        with patch(
            "hklii_downloader.proxy_pool.ProxyPool", _FakePool,
        ):
            result = CliRunner().invoke(main, [
                "check-freshness",
                "-o", str(tmp_path),
                "--direct", "--yes",
                "--json", "--no-events",
            ])
        assert result.exit_code != 0, result.output
        payload = json.loads(result.output)
        assert payload["healthy"] > 0
        # After probe_all every mapped triple has a row, so first_run
        # is empty. But every row has last_scrape_completed_at IS NULL,
        # so stale_buckets is populated. Split explicitly — the CLI
        # exit test above is on (stale ∪ first_run), the two lists
        # separately let a scripted consumer distinguish the two.
        assert payload["first_run"] == []
        assert len(payload["stale"]) > 0, (
            "stale list is empty even though no bucket has "
            "last_scrape_completed_at set"
        )


class TestScrapeRunnerMarksBuckets:
    """Regression pins for adversarial D2 finding #1: every scrape
    runner (BulkScraper, HoptRunner, LegisRunner, UkpcRunner) must
    call ``CheckpointDB.mark_bucket_scraped`` for each (kind, scope,
    lang) it swept, so ``_fresh`` rule (e) can eventually flip a
    bucket to FRESH.

    Pre-fix, no scrape runner touched mark_bucket_scraped anywhere —
    ``grep -rn mark_bucket_scraped src/`` returned only the module
    definition + the FreshnessRunner delegator. In production
    ``last_scrape_completed_at`` stayed NULL forever, every bucket
    always failed rule (e), ``hklii check-freshness`` could not exit
    0, and any cron script chained ``check-freshness && ...`` chain-
    failed every run. This suite proves the wire lands.
    """

    def test_scrape_marks_case_buckets_on_clean_completion(
        self, tmp_path,
    ):
        """After ``_run_scrape`` completes cleanly for court_list ×
        langs, every (cases, court, lang) triple must have a
        ``last_scrape_completed_at`` set in ``db_freshness``. Without
        this the freshness gate never flips FRESH.
        """
        from unittest.mock import patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.scraper import ScrapeResult

        out = tmp_path / "out"
        out.mkdir()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_enumerate(self, courts, langs=("en", "tc")):
            return 0

        async def noop_download_all(self, on_progress=None):
            return ScrapeResult(downloaded=0, failed=0)

        with patch(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight,
        ), patch(
            "hklii_downloader.scraper.BulkScraper.enumerate", noop_enumerate,
        ), patch(
            "hklii_downloader.scraper.BulkScraper.download_all",
            noop_download_all,
        ):
            result = CliRunner().invoke(main, [
                "scrape",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--courts", "hkcfa",
                "--lang", "en",
            ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            rec = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert rec is not None, (
            "no db_freshness row for cases/hkcfa/en after scrape — "
            "mark_bucket_scraped is not wired into the scrape "
            "runner. See finding #1."
        )
        assert rec.last_scrape_completed_at is not None, (
            "db_freshness row exists but last_scrape_completed_at is "
            "NULL — scrape completed without touching the freshness "
            "ledger. _fresh rule (e) will keep every bucket STALE. "
            "See finding #1."
        )

    def test_scrape_hopt_marks_hopt_buckets_on_clean_completion(
        self, tmp_path,
    ):
        """Same wiring for scrape-hopt: HoptRunner must flag each
        (hopt, abbr, lang) bucket after ``fetch_pending`` completes."""
        from unittest.mock import patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.hopt import HoptRunResult
        from hklii_downloader.proxy_pool import PreflightResult

        out = tmp_path / "out"
        out.mkdir()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_enumerate_all(self):
            return 0

        async def noop_fetch_pending(self, on_progress=None):
            return HoptRunResult(downloaded=0, failed=0)

        with patch(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight,
        ), patch(
            "hklii_downloader.hopt.HoptRunner.enumerate_all", noop_enumerate_all,
        ), patch(
            "hklii_downloader.hopt.HoptRunner.fetch_pending", noop_fetch_pending,
        ):
            result = CliRunner().invoke(main, [
                "scrape-hopt",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--abbr", "hkts",
                "--lang", "en",
            ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            rec = db.get_freshness_row("hopt", "hkts", "en")
        finally:
            db.close()
        assert rec is not None, (
            "no db_freshness row for hopt/hkts/en after scrape-hopt — "
            "wire missing. See finding #1."
        )
        assert rec.last_scrape_completed_at is not None, (
            "hopt/hkts/en db_freshness row has NULL "
            "last_scrape_completed_at. See finding #1."
        )

    def test_scrape_legis_marks_legis_buckets_on_clean_completion(
        self, tmp_path,
    ):
        """scrape-legis wiring — LegisRunner marks (legis, cap_type,
        lang) buckets after fetch_pending completes."""
        from unittest.mock import patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.legis import LegisRunResult
        from hklii_downloader.proxy_pool import PreflightResult

        out = tmp_path / "out"
        out.mkdir()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_enumerate_all(self):
            return 0

        async def noop_fetch_pending(self, on_progress=None):
            return LegisRunResult(downloaded=0, failed=0)

        with patch(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight,
        ), patch(
            "hklii_downloader.legis.LegisRunner.enumerate_all",
            noop_enumerate_all,
        ), patch(
            "hklii_downloader.legis.LegisRunner.fetch_pending",
            noop_fetch_pending,
        ):
            result = CliRunner().invoke(main, [
                "scrape-legis",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--abbr", "ord",
                "--lang", "en",
            ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            rec = db.get_freshness_row("legis", "ord", "en")
        finally:
            db.close()
        assert rec is not None, (
            "no db_freshness row for legis/ord/en after scrape-legis — "
            "wire missing. See finding #1."
        )
        assert rec.last_scrape_completed_at is not None, (
            "legis/ord/en db_freshness row has NULL "
            "last_scrape_completed_at. See finding #1."
        )

    def test_scrape_ukpc_marks_ukpc_bucket_on_clean_completion(
        self, tmp_path,
    ):
        """scrape-ukpc wiring — UkpcRunner marks (cases, ukpc, lang)
        buckets after run() completes. UKPC lives at kind='cases'
        because its rows live in the cases table (see ukpc.py)."""
        from unittest.mock import patch

        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        from hklii_downloader.proxy_pool import PreflightResult
        from hklii_downloader.ukpc import UkpcRunResult

        out = tmp_path / "out"
        out.mkdir()

        async def ok_preflight(self):
            return PreflightResult(
                home_ip="203.0.113.1",
                healthy_proxies=["http://localhost:8888"],
            )

        async def noop_run(self, on_progress=None):
            return UkpcRunResult(downloaded=0, failed=0)

        with patch(
            "hklii_downloader.proxy_pool.ProxyPool.preflight", ok_preflight,
        ), patch(
            "hklii_downloader.ukpc.UkpcRunner.run", noop_run,
        ):
            result = CliRunner().invoke(main, [
                "scrape-ukpc",
                "-p", "http://localhost:8888",
                "-o", str(out),
                "--lang", "en",
            ])
        assert result.exit_code == 0, result.output

        db = CheckpointDB(str(out / ".checkpoint.db"))
        try:
            rec = db.get_freshness_row("cases", "ukpc", "en")
        finally:
            db.close()
        assert rec is not None, (
            "no db_freshness row for cases/ukpc/en after scrape-ukpc — "
            "wire missing. See finding #1."
        )
        assert rec.last_scrape_completed_at is not None, (
            "cases/ukpc/en db_freshness row has NULL "
            "last_scrape_completed_at. See finding #1."
        )
