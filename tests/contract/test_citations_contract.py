"""Contract test for the shared ``citations`` table.

Schema-drift alarm: the writer (``CheckpointDB.insert_citation_edges``)
and the reader (``viewer.graph.cited_by``) live in separate modules
and evolve independently. This test writes canonical rows through the
writer's public API, reads them back through the reader, and asserts
semantic equivalence over the columns the routes actually consume:
``from_key``, ``to_key``, ``citer_lang``, ``citer_freq``, ``position``,
``first_seen``.

Landing here in the viewer worktree at ``tests/contract/`` — the
design's original "downloader package" directive (docs/viewer-design.md
§10) was amended so contract tests live where the viewer code lives.

Five review-lens angles covered (docs/review-patterns.md):
  * L2 semantic drift — writer tuple order must match reader columns
    (test_roundtrip_via_public_api_reads_back_via_cited_by).
  * L5 ambiguous state — bilingual citer is ONE citer, not two
    (test_bilingual_pair_collapses_to_one_row_with_both_langs).
  * L2 hidden performance regression — reader's WHERE ``to_key=?`` path
    depends on ``idx_cit_to``; a silent DROP INDEX in a future
    migration would still make every unit test pass at 20 rows while
    scaling from index-seek to table-scan at 250k rows
    (test_idx_cit_to_index_exists_on_shipped_schema).
  * L1 silent-skip — writer's INSERT OR IGNORE must not double-insert
    on same-PK re-write, and the reader must not surface duplicated
    langs from a garbage GROUP_CONCAT
    (test_reinsert_same_pk_is_idempotent).
  * L5 absence-is-not-error — deleting the underlying row must let
    ``cited_by`` return ``[]`` cleanly, not raise
    (test_delete_removes_row_from_cited_by).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hklii_downloader.checkpoint import CheckpointDB
from hklii_downloader.viewer.db import open_readonly
from hklii_downloader.viewer.graph import cited_by


FIRST_SEEN = "2026-07-06T05:00:00Z"


@pytest.fixture
def cp_path(tmp_path: Path) -> Path:
    """Path to a fresh checkpoint.db location. The writer opens/closes
    via CheckpointDB; the reader opens via viewer.db.open_readonly."""
    return tmp_path / "checkpoint.db"


def test_roundtrip_via_public_api_reads_back_via_cited_by(
    cp_path: Path,
) -> None:
    """Writer via ``CheckpointDB.insert_citation_edges``, reader via
    ``viewer.graph.cited_by`` — schema-drift alarm on the round-trip.

    A tuple-order swap in the writer (e.g. citer_lang and citer_freq
    reversed) would leave every writer-side unit test green — the
    columns still hold *some* value — but this contract would catch
    the drift because the reader's ``citer_freq`` would come back as
    the string ``"en"`` (not the integer ``3``).
    """
    db = CheckpointDB(str(cp_path))
    try:
        db.insert_citation_edges(
            [("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0)],
            first_seen=FIRST_SEEN,
        )
    finally:
        db.close()

    conn = open_readonly(cp_path)
    try:
        rows = cited_by(conn, "hkcfa/2020/32")
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["from_key"] == "hkcfi/2021/100"
    assert row["to_key"] == "hkcfa/2020/32"
    assert row["from_court"] == "hkcfi"
    assert row["langs"] == "en"
    assert row["citer_freq"] == 3
    assert row["position"] == 0
    assert row["first_seen"] == FIRST_SEEN


def test_bilingual_pair_collapses_to_one_row_with_both_langs(
    cp_path: Path,
) -> None:
    """Two writer rows for the same (from_key, to_key) but different
    ``citer_lang`` are ONE citer, not two — the reader's ``GROUP BY
    from_key`` + ``GROUP_CONCAT(DISTINCT citer_lang)`` collapses them.

    L5 ambiguous-state: a schema change that dropped ``citer_lang``
    from the reader's SELECT (say, in favor of a JOIN into a new
    lang table) would surface here as two rows instead of one, or as
    ``langs=NULL``.
    """
    db = CheckpointDB(str(cp_path))
    try:
        db.insert_citation_edges(
            [
                ("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0),
                ("hkcfi/2021/100", "hkcfa/2020/32", "tc", 3, 0),
            ],
            first_seen=FIRST_SEEN,
        )
    finally:
        db.close()

    conn = open_readonly(cp_path)
    try:
        rows = cited_by(conn, "hkcfa/2020/32")
    finally:
        conn.close()

    assert len(rows) == 1
    # GROUP_CONCAT ordering is unspecified in SQLite — compare as a set.
    langs = set(rows[0]["langs"].split(","))
    assert langs == {"en", "tc"}


def test_idx_cit_to_index_exists_on_shipped_schema(cp_path: Path) -> None:
    """``cited_by``'s hot path is ``WHERE to_key=?`` — served by the
    ``idx_cit_to`` index shipped in ``checkpoint._SCHEMA``. A migration
    that silently drops this index would let every unit test pass at
    20 rows while regressing production (250k+ edges) from index-seek
    to full scan.

    Assertion is on ``sqlite_master`` directly, not on a query plan —
    a plan-based check would test the reader's SQL text, not the
    writer's schema commitment.
    """
    db = CheckpointDB(str(cp_path))
    try:
        row = db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_cit_to'"
        ).fetchone()
        assert row is not None, (
            "idx_cit_to missing — cited_by would fall back to full "
            "table scan; checkpoint._SCHEMA must ship the index."
        )
    finally:
        db.close()


def test_reinsert_same_pk_is_idempotent(cp_path: Path) -> None:
    """Two ``insert_citation_edges`` calls with the same
    ``(from_key, to_key, citer_lang)`` must yield ONE row in the
    table, and the reader must surface it as one row with a single
    ``citer_lang`` (not ``"en,en"`` from a GROUP_CONCAT over two
    duplicate rows).

    Locks the writer's ``INSERT OR IGNORE`` contract from the
    reader's perspective — a switch to plain ``INSERT`` would raise
    IntegrityError inside the writer (caught early), but a switch to
    ``INSERT OR REPLACE`` would silently corrupt the reader-visible
    state (first_seen gets overwritten) without any writer-side
    signal. This contract catches that.
    """
    db = CheckpointDB(str(cp_path))
    try:
        edge = [("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0)]
        db.insert_citation_edges(edge, first_seen=FIRST_SEEN)
        db.insert_citation_edges(edge, first_seen="2099-12-31T23:59:59Z")

        count = db._conn.execute(
            "SELECT COUNT(*) FROM citations"
        ).fetchone()[0]
        assert count == 1
    finally:
        db.close()

    conn = open_readonly(cp_path)
    try:
        rows = cited_by(conn, "hkcfa/2020/32")
    finally:
        conn.close()

    assert len(rows) == 1
    # OR IGNORE keeps the original — first_seen must NOT be overwritten.
    assert rows[0]["first_seen"] == FIRST_SEEN
    assert rows[0]["langs"] == "en"


def test_delete_removes_row_from_cited_by(cp_path: Path) -> None:
    """No public delete API exists for the ``citations`` table — the
    scraper is write-only. Deleting via raw SQL and re-reading via
    ``cited_by`` verifies the reader's ``WHERE to_key=?`` branch
    returns ``[]`` cleanly, not a raise.

    L5 absence-is-not-error: an empty result set is a legitimate
    answer (case has no known citers), distinct from a raise (schema
    missing) or a None (row present with NULL fields).
    """
    db = CheckpointDB(str(cp_path))
    try:
        db.insert_citation_edges(
            [("hkcfi/2021/100", "hkcfa/2020/32", "en", 3, 0)],
            first_seen=FIRST_SEEN,
        )
        db._conn.execute(
            "DELETE FROM citations WHERE from_key=? AND to_key=?",
            ("hkcfi/2021/100", "hkcfa/2020/32"),
        )
        db._conn.commit()
    finally:
        db.close()

    conn = open_readonly(cp_path)
    try:
        rows = cited_by(conn, "hkcfa/2020/32")
    finally:
        conn.close()
    assert rows == []
