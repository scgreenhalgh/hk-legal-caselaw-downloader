"""Viewer-side CLI commands.

Owns two entry points, both registered on the top-level ``hklii`` group
in ``hklii_downloader/cli.py`` via ``main.add_command`` calls:

* ``hklii serve`` — boot the FastAPI viewer against a corpus (design §8).
* ``hklii viewer index`` — build ``viewer.db`` from ``.checkpoint.db`` +
  the on-disk bodies. Prerequisite of ``hklii serve``.

Registration lives in ``hklii_downloader/cli.py`` (not decorators here)
so the top-level command names read as peers of ``download`` / ``scrape``.
"""

from __future__ import annotations

import os
import sqlite3
import webbrowser
from pathlib import Path

import click
import uvicorn

from hklii_downloader.viewer.app import create_app
from hklii_downloader.viewer.schema import create_schema
from hklii_downloader.viewer.search import atomic_swap, build_index


_DEFAULT_PORT: int = 8787
_HOST: str = "127.0.0.1"

_ENV_CHECKPOINT = "HKLII_VIEWER_CHECKPOINT"
_ENV_FTS = "HKLII_VIEWER_FTS"
_ENV_OUTPUT = "HKLII_VIEWER_OUTPUT"


def _fail(message: str) -> None:
    """Print ``message`` to stderr and exit 1.

    Wrapped so the error path is one line at each check site — the four
    startup checks read as a linear checklist.
    """
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)


@click.command()
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=Path("./output"),
    show_default=True,
    help="Corpus root — directory containing .checkpoint.db and body files.",
)
@click.option(
    "--fts",
    type=click.Path(path_type=Path),
    default=None,
    help="Viewer FTS DB path. Default: <output>/viewer.db",
)
@click.option(
    "--port",
    type=int,
    default=_DEFAULT_PORT,
    show_default=True,
    help="TCP port to bind. 127.0.0.1 is hardcoded.",
)
@click.option(
    "--dev",
    is_flag=True,
    default=False,
    help="Enable uvicorn --reload and open a browser at boot.",
)
def serve(output: Path, fts: Path | None, port: int, dev: bool) -> None:
    """Boot the local HKLII viewer against a corpus on disk."""
    output = output.resolve()
    if not output.exists() or not output.is_dir():
        _fail(f"corpus root missing at {output}. Pass -o /path/to/output.")

    checkpoint_db = output / ".checkpoint.db"
    if not checkpoint_db.exists():
        _fail(
            f"checkpoint DB missing at {checkpoint_db}. "
            f"Run `hklii scrape` first."
        )

    fts_db = fts.resolve() if fts is not None else (output / "viewer.db")
    if not fts_db.exists():
        _fail(
            f"FTS index missing at {fts_db}. "
            f"Run `hklii viewer index`."
        )

    url = f"http://{_HOST}:{port}"
    click.echo(f"HKLII viewer serving on {url}")
    click.echo(f"  corpus:     {output}")
    click.echo(f"  fts index:  {fts_db}")

    if dev:
        # uvicorn --reload needs an import string, not an app instance.
        # Stash paths on env so the dev-entry module can rebuild the app.
        os.environ[_ENV_CHECKPOINT] = str(checkpoint_db)
        os.environ[_ENV_FTS] = str(fts_db)
        os.environ[_ENV_OUTPUT] = str(output)
        try:
            webbrowser.open(url)
        except (webbrowser.Error, OSError):
            # Non-GUI environment (headless SSH, minimal container) —
            # the server still boots, just no auto-launch.
            pass
        uvicorn.run(
            "hklii_downloader.viewer._dev_entry:app",
            host=_HOST,
            port=port,
            reload=True,
            log_level="info",
        )
    else:
        app = create_app(
            checkpoint_db=checkpoint_db,
            viewer_db=fts_db,
            output_root=output,
        )
        uvicorn.run(
            app,
            host=_HOST,
            port=port,
            reload=False,
            log_level="info",
        )


# ---------------------------------------------------------------------------
# `hklii viewer` group + `hklii viewer index` subcommand
# ---------------------------------------------------------------------------


@click.group()
def viewer() -> None:
    """Viewer-side operations: build the FTS index, precompute caches, ..."""


