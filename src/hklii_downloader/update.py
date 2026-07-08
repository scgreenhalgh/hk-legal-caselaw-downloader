"""Incremental refresh orchestrator for `hklii update`.

Profile-driven meta-runner that composes existing idempotent subcommands
into daily/weekly/monthly/quarterly cadences with lean date-window
enumeration.

Design rationale lives in:
- research/12-update-command.md (implementation-decision log)
- memory: 'hklii update shipped'

Every profile's plan is a list of `Step(name, kwargs)`.
The runner:
  - builds the plan from profile + explicit overrides,
  - holds a per-output advisory lock while executing (`OUTPUT/.hklii.lock`,
    `fcntl.LOCK_EX | LOCK_NB`),
  - dispatches each step to its underlying async runner (imported from
    cli._run_* helpers).

Date-window boundaries are computed in Asia/Hong_Kong so process TZ
never leaks into wire params.
"""
from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


HKT = ZoneInfo("Asia/Hong_Kong")


@dataclass
class Step:
    """A single planned unit of work.

    Callers iterate `UpdateRunner.plan()` and dispatch by `name`; `kwargs`
    are forwarded to the underlying runner. Wire-cost estimates live
    beside the class in `_STEP_EST` so plan() call sites stay terse.
    """
    name: str
    kwargs: dict[str, Any] = field(default_factory=dict)


# Human-readable estimate of wire cost per step, keyed by step name.
# Used by `format_plan()` for dry-run output; never read for logic.
_STEP_EST: dict[str, str] = {
    "check_freshness": "~28 (metadata-only probes; every mapped triple)",
    "scrape": "~26 enum + N new-case fetches",
    "recheck_html": "~5-20 per queue depth",
    "generate_html": "0 (local LibreOffice/pandoc)",
    "scrape_noteup": "~10-30 for new cases (idempotent whitelist)",
    "enrich": "~10-50 (capped at retry_limit)",
    "coverage_canary": "~13 (13 dbs × EN only, getmetacase)",
    "scrape_hopt": "~10 enum + new-row fetches",
    "scrape_ukpc": "~2 enum + N new-row fetches (idempotent skip)",
    "scrape_legis": "~6 enum + new-row fetches",
    "backfill_legis_history": "~500 (missing capversions)",
    "backfill_case_translations": "~50 for newly bilingual cases",
    "scrape_relatedcaps": "~4800 (full fresh diff)",
    "validate": "0 (local)",
    "full_reconcile": "~500-1500 enum (full corpus)",
    "orphan_mark": "0 (local diff)",
}


