from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class CaseRecord:
    court: str
    year: int
    number: int
    neutral: str
    title: str
    date: str
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
    summary_en_status     TEXT NOT NULL DEFAULT 'pending',
    summary_en_error      TEXT,
    summary_zh_status     TEXT NOT NULL DEFAULT 'pending',
    summary_zh_error      TEXT,
    appeal_history_status TEXT NOT NULL DEFAULT 'pending',
    appeal_history_error  TEXT,
    PRIMARY KEY (court, year, number)
);
"""

_ENRICHMENT_KINDS = ("summary_en", "summary_zh", "appeal_history")
_ENRICHMENT_STATUSES = ("pending", "downloaded", "na", "failed")


class CheckpointDB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_SCHEMA)
        self._migrate_enrichment_columns()
        self._conn.commit()

    def _migrate_enrichment_columns(self) -> None:
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(cases)").fetchall()
        }
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

    def upsert_case(
        self, court: str, year: int, number: int,
        neutral: str, title: str, date: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO cases (court, year, number, neutral, title, date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (court, year, number) DO UPDATE SET "
            "neutral=excluded.neutral, title=excluded.title, date=excluded.date",
            (court, year, number, neutral, title, date),
        )
        self._conn.commit()

    def claim_pending(self, court: str | None = None) -> CaseRecord | None:
        if court:
            row = self._conn.execute(
                "SELECT court, year, number, neutral, title, date "
                "FROM cases WHERE status='pending' AND court=? LIMIT 1",
                (court,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT court, year, number, neutral, title, date "
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
            status="in_progress",
        )

    def mark_downloaded(
        self, court: str, year: int, number: int, formats: list[str],
    ) -> None:
        self._conn.execute(
            "UPDATE cases SET status='downloaded', formats=? "
            "WHERE court=? AND year=? AND number=?",
            (json.dumps(formats), court, year, number),
        )
        self._conn.commit()

    def mark_failed(self, court: str, year: int, number: int, error: str) -> None:
        self._conn.execute(
            "UPDATE cases SET status='failed', error=? "
            "WHERE court=? AND year=? AND number=?",
            (error, court, year, number),
        )
        self._conn.commit()

    def release_in_progress(self) -> None:
        self._conn.execute(
            "UPDATE cases SET status='pending' WHERE status='in_progress'",
        )
        self._conn.commit()

    def pending_cases(self, courts: list[str] | None = None) -> list[CaseRecord]:
        if courts:
            placeholders = ",".join("?" * len(courts))
            rows = self._conn.execute(
                f"SELECT court, year, number, neutral, title, date "
                f"FROM cases WHERE status='pending' AND court IN ({placeholders})",
                courts,
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT court, year, number, neutral, title, date "
                "FROM cases WHERE status='pending'",
            ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="pending",
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
            f"SELECT court, year, number, neutral, title, date "
            f"FROM cases WHERE {where}",
            params,
        ).fetchall()
        return [
            CaseRecord(
                court=r[0], year=r[1], number=r[2],
                neutral=r[3], title=r[4], date=r[5],
                status="downloaded",
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

    def close(self) -> None:
        self._conn.close()
