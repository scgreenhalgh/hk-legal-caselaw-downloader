"""Freshness sanity check — replays the 2026-07-08 walkthrough.

Reads ``output/.checkpoint.db``, prints per-bucket freshness state
with human-readable reasons, simulates the post-scrape scoping the
update dispatcher would apply, and reverts every write on exit so
this is effectively a read-only diagnostic.

See ``docs/freshness-sanity-check.md`` for what to look for in the
output. Run from repo root:

    uv run python scripts/freshness_sanity_check.py
    uv run python scripts/freshness_sanity_check.py --output ./other-output

Exit codes:

    0 — script ran to completion (no assertion of state health).
    1 — checkpoint DB not found or unreadable.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import from the installed package so the script tracks any
# subsequent renames without going stale.
from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.freshness import _fresh

HKT = timezone(timedelta(hours=8))

# Dispatcher-side scope lists — must match the hardcoded tuples in
# cli._dispatch_update_plan. Change here + there in one commit.
ALL_COURTS = (
    "hkcfa", "hkca", "hkcfi", "hkdc", "hkldt", "hkfc",
    "hkmagc", "hkct", "hkcrc", "hklat", "hkoat", "hksct",
)
HOPT_ABBRS = ("bacpg", "bahkg", "hktmc", "hktml", "hkts")
LEGIS_CAP_TYPES = ("ord", "reg", "instrument")
NEW_D3_SLUGS = (
    "histlaw", "hkiac", "hklrccp", "hklrcr",
    "pcpdaab", "pcpdc", "pd",
)


def stale_reason(rec) -> str | None:
    """Return a short human tag for why a row would fail :func:`_fresh`,
    or None if the row IS fresh."""
    if rec is None:
        return "no-row"
    if rec.probe_error:
        return f"probe-err:{rec.probe_error[:40]}"
    if rec.live_count is None:
        return "no-live-count"
    if rec.local_count is None:
        return "no-local-count"
    if rec.last_scrape_completed_at is None:
        return "never-scraped"
    if rec.live_count != rec.local_count:
        return f"mismatch(live={rec.live_count},local={rec.local_count})"
    if rec.live_updated_at:
        scrape_date = (
            datetime.fromtimestamp(rec.last_scrape_completed_at, HKT)
            .date().isoformat()
        )
        if rec.live_updated_at > scrape_date:
            return f"upstream-newer({rec.live_updated_at}>{scrape_date})"
    return None


def print_current_state(db: CheckpointDB) -> None:
    print("=" * 72)
    print("Section 1 — current freshness ledger state")
    print("=" * 72)
    rows = db._conn.execute(
        "SELECT kind, scope, lang FROM db_freshness "
        "ORDER BY kind, scope, lang"
    ).fetchall()
    if not rows:
        print("  db_freshness is empty. Run `hklii check-freshness` first.")
        return

    fresh, stale = 0, 0
    for kind, scope, lang in rows:
        rec = db.get_freshness_row(kind, scope, lang)
        reason = stale_reason(rec)
        if reason is None:
            fresh += 1
        else:
            stale += 1
            print(f"  STALE  {kind}/{scope}/{lang:2s}  {reason}")
    print(f"\n  totals: {fresh} FRESH, {stale} STALE")


def scan(
    db: CheckpointDB, kind: str, scope_list, langs: tuple[str, ...],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Return (dropped_slugs, kept_slugs_with_stale_reasons).

    Mirrors the dispatcher's filter-helper logic: drop a slug only if
    EVERY lang is fresh."""
    dropped: list[str] = []
    kept: list[tuple[str, list[str]]] = []
    for scope in scope_list:
        stale_langs: list[str] = []
        for lang in langs:
            rec = db.get_freshness_row(kind, scope, lang)
            reason = stale_reason(rec)
            if reason is not None:
                stale_langs.append(f"{lang}={reason}")
        if not stale_langs:
            dropped.append(scope)
        else:
            kept.append((scope, stale_langs))
    return dropped, kept