# Per-profile boolean defaults. Explicit --include-* / --no-* overrides
# always win over these. Non-boolean defaults (recent_days, items_per_page,
# recheck_max_age_days, generate_html_limit) also live here so tests can
# assert on them.
PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "daily": {
        "recent_days": 30,
        "items_per_page": 500,
        "recheck_max_age_days": 30,
        "generate_html_limit": None,  # None → runner auto-sizes
        "enrich_retry_limit": 100,
        "validate_sample": 2000,
        "canary_divergence_threshold": 5,
        # D2 freshness gate (~28 metadata-only probes) — replaces the
        # counts-only canary as the drift-detection signal. Kept ON for
        # every cadence including daily because it's cheap and its
        # WHOLE POINT is to run on the schedule the canary used to.
        "include_freshness_check": True,
        "include_scrape": True,
        "include_recheck_html": True,
        "include_generate_html": True,
        "include_noteup": True,
        "include_enrich": True,
        "include_canary": True,
        # `--lang both` picks EN-when-both-exist during scrape, so
        # bilingual TC sidecars lag by a day until this runs. Cheap
        # (~5 calls/day for new bilingual cases) → keep it daily.
        "include_backfill_translations": True,
        "include_hopt": False,
        "include_ukpc": False,
        "include_legis": False,
        "include_legis_history": False,
        "include_relatedcaps": False,
        "include_validate": False,
        "include_full_reconcile": False,
        "include_orphan_mark": False,
    },
    "weekly": {
        "recent_days": 30,
        "items_per_page": 500,
        "recheck_max_age_days": 30,
        "generate_html_limit": None,
        "enrich_retry_limit": 100,
        "validate_sample": 2000,
        "canary_divergence_threshold": 5,
        "include_freshness_check": True,
        "include_scrape": True,
        "include_recheck_html": True,
        "include_generate_html": True,
        "include_noteup": True,
        "include_enrich": True,
        "include_canary": True,
        "include_backfill_translations": True,
        "include_hopt": True,
        "include_ukpc": True,
        "include_legis": True,
        "include_legis_history": False,
        "include_relatedcaps": False,
        "include_validate": False,
        "include_full_reconcile": False,
        "include_orphan_mark": False,
    },
    "monthly": {
        "recent_days": 90,
        "items_per_page": 500,
        "recheck_max_age_days": 365,
        "generate_html_limit": 0,  # 0 = unlimited
        "enrich_retry_limit": 100,
        "validate_sample": 2000,
        "canary_divergence_threshold": 5,
        "include_freshness_check": True,
        "include_scrape": True,
        "include_recheck_html": True,
        "include_generate_html": True,
        "include_noteup": True,
        "include_enrich": True,
        "include_canary": True,
        "include_hopt": True,
        "include_ukpc": True,
        "include_legis": True,
        "include_legis_history": True,
        # `scrape_relatedcaps` deliberately excluded from monthly — the
        # ord/reg graph is 100% derivable from the numeric-suffix pattern
        # and hasn't drifted across the last audit. Quarterly still runs
        # a fresh full sweep as belt-and-suspenders.
        "include_relatedcaps": False,
        "include_backfill_translations": True,
        "include_validate": True,
        "include_full_reconcile": False,
        "include_orphan_mark": False,
    },
    "quarterly": {
        "recent_days": None,  # no window → full corpus
        "items_per_page": 500,
        "recheck_max_age_days": 0,  # unlimited
        "generate_html_limit": 0,
        "enrich_retry_limit": 100,
        "validate_sample": 2000,
        "canary_divergence_threshold": 5,
        "include_freshness_check": True,
        "include_scrape": True,
        "include_recheck_html": True,
        "include_generate_html": True,
        "include_noteup": True,
        "include_enrich": True,
        "include_canary": True,
        "include_hopt": True,
        "include_ukpc": True,
        "include_legis": True,
        "include_legis_history": True,
        "include_relatedcaps": True,
        "include_backfill_translations": True,
        "include_validate": True,
        "include_full_reconcile": True,
        "include_orphan_mark": True,
    },
    "custom": {
        # Custom profile starts with EVERYTHING OFF; caller must explicitly
        # opt in via --include-*.
        "recent_days": None,
        "items_per_page": 500,
        "recheck_max_age_days": None,
        "generate_html_limit": None,
        "enrich_retry_limit": 100,
        "validate_sample": 2000,
        "canary_divergence_threshold": 5,
        "include_freshness_check": False,
        "include_scrape": False,
        "include_recheck_html": False,
        "include_generate_html": False,
        "include_noteup": False,
        "include_enrich": False,
        "include_canary": False,
        "include_hopt": False,
        "include_ukpc": False,
        "include_legis": False,
        "include_legis_history": False,
        "include_relatedcaps": False,
        "include_backfill_translations": False,
        "include_validate": False,
        "include_full_reconcile": False,
        "include_orphan_mark": False,
    },
}


class UpdateRunnerError(Exception):
    """Config/guard-error raised at __init__."""


class UpdateLockHeldError(Exception):
    """Advisory lock already held by another writer."""


