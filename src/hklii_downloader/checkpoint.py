from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
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
class HoptRecord:
    abbr: str      # bacpg | bahkg | hktmc | hktml | hkts
    year: int
    num: int
    lang: str      # en | tc
    title: str | None
    neutral: str | None
    doc_date: str | None
    status: str


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
"""

_ENRICHMENT_KINDS = ("summary_en", "summary_zh", "appeal_history")
_ENRICHMENT_STATUSES = ("pending", "downloaded", "na", "failed")


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
        self._conn.commit()

    def _check_integrity(self, path: str) -> None:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] != "ok":
            self._conn.close()
            raise CheckpointCorruptError(
                f"integrity_check failed for {path}: {row[0]}"
            )

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

    def pending_html_recheck(self, limit: int | None = None) -> list[CaseRecord]:
        """Rows previously captured via doc-fallback whose HTML may now
        be available at HKLII. status must be 'downloaded' — this is a
        deliberate follow-up pass, not a first-time download."""
        q = (
            "SELECT court, year, number, neutral, title, date, lang "
            "FROM cases WHERE status='downloaded' "
            "AND html_pending_at_hklii IS NOT NULL "
            "ORDER BY html_pending_at_hklii ASC"
        )
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        rows = self._conn.execute(q).fetchall()
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
        enumerated or all rows have NULL last_seen_at."""
        row = self._conn.execute(
            "SELECT MAX(last_seen_at) FROM cases "
            "WHERE court=? AND lang=?",
            (court, lang),
        ).fetchone()
        return row[0] if row else None

    def find_orphans(self, as_of_ts: int) -> list[CaseRecord]:
        """Rows whose last_seen_at is NULL or < as_of_ts — candidates for
        removal from HKLII since our last enumeration."""
        rows = self._conn.execute(
            "SELECT court, year, number, neutral, title, date, lang, status "
            "FROM cases WHERE last_seen_at IS NULL OR last_seen_at < ?",
            (as_of_ts,),
        ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status=r[7], lang=r[6],
            )
            for r in rows
        ]

    def verify_downloaded_against_files(self, output_dir) -> int:
        """Scan status='downloaded' rows; flip any whose expected files are
        missing or 0-byte back to status='pending'. Returns broken count."""
        from pathlib import Path
        output_dir = Path(output_dir)
        rows = self._conn.execute(
            "SELECT court, year, number, formats FROM cases WHERE status='downloaded'"
        ).fetchall()
        broken = 0
        for court, year, number, formats_json in rows:
            formats = json.loads(formats_json) if formats_json else []
            stem = f"{court}_{year}_{number}"
            case_dir = output_dir / court / str(year)
            for fmt in formats:
                ext = "docx" if fmt == "doc" and (case_dir / f"{stem}.docx").exists() else fmt
                path = case_dir / f"{stem}.{ext}"
                if not path.exists() or path.stat().st_size == 0:
                    self._conn.execute(
                        "UPDATE cases SET status='pending', formats=NULL "
                        "WHERE court=? AND year=? AND number=?",
                        (court, year, number),
                    )
                    broken += 1
                    break
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

    def upsert_relatedcap_fetch(self, cap_number, abbr, lang) -> None:
        raise NotImplementedError

    def mark_relatedcap_ok(self, cap_number, abbr, lang, edge_count,
                             fetched_at) -> None:
        raise NotImplementedError

    def mark_relatedcap_failed(self, cap_number, abbr, lang, error) -> None:
        raise NotImplementedError

    def claim_pending_relatedcap(self):
        raise NotImplementedError

    def relatedcap_stats(self) -> dict:
        raise NotImplementedError

    def insert_ord_reg_edges(self, edges, first_seen) -> None:
        raise NotImplementedError

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

    def close(self) -> None:
        self._conn.close()
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = None
