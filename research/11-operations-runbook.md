# Operations Runbook

This chapter is the operator-facing manual: how to bring the pool up, how to canary, how to kick off the full production run, how to monitor it, how to recover after a crash, and what to do afterwards. It documents the four CLI subcommands (`download`, `scrape`, `verify`, `enrich`) at flag granularity, spells out the pre-flight checklist, gives a measured wall-clock estimate, and names the operational gaps that are not yet automated.

For the internal design of the pieces this chapter drives — the `BulkScraper` loop, the checkpoint schema, the retry/backoff formula — see [Scraper Architecture](./09-scraper-architecture.md). For what `hklii verify` semantically checks against the on-disk files, see [Content Safeguards](./10-content-safeguards.md).

## CLI subcommand overview

`hklii` is a Click group with four subcommands. Three of the four (`download`, `scrape`, `enrich`) talk to HKLII or Judiciary over the network and therefore require an explicit choice of `--proxy` or `--direct` — leaving both off raises `click.UsageError` (`src/hklii_downloader/cli.py:84-85`, `:218-219`, `:348-349`). The fourth (`verify`) is offline and only touches the checkpoint DB and the local filesystem.

| Subcommand | Purpose | Network? | Proxy required? |
|---|---|---|---|
| `hklii download URL...` | Fetch one or more specific case URLs and save chosen formats | Yes | Yes (proxy XOR direct) |
| `hklii scrape` | Enumerate courts, then bulk-download every pending case | Yes | Yes (proxy XOR direct) |
| `hklii verify` | Reconcile `status='downloaded'` checkpoint rows against on-disk files | No | No — offline |
| `hklii enrich` | Backfill press summaries + appeal history for already-downloaded cases | Yes | Yes (proxy XOR direct) |

`--proxy` and `--direct` are mutually exclusive: the shared `MutuallyExclusiveOption` at `cli.py:16-20` raises `UsageError("--proxy and --direct are mutually exclusive.")` if both are set. For `scrape` and `enrich`, `--direct` also prints a confirmation prompt unless `-y`/`--yes` is passed (`cli.py:221-225`, `:351-355`) — a deliberate speed bump because a direct run exposes the operator's home IP.

## `hklii download` — targeted URL fetch

Fetch one or more specific case URLs. Used for spot-checks and small pulls, not for corpus-scale work.

**Flags** (`cli.py:31-88`):

| Flag | Default | Notes |
|---|---|---|
| `URLS...` (positional) | required, `nargs=-1` | One or more HKLII case URLs, e.g. `https://www.hklii.hk/en/cases/hkcfa/2023/32` |
| `-o, --output PATH` | `./downloads` | Output directory |
| `-f, --format {html,txt,json,doc}` | `html`, `txt`, `json` | Repeatable. All four formats are allowed in this subcommand — there is no `--allow-doc` gate here |
| `-p, --proxy URL` | none | Single proxy (SOCKS5 or HTTP). Mutually exclusive with `--direct` |
| `--direct` | off | Direct connection — mutually exclusive with `-p` |
| `-c, --concurrency N` | 5 | `DEFAULT_CONCURRENCY = 5` at `cli.py:13` |

Passing neither `--proxy` nor `--direct` raises `UsageError("Must specify --proxy or --direct.")` at `cli.py:84-85`.

Under the hood, `download` uses `make_async_client` (`src/hklii_downloader/client.py:28-36`) which builds a plain `httpx.AsyncClient(http2=True, follow_redirects=True, trust_env=False, timeout=30, headers=_BROWSER_HEADERS)`. The hardcoded Chrome-148 UA at `client.py:14-25` is deliberate — Judiciary's legacy F5 WAF used to drop any UA containing `python`, and while the current bare-Apache origin no longer does (see [HKLII Platform](./01-hklii-platform.md)), the hardcode is belt-and-suspenders. Note this subcommand does *not* go through `ProxyPool`: no curl_cffi impersonation, no throttler, no warm-up. It is intentionally lightweight for interactive use.

Example:

```bash
uv run hklii download --direct \
  https://www.hklii.hk/en/cases/hkcfa/2023/32 \
  https://www.hklii.hk/en/cases/hkcfi/2024/1000
```

## `hklii scrape` flag inventory

The bulk subcommand. All 16 flags in the order they appear in `cli.py:94-249`:

| Flag | Default | Behavior |
|---|---|---|
| `-o, --output PATH` | `./downloads` | Output root. `.checkpoint.db` and `scrape.log` land here |
| `-f, --format` (repeatable) | `html`, `json`, `txt` | Choice among `html`/`json`/`txt`/`doc`. `doc` is silently dropped with a yellow warning unless `--allow-doc` is set (`cli.py:228-230`) |
| `-p, --proxy` (repeatable) | none | One `-p` per proxy. Mutually exclusive with `--direct` |
| `--direct` | off | Direct connection — one worker only |
| `--courts CSV` | `hkcfi,hkca,hkdc,hkcfa` (`DEFAULT_COURTS` at `cli.py:90`) | Comma-separated. Covers ~97% of the confirmed corpus |
| `--limit N` | none | Cap the run at N downloads (canary / smoke test) |
| `--allow-doc` | off | Unlocks `.doc`/`.docx` as a saveable format |
| `--resume` | off | Skip enumeration if pending rows exist — see semantics below |
| `-y, --yes` | off | Skip the direct-mode confirmation prompt |
| `--with-summaries` | off | Fetch English + Chinese press summaries inline for every case |
| `--with-appeal-history` | off | Fetch `/api/getappealhistory?caseno=...` inline for every case |
| `--lang {en,tc,both}` | `both` | Which language(s) to enumerate. `both` maps to the tuple `('en', 'tc')` at `cli.py:233` |
| `--retry-failed` | off | Flips existing `status='failed'` rows back to `pending` before starting (`cli.py:576-578` -> `db.reset_failed_to_pending()`) |
| `--enum-max-age HOURS` | 0 | Skip re-enumeration of `(court, lang)` pairs enumerated within HOURS. `0` = always re-enumerate |
| `--save-enum-responses` | off | Write raw `getcasefiles` JSON pages to `<output>/.enum_cache/{court}_{lang}/{ts}_pageNNNN.json` for provenance |

The `-f doc` interaction is worth naming explicitly: choosing `-f doc` without `--allow-doc` is *not* fatal. The scraper drops `doc` from the set with `click.secho("Note: .doc disabled in bulk mode. Use --allow-doc to enable.", fg="yellow", err=True)` (`cli.py:229-230`) and continues with whatever formats remain. If no formats survive the pruning, downstream `BulkScraper` still runs but produces nothing but metadata JSON.

## `hklii scrape` worker sizing

Worker count is derived, not configured:

- **Proxy mode:** `workers = max(1, len(healthy_proxies))` — one worker per proxy that survived the preflight IP-leak check (`cli.py:561`). If the pool has 20 healthy proxies, 20 workers fan out; if it has 3, 3 workers.
- **Direct mode:** `workers = 1`, hardcoded (`cli.py:541-542`). A single-worker direct run is deliberately conservative — direct mode exposes the operator's home IP, so we do not stack request rate on top of that.

The `max(1, ...)` guard ensures the code path stays sane if preflight returns zero healthy proxies. However, when preflight returns zero and we are not in direct mode, the CLI aborts before ever reaching the worker-count line: `cli.py:556-560` raises `UsageError("No healthy proxies after preflight — every proxy was leaked or unreachable. Fix the pool (or use --direct) and retry.")`.

There is no `--concurrency` flag on `scrape` — this is deliberate. Adding more workers than proxies collapses onto shared exit IPs and defeats the anti-detection posture; adding fewer wastes pool capacity. The bare pool-size fan-out is the right knob.

## `hklii scrape --resume` semantics

`--resume` does **not** unconditionally skip enumeration. It skips enumeration only if the checkpoint already has pending work:

```python
# cli.py:583-589
pre_stats = db.stats()
if resume and pre_stats["pending"] > 0:
    click.echo(f"Resume: skipping enumeration; {pre_stats['pending']} pending cases already in DB.")
else:
    click.echo(f"Enumerating courts: {', '.join(court_list)}  langs: {', '.join(langs)}")
    total = await scraper.enumerate(court_list, langs=langs)
    click.echo(f"Found {total} cases.")
```

Interpretation:

- `--resume` + pending rows exist -> skip enumeration, get straight to downloading.
- `--resume` + no pending rows (fresh DB, or previous run completed everything) -> still re-enumerate. This is the right behaviour: if the checkpoint says everything is downloaded, "resume" almost certainly means "check for new cases and pick up any that landed since last run".
- No `--resume` + pending rows exist -> re-enumerate anyway, upserting `last_seen_at` timestamps and picking up any new HKLII listings.