def print_dispatcher_scoping(db: CheckpointDB) -> None:
    print()
    print("=" * 72)
    print("Section 2 — dispatcher scoping simulation")
    print("(what each step would target given the current ledger)")
    print("=" * 72)

    print("\n--- scrape (case-family) ---")
    dropped, kept = scan(db, "cases", ALL_COURTS, ("en", "tc"))
    if dropped:
        print(f"  DROPPED (fresh): {dropped}")
    if kept:
        print(f"  KEPT ({len(kept)}):")
        for slug, reasons in kept:
            print(f"    {slug}: {reasons}")
    if not dropped and not kept:
        print("  (empty)")

    print("\n--- scrape_hopt (weekly+) ---")
    dropped, kept = scan(db, "hopt", HOPT_ABBRS, ("en", "tc"))
    if dropped:
        print(f"  DROPPED (fresh): {dropped}")
    if kept:
        for slug, reasons in kept:
            print(f"  KEPT  {slug}: {reasons}")

    print("\n--- scrape_ukpc (weekly+) ---")
    # Dispatcher passes only ('en',) — matches /databases: UKPC is
    # EN-only.
    dropped, kept = scan(db, "cases", ("ukpc",), ("en",))
    print(f"  {'DROPPED' if dropped else 'KEPT'}: ", end="")
    if dropped:
        print(dropped)
    else:
        print(kept)

    print("\n--- scrape_legis (weekly+) ---")
    dropped, kept = scan(db, "legis", LEGIS_CAP_TYPES, ("en", "tc"))
    if dropped:
        print(f"  DROPPED (fresh): {dropped}")
    if kept:
        for slug, reasons in kept:
            print(f"  KEPT  {slug}: {reasons}")


def print_d3_backlog(db: CheckpointDB) -> None:
    print()
    print("=" * 72)
    print("Section 3 — D3 backlog (mapped in freshness, no scrape runner)")
    print("=" * 72)
    for slug in NEW_D3_SLUGS:
        rec_en = db.get_freshness_row("hopt", slug, "en")
        rec_tc = db.get_freshness_row("hopt", slug, "tc")
        en_live = rec_en.live_count if rec_en else None
        tc_live = rec_tc.live_count if rec_tc else None
        en_local = rec_en.local_count if rec_en else None
        tc_local = rec_tc.local_count if rec_tc else None
        print(
            f"  {slug:9s}  en: local={en_local}/live={en_live}  "
            f"tc: local={tc_local}/live={tc_live}"
        )


def simulate_post_scrape(db: CheckpointDB) -> list[tuple[str, str, str]]:
    """Mark every ``live == local`` bucket scraped today. Returns the
    list of buckets touched so the caller can revert."""
    from datetime import datetime as _dt
    today_ts = int(_dt.now(HKT).timestamp())
    touched: list[tuple[str, str, str]] = []
    rows = db._conn.execute(
        "SELECT kind, scope, lang FROM db_freshness "
        "WHERE live_count IS NOT NULL AND local_count IS NOT NULL "
        "  AND live_count = local_count"
    ).fetchall()
    for kind, scope, lang in rows:
        # Preserve original if any.
        orig = db._conn.execute(
            "SELECT last_scrape_completed_at FROM db_freshness "
            "WHERE kind=? AND scope=? AND lang=?",
            (kind, scope, lang),
        ).fetchone()
        touched.append((kind, scope, lang))
        # Use mark_bucket_scraped to exercise the real writer.
        db.mark_bucket_scraped(kind, scope, lang, completed_at=today_ts)
    return touched


def revert_simulation(
    db: CheckpointDB, orig_state: dict[tuple[str, str, str], int | None],
) -> None:
    for (kind, scope, lang), ts in orig_state.items():
        db._conn.execute(
            "UPDATE db_freshness SET last_scrape_completed_at=? "
            "WHERE kind=? AND scope=? AND lang=?",
            (ts, kind, scope, lang),
        )
    db._conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("./output"),
        help="Corpus root containing .checkpoint.db",
    )
    args = parser.parse_args()

    db_path = args.output / ".checkpoint.db"
    if not db_path.exists():
        print(f"ERROR: no checkpoint DB at {db_path}", file=sys.stderr)
        return 1

    db = CheckpointDB(str(db_path))

    # Snapshot for revert.
    orig_state = {
        (r[0], r[1], r[2]): r[3]
        for r in db._conn.execute(
            "SELECT kind, scope, lang, last_scrape_completed_at "
            "FROM db_freshness"
        ).fetchall()
    }

    try:
        print_current_state(db)
        print_d3_backlog(db)
        # Simulate post-scrape to show what the dispatcher would do
        # once buckets go green.
        simulate_post_scrape(db)
        print_dispatcher_scoping(db)
    finally:
        revert_simulation(db, orig_state)
        db.close()
        print("\n[reverted last_scrape_completed_at to pre-simulation values]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
