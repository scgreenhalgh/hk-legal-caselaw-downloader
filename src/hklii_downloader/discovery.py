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
# close (2026-07-08) — refreshing is a manual Playwright step. Shipped
# at TWO locations to keep both editable-checkout and wheel installs
# working:
#   * ``src/hklii_downloader/data/databases_matrix.html`` — the
#     authoritative packaged copy. Included in the wheel via
#     pyproject's package-data config so ``pip install .`` /
#     ``pip install hklii-downloader`` both ship it.
#   * ``tests/fixtures/databases_page_rendered_2026-07-08.html`` —
#     the drift-guard fixture the D1 tests parse directly. Editable
#     checkouts have both files; wheel installs only have the first.
# ``load_default_matrix`` prefers the packaged copy so it survives any
# install shape; the tests/fixtures fallback is retained purely for
# operators building against an uninstalled checkout with no wheel.
_DEFAULT_MATRIX_FIXTURE = "databases_page_rendered_2026-07-08.html"
_PACKAGED_MATRIX_FILENAME = "databases_matrix.html"


def load_default_matrix() -> DatabaseMatrix:
    """Load the packaged ``/databases`` fixture as a
    :class:`DatabaseMatrix`.

    Callers (``FreshnessRunner``, ``hklii check-freshness``,
    ``hklii update``'s freshness step) rely on the fixture as the
    authoritative slug × lang list until D3 ships a live-render step.

    Load order:
      1. ``importlib.resources.files("hklii_downloader") /
         "data" / "databases_matrix.html"`` — the wheel-safe primary.
      2. ``<repo>/tests/fixtures/databases_page_rendered_YYYY-MM-DD.html``
         — dev-mode fallback so a fresh checkout without an installed
         wheel still resolves. Walks up from ``discovery.py`` to find it.

    Raises ``FileNotFoundError`` if BOTH lookups miss — a clear signal
    (as opposed to a silently-empty matrix) so an operator who accidentally
    shipped without the packaged data sees the packaging bug at the CLI
    surface rather than at scrape-time.
    """
    # Primary: importlib.resources reads the packaged copy inside the
    # wheel. This is the only lookup that works when hklii_downloader
    # is pip-installed (non-editable) or a wheel is used directly —
    # the tests/ directory is never present in that case.
    try:
        from importlib.resources import files
        pkg_data = (
            files("hklii_downloader") / "data" / _PACKAGED_MATRIX_FILENAME
        )
        if pkg_data.is_file():
            return parse_databases_matrix(pkg_data.read_text())
    except (ModuleNotFoundError, FileNotFoundError):
        # importlib.resources may raise on old / unusual install layouts;
        # fall through to the dev-mode ancestor walk instead of failing.
        pass

    # Dev-mode fallback: walk up from discovery.py to find
    # ``tests/fixtures/databases_page_rendered_YYYY-MM-DD.html``. Only
    # trips inside an editable checkout without the packaged data
    # subtree (e.g. `git clone && pytest` with no `uv sync`/`pip install
    # -e .` step) — production installs never reach here.
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "tests" / "fixtures" / _DEFAULT_MATRIX_FIXTURE
        if candidate.is_file():
            return parse_databases_matrix(candidate.read_text())
    raise FileNotFoundError(
        f"Cannot find packaged fixture "
        f"src/hklii_downloader/data/{_PACKAGED_MATRIX_FILENAME} "
        f"nor tests/fixtures/{_DEFAULT_MATRIX_FIXTURE} beneath any "
        f"ancestor of {here}. If installing from a wheel, the wheel "
        "was built without the data/ subtree — check pyproject.toml's "
        "[tool.hatch.build.targets.wheel.force-include] section."
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
