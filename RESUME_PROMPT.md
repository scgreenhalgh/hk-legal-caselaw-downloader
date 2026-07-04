/effort max

ultracode

You are resuming the HKLII downloader project on 2026-07-04. Before anything else, load context from memory in this exact order (do not grep the repo — the memories carry the state):

1. `/Users/seangreenhalgh/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md` (the index)
2. `memory/session-2026-07-04-resume.md` (what happened, what's next)
3. `memory/vpn-infrastructure.md` (20-endpoint gluetun + PIA pool)
4. `memory/hklii-court-databases.md` (per-slug counts, production target scope)
5. `memory/hklii-vs-judiciary.md` (doc-fallback context for --allow-doc)
6. Cross-reference only: `research/README.md` (single entry point to the 12-chapter docs — do not read the chapters unless a review agent asks you to)

## Session position

- `main` at the latest commit, pushed to origin. Working tree clean.
- 369 tests passing (up from 293 — every audit item landed via TDD with a paired `test:` + `feat|fix:` commit).
- 10 audit items shipped this session: S-3 (parent-dir fsync), M-1 (URL-context Referer), M-3 (curl_cffi chrome146/142/136/131 + bare), S-1 (bilingual challenge detection), A+ (multiplicative jitter at 8 retry sites), M-2 (Sec-Fetch XHR-vs-nav) + M-7 (delete HeaderRotator.rotate), S-5 (atomic .enum_cache writes), S-4 (fcntl silent-fallback WARNING log), M-6 (http2=True direct client), M-4 (per-proxy warm-up GET https://www.hklii.hk/). Plus a preflight fix: `proxy_pool._fetch_ip` now checks `status_code` instead of `raise_for_status` (curl_cffi.HTTPError vs httpx.HTTPStatusError mismatch was aborting the 20-proxy preflight on a single httpbin 502).
- One new feature: `html_pending_at_hklii` nullable INTEGER column on the checkpoint + `hklii recheck-html` subcommand + `html_recheck.HtmlRecheckRunner`. Scraper stamps the column on the empty-content-with-doc-fallback path so a follow-up pass can find and re-check those rows once HKLII processes the HTML. Documented in `research/10` + `11` + `12`.
- VPN pool expanded 6 -> 20 endpoints (Singapore x7, JP Tokyo x3, Hong Kong x3, Taiwan x2, Malaysia x2, Macao x2, South Korea x1) via new `scripts/expand_vpn_pool.py`. 20 unique exit IPs verified.
- Two canaries, both 100/100 downloaded 0 failed: 100-file hkfc @ 6-pool = 2 min, same @ 20-pool = 52 s.
- 12-chapter `research/` directory authored (~52k words) and post-review-cleaned (0 blockers, 7 important + 7 nits all fixed inline).

## What's next — and what to hold on

The production scrape is the only remaining pending item. Do NOT launch it yet — the user is holding until a pre-flight review returns no blockers. Command (for reference; run only after clearance):

```
uv run hklii scrape \
  $(for p in $(seq 8888 8907); do printf ' -p http://localhost:%d' $p; done) \
  --courts hkcfi,hkca,hkdc,hkcfa --lang both \
  --with-summaries --with-appeal-history --save-enum-responses \
  --allow-doc -f html -f json -f txt -f doc \
  -o ./output
```

Est 15-20h, ~13 GB. Cannot fail silently mid-run.

## Immediate task — run the pre-flight review

Kick off the HUNT-VERIFY-ANCHORED review workflow below. It's a 4-phase orchestration (worst-case 18 subagents, best-case 8): 1 bootstrap manifest, 6 parallel failure-mode finders, up to 10 parallel default-refute verifiers, 1 synth. It's sized to catch what breaks over a 20-40h scrape without combinatorial blow-up.

Every downstream agent gets the "SHIPPED" preamble so verifiers auto-refute anything the 10 audit items already resolved (that's the biggest noise source when reviewing just-shipped work). The mimicry finder is chapter-anchored to `research/04-07` so it crosschecks published claims against actual code, not free-form guesses. The runbook finder actually EXECUTES command blocks from `research/11` and `12` (bash -n at minimum, real du/ls/sqlite3/rg/pytest --collect-only when safe). Verifiers use a default-refute posture — assume every finding is wrong, try three refutations before conceding CONFIRMED, and every CONFIRMED must carry a concrete `repro_path`.

Paste the script (embedded below) into a Workflow() call. Deliverables land at:
- `./scratchpad/manifest.json` (Phase 1 shared reference frame)
- `./scratchpad/REVIEW_VERDICT.md` (Phase 4 human report)
- `./scratchpad/REVIEW_VERDICT.json` (Phase 4 machine-readable: `{verdict, blockers, watches, accepted, hour4_checkpoint, needs_human, refuted_count}`)

Verdict values: `GO` / `GO-WITH-CONDITIONS` / `NO-GO`. Report BLOCKERS to the user with TDD-shaped fix sketches (`test:` commit signature + `fix|feat:` change direction) matching this repo's cadence. Also surface the single hour-4 checkpoint command the synth picks — that's what the user will run mid-scrape to catch drift earliest.

Do NOT start the production scrape until:
1. The review returns `GO` or the user has approved all `GO-WITH-CONDITIONS` items, AND
2. Any BLOCKERS have been fixed via the TDD cadence (test commit -> impl commit -> tests green), AND
3. Any NEEDS_HUMAN smoke tests have been run and passed.

## Workflow script

```python
# HUNT-VERIFY-ANCHORED — pre-flight for HKLII 20-40h production scrape
# 1 bootstrap + 6 failure-mode finders + up to 10 default-refute verifiers + 1 synth
# Worst case: 18 agents. Best case (clean): 8.

REPO = "/Users/seangreenhalgh/Developer/hklii_downloader"
SCRATCH = "./scratchpad"

SHIPPED = """
Session 2026-07-04 shipped 10 TDD audit items (test commit + impl commit each):
  S-3  fsync parent dir after os.replace                (atomic_write.py)
  M-1  URL-context Referer derivation                   (parser.referer_for)
  M-3  curl_cffi rotation chrome146/142/136/131 + bare  (impersonate_client._IMPERSONATE_PROFILES)
  S-1  bilingual challenge-page detection (7 EN + 6 TC) (scraper._looks_like_challenge_page)
  A+   multiplicative jitter on retry backoff (8 sites) (scraper.py retry loops)
  M-2  Sec-Fetch XHR-vs-nav split                       (scraper.HeaderRotator, client._BROWSER_HEADERS)
  M-7  delete HeaderRotator.rotate()                    (scraper.HeaderRotator)
  S-5  atomic_write_text for .enum_cache                (enum_cache writer)
  S-4  WARNING log on fcntl silent-fallback             (atomic_write.py)
  M-6  http2=True in direct-mode client + httpx[http2]  (client.py, pyproject.toml)
  M-4  per-proxy warm-up GET https://www.hklii.hk/      (proxy_pool._warm_up_target)

Plus preflight fix: proxy_pool._fetch_ip HTTPError type (curl_cffi vs httpx).
Pool expanded 6 -> 20 endpoints via scripts/expand_vpn_pool.py.
New feature: html_pending_at_hklii column + `hklii recheck-html` (walks doc-fallback rows).
369 tests passing. Two canaries: 100/100 hkfc downloaded, 0 failed (6-pool 2m, 20-pool 52s).

Production command about to run:
  uv run hklii scrape $(for p in $(seq 8888 8907); do printf ' -p http://localhost:%d' $p; done) \
    --courts hkcfi,hkca,hkdc,hkcfa --lang both \
    --with-summaries --with-appeal-history --save-enum-responses \
    --allow-doc -f html -f json -f txt -f doc -o ./output
Est 15-20h, ~13 GB. Cannot fail silently mid-run.
"""

# PHASE 1 — Bootstrap manifest
phase("bootstrap")
manifest = agent(
    name="bootstrap",
    prompt=f"""{SHIPPED}
    Emit JSON manifest to {SCRATCH}/manifest.json with:
      hot_files, retry_sites, atomic_write_sites, fsync_sites, log_paths,
      secret_touch_sites, runbook_commands (verbatim from research/11+12),
      canary_artifacts (./output tree if present else null),
      checkpoint_db_path/size, enum_cache_du, output_du,
      test_count (confirm 369 via pytest --collect-only -q | tail -1).
    This is the ONLY phase that shells out; downstream agents load this manifest.""",
    tools=["Read", "Grep", "Bash(rg|du|ls|find|sqlite3|wc|pytest --collect-only|bash -n)"],
)

# PHASE 2 — Six failure-mode finders (parallel)
phase("hunt")
LENSES = {
    "silent-corruption": "SIGKILL/OOM/power-cut over 20h. Verify S-3 parent-dir fsync landed. Checkpoint DB no-commit paths, .enum_cache freshness bumps (S-5), half-written output/, xdev os.replace edges. Auto-refute S-3/S-5-resolved items.",
    "credential-leak":   "Real IP or PIA creds exposure over 15-20h. Fallback when 20 proxies red. ERROR/DEBUG logs with URL/headers/proxy. .enum_cache contents. sqlite dump-on-crash. curl_cffi verbose. Tracebacks with .env. gluetun healthcheck token in docker logs.",
    "over-24h-hazard":   "Unbounded growth at ~150k HTTP calls. httpx.Client lifecycle (M-6 http2 leaks?), cookie jar retention, WAL checkpoint frequency, .enum_cache curve, log rotation, health-loop task lifetime, temp cleanup, fd count across 20 proxies * N workers, parser mem.",
    "mimicry-vs-docs":   "CHAPTER-ANCHORED walk of research/04-07. For each 'we send X' / 'we rotate Y' claim, grep code to confirm. Auto-refute M-1/M-2/M-3/M-4/M-6/M-7 resolved. Focus on GAPS: Accept-Language stability, UA-JA3 pairing (chrome146 impersonate ships chrome146 UA?), cadence uniformity (jitter delays as well as backoff?), per-proxy cookie isolation, warmup vs steady-state rate.",
    "runbook-executes":  "Read research/11 + research/12. For each command block: RUN it (bash -n minimum; if safe: du/ls/sqlite3/rg/pytest --collect-only/docker compose config). Report broken paths, wrong CLI flags, wrong container names, dead file:line cites. Verify every 'if X then Y' still holds against current code.",
    "test-vs-reality-gap": "What do 369 tests mock away that 20h prod hits? time.sleep patched in retry tests (A+ jitter actually kicks in?), MockTransport-only failover, canned WAF HTML not live 403, same-fs atomic writes (xdev os.replace?), no SIGKILL in checkpoint tests (WAL recover?). Auto-refute what canaries proved (100/100).",
}
findings = parallel([
    agent(name=f"finder-{lens}",
          prompt=f"""{SHIPPED}
Load manifest at {SCRATCH}/manifest.json.
Hunt through the {lens} lens ONLY. Rubric: {rubric}
Budget: 2 findings max. Empty list is a valid answer.
Return JSON list of {{file, line, lens, summary, failure_scenario, severity (BLOCKER|HIGH|MEDIUM|LOW), sig (hash of file:line:summary[:40])}}""",
          tools=["Read", "Grep", "Bash(rg|wc|du|ls|sqlite3|bash -n|docker compose config)"])
    for lens, rubric in LENSES.items()
])
all_candidates = flatten(findings)
seen, candidates = set(), []
for c in sorted(all_candidates, key=lambda x: {'BLOCKER':0,'HIGH':1,'MEDIUM':2,'LOW':3}[x.severity]):
    if c.sig not in seen:
        seen.add(c.sig); candidates.append(c)
candidates = candidates[:10]

# PHASE 3 — Default-refute verifiers (up to 10 parallel)
phase("verify")
verified = parallel([
    agent(name=f"verifier-{c.sig[:8]}",
          prompt=f"""{SHIPPED}
ADVERSARIAL verifier. DEFAULT POSTURE: the finding below is WRONG until proven otherwise.
Finding ({c.lens}, severity={c.severity}): {c.file}:{c.line} — {c.summary}
Failure scenario: {c.failure_scenario}
Try THREE refutations before conceding CONFIRMED:
  1. Test already covers this and passes? (grep tests/)
  2. Finder misreading control flow / helper called elsewhere? (trace call graph)
  3. Shipped audit item already resolves it? (S-3/M-1/M-3/S-1/A+/M-2/M-7/S-5/S-4/M-6/M-4)
If lens == runbook-executes, add:
  4. RUN the command block. Does it break as claimed?
Only if ALL applicable refutations FAIL, emit CONFIRMED with mandatory repro_path.
If genuinely undecidable statically, emit NEEDS_HUMAN with a one-line smoke test.
You never see other verifiers' findings.
Emit JSON: {{finding_sig, verdict (CONFIRMED|REFUTED|NEEDS_HUMAN), refutation_attempts:[3 strings], repro_path (if CONFIRMED), which_refutation_succeeded (if REFUTED), smoke_test (if NEEDS_HUMAN)}}""",
          tools=["Read", "Grep", "Bash(rg|pytest --collect-only|sqlite3|bash -n)"])
    for c in candidates
])
confirmed   = [(c,v) for c,v in zip(candidates,verified) if v.verdict == "CONFIRMED"]
refuted     = [(c,v) for c,v in zip(candidates,verified) if v.verdict == "REFUTED"]
needs_human = [(c,v) for c,v in zip(candidates,verified) if v.verdict == "NEEDS_HUMAN"]

# PHASE 4 — Synth: GO/NO-GO + hour-4 checkpoint
phase("synth")
verdict = agent(
    name="synthesizer",
    prompt=f"""{SHIPPED}
Confirmed ({len(confirmed)}): {confirmed}
Refuted (footnote 'we also considered'): {refuted}
Needs-human ({len(needs_human)}): {needs_human}

Triage:
  FIX-BEFORE-RUN (BLOCKER)   — data loss, checkpoint corruption, IP leak, ban cascade, unsafe mid-run crash
  MONITOR-DURING-RUN (WATCH) — add to monitor script; not blocking
  ACCEPT-RISK (DOCUMENTED)   — nit or theoretical edge, no concrete repro at scale
Top-level: GO | GO-WITH-CONDITIONS | NO-GO.

For EACH BLOCKER, TDD fix sketch in this repo's cadence:
  test: add failing test for X   — test name + one-line assertion signature
  fix|feat: implement X          — one-line change direction

Include:
  * hour-1  monitor (early ban-detection, warmup convergence)
  * hour-4  CHECKPOINT — THE single command that surfaces drift EARLIEST
            (candidates: checkpoint.db size trajectory vs. downloaded count,
             output/ byte rate vs 13GB/17h target, WAF-challenge-hit ratio in logs,
             fd count per container). Pick one, justify why it catches widest drift.
  * hour-12 monitor (cumulative resource exhaustion)
  * NEEDS_HUMAN smoke tests as pre-flight checklist
  * 'We also considered' footnote listing REFUTED (shows rigor)

Write:
  {SCRATCH}/REVIEW_VERDICT.md   (human)
  {SCRATCH}/REVIEW_VERDICT.json {{verdict, blockers, watches, accepted, hour4_checkpoint, needs_human, refuted_count}}""",
    tools=["Read", "Write"],
)
```

After the workflow completes, read `./scratchpad/REVIEW_VERDICT.md` and summarize BLOCKERS (if any) plus the hour-4 checkpoint command to the user for approval. Wait for the user's explicit go-ahead before launching the production scrape.
