# `checkpoint.db` — scraper state DB (SQLite)

Not a JSON Schema — SQL is native, so the source of truth is the DB
itself. Extract with:

```bash
sqlite3 output/.checkpoint.db .schema
```

**Location:** `output/.checkpoint.db` (dot-prefixed inside the corpus
directory; the top-level `checkpoint.db` at the repo root is a stub from
an earlier layout).

**Purpose:** authoritative state for the scraper — every fetch attempt,
every citation edge, every freshness signal. The on-disk JSON/HTML files
are derived artifacts. If they get deleted, `hklii verify` reconciles
against this DB.

## Tables (11)

| Table | Rows (2026-07 run) | Purpose |
|---|---:|---|
| `cases` | 162,713 | Per-judgment state for the 13 mainline courts. PK `(court, year, number)`. Tracks fetch status, formats saved, enrichment status (summaries + appeal history), and the `html_pending_at_hklii` flag used by the recheck runner. |
| `noteup_fetches` | 162,424 | One row per case whose noteups have been fetched. PK `(court, year, number)`. `edge_count` = number of inbound citations captured. |
| `citations` | 242,488 | Outbound citation edges from noteup responses. PK `(from_key, to_key, citer_lang)`. Keys are `"court/year/number"` strings. `citer_freq` is HKLII's snapshot of citation frequency at fetch time. |
| `case_parallel_cites` | 11,617 | Per-case parallel citations extracted at scrape time. PK `(case_key, parallel_cite)`. |
| `hopt_documents` | 3,014 | State for HOPT and active D3 rows. PK `(abbr, year, num, lang)`. D3 rows land here because `db_freshness.recompute_local_count` joins on `hopt_documents.abbr = scope` — see `docs/d3-runner-design.md`. |
| `legis_documents` | 9,464 | In-force legislation instruments. PK `(abbr, num, lang)`. `latest_vid` + `latest_version_date` mirror the newest entry in `.versions.json`. |
| `legis_versions` | 30,943 | Historical version rows. PK `(abbr, num, lang, vid)`. One row per `.v{vid}.content.json` file. |
| `ord_reg_edges` | 4,506 | Ordinance → subsidiary regulation edges from `getrelatedcaps`. PK `(parent_cap, child_cap, lang)`. |
| `relatedcap_fetches` | 4,800 | Per-cap fetch state for `getrelatedcaps`. PK `(cap_number, abbr, lang)`. |
| `enum_runs` | 8 | Audit trail — one row per `BulkScraper.enumerate()` invocation. `generation_id` is autoincrement PK; used by `orphan_mark` to identify the freshest clean full-corpus enum. |
| `db_freshness` | 64 | Phase-D2 freshness ledger. PK `(kind, scope, lang)` where `kind ∈ {cases, legis, hopt}`. See `docs/freshness-sanity-check.md` for the design invariants — column ownership is strictly split three ways (probe / local-count / scrape-run) and drift silently corrupts the freshness signal. |

## Selected schemas

### `cases`

```sql
CREATE TABLE cases (
    court    TEXT NOT NULL,
    year     INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    neutral  TEXT NOT NULL,
    title    TEXT NOT NULL,
    date     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    formats  TEXT,                 -- JSON list e.g. ["html","txt","json","doc"]
    error    TEXT,
    lang     TEXT NOT NULL DEFAULT 'en',
    last_seen_at INTEGER,
    summary_en_status     TEXT NOT NULL DEFAULT 'pending',
    summary_en_error      TEXT,
    summary_zh_status     TEXT NOT NULL DEFAULT 'pending',
    summary_zh_error      TEXT,
    appeal_history_status TEXT NOT NULL DEFAULT 'pending',
    appeal_history_error  TEXT,
    html_pending_at_hklii INTEGER,
    html_generated_from   TEXT,
    html_generated_error  TEXT,
    PRIMARY KEY (court, year, number)
);
```

`status` transitions: `pending → downloaded | failed | orphaned`.
`formats` is a JSON list of the formats actually saved to disk (subset
of `{html, txt, json, doc}`). `html_pending_at_hklii` is a UNIX timestamp
set when the initial fetch got an empty `content` field — the daily
`recheck_html` step polls these until HKLII has done its HTML rendering.

### `db_freshness`

```sql
CREATE TABLE db_freshness (
    kind                     TEXT NOT NULL,   -- 'cases' | 'legis' | 'hopt'
    scope                    TEXT NOT NULL,   -- court slug | cap type | hopt abbr
    lang                     TEXT NOT NULL,   -- 'en' | 'tc' | 'sc'
    live_count               INTEGER,
    live_updated_at          TEXT,            -- HKLII wire timestamp
    live_probed_at           INTEGER,
    probe_error              TEXT,
    local_count              INTEGER,
    local_counted_at         INTEGER,
    last_scrape_completed_at INTEGER,
    source_generation_id     INTEGER,         -- FK enum_runs.generation_id
    PRIMARY KEY (kind, scope, lang)
) WITHOUT ROWID;
```

A bucket is FRESH iff: `probe_error IS NULL AND live_count IS NOT NULL
AND local_count IS NOT NULL AND live_count == local_count AND
last_scrape_completed_at IS NOT NULL AND live_updated_at is parseable AND
date(live_updated_at) <= date(last_scrape_completed_at)`. Any missing
signal → STALE (fail-safe). See `src/hklii_downloader/freshness.py::_fresh`
and `docs/freshness-sanity-check.md` for the full invariant.

### `citations`

```sql
CREATE TABLE citations (
    from_key   TEXT NOT NULL,          -- "hkcfi/2023/155" (citer)
    to_key     TEXT NOT NULL,          -- "hkcfa/2020/32" (target)
    citer_lang TEXT NOT NULL,          -- 'en' | 'tc'
    citer_freq INTEGER,                -- HKLII citation_frequency snapshot
    position   INTEGER,                -- ordinal in getcasenoteup response
    first_seen TEXT NOT NULL,
    PRIMARY KEY (from_key, to_key, citer_lang)
) WITHOUT ROWID;
CREATE INDEX idx_cit_to ON citations(to_key);
```

Forward-index by `(from_key)` via the PK; reverse-index by `(to_key)` via
`idx_cit_to` — supports both "what does case X cite?" and "who cites case
X?" without a full table scan.

## Locking

`output/.hklii.lock` and `output/.checkpoint.db.lock` are fcntl exclusive
locks held for the duration of a scrape / update / verify run. Zero-byte
by design.
