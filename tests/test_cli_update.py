"""Tests for `hklii update` — profile-driven incremental refresh command.

The runner composes existing idempotent subcommands into daily/weekly/monthly/
quarterly cadences with lean date-window enumeration. Tests exercise:
- Profile → plan step composition
- Kwarg propagation (recent-days, items-per-page, recheck-max-age)
- Guards (--yes-narrow for windows <2 days; orphan-mark requires full-reconcile)
- HKT-anchored date computation
- Advisory lock file
- Dry-run output
"""
from __future__ import annotations

import fcntl
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner


def _fake_now(iso_utc: str):
    """Return a callable that produces `iso_utc` (UTC) converted to HKT."""
    dt_utc = datetime.fromisoformat(iso_utc).replace(tzinfo=ZoneInfo("UTC"))
    def _now():
        return dt_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
    return _now


class TestUpdateRegistration:
    def test_update_subcommand_registered(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["update", "--help"])
        assert result.exit_code == 0, result.output
        assert "update" in result.output.lower()

    def test_update_help_lists_profile_flag(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["update", "--help"])
        assert "--profile" in result.output, result.output

    def test_update_help_lists_include_ukpc_flag(self):
        """--include-ukpc / --no-ukpc — required so the operator can
        opt into a UKPC-only refresh outside the weekly cadence."""
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["update", "--help"])
        assert "--include-ukpc" in result.output, result.output

    def test_update_requires_proxy_or_direct(self):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, ["update", "-o", "./nope"])
        assert result.exit_code != 0
        assert "proxy" in result.output.lower() or "direct" in result.output.lower()


