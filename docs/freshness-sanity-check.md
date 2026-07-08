# Freshness-driven scraping — sanity check + known quirks

_Written 2026-07-08 after the Phase D2 (freshness) work landed._

The freshness gate (`hklii check-freshness` +
`--include-freshness-check` in `hklii update`) makes every scrape step
skip buckets whose HKLII live state already matches ours. This doc
captures the design invariants, the known quirks the gate has, and a
re-runnable sanity check anyone can point at a fresh corpus to prove
the wiring still holds.

---

## Design invariants

A **bucket** is a `(kind, scope, lang)` triple. `kind` is `cases`,
`legis`, or `hopt`; `scope` is a slug (`hkcfa`, `ord`, `hkiac`); `lang`
is `en` / `tc` / `sc`.

A bucket is **FRESH** iff all of:

| # | Rule | Column |
|---|---|---|
| a | probe error absent | `probe_error IS NULL` |
| b | wire count present | `live_count IS NOT NULL` |
| c | local count present | `local_count IS NOT NULL` |
| d | counts match | `live_count == local_count` |
| e | at least one clean scrape recorded | `last_scrape_completed_at IS NOT NULL` |
| f | live timestamp parsable | `live_updated_at parses cleanly` |
| g | upstream not newer than our scrape | `date(live_updated_at) <= date(last_scrape_completed_at)` in HKT |

Any missing signal → **STALE** (fail-safe). A first-run bucket (no
row) is STALE. A bucket with a probe error is STALE. See
`src/hklii_downloader/freshness.py::_fresh`.

## Plan ordering

Every profile plan puts `check_freshness()` at position 1. Downstream
scrape steps consume `db_freshness` via the shared filter helpers
(`_filter_fresh_case_buckets`, `_filter_fresh_hopt_buckets`), dropping
fresh scopes before enum/fetch.

| Step | Filter helper | Kind |
|---|---|---|
| `scrape` (case-family) | `_filter_fresh_case_buckets` | `cases` |
| `scrape_hopt` | `_filter_fresh_hopt_buckets(kind='hopt')` | `hopt` |
| `scrape_legis` | `_filter_fresh_hopt_buckets(kind='legis')` | `legis` |
| `scrape_ukpc` | `_filter_fresh_case_buckets(langs=('en',))` | `cases` (UKPC EN-only) |

The dispatcher only consults the freshness ledger when
`include_freshness_check` is on for the active profile. Custom
profiles that opt out get the pre-D2 full-sweep behaviour.

## Retry / catch-up paths

Freshness is bucket-granular. Pending/failed rows _inside_ a fresh
bucket are not retried by the daily/weekly/monthly cadence — the gate
only checks bucket-level count parity + timestamp.

Retry paths that bypass freshness (unchanged by D2):

| Step | Cadence | Retries |
|---|---|---|
| `recheck_html` | daily+ | doc-fallback rows whose HTML may now be extracted |
| `generate_html` | daily+ | local doc→HTML conversion queue |
| `enrich` | daily+ | press summary + appeal history, capped at `retry_limit` |
| `scrape_noteup` | daily+ | citation edges — enumerates fresh from downloaded rows |
| `backfill_case_translations` | daily+ | TC sidecars for bilingual EN rows |
| `backfill_legis_history` | monthly+ | non-latest capversions |
| `scrape_relatedcaps` | quarterly | ord→reg edges (fresh-diff) |
| `full_reconcile` | quarterly | full-corpus re-enum, ignores freshness |
| `orphan_mark` | quarterly | flip stale downloaded rows to orphaned |

Explicit `hklii scrape --retry-failed` also always retries regardless
of freshness state.

## Known quirks (design tradeoffs, not bugs)

