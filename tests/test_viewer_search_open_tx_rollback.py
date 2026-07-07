"""Tier-4 fix regression: index_case leaves no open transaction on raise.

Scenario: within the "sources non-empty" branch, we UPSERT into
fts_cases + case_bodies inside an implicit transaction (Python sqlite3
default isolation_level). If extract_plaintext (or body_sha256) raises
mid-loop, the transaction is left open on the connection, and subsequent
calls in build_index either fail (SQLITE_BUSY when a second writer is
around) or silently roll back when the caller finally hits a non-DML
statement that triggers the module's implicit-commit path.

Fix: wrap the write section in a try/except that rolls back on error
and re-raises. build_index also catches per-case exceptions and
continues so one bad body doesn't abort a 162k-corpus build.

Trade-off: rollback undoes the current autobegin transaction — in
build_index's batching mode that's up to ``commit_every`` cases. Use
``commit_every=1`` in the batching regression to keep the semantics
crisp; the docstring in index_case documents the trade-off.

5-lens angles pinned:
  L1 silent skip:      the exception must propagate (rollback must not
                       swallow it via bare except: return)
  L2 semantic drift:   "rollback" here means "unwind the current
                       autobegin transaction" — the failed case's writes
                       to fts_cases + case_bodies vanish; downstream
                       triggers (fts_body) stay consistent
  L4 wrong-side test:  helper (index_case) AND caller (build_index)
                       both get regression coverage; a helper-only test
                       would miss the batching-continuation angle where
                       one bad body must not abort a 162k-corpus build
  L5 ambiguous state:  post-raise connection state — is it "clean and
                       ready" or "in a half-committed transaction"?
                       We pin the former via ``in_transaction is False``
                       and a follow-up successful index_case call
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hklii_downloader.viewer.schema import create_schema
from hklii_downloader.viewer.search import build_index, index_case


_CP_CASES_MINIMAL_DDL = """
CREATE TABLE cases (
    court   TEXT NOT NULL,
    year    INTEGER NOT NULL,
    number  INTEGER NOT NULL,
    neutral TEXT NOT NULL,
    title   TEXT NOT NULL,
    date    TEXT NOT NULL,
    lang    TEXT NOT NULL DEFAULT 'en',
    PRIMARY KEY (court, year, number)
);
"""


_FIXED_NOW = "2026-07-07T12:00:00Z"


def _mk_cp(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "checkpoint.db"))
    conn.execute(_CP_CASES_MINIMAL_DDL)
    conn.commit()
    return conn


def _mk_vw(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "viewer.db"))
    create_schema(conn)
    return conn


def _seed_case(cp: sqlite3.Connection, case_key: str, *, lang: str = "en") -> None:
    court, year, num = case_key.split("/")
    cp.execute(
        "INSERT INTO cases (court, year, number, neutral, title, date, lang) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            court,
            int(year),
            int(num),
            f"[{year}] TEST {num}",
            "HKSAR v Test",
            "2020-01-01",
            lang,
        ],
    )
    cp.commit()


def _touch(path: Path, content: str = "<html></html>") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mk_paths(root: Path, case_key: str) -> dict[str, Path]:
    court, year, num = case_key.split("/")
    stem = f"{court}_{year}_{num}"
    d = root / court / year
    return {
        "html": d / f"{stem}.html",
        "tc.html": d / f"{stem}.tc.html",
    }


def _seed_bilingual(cp: sqlite3.Connection, tmp_path: Path, case_key: str) -> dict[str, Path]:
    """Bilingual case + both body files on disk; index_case sees two sources
    (TC first from .tc.html, EN second from .html)."""
    _seed_case(cp, case_key, lang="en")
    paths = _mk_paths(tmp_path, case_key)
    _touch(paths["html"], "<p>english body text</p>")
    _touch(paths["tc.html"], "<p>中文譯本判決書內文</p>")
    return paths


# ---------------------------------------------------------------------------
# L1 silent-skip: mid-loop raise MUST propagate to the caller
# ---------------------------------------------------------------------------


def test_index_case_propagates_extract_plaintext_raise_mid_loop(
    tmp_path: Path,
) -> None:
    """Bilingual case, second extract_plaintext call raises → index_case
    re-raises the same exception (does not silently swallow it).
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_bilingual(cp, tmp_path, "hkcfa/2020/32")

    with patch(
        "hklii_downloader.viewer.search.extract_plaintext"
    ) as mock_extract:
        # First call returns; second call raises. discover_body_sources
        # yields sources in order [TC, EN], so the raise fires on EN.
        mock_extract.side_effect = [
            "first source plaintext",
            RuntimeError("simulated body-parse failure"),
        ]
        with pytest.raises(RuntimeError, match="simulated body-parse failure"):
            index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)

    cp.close()
    vw.close()


