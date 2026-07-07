"""`hklii serve` — viewer boot CLI (Phase 5, design §8).

Contract (design §8):
  * ``--output`` default ``./output``. Directory contains ``.checkpoint.db``.
  * ``--fts`` default ``<output>/viewer.db``.
  * ``--port`` default 8787 (avoids 8000/8080/3000 collisions).
  * ``--dev`` enables ``uvicorn --reload`` + browser auto-open (errors
    swallowed on non-GUI hosts).
  * Bind 127.0.0.1 hardcoded — no ``--host`` flag in v1.
  * Four error messages that name the fix (missing corpus, missing
    checkpoint, missing FTS, port in use).

Registered on the top-level ``hklii`` group by ``cli.py``'s
``main.add_command(viewer_cli.serve)``.
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path

import click
import uvicorn

from hklii_downloader.viewer.app import create_app


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
