"""HTTP route handlers for the viewer app.

Each route module exposes a ``router`` :class:`fastapi.APIRouter` that the
app factory (:func:`hklii_downloader.viewer.app.create_app`) mounts. Route
handlers open per-request read-only sqlite connections and never share
state through app-level connection pools (design §8: 'Per-request
connections … sidesteps SQLITE_BUSY_SNAPSHOT').
"""
