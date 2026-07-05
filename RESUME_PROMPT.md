/effort max

This session is running with `ultracode`; use it where the spec calls for the multi-agent path. You are resuming the HKLII downloader project after a corpus-complete milestone. The next milestone is a **corpus validator**, not more scraping. Do not launch any scrape, enrich, or recheck runs.

## Load context (in this order)

1. `/Users/seangreenhalgh/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md` — memory index. Scan entries, then open the supplementary files it points at that touch validation surface (checkpoint DB schema, artifact layout, enrichment status columns).
2. `memory/session-2026-07-05-bugfix-and-sweep.md` — the just-shipped session retro. Reports the 100% corpus outcome, the five TDD fix pairs (#63–#67), the current test count, manual-grab receipts for the four linkage-stale rows, and the final artifact-size numbers.
3. `memory/incident-2026-07-05-pool-storm.md` — retro for the pool-death that motivated the shipped #65 re-queue fix. Read this so you understand why the code around `AllProxiesDeadError` and the drain-recovery path looks the way it does before the validator starts asserting invariants over it.
4. `/Users/seangreenhalgh/Developer/hklii_downloader/scratchpad/VALIDATOR_SPEC.md` — the design doc from the parallel workflow branch. This is the spec you will implement.

Do NOT paraphrase these files into working memory; open them.

## Session position

Corpus at **162,331 / 162,331 rows (100.000%)**, ~20 GB of artifacts on disk across the 13 API-alive slugs. **481 tests.** Five fix pairs shipped this session cycle (#63 challenge-page false-positive, #64 Word 95 magic bytes, #65 pool-death re-queue, #66/#67 marker cleanup). Working tree should be clean; nothing is scraping; no tmux `hklii` session should exist.

Threat scope is documented in `research/04` § "Threat scope: local artifacts vs. the wire" (commit `959b467`). Five pre-flight review rounds ran earlier in this project cycle. Local-artifact home-IP exposure is OUT OF SCOPE and must not be re-flagged.

## Immediate task

Build the corpus validator per `scratchpad/VALIDATOR_SPEC.md`. TDD as always (failing test → paste literal output → implement → refactor). The spec owns the design; your job is to execute it, not redesign it. If the spec is ambiguous on a specific decision, ask before diverging.

## First actions

a. Verify a clean starting state before touching the spec:

```bash
# working tree + recent history
git status
git log --oneline -8

# no rogue background work
tmux ls 2>&1 | grep -i hklii || echo NO_TMUX
docker ps --filter name=hklii-vpn --format 'table {{.Names}}\t{{.Status}}'
```

Nothing should be scraping. If tmux or a container looks off, ask before killing anything.

b. Read the four pointer files above **in order**. Do not skim MEMORY.md and skip straight to the spec — the session and incident retros set the invariants the validator will assert.

c. Confirm ground truth by re-running the corpus fact-gathering commands the spec lists (row counts, per-slug counts, per-format file counts, enrichment status columns). Numbers must match the 162,331 / ~20 GB baseline in the 2026-07-05 session file. If they don't, STOP and surface — don't start writing tests against a moving corpus.

d. Once ground truth matches, follow the TDD plan in the spec. Two commits per fix (`test:` then `feat:`/`fix:`), atomic scope, one logical change per commit.

## What NOT to do

- Do NOT launch `hklii scrape`, `hklii enrich`, `hklii recheck-html`, or any variant. Corpus is complete; there is nothing to fetch.
- Do NOT re-litigate the fixes shipped as #63–#67 (challenge-page FP, DOCX/Word-95 magic, `AllProxiesDeadError` re-queue, marker cleanup). They landed with tests; treat them as done.
- Do NOT drift into a fix-loop on `failed` rows. There are none — the manual grab already cleared the four linkage-stale rows and final status is 100.000%. If the validator flags a row as suspect, report it; do not "just try one more download" as a side-quest.
- Do NOT relitigate local-artifact home-IP exposure. Documented OOS across five review rounds.
- Do NOT run a fresh HUNT-VERIFY or full pre-flight review. If a specific concern arises, spawn ONE targeted subagent — not a workflow.
- Do NOT modify `research/`, memory files, or shipped fix commits as part of this task. The validator is additive.

Ask before touching anything outside the validator's scope.
