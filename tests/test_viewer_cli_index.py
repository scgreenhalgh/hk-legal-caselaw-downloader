"""Tests for `hklii viewer index` — Phase 5 index-build CLI.

The healthy path is end-to-end (real ``build_index`` over a seeded corpus)
because that's the coverage that catches the whole click → sqlite →
atomic_swap chain. Fixture size is 1-3 cases so each run is under 1s.

5-lens coverage (docs/review-patterns.md):

  L1 silent skip:      #3, #4  (missing dir / missing checkpoint fail loud)
  L2 semantic drift:   #6      (--court actually restricts, not a no-op)
  L3 docstring drift:  #1, #2  (help lists every documented option)
  L4 wrong-side test:  #5, #10 (CLI route wired to build_index AND the
                                summary counts surface through stdout)
  L5 ambiguous state:  #7, #8  (--incremental in-place vs default swap
                                are two distinct paths, not one collapsed)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from hklii_downloader.cli import main


# Minimal cases-table DDL matching the columns build_index / index_case
# actually read. Mirrors the fixture used in tests/test_viewer_search.py.
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


def _seed_case(cp: sqlite3.Connection, court: str, year: int, num: int) -> None:
    cp.execute(
        "INSERT INTO cases (court, year, number, neutral, title, date, lang) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            court, year, num, f"[{year}] TEST {num}",
            "HKSAR v Test", f"{year}-01-01", "en",
        ],
    )


def _write_body(
    output: Path, court: str, year: int, num: int,
    content: str = "<p>body</p>",
) -> None:
    d = output / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{court}_{year}_{num}.html").write_text(content, encoding="utf-8")


def _make_healthy_corpus(
    tmp_path: Path,
    cases: list[tuple[str, int, int]],
) -> Path:
    """Seed an output dir with .checkpoint.db + on-disk bodies for `cases`.

    Returns the output dir. Every case has both a cp.cases row AND an
    on-disk .html body so build_index has real work to do.
    """
    output = tmp_path / "output"
    output.mkdir()
    cp = sqlite3.connect(str(output / ".checkpoint.db"))
    try:
        cp.execute(_CP_CASES_MINIMAL_DDL)
        for court, year, num in cases:
            _seed_case(cp, court, year, num)
            _write_body(
                output, court, year, num,
                f"<p>body {court} {year} {num}</p>",
            )
        cp.commit()
    finally:
        cp.close()
    return output


# ---------------------------------------------------------------------
# Discovery + help (L3 docstring drift)
# ---------------------------------------------------------------------


def test_viewer_group_help_lists_index() -> None:
    """L3: `hklii viewer --help` must expose the `index` subcommand."""
    runner = CliRunner()
    result = runner.invoke(main, ["viewer", "--help"])
    assert result.exit_code == 0
    assert "index" in result.output


def test_viewer_index_help_lists_documented_options() -> None:
    """L3: help must list every documented option so a stray rename or
    dropped flag can't ship silently."""
    runner = CliRunner()
    result = runner.invoke(main, ["viewer", "index", "--help"])
    assert result.exit_code == 0
    text = result.output
    assert "--output" in text
    assert "--out" in text
    assert "--court" in text
    assert "--incremental" in text
    assert "--commit-every" in text


# ---------------------------------------------------------------------
# Startup failure surfaces (L1 silent skip / loud failure)
# ---------------------------------------------------------------------


