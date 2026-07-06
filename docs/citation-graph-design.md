# HKLII Citation Graph — Design Doc

*Produced 2026-07-06 by parallel multi-agent workflow (RAG lens / storage lens /
browser lens / live API probe) synthesised into this design.*

## 1. Executive summary

Two new scrapers (`scrape-noteup`, `scrape-relatedcaps`) build the citation and
ordinance-implementation graph on top of the completed judgment corpus.
Storage stays pure SQLite in `checkpoint.db`; JSON snapshots on disk mirror
existing per-case folders for reproducibility. The graph unlocks a killer
local-viewer feature (ordinance-section interpretation with treatment tags)
and boosts RAG retrieval via authority-weighted rerank, all at ~150 MB extra
storage and ~22 min total wall-clock through the existing 20-endpoint VPN pool.

## 2. Data model

### 2.1 SQLite tables (added to `checkpoint.db`)

```
citations
  from_case_id     INTEGER   -- FK cases.id (the citer)
  to_case_id       INTEGER   -- FK cases.id (target of getcasenoteup call)
  citer_lang       TEXT      -- 'en'|'tc' derived from citer path
  citer_freq_at_fetch INTEGER  -- HKLII-side snapshot; NOT our recomputed count
  position         INTEGER   -- ordinal in HKLII response
  first_seen       TEXT      -- ISO-8601
  PRIMARY KEY (from_case_id, to_case_id, citer_lang)
) WITHOUT ROWID;
CREATE INDEX idx_cit_reverse ON citations(to_case_id, from_case_id);
CREATE INDEX idx_cit_lang    ON citations(citer_lang, from_case_id);

case_parallel_cites
  case_id   INTEGER
  cite      TEXT   -- "[2021] 6 HKC 46"
  PRIMARY KEY (case_id, cite)
) WITHOUT ROWID;

ord_reg_edges
  parent_cap  TEXT   -- "622"
  child_cap   TEXT   -- "622A"
  lang        TEXT   -- 'en'|'tc'
  title       TEXT   -- captured for change detection
  first_seen  TEXT
  PRIMARY KEY (parent_cap, child_cap, lang)
) WITHOUT ROWID;
CREATE INDEX idx_ore_child ON ord_reg_edges(child_cap);

citation_hub_cache
  case_id       INTEGER PRIMARY KEY
  inbound_count INTEGER
  computed_at   TEXT

noteup_fetches
  case_id     INTEGER PRIMARY KEY
  status      TEXT     -- 'ok'|'pending'|'error:{reason}'
  fetched_at  TEXT
  edge_count  INTEGER
  raw_bytes   INTEGER
  http_status INTEGER

relatedcap_fetches
  cap_number  TEXT
  abbr        TEXT     -- 'ord'|'reg'
  lang        TEXT
  status      TEXT
  fetched_at  TEXT
  edge_count  INTEGER
  PRIMARY KEY (cap_number, abbr, lang)
```

Schema notes: `treatment` slot is deliberately **not** in v1 (see open
questions). `citations` stores citer→target because that's the API's natural
direction; forward edges ("cases I cite") come by reversing the index — no
separate table.

### 2.2 On-disk JSON layout

```
scrape_output/{court}/{year}/{num}/
  getjudgment.json            (already exists)
  getcasenoteup.json           NEW — raw array, atomic write

legis_output/{abbr}/{cap}/{lang}/
  getrelatedcaps.json          NEW — raw array per (abbr, cap, lang)
```

Atomic writes via `tmp.<uuid>` + `os.replace`. Bytes archived so we can
rebuild SQLite from disk without re-hitting the API.

## 3. Two new scrapers

### 3.1 `hklii scrape-noteup`

```
hklii scrape-noteup [--court CFA CA CFI ...] [--year YYYY]
                    [--limit N] [--workers 20] [--resume]
                    [--refetch-stale DAYS] [--dry-run]
```

| Concern | Decision |
|---|---|
| Wire path | ProxyPool (20-endpoint gluetun) with same jitter + rotation as `scrape` |
| Idempotency | `noteup_fetches(case_id)` PK; resume skips `status='ok'` |
| Membership guard | Iterate cases from CheckpointDB where `status='ok'` — never call for a case we don't own. Fixes silent-empty conflation |
| `lang` param | Never sent — API ignores it; single call yields mixed-lang response |
| Edge insert | `INSERT OR IGNORE` on `citations` under one transaction per case |
| Empty response | Written to JSON as `[]`, `edge_count=0`, `status='ok'` — legitimate zero |
| HTTP 500 / timeout | Retryable via existing scraper retry set, capped at 5 attempts; then `status='error:...'` |
| Logging | `StructuredEventLogger` events: `noteup.fetch.start`, `.ok`, `.retry`, `.fail`, one per case |
| Post-hook | On job completion, refresh `citation_hub_cache` via single `REPLACE INTO … GROUP BY to_case_id` — ~2 s across all edges |