| Quirk | Impact | Mitigation |
|---|---|---|
| UKPC has 5 permanent HKLII gaps (4× 404, 1 empty content). `local=237 vs live=242` never converges. | UKPC/en bucket is perpetually STALE → `scrape_ukpc` runs every weekly+ update. Costs ~5 wire calls per run. | Trivial cost; not worth engineering around. |
| **HKLII list-vs-count inconsistency**: `getmetahopt?dbcat=other&abbr=bahkg&lang=en` reports 218 but `gethoptfiles` returns 217 rows. Same for `hkts/tc` (266 vs 265). Observed 2026-07-08. | bahkg/en + hkts/tc perpetually STALE with `delta=+1`. `scrape_hopt` re-enumerates every weekly+ update (~2 pages of wire). | Trivial cost. Track via `db_freshness` — if HKLII fixes the inconsistency it'll self-heal. |
| **Narrow-window scrape misses older HKLII additions**. Daily/weekly use `recent_days=30`; monthly uses 90. Cases HKLII published to the archive outside those windows stay uncounted locally. Observed on hkdc/tc (+13) + hkcfi/tc (+2) after the initial D2 landing. | Real drift not closable by narrow-window scrape. Backfill helpers only fix "we have EN, HKLII shipped a TC counterpart" — they don't cover "HKLII shipped a new case we never enumerated". | Quarterly `full_reconcile` sweeps the full corpus regardless of window and closes these. |
| SC buckets (ord/reg/instrument) show `local=0 vs live=838 / 2253 / 63`. | Perpetually STALE, but never in any scrape step's target list (dispatcher hardcodes `('en', 'tc')` for legis). No wasted wire. | Correct: the freshness report surfaces the gap; no scrape happens because no SC scraper exists. |
| 6 newly-mapped "other"-bucket slugs (histlaw / hkiac / hklrccp / hklrcr / pcpdaab / pcpdc) have no scrape runners. | Perpetually STALE with `local=0`. No scrape step touches them. | Freshness honestly reports the gap. D3 backlog: ship runners. |
| `pd` shows `live=0 = local=0` → parity holds vacuously. | Would be marked FRESH if scraped. HKLII is genuinely empty for `pd` right now. | Freshness will correctly flip to STALE when HKLII adds `pd` content. |
| Freshness gate is bucket-granular. | Pending/failed rows inside a fresh bucket aren't retried by daily/weekly/monthly. | `full_reconcile` (quarterly) is the backstop. `hklii scrape --retry-failed` is the manual escape hatch. |
| UKPC's TC enum endpoint 500's, so `UkpcRunner` completes with `langs_enumerated=('en',)`. | Pre-2026-07-08: `_run_scrape_ukpc` still stamped `cases/ukpc/tc.last_scrape_completed_at`, creating a phantom row. | Fixed 2026-07-08 by iterating `outcome.langs_enumerated` instead of user-passed `langs`. Runner is now the sole source of truth for what got swept. |

## Re-running the sanity check

`scripts/freshness_sanity_check.py` is the re-runnable version of the
walkthrough. It reads `output/.checkpoint.db`, prints per-bucket
stale/fresh state with the reason, and simulates a post-scrape state
(marking every parity-holding bucket scraped today) so an operator
can see which buckets would drop out of each scrape step's target
list. All changes are reverted on exit — read-only in practice.

```
uv run python scripts/freshness_sanity_check.py
```

The three sections it prints:

1. **Current state** — every `db_freshness` row with its stale reason
   (`no-row`, `never-scraped`, `mismatch(...)`, `probe-err`, etc.).
2. **Simulated post-scrape scoping** — synthetically marks the
   `live == local` buckets scraped today and re-runs the dispatcher
   filter helpers. Confirms fresh buckets drop out and stale ones
   stay.
3. **Newly-mapped slugs** — flags the 6 D3-backlog slugs that show up
   in the freshness ledger but have no runner. Nothing scrapes them
   today.

## Wall-clock cost — `hklii update` per-step preflight

Empirically the biggest cost inside `hklii update --profile weekly`
is per-step ProxyPool preflight, not the scrape work itself. Each
scrape subcommand instantiates its own pool + runs an IP-echo
warmup against every proxy (~30 s for 20 proxies). Weekly's 10
scrape/enrich steps stack to 5–7 minutes of preflight overhead
before any real work runs; monthly + quarterly add more.

If you're validating the freshness pipeline end-to-end and don't
want to wait, run the individual subcommands instead — each does
one preflight, e.g.:

    hklii scrape --skip-if-fresh -o output -p …×20
    hklii scrape-hopt --skip-if-fresh -o output -p …
    hklii scrape-legis --skip-if-fresh -o output -p …
    hklii scrape-ukpc -o output -p …           # permanent-gap loop, still fast
    hklii check-freshness --report -o output -p …

A future refactor could share one ProxyPool across the dispatcher's
steps — filed as a follow-up.

## When to re-run

- After any change to `src/hklii_downloader/freshness.py`,
  `checkpoint.py::db_freshness accessors`, or the `_dispatch_update_plan`
  scrape branches.
- After a major HKLII change (new slug, new endpoint family, new
  dbcat variant).
- Whenever the `check-freshness` output looks suspicious.

## Related tests

- `tests/test_freshness.py` — parser, dispatch table, `_fresh`
  predicate, `probe_all`.
- `tests/test_freshness_checkpoint.py` — `db_freshness` schema, upsert
  discipline, `recompute_local_count` (incl. the TC sidecar walk).
- `tests/test_freshness_cli.py` — CLI + update dispatcher wiring:
  freshness step precedes scrapes, `_run_update_scrape` scopes by
  freshness, per-runner `mark_bucket_scraped` invariants.
- `tests/test_ukpc.py::TestUkpcRunResultLangsEnumerated` — the
  UKPC-specific tightening: only successful-enum langs propagate to
  `mark_bucket_scraped`.