# ---------------------------------------------------------------------------
# L2 semantic drift: rollback UNDOES the half-written UPSERT
# ---------------------------------------------------------------------------


def test_index_case_rollback_leaves_db_in_pre_call_state_after_raise(
    tmp_path: Path,
) -> None:
    """DB state assertion: after the raise, no fts_cases or case_bodies
    rows exist for this case_key. Guards against a rollback that no-ops
    when it should undo the first source's UPSERT.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_bilingual(cp, tmp_path, "hkcfa/2020/32")

    # Pre-call state — nothing indexed yet.
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (0,)
    assert vw.execute(
        "SELECT COUNT(*) FROM case_bodies WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (0,)

    with patch(
        "hklii_downloader.viewer.search.extract_plaintext"
    ) as mock_extract:
        mock_extract.side_effect = [
            "first source plaintext",
            RuntimeError("simulated body-parse failure"),
        ]
        with pytest.raises(RuntimeError):
            index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)

    # Post-call state — matches pre-call. The TC UPSERT that happened
    # BEFORE the EN raise must have been rolled back.
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (0,)
    assert vw.execute(
        "SELECT COUNT(*) FROM case_bodies WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (0,)
    # fts_body is populated via case_bodies triggers — must also be clean.
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["first"],
    ).fetchone() == (0,)

    cp.close()
    vw.close()


# ---------------------------------------------------------------------------
# L5 ambiguous state: connection is USABLE on next call (no lingering txn)
# ---------------------------------------------------------------------------


def test_index_case_connection_usable_after_raise(tmp_path: Path) -> None:
    """After the rollback+re-raise, the connection has no open
    transaction (conn.in_transaction is False). A follow-up index_case
    call on a different case succeeds and its writes land normally.

    Without the rollback, the autobegin transaction from the failed
    call would still hold the write lock — subsequent calls either
    hit SQLITE_BUSY (multi-writer) or accumulate into the stale
    transaction and land on disk with the failed case's partial writes.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_bilingual(cp, tmp_path, "hkcfa/2020/32")
    # Independent second case for the follow-up call.
    _seed_case(cp, "hkcfa/2020/99", lang="en")
    p2 = _mk_paths(tmp_path, "hkcfa/2020/99")
    _touch(p2["html"], "<p>independent case body uniqueok</p>")

    with patch(
        "hklii_downloader.viewer.search.extract_plaintext"
    ) as mock_extract:
        mock_extract.side_effect = [
            "first source plaintext",
            RuntimeError("boom"),
        ]
        with pytest.raises(RuntimeError):
            index_case(vw, cp, tmp_path, "hkcfa/2020/32", now_iso=_FIXED_NOW)

    # L5 pin: connection is not sitting inside a half-committed txn.
    assert vw.in_transaction is False, (
        "post-raise connection still has an open transaction — subsequent "
        "writers will either hit SQLITE_BUSY or commit stale partial writes"
    )

    # Follow-up call — must succeed and write normally.
    r = index_case(vw, cp, tmp_path, "hkcfa/2020/99", now_iso=_FIXED_NOW)
    assert r.action == "indexed"
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/99"],
    ).fetchone() == (1,)
    hits = vw.execute(
        "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
        ["uniqueok"],
    ).fetchone()
    assert hits == (1,)
    # Failed case remains absent — the follow-up did not silently
    # commit the earlier half-written state.
    assert vw.execute(
        "SELECT COUNT(*) FROM fts_cases WHERE case_key = ?",
        ["hkcfa/2020/32"],
    ).fetchone() == (0,)

    cp.close()
    vw.close()