### 3.2 `hklii scrape-relatedcaps`

```
hklii scrape-relatedcaps [--abbr ord reg] [--lang en tc]
                         [--cap-range 1-1200] [--workers 20]
                         [--resume] [--dry-run]
```

| Concern | Decision |
|---|---|
| Cap enumeration | Numeric caps 1..1200 (bounded above by known HK cap ceiling). **Alpha-suffix caps excluded at param-build** — API returns raw 500, would poison retry loop |
| Two-dimensional call plan | For each `cap ∈ 1..1200`: 4 calls = {ord, reg} × {en, tc}. `ord` is a self-lookup used only to prove cap existence and capture title in both langs |
| Idempotency | `relatedcap_fetches(cap, abbr, lang)` composite PK |
| Edge extraction | Only `abbr=reg` responses populate `ord_reg_edges` (parent = query cap, child = each returned `num`) |
| Ordinance registry | `abbr=ord` responses upsert into a small `ordinances(cap, title_en, title_tc, first_seen)` side table (bootstraps the ord node set for the browser without a separate legis scrape) |
| Empty → not-a-cap | `[]` from `abbr=ord` means cap doesn't exist; row still written with `status='ok', edge_count=0` for negative-cache |

## 4. Query API contract

Two consumer surfaces: `viewer/` (HTMX server) and `rag/` (retrieval
pipeline). Both go through a thin helper module — SQLite only, no ORM.

### 4.1 Python helpers (in `hklii/graph.py`)

| Function | Returns |
|---|---|
| `hub_cases(court=None, min_inbound=5, limit=100)` | list of (case_id, inbound_count) |
| `cited_by(case_id, court_filter=None, order='authority')` | list of citer rows w/ court, date, freq |
| `authorities_cited(case_id)` | list of target rows |
| `interpreting_cases(cap, section=None)` | list of case rows citing that cap[/section] (v2) |
| `child_regulations(cap, lang='en')` | list of (child_cap, title) |
| `parent_ordinance(child_cap, lang='en')` | (parent_cap, title) or None |
| `appeal_chain(case_id)` | ordered list from existing `appeal_history` table |
| `shortest_path(a, b, max_hops=6)` | list of case_ids |
| `neighbourhood(case_id, hops=1)` | set of case_ids |

### 4.2 SQL views (ad-hoc / notebook use)

- `v_citation_edges_bi` — UNION collapsing en/tc citer_lang (dedup on from,to)
- `v_citation_hubs_by_court` — hub cache joined w/ cases.court, ranked
- `v_ord_tree` — parent/child adjacency, both langs

## 5. RAG integration points

| Pipeline stage | Consumer of graph |
|---|---|
| **Ingest / metadata** | `citation_hub_cache.inbound_count` becomes a case-level metadata field on every chunk |
| **Rerank** | Score = `base_similarity + α·log(1+inbound) + β·court_authority(target) + γ·exp(-λ·years_gap)` |
| **Statute-first expansion** | Query embedding hits `Cap.X` chunk → `child_regulations(X)` fans out plus `interpreting_cases(X)` when v2 lands |
| **1-hop context expansion** | After top-k, call `authorities_cited(top1)` for supporting-context blocks |
| **De-dup at query time** | `case_parallel_cites` collapses "[2021] 6 HKC 46" and "[2020] HKCFA 6" to a single canonical case_id before retrieval |
| **Currency filter** | `appeal_chain(case_id)` last-hop `overturned` flag downweights obsolete authorities |

Span snippets and treatment labels — deliberately **out of v1**.

## 6. Browser integration points

```
CASE-DETAIL /case/{court}/{year}/{num}
  Signal strip          → hub_cases(this) + appeal_chain last-hop status
  Appeal chain strip    → appeal_chain(case_id)
  Tab: Cited by         → cited_by(case_id, order='authority')  [hx-get, paged]
  Tab: Authorities cited → authorities_cited(case_id)            [hx-get]
  Tab: Regulations touched → case_cap_refs (v2)
  Hover previews on citations → cases lookup by neutral cite (hx-get once)

ORD-SECTION /ord/cap/{n}/s/{sec}
  Interpreting cases (sorted CFA→CA→CFI/DC)
                        → interpreting_cases(cap, section)       [v2]
  Child regulations     → child_regulations(cap)                  [v1]

HUB-CASES INDEX /authorities
  Ranked list per court → hub_cases(court, limit=50)

CITE RESOLVER /cite/{neutral}
  One-hop redirect      → cases lookup + case_parallel_cites
```

HTMX lazy-loading for cited-by keeps first paint <100 ms even on CFA hubs
with 500+ inbound edges.

## 7. Rollout plan