class UpdateRunner:
    """Compose + execute a profile-driven incremental refresh."""

    def __init__(
        self,
        profile: str = "daily",
        output: Path = Path("./output"),
        proxies: list[str] | None = None,
        direct: bool = False,
        # Non-boolean overrides — None keeps profile default.
        recent_days: int | None = None,
        items_per_page: int | None = None,
        recheck_max_age_days: int | None = None,
        generate_html_limit: int | None = None,
        enrich_retry_limit: int | None = None,
        canary_divergence_threshold: int | None = None,
        validate_sample: int | None = None,
        # Boolean --include-*/--no-* overrides — None keeps profile default.
        include_freshness_check: bool | None = None,
        include_scrape: bool | None = None,
        include_recheck_html: bool | None = None,
        include_generate_html: bool | None = None,
        include_noteup: bool | None = None,
        include_enrich: bool | None = None,
        include_canary: bool | None = None,
        include_hopt: bool | None = None,
        include_ukpc: bool | None = None,
        include_legis: bool | None = None,
        include_legis_history: bool | None = None,
        include_relatedcaps: bool | None = None,
        include_backfill_translations: bool | None = None,
        include_validate: bool | None = None,
        include_full_reconcile: bool | None = None,
        include_orphan_mark: bool | None = None,
        # Misc
        yes_narrow: bool = False,
        now: Callable[[], datetime] | None = None,
    ):
        if profile not in PROFILE_DEFAULTS:
            raise UpdateRunnerError(
                f"unknown profile {profile!r}; "
                f"choose from {sorted(PROFILE_DEFAULTS)}"
            )

        self.profile = profile
        self.output = Path(output)
        self.proxies = list(proxies) if proxies else []
        self.direct = direct
        self.yes_narrow = yes_narrow
        self._now = now or (lambda: datetime.now(HKT))

        # Merge: profile defaults ← explicit overrides (if not None).
        base = dict(PROFILE_DEFAULTS[profile])
        overrides = {
            "recent_days": recent_days,
            "items_per_page": items_per_page,
            "recheck_max_age_days": recheck_max_age_days,
            "generate_html_limit": generate_html_limit,
            "enrich_retry_limit": enrich_retry_limit,
            "canary_divergence_threshold": canary_divergence_threshold,
            "validate_sample": validate_sample,
            "include_freshness_check": include_freshness_check,
            "include_scrape": include_scrape,
            "include_recheck_html": include_recheck_html,
            "include_generate_html": include_generate_html,
            "include_noteup": include_noteup,
            "include_enrich": include_enrich,
            "include_canary": include_canary,
            "include_hopt": include_hopt,
            "include_ukpc": include_ukpc,
            "include_legis": include_legis,
            "include_legis_history": include_legis_history,
            "include_relatedcaps": include_relatedcaps,
            "include_backfill_translations": include_backfill_translations,
            "include_validate": include_validate,
            "include_full_reconcile": include_full_reconcile,
            "include_orphan_mark": include_orphan_mark,
        }
        for k, v in overrides.items():
            if v is not None:
                base[k] = v
        self.settings = base

        # Guard: --recent-days=1 requires --yes-narrow. Zero is the
        # widest window (no filter, same as None) so it is intentionally
        # NOT covered by the narrow-day guard.
        rd = self.settings.get("recent_days")
        if rd is not None and 0 < rd < 2 and not yes_narrow:
            raise UpdateRunnerError(
                f"--recent-days={rd} needs --yes-narrow to protect against "
                "accidental same-day windows from foreign TZ. Pass "
                "--yes-narrow if you intend to run a sub-2-day window."
            )

        # Guard: orphan_mark only permitted alongside full_reconcile.
        if (
            self.settings.get("include_orphan_mark")
            and not self.settings.get("include_full_reconcile")
        ):
            raise UpdateRunnerError(
                "--include-orphan-mark requires --include-full-reconcile in "
                "the same run — orphan detection is only valid after a "
                "full-corpus enum."
            )

    # ------- planning -------------------------------------------------

    def _hkt_today(self):
        return self._now().astimezone(HKT).date()

    def _date_window(self, today=None) -> tuple[str | None, str | None]:
        """Derive HKLII dd/mm/yyyy window from recent_days.

        `today` is threadable so callers (format_plan) can snapshot the
        HKT date once and reuse it for both the plan and the header —
        preventing an internal drift across HKT midnight.
        """
        rd = self.settings.get("recent_days")
        if rd is None or rd <= 0:
            return None, None
        if today is None:
            today = self._hkt_today()
        min_d = (today - timedelta(days=rd)).strftime("%d/%m/%Y")
        max_d = today.strftime("%d/%m/%Y")
        return min_d, max_d

    def plan(self, today=None) -> list[Step]:
        s = self.settings
        min_date, max_date = self._date_window(today=today)
        steps: list[Step] = []

        # Freshness gate runs FIRST — the dispatcher consults
        # db_freshness after this step to scope downstream scrape
        # buckets. Ordering matters: a post-hoc probe would tell us
        # what we DID scrape, not what we SHOULD scrape.
        if s.get("include_freshness_check"):
            steps.append(Step(
                name="check_freshness",
                kwargs={},
            ))

        if s.get("include_scrape"):
            steps.append(Step(
                name="scrape",
                kwargs={
                    "recent_days": s.get("recent_days"),
                    "items_per_page": s.get("items_per_page"),
                    "min_date": min_date,
                    "max_date": max_date,
                    "sort": "-date" if min_date else None,
                    "allow_doc": True,
                    "with_summaries": True,
                    "with_appeal_history": True,
                },
            ))

        if s.get("include_recheck_html"):
            steps.append(Step(
                name="recheck_html",
                kwargs={
                    "max_age_days": s.get("recheck_max_age_days"),
                    "limit": None,  # queue-bounded by max_age_days
                },
            ))

        if s.get("include_generate_html"):
            steps.append(Step(
                name="generate_html",
                kwargs={"limit": s.get("generate_html_limit")},
            ))

        if s.get("include_noteup"):
            steps.append(Step(
                name="scrape_noteup",
                kwargs={},
            ))

        if s.get("include_enrich"):
            steps.append(Step(
                name="enrich",
                kwargs={"retry_limit": s.get("enrich_retry_limit")},
            ))

        if s.get("include_canary"):
            steps.append(Step(
                name="coverage_canary",
                kwargs={
                    "threshold": s.get("canary_divergence_threshold"),
                    "max_escalations": 3,
                },
            ))

        if s.get("include_hopt"):
            steps.append(Step(
                name="scrape_hopt",
                kwargs={},
            ))

        if s.get("include_ukpc"):
            steps.append(Step(
                name="scrape_ukpc",
                kwargs={},
            ))

        if s.get("include_legis"):
            steps.append(Step(
                name="scrape_legis",
                kwargs={},
            ))

        if s.get("include_legis_history"):
            steps.append(Step(
                name="backfill_legis_history",
                kwargs={},
            ))

        if s.get("include_backfill_translations"):
            steps.append(Step(
                name="backfill_case_translations",
                kwargs={},
            ))

        if s.get("include_relatedcaps"):
            steps.append(Step(
                name="scrape_relatedcaps",
                kwargs={},
            ))

        if s.get("include_validate"):
            steps.append(Step(
                name="validate",
                kwargs={
                    "sample": s.get("validate_sample"),
                },
            ))

        if s.get("include_full_reconcile"):
            steps.append(Step(
                name="full_reconcile",
                kwargs={
                    "items_per_page": s.get("items_per_page"),
                    "allow_doc": True,
                },
            ))

        if s.get("include_orphan_mark"):
            steps.append(Step(
                name="orphan_mark",
                kwargs={},
            ))

        return steps

    # ------- advisory lock -------------------------------------------

    def lock_path(self) -> Path:
        return self.output / ".hklii.lock"

    def acquire_lock(self):
        """Acquire an advisory LOCK_EX|LOCK_NB on `.hklii.lock`.

        Returns the file descriptor for later release. Raises
        UpdateLockHeldError if another writer already holds it.
        """
        self.output.mkdir(parents=True, exist_ok=True)
        path = self.lock_path()
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise UpdateLockHeldError(
                f"another writer holds {path}"
            ) from exc
        return fd

    @staticmethod
    def release_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------- summary print for dry-run -------------------------------

    def format_plan(self) -> str:
        # Snapshot the plan + HKT date ONCE — plan() reads self._now()
        # so two calls straddling HKT midnight would compute different
        # date windows within one dry-run output. Thread `today` into
        # plan() so both use the same read.
        today = self._hkt_today()
        steps = self.plan(today=today)
        lines = [
            f"Profile: {self.profile}",
            f"Output:  {self.output}",
            f"HKT today: {today.isoformat()}",
            "",
            "Planned steps:",
        ]
        for i, step in enumerate(steps, start=1):
            kw = ", ".join(
                f"{k}={v!r}" for k, v in step.kwargs.items()
                if v is not None
            )
            lines.append(f"  {i:>2}. {step.name}({kw})")
            lines.append(f"      est: {_STEP_EST.get(step.name, '?')}")
        if not steps:
            lines.append("  (empty)")
        return "\n".join(lines)


