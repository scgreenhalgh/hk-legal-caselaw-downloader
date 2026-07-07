"""Tests for the empty-snippet fallback in /search (Phase 6 tier-4 fix).

FTS5's ``snippet(fts_body, 1, ...)`` requests text from column 1 (body).
When the match hits only column 0 (title) and the body is empty or
whitespace-only, ``snippet()`` returns an empty / whitespace string.
The default template then renders a visually-empty ``<p class="snippet">``
paragraph — a row that appears blank even though a real match exists.

The fix (L5 ambiguous-state discipline) must distinguish three cases so
the UI is never mute about *why* a snippet is blank:

  * ``content``    — snippet has real text (usually with ``<mark>``).
  * ``empty``      — FTS5 returned the empty string (no body content
                     available at all).
  * ``whitespace`` — FTS5 returned only whitespace (body existed but
                     collapsed to nothing visible).

Both fallback states surface a marker paragraph so users see the row
does have a match, just not one FTS5 could excerpt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import (
    build_viewer_db,
    seed_cases,
    seed_search_index,
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Three cases exercising the three snippet_state values.

    * hkcfa/2020/1 — normal body containing the query term.
    * hkca/2019/10 — title matches; body is the empty string.
    * hkcfi/2021/22 — title matches; body is whitespace-only.
    """
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            ("hkcfa", 2020, 1, "[2020] HKCFA 1", "Contract dispute",
             "2020-05-05", "downloaded"),
            ("hkca",  2019, 10, "[2019] HKCA 10", "Contract emptybody",
             "2019-08-08", "downloaded"),
            ("hkcfi", 2021, 22, "[2021] HKCFI 22", "Contract whitespace",
             "2021-11-11", "downloaded"),
        ],
    )
    build_viewer_db(viewer)
    seed_search_index(
        viewer,
        [
            {
                "case_key": "hkcfa/2020/1", "lang": "en",
                "court": "hkcfa", "year": 2020, "number": 1,
                "neutral": "[2020] HKCFA 1", "title": "Contract dispute",
                "date": "2020-05-05",
                "body": (
                    "The court considered breach of contract in "
                    "commercial dealings between the parties."
                ),
            },
            {
                # Title carries the FTS match; body is truly empty.
                # snippet(fts_body, 1, ...) → "".
                "case_key": "hkca/2019/10", "lang": "en",
                "court": "hkca", "year": 2019, "number": 10,
                "neutral": "[2019] HKCA 10", "title": "Contract emptybody",
                "date": "2019-08-08",
                "body": "",
            },
            {
                # Title carries the match; body is whitespace-only.
                # snippet(fts_body, 1, ...) → "   " (or similar).
                "case_key": "hkcfi/2021/22", "lang": "en",
                "court": "hkcfi", "year": 2021, "number": 22,
                "neutral": "[2021] HKCFI 22", "title": "Contract whitespace",
                "date": "2021-11-11",
                "body": "   \n\t  ",
            },
        ],
    )
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def _row_for(soup: BeautifulSoup, case_key: str):
    return next(
        r for r in soup.select("[data-testid=search-result]")
        if r.get("data-case-key") == case_key
    )


def test_snippet_state_content_keeps_mark(client: TestClient) -> None:
    """Regression: a normal body match still renders a ``<mark>``.

    Signals ``snippet_state='content'`` on the snippet element so the
    template can distinguish it from the two fallback states.
    """
    resp = client.get("/search", params={"q": "contract"})
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    snippet = _row_for(soup, "hkcfa/2020/1").select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.get("data-snippet-state") == "content"
    assert snippet.find("mark") is not None


def test_snippet_state_empty_shows_fallback_marker(client: TestClient) -> None:
    """L5: an empty snippet must not render as a visually blank ``<p>``.

    When ``snippet(fts_body, 1, ...)`` returns ``""``, the paragraph
    still exists (so the row layout is preserved) but carries fallback
    text and ``data-snippet-state="empty"`` so tests and CSS can style
    or diagnose the state.
    """
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    snippet = _row_for(soup, "hkca/2019/10").select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.get("data-snippet-state") == "empty"
    # No <mark> — there was no body content to highlight.
    assert snippet.find("mark") is None
    # Fallback text is visible, not just whitespace.
    text = snippet.get_text().strip()
    assert text != ""
    assert "match outside displayed excerpt" in text.lower()


def test_snippet_state_whitespace_shows_fallback_marker(
    client: TestClient,
) -> None:
    """L5: distinguish whitespace-only from truly empty.

    The user-visible marker text is the same as the ``empty`` case, but
    the ``data-snippet-state`` attribute records the underlying reason
    so the two states cannot be silently collapsed in later work.
    """
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    snippet = _row_for(soup, "hkcfi/2021/22").select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.get("data-snippet-state") == "whitespace"
    assert snippet.find("mark") is None
    text = snippet.get_text().strip()
    assert text != ""
    assert "match outside displayed excerpt" in text.lower()


def test_snippet_state_values_are_pairwise_distinct(
    client: TestClient,
) -> None:
    """L4/L5 pinning: the three states really do land on three different
    rows — no accidental collapse to a single value across the batch.
    """
    resp = client.get("/search", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    states = {
        r.get("data-case-key"):
            r.select_one("[data-testid=snippet]").get("data-snippet-state")
        for r in soup.select("[data-testid=search-result]")
    }
    assert states["hkcfa/2020/1"] == "content"
    assert states["hkca/2019/10"] == "empty"
    assert states["hkcfi/2021/22"] == "whitespace"


def test_snippet_state_present_on_htmx_partial(client: TestClient) -> None:
    """L4 wrong-side pin: the HTMX partial (/search/results) shares
    the same include, so the fallback must render there too. If the
    fix landed only in the full page, this test fails.
    """
    resp = client.get("/search/results", params={"q": "contract"})
    soup = BeautifulSoup(resp.text, "html.parser")
    snippet = _row_for(soup, "hkca/2019/10").select_one("[data-testid=snippet]")
    assert snippet is not None
    assert snippet.get("data-snippet-state") == "empty"
    assert "match outside displayed excerpt" in snippet.get_text().lower()
