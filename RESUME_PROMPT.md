/effort max
/ultracode

Resuming HKLII offline viewer. Phases 1-3 done: graph helpers, FTS
pipeline, body-render pipeline — TDD pairs, two review rounds folded in.
**977 tests pass. 57 commits ahead of `main`. Nothing pushed to origin.**

**Immediate task: Phase 4 — 10 FastAPI routes.** TestClient + BS4
assertions, no live server until Phase 5.

## Load context (pull, don't preload)

1. `~/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md`
   — auto-memory index.
2. `memory/session-close-2026-07-07-viewer-phases-1-3.md` — Phase 1-3
   outcomes, tier-3/4 deferrals, SQLite footguns (UPSERT vs INSERT OR
   REPLACE trigger-fire).
3. `memory/viewer-build-through-phase-3.md` — module layout: `viewer/db.py`
   `schema.py` `graph.py` `search.py` `body_render/{text,render,sanitizer,
   cite}.py`. Read before adding modules — helpers exist.
4. `~/Developer/hklii_viewer/docs/viewer-design.md` §§7-10 — route
   contracts, templates, HTMX partials, error surfaces. **§11 line 320**
   has Phase 4 ship order.
5. `~/Developer/hklii_downloader/docs/review-patterns.md` — 5 lenses.
   Apply at each pair: L1 silent skip, L2 semantic drift, L3 docstring
   drift, L4 wrong-side test (route AND helper), L5 ambiguous state.

## Baseline (verify before starting)

```bash
cd ~/Developer/hklii_viewer
git status                                       # clean, worktree-local-viewer
git log --oneline main..HEAD | wc -l             # 57
timeout 300 uv run pytest -q 2>&1 | tail -3      # 977 passed

cd ~/Developer/hklii_downloader
git status                                       # clean
git log --oneline origin/main..HEAD | wc -l      # 0
```

If any drifts, stop and surface.

## Phase 4 sequencing (design §11 line 320)

Ten routes as TDD pairs. Order matters — later reuse earlier templates:

1. `GET /` — home: recent cases + court tiles
2. `GET /court/{slug}` — landing: year buckets + hub cases
3. `GET /court/{slug}/{year}` — year: paginated case list
4. `GET /case/{slug}/{cid}` — detail: metadata + rendered body
5. `GET /case/{slug}/{cid}/cited-by` — HTMX partial: inbound
6. `GET /case/{slug}/{cid}/authorities` — HTMX partial: outbound
7. `GET /case/{slug}/{cid}/parallel` — HTMX partial: parallel cites
8. `GET /search` — FTS form + BM25 results
9. `GET /search/results` — HTMX partial: paginated results
10. `GET /healthz` — DB open + schema-version check

Each pair: failing test (200, template renders, BS4 asserts element),
then implementation. **Paste failing test output before implementing**
(global rule #1). Two commits: `test: add failing test for route N`,
then `feat: implement route N`. Never combine.

## Design deviations locked in

- **Option 3 scope**: viewer never writes `checkpoint.db`. Hub cache in
  `viewer.db`. No `from_court` column added upstream.
- **Body dispatch**: `select_body_source` prefers native `.html`, falls
  back to `.generated.html`, invalidates via `format_availability_digest`.
- **Sanitizer**: unwrap (form/button) / drop-subtree (script/style/iframe)
  / void-drop (link/meta) — three sets, don't collapse.
- **Citation linkifier**: HKLII pre-wraps neutral cites in `<a>`; regex
  correctly skips. Zero additional wrapping on real bodies is expected.

## Deferrals (do not re-open unless asked)

- Tier-3: appeal_chain path traversal, sha+body_source drift, commit-per-
  case fsync — Phase 5 with CLI + WAL story.
- Tier-4 PLAUSIBLE: open-tx on mid-run raise, snippet empty-highlight,
  open_readonly concurrent-writer test — Phase 6 with contract tests.

## Ready

Run baseline. If clean, say "baseline clean, Phase 4 route 1 next" and
start the first failing test. If baseline drifts, stop and surface.

Do not push to origin without ask. Do not touch downloader repo unless a
helper migrates upstream (unlikely this phase).
