/effort max
/ultracode

You are resuming the HKLII downloader project after the `hklii update`
milestone shipped. **Immediate task (this session): run ONE ultracode
review of the shipped diff, apply anything that survives verification,
then wait for the user's next request.**

## Load context (do not skim)

Open, in this order, the memory files that carry current state. Do NOT
paraphrase them into working memory — open them:

1. `/Users/seangreenhalgh/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md`
   — the auto-memory index. Small; read every line.
2. `memory/update-command-shipped.md` — the milestone that just shipped.
   Profile matrix, dispatch scope, post-ship refinements (11 review
   findings applied), follow-up round A-H (8 items applied), altitude
   gaps addressed. **This is the file to know cold before the review.**
3. `memory/citation-graph-shipped.md` — prior milestone (forward edges,
   parallels, ord/reg). Still relevant: monthly profile deliberately
   excludes scrape-relatedcaps because ord/reg is 100 % locally derivable.
4. `memory/backup-coverage-final.md` — pre-graph headline corpus state
   (162,331 cases + legis + HOPT + translations = ~202k docs, ~30 GB).
5. `memory/hklii-waf-status.md` — origin behaviour + throttler rationale.
6. `memory/retro-2026-07-06.md` — lessons about coverage claims and
   enumerator transparency.

## Session position (verify before starting the review)

```bash
# Working tree + recent history
git status
git log --oneline -10

# The shipped milestone: two atomic commits
#   feat: hklii update — profile-driven incremental refresh
#   test: failing tests for hklii update stack
# plus this docs commit refreshing RESUME_PROMPT.

# Corpus + DB ground truth
sqlite3 -readonly output/.checkpoint.db "SELECT status, COUNT(*) FROM cases GROUP BY status;"
sqlite3 -readonly output/.checkpoint.db "SELECT COUNT(*) FROM citations;"
sqlite3 -readonly output/.checkpoint.db "SELECT name FROM sqlite_master WHERE type='table';" | sort
sqlite3 -readonly output/.checkpoint.db "SELECT COUNT(*) FROM enum_runs;"

# No rogue background work
tmux ls 2>&1 | grep -i hklii || echo NO_TMUX
pgrep -f "hklii scrape" | wc -l | xargs -I{} echo "hklii procs: {}"
docker ps --filter name=hklii-vpn --format 'table {{.Names}}\t{{.Status}}'

# Tests + smoke of the new command
timeout 300 uv run pytest -q -x 2>&1 | tail -3
uv run hklii update --profile daily -p http://127.0.0.1:8888 --dry-run 2>&1 | tail -15
```

**Expected baseline:**
- 734 tests pass
- Working tree clean apart from `scratchpad/`
- git log shows the three commits above ahead of `origin/main`
- 20 `hklii-vpn-*` containers healthy
- Corpus: 162,337 downloaded, 11 failed (empty-content-at-HKLII pending)
- Citations: 242,488 · Parallel cites: 11,617 · Ord/reg: 4,506
- `enum_runs` table exists (empty until first `hklii update` live run)

## What shipped (skim then move on)

- `hklii update` command with profile-driven cadence (daily/weekly/monthly/quarterly/custom)
- `EnumWindow` value object (enumerator.py) bundling min/max_date + sort + items_per_page
- `ScrapeConfig` dataclass (cli.py) replacing 17-kwarg `_run_scrape` signature
- `enum_runs` generation-marker table + accessors on CheckpointDB
- `CheckpointDB.reset_relatedcap_fetches`, `mark_orphaned_below_ts`,
  `is_locked_by_peer`, `latest_completed_enum_run`
- Canary auto-escalation, orphan_mark gated on enum_runs coverage
- Advisory lock at both update-level + CheckpointDB-level (documented)
- 734 tests total (+65 since baseline)

## Immediate task: ONE ultracode code review

Purpose: fresh-eyes review with the whole implementation now settled. The
in-session review already caught + fixed 11 findings from a high-effort
pass; a fresh multi-agent pass may surface things the in-session review
missed because it was reviewing a moving target.

**Do this:**
1. Confirm baseline (checks above pass).
2. Run `/code-review ultra` on the diff for the last two commits
   (`542ed5a feat` + `1ccb5f4 test`). If the cloud review isn't
   available in this environment, fall back to `/code-review max` on
   `main...HEAD~3`.
3. Verify each finding inline (don't rely solely on the reviewer's
   confidence). Apply what survives.
4. Add regression tests for anything you fix.
5. Commit each fix atomically (test then feat pair per global CLAUDE.md).

**Do NOT:**
- Reimplement anything already shipped — memory has the intent.
- Reopen decided design questions (profile matrix, cadence, HKT clock,
  two-lock design, canary uses getmetacase, monthly excludes relatedcaps).
- Re-derive the plan from cli.py — read `memory/update-command-shipped.md`.
- Skim memory files.

## After the review

Wait for the user's next request. There is **no new milestone queued**
after the review — the user is likely to pick the next feature.

Likely follow-up directions the user may ask about (do not preempt):
- Local viewer at `~/Developer/hklii_viewer` (its own worktree,
  reserved for a separate session there)
- AI case-summary service at `ai.hklii.hk` (deferred by explicit
  choice; endpoint unmapped)
- Push the current branch to `origin/main` (currently ahead by 3
  commits)
- Live smoke of `hklii update --profile daily` against production
  (requires clean 20-VPN pool + user confirm)

## Ready

Run the baseline checks. If they hold, tell the user "clean baseline
confirmed — starting ultracode review of the hklii update ship" and
launch the review. If any check drifts, stop and surface.
