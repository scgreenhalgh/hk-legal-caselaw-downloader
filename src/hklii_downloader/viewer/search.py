"""Search index build helpers over the on-disk corpus + viewer.db.

Owns:
- BodySource dataclass: one entry per (case, language) on disk
- discover_body_sources: bilingual sibling probe
- (Phase 2.5+) extract_plaintext, body_sha256, upsert_case, rebuild_index

See docs/viewer-design.md §4.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hklii_downloader.viewer.body_render.text import iter_text_nodes


#: Any run of whitespace (including newlines) collapses to a single space
#: in the extracted plaintext. Matches source-format churn that would
#: otherwise perturb body_sha256.
_WHITESPACE_RUN = re.compile(r"\s+")


def extract_plaintext(html_content: str | bytes) -> str:
    """Extract normalized plaintext for FTS indexing.

    Uses iter_text_nodes with DEFAULT_SKIP_TAGS (a/code/pre) plus the
    always-skip infrastructure set (script/style/head/…). Concatenates
    yielded text nodes with a single space, then collapses whitespace
    runs and strips the ends.

    Empty body → empty string. Callers may skip writing an FTS row for
    an empty body, or write a row whose body_sha256 is the empty-string
    sentinel — either is valid; the point is that the two states are
    distinguishable.
    """
    nodes = list(iter_text_nodes(html_content))
    if not nodes:
        return ""
    joined = " ".join(nodes)
    return _WHITESPACE_RUN.sub(" ", joined).strip()


def body_sha256(plaintext: str) -> str:
    """Return the SHA-256 hex digest of ``plaintext`` (utf-8 encoded).

    Basis of the incremental-index diff: a case's body_sha256 unchanged
    since the last index run means we can skip the reindex.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# index_case — single-case orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexResult:
    """Summary of what index_case did for one case.

    action codes:
      - 'no_case_row': case_key absent from cp.cases AND viewer.db has
        no stale rows for it (nothing to do)
      - 'no_body_on_disk': cp.cases row exists but no body files, AND
        viewer.db had no stale rows (nothing to do)
      - 'indexed': at least one write happened — insert, update, OR
        delete. langs_indexed / langs_unchanged / langs_pruned tell
        which per-language events occurred.
      - 'unchanged': existing rows matched, no writes.

    langs_pruned enumerates languages whose stored row was DELETEd —
    happens when the corresponding body file disappears between runs
    (upstream retract, scraper cleanup, manual delete), or when the
    case_key is removed from cp.cases entirely.
    """

    case_key: str
    action: str
    langs_indexed: tuple[str, ...] = field(default_factory=tuple)
    langs_unchanged: tuple[str, ...] = field(default_factory=tuple)
    langs_pruned: tuple[str, ...] = field(default_factory=tuple)


