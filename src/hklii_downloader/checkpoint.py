from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

_log = logging.getLogger("hklii_downloader.checkpoint")


class CheckpointLockError(RuntimeError):
    """Another process holds the checkpoint DB lock."""


class CheckpointCorruptError(RuntimeError):
    """PRAGMA integrity_check reported corruption."""


@dataclass
class CaseRecord:
    court: str
    year: int
    number: int
    neutral: str
    title: str
    date: str
    status: str
    lang: str = "en"


@dataclass
class LegisRecord:
    abbr: str          # capType — ord | reg | instrument
    num: str           # chapter/rule number as string ("1", "622C")
    lang: str          # en | tc
    title: str | None
    status: str


@dataclass
class LegisVersionRecord:
    abbr: str
    num: str
    lang: str
    vid: int
    version_date: str | None
    status: str


@dataclass
class NoteupRecord:
    court: str
    year: int
    number: int
    status: str


@dataclass
class RelatedcapRecord:
    cap_number: str
    abbr: str
    lang: str
    status: str


@dataclass
class HoptRecord:
    abbr: str      # bacpg | bahkg | hktmc | hktml | hkts
    year: int
    num: int
    lang: str      # en | tc
    title: str | None
    neutral: str | None
    doc_date: str | None
    status: str


@dataclass
class DbFreshnessRecord:
    """One row of db_freshness — per-(kind, scope, lang) freshness
    ledger backing Phase D2 freshness-driven scraping.

    Column ownership is split across three writers:
      * wire-side (upsert_freshness_probe): live_count, live_updated_at,
        live_probed_at, probe_error
      * local-side (recompute_local_count): local_count, local_counted_at
      * scrape-runner (mark_bucket_scraped): last_scrape_completed_at,
        source_generation_id

    Every non-key column is nullable — a first-run bucket exists with
    every value NULL, a probe-only bucket has wire cols set and
    scrape/local NULL, and so on. Callers interpret NULLs per the
    fresh_definition rule in freshness.py.
    """
    kind: str                              # 'cases' | 'legis' | 'hopt'
    scope: str                             # slug: 'hkcfa', 'ord', 'hkts'...
    lang: str                              # 'en' | 'tc'
    live_count: int | None                 # HKLII-reported count
    live_updated_at: str | None            # HKLII 'timestamp', 'YYYY-MM-DD'
    live_probed_at: int | None             # unix ts of last probe attempt
    probe_error: str | None                # last non-200/non-JSON error
    local_count: int | None                # our downloaded-status COUNT(*)
    local_counted_at: int | None           # unix ts of last recompute
    last_scrape_completed_at: int | None   # unix ts of last clean sweep
    source_generation_id: int | None       # enum_runs.generation_id link


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS cases (
    court    TEXT NOT NULL,
    year     INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    neutral  TEXT NOT NULL,
    title    TEXT NOT NULL,
    date     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    formats  TEXT,
    error    TEXT,
    lang     TEXT NOT NULL DEFAULT 'en',
    last_seen_at INTEGER,
    summary_en_status     TEXT NOT NULL DEFAULT 'pending',
    summary_en_error      TEXT,
    summary_zh_status     TEXT NOT NULL DEFAULT 'pending',
    summary_zh_error      TEXT,
    appeal_history_status TEXT NOT NULL DEFAULT 'pending',
    appeal_history_error  TEXT,
    html_pending_at_hklii INTEGER,
    html_generated_from   TEXT,
    html_generated_error  TEXT,
    PRIMARY KEY (court, year, number)
);
CREATE TABLE IF NOT EXISTS legis_documents (
    abbr    TEXT NOT NULL,           -- capType (ord | reg | instrument)
    num     TEXT NOT NULL,           -- chapter/rule number (1, 32, 622C)
    lang    TEXT NOT NULL,           -- en | tc
    title   TEXT,
    latest_vid          INTEGER,     -- version id captured in this backup
    latest_version_date TEXT,        -- publication date of that version
    status  TEXT NOT NULL DEFAULT 'pending',
    formats TEXT,                    -- JSON list e.g. ["versions","content"]
    error   TEXT,
    last_seen_at INTEGER,
    PRIMARY KEY (abbr, num, lang)
);
CREATE TABLE IF NOT EXISTS legis_versions (
    abbr    TEXT NOT NULL,           -- ord | reg | instrument
    num     TEXT NOT NULL,           -- chapter/rule number
    lang    TEXT NOT NULL,           -- en | tc
    vid     INTEGER NOT NULL,        -- capversion id (getcapversiontoc?id=vid)
    version_date TEXT,               -- ISO date this version came into force
    status  TEXT NOT NULL DEFAULT 'pending',
    error   TEXT,
    last_seen_at INTEGER,
    PRIMARY KEY (abbr, num, lang, vid)
);
CREATE TABLE IF NOT EXISTS ord_reg_edges (
    parent_cap TEXT NOT NULL,          -- "32"  (integer cap of the ordinance)
    child_cap  TEXT NOT NULL,          -- "32A" (subsidiary regulation cap)
    lang       TEXT NOT NULL,          -- 'en' | 'tc'
    title      TEXT,                   -- captured for change detection
    first_seen TEXT NOT NULL,
    PRIMARY KEY (parent_cap, child_cap, lang)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ore_child ON ord_reg_edges(child_cap);
CREATE TABLE IF NOT EXISTS relatedcap_fetches (
    cap_number TEXT    NOT NULL,       -- "32"
    abbr       TEXT    NOT NULL,       -- 'ord' | 'reg'
    lang       TEXT    NOT NULL,       -- 'en' | 'tc'
    status     TEXT    NOT NULL DEFAULT 'pending',
    fetched_at TEXT,
    edge_count INTEGER,
    error      TEXT,
    PRIMARY KEY (cap_number, abbr, lang)
);
CREATE TABLE IF NOT EXISTS citations (
    from_key   TEXT NOT NULL,          -- "hkcfi/2023/155" (the citer)
    to_key     TEXT NOT NULL,          -- "hkcfa/2020/32" (target)
    citer_lang TEXT NOT NULL,          -- 'en' | 'tc'
    citer_freq INTEGER,                -- HKLII citation_frequency snapshot
    position   INTEGER,                -- ordinal in getcasenoteup response
    first_seen TEXT NOT NULL,
    PRIMARY KEY (from_key, to_key, citer_lang)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_cit_to ON citations(to_key);
CREATE TABLE IF NOT EXISTS noteup_fetches (
    court      TEXT    NOT NULL,
    year       INTEGER NOT NULL,
    number     INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    fetched_at TEXT,
    edge_count INTEGER,
    error      TEXT,
    PRIMARY KEY (court, year, number)
);
CREATE TABLE IF NOT EXISTS case_parallel_cites (
    case_key      TEXT NOT NULL,       -- "hkcfa/2020/32"
    parallel_cite TEXT NOT NULL,       -- "[2020] 6 HKC 46"
    PRIMARY KEY (case_key, parallel_cite)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hopt_documents (
    abbr    TEXT NOT NULL,           -- bacpg | bahkg | hktmc | hktml | hkts
    year    INTEGER NOT NULL,
    num     INTEGER NOT NULL,
    lang    TEXT NOT NULL,           -- en | tc
    title   TEXT,
    neutral TEXT,                    -- e.g. "[2018] HKTS 1"
    doc_date TEXT,                   -- ISO date of the treaty/paper
    status  TEXT NOT NULL DEFAULT 'pending',
    formats TEXT,                    -- JSON list e.g. ["json"]
    error   TEXT,
    last_seen_at INTEGER,
    PRIMARY KEY (abbr, year, num, lang)
);
CREATE TABLE IF NOT EXISTS enum_runs (
    -- A single BulkScraper.enumerate() invocation. Row inserted on start,
    -- completed_at populated on clean finish. Used by `hklii update`'s
    -- orphan_mark step to consume the freshest clean full-corpus enum
    -- without timestamp heuristics over per-bucket last_seen_at.
    --
    -- min_date_text / max_date_text record the enumeration window
    -- (HKLII's dd/mm/yyyy strings). Both NULL → full-corpus sweep.
    -- Either non-NULL → narrow window; orphan_mark ignores such rows
    -- because their started_at cutoff would mass-orphan rows outside
    -- the window.
    generation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     INTEGER NOT NULL,
    completed_at   INTEGER,
    courts_json    TEXT    NOT NULL,   -- JSON list of court slugs enumerated
    langs_json     TEXT    NOT NULL,   -- JSON list of lang codes enumerated
    min_date_text  TEXT,               -- HKLII dd/mm/yyyy, NULL = no lower bound
    max_date_text  TEXT                -- HKLII dd/mm/yyyy, NULL = no upper bound
);
CREATE INDEX IF NOT EXISTS idx_enum_runs_completed
    ON enum_runs(completed_at);
CREATE TABLE IF NOT EXISTS db_freshness (
    -- Phase D2 freshness ledger. One row per (kind, scope, lang) triple
    -- ('cases' × ALL_COURTS × en/tc, 'legis' × LEGIS_CAP_TYPES × en/tc,
    -- 'hopt' × HOPT_ABBRS × en/tc, plus ukpc under kind='cases').
    --
    -- Column ownership is split three ways — each writer must touch
    -- ONLY its own columns and use COALESCE-preserving semantics on
    -- the others. Same discipline as upsert_hopt_document w.r.t.
    -- status:
    --   * upsert_freshness_probe (wire):     live_*, probe_error
    --   * recompute_local_count (local):     local_count, local_counted_at
    --   * mark_bucket_scraped (scrape run):  last_scrape_completed_at,
    --                                        source_generation_id
    --
    -- A drift here silently corrupts the freshness signal: a probe
    -- clobbering last_scrape_completed_at back to NULL would re-trigger
    -- every scrape at the next update.
    --
    -- Composite natural PK + WITHOUT ROWID matches ord_reg_edges /
    -- citations / case_parallel_cites convention. The table stays
    -- small (~100 rows).
    kind                     TEXT NOT NULL,
    scope                    TEXT NOT NULL,
    lang                     TEXT NOT NULL,
    live_count               INTEGER,
    live_updated_at          TEXT,
    live_probed_at           INTEGER,
    probe_error              TEXT,
    local_count              INTEGER,
    local_counted_at         INTEGER,
    last_scrape_completed_at INTEGER,
    source_generation_id     INTEGER,
    PRIMARY KEY (kind, scope, lang)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_db_freshness_probed
    ON db_freshness(live_probed_at);
CREATE INDEX IF NOT EXISTS idx_db_freshness_scraped
    ON db_freshness(last_scrape_completed_at);
"""

_ENRICHMENT_KINDS = ("summary_en", "summary_zh", "appeal_history")
_ENRICHMENT_STATUSES = ("pending", "downloaded", "na", "failed")

# db_freshness.kind values — dispatched by recompute_local_count over
# cases / legis_documents / hopt_documents respectively. UKPC lives
# under kind='cases' because its rows are stored in the cases table
# (see ukpc.py + upsert_downloaded_case).
_FRESHNESS_KINDS = ("cases", "legis", "hopt")

# Column-to-table dispatch for recompute_local_count. Isolated as a
# constant so a future kind (e.g. 'histlaw' once D3 lands) is a
# single-line addition rather than a scattered edit across the method
# body.
_FRESHNESS_TABLE_BY_KIND = {
    "cases": ("cases", "court"),
    "legis": ("legis_documents", "abbr"),
    "hopt": ("hopt_documents", "abbr"),
}


class CheckpointDB:
    def __init__(self, path: str):
        self._lock_fd: int | None = None
        if path != ":memory:":
            self._acquire_lock(path)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._check_integrity(path)
        # _SCHEMA holds multiple CREATE TABLE statements; sqlite3.execute
        # only handles one at a time, so we use executescript here.
        self._conn.executescript(_SCHEMA)
        self._migrate_enrichment_columns()
        self._migrate_enum_runs_window_columns()
        self._conn.commit()

    def _check_integrity(self, path: str) -> None:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] != "ok":
            self._conn.close()
            raise CheckpointCorruptError(
                f"integrity_check failed for {path}: {row[0]}"
            )

    @staticmethod
    def is_locked_by_peer(path: str) -> bool:
        """Non-blocking peek: is the checkpoint lock currently held by
        another process?

        Every writer command (scrape, scrape-noteup, enrich, recheck-html,
        etc.) opens a CheckpointDB which grabs `<path>.lock` via
        `LOCK_EX | LOCK_NB`. This helper lets a *would-be* writer check
        BEFORE opening the DB whether another writer is already active,
        so `hklii update` can fail fast at startup rather than trip a
        `CheckpointLockError` mid-step.

        Returns True if the lock is held by another process, False if it
        is free (or if we can't create the lock file at all — matches the
        best-effort semantics of `_acquire_lock`).
        """
        lock_path = str(path) + ".lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return True
        # Free — release immediately.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False

    def _acquire_lock(self, path: str) -> None:
        lock_path = str(path) + ".lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            _log.warning(
                "Could not create checkpoint lock file at %s (%s: %s); "
                "running without cross-process protection. Concurrent "
                "scrape runs against this DB WILL race and can corrupt "
                "state.",
                lock_path, type(e).__name__, e,
            )
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CheckpointLockError(
                f"Another process holds the checkpoint lock at {lock_path}. "
                "Wait for it to finish or kill the stale process."
            )
        self._lock_fd = fd

    def _migrate_enrichment_columns(self) -> None:
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(cases)").fetchall()
        }
        if "lang" not in existing:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'"
            )
        if "last_seen_at" not in existing:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN last_seen_at INTEGER"
            )
        for kind in _ENRICHMENT_KINDS:
            if f"{kind}_status" not in existing:
                self._conn.execute(
                    f"ALTER TABLE cases ADD COLUMN {kind}_status "
                    "TEXT NOT NULL DEFAULT 'pending'"
                )
            if f"{kind}_error" not in existing:
                self._conn.execute(
                    f"ALTER TABLE cases ADD COLUMN {kind}_error TEXT"
                )
        if "html_pending_at_hklii" not in existing:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN html_pending_at_hklii INTEGER"
            )
        if "html_generated_from" not in existing:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN html_generated_from TEXT"
            )
        if "html_generated_error" not in existing:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN html_generated_error TEXT"
            )

    def _migrate_enum_runs_window_columns(self) -> None:
        # enum_runs.min_date_text/max_date_text were added after the
        # initial ship; existing DBs may have the table without them.
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(enum_runs)").fetchall()
        }
        added_columns = False
        if "min_date_text" not in existing:
            self._conn.execute(
                "ALTER TABLE enum_runs ADD COLUMN min_date_text TEXT"
            )
            added_columns = True
        if "max_date_text" not in existing:
            self._conn.execute(
                "ALTER TABLE enum_runs ADD COLUMN max_date_text TEXT"
            )
            added_columns = True
        if added_columns:
            # Any pre-existing row was stamped by the pre-fix
            # start_enum_run(courts, langs) which recorded no window
            # info — its provenance (narrow vs full-corpus) is
            # unrecoverable, and the ALTER left both new columns NULL.
            # latest_completed_enum_run treats NULL windows as
            # full-corpus, so leaving these rows completed would let
            # orphan_mark fall back to a possibly-narrow legacy row
            # and mass-orphan every out-of-window case.
            # Nuke completed_at → users must run one fresh
            # full_reconcile before orphan_mark is safe again. Small
            # cost for silent-damage safety.
            self._conn.execute(
                "UPDATE enum_runs SET completed_at = NULL "
                "WHERE completed_at IS NOT NULL"
            )

    def upsert_case(
        self, court: str, year: int, number: int,
        neutral: str, title: str, date: str, lang: str = "en",
        last_seen_at: int | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO cases (court, year, number, neutral, title, date, lang, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (court, year, number) DO UPDATE SET "
            "neutral=excluded.neutral, title=excluded.title, date=excluded.date, "
            "lang=CASE "
            "  WHEN cases.lang='en' OR excluded.lang='en' THEN 'en' "
            "  ELSE excluded.lang "
            "END, "
            "last_seen_at=COALESCE(excluded.last_seen_at, cases.last_seen_at)",
            (court, year, number, neutral, title, date, lang, last_seen_at),
        )
        self._conn.commit()

    def upsert_downloaded_case(
        self, court: str, year: int, number: int, lang: str,
        neutral: str, title: str, date: str, formats: list[str],
        last_seen_at: int | None = None,
    ) -> None:
        """Insert a cases row directly at status='downloaded'.

        UKPC entries are enumerated + fetched + saved in one pass by
        :class:`hklii_downloader.ukpc.UkpcRunner`. They must never sit
        at status='pending' waiting to be claimed because
        :meth:`claim_pending` is court-unscoped and would let a plain
        ``hklii scrape`` invocation pull ukpc rows off the pending
        queue and hit ``getjudgment`` — the WRONG endpoint family for
        the hopt-C UKPC slug — on every subsequent run.

        On conflict, preserves the existing status (downloaded stays
        downloaded, no reverts) and refreshes the metadata columns.
        Uses the same lang-collapse rule as :meth:`upsert_case` so a
        future EN counterpart for an already-TC row collapses to
        lang='en' consistently across the cases table.
        """
        self._conn.execute(
            "INSERT INTO cases "
            "(court, year, number, neutral, title, date, lang, status, "
            "formats, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'downloaded', ?, ?) "
            "ON CONFLICT (court, year, number) DO UPDATE SET "
            "neutral=excluded.neutral, title=excluded.title, "
            "date=excluded.date, "
            "lang=CASE "
            "  WHEN cases.lang='en' OR excluded.lang='en' THEN 'en' "
            "  ELSE excluded.lang "
            "END, "
            "formats=excluded.formats, "
            "last_seen_at=COALESCE(excluded.last_seen_at, "
            "                      cases.last_seen_at)",
            (court, year, number, neutral, title, date, lang,
             json.dumps(formats), last_seen_at),
        )
        self._conn.commit()

    def has_downloaded_case(
        self, court: str, year: int, number: int,
    ) -> bool:
        """Return True iff (court, year, number) exists at
        status='downloaded'.

        Used by :class:`hklii_downloader.ukpc.UkpcRunner` for idempotent
        resume — a re-run over the same corpus skips already-downloaded
        rows without re-fetching. Cheaper than the row-loading version
        of :meth:`get_formats` because the caller only needs the
        boolean.
        """
        row = self._conn.execute(
            "SELECT 1 FROM cases "
            "WHERE court=? AND year=? AND number=? "
            "AND status='downloaded' LIMIT 1",
            (court, year, number),
        ).fetchone()
        return row is not None

    def claim_pending(self, court: str | None = None) -> CaseRecord | None:
        if court:
            row = self._conn.execute(
                "SELECT court, year, number, neutral, title, date, lang "
                "FROM cases WHERE status='pending' AND court=? LIMIT 1",
                (court,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT court, year, number, neutral, title, date, lang "
                "FROM cases WHERE status='pending' LIMIT 1",
            ).fetchone()

        if not row:
            return None

        self._conn.execute(
            "UPDATE cases SET status='in_progress' "
            "WHERE court=? AND year=? AND number=?",
            (row[0], row[1], row[2]),
        )
        self._conn.commit()
        return CaseRecord(
            court=row[0], year=row[1], number=row[2],
            neutral=row[3], title=row[4], date=row[5],
            status="in_progress", lang=row[6],
        )

    def mark_downloaded(
        self, court: str, year: int, number: int, formats: list[str],
        html_pending_ts: int | None = None,
    ) -> None:
        # html_pending_ts=None means HTML was captured (or was never
        # missing); clear any prior pending stamp. A non-None value
        # means we fell back to the doc — stamp it so a future
        # recheck-html pass can find these rows.
        self._conn.execute(
            "UPDATE cases SET status='downloaded', formats=?, "
            "html_pending_at_hklii=? "
            "WHERE court=? AND year=? AND number=?",
            (json.dumps(formats), html_pending_ts, court, year, number),
        )
        self._conn.commit()

    def bump_html_pending_ts(
        self, court: str, year: int, number: int, ts: int,
    ) -> None:
        """Update only html_pending_at_hklii — don't touch status/formats."""
        self._conn.execute(
            "UPDATE cases SET html_pending_at_hklii=? "
            "WHERE court=? AND year=? AND number=?",
            (ts, court, year, number),
        )
        self._conn.commit()

    def get_formats(
        self, court: str, year: int, number: int,
    ) -> list[str] | None:
        """Return the current formats list for a row, or None if the row
        does not exist or was never marked downloaded."""
        row = self._conn.execute(
            "SELECT formats FROM cases "
            "WHERE court=? AND year=? AND number=?",
            (court, year, number),
        ).fetchone()
        if not row or row[0] is None:
            return None
        return json.loads(row[0])

    def pending_html_recheck(
        self,
        limit: int | None = None,
        max_age_days: int | None = None,
        _today_iso: str | None = None,
    ) -> list[CaseRecord]:
        """Rows previously captured via doc-fallback whose HTML may now
        be available at HKLII. status must be 'downloaded' — this is a
        deliberate follow-up pass, not a first-time download.

        `max_age_days` bounds the queue by CASE DATE (not by stamp — the
        stamp bumps forward on every re-poll). None or 0 = unlimited.
        `limit` caps the returned row count. None or 0 = unlimited
        (aligned with max_age_days so 'no cap' carries through
        consistently across both parameters).
        `_today_iso` is a test hook; production callers omit it and the
        method reads today's date in Asia/Hong_Kong.
        """
        params: list = []
        where = [
            "status='downloaded'",
            "html_pending_at_hklii IS NOT NULL",
        ]
        if max_age_days is not None and max_age_days > 0:
            from datetime import date, datetime, timedelta
            from zoneinfo import ZoneInfo
            if _today_iso is not None:
                today = date.fromisoformat(_today_iso)
            else:
                today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
            cutoff = (today - timedelta(days=max_age_days)).isoformat()
            # `date` in the DB is ISO-8601 (YYYY-MM-DD…); lex compare is
            # correct as long as we anchor on the leading 10 chars.
            # A missing / non-ISO date bypasses the filter — we'd rather
            # over-recheck a row we can't age-check than silently drop it
            # from the queue forever.
            where.append(
                "(substr(date, 1, 10) >= ? OR substr(date, 1, 10) < '1000')"
            )
            params.append(cutoff)
        q = (
            "SELECT court, year, number, neutral, title, date, lang "
            f"FROM cases WHERE {' AND '.join(where)} "
            "ORDER BY html_pending_at_hklii ASC"
        )
        # Align `limit=0` with `max_age_days=0` — both mean 'no cap'.
        # Pre-fix, `LIMIT 0` returned zero rows while `max_age_days=0`
        # returned the full queue. A caller carrying "no cap" through
        # both parameters would accidentally suppress every row.
        if limit is not None and limit > 0:
            q += f" LIMIT {int(limit)}"
        rows = self._conn.execute(q, params).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="downloaded", lang=r[6],
            )
            for r in rows
        ]

    def mark_failed(self, court: str, year: int, number: int, error: str) -> None:
        self._conn.execute(
            "UPDATE cases SET status='failed', error=? "
            "WHERE court=? AND year=? AND number=?",
            (error, court, year, number),
        )
        self._conn.commit()

    def last_enumeration_ts(self, court: str, lang: str) -> int | None:
        """Max last_seen_at for the given (court, lang), or None if never
        enumerated or all rows have NULL last_seen_at.

        The `lang` parameter is retained for API stability but the query
        no longer filters by it — upsert_case collapses bilingual cases
        to lang='en' via a CASE expression, so a strict WHERE lang='tc'
        would return None for every court whose only cases are
        bilingual (misleading the scraper's enum-cache into never
        skipping the tc pass). Reading MAX across every lang is
        semantically correct: one enumeration bumps last_seen_at once
        regardless of which lang pass surfaced the entry.
        """
        row = self._conn.execute(
            "SELECT MAX(last_seen_at) FROM cases WHERE court=?",
            (court,),
        ).fetchone()
        return row[0] if row else None

    def find_orphans(
        self, as_of_ts: int, only_downloaded: bool = False,
    ) -> list[CaseRecord]:
        """Rows whose last_seen_at is NULL or < as_of_ts — candidates for
        removal from HKLII since our last enumeration.

        `only_downloaded=True` restricts to rows we actually captured
        (status='downloaded'); useful for `hklii update --profile quarterly`
        where we only orphan-mark rows that once existed on disk. Pending
        or failed rows aren't 'orphans' — they just weren't finished.
        """
        where = ["(last_seen_at IS NULL OR last_seen_at < ?)"]
        params: list = [as_of_ts]
        if only_downloaded:
            where.append("status='downloaded'")
        rows = self._conn.execute(
            "SELECT court, year, number, neutral, title, date, lang, status "
            f"FROM cases WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status=r[7], lang=r[6],
            )
            for r in rows
        ]

    def mark_orphaned(self, court: str, year: int, number: int) -> None:
        """Flip a row's status to 'orphaned' without touching files or
        formats. Idempotent — safe to call on an already-orphaned row.

        Reserved for `hklii update --profile quarterly` where we've just
        done a full-corpus enum and confirmed the row is no longer listed
        upstream. Local files are preserved as an audit trail; the caller
        can decide when/whether to delete them.
        """
        self._conn.execute(
            "UPDATE cases SET status='orphaned' "
            "WHERE court=? AND year=? AND number=?",
            (court, year, number),
        )
        self._conn.commit()

    def mark_orphaned_below_ts(self, cutoff_ts: int) -> int:
        """Batch-mark every stale downloaded row as 'orphaned' in one
        UPDATE + one commit. Returns the affected-row count.

        Skips rows already status='orphaned' (idempotent) and rows the
        caller didn't intend to touch (status != 'downloaded').
        """
        cur = self._conn.execute(
            "UPDATE cases SET status='orphaned' "
            "WHERE status='downloaded' "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (cutoff_ts,),
        )
        self._conn.commit()
        return cur.rowcount

    def verify_downloaded_against_files(self, output_dir) -> int:
        """Scan status='downloaded' rows; flip any whose expected files are
        missing or 0-byte back to status='pending'. Returns broken count.

        fmt='doc' accepts any of .doc / .docx / .rtf on disk — matches
        _DOC_FAMILY_EXTS used by validate.py and html_generator.py.
        scraper._fetch_doc records fmt='doc' regardless of which
        magic-derived extension it actually saved (Judiciary serves
        RTF at .doc URLs per task #67), so a strict .doc-only check
        here would falsely flip 19+ production rows.
        """
        from pathlib import Path
        output_dir = Path(output_dir)
        rows = self._conn.execute(
            "SELECT court, year, number, formats FROM cases WHERE status='downloaded'"
        ).fetchall()
        _DOC_FAMILY_EXTS = (".doc", ".docx", ".rtf")
        broken = 0
        for court, year, number, formats_json in rows:
            formats = json.loads(formats_json) if formats_json else []
            stem = f"{court}_{year}_{number}"
            case_dir = output_dir / court / str(year)
            row_broken = False
            for fmt in formats:
                if fmt == "doc":
                    # Any doc-family sibling present + non-empty wins.
                    if any(
                        (case_dir / f"{stem}{ext}").exists()
                        and (case_dir / f"{stem}{ext}").stat().st_size > 0
                        for ext in _DOC_FAMILY_EXTS
                    ):
                        continue
                    row_broken = True
                    break
                path = case_dir / f"{stem}.{fmt}"
                if not path.exists() or path.stat().st_size == 0:
                    row_broken = True
                    break
            if row_broken:
                self._conn.execute(
                    "UPDATE cases SET status='pending', formats=NULL "
                    "WHERE court=? AND year=? AND number=?",
                    (court, year, number),
                )
                broken += 1
        self._conn.commit()
        return broken

    def reset_failed_to_pending(self) -> int:
        cur = self._conn.execute(
            "UPDATE cases SET status='pending', error=NULL "
            "WHERE status='failed'"
        )
        self._conn.commit()
        return cur.rowcount

    def reset_enrichment_failed_to_pending(self, kinds: list[str]) -> int:
        """Flip failed enrichment rows for the given kinds back to pending.

        Motivated by task #30: after a scrape run, some enrichment rows
        land in 'failed' state — 81 appeal_history rows in the current
        corpus, for example. pending_any_enrichment / _enrich_one both
        gate on 'pending', so these need a reset before `hklii enrich`
        can pick them up. Called by --retry-failed.

        Rows in 'na' / 'downloaded' / 'pending' are left alone.
        Returns the number of {kind}_status flips applied.
        """
        for k in kinds:
            if k not in _ENRICHMENT_KINDS:
                raise ValueError(f"unknown enrichment kind {k!r}")
        total = 0
        for kind in kinds:
            cur = self._conn.execute(
                f"UPDATE cases SET {kind}_status='pending', "
                f"{kind}_error=NULL "
                f"WHERE {kind}_status='failed'"
            )
            total += cur.rowcount
        self._conn.commit()
        return total

    def release_in_progress(self) -> None:
        self._conn.execute(
            "UPDATE cases SET status='pending' WHERE status='in_progress'",
        )
        self._conn.commit()

    def release_in_progress_hopt(self) -> None:
        """Recover hopt_documents rows stuck at 'in_progress' after a
        worker crash. Called at HoptRunner startup."""
        self._conn.execute(
            "UPDATE hopt_documents SET status='pending' "
            "WHERE status='in_progress'"
        )
        self._conn.commit()

    def release_in_progress_legis(self) -> None:
        """Recover legis_documents rows stuck at 'in_progress'."""
        self._conn.execute(
            "UPDATE legis_documents SET status='pending' "
            "WHERE status='in_progress'"
        )
        self._conn.commit()

    def release_in_progress_legis_version(self) -> None:
        """Recover legis_versions rows stuck at 'in_progress'."""
        self._conn.execute(
            "UPDATE legis_versions SET status='pending' "
            "WHERE status='in_progress'"
        )
        self._conn.commit()

    def release_in_progress_noteup(self) -> None:
        """Recover noteup_fetches rows stuck at 'in_progress'."""
        self._conn.execute(
            "UPDATE noteup_fetches SET status='pending' "
            "WHERE status='in_progress'"
        )
        self._conn.commit()

    def release_in_progress_relatedcap(self) -> None:
        """Recover relatedcap_fetches rows stuck at 'in_progress'."""
        self._conn.execute(
            "UPDATE relatedcap_fetches SET status='pending' "
            "WHERE status='in_progress'"
        )
        self._conn.commit()

    def release_row(self, court: str, year: int, number: int) -> None:
        """Flip a specific in_progress row back to pending.

        Used by the pool-exhausted re-queue path (task #65): when a
        worker sees AllProxiesDeadError, we release the row so it will
        be re-claimed once the pool recovers, rather than terminal-failing
        it. `error` is cleared so a subsequent success doesn't inherit
        a stale error message. The status guard ensures we only touch
        rows we actually hold (belt-and-suspenders against a race with
        another worker)."""
        self._conn.execute(
            "UPDATE cases SET status='pending', error=NULL "
            "WHERE status='in_progress' "
            "AND court=? AND year=? AND number=?",
            (court, year, number),
        )
        self._conn.commit()

    def pending_cases(self, courts: list[str] | None = None) -> list[CaseRecord]:
        if courts:
            placeholders = ",".join("?" * len(courts))
            rows = self._conn.execute(
                f"SELECT court, year, number, neutral, title, date, lang "
                f"FROM cases WHERE status='pending' AND court IN ({placeholders})",
                courts,
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT court, year, number, neutral, title, date, lang "
                "FROM cases WHERE status='pending'",
            ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="pending", lang=r[6],
            )
            for r in rows
        ]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM cases GROUP BY status",
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "downloaded": counts.get("downloaded", 0),
            "failed": counts.get("failed", 0),
        }

    def mark_enrichment(
        self, court: str, year: int, number: int,
        kind: str, status: str, error: str | None = None,
    ) -> None:
        if kind not in _ENRICHMENT_KINDS:
            raise ValueError(
                f"unknown enrichment kind {kind!r}; "
                f"expected one of {_ENRICHMENT_KINDS}"
            )
        if status not in _ENRICHMENT_STATUSES:
            raise ValueError(
                f"unknown enrichment status {status!r}; "
                f"expected one of {_ENRICHMENT_STATUSES}"
            )
        self._conn.execute(
            f"UPDATE cases SET {kind}_status=?, {kind}_error=? "
            "WHERE court=? AND year=? AND number=?",
            (status, error, court, year, number),
        )
        self._conn.commit()

    def get_enrichment(
        self, court: str, year: int, number: int,
    ) -> dict[str, str]:
        cols = ", ".join(f"{k}_status" for k in _ENRICHMENT_KINDS)
        row = self._conn.execute(
            f"SELECT {cols} FROM cases WHERE court=? AND year=? AND number=?",
            (court, year, number),
        ).fetchone()
        if row is None:
            raise KeyError((court, year, number))
        return dict(zip(_ENRICHMENT_KINDS, row))

    def get_enrichment_errors(
        self, court: str, year: int, number: int,
    ) -> dict[str, str]:
        cols = ", ".join(f"{k}_error" for k in _ENRICHMENT_KINDS)
        row = self._conn.execute(
            f"SELECT {cols} FROM cases WHERE court=? AND year=? AND number=?",
            (court, year, number),
        ).fetchone()
        if row is None:
            raise KeyError((court, year, number))
        return {k: v for k, v in zip(_ENRICHMENT_KINDS, row) if v}

    def pending_enrichment(
        self, kind: str, courts: list[str] | None = None,
    ) -> list[CaseRecord]:
        if kind not in _ENRICHMENT_KINDS:
            raise ValueError(f"unknown enrichment kind {kind!r}")
        where = f"{kind}_status='pending' AND status='downloaded'"
        params: tuple = ()
        if courts:
            placeholders = ",".join("?" * len(courts))
            where += f" AND court IN ({placeholders})"
            params = tuple(courts)
        rows = self._conn.execute(
            f"SELECT court, year, number, neutral, title, date, lang "
            f"FROM cases WHERE {where}",
            params,
        ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="downloaded", lang=r[6],
            )
            for r in rows
        ]

    def pending_any_enrichment(
        self,
        kinds: list[str],
        courts: list[str] | None = None,
    ) -> list[CaseRecord]:
        """Return downloaded cases with any of the given enrichment kinds pending."""
        for k in kinds:
            if k not in _ENRICHMENT_KINDS:
                raise ValueError(f"unknown enrichment kind {k!r}")
        or_clauses = " OR ".join(f"{k}_status='pending'" for k in kinds)
        where = f"status='downloaded' AND ({or_clauses})"
        params: tuple = ()
        if courts:
            placeholders = ",".join("?" * len(courts))
            where += f" AND court IN ({placeholders})"
            params = tuple(courts)
        rows = self._conn.execute(
            f"SELECT court, year, number, neutral, title, date, lang "
            f"FROM cases WHERE {where}",
            params,
        ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="downloaded", lang=r[6],
            )
            for r in rows
        ]

    def enrichment_stats(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for kind in _ENRICHMENT_KINDS:
            counts = {s: 0 for s in _ENRICHMENT_STATUSES}
            for row in self._conn.execute(
                f"SELECT {kind}_status, COUNT(*) FROM cases GROUP BY {kind}_status"
            ).fetchall():
                counts[row[0]] = row[1]
            result[kind] = counts
        return result

    def mark_html_generated(
        self, court: str, year: int, number: int, source_ext: str,
    ) -> None:
        """Record a successful doc → html conversion.

        source_ext is the on-disk extension the html was generated from
        (`.doc` / `.docx` / `.rtf`) — used later for provenance and
        per-source-ext stats. Clears any prior error so a retry that
        succeeds doesn't leave stale error text behind.
        """
        self._conn.execute(
            "UPDATE cases SET html_generated_from=?, "
            "html_generated_error=NULL "
            "WHERE court=? AND year=? AND number=?",
            (source_ext, court, year, number),
        )
        self._conn.commit()

    def mark_html_generation_failed(
        self, court: str, year: int, number: int, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE cases SET html_generated_from=NULL, "
            "html_generated_error=? "
            "WHERE court=? AND year=? AND number=?",
            (error, court, year, number),
        )
        self._conn.commit()

    def pending_html_generation(
        self, limit: int | None = None, include_failed: bool = False,
    ) -> list[CaseRecord]:
        """Rows targeted for doc → html conversion.

        A row qualifies iff its formats list is exactly ["doc"] — those
        are the empty-content-at-HKLII cases where the doc-family file
        is the only judgment content on disk. formats=[..., "doc", ...]
        rows already have html/txt/json and are out of scope.

        By default, rows previously marked failed are excluded so a
        second run doesn't repeat the same failure — pass
        include_failed=True (or the CLI's --force flag) to retry them.
        """
        where = (
            "status='downloaded' "
            "AND formats=?"
            " AND html_generated_from IS NULL"
        )
        params: list = ['["doc"]']
        if not include_failed:
            where += " AND html_generated_error IS NULL"
        q = (
            "SELECT court, year, number, neutral, title, date, lang "
            f"FROM cases WHERE {where}"
        )
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        rows = self._conn.execute(q, params).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="downloaded", lang=r[6],
            )
            for r in rows
        ]

    def html_generation_stats(self) -> dict:
        """Report generated/failed/pending counts + per-source-ext breakdown.

        Scoped to formats=["doc"] rows (the population targeted for
        conversion). Rows outside that scope don't factor in.
        """
        scope = "status='downloaded' AND formats='[\"doc\"]'"
        generated = self._conn.execute(
            f"SELECT COUNT(*) FROM cases WHERE {scope} "
            "AND html_generated_from IS NOT NULL"
        ).fetchone()[0]
        failed = self._conn.execute(
            f"SELECT COUNT(*) FROM cases WHERE {scope} "
            "AND html_generated_from IS NULL "
            "AND html_generated_error IS NOT NULL"
        ).fetchone()[0]
        pending = self._conn.execute(
            f"SELECT COUNT(*) FROM cases WHERE {scope} "
            "AND html_generated_from IS NULL "
            "AND html_generated_error IS NULL"
        ).fetchone()[0]

        by_ext: dict[str, int] = {}
        for row in self._conn.execute(
            f"SELECT html_generated_from, COUNT(*) FROM cases "
            f"WHERE {scope} AND html_generated_from IS NOT NULL "
            "GROUP BY html_generated_from"
        ).fetchall():
            by_ext[row[0]] = row[1]

        return {
            "generated": generated,
            "failed": failed,
            "pending": pending,
            "by_source_ext": by_ext,
        }

    def upsert_relatedcap_fetch(
        self, cap_number: str, abbr: str, lang: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO relatedcap_fetches "
            "(cap_number, abbr, lang) VALUES (?, ?, ?)",
            (cap_number, abbr, lang),
        )
        self._conn.commit()

    def mark_relatedcap_ok(
        self, cap_number: str, abbr: str, lang: str,
        edge_count: int, fetched_at: str,
    ) -> None:
        self._conn.execute(
            "UPDATE relatedcap_fetches SET status='ok', "
            "edge_count=?, fetched_at=?, error=NULL "
            "WHERE cap_number=? AND abbr=? AND lang=?",
            (edge_count, fetched_at, cap_number, abbr, lang),
        )
        self._conn.commit()

    def mark_relatedcap_failed(
        self, cap_number: str, abbr: str, lang: str, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE relatedcap_fetches SET status='error', error=? "
            "WHERE cap_number=? AND abbr=? AND lang=?",
            (error, cap_number, abbr, lang),
        )
        self._conn.commit()

    # ---- enum-run generation tracking (see enum_runs table) ---------

    def start_enum_run(
        self, courts: list[str] | tuple[str, ...],
        langs: list[str] | tuple[str, ...],
        *,
        min_date_text: str | None = None,
        max_date_text: str | None = None,
    ) -> int:
        """Record the start of a BulkScraper.enumerate() invocation.

        Returns the newly allocated generation_id which the caller passes
        back to `complete_enum_run` on clean finish. If the caller
        crashes or aborts before completion, the row's `completed_at`
        stays NULL and downstream consumers (orphan_mark) treat it as
        an incomplete run and skip it.

        `min_date_text` / `max_date_text` record the enumeration window
        (HKLII dd/mm/yyyy strings). Both None → full-corpus sweep, the
        only kind orphan_mark will consume as its reference generation.
        Either non-None → narrow window; the row completes normally and
        is queryable, but `latest_completed_enum_run` filters it out so
        a subsequent partial full_reconcile can't fall back to a
        daily-narrow row and mass-orphan every out-of-window case.
        """
        cur = self._conn.execute(
            "INSERT INTO enum_runs "
            "(started_at, completed_at, courts_json, langs_json, "
            "min_date_text, max_date_text) "
            "VALUES (?, NULL, ?, ?, ?, ?)",
            (
                int(time.time()),
                json.dumps(list(courts)),
                json.dumps(list(langs)),
                min_date_text,
                max_date_text,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def complete_enum_run(self, generation_id: int) -> None:
        """Mark a previously-started enum run as cleanly completed."""
        self._conn.execute(
            "UPDATE enum_runs SET completed_at=? WHERE generation_id=?",
            (int(time.time()), generation_id),
        )
        self._conn.commit()

    def latest_completed_enum_run(self) -> dict | None:
        """Latest completed FULL-CORPUS enum_runs row. Returns
        {generation_id, started_at, completed_at, courts, langs} or None
        if no full-corpus run has ever completed cleanly. Used by
        orphan_mark.

        A narrow-window enum (daily/weekly/monthly scrape with a
        min_date_text or max_date_text) can complete cleanly and touch
        every court/lang bucket, but only enumerates rows dated inside
        the window — every downloaded row older than the window keeps
        its stale last_seen_at. If orphan_mark used a narrow row's
        started_at as its cutoff, mark_orphaned_below_ts would flip
        every out-of-window downloaded row to 'orphaned'. Filtering to
        min_date_text IS NULL AND max_date_text IS NULL prevents that
        silent-corpus-damage path.
        """
        row = self._conn.execute(
            "SELECT generation_id, started_at, completed_at, "
            "courts_json, langs_json FROM enum_runs "
            "WHERE completed_at IS NOT NULL "
            "AND min_date_text IS NULL AND max_date_text IS NULL "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "generation_id": row[0],
            "started_at": row[1],
            "completed_at": row[2],
            "courts": json.loads(row[3]),
            "langs": json.loads(row[4]),
        }

    def reset_relatedcap_fetches(self) -> int:
        """Reset every relatedcap_fetches row to status='pending' so the
        next scrape-relatedcaps run does a fresh diff. Returns the
        affected row count. Idempotent no-op when the table is missing.

        Kept on CheckpointDB so the reset reuses the shared connection —
        avoids the WAL / busy_timeout pragma-bypass a raw sqlite3.connect
        would introduce.
        """
        try:
            cur = self._conn.execute(
                "UPDATE relatedcap_fetches SET status='pending'"
            )
            self._conn.commit()
            return cur.rowcount
        except sqlite3.OperationalError as exc:
            # Narrow the swallow to "table missing" specifically —
            # otherwise a real op error (disk full, lock contention,
            # corrupt image) would silently return 0 and the caller
            # would launch scrape-relatedcaps thinking the DB is
            # already fresh-diff-ready.
            if "no such table" in str(exc).lower():
                return 0
            raise

    def claim_pending_relatedcap(self) -> RelatedcapRecord | None:
        row = self._conn.execute(
            "SELECT cap_number, abbr, lang FROM relatedcap_fetches "
            "WHERE status='pending' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE relatedcap_fetches SET status='in_progress' "
            "WHERE cap_number=? AND abbr=? AND lang=?",
            (row[0], row[1], row[2]),
        )
        self._conn.commit()
        return RelatedcapRecord(
            cap_number=row[0], abbr=row[1], lang=row[2],
            status="in_progress",
        )

    def relatedcap_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM relatedcap_fetches GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "ok": counts.get("ok", 0),
            "error": counts.get("error", 0),
        }

    def insert_ord_reg_edges(
        self, edges: list[tuple[str, str, str, str]], first_seen: str,
    ) -> None:
        """Bulk insert (parent_cap, child_cap, lang, title) tuples.
        Idempotent via INSERT OR IGNORE."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO ord_reg_edges "
            "(parent_cap, child_cap, lang, title, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            [(p, c, lang, title, first_seen)
             for p, c, lang, title in edges],
        )
        self._conn.commit()

    def upsert_noteup_fetch(
        self, court: str, year: int, number: int,
    ) -> None:
        """Insert a per-source-case row in status='pending'. Idempotent —
        existing rows retain their state (so a re-enumeration doesn't
        overwrite already-completed fetches)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO noteup_fetches (court, year, number) "
            "VALUES (?, ?, ?)",
            (court, year, number),
        )
        self._conn.commit()

    def mark_noteup_ok(
        self, court: str, year: int, number: int,
        edge_count: int, fetched_at: str,
    ) -> None:
        self._conn.execute(
            "UPDATE noteup_fetches SET status='ok', edge_count=?, "
            "fetched_at=?, error=NULL "
            "WHERE court=? AND year=? AND number=?",
            (edge_count, fetched_at, court, year, number),
        )
        self._conn.commit()

    def mark_noteup_failed(
        self, court: str, year: int, number: int, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE noteup_fetches SET status='error', error=? "
            "WHERE court=? AND year=? AND number=?",
            (error, court, year, number),
        )
        self._conn.commit()

    def claim_pending_noteup(self) -> NoteupRecord | None:
        row = self._conn.execute(
            "SELECT court, year, number FROM noteup_fetches "
            "WHERE status='pending' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE noteup_fetches SET status='in_progress' "
            "WHERE court=? AND year=? AND number=?",
            (row[0], row[1], row[2]),
        )
        self._conn.commit()
        return NoteupRecord(
            court=row[0], year=row[1], number=row[2],
            status="in_progress",
        )

    def noteup_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM noteup_fetches GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "ok": counts.get("ok", 0),
            "error": counts.get("error", 0),
        }

    def insert_citation_edges(
        self,
        edges: list[tuple[str, str, str, int | None, int]],
        first_seen: str,
    ) -> None:
        """Bulk insert (from_key, to_key, citer_lang, citer_freq, position)
        tuples. Idempotent via INSERT OR IGNORE — a re-run of the same
        source case won't duplicate rows."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO citations "
            "(from_key, to_key, citer_lang, citer_freq, position, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(f, t, lang, freq, pos, first_seen)
             for f, t, lang, freq, pos in edges],
        )
        self._conn.commit()

    def insert_parallel_cites(
        self, case_key: str, cites: list[str],
    ) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO case_parallel_cites "
            "(case_key, parallel_cite) VALUES (?, ?)",
            [(case_key, c) for c in cites],
        )
        self._conn.commit()

    def upsert_hopt_document(
        self, abbr: str, year: int, num: int, lang: str, title: str,
        neutral: str | None = None, doc_date: str | None = None,
        last_seen_at: int | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO hopt_documents "
            "(abbr, year, num, lang, title, neutral, doc_date, "
            "last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (abbr, year, num, lang) DO UPDATE SET "
            "title=excluded.title, "
            "neutral=COALESCE(excluded.neutral, hopt_documents.neutral), "
            "doc_date=COALESCE(excluded.doc_date, hopt_documents.doc_date), "
            "last_seen_at=COALESCE(excluded.last_seen_at, "
            "                       hopt_documents.last_seen_at)",
            (abbr, year, num, lang, title, neutral, doc_date, last_seen_at),
        )
        self._conn.commit()

    def claim_pending_hopt(self) -> HoptRecord | None:
        row = self._conn.execute(
            "SELECT abbr, year, num, lang, title, neutral, doc_date "
            "FROM hopt_documents WHERE status='pending' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE hopt_documents SET status='in_progress' "
            "WHERE abbr=? AND year=? AND num=? AND lang=?",
            (row[0], row[1], row[2], row[3]),
        )
        self._conn.commit()
        return HoptRecord(
            abbr=row[0], year=row[1], num=row[2], lang=row[3],
            title=row[4], neutral=row[5], doc_date=row[6],
            status="in_progress",
        )

    def mark_hopt_downloaded(
        self, abbr: str, year: int, num: int, lang: str,
        formats: list[str],
    ) -> None:
        self._conn.execute(
            "UPDATE hopt_documents SET status='downloaded', "
            "formats=?, error=NULL "
            "WHERE abbr=? AND year=? AND num=? AND lang=?",
            (json.dumps(formats), abbr, year, num, lang),
        )
        self._conn.commit()

    def mark_hopt_failed(
        self, abbr: str, year: int, num: int, lang: str, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE hopt_documents SET status='failed', error=? "
            "WHERE abbr=? AND year=? AND num=? AND lang=?",
            (error, abbr, year, num, lang),
        )
        self._conn.commit()

    def hopt_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM hopt_documents GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "downloaded": counts.get("downloaded", 0),
            "failed": counts.get("failed", 0),
        }

    def hopt_stats_by_abbr(self) -> dict:
        rows = self._conn.execute(
            "SELECT abbr, status, COUNT(*) FROM hopt_documents "
            "GROUP BY abbr, status"
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for abbr, status, n in rows:
            bucket = result.setdefault(abbr, {
                "total": 0, "pending": 0, "in_progress": 0,
                "downloaded": 0, "failed": 0,
            })
            bucket["total"] += n
            bucket[status] = bucket.get(status, 0) + n
        return result

    def upsert_legis_version(
        self, abbr: str, num: str, lang: str, vid: int,
        version_date: str, last_seen_at: int | None = None,
    ) -> None:
        """Insert-or-update a historical-version row. Never touches
        status — owned by the backfill workers."""
        self._conn.execute(
            "INSERT INTO legis_versions "
            "(abbr, num, lang, vid, version_date, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (abbr, num, lang, vid) DO UPDATE SET "
            "version_date=COALESCE(excluded.version_date, "
            "                       legis_versions.version_date), "
            "last_seen_at=COALESCE(excluded.last_seen_at, "
            "                       legis_versions.last_seen_at)",
            (abbr, num, lang, vid, version_date, last_seen_at),
        )
        self._conn.commit()

    def claim_pending_legis_version(self) -> LegisVersionRecord | None:
        row = self._conn.execute(
            "SELECT abbr, num, lang, vid, version_date "
            "FROM legis_versions WHERE status='pending' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE legis_versions SET status='in_progress' "
            "WHERE abbr=? AND num=? AND lang=? AND vid=?",
            (row[0], row[1], row[2], row[3]),
        )
        self._conn.commit()
        return LegisVersionRecord(
            abbr=row[0], num=row[1], lang=row[2], vid=row[3],
            version_date=row[4], status="in_progress",
        )

    def mark_legis_version_downloaded(
        self, abbr: str, num: str, lang: str, vid: int,
    ) -> None:
        self._conn.execute(
            "UPDATE legis_versions SET status='downloaded', error=NULL "
            "WHERE abbr=? AND num=? AND lang=? AND vid=?",
            (abbr, num, lang, vid),
        )
        self._conn.commit()

    def mark_legis_version_failed(
        self, abbr: str, num: str, lang: str, vid: int, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE legis_versions SET status='failed', error=? "
            "WHERE abbr=? AND num=? AND lang=? AND vid=?",
            (error, abbr, num, lang, vid),
        )
        self._conn.commit()

    def legis_version_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM legis_versions GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "downloaded": counts.get("downloaded", 0),
            "failed": counts.get("failed", 0),
        }

    def pending_legis_versions(self) -> list[LegisVersionRecord]:
        rows = self._conn.execute(
            "SELECT abbr, num, lang, vid, version_date "
            "FROM legis_versions WHERE status='pending'"
        ).fetchall()
        return [
            LegisVersionRecord(
                abbr=r[0], num=r[1], lang=r[2], vid=r[3],
                version_date=r[4], status="pending",
            )
            for r in rows
        ]

    def upsert_legis_document(
        self, abbr: str, num: str, lang: str, title: str,
        last_seen_at: int | None = None,
    ) -> None:
        """Insert or refresh a legislation row. Never touches status —
        that's owned by the scrape workers via claim/mark methods."""
        self._conn.execute(
            "INSERT INTO legis_documents "
            "(abbr, num, lang, title, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (abbr, num, lang) DO UPDATE SET "
            "title=excluded.title, "
            "last_seen_at=COALESCE(excluded.last_seen_at, "
            "                       legis_documents.last_seen_at)",
            (abbr, num, lang, title, last_seen_at),
        )
        self._conn.commit()

    def claim_pending_legis(
        self, abbr: str | None = None,
    ) -> LegisRecord | None:
        if abbr:
            row = self._conn.execute(
                "SELECT abbr, num, lang, title FROM legis_documents "
                "WHERE status='pending' AND abbr=? LIMIT 1",
                (abbr,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT abbr, num, lang, title FROM legis_documents "
                "WHERE status='pending' LIMIT 1"
            ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE legis_documents SET status='in_progress' "
            "WHERE abbr=? AND num=? AND lang=?",
            (row[0], row[1], row[2]),
        )
        self._conn.commit()
        return LegisRecord(
            abbr=row[0], num=row[1], lang=row[2],
            title=row[3], status="in_progress",
        )

    def mark_legis_downloaded(
        self, abbr: str, num: str, lang: str,
        latest_vid: int, latest_version_date: str, formats: list[str],
    ) -> None:
        self._conn.execute(
            "UPDATE legis_documents SET status='downloaded', "
            "formats=?, latest_vid=?, latest_version_date=?, error=NULL "
            "WHERE abbr=? AND num=? AND lang=?",
            (json.dumps(formats), latest_vid, latest_version_date,
             abbr, num, lang),
        )
        self._conn.commit()

    def mark_legis_failed(
        self, abbr: str, num: str, lang: str, error: str,
    ) -> None:
        self._conn.execute(
            "UPDATE legis_documents SET status='failed', error=? "
            "WHERE abbr=? AND num=? AND lang=?",
            (error, abbr, num, lang),
        )
        self._conn.commit()

    def legis_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM legis_documents GROUP BY status"
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "downloaded": counts.get("downloaded", 0),
            "failed": counts.get("failed", 0),
        }

    def legis_stats_by_abbr(self) -> dict:
        rows = self._conn.execute(
            "SELECT abbr, status, COUNT(*) FROM legis_documents "
            "GROUP BY abbr, status"
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for abbr, status, n in rows:
            bucket = result.setdefault(abbr, {
                "total": 0, "pending": 0, "in_progress": 0,
                "downloaded": 0, "failed": 0,
            })
            bucket["total"] += n
            bucket[status] = bucket.get(status, 0) + n
        return result

    # ---- db_freshness accessors (Phase D2) ---------------------------
    #
    # Ownership discipline: each writer touches only its own columns.
    # A drift here silently corrupts the freshness signal — e.g. a
    # probe clobbering last_scrape_completed_at back to NULL would
    # re-trigger every scrape at the next update. Enforced by the
    # tests in tests/test_freshness_checkpoint.py.

    def upsert_freshness_probe(
        self, kind: str, scope: str, lang: str, *,
        live_count: int | None,
        live_updated_at: str | None,
        live_probed_at: int,
        probe_error: str | None,
    ) -> None:
        """Record the outcome of one wire probe against the getmeta*
        endpoint for (kind, scope, lang).

        * live_count / live_updated_at are COALESCE-preserved on
          conflict — a failed probe (live_count=None) must NOT wipe a
          previous good value, otherwise a single wire flake flips
          every healthy bucket to STALE via the _fresh rule's
          `live_count IS NOT NULL` requirement.
        * live_probed_at / probe_error describe the LAST attempt and
          are always overwritten. A healthy probe after a failed one
          must clear probe_error to NULL.
        * Never touches local_count / last_scrape_completed_at /
          source_generation_id — those belong to other writers.
        """
        self._conn.execute(
            "INSERT INTO db_freshness "
            "(kind, scope, lang, live_count, live_updated_at, "
            "live_probed_at, probe_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, scope, lang) DO UPDATE SET "
            "live_count=COALESCE(excluded.live_count, "
            "                     db_freshness.live_count), "
            "live_updated_at=COALESCE(excluded.live_updated_at, "
            "                          db_freshness.live_updated_at), "
            "live_probed_at=excluded.live_probed_at, "
            "probe_error=excluded.probe_error",
            (kind, scope, lang, live_count, live_updated_at,
             live_probed_at, probe_error),
        )
        self._conn.commit()

    def recompute_local_count(
        self, kind: str, scope: str, lang: str,
    ) -> int:
        """Refresh local_count / local_counted_at for a bucket by
        running the kind-specific SELECT COUNT(*) over the
        status='downloaded' slice, and return the count so the caller
        can log it without a re-SELECT.

        Dispatch is table-driven via _FRESHNESS_TABLE_BY_KIND so a
        future kind (e.g. 'histlaw' after D3) is a single-line add.
        Wire columns and scrape-runner columns are COALESCE-preserved
        on conflict — this writer owns local_count / local_counted_at
        only.
        """
        if kind not in _FRESHNESS_KINDS:
            raise ValueError(
                f"unknown freshness kind {kind!r}; "
                f"expected one of {_FRESHNESS_KINDS}"
            )
        table, scope_col = _FRESHNESS_TABLE_BY_KIND[kind]
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {scope_col}=? AND lang=? AND status='downloaded'",
            (scope, lang),
        ).fetchone()
        count = int(row[0]) if row else 0
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO db_freshness "
            "(kind, scope, lang, local_count, local_counted_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, scope, lang) DO UPDATE SET "
            "local_count=excluded.local_count, "
            "local_counted_at=excluded.local_counted_at",
            (kind, scope, lang, count, now),
        )
        self._conn.commit()
        return count

    def mark_bucket_scraped(
        self, kind: str, scope: str, lang: str, *,
        completed_at: int,
        source_generation_id: int | None = None,
    ) -> None:
        """Record a clean scrape completion for (kind, scope, lang).

        Called by every scrape runner (BulkScraper, HoptRunner,
        LegisRunner, UkpcRunner) on successful sweep completion. If no
        db_freshness row exists yet (first-run scrape landing before
        any probe), INSERT with NULL wire columns so a later probe
        can UPSERT-refresh them.

        source_generation_id is optional — hopt/legis scrapes don't
        touch enum_runs and pass None; cases scrapes pass the
        generation_id of the enum_run whose enumeration surfaced the
        rows this scrape captured.

        Wire columns (live_*, probe_error) and local columns are
        COALESCE-preserved on conflict — this writer owns
        last_scrape_completed_at / source_generation_id only.
        """
        self._conn.execute(
            "INSERT INTO db_freshness "
            "(kind, scope, lang, last_scrape_completed_at, "
            "source_generation_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, scope, lang) DO UPDATE SET "
            "last_scrape_completed_at="
            "excluded.last_scrape_completed_at, "
            "source_generation_id=excluded.source_generation_id",
            (kind, scope, lang, completed_at, source_generation_id),
        )
        self._conn.commit()

    def get_freshness_row(
        self, kind: str, scope: str, lang: str,
    ) -> DbFreshnessRecord | None:
        """Point-read a single freshness row. Returns None if the
        triple has no row (first-run — caller treats as STALE per
        fresh_definition rule (1))."""
        row = self._conn.execute(
            "SELECT kind, scope, lang, live_count, live_updated_at, "
            "live_probed_at, probe_error, local_count, "
            "local_counted_at, last_scrape_completed_at, "
            "source_generation_id FROM db_freshness "
            "WHERE kind=? AND scope=? AND lang=?",
            (kind, scope, lang),
        ).fetchone()
        if row is None:
            return None
        return DbFreshnessRecord(*row)

    def iter_freshness_rows(self):
        """Full-scan iterator over db_freshness. Cheap in practice —
        the table stays under ~100 rows (one per mapped
        category × slug × lang triple). Consumers: FreshnessRunner.
        stale_buckets(), the check-freshness CLI JSON report."""
        for row in self._conn.execute(
            "SELECT kind, scope, lang, live_count, live_updated_at, "
            "live_probed_at, probe_error, local_count, "
            "local_counted_at, last_scrape_completed_at, "
            "source_generation_id FROM db_freshness"
        ).fetchall():
            yield DbFreshnessRecord(*row)

    def close(self) -> None:
        self._conn.close()
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = None
