"""Tests for the db_freshness table and CheckpointDB accessors.

Phase D2 introduces a freshness ledger keyed on (kind, scope, lang). The
runner-level tests (probe_all, stale_buckets, dispatch_url) live in
tests/test_freshness.py once the runner ships; this file covers the
checkpoint layer in isolation:

  * schema retrofit via CREATE TABLE IF NOT EXISTS (no _migrate_* helper),
  * upsert_freshness_probe — wire-side columns only, COALESCE preserved,
  * recompute_local_count — kind-specific COUNT(*) dispatch,
  * mark_bucket_scraped — scrape-runner columns only, insert if missing,
  * get_freshness_row / iter_freshness_rows — read accessors.

Ownership discipline mirrors upsert_hopt_document: each writer touches
only its own columns. A violation would silently corrupt the freshness
signal (e.g. a probe clobbering last_scrape_completed_at back to NULL
would re-trigger every scrape at the next update), so ownership is
asserted directly rather than left to code review.
"""
from __future__ import annotations

import sqlite3

import pytest

from hklii_downloader.checkpoint import CheckpointDB, DbFreshnessRecord


class TestDbFreshnessSchema:
    """`db_freshness` is a per-(kind, scope, lang) ledger. Composite
    natural PK, WITHOUT ROWID (matches ord_reg_edges / citations
    convention). Retrofits into pre-existing DBs via CREATE TABLE IF
    NOT EXISTS — no _migrate_* helper needed for a whole-table add
    (matches the enum_runs precedent)."""

    def test_table_created_on_fresh_init(self):
        db = CheckpointDB(":memory:")
        cols = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(db_freshness)"
            ).fetchall()
        }
        expected = {
            "kind", "scope", "lang",
            "live_count", "live_updated_at", "live_probed_at",
            "probe_error",
            "local_count", "local_counted_at",
            "last_scrape_completed_at", "source_generation_id",
        }
        assert cols == expected, (
            f"missing: {expected - cols}; extra: {cols - expected}"
        )

    def test_table_created_on_upgrade_of_pre_ship_db(self, tmp_path):
        """Legacy DBs from before the D2 ship have no db_freshness
        table. CheckpointDB init must add it via CREATE TABLE IF NOT
        EXISTS without erroring on the pre-existing cases table."""
        db_path = tmp_path / "old.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            "CREATE TABLE cases (court TEXT NOT NULL, "
            "year INTEGER NOT NULL, number INTEGER NOT NULL, "
            "neutral TEXT, title TEXT, date TEXT, "
            "status TEXT DEFAULT 'pending', formats TEXT, error TEXT, "
            "PRIMARY KEY (court, year, number));"
        )
        raw.commit()
        raw.close()

        db = CheckpointDB(str(db_path))
        try:
            cols = {
                row[1]
                for row in db._conn.execute(
                    "PRAGMA table_info(db_freshness)"
                ).fetchall()
            }
            assert "kind" in cols
            assert "live_count" in cols
            assert "last_scrape_completed_at" in cols
        finally:
            db.close()

    def test_composite_pk_rejects_duplicate_triple(self):
        """Composite PK on (kind, scope, lang) — a bare INSERT of an
        already-existing triple must raise IntegrityError.
        Idempotent upserts must go via upsert_freshness_probe /
        mark_bucket_scraped, both of which use ON CONFLICT DO UPDATE."""
        db = CheckpointDB(":memory:")
        db._conn.execute(
            "INSERT INTO db_freshness (kind, scope, lang) "
            "VALUES ('cases', 'hkcfa', 'en')"
        )
        db._conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO db_freshness (kind, scope, lang) "
                "VALUES ('cases', 'hkcfa', 'en')"
            )


