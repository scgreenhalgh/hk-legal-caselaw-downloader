/effort max

You are resuming the HKLII downloader project. **Production scrape is either already running in a tmux session named `hklii`, or it needs to be launched.** Before anything, load context from memory in this order:

1. `/Users/seangreenhalgh/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md` (index)
2. `memory/threat-model-local-vs-wire.md` (**READ THIS** — 5 review rounds re-litigated local IP hygiene; the threat scope is documented as out-of-scope)
3. `memory/vpn-infrastructure.md` (20-endpoint gluetun + PIA pool on ports 8888-8907)
4. `memory/hklii-court-databases.md` (per-slug counts, 118,188 total across 11 courts)
5. `memory/cli-feature-matrix.md` (6 subcommands: download, scrape, verify, enrich, recheck-html, monitor)
6. `scratchpad/REVIEW_VERDICT_V5.md` (final GO-WITH-CONDITIONS verdict + abort triggers + composite monitor command)

## Session position (as of resume)

- **main pushed to origin at `0c03cee`**, working tree clean. 63 commits ahead of the pre-fix baseline (`0fc4f84`).
- **469 tests passing** (up from 369). Two-commit `test:` + `fix|feat:` pairs throughout.
- **5 pre-flight review rounds ran this project cycle** (V1-V5 in `scratchpad/REVIEW_VERDICT{,_V2,_V3,_V4,_V5}.md`). All real blockers shipped; V5 returned **GO-WITH-CONDITIONS** with conditions satisfied by the production command as written.
- **VPN pool**: 20 healthy `hklii-vpn-*` containers on ports 8888-8907 (`docker ps --filter name=hklii-vpn` to verify).
- **Threat scope** documented in `research/04` § "Threat scope: local artifacts vs. the wire" (commit `959b467`). Local-artifact home-IP exposure is OUT OF SCOPE. Do NOT flag it in any review.

## What's happening

**Full-corpus production scrape.** 118,188 files across 11 API-alive HKLII courts (all except empty `hksct`/`ukpc`). Wall clock estimate: 16-21h. Disk estimate: ~13.4 GB. Command:

```bash
mkdir -p output
tmux new-session -d -s hklii "uv run hklii scrape \
  \$(for p in \$(seq 8888 8907); do printf ' -p http://localhost:%d' \$p; done) \
  --courts hkcfi,hkca,hkdc,hkcfa,hkldt,hkfc,hkct,hkmagc,hkcrc,hklat,hkoat \
  --lang both \
  --with-summaries --with-appeal-history --save-enum-responses \
  --allow-doc -f html -f json -f txt -f doc \
  -o ./output 2>&1 | tee output/run.stdout"
```

**FIRST ACTION**: check whether the tmux session already exists.

```bash
tmux ls 2>&1 | grep hklii || echo NO_SESSION
```

- If `NO_SESSION`: fire the launch command above.
- If session exists: skip the launch, jump to monitoring.

## Immediate task — start the /loop monitor

Once the scrape is running, kick the adaptive monitor cadence:

**Hour 0-1** (early ban detection, warmup convergence):
```
/loop 5m uv run hklii monitor -o ./output --workers 20 --json
```

**Hour 1-6** (cumulative wire signals):
```
/loop 15m uv run hklii monitor -o ./output --workers 20 --json
```

**Hour 6+** (routine):
```
/loop 30m uv run hklii monitor -o ./output --workers 20 --json
```

Ask the user before swapping cadences.

Each cycle: read the monitor JSON output. On exit 0 → single-line "healthy at hour X.Y, Y%, Z/hr". On exit 1 → surface the WARN alert. On exit 2 → alert loudly + recommend abort.

## Extended manual checks (run once per hour or on any suspicion)

```bash
# W1 magic-byte scan — should print nothing
find ./output -name "*.docx" -mmin -60 -exec sh -c '
  magic=$(head -c 4 "$1" | xxd -p)
  case "$magic" in 504b*|d0cf*) ;; *) echo "W1 SUSPECT: $1 magic=$magic" ;; esac
' _ {} \;

# W5 direct-mode sanity — must be 0
grep -c 'via direct' ./output/scrape.log

# Growth trajectory
wc -l ./output/events.jsonl
du -sh ./output/{failure_samples,.enum_cache,.} 2>/dev/null
```

## Abort triggers

Kill the tmux session (`tmux kill-session -t hklii`) and surface to user when:

| Trigger | Meaning |
| --- | --- |
| `hklii monitor` exit 2 | error-prefix > 100 OR in_progress > 80 OR rate < 4000/hr |
| Any single proxy > 3σ failure count above pool mean | individual IP banned |
| `direct=` counter > 0 | W5 fired (should be impossible for the shipped command) |
| W1 SUSPECT hit | Judiciary WAF flipped |
| Any `degraded` event in events.jsonl | proxy_pool safety-net swallowed a leak-detection |

**Recovery**: restart with `--resume`. Same command, add `--resume`. Checkpoint DB survives kill/reboot (S-3 fsync-parent + WAL).

## Filesystem layout during the run

```
./output/
├── scrape.log                              human log (grep WARNING/ERROR)
├── events.jsonl                            structured JSONL (jq recipes in research/13)
├── run.stdout                              CLI stdout mirror
├── .checkpoint.db                          SQLite WAL — cases table, status, error
├── .enum_cache/                            raw getcasefiles JSON per (court, lang)
├── failure_samples/                        first 20 challenge bodies + 5/prefix
├── hkcfi/, hkca/, hkdc/, hkcfa/, hkldt/, hkfc/, hkct/, hkmagc/, hkcrc/, hklat/, hkoat/
    └── {court}_{year}_{number}.{html,txt,json,doc|docx,summary_en.html,summary_tc.html,appeal_history.json}
```

## Open follow-up tasks (post-run cleanup, NOT blocking)

- **#30** `pending_any_enrichment` should include `failed` rows (manual SQL workaround exists)
- **#38** `HtmlRecheckRunner` challenge branch event emission
- **#61** W5 — thread `HeaderRotator` into `proxy_pool.py:365-371` direct branch (~5 lines + 1 test). LAND BEFORE next `--direct` usage.
- **#62** A2 — court-code → URL-slug lookup for `parser.referer_for` on `/api/getappealhistory` (~30 lines). Not a regression, just an unshipped improvement.

## What NOT to do

- Do NOT relitigate local-artifact home-IP exposure (`stdout`, `scrape.log`, `events.jsonl`, `.checkpoint.db`, `hklii monitor --json`). Documented OOS.
- Do NOT run new pre-flight reviews. 5 rounds already ran; the codebase is heavily hardened. If concerned about something specific, spawn ONE targeted subagent, not a full HUNT-VERIFY workflow.
- Do NOT swap `/loop` cadences without asking the user first.
- Do NOT toggle `--direct` on any subcommand until W5 (#61) lands.
