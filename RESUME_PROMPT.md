/effort max
/ultracode

You are resuming the HKLII downloader project after the citation-graph
milestone. **No new milestone is queued** — the next session's task is
whatever the user asks. This prompt boots you cleanly so the first
message doesn't burn context re-deriving state.

## Load context (in this order — do not skim)

1. Read `/Users/seangreenhalgh/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md` — the auto-memory index. It's small; read every line.
2. Open, in this order, the memory files that carry the current state:
   - `memory/backup-coverage-final.md` — pre-graph headline corpus state (162,331 cases + legis + HOPT + translations = ~202k docs, ~29-30 GB).
   - `memory/citation-graph-shipped.md` — this session's shipped work: 242,488 forward edges, 11,617 parallels, 4,506 ord/reg edges, throttler retuned 3-5×, silent-empty gotcha, top hub cases.
   - `memory/hklii-api-structure.md` — endpoint reference. All 11 API scrapers we hit.
   - `memory/hklii-waf-status.md` — origin behaviour + rationale for the throttler defaults.
   - `memory/retro-2026-07-06.md` — earlier session's lessons about coverage claims, sample extrapolation, and enumerator transparency.
3. Read `docs/backup-coverage-2026-07-06.md` and `docs/citation-graph-design.md` for the current shipped design.

DO NOT paraphrase these into working memory — open them.

## Session position (start-of-session ground truth)

Verify the following holds before answering any substantive question.
If any check fails, STOP and surface — do not proceed assuming state:

```bash
# Working tree
git status
git log --oneline -10

# No rogue background work
tmux ls 2>&1 | grep -i hklii || echo NO_TMUX
pgrep -f "hklii scrape" | wc -l | xargs -I{} echo "hklii procs: {}"
docker ps --filter name=hklii-vpn --format 'table {{.Names}}\t{{.Status}}'

# Corpus ground truth
sqlite3 -readonly output/.checkpoint.db "SELECT status, COUNT(*) FROM cases GROUP BY status;"
sqlite3 -readonly output/.checkpoint.db "SELECT COUNT(*) FROM citations;"
sqlite3 -readonly output/.checkpoint.db "SELECT COUNT(*) FROM case_parallel_cites;"
sqlite3 -readonly output/.checkpoint.db "SELECT COUNT(*) FROM ord_reg_edges;"
find output -name "*.noteup.json" | wc -l
du -sh output/
```

Expected on the clean baseline:
- `cases`: 162,331 downloaded, no other status
- `citations`: 242,488
- `case_parallel_cites`: 11,617
- `ord_reg_edges`: 4,506
- `.noteup.json` count: 162,331
- `output/`: ~30 GB
- 20 `hklii-vpn-*` containers healthy
- No live `hklii scrape*` processes
- Working tree clean apart from `scratchpad/`

Test suite: **669 tests, all green** (last verified this session).
Unpushed commits ahead of `origin/main`: some — check `git log`.

## What NOT to do

- Do NOT paraphrase memory files into a summary — read them.
- Do NOT re-scrape any HKLII endpoint the user hasn't explicitly asked for. Every backup phase is complete: cases, enrich, legis (current + history), HOPT, translations, doc-to-html, validate, noteup, relatedcaps.
- Do NOT re-run `scrape-relatedcaps` even for validation — the API adds nothing beyond the numeric-suffix pattern. See `citation-graph-shipped.md` "The critical ord→reg finding" for the SQL derivation.
- Do NOT tighten the throttler further without evidence of headroom — the current retune already achieves ~35-40 req/sec / 20 workers with zero HKLII pushback observed over ~200k requests. Rolling back is fine; further tightening needs a canary.
- Do NOT relitigate the "HKLII has a WAF" concern — `hklii-waf-status` is settled, backed by many completed runs.
- Do NOT touch the local-viewer worktree at `~/Developer/hklii_viewer` unless the user asks. Tasks #79-#83 live there and are reserved for a fresh session in that directory.
- Do NOT re-derive citation counts from judgment HTML — the API-backed `citations` table is canonical for the corpus we have. Local regex extraction is a fallback we discussed but explicitly deferred.

## Likely first-question shapes and where to route

- **"What did we ship last session?"** → Read `citation-graph-shipped.md`, answer from that + the corpus ground-truth checks. Don't re-derive.
- **"Run the RAG pipeline / build the viewer"** → Worktree at `~/Developer/hklii_viewer` is where the viewer scaffold lives. Tasks #79-#83 waiting. Confirm before touching main-repo files.
- **"Refresh the citation graph"** → Idempotent via `hklii scrape-noteup`. Only ~20-25 min at retuned throughput. Point at `citation-graph-shipped.md` first so the user knows the current state.
- **"Compare against a fresh scrape / diff over time"** → Raw `.noteup.json` sidecars are the canonical baseline. Diff before touching the DB.
- **"Anything else worth scraping at HKLII?"** → No, per the exhaustive endpoint audit in `docs/backup-coverage-2026-07-06.md` and this session's confirmed local-derivability for `getrelatedcaps`. Deferred items (AI case-summary service at `ai.hklii.hk`) are noted in that doc.

## Deliberate non-goals (do not re-litigate)

- **AI case summaries at `ai.hklii.hk`** — separate service, endpoint unmapped, deferred by explicit choice.
- **`getcasenoteup` sample-based diff** — the API-backed count IS the answer; there's no other source of truth on citer inbound-count.
- **Rebuilding graph tables from disk** — supported (all raw sidecars preserved) but only if the DB is lost. Don't do it proactively.

## Ready

Verify the baseline. If it holds, tell the user "clean baseline confirmed" plus a one-line summary of state, and wait for their request. If any check drifts, stop and surface.