def _default_now_iso() -> str:
    """UTC ISO-8601 with second precision + trailing 'Z' (matches the
    format the downloader uses on first_seen etc)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _prune_case_key_except(
    vw_conn: sqlite3.Connection,
    case_key: str,
    keep_langs: set[str],
) -> list[str]:
    """Delete rows for ``case_key`` whose lang is NOT in ``keep_langs``.

    Reads case_bodies (authoritative — its DELETE trigger cleans up
    fts_body), computes the diff, and issues one DELETE against each of
    fts_cases + case_bodies. Returns the sorted list of pruned langs.

    ``keep_langs`` = empty set means 'prune everything for this case_key'.
    """
    existing = [
        r[0]
        for r in vw_conn.execute(
            "SELECT lang FROM case_bodies WHERE case_key = ?", [case_key]
        )
    ]
    to_prune = sorted(l for l in existing if l not in keep_langs)
    if not to_prune:
        return []
    placeholders = ",".join(["?"] * len(to_prune))
    vw_conn.execute(
        f"DELETE FROM fts_cases WHERE case_key = ? AND lang IN ({placeholders})",
        [case_key, *to_prune],
    )
    vw_conn.execute(
        f"DELETE FROM case_bodies WHERE case_key = ? AND lang IN ({placeholders})",
        [case_key, *to_prune],
    )
    return to_prune


def _fetch_case_row(
    cp_conn: sqlite3.Connection, case_key: str
) -> dict | None:
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year_s, num_s = parts[0], parts[1], parts[2]
    row = cp_conn.execute(
        "SELECT court, year, number, neutral, title, date, lang "
        "FROM cases WHERE court = ? AND year = ? AND number = ?",
        [court, int(year_s), int(num_s)],
    ).fetchone()
    if row is None:
        return None
    return {
        "court": row[0],
        "year": row[1],
        "number": row[2],
        "neutral": row[3],
        "title": row[4],
        "date": row[5],
        "lang": row[6],
    }


def index_case(
    vw_conn: sqlite3.Connection,
    cp_conn: sqlite3.Connection,
    output_root: str | Path,
    case_key: str,
    *,
    now_iso: str | None = None,
) -> IndexResult:
    """Index one case into viewer.db. Writes are one of insert/update/delete.

    Reads case metadata from ``cp_conn`` (checkpoint.db, expected columns:
    court/year/number/neutral/title/date/lang). Discovers on-disk bodies
    via :func:`discover_body_sources`.

    Branches:
      1. ``case_row`` missing (case removed from cp.cases) → prune every
         (case_key, lang) row from viewer.db. Result carries langs_pruned;
         action='indexed' if anything was pruned, else 'no_case_row'.
      2. ``case_row`` present, ``sources`` empty → NO-OP. We cannot
         distinguish 'all bodies deleted upstream' from 'output_root
         is wrong' (see the meta-lens comment inline). Default to safety;
         action='no_body_on_disk'.
      3. ``case_row`` present, ``sources`` non-empty → prune any prior
         (case_key, lang) whose lang isn't on disk any more, then per
         source: extract plaintext, compute sha, compare to stored;
         if unchanged skip, else UPSERT fts_cases + case_bodies (fts_body
         sync happens via the schema's AFTER-INSERT / AFTER-UPDATE
         triggers on case_bodies). Result carries langs_indexed,
         langs_unchanged, and langs_pruned as needed; action='indexed'
         if any write, else 'unchanged'.

    ``now_iso`` overrides the timestamp for testing; production omits it
    and gets UTC now. UPSERT is used (not INSERT OR REPLACE) because
    SQLite suppresses AFTER-DELETE triggers on REPLACE-conflict unless
    PRAGMA recursive_triggers=1 (per-connection, easy to forget).
    """
    now = now_iso if now_iso is not None else _default_now_iso()
    case_row = _fetch_case_row(cp_conn, case_key)

    # Case_key gone from cp.cases → prune EVERY viewer.db row for it.
    # Without this, an orphan FTS row keeps matching search queries and
    # its body_source points at a file that will 404 in Phase 3.
    if case_row is None:
        pruned = _prune_case_key_except(vw_conn, case_key, keep_langs=set())
        vw_conn.commit()
        if pruned:
            return IndexResult(
                case_key=case_key,
                action="indexed",
                langs_pruned=tuple(pruned),
            )
        return IndexResult(case_key=case_key, action="no_case_row")

    sources = discover_body_sources(output_root, case_key, case_row["lang"])

    if not sources:
        # META-LENS guard: sources=[] AND case_row exists could mean
        # 'every body genuinely deleted' OR 'wrong output_root — we're
        # pointing at a directory with nothing indexable'. We CANNOT
        # distinguish the two from here, so default to safety: no prune.
        # Cost of missing a legit all-bodies-deleted case: a stale row
        # that survives until the next --rebuild; recoverable.
        # Cost of pruning here: mass FTS destruction on a typo'd -o;
        # not recoverable without a full re-index run. Choose caution.
        return IndexResult(case_key=case_key, action="no_body_on_disk")

    # sources non-empty — we have physical evidence the root is legit,
    # so it's safe to prune (case_key, lang) rows whose lang isn't on
    # disk. Runs BEFORE the upsert loop so the existing-sha check can't
    # mistakenly report 'unchanged' for a case whose stale companion row
    # we're about to delete.
    current_langs = {s.lang for s in sources}
    pruned = _prune_case_key_except(vw_conn, case_key, current_langs)

    indexed: list[str] = []
    unchanged: list[str] = []
    for source in sources:
        html_bytes = source.path.read_bytes()
        plaintext = extract_plaintext(html_bytes)
        sha = body_sha256(plaintext)

        existing = vw_conn.execute(
            "SELECT body_sha256, body_source FROM fts_cases "
            "WHERE case_key = ? AND lang = ?",
            [case_key, source.lang],
        ).fetchone()
        # Tier-3 fix: skip only when BOTH sha and source_kind match. A
        # rename like .html → .generated.html preserves plaintext (same
        # sha) but changes body_source. Sha-only skip would leave the
        # stored body_source pointing at the old kind.
        if (
            existing is not None
            and existing[0] == sha
            and existing[1] == source.source_kind
        ):
            unchanged.append(source.lang)
            continue

        # UPSERT — INSERT OR REPLACE would delete-then-reinsert case_bodies,
        # but SQLite suppresses DELETE-triggers on REPLACE unless
        # PRAGMA recursive_triggers=1 (per-connection, easy to forget).
        # ON CONFLICT DO UPDATE fires the AFTER UPDATE trigger which keeps
        # the row's id stable and correctly resyncs fts_body — proven in
        # the schema tests + a manual trace.
        vw_conn.execute(
            "INSERT INTO fts_cases "
            "(case_key, lang, court, year, number, neutral, title, date, "
            " body_source, body_sha256, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (case_key, lang) DO UPDATE SET "
            " court = excluded.court, year = excluded.year, "
            " number = excluded.number, neutral = excluded.neutral, "
            " title = excluded.title, date = excluded.date, "
            " body_source = excluded.body_source, "
            " body_sha256 = excluded.body_sha256, "
            " indexed_at = excluded.indexed_at",
            [
                case_key,
                source.lang,
                case_row["court"],
                case_row["year"],
                case_row["number"],
                case_row["neutral"],
                case_row["title"],
                case_row["date"],
                source.source_kind,
                sha,
                now,
            ],
        )
        vw_conn.execute(
            "INSERT INTO case_bodies "
            "(case_key, lang, title, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (case_key, lang) DO UPDATE SET "
            " title = excluded.title, body = excluded.body",
            [case_key, source.lang, case_row["title"], plaintext],
        )
        indexed.append(source.lang)

    vw_conn.commit()
    if indexed or pruned:
        return IndexResult(
            case_key=case_key,
            action="indexed",
            langs_indexed=tuple(indexed),
            langs_unchanged=tuple(unchanged),
            langs_pruned=tuple(pruned),
        )
    return IndexResult(
        case_key=case_key,
        action="unchanged",
        langs_unchanged=tuple(unchanged),
    )


# ---------------------------------------------------------------------------
# build_index — iterate cp.cases and index_case each
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildIndexResult:
    """Action counters across the iteration.

    - processed: cases visited (matches the cp.cases WHERE-court filter)
    - indexed:   cases where at least one write happened (insert, update,
                 OR delete). Superset of `pruned`.
    - unchanged: cases where every stored lang's sha matched — no writes.
    - no_body:   cases whose cp.cases row exists but no body files on
                 disk under output_root — no writes. Distinct from
                 wrong-root scenarios which also produce no_body (that's
                 an intentional safety default, see index_case docs).
    - pruned:    subset of `indexed` — cases whose langs_pruned tuple was
                 non-empty. Lets the operator distinguish 'N fresh bodies
                 indexed' from 'N stale rows removed' (L5 disambiguation).

    Sum of indexed + unchanged + no_body may be < processed if any case
    hit an unexpected action code — defensive for the 'no_case_row'
    branch, which shouldn't fire when iterating cp.cases.
    """

    processed: int
    indexed: int
    unchanged: int
    no_body: int
    pruned: int = 0


def build_index(
    vw_conn: sqlite3.Connection,
    cp_conn: sqlite3.Connection,
    output_root: str | Path,
    *,
    courts: list[str] | None = None,
    now_iso: str | None = None,
) -> BuildIndexResult:
    """Walk cp.cases and call :func:`index_case` for each row.

    Args:
      courts: which courts to iterate. Two-state semantic — L5:
        - ``None`` → all courts (no WHERE clause)
        - ``[]``   → no courts (WHERE court IN ()) — a legitimate no-op
          for callers who computed an empty allowlist. Do NOT collapse
          to ``if courts:`` — [] and None then become the same path
          and an empty allowlist silently kicks off a full-corpus rebuild.
        - non-empty list → WHERE court IN (?, ...)
      now_iso: passed through to index_case for deterministic timestamps
        (tests). None → real UTC clock.

    Returns a :class:`BuildIndexResult` summarizing the action mix,
    including a `pruned` counter that disambiguates 'N fresh bodies
    indexed' from 'N stale rows removed' within the `indexed` total.
    """
    processed = 0
    indexed = 0
    unchanged = 0
    no_body = 0
    pruned = 0

    # L5 ambiguous-state: courts=None means 'all courts' (no filter);
    # courts=[] means 'zero courts' (a legitimate no-op signal — e.g.
    # a CLI intersected --courts with the shipped list and found no
    # overlap). Do NOT collapse to the same path — that silently kicks
    # off a full-corpus rebuild instead of a no-op.
    if courts is None:
        q = "SELECT court, year, number FROM cases"
        params: list[object] = []
    else:
        placeholders = ",".join(["?"] * len(courts))
        q = f"SELECT court, year, number FROM cases WHERE court IN ({placeholders})"
        params = list(courts)

    for row in cp_conn.execute(q, params):
        case_key = f"{row[0]}/{row[1]}/{row[2]}"
        result = index_case(
            vw_conn, cp_conn, output_root, case_key, now_iso=now_iso
        )
        processed += 1
        if result.action == "indexed":
            indexed += 1
            if result.langs_pruned:
                pruned += 1
        elif result.action == "unchanged":
            unchanged += 1
        elif result.action == "no_body_on_disk":
            no_body += 1

    return BuildIndexResult(
        processed=processed,
        indexed=indexed,
        unchanged=unchanged,
        no_body=no_body,
        pruned=pruned,
    )


# ---------------------------------------------------------------------------
# atomic_swap — os.replace wrapper for viewer.db.new → viewer.db
# ---------------------------------------------------------------------------


def atomic_swap(src: str | Path, dst: str | Path) -> None:
    """Atomically replace ``dst`` with ``src``.

    Wraps :func:`os.replace`, which is atomic on POSIX filesystems: any
    open file descriptor on the old ``dst`` inode keeps working (the
    unlinked-but-open inode stays alive until the last handle closes).
    A fresh open on ``dst`` after this call sees the new inode.

    Raises :class:`FileNotFoundError` if ``src`` does not exist — L1
    loud-failure: a missing new-index is a real bug, not a silent no-op.
    """
    os.replace(str(src), str(dst))


@dataclass(frozen=True)
class BodySource:
    """One indexable body for a (case, language) pair.

    Attributes:
      lang: 'en' or 'tc' — the language the body is written in
      path: absolute or relative Path to the file on disk
      source_kind: one of 'html', 'tc.html', 'generated.html' — the
        physical file variant. Downstream (Phase 3 render) uses this
        to pick the right dispatch branch (native HKLII shape vs
        pandoc fragment).
    """

    lang: str
    path: Path
    source_kind: str


def discover_body_sources(
    output_root: str | Path,
    case_key: str,
    case_lang: str,
) -> list[BodySource]:
    """Enumerate the on-disk body sources for a case.

    Rules (design §4 line 82, INDEX-time enumeration):
      - ``{stem}.tc.html`` is unambiguously TC (regardless of case.lang)
      - ``{stem}.html`` is EN when a .tc.html sibling exists (bilingual
        pair); otherwise it reflects case.lang
      - ``{stem}.generated.html`` fills the case.lang slot as a fallback
        for cases without a real .html body; it never overrides a real
        .html

    Returns one BodySource per language present on disk. Empty list if
    the case has no body files (L5: distinct from a raise — the case
    simply has nothing to index yet).

    Raises ValueError for a malformed case_key (< 2 slashes).
    """
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year, num = parts[0], parts[1], parts[2]
    stem = f"{court}_{year}_{num}"
    d = Path(output_root) / court / year

    html_path = d / f"{stem}.html"
    tc_html_path = d / f"{stem}.tc.html"
    gen_html_path = d / f"{stem}.generated.html"

    sources: list[BodySource] = []

    # .tc.html: always TC
    if tc_html_path.exists():
        sources.append(
            BodySource(lang="tc", path=tc_html_path, source_kind="tc.html")
        )

    # .html: EN if bilingual, else case.lang
    if html_path.exists():
        html_lang = "en" if tc_html_path.exists() else case_lang
        sources.append(
            BodySource(lang=html_lang, path=html_path, source_kind="html")
        )

    # .generated.html: covers case.lang if that language has no source yet
    covered_langs = {s.lang for s in sources}
    if case_lang not in covered_langs and gen_html_path.exists():
        sources.append(
            BodySource(
                lang=case_lang,
                path=gen_html_path,
                source_kind="generated.html",
            )
        )

    return sources