class TestUpdateRunnerProfilePlans:
    """`UpdateRunner.plan()` returns an ordered list[Step] whose composition
    depends on --profile and --include-*/--no-* overrides."""

    def test_daily_plan_includes_expected_steps(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path,
            proxies=["p"], now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape" in names
        assert "recheck_html" in names
        assert "generate_html" in names
        assert "scrape_noteup" in names
        assert "enrich" in names
        assert "coverage_canary" in names
        # backfill_case_translations belongs in daily: `--lang both` picks
        # EN-when-both-exist during scrape, so bilingual TC sidecars lag
        # by a day until this runs. Cheap (~5 calls/day for new bilingual
        # cases) so we keep it daily rather than deferring to monthly.
        assert "backfill_case_translations" in names
        # Excluded on daily
        assert "scrape_hopt" not in names
        assert "scrape_legis" not in names
        assert "scrape_relatedcaps" not in names
        assert "backfill_legis_history" not in names
        assert "validate" not in names
        assert "full_reconcile" not in names
        assert "orphan_mark" not in names

    def test_daily_recent_days_defaults_to_30(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["recent_days"] == 30

    def test_daily_items_per_page_defaults_to_500(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["items_per_page"] == 500

    def test_daily_recheck_max_age_defaults_to_30(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        recheck = next(s for s in runner.plan() if s.name == "recheck_html")
        assert recheck.kwargs["max_age_days"] == 30

    def test_daily_scrape_sends_min_and_max_date(self, tmp_path):
        """Both bounds set → snapshot pagination is frozen against mid-run
        publications (adversarial correctness #4)."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["min_date"] is not None
        assert scrape.kwargs["max_date"] is not None

    def test_daily_scrape_sort_is_dash_date(self, tmp_path):
        """sort=-date matches HKLII UI fingerprint under narrow window."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["sort"] == "-date"

    def test_weekly_adds_hopt_and_legis(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="weekly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_hopt" in names
        assert "scrape_legis" in names

    def test_daily_excludes_ukpc(self, tmp_path):
        """Daily default is EN case-family scrape only; UKPC (hopt-C)
        picks up on weekly rhythm alongside scrape_hopt/scrape_legis
        because the UKPC listing changes rarely (~1 record/month)."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_ukpc" not in names

    def test_weekly_adds_ukpc(self, tmp_path):
        """UKPC integration slot — same cadence as hopt/legis."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="weekly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_ukpc" in names

    def test_monthly_and_quarterly_include_ukpc(self, tmp_path):
        """Both cadences inherit the ukpc slot from weekly semantics."""
        from hklii_downloader.update import UpdateRunner
        for prof in ("monthly", "quarterly"):
            runner = UpdateRunner(
                profile=prof, output=tmp_path, proxies=["p"],
                now=_fake_now("2026-07-06T02:00:00"),
            )
            names = [s.name for s in runner.plan()]
            assert "scrape_ukpc" in names, (
                f"{prof} plan missing scrape_ukpc: {names}"
            )

    def test_include_ukpc_override_adds_step_on_daily(self, tmp_path):
        """Explicit --include-ukpc reaches the plan even under daily
        default (which otherwise excludes it)."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            include_ukpc=True,
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_ukpc" in names

    def test_no_ukpc_override_removes_step_on_weekly(self, tmp_path):
        """Explicit --no-ukpc suppresses the step even on weekly."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="weekly", output=tmp_path, proxies=["p"],
            include_ukpc=False,
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_ukpc" not in names

    def test_monthly_adds_translations_history_validate_but_not_relatedcaps(
        self, tmp_path,
    ):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="monthly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "backfill_case_translations" in names
        assert "backfill_legis_history" in names
        assert "validate" in names
        # Monthly deliberately excludes scrape_relatedcaps: ord/reg is
        # 100% locally-derivable via numeric-suffix pattern; quarterly
        # runs the fresh audit sweep.
        assert "scrape_relatedcaps" not in names
        # Still contains everything from weekly
        assert "scrape_hopt" in names
        assert "scrape_legis" in names

    def test_quarterly_still_includes_relatedcaps(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="quarterly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "scrape_relatedcaps" in names

    def test_monthly_recent_days_is_90(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="monthly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["recent_days"] == 90

    def test_quarterly_full_reconcile_and_orphan_mark(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="quarterly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "full_reconcile" in names
        assert "orphan_mark" in names

    def test_quarterly_recheck_unlimited(self, tmp_path):
        """max_age_days=0 → unbounded → catches HTML on ancient rows."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="quarterly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        recheck = next(s for s in runner.plan() if s.name == "recheck_html")
        assert recheck.kwargs["max_age_days"] == 0


class TestFormatPlanClockSnapshot:
    """format_plan reads the HKT clock ONCE across both the header line
    and every step kwarg — the block comment at update.py:468 promises
    this. Pre-fix, plan() and the header did separate reads; a run
    straddling HKT midnight could show `HKT today: 2026-07-07` beside
    a `min_date='06/06/2026'` (max_date='06/07/2026') derived from
    the previous day's snapshot — internally inconsistent."""

    def test_format_plan_reads_clock_once(self, tmp_path):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from hklii_downloader.update import UpdateRunner

        HKT = ZoneInfo("Asia/Hong_Kong")
        calls = {"n": 0}
        # Iterator so a second read would give a NEW HKT day.
        times = iter([
            datetime(2026, 7, 6, 23, 59, 59, 999_999, tzinfo=HKT),
            datetime(2026, 7, 7, 0, 0, 0, 0, tzinfo=HKT),
        ])

        def _fake():
            calls["n"] += 1
            return next(times)

        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"], now=_fake,
        )
        out = runner.format_plan()
        # If plan() and the header re-read the clock, calls["n"] > 1
        # and the header/date parts would drift across the day boundary.
        assert calls["n"] == 1, (
            f"format_plan called _now() {calls['n']}× — must snapshot"
        )
        # Both places reflect the FIRST read (2026-07-06):
        assert "2026-07-06" in out, out
        assert "2026-07-07" not in out, out
        # Scrape step window derived from HKT today = 2026-07-06:
        assert "min_date='06/06/2026'" in out, out
        assert "max_date='06/07/2026'" in out, out


class TestUpdateRunnerHKTClock:
    """HKT-anchored date-window boundaries — process TZ must NOT leak."""

    def test_hkt_boundary_uses_hkt_date(self, tmp_path):
        """At 16:30 UTC on 2026-07-05, process TZ (UTC) says today=2026-07-05.
        HKT (UTC+8) says today=2026-07-06 (crossed midnight already).
        Daily runner must compute today-30d from HKT → 06/06/2026, not 05/06/2026."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-05T16:30:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        # today HKT = 2026-07-06; today - 30d = 2026-06-06 → DD/MM/YYYY
        assert scrape.kwargs["min_date"] == "06/06/2026", (
            f"expected HKT date; got {scrape.kwargs['min_date']}"
        )
        assert scrape.kwargs["max_date"] == "06/07/2026"


class TestUpdateRunnerGuards:
    def test_recent_days_below_2_without_yes_narrow_raises(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        with pytest.raises(Exception) as exc:
            UpdateRunner(
                profile="custom", output=tmp_path, proxies=["p"],
                recent_days=1,
                now=_fake_now("2026-07-06T02:00:00"),
            )
        assert "yes-narrow" in str(exc.value).lower(), str(exc.value)

    def test_recent_days_1_with_yes_narrow_ok(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="custom", output=tmp_path, proxies=["p"],
            recent_days=1, yes_narrow=True,
            include_scrape=True,
            now=_fake_now("2026-07-06T02:00:00"),
        )
        scrape = next(s for s in runner.plan() if s.name == "scrape")
        assert scrape.kwargs["recent_days"] == 1

    def test_orphan_mark_requires_full_reconcile(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        with pytest.raises(Exception) as exc:
            UpdateRunner(
                profile="daily", output=tmp_path, proxies=["p"],
                include_orphan_mark=True,
                include_full_reconcile=False,
                now=_fake_now("2026-07-06T02:00:00"),
            )
        assert "full" in str(exc.value).lower() or "reconcile" in str(exc.value).lower()

    def test_narrow_window_plan_does_not_include_orphan_mark(self, tmp_path):
        """Even under daily profile, orphan-marking must NOT appear —
        it would delete-mark rows on a narrow-window enum."""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        names = [s.name for s in runner.plan()]
        assert "orphan_mark" not in names


class TestUpdateRunnerCoverageCanary:
    def test_canary_step_carries_threshold(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            canary_divergence_threshold=7,
            now=_fake_now("2026-07-06T02:00:00"),
        )
        canary = next(s for s in runner.plan() if s.name == "coverage_canary")
        assert canary.kwargs["threshold"] == 7

    def test_canary_step_caps_escalations(self, tmp_path):
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="daily", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        canary = next(s for s in runner.plan() if s.name == "coverage_canary")
        assert canary.kwargs.get("max_escalations") == 3


class TestCoverageCanaryFunction:
    """`coverage_canary()` compares live `getcasefiles?itemsPerPage=1`
    totalfiles against local DB row counts, per (court, lang) bucket.
    Returns divergent buckets sorted by absolute divergence, capped at
    max_escalations."""

    def _make_get(self, court_totals):
        """Build an async mock that returns count per court (getmetacase)."""
        import httpx
        async def _get(url, **kw):
            for court, langs in court_totals.items():
                if f"caseDb={court}&" in url or f"caseDb={court}" in url:
                    for lang, total in langs.items():
                        if f"lang={lang}" in url:
                            return httpx.Response(
                                200, json={
                                    "count": total,
                                    "timestamp": "2026-07-06",
                                },
                            )
            return httpx.Response(200, json={"count": 0, "timestamp": "2026-07-06"})
        return _get

    async def test_probes_every_court_lang_bucket(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        seen_urls = []

        import httpx
        async def _get(url, **kw):
            seen_urls.append(url)
            return httpx.Response(
                200, json={"count": 0, "timestamp": "2026-07-06"},
            )

        await coverage_canary(
            get=_get, checkpoint=db,
            courts=["hkcfi", "hkca"], langs=["en", "tc"],
            threshold=5,
        )
        assert len(seen_urls) == 4
        assert any("caseDb=hkcfi" in u and "lang=en" in u for u in seen_urls)
        assert any("caseDb=hkcfi" in u and "lang=tc" in u for u in seen_urls)
        assert any("caseDb=hkca" in u and "lang=en" in u for u in seen_urls)
        assert any("caseDb=hkca" in u and "lang=tc" in u for u in seen_urls)

    async def test_uses_getmetacase_endpoint(self, tmp_path):
        """Canary uses getmetacase — leaner than getcasefiles per fork
        research (40B vs 275B, ~7-46% faster)."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        seen = []
        import httpx
        async def _get(url, **kw):
            seen.append(url)
            return httpx.Response(200, json={"count": 0, "timestamp": "x"})
        await coverage_canary(
            get=_get, checkpoint=db,
            courts=["hkcfi"], langs=["en"], threshold=5,
        )
        assert "getmetacase" in seen[0], seen[0]
        assert "getcasefiles" not in seen[0]

    async def test_tolerates_per_bucket_5xx(self, tmp_path):
        """A 500 on one bucket (e.g. ukpc/tc) must not tank the sweep —
        it's silently skipped and other buckets still evaluated."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # Seed some local rows for hkcfi/en so a divergence would show
        for i in range(3):
            db.upsert_case("hkcfi", 2026, i, f"x{i}", "t", "2026-07-01")
            db.mark_downloaded("hkcfi", 2026, i, ["html"])

        import httpx
        async def _get(url, **kw):
            if "hkfake" in url and "tc" in url:
                # Simulate the persistent ukpc/tc 500
                return httpx.Response(500, text="Server Error")
            if "caseDb=hkcfi" in url and "lang=en" in url:
                return httpx.Response(200, json={
                    "count": 10, "timestamp": "x",
                })
            return httpx.Response(200, json={"count": 0, "timestamp": "x"})

        divergent = await coverage_canary(
            get=_get, checkpoint=db,
            courts=["hkcfi", "hkfake"], langs=["en", "tc"],
            threshold=5,
        )
        # hkcfi/en is +7 (10 live vs 3 local) → included
        # ukpc/tc's 500 is quietly skipped
        # ukpc/en, hkcfi/tc return 0, and local is 0 → no divergence
        assert len(divergent) == 1
        assert divergent[0]["court"] == "hkcfi"

    async def test_returns_only_divergent_buckets(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # Seed local: hkcfi/en has 10, hkca/en has 5
        for i in range(10):
            db.upsert_case("hkcfi", 2026, i, f"[2026] HKCFI {i}", "T", "2026-07-01")
            db.mark_downloaded("hkcfi", 2026, i, ["html"])
        for i in range(5):
            db.upsert_case("hkca", 2026, i, f"[2026] HKCA {i}", "T", "2026-07-01")
            db.mark_downloaded("hkca", 2026, i, ["html"])
        # Live: hkcfi=10 (match), hkca=15 (+10 divergent)
        get = self._make_get({
            "hkcfi": {"en": 10, "tc": 0},
            "hkca": {"en": 15, "tc": 0},
        })
        divergent = await coverage_canary(
            get=get, checkpoint=db,
            courts=["hkcfi", "hkca"], langs=["en"], threshold=5,
        )
        assert len(divergent) == 1
        assert divergent[0]["court"] == "hkca"
        assert divergent[0]["diff"] == 10

    async def test_threshold_excludes_small_divergences(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        for i in range(10):
            db.upsert_case("hkcfi", 2026, i, f"x{i}", "t", "2026-07-01")
            db.mark_downloaded("hkcfi", 2026, i, ["html"])
        get = self._make_get({"hkcfi": {"en": 13, "tc": 0}})  # +3, below 5
        divergent = await coverage_canary(
            get=get, checkpoint=db,
            courts=["hkcfi"], langs=["en"], threshold=5,
        )
        assert divergent == []

    async def test_caps_returned_buckets_at_max_escalations(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        # 5 court/lang buckets all wildly divergent, cap = 3
        get = self._make_get({
            "hkcfi": {"en": 100, "tc": 100},
            "hkca":  {"en": 100, "tc": 100},
            "hkdc":  {"en": 100, "tc": 100},
        })
        divergent = await coverage_canary(
            get=get, checkpoint=db,
            courts=["hkcfi", "hkca", "hkdc"], langs=["en", "tc"],
            threshold=5, max_escalations=3,
        )
        assert len(divergent) == 3


class TestCoverageCanaryHonesty:
    """The canary's whole purpose is to loudly signal drift. Four failure
    modes previously produced silent-green output — this suite pins the
    honest behaviour on each.

    A) Bilingual TC undercount — bilingual cases live in lang='en' per
       the UPSERT rule, so `SELECT COUNT WHERE lang='tc'` reports
       fewer rows than HKLII's per-lang tc count for every court that
       has any bilingual case. The wrapper must canary EN only.
    B) Blind probes — if every getmetacase probe fails (pool exhausted
       or origin storm), the underlying function must raise, not
       silently return [].
    C) Failed escalations — if a divergent bucket's follow-up scrape
       raises, the wrapper must propagate so the dispatcher marks the
       step FAIL. Silent-swallow contradicts the module's own
       'non-zero exit' contract.
    D) Preflight leak — if pool.preflight() raises, pool.close() must
       still fire so the 20 curl_cffi clients don't leak.
    """

    async def test_bilingual_case_does_not_cause_tc_false_positive(self, tmp_path):
        """Repro: seed a bilingual case (upsert en then tc). The DB row
        keeps lang='en' by the UPSERT collapse rule. HKLII's per-lang
        counts (en+bilingual, tc+bilingual) match local IF we count
        only lang='en'. Wrapper must NOT probe lang='tc' or it'll flag
        this bucket as +N_bilingual on every run."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_coverage_canary

        db_path = tmp_path / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        # Bilingual case: enumerated first as en, then as tc. UPSERT keeps
        # lang='en' (see checkpoint.py CASE WHEN cases.lang='en' OR ...).
        db.upsert_case("hkcfi", 2026, 1, "[2026] HKCFI 1", "T", "2026-07-01", lang="en")
        db.upsert_case("hkcfi", 2026, 1, "[2026] HKCFI 1", "T", "2026-07-01", lang="tc")
        db.mark_downloaded("hkcfi", 2026, 1, ["html"])
        assert db._conn.execute(
            "SELECT lang FROM cases WHERE court='hkcfi' AND year=2026 AND number=1"
        ).fetchone()[0] == "en"
        db.close()

        captured_kwargs = {}

        async def _fake_canary(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        runner = MagicMock()
        runner.output = tmp_path
        runner.proxies = ["http://127.0.0.1:8888"]
        runner.direct = True
        step = MagicMock()
        step.kwargs = {"threshold": 5, "max_escalations": 3}

        with patch("hklii_downloader.update.coverage_canary", side_effect=_fake_canary):
            await _run_coverage_canary(runner, step, no_events=True)

        # Wrapper MUST pass langs=['en'] only — TC would false-positive
        # by N_bilingual per court.
        assert captured_kwargs.get("langs") == ["en"], (
            f"expected langs=['en'], got {captured_kwargs.get('langs')!r} — "
            "bilingual UPSERT rule means TC bucket local count undercounts"
        )

    async def test_canary_raises_when_majority_of_probes_fail(self, tmp_path):
        """Meta-review: my Cluster C fix only raised when probes_ok==0.
        If 12 of 13 buckets fail (proxy pool degraded but 1 endpoint
        still works, or 12 courts hit persistent 5xx while 1 doesn't),
        probes_ok=1 → NO raise → wrapper prints 'all N within
        tolerance'. Canary is 92 % blind but reports healthy.

        Guard: raise if probes_ok < probes_total / 2. Preserves the
        single-bucket-5xx tolerance the ukpc/tc case needs while
        catching the majority-blind case."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import (
            coverage_canary, CoverageCanaryBlindError,
        )

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))

        import httpx
        async def _mostly_fail(url, **kw):
            # 12 courts fail; 1 court succeeds with count matching local
            # (so no divergence surfaces even from the one probe that
            # worked).
            if "hkcfa" in url:
                return httpx.Response(200, json={"count": 0, "timestamp": "x"})
            raise RuntimeError("proxy dead")

        courts_13 = [
            "hkcfa", "hkca", "hkcfi", "hkdc", "hkldt", "hkfc",
            "hkmagc", "hkct", "hkcrc", "hklat", "hkoat", "hksct", "hkfake",
        ]
        with pytest.raises(CoverageCanaryBlindError):
            await coverage_canary(
                get=_mostly_fail, checkpoint=db,
                courts=courts_13, langs=["en"], threshold=5,
            )

    async def test_canary_tolerates_single_bucket_failure_still(self, tmp_path):
        """Sibling positive: 12 of 13 buckets return CLEAN, 1 bucket 5xx
        (the ukpc/tc persistent-500 case). Must NOT raise — that would
        turn a known-benign scenario into a step failure every run."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import coverage_canary

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))

        import httpx
        async def _one_fails(url, **kw):
            if "hkfake" in url:
                return httpx.Response(500, text="err")
            return httpx.Response(200, json={"count": 0, "timestamp": "x"})

        courts_13 = [
            "hkcfa", "hkca", "hkcfi", "hkdc", "hkldt", "hkfc",
            "hkmagc", "hkct", "hkcrc", "hklat", "hkoat", "hksct", "hkfake",
        ]
        # Must return normally — 12 successes is well above the majority
        # threshold, single 5xx is a known-benign per-bucket skip.
        result = await coverage_canary(
            get=_one_fails, checkpoint=db,
            courts=courts_13, langs=["en"], threshold=5,
        )
        assert result == []

    async def test_canary_raises_when_every_probe_fails(self, tmp_path):
        """Pool exhausted / origin 500 storm / DNS glitch → every
        `pool.get()` in the canary loop raises. Silent-continue leaves
        `divergent=[]` and reports 'all buckets within tolerance' — the
        tripwire ran blind. Instead, the function must raise a distinct
        error so the dispatch marks the step FAIL."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.update import (
            coverage_canary, CoverageCanaryBlindError,
        )

        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))

        async def _always_fail(url, **kw):
            raise RuntimeError("all proxies dead")

        with pytest.raises(CoverageCanaryBlindError):
            await coverage_canary(
                get=_always_fail, checkpoint=db,
                courts=["hkcfi", "hkca"], langs=["en"],
                threshold=5,
            )

    async def test_run_coverage_canary_fails_when_all_escalations_raise(self, tmp_path):
        """Canary detects 2 divergent buckets, both escalations raise.
        Current code prints red 'escalation failed' lines but returns
        cleanly — the dispatch loop marks the step 'ok'. Contract per
        _dispatch_update_plan docstring: non-zero failures translate to
        non-zero exit. The wrapper must propagate."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_coverage_canary

        db_path = tmp_path / ".checkpoint.db"
        CheckpointDB(str(db_path)).close()  # create empty DB

        async def _fake_canary(**kwargs):
            return [
                {"court": "hkcfi", "lang": "en", "live": 100, "local": 90, "diff": 10},
                {"court": "hkca", "lang": "en", "live": 50, "local": 40, "diff": 10},
            ]

        async def _boom(*a, **kw):
            raise RuntimeError("scrape failed")

        runner = MagicMock()
        runner.output = tmp_path
        runner.proxies = ["http://127.0.0.1:8888"]
        runner.direct = True
        step = MagicMock()
        step.kwargs = {"threshold": 5, "max_escalations": 3}

        with patch("hklii_downloader.update.coverage_canary", side_effect=_fake_canary), \
             patch("hklii_downloader.cli._run_scrape", side_effect=_boom):
            with pytest.raises(Exception) as exc_info:
                await _run_coverage_canary(runner, step, no_events=True)
        # Message must reference the escalation-failure count so operators
        # grepping logs can find it.
        assert "escalat" in str(exc_info.value).lower()

    async def test_db_closed_even_when_pool_close_raises(self, tmp_path):
        """Meta-review: outer finally does `await pool.close()` then
        `db.close()`. If pool.close() itself raises (e.g. curl_cffi
        AsyncSession refuses aclose after an aborted request), the
        db.close() call is skipped — the CheckpointDB advisory lock fd
        leaks, and every subsequent step in the same dispatch loop
        opens a new CheckpointDB that trips CheckpointLockError. One
        transient close error cascades into a run-wide failure."""
        from unittest.mock import MagicMock, patch
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_coverage_canary

        db_path = tmp_path / ".checkpoint.db"
        CheckpointDB(str(db_path)).close()

        db_close_calls = {"n": 0}
        real_close = CheckpointDB.close
        def _wrap_close(self):
            db_close_calls["n"] += 1
            real_close(self)

        class _PoolThatFailsToClose:
            def __init__(self, *a, **kw): pass
            async def preflight(self): pass
            async def close(self):
                raise RuntimeError("curl_cffi refused aclose")
            async def get(self, url, **kw):
                import httpx
                return httpx.Response(200, json={"count": 0, "timestamp": "x"})

        runner = MagicMock()
        runner.output = tmp_path
        runner.proxies = ["http://127.0.0.1:8888"]
        runner.direct = False
        step = MagicMock()
        step.kwargs = {"threshold": 5, "max_escalations": 3}

        with patch.object(CheckpointDB, "close", _wrap_close), \
             patch("hklii_downloader.proxy_pool.ProxyPool", _PoolThatFailsToClose):
            with pytest.raises(RuntimeError):
                await _run_coverage_canary(runner, step, no_events=True)

        assert db_close_calls["n"] >= 1, (
            "db.close() skipped when pool.close() raised — CheckpointDB "
            "lock fd leaked, cascading CheckpointLockError to every "
            "subsequent step."
        )

    async def test_pool_closed_when_preflight_raises(self, tmp_path):
        """If pool.preflight() raises (e.g. all IP echo services
        unreachable), pool.close() must still fire — otherwise every
        curl_cffi client per proxy leaks."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_coverage_canary

        db_path = tmp_path / ".checkpoint.db"
        CheckpointDB(str(db_path)).close()

        close_calls = {"n": 0}

        class _FakePool:
            def __init__(self, *a, **kw): pass
            async def preflight(self):
                raise RuntimeError("all echo services unreachable")
            async def close(self):
                close_calls["n"] += 1
            async def get(self, url, **kw):
                raise RuntimeError("should not be called")

        runner = MagicMock()
        runner.output = tmp_path
        runner.proxies = ["http://127.0.0.1:8888"]
        runner.direct = False
        step = MagicMock()
        step.kwargs = {"threshold": 5, "max_escalations": 3}

        with patch("hklii_downloader.proxy_pool.ProxyPool", _FakePool):
            with pytest.raises(RuntimeError, match="echo services"):
                await _run_coverage_canary(runner, step, no_events=True)

        assert close_calls["n"] == 1, (
            "pool.close() must fire when preflight raises — otherwise "
            "20 curl_cffi clients leak per canary failure"
        )


class TestUpdateCliDryRun:
    def test_dry_run_daily_prints_plan_and_exits_zero(self, tmp_path):
        from hklii_downloader.cli import main
        result = CliRunner().invoke(main, [
            "update", "-o", str(tmp_path),
            "--proxy", "http://127.0.0.1:8888",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "scrape" in result.output.lower()
        assert "canary" in result.output.lower()

    def test_dry_run_holds_lock_only_briefly(self, tmp_path):
        """Dry-run acquires the lock (to prevent a concurrent live run
        sneaking in while the operator inspects a plan) but must release
        it before returning — so a second dry-run right after succeeds
        rather than exit-2-locking."""
        from hklii_downloader.cli import main
        r1 = CliRunner().invoke(main, [
            "update", "-o", str(tmp_path),
            "--proxy", "http://127.0.0.1:8888",
            "--dry-run",
        ])
        r2 = CliRunner().invoke(main, [
            "update", "-o", str(tmp_path),
            "--proxy", "http://127.0.0.1:8888",
            "--dry-run",
        ])
        assert r1.exit_code == 0, r1.output
        assert r2.exit_code == 0, r2.output


class TestUpdateDispatchArgContract:
    """Regression: earlier revs of _dispatch_update_plan called
    _run_enrich(summaries=..., appeal_history=...) and
    _run_scrape_noteup(langs=...) — both are TypeErrors that were
    silently swallowed by the broad `except Exception` in the dispatch
    loop. The prior tests here inspected only the CALLEE's signature —
    a caller-side regression (dispatch flipped back to `summaries=`)
    would leave the callee's signature intact and pass green.

    These tests inspect the DISPATCHER'S source and assert on the
    kwargs it actually names at the call site.
    """

    def test_dispatch_source_uses_do_prefix_for_enrich_kwargs(self):
        """_dispatch_update_plan must name `do_summaries=` /
        `do_appeal_history=` at the _run_enrich call site — the bare
        `summaries=`/`appeal_history=` shape is the exact regression
        we already fixed once."""
        import inspect
        from hklii_downloader.cli import _dispatch_update_plan
        src = inspect.getsource(_dispatch_update_plan)
        assert "do_summaries=" in src
        assert "do_appeal_history=" in src
        # Any bare form is a regression. Match on ' summaries=' /
        # ' appeal_history=' to avoid hitting the do_ prefix versions.
        assert " summaries=" not in src, (
            "_dispatch_update_plan appears to call _run_enrich with "
            "bare `summaries=` — this is the shape that TypeError'd "
            "under the previously-swallowed except-clause."
        )
        assert " appeal_history=" not in src, (
            "_dispatch_update_plan appears to call _run_enrich with "
            "bare `appeal_history=` — same regression shape."
        )

    def test_dispatch_source_does_not_pass_langs_to_run_scrape_noteup(self):
        """_dispatch_update_plan must NOT pass `langs=` to
        _run_scrape_noteup (which lacks that kwarg). We look for the
        specific call site by locating `_run_scrape_noteup(` in the
        dispatcher source and searching only its argument list."""
        import inspect
        from hklii_downloader.cli import _dispatch_update_plan
        src = inspect.getsource(_dispatch_update_plan)
        idx = src.find("_run_scrape_noteup(")
        assert idx != -1, "_run_scrape_noteup not called by dispatcher"
        # Consume until the matching `)`. Naive but sufficient — no
        # nested parens are expected inside the kwarg list here.
        depth = 0
        end = idx
        for i, ch in enumerate(src[idx:], start=idx):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        call_args = src[idx:end]
        assert "langs=" not in call_args, (
            f"_run_scrape_noteup call site contains langs= — regression "
            f"of the previously-fixed TypeError. Call fragment:\n{call_args}"
        )


class TestOrphanMarkHandler:
    """`_run_update_orphan_mark` guards against silent corpus damage.
    Prior tests exercised only the UpdateRunner __init__ guard (a
    plan-composition check); the actual handler that reads enum_runs,
    validates coverage, and calls mark_orphaned_below_ts had no
    behavioural coverage."""

    def _capture_orphan_flips(self, monkeypatch):
        """Wrap CheckpointDB.mark_orphaned_below_ts so we can assert on
        whether the guard let the flip happen."""
        from hklii_downloader.checkpoint import CheckpointDB
        real = CheckpointDB.mark_orphaned_below_ts
        calls = {"n": 0, "cutoffs": []}
        def _wrap(self, cutoff_ts):
            calls["n"] += 1
            calls["cutoffs"].append(cutoff_ts)
            return real(self, cutoff_ts)
        monkeypatch.setattr(CheckpointDB, "mark_orphaned_below_ts", _wrap)
        return calls

    def test_partial_coverage_enum_aborts_without_orphaning(
        self, tmp_path, monkeypatch,
    ):
        """A completed enum_run covering only ['hkcfi']×['en'] is not
        authoritative for orphan_mark; the guard must abort and
        mark_orphaned_below_ts must NOT run."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_update_orphan_mark
        from unittest.mock import MagicMock
        calls = self._capture_orphan_flips(monkeypatch)

        db_path = tmp_path / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        # Seed a stale downloaded row in a court the enum didn't cover.
        db.upsert_case("hkca", 2020, 1, "[2020] HKCA 1", "T", "2020-01-01", lang="en", last_seen_at=100)
        db.mark_downloaded("hkca", 2020, 1, ["html"])
        # Narrow enum covering only hkcfi/en → completes cleanly.
        g = db.start_enum_run(["hkcfi"], ["en"])
        db.complete_enum_run(g)
        db.close()

        runner = MagicMock()
        runner.output = tmp_path
        _run_update_orphan_mark(runner)

        assert calls["n"] == 0, (
            "mark_orphaned_below_ts was called despite partial coverage — "
            "hkca/en (not in the completed enum) would have been mass-"
            "orphaned."
        )

    def test_no_completed_enum_aborts_without_orphaning(
        self, tmp_path, monkeypatch,
    ):
        """A fresh DB with no enum_runs row must not orphan anything."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_update_orphan_mark
        from unittest.mock import MagicMock
        calls = self._capture_orphan_flips(monkeypatch)

        db_path = tmp_path / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        db.upsert_case("hkcfi", 2020, 1, "[2020] HKCFI 1", "T", "2020-01-01", lang="en", last_seen_at=100)
        db.mark_downloaded("hkcfi", 2020, 1, ["html"])
        db.close()

        runner = MagicMock()
        runner.output = tmp_path
        _run_update_orphan_mark(runner)

        assert calls["n"] == 0

    def test_narrow_window_enum_aborts_without_orphaning(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end sibling of the checkpoint-level test: even if a
        daily-narrow enum's completed row covers ALL_COURTS × ALL_LANGS,
        `latest_completed_enum_run` filters it out and the handler
        aborts. This is the mass-orphan scenario Cluster B addresses."""
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_update_orphan_mark, ALL_COURTS, ALL_LANGS
        from unittest.mock import MagicMock
        calls = self._capture_orphan_flips(monkeypatch)

        db_path = tmp_path / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        # Narrow-window sweep of every court/lang → completes cleanly
        # but its cutoff would mass-orphan out-of-window rows.
        g = db.start_enum_run(
            list(ALL_COURTS), list(ALL_LANGS),
            min_date_text="06/06/2026",
            max_date_text="06/07/2026",
        )
        db.complete_enum_run(g)
        db.close()

        runner = MagicMock()
        runner.output = tmp_path
        _run_update_orphan_mark(runner)

        assert calls["n"] == 0, (
            "orphan_mark used a narrow-window enum_run as its reference — "
            "the Cluster-B mass-orphan bug regressed."
        )

    def test_full_coverage_enum_orphans_stale_rows(
        self, tmp_path, monkeypatch,
    ):
        """Positive path: a full-corpus completed enum_run covering
        ALL_COURTS × ALL_LANGS DOES allow the orphan flip on rows
        whose last_seen_at is older than the sweep's started_at."""
        import time
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import _run_update_orphan_mark, ALL_COURTS, ALL_LANGS
        from unittest.mock import MagicMock
        calls = self._capture_orphan_flips(monkeypatch)

        db_path = tmp_path / ".checkpoint.db"
        db = CheckpointDB(str(db_path))
        # Row with stale last_seen_at way before now — must flip.
        stale_ts = int(time.time()) - 100_000
        db.upsert_case("hkcfi", 2020, 1, "[2020] HKCFI 1", "T", "2020-01-01", lang="en", last_seen_at=stale_ts)
        db.mark_downloaded("hkcfi", 2020, 1, ["html"])
        # Fresh full-corpus completed enum.
        g = db.start_enum_run(list(ALL_COURTS), list(ALL_LANGS))
        db.complete_enum_run(g)
        db.close()

        runner = MagicMock()
        runner.output = tmp_path
        _run_update_orphan_mark(runner)

        assert calls["n"] == 1, "mark_orphaned_below_ts should have fired"

        # Verify the row actually flipped.
        db2 = CheckpointDB(str(db_path))
        try:
            row = db2._conn.execute(
                "SELECT status FROM cases WHERE court='hkcfi' AND year=2020 AND number=1"
            ).fetchone()
            assert row[0] == "orphaned", f"expected 'orphaned', got {row[0]!r}"
        finally:
            db2.close()


class TestUpdatePlanDoesNotAdvertiseDeadKwargs:
    """Regression pins for adversarial-review dead-kwarg findings —
    plan-level kwargs must not include knobs the dispatcher never
    reads (or the dry-run output misleads operators about switches
    that have no effect)."""

    def test_validate_step_kwargs_omit_include_graph(self, tmp_path):
        """`_run_update_validate` reads only 'sample' from step.kwargs;
        Validator has no include_graph parameter and CHECK_KEYS has no
        graph check. Advertising 'include_graph=True' in --dry-run is
        misleading. (Finding: update.py:413)"""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="monthly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        validate = next(s for s in runner.plan() if s.name == "validate")
        assert "include_graph" not in validate.kwargs, (
            "validate Step advertises include_graph but the dispatcher "
            "never reads it — dead kwarg."
        )

    def test_orphan_mark_step_kwargs_omit_dry_run(self, tmp_path):
        """`_run_update_orphan_mark(runner)` takes no step arg; the
        'dry_run' kwarg on the orphan_mark Step is unread and would
        mislead any operator adding a future --dry-run flag.
        (Finding: update.py:430)"""
        from hklii_downloader.update import UpdateRunner
        runner = UpdateRunner(
            profile="quarterly", output=tmp_path, proxies=["p"],
            now=_fake_now("2026-07-06T02:00:00"),
        )
        orphan = next(s for s in runner.plan() if s.name == "orphan_mark")
        assert "dry_run" not in orphan.kwargs, (
            "orphan_mark Step advertises dry_run but the dispatcher "
            "never forwards step to _run_update_orphan_mark — dead kwarg."
        )


class TestUpdateAdvisoryLock:
    def test_second_invocation_exits_nonzero_when_lock_held(self, tmp_path):
        """Existing process holding OUTPUT/.hklii.lock → new invocation aborts."""
        from hklii_downloader.cli import main
        lock_path = tmp_path / ".hklii.lock"
        # Open + lock in this process
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = CliRunner().invoke(main, [
                "update", "-o", str(tmp_path),
                "--direct", "--yes",
                "--dry-run",  # dry-run still checks the lock
            ], catch_exceptions=False)
            # exit_code 2 per design; anything nonzero is acceptable as long as
            # the output mentions the lock
            assert result.exit_code != 0, result.output
            assert "lock" in result.output.lower(), result.output
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestUpdateStandaloneWriterConflict:
    """`hklii update` should refuse to start if a standalone writer (scrape,
    scrape-noteup, enrich, …) currently holds the CheckpointDB lock —
    they'd race per-step otherwise, which is confusing to diagnose."""

    def test_update_aborts_when_checkpoint_lock_held(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        from hklii_downloader.cli import main
        # Open CheckpointDB → grabs the .checkpoint.db.lock EX lock
        db = CheckpointDB(str(tmp_path / ".checkpoint.db"))
        try:
            result = CliRunner().invoke(main, [
                "update", "-o", str(tmp_path),
                "--direct", "--yes",
                "--dry-run",
            ], catch_exceptions=False)
            assert result.exit_code != 0, result.output
            assert "lock" in result.output.lower(), result.output
        finally:
            db.close()

    def test_is_locked_by_peer_reports_false_when_free(self, tmp_path):
        from hklii_downloader.checkpoint import CheckpointDB
        db_path = tmp_path / ".checkpoint.db"
        # Never opened → lock file may or may not exist; is_locked_by_peer
        # must not lie either way.
        assert CheckpointDB.is_locked_by_peer(str(db_path)) is False
        # Open and close → lock released → still False.
        db = CheckpointDB(str(db_path))
        db.close()
        assert CheckpointDB.is_locked_by_peer(str(db_path)) is False