def test_index_missing_output_dir_exit_1_with_fix(tmp_path: Path) -> None:
    """L1: nonexistent -o must fail loud, not silently no-op."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["viewer", "index", "-o", str(tmp_path / "nowhere")]
    )
    assert result.exit_code == 1
    assert "corpus root missing" in result.output.lower()


def test_index_missing_checkpoint_exit_1_with_fix(tmp_path: Path) -> None:
    """L1: missing .checkpoint.db must fail loud with the fix-hint
    that points at `hklii scrape` (matches the `serve` error surface)."""
    output = tmp_path / "output"
    output.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["viewer", "index", "-o", str(output)])
    assert result.exit_code == 1
    assert "checkpoint" in result.output.lower()
    assert "hklii scrape" in result.output.lower()


# ---------------------------------------------------------------------
# Healthy path (L4 wrong-side test — real build_index end to end)
# ---------------------------------------------------------------------


def test_index_healthy_path_builds_viewer_db(tmp_path: Path) -> None:
    """L4: 3-case corpus → viewer.db exists at <output>/viewer.db with
    exactly 3 fts_cases rows. Exercises the entire click → sqlite chain."""
    output = _make_healthy_corpus(tmp_path, [
        ("hkcfa", 2020, 1),
        ("hkcfa", 2020, 2),
        ("hkca", 2020, 3),
    ])

    runner = CliRunner()
    result = runner.invoke(main, ["viewer", "index", "-o", str(output)])
    assert result.exit_code == 0, result.output

    viewer_db = output / "viewer.db"
    assert viewer_db.exists()

    conn = sqlite3.connect(str(viewer_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM fts_cases").fetchone()[0]
    finally:
        conn.close()
    assert n == 3


# ---------------------------------------------------------------------
# --court filter (L2 semantic drift)
# ---------------------------------------------------------------------


def test_index_court_filter_restricts_processing(tmp_path: Path) -> None:
    """L2: --court hkcfa must produce ONLY hkcfa rows, not the full corpus.

    A bug like ``courts = list(courts) or None`` on an empty tuple would
    silently rebuild everything — this asserts the filter is honoured.
    """
    output = _make_healthy_corpus(tmp_path, [
        ("hkcfa", 2020, 1),
        ("hkca", 2020, 2),
    ])
    runner = CliRunner()
    result = runner.invoke(main, [
        "viewer", "index", "-o", str(output), "--court", "hkcfa",
    ])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(output / "viewer.db"))
    try:
        courts = sorted(
            r[0] for r in conn.execute(
                "SELECT DISTINCT court FROM fts_cases"
            )
        )
    finally:
        conn.close()
    assert courts == ["hkcfa"]


# ---------------------------------------------------------------------
# --incremental vs default (L5 ambiguous state)
# ---------------------------------------------------------------------


def test_index_incremental_writes_in_place_and_preserves_marker(
    tmp_path: Path,
) -> None:
    """L5: --incremental modifies the existing viewer.db in place — a
    pre-seeded unrelated table row (viewer_hub_cache) must survive.

    If the CLI mistakenly went through the .new + swap path here, the
    fresh viewer.db would replace the pre-seeded one and the marker
    would disappear.
    """
    output = _make_healthy_corpus(tmp_path, [("hkcfa", 2020, 1)])

    # Pre-seed viewer.db with a marker row that build_index will not touch.
    from hklii_downloader.viewer.schema import create_schema
    viewer_db = output / "viewer.db"
    conn = sqlite3.connect(str(viewer_db))
    try:
        create_schema(conn)
        conn.execute(
            "INSERT INTO viewer_hub_cache "
            "(case_key, inbound_count, computed_at) VALUES (?, ?, ?)",
            ["marker/1/1", 42, "2026-07-07T00:00:00Z"],
        )
        conn.commit()
    finally:
        conn.close()

    runner = CliRunner()
    result = runner.invoke(main, [
        "viewer", "index", "-o", str(output), "--incremental",
    ])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(viewer_db))
    try:
        marker = conn.execute(
            "SELECT inbound_count FROM viewer_hub_cache "
            "WHERE case_key = ?",
            ["marker/1/1"],
        ).fetchone()
        n_cases = conn.execute(
            "SELECT COUNT(*) FROM fts_cases"
        ).fetchone()[0]
    finally:
        conn.close()

    # Marker survived → proves in-place write, not full replace.
    assert marker == (42,)
    # And the fresh case landed alongside.
    assert n_cases == 1


def test_index_default_calls_atomic_swap(tmp_path: Path) -> None:
    """L5: non-incremental default path builds to a `.new` sidecar file
    and finalises with atomic_swap. Distinct from --incremental."""
    output = _make_healthy_corpus(tmp_path, [("hkcfa", 2020, 1)])

    with patch("hklii_downloader.viewer.cli.atomic_swap") as swap:
        runner = CliRunner()
        result = runner.invoke(main, ["viewer", "index", "-o", str(output)])
    assert result.exit_code == 0, result.output
    swap.assert_called_once()

    # The call must move the `.new` sidecar onto the target viewer.db.
    args = swap.call_args.args
    src = Path(str(args[0]))
    dst = Path(str(args[1]))
    assert src.name.endswith(".new")
    assert dst == output / "viewer.db"


# ---------------------------------------------------------------------
# --commit-every passthrough
# ---------------------------------------------------------------------


def test_index_commit_every_flag_passes_through_to_build_index(
    tmp_path: Path,
) -> None:
    """--commit-every N is forwarded to build_index verbatim."""
    from hklii_downloader.viewer.search import BuildIndexResult

    output = _make_healthy_corpus(tmp_path, [("hkcfa", 2020, 1)])

    with patch(
        "hklii_downloader.viewer.cli.build_index",
        return_value=BuildIndexResult(
            processed=1, indexed=1, unchanged=0, no_body=0,
        ),
    ) as bi:
        runner = CliRunner()
        result = runner.invoke(main, [
            "viewer", "index", "-o", str(output), "--commit-every", "200",
        ])
    assert result.exit_code == 0, result.output
    assert bi.call_args.kwargs["commit_every"] == 200


# ---------------------------------------------------------------------
# Summary line (L4 wrong-side test — counter comes through stdout)
# ---------------------------------------------------------------------


def test_index_prints_action_summary_on_success(tmp_path: Path) -> None:
    """L4: stdout must expose processed/indexed/unchanged counts so
    the operator can see what happened without opening the DB."""
    output = _make_healthy_corpus(tmp_path, [("hkcfa", 2020, 1)])
    runner = CliRunner()
    result = runner.invoke(main, ["viewer", "index", "-o", str(output)])
    assert result.exit_code == 0, result.output
    text = result.output.lower()
    assert "processed" in text
    assert "indexed" in text
    assert "unchanged" in text