Independently of `--resume`, `db.release_in_progress()` runs on every invocation (`cli.py:591` calls into `checkpoint.py:255-259`) to reclaim any `in_progress` rows left behind by a crashed prior run. That reclaim is what makes `--resume` idempotent after a hard kill.

For faster resumes, pair `--resume` with `--enum-max-age 24`: even in the "no pending rows" fallthrough branch, the `enum_max_age_hours` window inside `BulkScraper.enumerate` (`src/hklii_downloader/scraper.py:116-124`) will short-circuit any `(court, lang)` that was enumerated within the window.

## `hklii verify`

Offline reconciliation. Only one flag: `-o, --output PATH` (default `./downloads`). Behaviour (`cli.py:259-283`):

1. Loads `.checkpoint.db` from the output directory (or `UsageError` if missing).
2. Runs `db.verify_downloaded_against_files(output)` — iterates every `status='downloaded'` row, checks each expected format file exists and is non-zero-byte at `<output>/<court>/<year>/<court>_<year>_<number>.<ext>` (with `.doc` -> `.docx` fallback), and flips broken rows back to `status='pending'` (`checkpoint.py:220-245`).
3. Prints `Verified <output>. Broken rows flipped to pending: N`.
4. Prints post-verify `stats` (total / pending / in_progress / downloaded / failed counts).

Rerun `hklii scrape --resume ...` afterwards to re-download the freshly-`pending` rows.

`verify` intentionally does NOT check enrichment sidecars (`.summary_en.html`, `.summary_zh.html`, `.appeal_history.json`) — those live in the per-kind enrichment status columns (`summary_en_status`, etc.) and are re-runnable via `hklii enrich`. See [Content Safeguards](./10-content-safeguards.md) for the full validation surface `verify` covers and the gaps it does not (no SHA-256, no content-shape validation, no captcha-HTML detection).

## `hklii enrich`

Backfill press summaries and appeal history for cases already marked `status='downloaded'`. Reads existing on-disk sidecars (`{stem}.html`, `{stem}.json`) rather than re-fetching judgments. Flags (`cli.py:286-370`):

| Flag | Default | Behavior |
|---|---|---|
| `-o, --output PATH` | `./downloads` | Must contain `.checkpoint.db` |
| `-p, --proxy` (repeatable) | none | One per proxy |
| `--direct` | off | Direct connection |
| `--summaries / --no-summaries` | on | Backfill EN + ZH press summaries |
| `--appeal-history / --no-appeal-history` | on | Backfill `/api/getappealhistory` JSON |
| `--limit N` | none | Stop after N cases |
| `-y, --yes` | off | Skip direct-mode confirmation |

Passing `--no-summaries --no-appeal-history` raises `UsageError("Nothing to do — pass --summaries or --appeal-history (or both).")` at `cli.py:357-360`.

Preflight and worker sizing mirror `scrape`: `workers = max(1, len(healthy_proxies))` for proxy mode (`cli.py:408`), `workers = 1` for direct mode (`cli.py:393`).

`EnrichmentRunner` at `src/hklii_downloader/enrichment.py:109-227` is a strict backfill path — it never fetches judgments. If the `.html` sidecar for a case is missing on disk it marks any pending `summary_*` rows `failed` with error `"html file missing on disk"` (`:190-196`). If the `.json` sidecar is missing it marks `appeal_history` failed with `"json sidecar missing on disk"` (`:207-211`); if the JSON exists but has no `case_number` it fails with `"case_number missing in json sidecar"` (`:216-220`). Running `hklii verify` before `hklii enrich` catches the disk-drift case before enrichment burns its budget on doomed rows.

## Full production command

The recommended full-corpus invocation. All 20 gluetun containers on ports 8888-8907, both languages, all enrichment, provenance snapshots, 24h enum cache:

```bash
uv run hklii scrape -o ./downloads \
  -p http://127.0.0.1:8888 -p http://127.0.0.1:8889 \
  -p http://127.0.0.1:8890 -p http://127.0.0.1:8891 \
  -p http://127.0.0.1:8892 -p http://127.0.0.1:8893 \
  -p http://127.0.0.1:8894 -p http://127.0.0.1:8895 \
  -p http://127.0.0.1:8896 -p http://127.0.0.1:8897 \
  -p http://127.0.0.1:8898 -p http://127.0.0.1:8899 \
  -p http://127.0.0.1:8900 -p http://127.0.0.1:8901 \
  -p http://127.0.0.1:8902 -p http://127.0.0.1:8903 \
  -p http://127.0.0.1:8904 -p http://127.0.0.1:8905 \
  -p http://127.0.0.1:8906 -p http://127.0.0.1:8907 \
  --with-summaries --with-appeal-history \
  --save-enum-responses --enum-max-age 24 \
  --lang both
```

