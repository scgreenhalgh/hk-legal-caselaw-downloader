"""Local viewer for the downloaded HKLII corpus.

Read-only FastAPI + Jinja + HTMX app over the checkpoint DB. Ships with
a SQLite FTS5 index over judgment bodies for full-text search. See
viewer/search.py for the index shape and viewer/app.py for the routes.
"""
