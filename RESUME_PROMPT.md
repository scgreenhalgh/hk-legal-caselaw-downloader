/effort max
/ultracode

You are resuming the HKLII project. Prior session shipped 3 review-pass
rounds, 51 fixes across 54 commits — **all pushed to `origin/main`**.
Corpus is stable (162,331 cases + 242,488 citations + 11,617 parallel
cites + 4,506 ord/reg edges + ~6k legis). 786 tests pass.

**Immediate task (this session): design + build the offline viewer at
`~/Developer/hklii_viewer` (branch `worktree-local-viewer`).**
Search, filtering, case-to-case linking, citation-map view.

## Load context (don't skim — but pull, don't preload)

Open in order; each pull is small:

1. `~/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md`
   — auto-memory index. Read every line; it points at the rest.
2. `memory/whole-codebase-review-2026-07-07.md` — what shipped last
   session, what's deferred, and the 5-lens methodology in use.
3. `docs/review-patterns.md` — the 5 review lenses. Apply them
   while coding the viewer, especially L4 (test the wrapper, not
   just the wrapped) and L1 (no bare `except`).
4. `docs/citation-graph-design.md` — SEED SPEC for the viewer.
   Sections that matter: **§4.1** (helper contract in
   `hklii/graph.py`), **§5** (RAG integration hooks — informs
   metadata surface), **§6** (browser integration points — the
   four HTMX routes). Design is 3 sessions old; treat as
   starting point, not a spec.
5. `memory/citation-graph-shipped.md` — data model summary.
6. `memory/backup-coverage-final.md` — corpus scale (29 GB, 162k
   judgments, formats per court).

## Session position (verify before starting)

```bash
# Downloader repo — must be clean and level with origin
cd /Users/seangreenhalgh/Developer/hklii_downloader
git status
git log --oneline origin/main..HEAD | wc -l   # 0

# Suite — must still be green
timeout 300 uv run pytest -q 2>&1 | tail -3    # 786 passed

# Viewer worktree — clean, on the right branch
cd ~/Developer/hklii_viewer
git status
git branch --show-current                       # worktree-local-viewer

# Corpus — snapshot to /tmp so we don't fight scrapers over WAL
cp /Users/seangreenhalgh/Developer/hklii_downloader/output/.checkpoint.db /tmp/cp.db
sqlite3 /tmp/cp.db "SELECT status, COUNT(*) FROM cases GROUP BY status;"
sqlite3 /tmp/cp.db "SELECT COUNT(*) FROM citations;"
sqlite3 /tmp/cp.db "SELECT COUNT(*) FROM case_parallel_cites;"
sqlite3 /tmp/cp.db "SELECT COUNT(*) FROM ord_reg_edges;"
sqlite3 /tmp/cp.db "SELECT COUNT(*) FROM legis_documents;"
```

**Expected baseline:**
- 786 tests pass (no drift from prior session's fixes)
- Working tree clean apart from `scratchpad/`
- 0 commits ahead of `origin/main` in the downloader repo
- `~/Developer/hklii_viewer` exists on branch `worktree-local-viewer`,
  clean, no viewer scaffolding yet — layout mirrors parent repo, but
  `pyproject.toml` has `fastapi>=0.115.0 / uvicorn>=0.32.0 /
  jinja2>=3.1.0` pre-declared under a "Viewer stack" comment
- Corpus: 162,331 downloaded, 242,488 citations, etc.

## Immediate task: offline viewer

**Purpose:** browse the 162k HKLII case corpus + 242k citation graph
offline, with:

- **Full-text search** over judgment bodies (SQLite FTS5 is the
  natural fit — same DB, no extra service)
- **Filtering** by court, year, date range, has_translation,
  has_summaries, format present
- **Case linking** — click any citation `[YYYY] COURT N` to jump
  to the cited case
- **Citation map** — visualize inbound (who cited this) and
  outbound (who this cites) edges per case

**Framework (already decided by pyproject.toml signals):**
- FastAPI + uvicorn + Jinja2 templates
- HTMX for interactivity (server-rendered — matches
  design-doc §6)
- No React / Vite / Solid / Svelte
- No extra search service — SQLite FTS5

**Non-goals (explicit):**
- No online sync to HKLII (downloader owns that)
- No user accounts / auth (single-user, local-only)
- No force-directed graph explorer (§6: "lawyers work in ranked lists")
- No treatment labels (applied/distinguished/overruled) — v2
- No character-level pinpoints — v2
- No search results ranking beyond FTS5's BM25 default

## Design phase — run this first as a workflow

The design doc locks helper contracts (§4.1) + route shapes (§6) but
leaves these viewer-build gaps open — hit each as a parallel angle:

| Angle | Question |
|---|---|
| Full-text search | FTS5 schema (which columns virtual? Chinese tokenisation for TC bodies?), query grammar (bare terms, `"quoted"`, `court:CFA`, `year:2020..2023`), result rendering |
| Case body rendering | 162k judgments range 0–2 MB. Server-render inline? iframe the raw .html? Where does citation-highlighting fire (server-side regex over .html, or a client HTMX pass)? |
| Browse & filter | `/court/{slug}/{year}` list pages. What columns? Pagination shape? |
| Citation map viz | D3 vs Cytoscape vs plain SVG. Design doc says "no force-directed" — settle on hierarchical / list-based tree with click-to-expand? |
| `hklii serve` CLI shape | Bind localhost only? Port? Live-reload? How does the viewer read `checkpoint.db` while scrapers write (open in `mode=ro&immutable=0` URI? WAL polling?) |
| Styling | Tailwind CDN vs a classless framework (Pico?) vs vanilla. Legal-corpus body typography matters — serif for judgment text? |
| Testing | Playwright / pytest-async? What's the golden-path e2e? How do the 5 review lenses map to a web UI? |

**Recommended workflow shape:** fan out these 7 angles as parallel
design agents, judge with an adversarial verify pass, synthesize a
single design doc at `docs/viewer-design.md`. Then ship as TDD pairs.

## Then build

Per session CLAUDE.md rules: TDD always, atomic commits, test-first.
Apply the 5 review lenses at each PR-scoped block:

- **L1** silent skip — no bare `except`; observable side-effects only
- **L2** semantic drift — grep all readers of shared state (the
  `citation_hub_cache`, `formats`, `has_translation`) before writes
- **L3** docstring drift — every non-trivial docstring is a test
- **L4** wrong-side test — test the FastAPI route AND the helper it
  calls; integration test the HTMX-driven partial swaps
- **L5** ambiguous state — enumerate NULL / 0 / empty for every
  nullable column the viewer reads

## Ready

Run the baseline checks. If they hold, tell the user "clean baseline
confirmed — launching viewer design workflow" and launch a Workflow
that fans out the 7 design angles above.

If baseline drifts (tests broken, unpushed commits, missing
worktree), stop and surface. Do not start the viewer build on a
drifting foundation.

Do not touch the parent downloader repo unless a fix migrates from
the viewer scope (e.g., a `graph.py` helper the viewer needs but
belongs in the downloader package). If a cross-repo change is
needed, ask before making it.