What each flag buys:

- **20 `-p`** — one worker per healthy proxy. Preflight will reject any leaked or unreachable containers, so if the pool is degraded the run still starts with the survivors.
- **`--with-summaries --with-appeal-history`** — enrichment inline during scrape. Cheaper than a `hklii enrich` backfill pass because the sidecar HTML is already in memory when we extract Press Summary anchors.
- **`--save-enum-responses`** — raw `getcasefiles` JSON dumped to `.enum_cache/<court>_<lang>/<ts>_pageNNNN.json` for after-the-fact provenance and dedupe.
- **`--enum-max-age 24`** — if this command has to be restarted within 24h, per-`(court, lang)` enumeration will short-circuit on freshness.
- **`--lang both`** — enumerate English and Traditional Chinese. The UPSERT rule at `checkpoint.py:138-141` prefers the `en` row for any `(court, year, number)` that appears in both languages, so bilingual cases still produce one row keyed by the EN judgment; TC-only cases end up with `lang='tc'`.

`--courts` is deliberately omitted; the default `hkcfi,hkca,hkdc,hkcfa` covers ~97% of the confirmed corpus (see [Endpoint Reference](./03-endpoint-reference.md)). `--allow-doc` is deliberately omitted because Word/DOCX bloats the on-disk footprint from ~5.6 GB (HTML+JSON+TXT) to ~13-14 GB with little RAG-side benefit — add it back if the DOCX is needed as the authoritative source.

## Canary run pattern

Before the full run, canary against a small court to prove every reliability layer fires end-to-end. HKFC has 1,789 files and is small enough to canary in minutes:

```bash
uv run hklii scrape -o ./canary_output \
  -p http://127.0.0.1:8888 -p http://127.0.0.1:8889 \
  -p http://127.0.0.1:8890 -p http://127.0.0.1:8891 \
  -p http://127.0.0.1:8892 -p http://127.0.0.1:8893 \
  --with-summaries --with-appeal-history \
  --save-enum-responses \
  --courts hkfc --limit 100 --lang en
```

A passing canary means all of the following, as verified on the 2026-07-04 canary:

- 100/100 rows land in `status='downloaded'`, zero `failed`.
- All 6 proxies stay healthy through the run (no circuit-breaker kills).
- Zero false positives from the S-1 challenge-page marker set (`scraper.py:30-47`) — real judgments should never match `just a moment` / `cf-challenge` / `请稍候` etc.
- The M-4 warm-up fires once per proxy — grep `scrape.log` for the initial IP echo followed by the landing-page fetch on each proxy index.
- Empty-content-with-doc-fallback captures the ~2026 judgments correctly: `content=""` returns from `/api/getjudgment` are recovered by fetching `judgment.doc` from `legalref.judiciary.hk` (`scraper.py:293-319`). The 2026-07-04 canary produced 99 HTML files + 55 `.doc` + 45 `.docx`.
- `appeal_history` marked `downloaded` on 100/100.
- Throughput lands around ~3,000 files/hour on a 6-proxy pool.

If any of those fail, do not proceed to the full production run — chase the failure first. Delete `./canary_output` before re-canarying so `.checkpoint.db` starts clean.

## Resume-after-crash workflow

The scraper is designed to survive `SIGKILL` and pick up cleanly on restart. The recovery sequence:

1. **On startup**, `db.release_in_progress()` fires unconditionally (`cli.py:591` -> `checkpoint.py:255-259`). Any row left in `status='in_progress'` from the crashed prior run gets flipped back to `pending`. This is a raw SQL `UPDATE cases SET status='pending' WHERE status='in_progress'` on a WAL-mode connection — it commits before any worker starts dispatching.
2. **Add `--resume`** to skip the ~7-minute enumeration pass, assuming the checkpoint has pending rows to work on. If it does not (because the prior run had finished enumeration and was mid-download when it died), the code will re-enumerate anyway — which is defensively fine because `upsert_case` at `checkpoint.py:128-145` is idempotent.
3. **Add `--retry-failed`** if the prior run left failures that were transient (a proxy region blip, a Judiciary 5xx that no longer holds). `db.reset_failed_to_pending()` flips every `status='failed'` row back to `status='pending'` with `error=NULL` (`checkpoint.py:247-253`) before workers start.