@viewer.command("index")
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=Path("./output"),
    show_default=True,
    help="Corpus root — directory containing .checkpoint.db and body files.",
)
@click.option(
    "--out",
    "out_db",
    type=click.Path(path_type=Path),
    default=None,
    help="Viewer.db output path. Default: <output>/viewer.db",
)
@click.option(
    "--court",
    "courts",
    multiple=True,
    help="Restrict indexing to a specific court slug. Repeatable. "
         "Default: every court found in cp.cases.",
)
@click.option(
    "--incremental",
    is_flag=True,
    default=False,
    help="Write into the existing viewer.db in place (no atomic swap). "
         "Default: build a fresh viewer.db.new sidecar and swap.",
)
@click.option(
    "--commit-every",
    type=int,
    default=100,
    show_default=True,
    help="Batch commit boundary passed through to build_index. Higher "
         "= fewer fsyncs, larger rollback window on abort.",
)
def index(
    output: Path,
    out_db: Path | None,
    courts: tuple[str, ...],
    incremental: bool,
    commit_every: int,
) -> None:
    """Build the viewer's FTS index from a downloaded corpus.

    Reads case metadata from ``<output>/.checkpoint.db`` and body files
    under ``<output>/<court>/<year>/*.html``. Writes ``fts_cases`` +
    ``case_bodies`` + ``fts_body`` into ``viewer.db``.

    Default is a full rebuild: writes to ``viewer.db.new`` then
    atomically renames over the existing ``viewer.db`` (design §4).
    ``--incremental`` writes in place — useful when a small subset
    changed and a full rebuild's cost isn't worth paying.

    Prerequisite of ``hklii serve``.

    \b
    Examples:
      hklii viewer index -o ./output
      hklii viewer index -o ./output --court hkcfa --court hkca
      hklii viewer index -o ./output --incremental
      hklii viewer index -o ./output --commit-every 500
    """
    output = output.resolve()
    if not output.exists() or not output.is_dir():
        _fail(f"corpus root missing at {output}. Pass -o /path/to/output.")

    checkpoint_db = output / ".checkpoint.db"
    if not checkpoint_db.exists():
        _fail(
            f"checkpoint DB missing at {checkpoint_db}. "
            f"Run `hklii scrape` first."
        )

    target_db = out_db.resolve() if out_db is not None else (output / "viewer.db")

    if incremental:
        # In-place: open target_db directly. If the caller pre-seeded
        # unrelated rows (viewer_hub_cache, ...) those survive.
        build_path = target_db
    else:
        # Full rebuild: write to `<target>.new`, checkpoint, swap onto
        # target. L5 — do NOT collapse this branch with incremental
        # even when target_db doesn't yet exist; the swap is a semantic
        # commitment (atomic-cutover for a concurrent reader), not an
        # incidental optimisation.
        build_path = target_db.with_name(target_db.name + ".new")
        # Best-effort cleanup: a prior failed run may have left a stale
        # .new file behind. Remove it (plus its WAL/SHM siblings) so the
        # fresh build isn't polluted by the leftover.
        for suffix in ("", "-wal", "-shm"):
            stale = build_path.with_name(build_path.name + suffix)
            if stale.exists():
                stale.unlink()

    cp_conn = sqlite3.connect(
        f"file:{checkpoint_db}?mode=ro", uri=True,
    )
    try:
        vw_conn = sqlite3.connect(str(build_path))
        try:
            create_schema(vw_conn)
            court_list = list(courts) if courts else None
            result = build_index(
                vw_conn, cp_conn, output,
                courts=court_list,
                commit_every=commit_every,
            )
            # Force a full checkpoint before close so the DB file
            # standalone contains every write; the WAL sidecar can
            # then be safely orphaned by the swap without data loss.
            vw_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            vw_conn.close()
    finally:
        cp_conn.close()

    if not incremental:
        # atomic_swap = os.replace on the main .db file. Any stale
        # WAL/SHM on the destination from a prior incarnation would
        # confuse a reader, so wipe them first.
        for suffix in ("-wal", "-shm"):
            stale = target_db.with_name(target_db.name + suffix)
            if stale.exists():
                stale.unlink()
        atomic_swap(build_path, target_db)
        # Clean the build-side sidecars — they're empty after the
        # TRUNCATE checkpoint but keeping the tree tidy avoids
        # operator confusion on the next run.
        for suffix in ("-wal", "-shm"):
            stale = build_path.with_name(build_path.name + suffix)
            if stale.exists():
                stale.unlink()

    click.echo(
        f"Indexed {target_db}: "
        f"processed={result.processed} "
        f"indexed={result.indexed} "
        f"unchanged={result.unchanged} "
        f"no_body={result.no_body} "
        f"pruned={result.pruned}"
    )
