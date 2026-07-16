HKLII downloader + viewer — resume prompt.

## Load context via subagents in parallel

Do NOT read these on the main thread. Spawn three forks in one message.

1. **Fork A — recent state + shipping.** Read
   `~/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md`
   and the newest close
   `memory/session-close-2026-07-09-D3-shipped.md`. Summarise: what
   shipped in the 2026-07-09 burst, what health-check on 2026-07-17
   confirmed, standing rules (pool-only, TDD, no push), and the
   ranked next-work options. Under 250 words.

2. **Fork B — quirks + gaps.** Read `docs/freshness-sanity-check.md`
   and `docs/ukpc-manual-download.md`. Summarise permanent HKLII
   quirks + phantom entries + UKPC 5-of-242 manual plan. Under 200 words.

3. **Fork C — D3 architecture as-shipped.** Read
   `src/hklii_downloader/d3.py` (ACTIVE_D3_FAMILIES, resolver_kind
   dispatch, D3Family spec) and `src/hklii_downloader/pcpdaab.py`
   (pcpd.org.hk discovery + fetch + save). Report the shipped shape
   so future work extends it consistently. Under 300 words.

Wait for all three. Do not answer the operator until grounded.

## Baseline (verify before any code)

```
cd ~/Developer/hklii_downloader
git status                                            # clean apart from scratchpad
git log --oneline origin/main..HEAD | wc -l           # 0 ahead of origin/main
uv run pytest --collect-only -q 2>&1 | tail -1        # 1160 tests collected

cd ~/Developer/hklii_viewer
git status
git log --oneline origin/worktree-local-viewer..HEAD | wc -l  # 59 ahead of its remote
git log --oneline HEAD..origin/main | wc -l                   # 83 behind main
```

If any drifts materially, stop and surface — a session may have shipped
new work.

## Where we are (as of 2026-07-17)

- **Downloader**: main @ `bca049d`, 0 ahead of origin, working tree
  clean apart from `scratchpad/`. **1160 tests passing.** No CI.
- **Corpus**: 162,713 cases · 9,464 legis in-force · 30,943 historical
  revisions · 3,014 hopt/D3 rows · 64 db_freshness rows. **33 GB on disk.**
- **D3 pipeline SHIPPED (2026-07-09)**: `ACTIVE_D3_FAMILIES` =
  `hklrccp` / `hklrcr` / `pcpdaab` / `pcpdc`. `pcpdaab` uses the new
  `resolver_kind='pcpd'` dispatch against pcpd.org.hk (735 PDFs on disk).
  `histlaw` + `hkiac` sit permanently `enabled=False` as provenance
  markers (HKLII source SPA-placeholders / hkiac.org restructured).
- **Freshness**: 64 db_freshness rows, all FRESH except intentional
  histlaw/hkiac STALEs and 2 documented single-row phantoms
  (hkts/1961/2 tc, pcpdaab/2019/8 tc).
- **VPN pool** (2026-07-17 probe): 20/20 containers healthy.
- **External hosts** (2026-07-17 probe): HKLII 200 ✓,
  pcpd.org.hk resolver URL 200 ✓, Judiciary judgment endpoint 200 ✓.
- **Viewer worktree** at `~/Developer/hklii_viewer`, branch
  `worktree-local-viewer`, HEAD `97bd09d`: 1446 tests pass, **59
  commits ahead of remote (unpushed) AND 83 behind origin/main**
  (missed the D3 push). `/freshness` page needs a merge + boot smoke
  test before publication.

## Standing rules (non-negotiable)

- **ALWAYS use the 20-proxy VPN pool (`127.0.0.1:8888-8907`)** for
  ANY HKLII / Judiciary / pcpd.org.hk probe. Never direct curl.
  Runners always route through `ProxyPool`.
- TDD strict: failing test → paste output → implement. Two commits
  per pair.
- Never modify a failing test to make it pass. Correct wrong-
  expectation tests in their own `test: correct expectation` commit.
- **Do NOT push to origin without ask.**
- Docstrings explain WHY, not WHAT. No emojis. Match surrounding style.

## Ranked next-work options (from 2026-07-17 grounding workflow)

Pick one; the ranking reflects value/effort/risk.

1. **[HIGH]** Viewer catch-up — merge `origin/main` into
   `worktree-local-viewer`, rerun 1446 tests, boot `hklii viewer
   index` + `hklii serve`, verify `/freshness` reflects the
   D3+pcpdaab reality, then ask before pushing 59 commits.
   Effort: ~1 session. Risk: merge conflicts around freshness
   schema likely.
2. **[MEDIUM]** Docs sync — refresh `docs/d3-runner-design.md`
   status table + D3Family spec, `docs/freshness-sanity-check.md:87`,
   `README.md` corpus counts + CLI subcommand list; rehome the
   6 committed source citations to `scratchpad/REVIEW_VERDICT.md` /
   `scratchpad/VALIDATOR_SPEC.md`. Effort: ~4-6 hours. Risk: low.
3. **[MEDIUM]** Structural refactor pair — extract
   `src/hklii_downloader/constants.py` (audit `_BASE_URL` × 11 and
   the scraper/enumerator 403-retry divergence at `scraper.py:30`
   vs `enumerator.py:22`), then split `CheckpointDB` (1935 lines,
   82 methods, 8 domains) into per-domain repos.
   Effort: ~2 hours + 1 session. Risk: god-object migration.
4. **[MEDIUM]** Permanent-gap taxonomy — model hkts/1961/2 tc,
   pcpdaab/2019/8 tc, UKPC 5 (1987/3, 1988/2, 1993/3, 1995/4,
   1997/4), hkdc/2019/128 EN-alt as `documented_gap` status so
   validators stop re-flagging permanent breakage.
   Effort: ~2-3 hours. Risk: schema change + migration.
5. **[LOW]** Deferred fills — manual UKPC 5-of-242 fetch from
   BAILII (recipe in `docs/ukpc-manual-download.md`); probe HKIAC
   for new URL pattern per `d3-live-wire-findings.md`.
   Effort: ~1-2 hours each. Risk: low.
6. **[LOW]** histlaw disposition — either ship the HKU Omeka
   resolver (design in `d3-alt-source-research.md`) OR write it
   off. Effort: ~1 session decision + variable ship time. Get
   operator's call before spending session on the resolver.

## Deliberate non-goals (unchanged)

- Do NOT ship a BAILII scraper for UKPC automated — report is manual.
- Do NOT generalise the `hkdc/2019/128` Judiciary-DOCX fix into a
  global fallback — one row, documented.
- Do NOT push to `origin/main` or `origin/worktree-local-viewer`
  without explicit ask.

## Ready

Run the three forks. When they return, run baseline, then say
"**D3 shipped, health-checked green — picking from ranked options**"
and ask the operator which of items 1–6 above to start on.
