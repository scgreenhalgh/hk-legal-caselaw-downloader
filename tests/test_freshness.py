"""Tests for :mod:`hklii_downloader.freshness` (Phase D2 runner).

This file covers the runner-level surface — ``dispatch_url``, the
``_fresh`` predicate, ``FreshnessRunner.probe_all`` /
``stale_buckets`` / ``first_run_missing``. The checkpoint-layer tests
for ``db_freshness`` (schema retrofit, COALESCE-preserving upsert
discipline, ownership boundaries) live in
:mod:`tests.test_freshness_checkpoint` — they're the storage contract
this runner rides on and would drown out the runner behaviour here.

Design contract:

* Every mapped ``(kind, scope, lang)`` triple gets a ``getmeta*`` URL
  via :func:`dispatch_url`. Unmapped slugs — historical HKLII bits like
  ``histlaw`` or the ``other`` bucket (``hkiac``, ``pd``, …) — return
  None and MUST NOT create a ``db_freshness`` row. See
  :attr:`freshness_module_outline.first_run_semantics` rule (5).
* ``_fresh`` fails safe. Any missing signal → STALE; the caller can
  distinguish first-run (no row) from probe-error/mismatch/upstream-newer
  via the row it fetches. A wrong assumption about the ``live_updated_at``
  semantics produces an over-scrape, never a false-FRESH.
* ``probe_all`` is error-tolerant per bucket: one 5xx bucket must not
  tank the whole sweep — the wire-side ``probe_error`` column records
  the failure and the loop continues.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from hklii_downloader.checkpoint import CheckpointDB, DbFreshnessRecord
from hklii_downloader.discovery import DatabaseMatrix
from hklii_downloader.freshness import (
    FreshnessRow,
    FreshnessRunner,
    ProbeOutcome,
    _fresh,
    dispatch_url,
)

HKT = timezone(timedelta(hours=8))

# 12-slug case-family list mirrors cli.ALL_COURTS (see cli.py:102). Kept
# local rather than imported so this test file doesn't chain on cli.py
# — the runner is meant to be usable standalone.
_ALL_COURTS = (
    "hkcfa", "hkca", "hkcfi", "hkdc", "hkldt", "hkfc",
    "hkmagc", "hkct", "hkcrc", "hklat", "hkoat", "hksct",
)

_LEGIS_CAP_TYPES = ("ord", "reg", "instrument")
_HOPT_ABBRS = ("bacpg", "bahkg", "hktmc", "hktml", "hkts")
_LANGS = ("en", "tc")


def _make_matrix(
    *,
    cases: dict[str, tuple[str, ...]] | None = None,
    legis: dict[str, tuple[str, ...]] | None = None,
    other: dict[str, tuple[str, ...]] | None = None,
) -> DatabaseMatrix:
    return DatabaseMatrix(
        cases=cases or {},
        legis=legis or {},
        other=other or {},
    )


def _hkt_ts_at(iso_date: str, hour: int = 12) -> int:
    """Convert a YYYY-MM-DD + hour to a HKT-midday unix timestamp.

    Anchors the test's expectation about the freshness date-boundary
    rule: `_fresh` converts `last_scrape_completed_at` from unix to a
    Hong Kong civil date before comparing to `live_updated_at`. A midday
    timestamp keeps the tests unambiguous across DST (HK has no DST but
    other locales might if a future contributor runs pytest under a
    different TZ).
    """
    d = date.fromisoformat(iso_date)
    return int(
        datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=HKT).timestamp()
    )


# ---------- dispatch_url --------------------------------------------------

class TestDispatchUrl:
    """The 6-row endpoint dispatch table is the design contract with
    HKLII. Getting a URL wrong here silently probes the wrong resource
    (or a 404) and every bucket flips STALE on the next update run."""

    @pytest.mark.parametrize("slug", _ALL_COURTS)
    @pytest.mark.parametrize("lang", _LANGS)
    def test_case_family_slugs_return_getmetacase(self, slug, lang):
        """12 case-family slugs × en/tc → getmetacase?caseDb=..."""
        url = dispatch_url("cases", slug, lang)
        assert url == (
            f"https://www.hklii.hk/api/getmetacase"
            f"?caseDb={slug}&lang={lang}"
        )

    @pytest.mark.parametrize("lang", _LANGS)
    def test_ukpc_uses_getmetahopt_dbcat_c(self, lang):
        """UKPC is a hopt-C slug; its metadata endpoint is getmetahopt
        with dbcat=C, NOT getmetacase. Routing it to getmetacase would
        hit an empty bucket for ever (see cli.py:107 note about the
        empty ukpc slug on getmetacase)."""
        url = dispatch_url("cases-ukpc", "ukpc", lang)
        assert url == (
            f"https://www.hklii.hk/api/getmetahopt"
            f"?dbcat=C&abbr=ukpc&lang={lang}"
        )

    @pytest.mark.parametrize("slug", _LEGIS_CAP_TYPES)
    @pytest.mark.parametrize("lang", _LANGS)
    def test_legis_cap_types_return_getmetalegis(self, slug, lang):
        """ord/reg/instrument use ``getmetalegis?cap_type=...`` (underscore).

        Prior expectation pinned the camelCase ``capType`` shape used by
        every OTHER HKLII query endpoint. Live probe on 2026-07-08 via
        the 20-proxy pool showed that ``getmetalegis`` is the outlier —
        it silently returns ``{"count":0,"timestamp":"…"}`` for any
        camelCase param, only responding with real per-DB totals when
        given the underscore form. The SPA at
        ``https://www.hklii.hk/en/legis/ord/`` uses precisely
        ``?cap_type=ord&lang=EN`` (confirmed via Playwright network
        capture), which is our source of truth for this fix.
        """
        url = dispatch_url("legis", slug, lang)
        assert url == (
            f"https://www.hklii.hk/api/getmetalegis"
            f"?cap_type={slug}&lang={lang}"
        )

    @pytest.mark.parametrize("slug", _HOPT_ABBRS)
    @pytest.mark.parametrize("lang", _LANGS)
    def test_hopt_abbrs_return_getmetahopt_dbcat_other(self, slug, lang):
        """bacpg/bahkg/hktmc/hktml/hkts use getmetahopt with dbcat=other.

        The `wire_abbr` rewrite (bacpg/bahkg → hktba) that gettreaty
        needs at the fetch step is NOT applied here — the design contract
        keeps raw abbrs until D2.5 calibration confirms whether
        getmetahopt honours the rewrite. Encoded as an open question in
        the design; probes will return count=0 if the rewrite IS needed
        and the caller will treat that as a genuine empty bucket signal.
        """
        url = dispatch_url("legis-hopt", slug, lang)
        assert url == (
            f"https://www.hklii.hk/api/getmetahopt"
            f"?dbcat=other&abbr={slug}&lang={lang}"
        )

    @pytest.mark.parametrize("category,slug", [
        ("other-unknown", "hkiac"),
        ("other-unknown", "pd"),
        ("other-unknown", "pcpdaab"),
        ("legis-histlaw", "histlaw"),
    ])
    def test_returns_none_for_unmapped_categories(self, category, slug):
        """Unmapped slugs are D3 backlog. dispatch_url returns None so
        the runner can distinguish "not yet in scope" from "probe failed"
        — the former MUST NOT create a db_freshness row."""
        assert dispatch_url(category, slug, "en") is None


# ---------- _fresh --------------------------------------------------------

# Sentinel for _fresh_row's `last_scrape_completed_at` kwarg. Using
# `None` as the "not passed → use default" signal collided with the
# test that WANTS to check the `last_scrape_completed_at IS NULL`
# STALE branch — the sentinel lets callers distinguish the two.
_UNSET = object()


def _fresh_row(
    *,
    kind: str = "cases",
    scope: str = "hkcfa",
    lang: str = "en",
    live_count: int | None = 100,
    live_updated_at: str | None = "2026-07-07",
    live_probed_at: int | None = 1_720_000_000,
    probe_error: str | None = None,
    local_count: int | None = 100,
    local_counted_at: int | None = 1_720_000_100,
    last_scrape_completed_at=_UNSET,
    source_generation_id: int | None = None,
) -> DbFreshnessRecord:
    """Convenience constructor for `_fresh` tests. Defaults produce an
    all-fresh row when combined with `_hkt_ts_at("2026-07-08")` for the
    scrape completion — one day after the upstream update, comfortably
    past the date-boundary check.
    """
    if last_scrape_completed_at is _UNSET:
        last_scrape_completed_at = _hkt_ts_at("2026-07-08")
    return DbFreshnessRecord(
        kind=kind, scope=scope, lang=lang,
        live_count=live_count, live_updated_at=live_updated_at,
        live_probed_at=live_probed_at, probe_error=probe_error,
        local_count=local_count, local_counted_at=local_counted_at,
        last_scrape_completed_at=last_scrape_completed_at,
        source_generation_id=source_generation_id,
    )


class TestFreshPredicate:
    """`_fresh` encodes the fresh_definition rule from the design
    contract. Each condition (a)–(g) has a dedicated test so a
    regression in the AND chain is bisectable to one condition."""

    def test_fresh_when_all_conditions_met(self):
        """Baseline: probe OK, counts match, upstream date <= scrape
        date. Every subsequent test flips one condition and asserts
        STALE — this test proves the fixture is genuinely fresh so
        those flips are meaningful."""
        assert _fresh(_fresh_row()) is True

    def test_stale_when_live_count_differs_from_local(self):
        """Preserves the old canary signal: count parity is still a
        must-have. `live=101, local=100` means HKLII added a row
        upstream and our local copy is behind."""
        row = _fresh_row(live_count=101, local_count=100)
        assert _fresh(row) is False

    def test_stale_when_live_updated_at_after_last_scrape(self):
        """NEW signal that db_freshness adds over the canary: an
        upstream refresh with a NEW timestamp is STALE even if counts
        match. This catches swap-in-place edits (same row count,
        different content) — the canary blind spot #3."""
        row = _fresh_row(
            live_updated_at="2026-07-08",
            last_scrape_completed_at=_hkt_ts_at("2026-07-07"),
        )
        assert _fresh(row) is False

    def test_fresh_when_live_updated_at_equals_last_scrape_date(self):
        """Boundary case: rule (g) is `<=` not `<`. Same-day upstream
        update and same-day local scrape must be FRESH — otherwise the
        first freshness check after a scrape would flip everything back
        to stale (upstream_date == today, scrape_date == today)."""
        row = _fresh_row(
            live_updated_at="2026-07-08",
            last_scrape_completed_at=_hkt_ts_at("2026-07-08", hour=1),
        )
        assert _fresh(row) is True

    def test_stale_when_probe_error_present(self):
        """Fail-safe: we couldn't confirm freshness. Every other column
        may be OK but the last probe failed → we don't KNOW if HKLII has
        moved on, so scrape to be safe."""
        row = _fresh_row(probe_error="HTTP 500")
        assert _fresh(row) is False

    def test_stale_when_last_scrape_completed_at_null(self):
        """Probe landed, counts happen to match, but we've never
        cleanly scraped this bucket. STALE — the counts might be a
        coincidence of an empty upstream + empty local."""
        row = _fresh_row(last_scrape_completed_at=None)
        assert _fresh(row) is False

    def test_stale_when_live_updated_at_null(self):
        """A wire probe hasn't succeeded yet — cannot claim freshness
        without an upstream signal. STALE."""
        row = _fresh_row(live_updated_at=None)
        assert _fresh(row) is False

    def test_stale_when_live_count_null(self):
        """Sister of the previous test: wire probe never got a count
        back either. STALE."""
        row = _fresh_row(live_count=None)
        assert _fresh(row) is False

    def test_stale_when_local_count_null(self):
        """`recompute_local_count` has never run for this bucket.
        Cannot compare counts → STALE."""
        row = _fresh_row(local_count=None)
        assert _fresh(row) is False

    def test_stale_when_live_updated_at_malformed(self):
        """`live_updated_at` comes off the wire — if HKLII ever
        serves a malformed value (e.g. a locale-formatted string),
        rule (g) can't parse it. STALE, not exception. Fail-safe."""
        row = _fresh_row(live_updated_at="not-a-date")
        assert _fresh(row) is False

    def test_stale_when_same_day_probe_is_more_recent_than_scrape(self):
        """Regression pin for adversarial D2 finding #3 (same-day HKLII
        update race).

        Scenario:
          * Day 1 09:00 HKT — probe: live_updated_at='2026-07-08',
            live_probed_at=T_probe1.
          * Day 1 09:30 HKT — scrape completes:
            last_scrape_completed_at=T_scrape > T_probe1.
          * Day 1 14:00 HKT — HKLII publishes a new judgment;
            live_updated_at still reads '2026-07-08' (server has not
            rolled to Day 2 yet).
          * Day 2 09:00 HKT — probe: live_updated_at='2026-07-08'
            (unchanged), live_probed_at=T_probe2 > T_scrape.

        Under the pre-fix rule ``date(live_updated_at) <= date(
        last_scrape_completed_at)``, the Day 2 probe finds
        '2026-07-08' <= '2026-07-08' → FRESH. We skip the scrape, and
        the new judgment stays invisible until either HKLII rolls
        live_updated_at forward (uncertain) or the count parity trips
        (which the bilingual UPSERT blind spot in finding #4 can also
        hide).

        The design's fail-safe claim (``fresh_definition``) was that a
        wrong assumption produces false-STALE, never false-FRESH. This
        scenario is the case where the guarantee didn't hold. The fix
        must catch it: when live_updated_at date == last_scrape_
        completed_at date AND the probe happened AFTER the scrape
        completed, the bucket is STALE — HKLII may have added content
        between the scrape end and the probe start, and same-day-
        granularity live_updated_at cannot distinguish that.
        """
        row = _fresh_row(
            live_updated_at="2026-07-08",
            # Day 1 09:30 HKT — scrape end
            last_scrape_completed_at=_hkt_ts_at("2026-07-08", hour=9)
            + 30 * 60,
            # Day 2 09:00 HKT — probe fires AFTER Day 1 scrape end
            live_probed_at=_hkt_ts_at("2026-07-09", hour=9),
        )
        assert _fresh(row) is False, (
            "Same-day upstream date + probe AFTER scrape end should "
            "STALE — otherwise HKLII updates published between scrape "
            "end and probe stay invisible until the wire date rolls. "
            "See finding #3."
        )