# ---------------------------------------------------------------------------
# L4 wrong-side test: build_index continues past a failed case
# ---------------------------------------------------------------------------


def test_build_index_continues_past_failed_case(tmp_path: Path) -> None:
    """N cases where one raises mid-batch → build_index catches the
    per-case exception, counts it, and continues. Cases before AND after
    the failing one end up indexed.

    Uses commit_every=1 so each successful case is durably committed
    before the failing one runs — otherwise plain rollback would drop
    the entire batch. The failed case's partial writes are still rolled
    back by index_case's own guard (L1 test above), so build_index just
    needs to recover from the raise and move on.
    """
    cp = _mk_cp(tmp_path)
    vw = _mk_vw(tmp_path)
    _seed_case(cp, "hkcfa/2020/1", lang="en")
    _seed_case(cp, "hkcfa/2020/2", lang="en")
    # case 3 is bilingual — its second extract will raise.
    _seed_bilingual(cp, tmp_path, "hkcfa/2020/3")
    _seed_case(cp, "hkcfa/2020/4", lang="en")
    _seed_case(cp, "hkcfa/2020/5", lang="en")

    for k in (1, 2, 4, 5):
        p = _mk_paths(tmp_path, f"hkcfa/2020/{k}")
        _touch(p["html"], f"<p>body-{k} unique{k}</p>")

    # Real extract_plaintext for non-bilingual (single-source) cases, then
    # raise on the SECOND call for the bilingual case (which is case 3).
    from hklii_downloader.viewer.search import (
        extract_plaintext as real_extract,
    )

    calls: dict[str, int] = {"bilingual": 0}

    def side_effect(html_bytes: bytes | str) -> str:
        # Bilingual case's TC body contains 中文譯本 marker; use that to
        # distinguish it from the plain-EN cases.
        as_str = (
            html_bytes.decode("utf-8", errors="ignore")
            if isinstance(html_bytes, bytes)
            else html_bytes
        )
        if "中文" in as_str or "english body text" in as_str:
            calls["bilingual"] += 1
            if calls["bilingual"] == 2:
                # Second bilingual source (the EN half) raises.
                raise RuntimeError("simulated bilingual body-parse failure")
        return real_extract(html_bytes)

    with patch(
        "hklii_downloader.viewer.search.extract_plaintext",
        side_effect=side_effect,
    ):
        result = build_index(
            vw, cp, tmp_path, commit_every=1, now_iso=_FIXED_NOW,
        )

    # Contract: 5 cases visited; 4 indexed cleanly; 1 failed (case 3).
    assert result.processed == 5
    assert result.indexed == 4
    assert result.failed == 1

    # Cases 1, 2, 4, 5 are in the DB; case 3 (which raised) is not.
    indexed_keys = {
        r[0]
        for r in vw.execute("SELECT case_key FROM fts_cases")
    }
    assert indexed_keys == {
        "hkcfa/2020/1",
        "hkcfa/2020/2",
        "hkcfa/2020/4",
        "hkcfa/2020/5",
    }, f"unexpected fts_cases contents: {indexed_keys}"

    # FTS body content for successful cases is searchable; failed case
    # marker is not (it was rolled back).
    for k in (1, 2, 4, 5):
        hits = vw.execute(
            "SELECT COUNT(*) FROM fts_body WHERE fts_body MATCH ?",
            [f"unique{k}"],
        ).fetchone()
        assert hits == (1,), f"case {k} not found in fts_body: hits={hits}"

    cp.close()
    vw.close()