Full recovery command:

```bash
uv run hklii scrape -o ./downloads -p ... -p ... \
  --resume --retry-failed \
  --with-summaries --with-appeal-history --lang both
```

Order of operations inside the scraper: reset failed -> release in-progress -> stats print -> download loop. That order matters: `--retry-failed` must run before the pending count is measured, otherwise the previously-failed rows would not count against the target for the progress bar.

The `.checkpoint.db` file is locked exclusively via `fcntl.flock(LOCK_EX | LOCK_NB)` on `.checkpoint.db.lock` (`checkpoint.py:81-102`). If a stale process is still holding the lock, the second scraper aborts with `CheckpointLockError("Another process holds the checkpoint lock at ..."` — kill the stale process first. If the underlying filesystem cannot create the lock file at all (some network mounts), the code logs a warning and continues without cross-process protection (`checkpoint.py:83-93`); do not run two scrapers on the same DB in that scenario.

## Bilingual scrape

`--lang both` maps to the tuple `('en', 'tc')` at `cli.py:233`. `BulkScraper.enumerate` iterates the outer `for court in courts` loop, then the inner `for lang in langs` loop (`scraper.py:114-153`) — so for four courts and two languages, up to eight `(court, lang)` enumeration passes fire before any downloads begin.

The checkpoint has a single row per `(court, year, number)` PK — no `lang` in the PK. The UPSERT collision rule at `checkpoint.py:128-145` decides which language wins on the same `(court, year, number)`:

```sql
lang=CASE
  WHEN cases.lang='en' OR excluded.lang='en' THEN 'en'
  ELSE excluded.lang
END
```

- Case exists only in EN: `lang='en'` (single row).
- Case exists only in TC: `lang='tc'` (single row).
- Case exists in both EN and TC: whichever gets enumerated first sets `lang`. On the second enumeration, if either the existing row or the new insert is `en`, the row lands on `en`. So EN wins for bilingual cases regardless of enumeration order.

Downstream, `_download_one` uses `record.lang` to construct the API URL (`scraper.py:234-236`), so EN-labelled bilingual rows are downloaded as EN and never fetch the TC judgment. If the RAG use case ever needs both language versions of the same case, this is where to change — but it needs a schema migration (adding `lang` to the PK) not just a scraper change.

The 2026-07-04 court counts (see [Endpoint Reference](./03-endpoint-reference.md)) sum to ~118,188 across 13 slugs at the API layer. HKLII's homepage counter claims 122,460; the ~4,300 delta is likely bilingual dupes and press-summary counting, not missing slugs, so a bilingual sweep should land in that neighbourhood after dedupe.

## Format selection matrix

`-f` is repeatable. Which combinations are legal and what actually gets written:

| Format | `scrape` default | `download` default | Requires flag | On-disk artifact |
|---|---|---|---|---|
| `html` | yes | yes | none | `{stem}.html` — raw `content` HTML from the JSON API |
| `json` | yes | yes | none | `{stem}.json` — metadata: title, case_number, court, date, neutral_citation, parallel_citations, doc_url, has_translation, url (`client.py:92-106`) |
| `txt` | yes | yes | none | `{stem}.txt` — plaintext from `html_to_text` (`parser.py:84-99`) |
| `doc` | **no** | yes | `--allow-doc` (bulk mode only) | `{stem}.doc` or `{stem}.docx` from `legalref.judiciary.hk`, extension chosen at `scraper.py:350` by lowercased URL suffix |

The `--allow-doc` gate is bulk-mode-only. `hklii download -f doc URL` works out of the box because that subcommand is for targeted spot pulls where the operator explicitly asked for one thing. `hklii scrape -f doc` without `--allow-doc` silently drops `doc` from the format set with a yellow stderr warning (`cli.py:228-230`) — no crash. If you really want DOCX in a bulk run, both `-f doc --allow-doc` are required.

The `doc` fallback is also implicit for cases that come back with `content=""` in `/api/getjudgment`: `scraper.py:293-319` first tries to save the HTML and other formats normally, then if `content` was empty and the operator opted into `doc`, it fetches the Word document from Judiciary and records success. This is how recent (~2026) judgments get captured — HKLII stopped sending inline HTML for those and only supplies the `doc` URL.

## Enumeration cache management

