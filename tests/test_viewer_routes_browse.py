"""Tests for GET /browse — corpus-wide list with court prefilter (route 30).

Design §6:
  * ``GET /browse?court=<slug>[&year=YYYY][&page=N][&sort=<mode>]``
  * UI *requires* at least one court prefilter — bare ``GET /browse`` is
    rejected (design §6: 'UI requires at least one court prefilter').
  * Court facet is single-select (verdict-YAGNI).
  * Sort modes: ``date_desc`` (default) | ``date_asc`` | ``neutral_asc``.
  * Pagination: 50 rows/page, ``?page=N``.
  * Row shape mirrors the year page — neutral (mono, scarlet) + parties
    (italic serif) + date (ISO, tabular).

L-lens coverage:
  * L1 silent skip — the no-court case fails loudly (400), it doesn't
    silently render the whole corpus (see ``no_court_rejected_with_400``).
  * L2 semantic drift — control rows in a second court and a second year
    stay off the ``?court=hkcfa&year=2023`` result (see the year and court
    filter tests).
  * L3 docstring drift — not applicable; the module docstring pins the
    contract that these tests exercise.
  * L4 wrong-side test — ``date_desc`` order is asserted from the rendered
    ``data-date`` attribute (route + template together), not the SQL string.
  * L5 ambiguous state — bare ``/browse`` cannot be conflated with
    ``/browse?court=…&`` empty result set; distinct status codes and
    distinct testids pin the difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app
from hklii_downloader.viewer.routes.browse import router as browse_router

from tests._route_helpers import build_viewer_db, seed_cases


def _make_cfa_cases(count: int, year: int = 2023) -> list[tuple]:
    """Fabricate ``count`` hkcfa cases with strictly distinct descending dates.

    Case n gets neutral ``[YEAR] HKCFA {n:03d}`` (three-digit pad so
    lexicographic order matches numeric order for the sort tests) and
    date computed to be unique across the whole run — no month overflow,
    no date-collision, so ``date DESC`` is strictly deterministic.
    """
    rows: list[tuple] = []
    for n in range(1, count + 1):
        # Distinct days spread across Dec/Nov/Oct so the sort test can't
        # pass on a partial-key comparison.
        day = 28 - ((n - 1) % 28)
        month = 12 - ((n - 1) // 28)
        rows.append(
            (
                "hkcfa",
                year,
                n,
                f"[{year}] HKCFA {n:03d}",
                f"Party{n} v Other{n}",
                f"{year}-{month:02d}-{day:02d}",
                "downloaded",
            )
        )
    return rows


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            # 55 hkcfa 2023 rows — pagination boundary.
            *_make_cfa_cases(55, year=2023),
            # hkcfa 2024 control — must not appear on ``?court=hkcfa&year=2023``.
            ("hkcfa", 2024, 900, "[2024] HKCFA 900", "AA v BB",
             "2024-01-01", "downloaded"),
            # hkca 2023 control — must not appear on ``?court=hkcfa``.
            ("hkca", 2023, 500, "[2023] HKCA 500", "CC v DD",
             "2023-05-05", "downloaded"),
        ],
    )
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    # Synthesis stage will wire browse_router into ``create_app``; while we
    # ship the route module, we mount the router here so the tests run
    # against the same code path production will hit.
    app.include_router(browse_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Bare /browse rejected (no court prefilter)
# ---------------------------------------------------------------------------


def test_browse_bare_returns_400_no_court(client: TestClient) -> None:
    """L1 silent-skip: without a court, the route MUST reject, not
    silently render 162k rows. Design §6: 'UI requires at least one
    court prefilter'.
    """
    # ``follow_redirects=False`` covers both allowed shapes: a 400 rejection
    # OR a 302 to a pick-a-court landing. Any other status is a bug.
    resp = client.get("/browse", follow_redirects=False)
    assert resp.status_code in (302, 400)


def test_browse_bare_response_hints_at_fix(client: TestClient) -> None:
    """Whichever status is returned, the caller must get a hint pointing
    them at ``?court=<slug>`` — silent 400 with no body would leave a
    reader hunting.
    """
    resp = client.get("/browse", follow_redirects=False)
    if resp.status_code == 302:
        # Landing redirect — Location must exist.
        assert "location" in {k.lower() for k in resp.headers.keys()}
    else:
        # Rendered 400 — body must mention the missing facet.
        assert "court" in resp.text.lower()


# ---------------------------------------------------------------------------
# 2. Happy path: /browse?court=hkcfa
# ---------------------------------------------------------------------------


def test_browse_court_only_returns_200_html(client: TestClient) -> None:
    resp = client.get("/browse?court=hkcfa")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_browse_court_only_renders_rows(client: TestClient) -> None:
    resp = client.get("/browse?court=hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=case-row]")
    assert len(rows) > 0


# ---------------------------------------------------------------------------
# 3. Unknown court → 404
# ---------------------------------------------------------------------------


def test_browse_unknown_court_returns_404(client: TestClient) -> None:
    """Design §6: canonical slug list is ``CANONICAL_COURTS``."""
    resp = client.get("/browse?court=hkxyz")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. Default sort is date_desc
# ---------------------------------------------------------------------------


def test_browse_default_sort_is_date_desc(client: TestClient) -> None:
    """Design §6: 'Sort: date_desc (default) | date_asc | neutral_asc'.
    L4 wrong-side test: assert the RENDERED order, not the SQL string —
    the template drift lens.
    """
    resp = client.get("/browse?court=hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    dates = [r.get("data-date") for r in soup.select("[data-testid=case-row]")]
    # L1 silent-skip: don't let a zero-row response vacuously satisfy
    # ``sorted(dates, reverse=True) == dates``.
    assert len(dates) >= 2
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# 5. sort=neutral_asc reorders rows
# ---------------------------------------------------------------------------


def test_browse_sort_neutral_asc_reorders_rows(client: TestClient) -> None:
    """?sort=neutral_asc lists rows in ascending lexicographic neutral order.
    Neutrals are zero-padded (``HKCFA 001``…``HKCFA 055``) so the lex sort
    is unambiguous, and page 1 pins the first 50 in ``001``→``050`` order.
    """
    resp = client.get("/browse?court=hkcfa&sort=neutral_asc")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=case-row]")
    # Grab the neutral cell from each row.
    neutrals = [r.select_one(".neutral").get_text(strip=True) for r in rows]
    assert neutrals[0] == "[2023] HKCFA 001"
    assert neutrals[-1] == "[2023] HKCFA 050"
    # Full lex-order check across page 1 — a single misplaced row fails.
    assert neutrals == sorted(neutrals)


def test_browse_sort_neutral_asc_differs_from_default(client: TestClient) -> None:
    """L2 semantic drift: default and neutral_asc must actually produce
    different first rows — a coincidence pass on the previous test
    (e.g. sort silently ignored) is caught here.
    """
    default = client.get("/browse?court=hkcfa")
    neutral = client.get("/browse?court=hkcfa&sort=neutral_asc")
    d_first = BeautifulSoup(default.text, "html.parser").select_one(
        "[data-testid=case-row] .neutral"
    ).get_text(strip=True)
    n_first = BeautifulSoup(neutral.text, "html.parser").select_one(
        "[data-testid=case-row] .neutral"
    ).get_text(strip=True)
    assert d_first != n_first


# ---------------------------------------------------------------------------
# 6. Pagination: 55 rows → page 1 has 50, page 2 has 5
# ---------------------------------------------------------------------------


def test_browse_page_1_has_50_rows(client: TestClient) -> None:
    """55 seeded hkcfa/2023 rows → page 1 holds exactly the fixed page size."""
    resp = client.get("/browse?court=hkcfa&year=2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert len(soup.select("[data-testid=case-row]")) == 50


def test_browse_page_2_has_remainder(client: TestClient) -> None:
    """55 seeded hkcfa/2023 rows → page 2 holds 5 (no gap, no overlap).
    Year is pinned so the hkcfa/2024/900 control row doesn't spill onto
    page 2 (it belongs to hkcfa, but not to 2023).
    """
    resp = client.get("/browse?court=hkcfa&year=2023&page=2")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("[data-testid=case-row]")
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# 7. Year filter narrows to (court, year)
# ---------------------------------------------------------------------------


def test_browse_year_filter_narrows_to_year_and_court(client: TestClient) -> None:
    """?year=2023 narrows the hkcfa list to 2023 rows.
    L2 semantic drift: hkcfa/2024/900 is seeded as a control — it must
    NOT appear in the 2023 result.
    """
    resp = client.get("/browse?court=hkcfa&year=2023")
    soup = BeautifulSoup(resp.text, "html.parser")
    keys = {
        r.get("data-case-key")
        for r in soup.select("[data-testid=case-row]")
    }
    # L1 silent-skip: an empty result would vacuously satisfy the two
    # ``not in`` checks and the ``all(...)`` predicate. Require rows.
    assert len(keys) > 0
    # 2024 control absent.
    assert "hkcfa/2024/900" not in keys
    # hkca 2023 control also absent (different court).
    assert "hkca/2023/500" not in keys
    # And every returned row belongs to hkcfa/2023.
    assert all(k.startswith("hkcfa/2023/") for k in keys)


def test_browse_court_filter_excludes_other_court(client: TestClient) -> None:
    """?court=hkcfa must not surface the hkca/2023/500 control row."""
    resp = client.get("/browse?court=hkcfa")
    soup = BeautifulSoup(resp.text, "html.parser")
    keys = {
        r.get("data-case-key")
        for r in soup.select("[data-testid=case-row]")
    }
    # L1 silent-skip: require rows so ``not in {}`` is not vacuous.
    assert len(keys) > 0
    assert "hkca/2023/500" not in keys


def test_browse_year_filter_only_hkcfa_2024_row(client: TestClient) -> None:
    """L5 ambiguous state: 2024 filter should surface exactly the one
    seeded 2024 row and none of the 55 2023 rows.
    """
    resp = client.get("/browse?court=hkcfa&year=2024")
    soup = BeautifulSoup(resp.text, "html.parser")
    keys = [
        r.get("data-case-key")
        for r in soup.select("[data-testid=case-row]")
    ]
    assert keys == ["hkcfa/2024/900"]


# ---------------------------------------------------------------------------
# 8. Extras: row shape matches design §6
# ---------------------------------------------------------------------------


def test_browse_row_shows_neutral_title_date(client: TestClient) -> None:
    """Row surfaces the neutral cite, parties title, and ISO date."""
    resp = client.get("/browse?court=hkcfa&sort=neutral_asc")
    soup = BeautifulSoup(resp.text, "html.parser")
    first = soup.select("[data-testid=case-row]")[0]
    text = first.get_text()
    assert "[2023] HKCFA 001" in text
    assert "Party1 v Other1" in text
    assert "2023-12-28" in text  # day=28 for n=1 per _make_cfa_cases


def test_browse_row_anchor_links_to_case_detail(client: TestClient) -> None:
    """Row anchor pins the ``/case/{court}/{year}/{number}`` URL shape —
    the same contract the year page holds.
    """
    resp = client.get("/browse?court=hkcfa&sort=neutral_asc")
    soup = BeautifulSoup(resp.text, "html.parser")
    first = soup.select_one("[data-testid=case-row]")
    anchor = first.find("a", href=True)
    assert anchor is not None
    assert anchor["href"] == "/case/hkcfa/2023/1"


def test_browse_invalid_sort_falls_back_or_400(client: TestClient) -> None:
    """L1 silent-skip: an unknown ``?sort=`` value must not silently return
    an arbitrary order. Route may reject with 400 OR fall back to the
    default (``date_desc``), but must not honor the garbage value.
    """
    resp = client.get("/browse?court=hkcfa&sort=lol_random")
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, "html.parser")
        dates = [r.get("data-date") for r in soup.select("[data-testid=case-row]")]
        assert dates == sorted(dates, reverse=True)  # default
    else:
        assert resp.status_code == 400
