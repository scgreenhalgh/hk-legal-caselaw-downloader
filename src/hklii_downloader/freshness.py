"""Phase D2 freshness runner.

Freshness drives WHICH ``(kind, scope, lang)`` buckets need re-scraping
on the next ``hklii update`` invocation. The signal replaces the old
``coverage_canary`` counts-only heuristic, which was blind to:

  * upstream swap-in-place edits (same row count, new content),
  * TC-only drift when the bilingual-collapse rule masks it locally,
  * sub-threshold movement (canary's fixed threshold=5 hid smaller
    diffs).

The design layers three writers over one ledger table
(``db_freshness``) — see :mod:`hklii_downloader.checkpoint`:

  * :meth:`CheckpointDB.upsert_freshness_probe` — wire-side columns
    (``live_count``, ``live_updated_at``, ``live_probed_at``,
    ``probe_error``). Called by :meth:`FreshnessRunner.probe_all`.
  * :meth:`CheckpointDB.recompute_local_count` — local-side columns
    (``local_count``, ``local_counted_at``). Called by probe_all
    right after each probe so the freshness check has both sides.
  * :meth:`CheckpointDB.mark_bucket_scraped` — scrape-runner columns
    (``last_scrape_completed_at``, ``source_generation_id``). Called
    by every scrape runner on clean sweep completion (BulkScraper,
    HoptRunner, LegisRunner, UkpcRunner).

Each writer touches ONLY its own columns and uses COALESCE-preserving
semantics on the others. A drift silently corrupts the freshness
signal — enforced by the tests in ``tests/test_freshness_checkpoint.py``.

The runner injects its HTTP call site via an async ``get`` callable
(same pattern as :class:`~.ukpc.UkpcRunner` and
:class:`~.hopt.HoptRunner`) so unit tests don't need a live wire.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterator

from .checkpoint import CheckpointDB, DbFreshnessRecord
from .discovery import DatabaseMatrix

_log = logging.getLogger("hklii_downloader.freshness")

# HKLII is a Hong Kong service — ``live_updated_at`` is a bare
# ``YYYY-MM-DD`` string with no time-of-day component (probes to date
# haven't distinguished last-updated-of-db from server-now-rendered-as-
# date; see :attr:`.timestamp_semantics_conclusion` in the design). We
# convert ``last_scrape_completed_at`` (unix ts) to a Hong Kong civil
# date before comparing — under either semantic interpretation, a wrong
# assumption produces a false-STALE (over-scrape), never a false-FRESH.
HKT = timezone(timedelta(hours=8))
FRESHNESS_TIMEOUT_SECONDS_DEFAULT = 15.0

_BASE = "https://www.hklii.hk"

# Slugs served by getmetahopt?dbcat=other (the treaty / HOPT family).
# Kept local rather than imported from :mod:`.hopt` because a future
# addition may want to grow independently (freshness is metadata-only,
# scrape needs the full runner surface).
_HOPT_SLUGS = frozenset({"bacpg", "bahkg", "hktmc", "hktml", "hkts"})

# Every lang HKLII serves. ``sc`` is a real slice on the three
# trilingual legis-native slugs (ord/reg/instrument) plus the 3
# trilingual /databases "other" bucket entries; live probe on
# 2026-07-08 confirmed ``getmetalegis?cap_type=ord&lang=SC`` returns
# 838, matching EN/TC. Probing SC gives operators drift visibility on
# those DBs even though we don't currently scrape SC — buckets sit at
# permanent-STALE with a clear "we have 0, HKLII has N" signal that
# points at the scrape gap. The matrix already filters per-slug lang
# availability, so this frozenset is only a global sanity fence.
_ACCEPTED_LANGS = frozenset({"en", "tc", "sc"})

# The category tokens ``classify`` emits AND ``dispatch_url`` accepts.
# Keeping the token space small keeps drift bugs local.
_CATEGORIES = frozenset({
    "cases", "cases-ukpc",
    "legis", "legis-hopt", "legis-histlaw",
    "other-unknown",
})

# Checkpoint kind per category, or None for categories with no wire
# endpoint (D3 backlog). UKPC lives at kind='cases' because its rows
# are stored in the cases table (see ukpc.py + upsert_downloaded_case).
_CATEGORY_TO_KIND = {
    "cases": "cases",
    "cases-ukpc": "cases",
    "legis": "legis",
    "legis-hopt": "hopt",
    "legis-histlaw": None,
    "other-unknown": None,
}


@dataclass(frozen=True)
class FreshnessRow:
    """Identifier for one bucket in the freshness ledger.

    Frozen so it can go into sets — ``FreshnessRunner.first_run_missing``
    uses set difference against the ledger's present triples.
    """
    kind: str      # 'cases' | 'legis' | 'hopt' — matches checkpoint kind
    scope: str     # slug: 'hkcfa', 'ord', 'hkts', 'ukpc', ...
    lang: str      # 'en' | 'tc'


@dataclass(frozen=True)
class ProbeOutcome:
    """Result of one wire probe. Never raises — a transport exception
    or non-JSON body is recorded as ``error`` and ``ok=False`` so the
    caller (``probe_all``) can keep sweeping. Sole way a probe leaves
    the runner without a row write is ``url == ""`` (unmapped
    endpoint) — see :attr:`.first_run_semantics` rule (5).
    """
    row: FreshnessRow
    url: str
    ok: bool
    live_count: int | None
    live_updated_at: str | None
    probed_at: int          # unix ts of the probe call
    error: str | None       # None iff ok=True


def classify(bucket: str, slug: str) -> str | None:
    """Assign a ``(matrix_bucket, slug)`` pair to a dispatch category.

    ``bucket`` is one of the top-level buckets on
    :class:`.discovery.DatabaseMatrix` (``cases`` / ``legis`` / ``other``).
    Returns the category token or None if the pair is malformed
    (empty bucket, unknown top-level, etc.).

    The mapping is intentionally table-first:

      * ``cases`` bucket → ``cases-ukpc`` for slug=='ukpc', else ``cases``
      * ``legis`` bucket → ``legis-histlaw`` for slug=='histlaw';
        ``legis-hopt`` for HOPT abbrs (bacpg/bahkg/hktmc/hktml/hkts);
        else ``legis``
      * ``other`` bucket → ``other-unknown`` (D3 backlog)

    A category that maps to a None kind (``legis-histlaw`` /
    ``other-unknown``) still classifies successfully — the runner
    filters at the triple-yield step so ``probe_all`` never sees a
    triple it can't route.
    """
    if bucket == "cases":
        return "cases-ukpc" if slug == "ukpc" else "cases"
    if bucket == "legis":
        if slug == "histlaw":
            return "legis-histlaw"
        if slug in _HOPT_SLUGS:
            return "legis-hopt"
        return "legis"
    if bucket == "other":
        return "other-unknown"
    return None


def dispatch_url(category: str, slug: str, lang: str) -> str | None:
    """Return the ``getmeta*`` URL for a ``(category, slug, lang)``
    triple, or None if the category has no known endpoint.

    Mapped rows (matches :attr:`.endpoint_dispatch_table` in the design):

      ============  =======================================================
      category      URL template
      ============  =======================================================
      cases         ``getmetacase?caseDb={slug}&lang={lang}``
      cases-ukpc    ``getmetahopt?dbcat=C&abbr={slug}&lang={lang}``
      legis         ``getmetalegis?cap_type={slug}&lang={lang}``
      legis-hopt    ``getmetahopt?dbcat=other&abbr={slug}&lang={lang}``

    Legis note: ``getmetalegis`` uses the underscore param name
    ``cap_type`` (all other endpoints use camelCase). CamelCase
    ``capType=…`` silently returns count=0 rather than 400 — a
    classic silent-drift trap. See the 2026-07-08 test-correction
    commit for the live probe.
      ============  =======================================================

    ``other-unknown`` and ``legis-histlaw`` return None — the D3
    backlog. A None return is a KNOWN GAP, not a fail-safe stale
    signal (see :attr:`.first_run_semantics` rule 5): the runner must
    skip these slugs entirely so an update pass doesn't blindly try
    to scrape hkiac / pd / histlaw which have no runner.
    """
    if category == "cases":
        return f"{_BASE}/api/getmetacase?caseDb={slug}&lang={lang}"
    if category == "cases-ukpc":
        return f"{_BASE}/api/getmetahopt?dbcat=C&abbr={slug}&lang={lang}"
    if category == "legis":
        return f"{_BASE}/api/getmetalegis?cap_type={slug}&lang={lang}"
    if category == "legis-hopt":
        return f"{_BASE}/api/getmetahopt?dbcat=other&abbr={slug}&lang={lang}"
    # legis-histlaw / other-unknown / anything else — no known endpoint.
    return None


def _fresh(row: DbFreshnessRecord) -> bool:
    """Apply :attr:`.fresh_definition` to a persisted row.

    A bucket is FRESH iff ALL of:

      (a) probe_error IS NULL,
      (b) live_count IS NOT NULL,
      (c) local_count IS NOT NULL,
      (d) live_count == local_count,
      (e) last_scrape_completed_at IS NOT NULL,
      (f) live_updated_at parses cleanly,
      (g) date_of(live_updated_at) <= date_of(last_scrape_completed_at)
          on the Hong Kong civil calendar, and
      (h) if the upstream date and scrape date are the SAME civil day,
          the probe must have run AT-OR-BEFORE the scrape completed
          (``live_probed_at <= last_scrape_completed_at``). This is
          the same-day-race guard covering the case where HKLII adds
          content between our scrape end and the next probe, without
          rolling live_updated_at into the next calendar day.

    Any failure → STALE (fail-safe). A wrong semantic assumption in
    (g)/(h) produces at worst a false-STALE (over-scrape) not a
    false-FRESH — that's the whole point.

    Rule (h) motivation (adversarial D2 finding #3): the original
    ``<=`` rule (g) permitted the pattern where a probe runs today, a
    scrape completes today, HKLII publishes 4 hours later, and the
    NEXT probe (tomorrow morning) still reads the same date-granular
    live_updated_at — flipping the bucket FRESH and hiding the new
    judgment. Rule (h) tightens the boundary by requiring, on the
    ambiguous same-day case, that the probe be no more recent than the
    scrape end. In the normal update flow (probe → scrape in the same
    session) that condition holds because the probe runs BEFORE the
    scrape completes. In the stale-probe-relative-to-old-scrape case
    (the finding #3 race) it fails and the bucket flips STALE.
    """
    if row.probe_error is not None:
        return False
    if row.live_count is None or row.local_count is None:
        return False
    if row.live_count != row.local_count:
        return False
    if row.last_scrape_completed_at is None:
        return False
    if row.live_updated_at is None:
        return False
    try:
        upstream = date.fromisoformat(row.live_updated_at)
    except (TypeError, ValueError):
        # Malformed wire value — fail safe rather than crash. The next
        # probe will overwrite the bad value assuming HKLII corrects it.
        return False
    scrape_dt = datetime.fromtimestamp(
        row.last_scrape_completed_at, HKT,
    ).date()
    if upstream > scrape_dt:
        return False
    if upstream == scrape_dt:
        # Same civil day — apply rule (h). live_probed_at is populated
        # whenever the probe runs (success or failure); a NULL here
        # means the probe never fired, but a probe-never-fired row
        # would have failed rules (a)/(b)/(f) above, so we should not
        # reach here with live_probed_at == NULL. Fail safe just in case.
        if row.live_probed_at is None:
            return False
        if row.live_probed_at > row.last_scrape_completed_at:
            return False
    return True


class FreshnessRunner:
    """Orchestrate probes over the mapped triples in a
    :class:`~.discovery.DatabaseMatrix`.

    Injectable ``get`` matches the pattern in
    :class:`~.ukpc.UkpcRunner` and :class:`~.hopt.HoptRunner` — an
    async callable ``(url) -> httpx.Response`` — so unit tests can
    stand in a stub without a live wire.

    Not a background task. Callers drive :meth:`probe_all` once per
    ``hklii check-freshness`` / ``hklii update`` invocation; the
    freshness ledger is a durable, point-in-time snapshot the caller
    then consults via :meth:`stale_buckets` and
    :meth:`first_run_missing` to decide which scrape steps to run.
    """

    def __init__(
        self,
        *,
        get: Callable[[str], Awaitable],
        checkpoint: CheckpointDB,
        matrix: DatabaseMatrix,
        timeout: float = FRESHNESS_TIMEOUT_SECONDS_DEFAULT,
        output_dir: "Path | None" = None,
    ) -> None:
        self._get = get
        self._checkpoint = checkpoint
        self._matrix = matrix
        self._timeout = timeout
        # ``output_dir`` unlocks the ``*.tc.json`` sidecar walk for
        # cases+tc buckets. Without it we fall back to the naive
        # tc-only count in ``recompute_local_count`` — safe (STALE,
        # not FRESH) but parity for bilingual courts requires the walk.
        self._output_dir = output_dir

    # ---- triple enumeration ------------------------------------------

    def _triples(
        self,
        kinds: list[str] | None = None,
        slugs: list[str] | None = None,
        langs: list[str] | None = None,
    ) -> Iterator[tuple[FreshnessRow, str]]:
        """Yield ``(FreshnessRow, category)`` for every mapped triple
        in the matrix, filtered by the optional constraints.

        Filters compose as intersections: ``kinds=['cases']`` drops
        legis/hopt, ``slugs=['hkcfa']`` drops every other slug, etc.
        Empty / None constraints impose no filter.

        Yields the category alongside the row so :meth:`probe_one`
        avoids a re-classify per row — small, but keeps the URL wiring
        provenance-obvious to future readers.
        """
        kinds_set = set(kinds) if kinds else None
        slugs_set = set(slugs) if slugs else None
        langs_set = set(langs) if langs else None
        for bucket_name, bucket in (
            ("cases", self._matrix.cases),
            ("legis", self._matrix.legis),
            ("other", self._matrix.other),
        ):
            for slug, matrix_langs in bucket.items():
                category = classify(bucket_name, slug)
                if category is None:
                    continue
                kind = _CATEGORY_TO_KIND.get(category)
                if kind is None:
                    # Unmapped category (legis-histlaw / other-unknown)
                    # — D3 gap. Skip rather than yield: rule (5) of
                    # first_run_semantics keeps these slugs OUT of the
                    # stale-buckets set so update doesn't scrape them.
                    continue
                if kinds_set is not None and kind not in kinds_set:
                    continue
                if slugs_set is not None and slug not in slugs_set:
                    continue
                for lang in matrix_langs:
                    if lang not in _ACCEPTED_LANGS:
                        # sc is a D3 punt — see _ACCEPTED_LANGS.
                        continue
                    if langs_set is not None and lang not in langs_set:
                        continue
                    yield FreshnessRow(kind, slug, lang), category

    def expected_triples(
        self,
        kinds: list[str] | None = None,
        slugs: list[str] | None = None,
        langs: list[str] | None = None,
    ) -> list[FreshnessRow]:
        """Public alias for :meth:`_triples` that strips the category —
        callers who need the full expected matrix (e.g. the CLI's
        report renderer) can iterate this without knowing about
        dispatch categories."""
        return [row for row, _cat in self._triples(kinds, slugs, langs)]

    # ---- probe orchestration -----------------------------------------

    async def probe_one(
        self, row: FreshnessRow, category: str | None = None,
    ) -> ProbeOutcome:
        """Probe one bucket. Never raises — a transport / parse
        failure becomes ``ok=False`` + populated ``error``.

        ``category`` is optional; if omitted, it's derived from
        (kind, scope). Passing it explicitly is how :meth:`probe_all`
        avoids the re-classify.
        """
        if category is None:
            category = _rederive_category(row.kind, row.scope)
        url = dispatch_url(category, row.scope, row.lang) if category else None
        if url is None:
            return ProbeOutcome(
                row=row, url="", ok=False,
                live_count=None, live_updated_at=None,
                probed_at=int(time.time()),
                error="unmapped_endpoint",
            )
        try:
            resp = await self._get(url)
            status = getattr(resp, "status_code", 0)
            if status != 200:
                return ProbeOutcome(
                    row=row, url=url, ok=False,
                    live_count=None, live_updated_at=None,
                    probed_at=int(time.time()),
                    error=f"HTTP {status}",
                )
            body = resp.json()
            live_count = int(body["count"])
            live_updated_at = body["timestamp"]
            return ProbeOutcome(
                row=row, url=url, ok=True,
                live_count=live_count,
                live_updated_at=live_updated_at,
                probed_at=int(time.time()),
                error=None,
            )
        except Exception as exc:
            # Broad catch is intentional: this method is the safety
            # net for the whole probe_all loop. Any leaked exception
            # aborts the sweep, hiding freshness signal for every
            # bucket that came after — the exact silent-continue
            # regression coverage_canary shipped with pre-fix.
            return ProbeOutcome(
                row=row, url=url, ok=False,
                live_count=None, live_updated_at=None,
                probed_at=int(time.time()),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _count_tc_sidecars(self, scope: str) -> int:
        """Count ``*.tc.json`` sidecars under ``output_dir/{scope}/``.

        Each bilingual case has exactly one — written by
        :mod:`case_translations` alongside the en primary. Combined
        with the naive ``lang='tc'`` count in
        :meth:`CheckpointDB.recompute_local_count`, this restores parity
        with HKLII's ``getmetacase?lang=tc`` for every case-family court
        (verified against the live corpus 2026-07-08: hkcfa/tc, hkca/tc,
        hkcfi/tc, and every other bucket land at drift <= 13 records —
        the residual is real HKLII delta, not formula error).

        Absent scope directory (a slug we haven't scraped yet) returns
        0, so a first-run bucket lands with local_count = naive_tc = 0,
        which the caller then treats as STALE per rule (1) of
        :attr:`.first_run_semantics`.
        """
        if self._output_dir is None:
            return 0
        court_dir = Path(self._output_dir) / scope
        if not court_dir.is_dir():
            return 0
        return sum(1 for _ in court_dir.rglob("*.tc.json"))

    async def probe_all(
        self,
        *,
        kinds: list[str] | None = None,
        slugs: list[str] | None = None,
        langs: list[str] | None = None,
    ) -> list[ProbeOutcome]:
        """Probe every mapped triple in the matrix (filtered).

        For each mapped triple:

          1. call :meth:`probe_one`,
          2. upsert wire columns via
             :meth:`CheckpointDB.upsert_freshness_probe`,
          3. recompute local_count via
             :meth:`CheckpointDB.recompute_local_count`.

        Unmapped triples never enter the loop (filtered at
        :meth:`_triples`) — so no db_freshness row is created for
        histlaw / other-unknown / sc-lang.

        Returns every :class:`ProbeOutcome` (including failures) so
        the CLI can render a per-bucket summary.
        """
        outcomes: list[ProbeOutcome] = []
        _log.info("freshness.probe_all starting")
        for row, category in self._triples(kinds, slugs, langs):
            outcome = await self.probe_one(row, category=category)
            self._checkpoint.upsert_freshness_probe(
                row.kind, row.scope, row.lang,
                live_count=outcome.live_count,
                live_updated_at=outcome.live_updated_at,
                live_probed_at=outcome.probed_at,
                probe_error=outcome.error,
            )
            sidecar_count = None
            if (
                row.kind == "cases"
                and row.lang == "tc"
                and self._output_dir is not None
            ):
                sidecar_count = self._count_tc_sidecars(row.scope)
            self._checkpoint.recompute_local_count(
                row.kind, row.scope, row.lang,
                sidecar_count=sidecar_count,
            )
            outcomes.append(outcome)
            _log.debug(
                "freshness probe (%s, %s, %s) → ok=%s error=%s",
                row.kind, row.scope, row.lang, outcome.ok, outcome.error,
            )
        healthy = sum(1 for o in outcomes if o.ok)
        _log.info(
            "freshness.probe_all complete: probed=%s healthy=%s failed=%s",
            len(outcomes), healthy, len(outcomes) - healthy,
        )
        return outcomes

    # ---- consumer surface --------------------------------------------

    def stale_buckets(self) -> list[FreshnessRow]:
        """Full-scan db_freshness; return every non-fresh triple.

        Does NOT surface first-run buckets (no row in ledger) — those
        are :meth:`first_run_missing`'s job. Split for clarity: the
        CLI report renders "N stale, M first-run" separately, and the
        update dispatcher's stale-scoping is ``stale_buckets ∪
        first_run_missing`` (both treated as "scrape me").
        """
        stale: list[FreshnessRow] = []
        for rec in self._checkpoint.iter_freshness_rows():
            if not _fresh(rec):
                stale.append(FreshnessRow(rec.kind, rec.scope, rec.lang))
        return stale

    def first_run_missing(
        self,
        kinds: list[str] | None = None,
        slugs: list[str] | None = None,
        langs: list[str] | None = None,
    ) -> list[FreshnessRow]:
        """Return every expected triple (from the matrix) that has no
        db_freshness row yet.

        Semantically equivalent to ``set(expected_triples) -
        set(present_triples)``. Used by the update dispatcher on the
        first run to include never-seen buckets in the scrape scope,
        and by ``hklii check-freshness`` to render an accurate report
        before the first probe pass has ever run.
        """
        present = {
            (rec.kind, rec.scope, rec.lang)
            for rec in self._checkpoint.iter_freshness_rows()
        }
        return [
            row
            for row in self.expected_triples(kinds, slugs, langs)
            if (row.kind, row.scope, row.lang) not in present
        ]

    # ---- scrape-runner hook ------------------------------------------

    def mark_bucket_scraped(
        self,
        kind: str,
        scope: str,
        lang: str,
        *,
        completed_at: int,
        source_generation_id: int | None = None,
    ) -> None:
        """Thin delegator to :meth:`CheckpointDB.mark_bucket_scraped`.

        Scrape runners import this via the runner rather than the DB
        directly — mirrors how :class:`.ukpc.UkpcRunner` takes a
        ``checkpoint`` object and lets the caller depend on one facade
        instead of two.
        """
        self._checkpoint.mark_bucket_scraped(
            kind, scope, lang,
            completed_at=completed_at,
            source_generation_id=source_generation_id,
        )


def _rederive_category(kind: str, scope: str) -> str | None:
    """Reverse-map a checkpoint kind + scope back to a dispatch category.

    ``_triples`` yields ``(row, category)`` pairs so callers can pass
    the category through unchanged, but a caller with only the
    ``FreshnessRow`` can still probe by re-classifying here. Kept
    private — external code should route through the matrix.
    """
    if kind == "cases":
        return "cases-ukpc" if scope == "ukpc" else "cases"
    if kind == "legis":
        return "legis"
    if kind == "hopt":
        return "legis-hopt"
    return None