Two orthogonal levers:

**`--enum-max-age HOURS`** — freshness skip. `BulkScraper.enumerate` reads `db.last_enumeration_ts(court, lang)` (max of `last_seen_at` for that `(court, lang)`, at `checkpoint.py:193-201`) and skips the enumeration if `run_ts - last_ts < enum_max_age_hours * 3600` (`scraper.py:116-124`). Logs `skip enumerate court=... lang=... (last Xh ago, cache window Yh)`. Default is `0` = never skip. Set to `24` for daily incremental runs; set to `1` for tight-loop debugging.

**`--save-enum-responses`** — provenance snapshots. When set, `enumerate_court` writes each `getcasefiles` page's raw JSON via `atomic_write_text` to `<output>/.enum_cache/<court>_<lang>/<ts>_pageNNNN.json` (`scraper.py:141-145` -> `enumerator.py:117-137`). `ts` is the run timestamp; `NNNN` is zero-padded page number. Storage cost is small (~234 B/row * one row per case), roughly 30-40 MB for the full corpus. Useful for after-the-fact "was this case in the corpus on date X" queries and for debugging counter drift between HKLII's homepage and the API.

To clear the enum cache without wiping the corpus:

```bash
rm -rf ./downloads/.enum_cache
```

The next `hklii scrape` will re-populate it.

### Consuming `.enum_cache/` snapshots with `jq`

Each snapshot is `getcasefiles`'s exact JSON envelope: `{"totalfiles": N, "judgments": [...]}`. Useful one-liners:

```bash
# Counter-drift check: how did totalfiles change across today's runs for HKCFI-en?
for f in ./downloads/.enum_cache/hkcfi_en/*_page0001.json; do
  ts=$(basename "$f" | cut -d_ -f1)
  n=$(jq '.totalfiles' "$f")
  echo "$ts  $n"
done | sort

# Count judgments actually returned in the most recent single page — sanity-checks itemsPerPage:
jq '.judgments | length' \
  "$(ls -t ./downloads/.enum_cache/hkcfi_en/*.json | head -1)"

# Verify no duplicates across all pages of the latest run (neutral citations must be unique):
run_ts=$(ls -t ./downloads/.enum_cache/hkcfi_en/ | head -1 | cut -d_ -f1)
jq -r '.judgments[].neutral' \
  ./downloads/.enum_cache/hkcfi_en/${run_ts}_page*.json \
  | sort | uniq -c | awk '$1 > 1'   # non-empty output = duplicates

# Newest judgment in a given court+lang (for freshness monitoring):
jq -r '.judgments[0] | "\(.date) \(.neutral)"' \
  "$(ls -t ./downloads/.enum_cache/hkcfi_en/*.json | head -1)"

# Compare two runs — which neutrals are new since the previous snapshot?
old=$(ls -1 ./downloads/.enum_cache/hkcfi_en/*_page0001.json | sort | tail -2 | head -1)
new=$(ls -1 ./downloads/.enum_cache/hkcfi_en/*_page0001.json | sort | tail -1)
comm -23 \
  <(jq -r '.judgments[].neutral' "$new" | sort) \
  <(jq -r '.judgments[].neutral' "$old" | sort)

# Confirm the probe finding that parallel[] is empty in current data — max array length seen:
jq '[.judgments[].parallel | length] | max' \
  "$(ls -t ./downloads/.enum_cache/hkcfi_en/*.json | head -1)"
```

The envelope schema is fixed at exactly two top-level keys (`totalfiles`, `judgments`) and each judgment record has five keys (`neutral`, `path`, `date`, `parallel`, `cases`) — see [03 Endpoint reference](./03-endpoint-reference.md) for the authoritative schema. Any `jq` recipe that assumes those keys will keep working as long as HKLII's API shape is stable.

## Logging locations

`setup_logging` at `src/hklii_downloader/logging_setup.py:14-32` writes to `<output>/<subcommand>.log` using a `FileHandler` with `utf-8` encoding and the format `%(asctime)s %(levelname)-7s %(name)s: %(message)s`. Root logger is `hklii_downloader` at `logging.INFO`; existing handlers are cleared on entry so repeated invocations do not double up.

| Subcommand | Log path |
|---|---|
| `hklii download` | Not currently wired (no `setup_logging` call in `download` path) |
| `hklii scrape` | `<output>/scrape.log` |
| `hklii verify` | Not wired — `verify` is fast, output goes to stdout |
| `hklii enrich` | Not wired at the CLI layer (individual module loggers still emit to stderr) |

