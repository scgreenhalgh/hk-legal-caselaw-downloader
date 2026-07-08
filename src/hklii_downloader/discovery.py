"""Phase D1 discovery — extract the ``slug × lang`` matrix from the
HKLII ``/databases`` page.

The ``/databases`` route is a Vue SPA — the server-rendered HTML is
a ~2.7KB skeleton with no DB list. To ship a drift guard against
our hardcoded fan-out lists (``ALL_COURTS`` in ``cli.py``,
``HOPT_C_COURTS`` in ``ukpc.py``, ``LEGIS_CAP_TYPES`` in ``legis.py``,
``HOPT_ABBRS`` in ``hopt.py``), Phase D1 works on a checked-in
rendered-HTML fixture. Refreshing the fixture is a manual step —
run Playwright against ``https://www.hklii.hk/databases``,
capture ``document.documentElement.outerHTML``, and replace
``tests/fixtures/databases_page_rendered_YYYY-MM-DD.html``.

Phase D2 (freshness via count / last-updated) and Phase D3 (remove
all hardcoded court lists) are deliberately out of scope for D1.
The primary consumer of D1 is the drift-guard test in
``tests/test_discovery.py``.

Anchor shape parsed here — every DB card on ``/databases`` renders
a link like:

    <a href="/en/cases/hkcfa/">Court of Final Appeal</a>
    <a href="/tc/cases/hkcfa/">...</a>
    <a href="/en/legis/ord/">Ordinances</a>
    <a href="/tc/legis/ord/">...</a>
    <a href="/sc/legis/ord/">...</a>
    <a href="/en/other/pd/">...</a>

We collect (lang, category, slug) triples across every anchor in the
DOM, dedupe langs per (category, slug), and return sorted tuples so
comparisons are deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

# Deliberately strict: the path must be exactly
# ``/<lang>/<category>/<slug>`` optionally followed by ``/`` and any
# deeper segments (a link into ``/en/cases/hkcfa/2020/1`` still counts
# for the (en, cases, hkcfa) triple). Slug is [a-z0-9_-]+ to cover
# every slug HKLII has shipped to date (``hkcfa``, ``pcpdaab``, etc.).
_DB_ANCHOR_RE = re.compile(
    r"^/(en|tc|sc)/(cases|legis|other)/([a-z0-9_-]+)(?:/|$)"
)


@dataclass
class DatabaseMatrix:
    """Extracted matrix by category. Each dict maps
    ``slug`` → sorted tuple of language codes present.
    """
    cases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    legis: dict[str, tuple[str, ...]] = field(default_factory=dict)
    other: dict[str, tuple[str, ...]] = field(default_factory=dict)


# Frozen ground truth for the /databases matrix. See the D1 session
# close (2026-07-08) — refreshing is a manual Playwright step. Kept in
# tests/fixtures/ rather than src/ so packaging stays clean; discovery
# code walks up from __file__ to find it. The fallback (empty matrix)
# only trips when the fixture is missing entirely — every consumer that
# depends on a non-empty matrix (FreshnessRunner, drift-guard tests)
# raises loudly on the empty case.
_DEFAULT_MATRIX_FIXTURE = "databases_page_rendered_2026-07-08.html"


def load_default_matrix() -> DatabaseMatrix:
    """Load the checked-in ``/databases`` fixture as a
    :class:`DatabaseMatrix`.

    Callers (``FreshnessRunner``, ``hklii check-freshness``,
    ``hklii update``'s freshness step) rely on the fixture as the
    authoritative slug × lang list until D3 ships a live-render step.
    A missing fixture raises FileNotFoundError so an operator who
    accidentally shipped without ``tests/fixtures/`` sees a clear
    signal instead of a silently-empty matrix.
    """
    # Walk up from this file to the repo root, then into tests/fixtures.
    # Deliberately dev-mode-friendly — we're not packaging the fixture as
    # data yet because D3 will replace the whole approach.
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "tests" / "fixtures" / _DEFAULT_MATRIX_FIXTURE
        if candidate.is_file():
            return parse_databases_matrix(candidate.read_text())
    raise FileNotFoundError(
        f"Cannot find fixture {_DEFAULT_MATRIX_FIXTURE!r} beneath any "
        f"ancestor of {here}. Refresh via a Playwright render of "
        "https://www.hklii.hk/databases and drop into tests/fixtures/."
    )


def parse_databases_matrix(html: str) -> DatabaseMatrix:
    """Parse the rendered ``/databases`` HTML into a
    :class:`DatabaseMatrix`.

    Uses BeautifulSoup because the fixture is a full-DOM outerHTML
    dump with the usual SPA cruft — a regex over the whole document
    would false-match on inline JSON or script bodies that reference
    a slug. BS4 walks anchor tags only.

    Langs are deduplicated per (category, slug) and returned sorted so
    repeated calls produce identical output — the drift-guard test
    depends on that stability.
    """
    soup = BeautifulSoup(html, "lxml")
    # {category: {slug: {"en", "tc", "sc"}}} — set until we sort.
    scratch: dict[str, dict[str, set[str]]] = {
        "cases": {}, "legis": {}, "other": {},
    }
    for a in soup.find_all("a", href=True):
        m = _DB_ANCHOR_RE.match(a["href"])
        if m is None:
            continue
        lang, category, slug = m.group(1), m.group(2), m.group(3)
        # cases/legis/other are the only categories in the regex, so
        # the `.setdefault(...)` bucket is always in `scratch`.
        scratch[category].setdefault(slug, set()).add(lang)

    def _finalize(bucket: dict[str, set[str]]) -> dict[str, tuple[str, ...]]:
        return {slug: tuple(sorted(langs)) for slug, langs in bucket.items()}

    return DatabaseMatrix(
        cases=_finalize(scratch["cases"]),
        legis=_finalize(scratch["legis"]),
        other=_finalize(scratch["other"]),
    )
