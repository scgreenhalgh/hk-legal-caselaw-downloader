"""Dev entry point for ``hklii serve --dev`` (uvicorn --reload).

uvicorn's ``reload=True`` re-imports the module on file change and needs
an import string, not an app instance. The dev CLI stashes the corpus
paths on env vars and points uvicorn at this module. The module rebuilds
the app on every import, so reloaded workers pick up template/CSS/
Python edits at the next request.
"""

from __future__ import annotations

import os
from pathlib import Path

from hklii_downloader.viewer.app import create_app


_ENV_CHECKPOINT = "HKLII_VIEWER_CHECKPOINT"
_ENV_FTS = "HKLII_VIEWER_FTS"
_ENV_OUTPUT = "HKLII_VIEWER_OUTPUT"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} not set — this module is only for `hklii serve --dev`, "
            f"which populates the env before uvicorn.run."
        )
    return value


app = create_app(
    checkpoint_db=Path(_require_env(_ENV_CHECKPOINT)),
    viewer_db=Path(_require_env(_ENV_FTS)),
    output_root=Path(_require_env(_ENV_OUTPUT)),
)