Practically the log that matters is `downloads/scrape.log`. Grep-friendly:

```bash
# per-proxy failures
grep 'IPLeakError' downloads/scrape.log

# enumeration timing
grep 'enumerate court=' downloads/scrape.log

# freshness skips
grep 'skip enumerate' downloads/scrape.log

# per-case download failures
grep 'FAILED\|mark_failed\|challenge-page detected' downloads/scrape.log

# progress heartbeats — the Rich progress bar goes to stderr, not the log,
# so tail stderr separately when running under nohup
```

For at-a-glance stats mid-run, query the checkpoint directly:

```bash
sqlite3 downloads/.checkpoint.db \
  "SELECT status, COUNT(*) FROM cases GROUP BY status"

sqlite3 downloads/.checkpoint.db \
  "SELECT court, status, COUNT(*) FROM cases GROUP BY court, status ORDER BY court"

sqlite3 downloads/.checkpoint.db \
  "SELECT error, COUNT(*) FROM cases WHERE status='failed' GROUP BY error ORDER BY 2 DESC LIMIT 20"
```

Running SQLite reads on a WAL-mode database while the scraper is actively writing is safe — SQLite's WAL model gives readers a consistent snapshot without blocking writers.

## Wall-clock estimate

Baseline canary (2026-07-04, 6-proxy pool, HKFC, 100 files, `--lang en`): ~3,000 files/hour throughput. Extrapolated to the ~114,398 case default-court corpus at 20 proxies:

| Pool size | Approx files/hr | Full-corpus wall-clock | Corpus size |
|---|---|---|---|
| 6 proxies (canary baseline) | ~3,000 | ~38 h | HTML+JSON+TXT ~5.6 GB |
| 20 proxies (production) | ~6,000-8,000 (linear-ish scaling) | **~15-20 h** | HTML+JSON+TXT ~5.6 GB; add `--allow-doc` -> ~13 GB |
| 20 proxies, both langs, enrichment | slightly lower per-case (extra `/api/getappealhistory` + press-summary fetches) | ~18-22 h | +~0.5-1 GB enrichment sidecars |

The 15-20h band with 20 proxies is a projection from the 6-pool canary throughput — it has not yet been measured end-to-end at 20 pool size against the full corpus. The gap between the 6-pool measured baseline (3,000 files/hr, ~38h projection) and the 20-pool estimate (15-20h) assumes near-linear pool scaling, which the M-4 warm-up + per-proxy throttler design supports but has not been validated at 20-pool over a 15h+ run.

Corpus sizing evidence: JSON API responses average ~31 KB, HTML ~29 KB, plaintext ~19 KB, Judiciary DOCX ~70 KB. Skipping DOCX keeps the on-disk footprint under 6 GB; enabling `--allow-doc` triples it. See [HKLII Platform](./01-hklii-platform.md) and [Judiciary Platform](./02-judiciary-platform.md) for the per-format size measurements.

## Pre-flight checklist

Before kicking off the full run:

- [ ] **Disk space.** `df -h $(pwd)` should show free space >= `corpus_size * 1.5`. That is >= ~9 GB for HTML+JSON+TXT, >= ~20 GB with `--allow-doc`. The scraper does NOT check disk space itself (see gaps below).
- [ ] **File descriptor limit.** `ulimit -n` should be >= 65,536. Twenty curl_cffi HTTP/2 clients each with their own cookie jars, plus SQLite handles (main + WAL + SHM + lock), plus log handles, plus every `.part` file mid-write, can climb into the low thousands on macOS's default (256 stock, 1024 in recent shells). If in doubt: `ulimit -n 65536` in the shell before starting.
- [ ] **VPN pool healthy.** From the compose directory: `docker compose ps` should show 20 containers, all `healthy`. Sanity-check the IPs are distinct: `for p in {8888..8907}; do curl -s -x http://127.0.0.1:$p https://httpbin.org/ip; done | sort -u | wc -l` should return `20`. `scripts/expand_vpn_pool.py --up --test` handles boot and verification in one shot; see [VPN Pool](./08-vpn-pool.md).
- [ ] **Checkpoint lock not held.** `lsof downloads/.checkpoint.db.lock 2>/dev/null` should return no rows. If a stale process is still holding it, the scraper aborts with `CheckpointLockError` — kill it (or reboot) before restart.
- [ ] **robots.txt sanity check.** `curl -s https://www.hklii.hk/robots.txt` — verify HKLII's stance has not changed since last review. Deferred as an automated check today (see gaps below).
- [ ] **Log path writable.** `touch downloads/scrape.log && rm downloads/scrape.log` — the run will `mkdir -p` on `output_dir`, but a read-only mount surfaces itself as an obscure `FileHandler` traceback three minutes into the run.
- [ ] **Peak-hours awareness.** Choose your start time deliberately — see the HKT scheduling section below.