class TestUpsertFreshnessProbe:
    """upsert_freshness_probe owns the wire-side columns (live_count,
    live_updated_at, live_probed_at, probe_error). Never touches
    local_count / last_scrape_completed_at — those belong to other
    accessors. Mirrors upsert_hopt_document's COALESCE-preserving
    discipline: a failed probe (live_count=None) does NOT wipe the
    previous good value — only overwrites probe_error and
    live_probed_at (both of which describe the LAST probe attempt
    regardless of outcome)."""

    def test_first_probe_inserts_wire_columns(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, live_probed_at, "
            "probe_error FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (2143, "2026-07-08", 1_720_000_000, None)

    def test_second_probe_coalesces_wire_columns_on_conflict(self):
        """First probe records live_count=2143. Second probe FAILED —
        only probe_error is meaningful. live_count / live_updated_at
        must be preserved (COALESCE), but probe_error and
        live_probed_at overwrite unconditionally because they describe
        the most recent attempt.

        Rationale: if a wire flake wipes live_count back to NULL, the
        _fresh rule (live_count IS NOT NULL) would flip healthy
        buckets to STALE on every flake — the whole point of the ledger
        is to remember the last known good value."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=None,
            live_updated_at=None,
            live_probed_at=1_720_003_600,
            probe_error="HTTP 500",
        )
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, live_probed_at, "
            "probe_error FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (2143, "2026-07-08", 1_720_003_600, "HTTP 500")

    def test_probe_clears_error_when_next_probe_succeeds(self):
        """Symmetric to the coalesce test: a healthy probe after a
        failed one must clear probe_error to NULL — the LAST probe was
        healthy, so the recorded error is stale."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=None, live_updated_at=None,
            live_probed_at=1_720_000_000,
            probe_error="HTTP 500",
        )
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_003_600,
            probe_error=None,
        )
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, probe_error "
            "FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (2143, "2026-07-08", None)

    def test_probe_does_not_touch_scrape_runner_columns(self):
        """Ownership discipline: upsert_freshness_probe MUST leave
        last_scrape_completed_at and source_generation_id untouched.
        A drift here would silently re-trigger scrapes for buckets a
        prior scrape sweep already closed — the corpus canary flake we
        are explicitly moving away from."""
        db = CheckpointDB(":memory:")
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_719_000_000,
            source_generation_id=42,
        )
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        row = db._conn.execute(
            "SELECT last_scrape_completed_at, source_generation_id "
            "FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (1_719_000_000, 42)


class TestRecomputeLocalCount:
    """recompute_local_count owns local_count / local_counted_at.
    Kind-specific dispatch: cases → cases table, legis →
    legis_documents, hopt → hopt_documents. Filters status='downloaded'
    (in-progress / failed / pending don't count as 'we have it')."""

    def test_dispatches_by_kind_over_correct_table(self):
        """Seed 5 downloaded cases, 3 downloaded legis, 2 downloaded
        hopt rows — assert each kind's count is scoped to its own
        table. A cross-table dispatch bug would surface here as e.g.
        cases count == 3 (legis leaked in)."""
        db = CheckpointDB(":memory:")
        for n in range(1, 6):
            db.upsert_case(
                "hkcfa", 2026, n, f"N{n}", "T", "2026-01-01",
                lang="en",
            )
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, n, ["html"])
        for n in range(1, 4):
            db.upsert_legis_document("ord", str(n), "en", "title")
            db.claim_pending_legis()
            db.mark_legis_downloaded(
                "ord", str(n), "en", 1, "2020-01-01", ["content"],
            )
        for n in range(1, 3):
            db.upsert_hopt_document(
                "hkts", 2026, n, "en", "T",
                neutral="N", doc_date="2026-01-01",
            )
            db.claim_pending_hopt()
            db.mark_hopt_downloaded("hkts", 2026, n, "en", ["json"])

        assert db.recompute_local_count("cases", "hkcfa", "en") == 5
        assert db.recompute_local_count("legis", "ord", "en") == 3
        assert db.recompute_local_count("hopt", "hkts", "en") == 2

    def test_filters_by_scope_and_lang(self):
        """Only rows whose scope (court/abbr) match the argument are
        counted. A scope-blind implementation would return the global
        table count instead.

        Bilingual-collapse compensation for kind='cases' + lang='tc':
        because upsert_case collapses en+tc rows to lang='en', a plain
        ``WHERE lang='tc'`` count silently drops bilingual rows.
        recompute_local_count OR's lang='en' into the tc-cases filter
        so the TC bucket is countable against HKLII's
        getmetacase?lang=tc (which includes bilingual). The 'hkcfa'
        assertion below therefore returns 3 (2 en-only + 1 tc-only) —
        the compensation over-counts slightly but that's still a
        fail-safe direction (STALE, not FRESH) and it finally allows
        parity on heavily bilingual courts where en-only is rare. See
        finding #4 in the D2 adversarial review pass.
        """
        db = CheckpointDB(":memory:")
        # hkcfa/en: 2 rows
        for n in (1, 2):
            db.upsert_case(
                "hkcfa", 2026, n, f"N{n}", "T", "2026-01-01",
                lang="en",
            )
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, n, ["html"])
        # hkca/en: 1 row  — different scope
        db.upsert_case("hkca", 2026, 100, "N", "T", "2026-01-01", lang="en")
        db.claim_pending()
        db.mark_downloaded("hkca", 2026, 100, ["html"])
        # hkcfa/tc: 1 row  — different (court, year, number) triple so
        # the en-collapse rule in upsert_case doesn't touch it.
        db.upsert_case("hkcfa", 2026, 3, "N3", "T", "2026-01-01", lang="tc")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 3, ["html"])

        assert db.recompute_local_count("cases", "hkcfa", "en") == 2
        assert db.recompute_local_count("cases", "hkca", "en") == 1
        # hkcfa/tc counts 2 en-only + 1 tc-only under the collapse
        # compensation. Different scope (hkca) still isolated because
        # scope is the top filter.
        assert db.recompute_local_count("cases", "hkcfa", "tc") == 3
        # Sanity: scope isolation still holds for the compensated path.
        assert db.recompute_local_count("cases", "hkca", "tc") == 1

    def test_only_counts_downloaded_status(self):
        """status='downloaded' is the only kind the local corpus
        contains. Pending / in-progress / failed rows are 'we intend
        to have it, but don't yet' and MUST NOT contribute — otherwise
        the count-parity signal (live_count == local_count) is
        contaminated by half-done work."""
        db = CheckpointDB(":memory:")
        # 1 downloaded
        db.upsert_case("hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="en")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        # 1 pending (never claimed)
        db.upsert_case("hkcfa", 2026, 2, "N2", "T", "2026-01-01", lang="en")
        # 1 failed
        db.upsert_case("hkcfa", 2026, 3, "N3", "T", "2026-01-01", lang="en")
        db.mark_failed("hkcfa", 2026, 3, "err")
        assert db.recompute_local_count("cases", "hkcfa", "en") == 1

    def test_writes_local_count_into_freshness_row(self):
        """After recompute, the db_freshness row holds local_count and
        local_counted_at. If the row doesn't exist yet, one is
        INSERTed with NULLs for every non-owned column."""
        db = CheckpointDB(":memory:")
        db.upsert_case("hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="en")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        db.recompute_local_count("cases", "hkcfa", "en")
        row = db._conn.execute(
            "SELECT local_count, local_counted_at "
            "FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] is not None
        assert row[1] > 0

    def test_recompute_preserves_wire_columns(self):
        """Pre-seed a probe result. Call recompute_local_count. Wire
        columns must remain — recompute owns local_* only."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        db.upsert_case("hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="en")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        db.recompute_local_count("cases", "hkcfa", "en")
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, live_probed_at, "
            "probe_error FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (2143, "2026-07-08", 1_720_000_000, None)

    def test_returns_computed_count(self):
        """Return value matches the persisted local_count. Callers rely
        on this for logging without a re-SELECT."""
        db = CheckpointDB(":memory:")
        for n in (1, 2, 3):
            db.upsert_case(
                "hkcfa", 2026, n, f"N{n}", "T", "2026-01-01", lang="en",
            )
            db.claim_pending()
            db.mark_downloaded("hkcfa", 2026, n, ["html"])
        assert db.recompute_local_count("cases", "hkcfa", "en") == 3

    def test_raises_on_unknown_kind(self):
        """Unknown kind values are a caller bug — never a silent 0.
        Matches _ENRICHMENT_KINDS validation style used elsewhere."""
        db = CheckpointDB(":memory:")
        with pytest.raises(ValueError, match="kind"):
            db.recompute_local_count("other-unknown", "hkiac", "en")

    def test_cases_tc_bucket_needs_sidecar_count_for_parity(self):
        """Bilingual UPSERT rule collapses en+tc entries to lang='en',
        so ``recompute_local_count("cases", scope, "tc")`` without a
        ``sidecar_count`` returns naive-tc-only.

        The prior OR compensation (``lang IN ('tc', 'en')``) tried to
        cover the bilingual case by counting every row, but that
        over-counts by en-only rows and permanently blocks parity for
        every court with any en-only content — live probe on 2026-07-08
        confirmed the compensation returned 39170 for hkca/tc against a
        live count of 9830. The right split is:

            live_tc  = bilingual + tc-only    (HKLII's getmetacase?lang=tc)
            local_tc = bilingual + tc-only

        We identify the bilingual count from disk via ``*.tc.json``
        sidecars — one exists for every bilingual case (upsert
        ``case_translations.py`` writes them alongside the en primary).
        The caller (``FreshnessRunner``) walks the disk and passes the
        count via ``sidecar_count``; the DB layer stays filesystem-free.

        Setup mirrors the old test: 1 bilingual + 1 en-only + 1 tc-only
        under hkcfa. The bilingual case has a ``.tc.json`` sidecar on
        disk (represented here as ``sidecar_count=1``).
        """
        db = CheckpointDB(":memory:")
        # Case 1: bilingual — two upserts, second collapses to lang='en'.
        db.upsert_case(
            "hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="en",
        )
        db.upsert_case(
            "hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="tc",
        )
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        # Case 2: en-only.
        db.upsert_case(
            "hkcfa", 2026, 2, "N2", "T", "2026-01-01", lang="en",
        )
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 2, ["html"])
        # Case 3: tc-only.
        db.upsert_case(
            "hkcfa", 2026, 3, "N3", "T", "2026-01-01", lang="tc",
        )
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 3, ["html"])

        # Sanity: verify the collapse happened as the setup assumes.
        row1_lang = db._conn.execute(
            "SELECT lang FROM cases WHERE court='hkcfa' AND year=2026 "
            "AND number=1",
        ).fetchone()[0]
        assert row1_lang == "en"

        # EN bucket unchanged: counts bilingual (collapsed to en) + en-only.
        assert db.recompute_local_count("cases", "hkcfa", "en") == 2

        # TC without sidecar_count — naive: 1 tc-only row.
        # Post-fix, this is the DETERMINISTIC value; the caller adds the
        # sidecar count on top for the parity comparison.
        assert db.recompute_local_count("cases", "hkcfa", "tc") == 1

        # TC WITH sidecar_count — exact parity with getmetacase?lang=tc:
        # 1 tc-only + 1 bilingual (one .tc.json exists) = 2.
        assert db.recompute_local_count(
            "cases", "hkcfa", "tc", sidecar_count=1,
        ) == 2


class TestMarkBucketScraped:
    """mark_bucket_scraped owns last_scrape_completed_at and
    source_generation_id. Called by every scrape runner on clean
    completion of a (kind, scope, lang) sweep. If the row doesn't
    exist yet (first-run scrape landing before any probe), INSERT with
    NULL wire columns so a later probe can UPSERT-refresh them."""

    def test_updates_last_scrape_completed_at(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_720_001_000,
            source_generation_id=42,
        )
        row = db._conn.execute(
            "SELECT last_scrape_completed_at, source_generation_id "
            "FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (1_720_001_000, 42)

    def test_creates_row_if_missing(self):
        """Scrape completes before any freshness probe has ever run.
        No db_freshness row exists yet. mark_bucket_scraped INSERTs one
        with NULL wire cols — a later probe can UPSERT-refresh them."""
        db = CheckpointDB(":memory:")
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_720_001_000,
            source_generation_id=42,
        )
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, live_probed_at, "
            "probe_error, last_scrape_completed_at, "
            "source_generation_id FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row is not None
        # Wire columns default to NULL — probe hasn't landed yet.
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        # Scrape-side columns populated.
        assert row[4] == 1_720_001_000
        assert row[5] == 42

    def test_preserves_wire_columns_when_scrape_completes(self):
        """Pre-seed wire data; mark_bucket_scraped must not touch it.
        Same ownership discipline as upsert_freshness_probe in
        reverse."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_720_001_000,
        )
        row = db._conn.execute(
            "SELECT live_count, live_updated_at, live_probed_at, "
            "probe_error FROM db_freshness "
            "WHERE kind='cases' AND scope='hkcfa' AND lang='en'"
        ).fetchone()
        assert row == (2143, "2026-07-08", 1_720_000_000, None)

    def test_source_generation_id_is_optional(self):
        """Hopt/legis scrapes don't touch enum_runs — the argument
        must default to None so those callers don't need to invent a
        placeholder id."""
        db = CheckpointDB(":memory:")
        db.mark_bucket_scraped(
            "hopt", "hkts", "en",
            completed_at=1_720_001_000,
        )
        row = db._conn.execute(
            "SELECT source_generation_id FROM db_freshness "
            "WHERE kind='hopt' AND scope='hkts' AND lang='en'"
        ).fetchone()
        assert row[0] is None


class TestGetFreshnessRow:
    """Point-read used by --skip-if-fresh gates and stale_buckets().
    Returns a DbFreshnessRecord dataclass, or None if the triple has
    no row (first-run — caller treats as STALE)."""

    def test_returns_none_when_missing(self):
        db = CheckpointDB(":memory:")
        assert db.get_freshness_row("cases", "hkcfa", "en") is None

    def test_returns_dataclass_with_every_column_populated(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        db.upsert_case("hkcfa", 2026, 1, "N1", "T", "2026-01-01", lang="en")
        db.claim_pending()
        db.mark_downloaded("hkcfa", 2026, 1, ["html"])
        db.recompute_local_count("cases", "hkcfa", "en")
        db.mark_bucket_scraped(
            "cases", "hkcfa", "en",
            completed_at=1_720_001_000,
            source_generation_id=42,
        )
        rec = db.get_freshness_row("cases", "hkcfa", "en")
        assert isinstance(rec, DbFreshnessRecord)
        assert rec.kind == "cases"
        assert rec.scope == "hkcfa"
        assert rec.lang == "en"
        assert rec.live_count == 2143
        assert rec.live_updated_at == "2026-07-08"
        assert rec.live_probed_at == 1_720_000_000
        assert rec.probe_error is None
        assert rec.local_count == 1
        assert rec.local_counted_at is not None
        assert rec.last_scrape_completed_at == 1_720_001_000
        assert rec.source_generation_id == 42

    def test_returns_dataclass_with_null_columns_where_absent(self):
        """A probe-only row (no scrape completion) surfaces with NULLs
        in scrape-runner columns. Dataclass fields are None, not
        AttributeError — callers can .last_scrape_completed_at freely
        and let their own logic classify NULL as stale."""
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=2143,
            live_updated_at="2026-07-08",
            live_probed_at=1_720_000_000,
            probe_error=None,
        )
        rec = db.get_freshness_row("cases", "hkcfa", "en")
        assert rec is not None
        assert rec.local_count is None
        assert rec.local_counted_at is None
        assert rec.last_scrape_completed_at is None
        assert rec.source_generation_id is None


class TestIterFreshnessRows:
    """Full-scan iteration used by FreshnessRunner.stale_buckets() and
    the check-freshness CLI. Table stays under ~100 rows in practice
    (one per mapped triple)."""

    def test_empty_yields_nothing(self):
        db = CheckpointDB(":memory:")
        assert list(db.iter_freshness_rows()) == []

    def test_yields_every_row_across_kinds(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=100, live_updated_at="2026-07-01",
            live_probed_at=1, probe_error=None,
        )
        db.upsert_freshness_probe(
            "legis", "ord", "en",
            live_count=200, live_updated_at="2026-07-02",
            live_probed_at=2, probe_error=None,
        )
        db.upsert_freshness_probe(
            "hopt", "hkts", "en",
            live_count=300, live_updated_at="2026-07-03",
            live_probed_at=3, probe_error=None,
        )
        rows = list(db.iter_freshness_rows())
        assert len(rows) == 3
        triples = sorted((r.kind, r.scope, r.lang) for r in rows)
        assert triples == [
            ("cases", "hkcfa", "en"),
            ("hopt", "hkts", "en"),
            ("legis", "ord", "en"),
        ]

    def test_yields_dataclass_instances(self):
        db = CheckpointDB(":memory:")
        db.upsert_freshness_probe(
            "cases", "hkcfa", "en",
            live_count=100, live_updated_at="2026-07-01",
            live_probed_at=1, probe_error=None,
        )
        rows = list(db.iter_freshness_rows())
        assert len(rows) == 1
        assert isinstance(rows[0], DbFreshnessRecord)
        assert rows[0].live_count == 100