# ---------- FreshnessRunner.probe_all / probe_one -------------------------

class _FakeGet:
    """Test double for the injected `get` callable — records every URL
    it was called with (order preserved) and returns pre-programmed
    responses either as a fixed body/status or a URL → response map.
    """

    def __init__(
        self,
        *,
        default_status: int = 200,
        default_body: dict | None = None,
        by_url: dict[str, httpx.Response] | None = None,
        raise_for: dict[str, Exception] | None = None,
    ):
        self.calls: list[str] = []
        self._default_status = default_status
        self._default_body = default_body or {
            "count": 100, "timestamp": "2026-07-07",
        }
        self._by_url = by_url or {}
        self._raise_for = raise_for or {}

    async def __call__(self, url: str, **kw):
        self.calls.append(url)
        if url in self._raise_for:
            raise self._raise_for[url]
        if url in self._by_url:
            return self._by_url[url]
        return httpx.Response(
            self._default_status, json=self._default_body,
            request=httpx.Request("GET", url),
        )


class TestProbeAllUrlDispatch:
    """probe_all iterates every mapped triple in the matrix and calls
    the correct getmeta* URL for each. Pins the URL contract at the
    runner boundary — if dispatch_url is right but probe_all wires it
    to the wrong triple, this suite fails."""

    async def test_case_family_slug_hits_getmetacase(self):
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert get.calls == [
            "https://www.hklii.hk/api/getmetacase?caseDb=hkcfa&lang=en",
        ]

    async def test_ukpc_slug_hits_getmetahopt_dbcat_c(self):
        """UKPC in matrix.cases must NOT route to getmetacase — its
        metadata lives on the hopt-C endpoint. Same wire-family split
        as ukpc.py's scrape path."""
        matrix = _make_matrix(cases={"ukpc": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert get.calls == [
            "https://www.hklii.hk/api/getmetahopt"
            "?dbcat=C&abbr=ukpc&lang=en",
        ]

    async def test_legis_cap_type_hits_getmetalegis(self):
        matrix = _make_matrix(legis={"ord": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert get.calls == [
            "https://www.hklii.hk/api/getmetalegis?cap_type=ord&lang=en",
        ]

    async def test_legis_hopt_slug_hits_getmetahopt_dbcat_other(self):
        """bacpg/bahkg/hktmc/hktml/hkts are HOPT abbrs that share the
        `legis` bucket in DatabaseMatrix. probe_all must route them to
        getmetahopt?dbcat=other, NOT getmetalegis."""
        matrix = _make_matrix(legis={"hkts": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert get.calls == [
            "https://www.hklii.hk/api/getmetahopt"
            "?dbcat=other&abbr=hkts&lang=en",
        ]

    async def test_other_bucket_slugs_are_skipped(self):
        """other/hkiac etc. are D3 backlog — dispatch_url returns None
        and probe_all must not hit them. Otherwise every update run
        would blindly try to scrape hkiac/pd/histlaw which have no
        runner (first_run_semantics rule 5)."""
        matrix = _make_matrix(other={"hkiac": ("en",), "pd": ("en", "tc")})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert get.calls == []

    async def test_legis_histlaw_is_skipped(self):
        """histlaw has no known getmeta* endpoint — same D3 gap as
        other/*. Must skip without probing OR writing a row."""
        matrix = _make_matrix(legis={"histlaw": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            rows = list(db.iter_freshness_rows())
        finally:
            db.close()
        assert get.calls == []
        assert rows == []

    async def test_sc_lang_is_skipped(self):
        """DatabaseMatrix surfaces `sc` (Simplified Chinese) for some
        legis slugs. LEGIS_LANGS is en/tc-only and there is no local
        sc corpus; sc probes would return counts that never converge
        against local_count. Punted to D3 — skip for now."""
        matrix = _make_matrix(legis={"ord": ("en", "sc", "tc")})
        db = CheckpointDB(":memory:")
        get = _FakeGet()
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
        finally:
            db.close()
        assert "lang=sc" not in "".join(get.calls)
        # Exactly two probes: en + tc.
        assert len(get.calls) == 2


class TestProbeAllPersistence:
    """probe_all writes ONLY the wire-side columns (via
    upsert_freshness_probe) plus a recompute of local_count. It MUST
    NOT touch last_scrape_completed_at — the checkpoint tests already
    lock that boundary at the accessor layer; this class locks it at
    the runner boundary."""

    async def test_upserts_wire_columns_only_preserving_scrape_columns(
        self,
    ):
        db = CheckpointDB(":memory:")
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_720_000_000,
            source_generation_id=99,
        )
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        get = _FakeGet(default_body={
            "count": 2143, "timestamp": "2026-07-08",
        })
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert row is not None
        assert row.live_count == 2143
        assert row.live_updated_at == "2026-07-08"
        assert row.probe_error is None
        # Scrape-runner columns untouched.
        assert row.last_scrape_completed_at == 1_720_000_000
        assert row.source_generation_id == 99

    async def test_recomputes_local_count(self):
        """After probe_all, local_count reflects the current
        status='downloaded' row count for that (kind, scope, lang).
        Otherwise `_fresh` will always see `local_count IS NULL` and
        every bucket flips STALE regardless of wire state."""
        db = CheckpointDB(":memory:")
        for n in (1, 2, 3):
            db.upsert_case(
                "hkcfa", 2026, n, f"N{n}", "T", "2026-01-01", lang="en",
            )
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, n, ["html"])
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert row is not None
        assert row.local_count == 3

    async def test_legis_sc_lang_probes_and_persists(self):
        """SC lang for legis+{ord,reg,instrument} is a real HKLII slice
        (ord SC=838, reg SC=2253, instrument SC=63 per 2026-07-08 live
        probe). Previously filtered out at ``_triples`` yield time, so
        no freshness row ever landed for the trilingual legis
        databases; enabling SC gives the operator visibility on drift.

        Local corpus is still EN+TC only per ``legis.LEGIS_LANGS``, so
        SC buckets stay permanent-STALE until an SC scraper ships —
        that's the correct signal ('HKLII has 838 ordinances in SC,
        we have 0') rather than silent gap.
        """
        matrix = _make_matrix(legis={"ord": ("en", "sc", "tc")})
        db = CheckpointDB(":memory:")
        get = _FakeGet(default_body={"count": 838, "timestamp": "2026-07-08"})
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            sc_row = db.get_freshness_row("legis", "ord", "sc")
        finally:
            db.close()
        assert sc_row is not None, (
            "SC lang was filtered out — freshness never probes trilingual "
            "legis dbs. Remove sc from the _ACCEPTED_LANGS blocklist."
        )
        assert sc_row.live_count == 838
        assert (
            "https://www.hklii.hk/api/getmetalegis?cap_type=ord&lang=sc"
            in get.calls
        )

    async def test_cases_tc_local_count_uses_sidecar_walk(self, tmp_path):
        """probe_all for a cases+tc bucket walks output_dir/{scope}/ for
        ``*.tc.json`` sidecars and adds that count on top of the naive
        ``lang='tc'`` count when calling ``recompute_local_count``.

        Why: ``upsert_case`` collapses bilingual (en+tc) rows to
        lang='en', so the naive TC count only sees tc-only rows and
        misses the bilingual half of HKLII's ``getmetacase?lang=tc``
        total. Each bilingual case has one ``.tc.json`` sidecar written
        by ``case_translations.py``; walking the disk restores parity.

        Setup: hkcfa has 1 tc-only row in the DB plus 3 ``.tc.json``
        sidecars on disk (representing 3 bilingual cases). Expected
        local_count = 1 + 3 = 4.
        """
        db = CheckpointDB(":memory:")
        # 1 tc-only row for hkcfa.
        db.upsert_case(
            "hkcfa", 2026, 100, "N", "T", "2026-01-01", lang="tc",
        )
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 100, ["html"])
        # 3 bilingual cases represented by disk-only sidecars.
        (tmp_path / "hkcfa" / "2026").mkdir(parents=True)
        for n in (1, 2, 3):
            (tmp_path / "hkcfa" / "2026" / f"hkcfa_2026_{n}.tc.json").write_text("{}")

        matrix = _make_matrix(cases={"hkcfa": ("tc",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
            output_dir=tmp_path,
        )
        try:
            await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "tc")
        finally:
            db.close()
        assert row is not None
        assert row.local_count == 4, (
            "expected 1 tc-only + 3 sidecars; got "
            f"{row.local_count}. The FreshnessRunner disk walk did not "
            "run or did not pass sidecar_count to recompute_local_count."
        )

    async def test_output_dir_none_skips_sidecar_walk(self):
        """Without ``output_dir`` FreshnessRunner falls back to the
        naive tc-only count. Existing test callers that never had disk
        access continue to work unchanged."""
        db = CheckpointDB(":memory:")
        db.upsert_case(
            "hkcfa", 2026, 100, "N", "T", "2026-01-01", lang="tc",
        )
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 100, ["html"])
        matrix = _make_matrix(cases={"hkcfa": ("tc",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "tc")
        finally:
            db.close()
        assert row is not None
        assert row.local_count == 1

    async def test_sidecar_walk_ignored_for_legis_and_hopt_kinds(
        self, tmp_path,
    ):
        """The sidecar walk only fires for ``cases`` + ``tc``. legis
        and hopt kinds don't have a bilingual collapse rule, so their
        recompute stays direct even when output_dir is set."""
        db = CheckpointDB(":memory:")
        db.upsert_legis_document("ord", "1", "tc", "T")
        db.claim_pending_legis()
        db.mark_legis_downloaded(
            "ord", "1", "tc",
            latest_vid=99, latest_version_date="2026-01-01",
            formats=["content"],
        )
        # Sidecar for a different kind — must be ignored.
        (tmp_path / "legis" / "ord" / "1").mkdir(parents=True)
        (tmp_path / "legis" / "ord" / "1" / "ord_1_tc.tc.json").write_text("{}")

        matrix = _make_matrix(legis={"ord": ("tc",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
            output_dir=tmp_path,
        )
        try:
            await runner.probe_all()
            row = db.get_freshness_row("legis", "ord", "tc")
        finally:
            db.close()
        assert row is not None
        assert row.local_count == 1

    async def test_skips_row_write_for_unmapped_triples(self):
        """other/hkiac probe returns None from dispatch_url — no wire
        request, no db_freshness row. If a row WERE inserted, the
        --skip-if-fresh gate would see it and skip a scrape that has
        never actually run."""
        db = CheckpointDB(":memory:")
        matrix = _make_matrix(other={"hkiac": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            await runner.probe_all()
            rows = list(db.iter_freshness_rows())
        finally:
            db.close()
        assert rows == []


class TestProbeAllErrorTolerance:
    """One bucket 5xx / non-JSON / timeout MUST NOT abort the sweep
    — the wire-side probe_error column records the failure, the loop
    continues, and callers get a full picture across every bucket."""

    async def test_records_probe_error_on_http_500(self):
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        db = CheckpointDB(":memory:")
        get = _FakeGet(default_status=500, default_body={})
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            outcomes = await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert len(outcomes) == 1
        assert outcomes[0].ok is False
        assert outcomes[0].error is not None
        assert "500" in outcomes[0].error
        assert row is not None
        assert row.probe_error is not None
        assert "500" in row.probe_error
        # Wire values were never populated — nothing to preserve
        # against on this run either.
        assert row.live_count is None

    async def test_records_probe_error_on_non_json_body(self):
        """Some 200-OK bodies aren't JSON (proxy captchas, gunicorn
        error pages). Treat as probe_error, not exception."""
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        db = CheckpointDB(":memory:")
        by_url = {
            "https://www.hklii.hk/api/getmetacase?caseDb=hkcfa&lang=en":
                httpx.Response(
                    200, text="not json",
                    request=httpx.Request(
                        "GET",
                        "https://www.hklii.hk/api/getmetacase"
                        "?caseDb=hkcfa&lang=en",
                    ),
                ),
        }
        get = _FakeGet(by_url=by_url)
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            outcomes = await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert outcomes[0].ok is False
        assert row is not None
        assert row.probe_error is not None

    async def test_records_probe_error_on_transport_exception(self):
        """A raw httpx.ConnectError / TimeoutException must be caught
        by probe_one (not surface out of probe_all) so a single flaky
        bucket doesn't tank the sweep."""
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        db = CheckpointDB(":memory:")
        raise_for = {
            "https://www.hklii.hk/api/getmetacase?caseDb=hkcfa&lang=en":
                httpx.ConnectError("connection refused"),
        }
        get = _FakeGet(raise_for=raise_for)
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            outcomes = await runner.probe_all()
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert outcomes[0].ok is False
        assert row is not None
        assert "ConnectError" in row.probe_error

    async def test_one_bucket_5xx_does_not_tank_the_sweep(self):
        """The whole point of per-bucket error tolerance: hkcfa returns
        500, hkca returns 200 — hkca still ends up with a healthy row.
        Regression pin against the pre-canary silent-continue bug where
        one bad bucket aborted the whole probe pass."""
        matrix = _make_matrix(cases={"hkcfa": ("en",), "hkca": ("en",)})
        db = CheckpointDB(":memory:")
        by_url = {
            "https://www.hklii.hk/api/getmetacase?caseDb=hkcfa&lang=en":
                httpx.Response(
                    500, text="err",
                    request=httpx.Request(
                        "GET",
                        "https://www.hklii.hk/api/getmetacase"
                        "?caseDb=hkcfa&lang=en",
                    ),
                ),
            "https://www.hklii.hk/api/getmetacase?caseDb=hkca&lang=en":
                httpx.Response(
                    200,
                    json={"count": 500, "timestamp": "2026-07-08"},
                    request=httpx.Request(
                        "GET",
                        "https://www.hklii.hk/api/getmetacase"
                        "?caseDb=hkca&lang=en",
                    ),
                ),
        }
        get = _FakeGet(by_url=by_url)
        runner = FreshnessRunner(
            get=get, checkpoint=db, matrix=matrix,
        )
        try:
            outcomes = await runner.probe_all()
            hkcfa = db.get_freshness_row("cases", "hkcfa", "en")
            hkca = db.get_freshness_row("cases", "hkca", "en")
        finally:
            db.close()
        assert len(outcomes) == 2
        assert hkcfa.probe_error is not None
        assert hkca.probe_error is None
        assert hkca.live_count == 500


# ---------- FreshnessRunner.stale_buckets ---------------------------------

class TestStaleBuckets:
    """stale_buckets walks db_freshness and returns non-fresh triples.
    First-run buckets (no row) are the CALLER's responsibility
    (`first_run_missing`) — stale_buckets doesn't need the matrix and
    is a pure ledger scan.

    Order between fresh conditions is unimportant to the callers, so
    the tests assert set membership rather than list equality."""

    def _seed_fresh(self, db: CheckpointDB, kind: str, scope: str, lang: str):
        db.upsert_freshness_probe(
            kind, scope, lang,
            live_count=100, live_updated_at="2026-07-07",
            live_probed_at=1_720_000_000, probe_error=None,
        )
        # Bypass the accessor's own scope-scan by writing local_count
        # directly — the runner has no need to spin up real case rows
        # to test the freshness predicate.
        db._conn.execute(
            "UPDATE db_freshness SET local_count=100, local_counted_at=? "
            "WHERE kind=? AND scope=? AND lang=?",
            (1_720_000_100, kind, scope, lang),
        )
        db.mark_bucket_scraped(
            kind, scope, lang,
            completed_at=_hkt_ts_at("2026-07-08"),
        )

    def test_empty_when_ledger_is_empty(self):
        """No rows in db_freshness → no stale buckets. First-run
        detection is handled by first_run_missing, not stale_buckets."""
        db = CheckpointDB(":memory:")
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            assert runner.stale_buckets() == []
        finally:
            db.close()

    def test_empty_when_all_fresh(self):
        db = CheckpointDB(":memory:")
        self._seed_fresh(db, "cases", "hkcfa", "en")
        self._seed_fresh(db, "cases", "hkca", "en")
        matrix = _make_matrix(cases={
            "hkcfa": ("en",), "hkca": ("en",),
        })
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            assert runner.stale_buckets() == []
        finally:
            db.close()

    def test_includes_upstream_newer_bucket(self):
        """A bucket whose live_updated_at post-dates the last scrape
        completion (by HKT date) is STALE — the NEW signal db_freshness
        adds over the canary."""
        db = CheckpointDB(":memory:")
        self._seed_fresh(db, "cases", "hkcfa", "en")
        # Bump live_updated_at forward by one day.
        db._conn.execute(
            "UPDATE db_freshness SET live_updated_at='2026-07-09' "
            "WHERE kind='cases' AND scope='hkcfa'"
        )
        db._conn.commit()
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            stale = runner.stale_buckets()
        finally:
            db.close()
        assert stale == [FreshnessRow("cases", "hkcfa", "en")]

    def test_includes_count_mismatch_bucket(self):
        """The old canary signal, preserved. live=101 vs local=100 →
        STALE."""
        db = CheckpointDB(":memory:")
        self._seed_fresh(db, "cases", "hkcfa", "en")
        db._conn.execute(
            "UPDATE db_freshness SET live_count=101 "
            "WHERE kind='cases' AND scope='hkcfa'"
        )
        db._conn.commit()
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            stale = runner.stale_buckets()
        finally:
            db.close()
        assert stale == [FreshnessRow("cases", "hkcfa", "en")]

    def test_includes_probe_error_bucket(self):
        """Fail-safe: last probe failed → scrape. Better a wasted
        scrape than a stale local corpus."""
        db = CheckpointDB(":memory:")
        self._seed_fresh(db, "cases", "hkcfa", "en")
        db._conn.execute(
            "UPDATE db_freshness SET probe_error='HTTP 500' "
            "WHERE kind='cases' AND scope='hkcfa'"
        )
        db._conn.commit()
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            stale = runner.stale_buckets()
        finally:
            db.close()
        assert stale == [FreshnessRow("cases", "hkcfa", "en")]

    def test_includes_probe_only_bucket_with_no_scrape(self):
        """A row exists (probe landed) but last_scrape_completed_at is
        NULL — bucket has never been scraped. STALE."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=100, live_updated_at="2026-07-07",
            live_probed_at=1_720_000_000, probe_error=None,
        )
        db._conn.execute(
            "UPDATE db_freshness SET local_count=100 "
            "WHERE kind='cases' AND scope='hkcfa'"
        )
        db._conn.commit()
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            stale = runner.stale_buckets()
        finally:
            db.close()
        assert stale == [FreshnessRow("cases", "hkcfa", "en")]


# ---------- FreshnessRunner.first_run_missing -----------------------------

class TestFirstRunMissing:
    """`first_run_missing` = expected triples (from the matrix) minus
    present triples (in the ledger). Encodes first_run_semantics
    rule (1): a triple with no row is STALE for scrape-scoping
    purposes. Split from stale_buckets so the caller can distinguish
    'never seen before' from 'seen and drifted' when rendering the
    freshness report."""

    def test_all_triples_missing_when_ledger_is_empty(self):
        db = CheckpointDB(":memory:")
        matrix = _make_matrix(cases={
            "hkcfa": ("en", "tc"), "hkca": ("en",),
        })
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            missing = runner.first_run_missing()
        finally:
            db.close()
        missing_set = {(r.kind, r.scope, r.lang) for r in missing}
        assert missing_set == {
            ("cases", "hkcfa", "en"),
            ("cases", "hkcfa", "tc"),
            ("cases", "hkca", "en"),
        }

    def test_returns_empty_when_every_triple_has_a_row(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=1, live_updated_at="2026-07-07",
            live_probed_at=1, probe_error=None,
        )
        matrix = _make_matrix(cases={"hkcfa": ("en",)})
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            missing = runner.first_run_missing()
        finally:
            db.close()
        assert missing == []

    def test_excludes_unmapped_triples(self):
        """other/hkiac has no dispatch — it's not an expected triple
        and MUST NOT surface as 'missing'. Otherwise the update
        dispatcher would try to scope a scrape to a slug with no
        runner (first_run_semantics rule 5)."""
        db = CheckpointDB(":memory:")
        matrix = _make_matrix(
            cases={"hkcfa": ("en",)},
            other={"hkiac": ("en",)},
        )
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db, matrix=matrix,
        )
        try:
            missing = runner.first_run_missing()
        finally:
            db.close()
        assert {(r.kind, r.scope, r.lang) for r in missing} == {
            ("cases", "hkcfa", "en"),
        }


# ---------- FreshnessRunner.mark_bucket_scraped ---------------------------

class TestMarkBucketScrapedDelegates:
    """The runner is the natural place a scrape runner will import to
    stamp completion — kept as a thin delegator so the scrape callers
    don't need to import CheckpointDB directly (mirrors how
    UkpcRunner takes a `checkpoint` object)."""

    def test_delegates_to_checkpoint(self):
        db = CheckpointDB(":memory:")
        runner = FreshnessRunner(
            get=_FakeGet(), checkpoint=db,
            matrix=_make_matrix(cases={"hkcfa": ("en",)}),
        )
        try:
            runner.mark_bucket_scraped(
                "cases", "hkcfa", "en",
                completed_at=1_720_005_000,
                source_generation_id=7,
            )
            row = db.get_freshness_row("cases", "hkcfa", "en")
        finally:
            db.close()
        assert row is not None
        assert row.last_scrape_completed_at == 1_720_005_000
        assert row.source_generation_id == 7
