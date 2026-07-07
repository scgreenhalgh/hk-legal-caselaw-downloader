"""Self-tests for ``assert_htmx_swap_matches`` (design §10 line 302).

The helper lives in ``tests/conftest.py`` and pins the HTMX swap
pattern used across the viewer's Phase 4 tab shells + future
partials. Six angles the tests pin, one per review lens:

  1. innerHTML happy path — silent pass (L4 verifies the panel slot)
  2. innerHTML target missing → pytest.fail (L1 silent-skip mitigation)
  3. outerHTML happy path — silent pass
  4. outerHTML fragment root missing id → pytest.fail
  5. Unknown swap mode (``beforeend`` — a real HTMX mode used by the
     paginated cited-by list) → pytest.fail so the tab-vs-append
     distinction can't drift unnoticed
  6. Shipped case-detail HTMX pattern (cited-by / authorities /
     parallel) round-trips through the helper against the real app —
     L4 pair: helper works AND the routes actually satisfy it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from hklii_downloader.viewer.app import create_app

from tests._route_helpers import build_viewer_db, seed_cases
from tests.conftest import assert_htmx_swap_matches


_NATIVE_HKLII_HTML = (
    "<html><head><title>x</title></head>"
    "<body><form name=\"search_body\"><p>x</p></form></body></html>"
)


def _stub_client(routes: dict[str, str]) -> TestClient:
    """Tiny FastAPI app that echoes ``routes[path]`` for each key.

    Used by tests 1-5 so the helper is exercised in isolation from the
    viewer app — the pattern being verified is HTMX attribute wiring,
    not the shipped routes, and inlined HTML pins exactly what BS4 sees.
    """
    app = FastAPI()

    def _make(payload: str):
        def _handler() -> HTMLResponse:
            return HTMLResponse(payload)

        return _handler

    for path, html in routes.items():
        app.get(path, response_class=HTMLResponse)(_make(html))
    return TestClient(app)


# ----- 1: innerHTML happy path ------------------------------------------
def test_innerhtml_target_id_present_passes() -> None:
    """L4 happy path: hx-target selector resolves to a container whose
    ``id`` matches ``target_id`` — helper returns silently.
    """
    parent_html = """
    <html><body>
      <button hx-get="/frag" hx-target="#slot" hx-swap="innerHTML">Load</button>
      <div id="slot"></div>
    </body></html>
    """
    client = _stub_client({"/parent": parent_html})
    # No raise expected — silent return is the pass signal.
    assert_htmx_swap_matches(client, "/parent", "/frag", "slot")


# ----- 2: innerHTML target missing --------------------------------------
def test_innerhtml_target_id_missing_fails() -> None:
    """L1 silent-skip: hx-target=``#slot`` selector matches nothing in
    the parent DOM (a class was used where an id was intended). Helper
    must pytest.fail with the missing id surfaced in the message.
    """
    parent_html = """
    <html><body>
      <button hx-get="/frag" hx-target="#slot" hx-swap="innerHTML">Load</button>
      <div class="slot"></div>
    </body></html>
    """
    client = _stub_client({"/parent": parent_html})
    with pytest.raises(pytest.fail.Exception) as exc:
        assert_htmx_swap_matches(client, "/parent", "/frag", "slot")
    assert "slot" in str(exc.value)


# ----- 3: outerHTML happy path ------------------------------------------
def test_outerhtml_fragment_root_has_id_passes() -> None:
    """Fragment root ``<div id="slot">`` is what replaces the source
    element, so the ``id`` survives the swap. Helper returns silently.
    """
    parent_html = """
    <html><body>
      <div id="slot" hx-get="/frag" hx-target="this" hx-swap="outerHTML"></div>
    </body></html>
    """
    fragment_html = '<div id="slot"><p>Loaded content.</p></div>'
    client = _stub_client(
        {"/parent": parent_html, "/frag": fragment_html},
    )
    assert_htmx_swap_matches(client, "/parent", "/frag", "slot")


# ----- 4: outerHTML fragment root missing id ----------------------------
def test_outerhtml_fragment_root_missing_id_fails() -> None:
    """Fragment root has no ``id`` — after the outerHTML swap the id
    disappears from the DOM, silently breaking any subsequent selector
    that expected to find it. Helper must pytest.fail loudly.
    """
    parent_html = """
    <html><body>
      <div id="slot" hx-get="/frag" hx-target="this" hx-swap="outerHTML"></div>
    </body></html>
    """
    fragment_html = "<div><p>No id here.</p></div>"
    client = _stub_client(
        {"/parent": parent_html, "/frag": fragment_html},
    )
    with pytest.raises(pytest.fail.Exception) as exc:
        assert_htmx_swap_matches(client, "/parent", "/frag", "slot")
    assert "slot" in str(exc.value)


# ----- 5: Unknown swap mode ---------------------------------------------
def test_beforeend_swap_mode_fails() -> None:
    """L1: ``beforeend`` is a valid HTMX mode (used by 'Load next 50'
    for cited-by pagination) but has DIFFERENT semantics from the tab
    pattern — it appends to the target rather than replacing content.
    Silence here would let a tab-panel route drift into append-mode
    unnoticed. Helper must pytest.fail and name the mode.
    """
    parent_html = """
    <html><body>
      <button hx-get="/frag" hx-target="#list" hx-swap="beforeend">More</button>
      <ol id="list"></ol>
    </body></html>
    """
    client = _stub_client({"/parent": parent_html})
    with pytest.raises(pytest.fail.Exception) as exc:
        assert_htmx_swap_matches(client, "/parent", "/frag", "list")
    assert "beforeend" in str(exc.value)


# ----- 6: Real case-detail HTMX pattern ---------------------------------


def _write_body(
    output_root: Path, court: str, year: int, number: int, html: str
) -> None:
    d = output_root / court / str(year)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{court}_{year}_{number}.html").write_text(html, encoding="utf-8")


@pytest.fixture
def case_detail_client(tmp_path: Path) -> TestClient:
    """Mirrors ``tests/test_viewer_routes_case_detail.py::client`` — a
    single-row corpus with an on-disk HKLII HTML body so the case
    detail route returns 200 for /case/hkcfa/2020/1.
    """
    checkpoint = tmp_path / "checkpoint.db"
    viewer = tmp_path / "viewer.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    seed_cases(
        checkpoint,
        [
            (
                "hkcfa", 2020, 1, "[2020] HKCFA 1", "P v Q",
                "2020-05-05", "downloaded",
            ),
        ],
    )
    _write_body(output_root, "hkcfa", 2020, 1, _NATIVE_HKLII_HTML)
    build_viewer_db(viewer)
    app = create_app(
        checkpoint_db=checkpoint, viewer_db=viewer, output_root=output_root,
    )
    return TestClient(app)


def test_shipped_case_detail_tabs_pass_helper(
    case_detail_client: TestClient,
) -> None:
    """L4 wrong-side test coverage: the Phase 4 case-detail template
    wires three ``hx-get``/``hx-target``/``hx-swap="innerHTML"`` tabs
    into three ``<div id="panel-*">`` panels. The helper must accept
    all three round-trips through the shipped app — future template
    drift (e.g. someone renames a panel id but leaves hx-target alone)
    now fails HERE too, not just in the per-route pin tests.
    """
    parent = "/case/hkcfa/2020/1"
    assert_htmx_swap_matches(
        case_detail_client,
        parent,
        "/case/hkcfa/2020/1/cited-by",
        "panel-cited-by",
    )
    assert_htmx_swap_matches(
        case_detail_client,
        parent,
        "/case/hkcfa/2020/1/authorities",
        "panel-authorities",
    )
    assert_htmx_swap_matches(
        case_detail_client,
        parent,
        "/case/hkcfa/2020/1/parallel",
        "panel-parallel",
    )