| Phase | Scope | Purpose | Est. wall-clock |
|---|---|---|---|
| **0. TDD** | Write failing tests: response parser (fixtures from the probe artifacts), CheckpointDB idempotency, `lang`-ignored assertion, 500-on-alpha-suffix skip | Lock behavioural contract before wire calls | 1 dev-day |
| **1. Canary** | `--court CFA --limit 100` | Confirm 4.8 rows/sec/worker still holds and edge counts match probe | ~1 min |
| **2. High-authority sweep** | All CFA + CA cases (~4k) | Front-load high-inbound targets so hub cache is useful early | ~1 min |
| **3. Full corpus sweep** | Remaining ~114k cases | Bulk fetch, resume-safe | ~20 min |
| **4. Relatedcaps sweep** | 1..1200 × {ord,reg} × {en,tc} = 4,800 calls, mostly `[]` | Cheap, do last | ~1 min |
| **5. Hub cache refresh** | Single `REPLACE INTO` | Post-hook | ~2 s |
| **6. Verify pass** | New `hklii verify --graph`: row-count sanity, no orphan `from_case_id`, all `citer_lang` in `{'en','tc'}` | Ship gate | ~30 s |

Pool safety: reuse existing session-kill circuit breaker from
incident 2026-07-05. StructuredEventLogger emits per-batch throughput.

## 8. Deliberate non-goals

| We are NOT building | Reason |
|---|---|
| Forward-edge storage (separate "cites →" table) | Trivially derivable by reversing `citations` reverse-index; storing twice invites divergence |
| Treatment labels (applied / distinguished / overruled) | Requires HTML span extraction + classifier; ship schema slot in v2 with labelled eval set |
| Character-level pinpoint offsets | ¶-level enough for practitioners; wire cost + brittleness not worth it |
| Foreign citation resolution (UKHL/PC/HCA/etc.) | Out-of-corpus; high failure rate; store as opaque strings only |
| PageRank / betweenness on ingest | Inbound count is 90% as informative; export to NetworkX pickle on demand |
| Separate en/tc noteup calls | API ignores `lang` — MD5-identical responses. One call per case |
| Alpha-suffix cap enumeration on `reg` endpoint | Raw 500s poison retry loop; enumerate integer parents only |
| Second graph store (Kuzu / sqlite-graph / on-disk NetworkX) | Adds packaging + query-language surface for zero measured benefit at 300k edges |
| Interactive graph explorer | Force-directed layouts open twice, then never; lawyers work in ranked lists |
| Counsel / firm / listing metadata | No consumer view; not doctrinally relevant |

## 9. Estimated cost

| Metric | Value |
|---|---|
| noteup calls | **118,188** (one per corpus case — NOT 324k; API ignores `lang`) |
| relatedcaps calls | 4,800 |
| Total wire calls | **~123k** |
| Throughput | 4.8 rows/sec × 20 workers = 96 rows/sec |
| **Total wall-clock** | **~22 min** (~35 min worst case with retry pressure) |
| Edge rows (citations) | ~250-400k (median 0, CFA/CA heavy tail) |
| Edge rows (ord_reg) | ~2-3k |
| SQLite delta | ~30-50 MB (edges + hub cache + indexes) |
| JSON on disk | ~60-100 MB (raw responses, atomic-written) |
| **Total storage delta** | **~150 MB** |

## 10. Open questions

1. **Snapshot vs weekly refresh?** One-shot fetch is 22 min; a weekly cron on
   new-cases-only would cost <2 min but require a "cases added since last run"
   query.

2. **Treatment slot in v1 schema, or defer?** Adding `treatment TEXT NULL` to
   `citations` costs nothing now and avoids a migration when the classifier
   lands. Recommendation: add the column, leave NULL, populate in v2.

3. **Ordinance sections as first-class sub-nodes, or pinpoint-only on edges?**
   Full section extraction requires parsing every judgment body for `s.\d+`
   references. Sub-nodes let queries hit `Cap.622 s.465` directly;
   pinpoint-only is cheaper but loses statute-first retrieval. Section
   extraction is ~1 dev-week; sub-node schema adds ~500k rows.

## Appendix: critical findings from live API probe

- **`getcasenoteup` ignores `lang`.** A single call returns citing judgments
  from BOTH English AND Chinese corpora merged. Halves the fetch budget.
- **`getcasenoteup` never 4xx's.** Returns `[]` for zero-citation AND
  nonexistent case AND bad params. Scraper must validate corpus membership
  before treating `[]` as ground truth.
- **`getrelatedcaps` directionality is parent-ord → children-regs only.**
  `abbr=reg&num_int=32A` returns raw 500 because `num_int` doesn't parse
  letter suffixes — exclude at param-build.
- **The ord→reg relationship is 100 % numeric-suffix-encoded.** Cap 32 →
  32A/B/C/D; Cap 622 → 622A-J. This means we can DERIVE the entire
  ord_reg_edges table from the 6,310 legis rows already on disk with a single
  SQL query. **Whether to skip `scrape-relatedcaps` entirely and derive
  locally is worth deciding before build** — it saves 4,800 wire calls and
  1 min at the cost of missing any manually-curated exceptions HKLII might
  have (none observed in probes so far).
