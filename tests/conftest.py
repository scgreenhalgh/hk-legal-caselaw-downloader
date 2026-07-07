"""Shared pytest hooks and helpers for HKLII viewer tests.

Design §10 line 302 pins ``assert_htmx_swap_matches`` as the standard
way route tests verify HTMX ``hx-get``/``hx-target``/``hx-swap`` triples
land where their author intended. The helper branches on ``hx-swap``:

* ``innerHTML`` (HTMX default) — the target container (matching
  ``hx-target``) must exist in the parent DOM AND carry ``target_id``
  as its ``id`` attribute. That container is the slot the fragment
  content lands in; if its id drifts, subsequent selectors, OOB
  updates, and screen-reader landmarks all lose their anchor.
* ``outerHTML`` — the fragment ROOT returned from ``htmx_url`` must
  carry ``id=target_id``. The source element is replaced whole, so
  the id has to be re-emitted by the fragment or it disappears.
* anything else (``beforeend``, ``afterbegin`` etc.) — ``pytest.fail``
  by name. Silence here would let the tab-panel pattern drift into an
  append-only pattern with different semantics unnoticed (L1 silent
  skip mitigation from ``docs/review-patterns.md``).

Kept at the top of ``tests/`` (not per-directory) so any future test
module can ``from tests.conftest import assert_htmx_swap_matches``
without re-importing across sibling conftests.
"""

from __future__ import annotations

from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag


def assert_htmx_swap_matches(
    client: Any,
    parent_url: str,
    htmx_url: str,
    target_id: str,
) -> None:
    """Assert an HTMX swap-into-slot pattern lands where it says.

    Fetches ``parent_url`` via ``client``, finds the element whose
    ``hx-get`` equals ``htmx_url``, and — based on that element's
    ``hx-swap`` mode — verifies ``target_id`` still names the slot
    the swap will end up in.

    Returns silently on match. Calls :func:`pytest.fail` with a
    diagnostic message on mismatch or on any swap mode other than
    ``innerHTML`` / ``outerHTML``.
    """
    parent_resp = client.get(parent_url)
    if parent_resp.status_code != 200:
        pytest.fail(
            f"GET {parent_url} returned {parent_resp.status_code}; "
            f"expected 200 to look up hx-get={htmx_url!r}"
        )
    parent_soup = BeautifulSoup(parent_resp.text, "html.parser")

    element = parent_soup.find(attrs={"hx-get": htmx_url})
    if element is None:
        pytest.fail(
            f"No element in {parent_url} carries hx-get={htmx_url!r}; "
            f"cannot verify swap for target id={target_id!r}"
        )

    swap_mode = element.get("hx-swap", "innerHTML")

    if swap_mode == "innerHTML":
        target_sel = element.get("hx-target")
        if target_sel is None:
            pytest.fail(
                f"Element with hx-get={htmx_url!r} has no hx-target; "
                f"cannot verify innerHTML swap into id={target_id!r}"
            )
        container = parent_soup.select_one(target_sel)
        if container is None:
            pytest.fail(
                f"hx-target={target_sel!r} on the {htmx_url} trigger "
                f"resolves to nothing in {parent_url}; the innerHTML "
                f"swap has no slot to land in (expected id={target_id!r})"
            )
        found_id = container.get("id")
        if found_id != target_id:
            pytest.fail(
                f"innerHTML swap for {htmx_url}: hx-target={target_sel!r} "
                f"resolves to id={found_id!r}, expected id={target_id!r}"
            )
        return

    if swap_mode == "outerHTML":
        frag_resp = client.get(htmx_url)
        if frag_resp.status_code != 200:
            pytest.fail(
                f"GET {htmx_url} returned {frag_resp.status_code}; "
                f"expected 200 to inspect fragment root for id={target_id!r}"
            )
        frag_soup = BeautifulSoup(frag_resp.text, "html.parser")
        root = next(
            (node for node in frag_soup.contents if isinstance(node, Tag)),
            None,
        )
        if root is None:
            pytest.fail(
                f"Fragment from {htmx_url} has no top-level element; "
                f"outerHTML swap has nothing to carry id={target_id!r}"
            )
        found_id = root.get("id")
        if found_id != target_id:
            pytest.fail(
                f"outerHTML swap for {htmx_url}: fragment root "
                f"<{root.name}> has id={found_id!r}, expected id={target_id!r}"
            )
        return

    pytest.fail(
        f"Unsupported hx-swap={swap_mode!r} on element with "
        f"hx-get={htmx_url!r}; helper only verifies innerHTML and "
        f"outerHTML (design §10 tab-panel pattern). If this swap is "
        f"legitimate, add a branch — do not accept it silently."
    )
