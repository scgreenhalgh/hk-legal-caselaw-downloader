from __future__ import annotations

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


class CheckpointDB:
    def __init__(self, path: str):
        pass

    def upsert_case(self, court, year, number, neutral, title, date):
        pass

    def claim_pending(self, court=None):
        return None

    def mark_downloaded(self, court, year, number, formats):
        pass

    def mark_failed(self, court, year, number, error):
        pass

    def release_in_progress(self):
        pass

    def pending_cases(self, courts=None):
        return []

    def stats(self):
        return {"total": 0, "pending": 0, "in_progress": 0, "downloaded": 0, "failed": 0}

    def close(self):
        pass