## Known operational gaps

Explicit list of what the runbook currently expects the operator to do manually. Each is a potential automation target:

1. **VPN health monitoring during long runs.** The scraper trusts the preflight snapshot of pool health for the entire duration of the run. If a gluetun container crashes 6 h in, the associated proxy fails per-request, the circuit breaker retires it after 5 consecutive failures, and cooldown attempts to revive it 300 s later — but the operator gets no top-level health signal. The runbook currently relies on manually watching `docker compose ps` in a second terminal.
2. **Disk-space mid-run monitoring.** No `shutil.disk_usage` preflight and no periodic recheck. A full disk mid-run manifests as `OSError` inside `atomic_write_bytes`, which `scraper.py:221-230` catches and marks the case failed — but there is no ceiling and no early alert.
3. **`ulimit -n` auto-detect.** The scraper does not read `resource.getrlimit(RLIMIT_NOFILE)` at boot or warn if it is low. macOS ships small defaults; the operator must set it by hand.
4. **SQLite `synchronous` PRAGMA.** The checkpoint opens with `PRAGMA journal_mode=WAL` (`checkpoint.py:66`) and default `synchronous=NORMAL`. Under WAL a power-loss or SIGKILL during a WAL checkpoint can lose the last committed transaction. Upgrading to `synchronous=FULL` is safer (and the marginal cost is dwarfed by network latency), but has not been changed.
5. **JSONL metrics stream.** No structured metrics file. Progress goes to Rich's stderr progress bar and to `scrape.log` in freeform text. A `--metrics-file` writing rows/hour, per-proxy failure rate, retry rate, average latency, ETA every N seconds would make long-run monitoring far easier.
6. **HKT off-peak scheduling.** Not automated — no cron helper, no "sleep until 22:00 HKT" gate. Operator picks the start time by hand. See HKT peak-hours section next.
7. **`robots.txt` / ToS recheck cadence.** No scheduled probe; deferred as a manual pre-flight step above.

None of these will block a run today. They are named here so a future refactor can pick them up.

## HKT peak-hours consideration

HKLII's traffic pattern (inferred from the origin's server-side latency distribution and from the target audience being Hong Kong lawyers) peaks during Hong Kong business hours: 09:00-18:00 HKT, which is 01:00-10:00 UTC.

Two defensible start-time strategies:

- **22:00-23:00 HKT start (14:00-15:00 UTC).** Late-evening / overnight in HK. Origin traffic is at its lowest, so any rate-limit or 5xx behaviour we trigger is easier to spot in HKLII's own logs — and easier to notice from our side because retries stand out cleanly. This is the safer choice if the run is short enough to finish by morning HK time (~7-10 h wall-clock at 20-pool would end by 06:00-09:00 HKT).
- **09:00 HKT start (01:00 UTC).** Blend into peak. Percentile-based detection (top 1% of source IPs by RPS) has a harder time flagging us when there is more surrounding traffic to be a percentile of. This is the safer choice for a full 15-20 h run that will cross the 22:00 quiet window either way — starting at the peak means the run's "loud" phase overlaps with everyone else's, and by the time we cross into the overnight quiet period we are ideally in the tail of finishing rather than the burst of enumeration.

The 15-20 h projected wall-clock at 20-pool straddles both windows: an 09:00 HKT start finishes 00:00-05:00 HKT the next day. A 22:00 HKT start finishes 13:00-18:00 HKT the next day. Given that enumeration front-loads the visible-burst signature (up to eight sequential ~2.3 MB `getcasefiles` fetches in the first 7 minutes — see [Anti-Detection Strategy](./04-anti-detection-strategy.md), signal 11), starting during peak so that burst is hidden in surrounding traffic is the tiebreaker in favour of the 09:00 HKT window for full corpus runs. For canaries and shorter incremental runs, 22:00 HKT is fine.

The scraper has no built-in scheduler for this; use `at` or a crontab entry to fire the `hklii scrape` command at the chosen wall-clock time.