# ---------- coverage canary ---------------------------------------------


class CoverageCanaryBlindError(Exception):
    """coverage_canary probed at least one bucket but zero returned a
    usable live count. Distinct from 'no divergence found' — the
    tripwire ran blind (proxy pool exhausted, origin 5xx storm, DNS
    glitch, or every bucket happened to fail simultaneously) and its
    empty return would otherwise be indistinguishable from a healthy
    green run. Callers must escalate to step-failure so operators grepping
    for FAIL see the true state."""


async def coverage_canary(
    get: Callable,
    checkpoint,
    courts: list[str],
    langs: list[str],
    threshold: int,
    max_escalations: int = 3,
) -> list[dict]:
    """Cheap tripwire: hit `getmetacase` for every (court, lang) bucket
    and diff the returned `count` against our local downloaded row count.
    Return buckets whose |live - local| >= threshold, sorted by absolute
    divergence (largest first), capped at max_escalations.

    `getmetacase` returns just {count, timestamp} — leaner than
    `getcasefiles` (no judgments array), same info for canary purposes.

    Error-tolerant PER BUCKET: a 5xx / non-JSON response for a single
    bucket (e.g. ukpc/tc's persistent 500) is treated as "unknown live"
    and skipped rather than aborting the whole sweep.

    Error-INTOLERANT when EVERY bucket fails: if zero buckets returned
    a usable live count (and we did attempt to probe at least one),
    raise CoverageCanaryBlindError. Silent-continue on total failure
    was the pre-fix silent-green bug — the wrapper would return [] and
    print 'all N buckets within tolerance' even though nothing was
    actually observed.
    """
    from urllib.parse import urlencode

    _BASE = "https://www.hklii.hk"
    divergent: list[dict] = []
    probes_ok = 0
    probes_total = 0

    for court in courts:
        for lang in langs:
            probes_total += 1
            params = urlencode({"caseDb": court, "lang": lang})
            url = f"{_BASE}/api/getmetacase?{params}"
            try:
                resp = await get(url)
                data = resp.json()
                live = int(data.get("count", 0))
            except Exception:
                # Per-bucket probe failure — silently skip. A single 500
                # (or JSON decode error, or hang) mustn't tank the canary.
                continue

            probes_ok += 1
            local = checkpoint._conn.execute(
                "SELECT COUNT(*) FROM cases "
                "WHERE court=? AND lang=? AND status='downloaded'",
                (court, lang),
            ).fetchone()[0]

            diff = live - local
            if abs(diff) >= threshold:
                divergent.append({
                    "court": court, "lang": lang,
                    "live": live, "local": local, "diff": diff,
                })

    # Majority-blind check: raise if fewer than half the probes returned
    # a usable live count. 0-of-N is the obvious "pool exhausted / origin
    # unreachable" case, but 1-of-13 (or 4-of-13) is just as blind — the
    # canary can't confidently report tolerance on the buckets it never
    # observed. Keeps single-bucket per-probe failures (ukpc/tc's
    # persistent 500) below the raise threshold.
    if probes_total > 0 and probes_ok < probes_total // 2:
        raise CoverageCanaryBlindError(
            f"coverage_canary probed {probes_ok}/{probes_total} buckets "
            "successfully — majority blind, pool exhausted or origin "
            "unreachable; treat as step failure"
        )

    # Rank by absolute divergence, cap for escalation.
    divergent.sort(key=lambda b: abs(b["diff"]), reverse=True)
    return divergent[:max_escalations]
