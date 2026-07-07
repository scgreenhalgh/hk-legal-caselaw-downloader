# HKLII Offline Viewer — Design

## 0. Scope decision — Option 3 (viewer-scope, zero downloader changes)

The workflow synthesis below (§§1–12) proposes several downloader-side prerequisites — `hklii_downloader/graph.py` at top level, a `citation_hub_cache` table in `checkpoint.db`, a `citations.from_court` decomposition column, and a `scrape-noteup` post-hook. **Per session decision (2026-07-07), all move to the viewer scope:**

- `graph.py` lives at `src/hklii_downloader/viewer/graph.py`, NOT at top-level `hklii_downloader/graph.py`. If a future RAG pipeline needs the same reads, extract a shared package then — not now.
- Hub cache lives as `viewer_hub_cache` inside `viewer.db`, rebuilt by `hklii viewer index` (populated via cross-DB read of `checkpoint.db.citations`). Zero ALTER TABLE, zero new tables in checkpoint.db.
- `citations.from_court` decomposition is not added. `cited_by` computes `substr(from_key, 1, instr(from_key,'/')-1)` at query time — cheap because the sort runs on the small result set that `idx_cit_to` filters, not 242k rows.
- No `scrape-noteup` post-hook. `hklii viewer index --incremental` is the sole refresh path.

**Rationale:** the viewer is a reader; readers shouldn't ALTER the writer's schema. RAG-sharing was the design's main architectural argument for cross-package placement, but RAG doesn't exist yet, and duplicating a small module later is cheaper than debating architecture now.

**Reading the rest of this document:** wherever §§1–12 say `hklii_downloader.graph`, `checkpoint.db.citation_hub_cache`, `citations.from_court`, or the noteup post-hook, substitute the Option 3 mapping above. Phase 0 in §11 collapses into Phase 1 (viewer package only).

## 1. Purpose & scope

**DECISION**: Ship a local read-only web UI over the 162,348 downloaded court judgments + citation graph. Stack: FastAPI + uvicorn + Jinja2 + HTMX + SQLite FTS5. Localhost 127.0.0.1 bind only, single-user, no auth.

Non-goals (from citation-graph-design.md §6 + fixed task decisions): no online sync, no auth, no force-directed graph explorer, no treatment labels, no character-pinpoints, no custom search ranking beyond BM25, no external search service (Meilisearch/Typesense/Elasticsearch), no React/Vue/Solid/Svelte, no non-localhost bind.

Adversarial review across 7 angles surfaced additional items to explicitly defer (see §12 for full list). Highlights:
- Hand-rolled query DSL (form fields cover v1 expressiveness)
- `sort:authority` FTS rank (waits for citation_hub_cache to exist and be keyed compatibly)
- Hover previews on cite links (row IS the affordance)
- `viewer_meta.db` persistent SQLite (in-memory dicts suffice at 162k rows)
- Manual dark-mode toggle (rely on `prefers-color-scheme`)
- `--host` and multi-exit-code enumeration
- Playwright e2e in the default suite

**Rationale**: The corpus is static and small; the user is one person on a laptop. The design targets the smallest surface that makes the corpus browsable and searchable end-to-end. Every add-on that anticipated multi-user, remote access, or CI-scale ceremony has been demoted to v2.

**Risks + mitigations**: (a) v1 will feel spartan compared to Casetext/Bloomberg Law — accept it; each deferred item can land when a concrete pain arrives. (b) Non-goals list can drift with time; document them in this file rather than tribal knowledge.

**Integration notes**: Sections 2-10 assume the non-goals above. If a future contributor wants (e.g.) a query DSL, they revisit this document first, not the codebase.

## 2. Architecture overview

**DECISION**: Read-only viewer package at `src/hklii_downloader/viewer/`. Data flow: `checkpoint.db` (read-only) → helpers in `hklii_downloader.graph` (shared with future RAG) → FastAPI sync-def route → Jinja2 template → HTMX partial swap for lazy tabs.

`graph.py` lives at **top-level `src/hklii_downloader/graph.py`**, NOT under `viewer/`. All 7 design angles converged on this: the RAG pipeline (per citation-graph-design.md §5) is a second future consumer, and `viewer/` cannot be a dependency of RAG. `graph.py` is framework-agnostic (no fastapi/starlette imports) and returns plain dicts or small `@dataclass` NamedTuples so schema growth doesn't break call sites.

Layer summary:
- **DB**: checkpoint.db (WAL). Viewer opens read-only via `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. NO `nolock=1` (unsafe under WAL — silently drops uncheckpointed frames). Matches the pattern already present at `monitor.py:127`. Viewer NEVER opens through `CheckpointDB` (which would grab the fcntl writer lock and 503 during noteup runs).
- **Helpers**: `hklii_downloader.graph` (citation reads), `viewer.search` (FTS build + query), `viewer.body_render` (sanitizer + text walker + cache), `viewer.queries` (browse-shaped list queries with total-count).
- **Routes**: FastAPI `def` (sync) handlers so blocking sqlite3 calls run in FastAPI's threadpool. Background WAL watcher (if it ships) is `async def` wrapping IO in `asyncio.to_thread`.
- **Templates**: Jinja2 partials in `viewer/templates/`, each renderable in isolation for unit tests. Row shape uses shared `_case_row.html` + `CaseSummary` dataclass.
- **HTMX**: Lazy-loading only for citations panel tabs. Filter forms are pure GETs with full-page reload in v1.

**Rationale**: Sync-def routes plus per-request short-lived sqlite3 connections is the correct FastAPI + sqlite3 pairing (the stdlib driver is blocking; `async def` would freeze the event loop on every SELECT). Separate `viewer.db` for FTS isolates schema ownership: downloader owns checkpoint.db, viewer owns viewer.db. WAL mode already set by the downloader (`checkpoint.py:220`) enables concurrent reader + writer.

**Risks + mitigations**: (a) WAL checkpoint mid-request may raise `SQLITE_BUSY` (5) on the next transaction — caught narrowly, treated as retry-once with 503 fallback. (b) Long-lived reader txns can trigger `SQLITE_BUSY_SNAPSHOT` — sidestepped by per-request connection open/close. (c) Package layout drift when viewer wants a helper only downloader owns — resolved by putting all citation reads in `graph.py` from day one.

**Integration notes**: `hklii serve` opens both DB paths at boot. The FTS `viewer.db` is a SEPARATE file next to `checkpoint.db`, viewer-owned, rebuildable derivative. Losing `viewer.db` costs one `hklii viewer index` invocation (a few minutes), not a re-scrape.

## 3. Data access layer

**DECISION**: `hklii_downloader.graph` module owns all SQL read helpers for citation queries. Framework-agnostic. Opens checkpoint.db raw: `sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)`.

**All helpers keyed on `case_key: str`**, the "hkcfa/2020/32" slug that `citations.py:81-104` already writes. NOT `case_id: int` — `cases.id` doesn't exist; PK is `(court, year, number)` (checkpoint.py:82-104). Citations store TEXT `from_key`/`to_key`. Every cross-angle contract (search-hit badging, viewer↔RAG) uses this string form.

Helpers exposed (v1):
- `cited_by(case_key, court_filter=None, page=1, per_page=50) -> list[dict]`
- `authorities_cited(case_key, page=1, per_page=50) -> list[dict]`
- `parallel_cites(case_key) -> list[dict]`
- `hub_cases(court=None, limit=50) -> list[dict]`
- `inbound_counts(case_keys: Iterable[str]) -> dict[str, int]` (batch, for search-hit + browse decoration)
- `appeal_chain(case_key) -> list[dict]` — reads `output/{court}/{year}/{court}_{year}_{number}.appeal_history.json` lazily (there is no `appeal_history` DB table; the citation-graph design-doc wording drifted, and the sidecar layout is FLAT — see §5)

**Downloader-side prerequisites** (Phase 0 in build sequence). Two schema additions ship BEFORE viewer code that reads them:
1. `citation_hub_cache(case_key TEXT PK, inbound_count INTEGER, computed_at TEXT)`, populated by a new `hklii scrape-noteup` post-hook. `COUNT(DISTINCT from_key)` (not `COUNT(*)`) so bilingual `citer_lang` doesn't double-count.
2. `citations.from_court TEXT NOT NULL` decomposition column (populated at insert + backfilled once) so `cited_by` can `ORDER BY court_rank` without substring parsing on every row.

If `citation_hub_cache` is not yet populated, `hub_cases()` returns an empty marker and the FastAPI route renders a "run `hklii scrape-noteup` post-hook" banner. NO fallback to a compute-on-demand `SELECT ... GROUP BY` — 250k edges per request is too expensive.

**Rationale**: Two-module split (writer=`citations.py`, reader=`graph.py`) is the direct L2 semantic-drift mitigation. Shared contract is the schema. `case_key: str` uniformity closes an L2 hazard where a `Iterable[int]` type hint would silently match zero rows.

**L5 NULL vs 0 vs missing-row**: For `inbound_counts`, missing-row means "cache not populated for that key"; 0 means "cached, no incoming edges". UI renders "Not yet cited" for the first, "0 citations" for the second — never conflated.

**Risks + mitigations**: (a) Hub cache staleness after new noteup — render cached `computed_at` as an "as of" note. (b) Concurrent WAL checkpoint during read — per-request short-lived connections avoid `SQLITE_BUSY_SNAPSHOT`. (c) Downloader schema drift — a `test_checkpoint_schema_matches_graph_selects` smoke test opens today's real checkpoint.db, runs each helper's SELECT with `EXPLAIN QUERY PLAN`, and asserts every referenced column exists.

**Integration notes**: FTS layer imports `graph.inbound_counts` for search-hit badging. Browse `list_cases()` joins to hub counts via `inbound_counts()`. Body render doesn't touch graph.py (highlights via regex, resolution deferred to click on `/cite/`).

## 4. Search (FTS5)

**DECISION**: Separate `viewer.db` co-located with `checkpoint.db`. Three-table pattern:
- `fts_cases(case_key TEXT PK, court, year, number, lang, neutral, title, date, body_source, body_sha256, indexed_at)` with covering indexes on `(court, year)` and `(lang, court)`. Composite unique on `(court, year, number, lang)`.
- `case_bodies(case_key INTEGER PK, title TEXT, body TEXT)` — **title column INCLUDED** (verdict-wrong-tool fix: FTS5 external content requires every indexed column present in the content table; without `title`, `snippet()` errors with `no such column: T.title`).
- `fts_body` FTS5 virtual with `content=case_bodies content_rowid=case_key`, `tokenize='trigram case_sensitive 0'`.

**Trigram tokenizer** — the only single-tokenizer choice for a 50/50 EN/TC corpus. `unicode61` treats a CJK run as ONE token (empirically verified: `unicode61` returns zero for `香港` query on `香港特別行政區`). `porter` is EN-only. `ICU` isn't in stock CPython sqlite3. Trigram requires ≥3 chars in the MATCH term; UI surfaces this as an explicit error, not silent empty results.

**Query surface**: NO hand-rolled DSL. Filters bind to indexed columns via HTML form fields (`<select name="court">`, `<input name="year_min">`, checkboxes). Text `q=` is escaped (~15 lines: strip FTS-reserved metacharacters, balance quotes). HTMX form uses `hx-push-url="true"` for bookmarkable URLs.

**Bilingual keying (L2 fix)**: `fts_cases` PK is composite `(court, year, number, lang)`. Bilingual detection is a **filesystem probe** — does `{stem}.tc.html` exist alongside `{stem}.html`? — NOT `cases.lang` (which collapses bilingual to 'en' per checkpoint.py:373-380). Indexer inserts two `fts_cases` rows per bilingual case, sourcing bodies from both files. INFO-level log per bilingual case so operators see the count.

**Ranking**: `bm25(fts_body)` with `title=3.0, body=1.0`. `sort:relevance|date|court` in v1. `sort:court` uses a CASE expression covering all 13 court slugs (hkcfa=0, hkca=1, hkcfi=2, hkdc=3, hklt=4, hkfc=5, hkcort=6, ceo=7, cta=8, hktvpp=9, other=99).

**Snippets**: `snippet(fts_body, 1, '<mark>', '</mark>', '…', 32)`. Module-level constants `FTS_HIGHLIGHT_START = "<mark>"` / `FTS_HIGHLIGHT_END = "</mark>"` — a comment names the styling layer's CSS selector so writer and reader share a documented contract.

**Index build**: `hklii viewer index [--rebuild | --incremental | --court X]`. `--rebuild` writes to `viewer.db.new` then `os.replace()` — server holds `viewer.db` open by inode; new connections open the new inode (same atomic_write pattern already in the downloader). `viewer.db` created with `PRAGMA journal_mode=WAL`.

**Refresh policy**: NO auto-refresh middleware. `hklii update` and `hklii scrape-noteup` print a "run `hklii viewer index --incremental`" hint at completion. Mtime-based staleness is unreliable under WAL (main file mtime only advances on checkpoint), so no banner in v1.

**Rationale**: External-content + `case_bodies` as the plaintext source of truth means snippets can highlight, and the RAG pipeline can re-use the extracted text. Trigram is the only tokenizer that gives usable CJK recall without a language classifier or external segmenter.

**Risks + mitigations**: (a) Trigram cannot answer 2-char CJK queries — UI validates and returns a clear error, not silent empty. (b) FTS5 module missing in custom SQLite — catch `sqlite3.OperationalError` on first `CREATE VIRTUAL TABLE`, exit with a pysqlite3-binary hint. (c) Bilingual `.tc.html` sidecar convention drift — `case_langs(case_key)` helper is the single source of truth, tested against fixtures in all four language combinations.

**Integration notes**: Body render imports `iter_text_nodes` from `viewer/body_render/text.py` — shared between sanitizer and FTS indexer so hit offsets align with rendered anchor positions. Browse shares `BrowseFilters` TypedDict for filter shape. `hklii serve` verifies `viewer.db` exists at boot; if missing, exits 1 with "run `hklii viewer index` first."

## 5. Case body rendering

**DECISION**: Server-side sanitize-and-cache pipeline. One canonical URL per (case, requested_lang). Discriminator selects body file from disk. Text walker performs sanitization + citation highlighting + optional search-term marking in one pass. Output cached to `viewer_cache/{court}/{year}/{stem}[.tc].rendered.html`.

**Path layout is FLAT (verdict-integration fix)**: `output/{court}/{year}/{court}_{year}_{num}.{html,tc.html,generated.html}`. NOT `scrape_output/{court}/{year}/{num}/content.html` — that path in the design doc drifted from disk. Code follows disk. A smoke test walks 3 real corpus samples asserting the discriminator returns non-None.

**Discriminator keys on `cases.lang` + filesystem probe (verdict-wrong-tool fix)**, NOT filename suffix. Rules:
- `case.lang == requested_lang` and `{stem}.html` exists → serve as `case.lang`
- `requested_lang == 'tc'` and `{stem}.tc.html` exists (paired bilingual) → serve TC
- `case.lang == 'tc'` (or 'zh') and only `{stem}.html` exists (TC-only in bare .html) → serve at `/tc`
- `case.lang == 'zh'/'tc'` and `requested_lang == 'en'` and no EN body on disk → 404 with formats-on-disk strip
- `.generated.html` maps to `case.lang`; bilingual pandoc fragments get `lang="und"`

`select_body_source(case_row, requested_lang) -> BodySource(kind, path, lang, has_synth_anchors, upstream_status)`.

**Orphan/upstream status (verdict-integration fix)**: `upstream_status` drawn from `cases.status`. Orphaned → template renders "retracted from upstream after YYYY-MM-DD; served from local snapshot" strip. Pending/in_progress → 410 Gone.

**Sanitizer**: lxml.html walker with rejection-list (script/style/link/meta/iframe/object/embed/form/input/button/base) + attribute allowlist. HKLII semantic tags `<parties>/<coram>/<date>/<representation>` on the allowlist by name. Unknown tags preserved silently — no WARN log, no `hklii viewer audit --unknown-tags` (deferred). Golden fixtures over ~10-15 real judgments fail loudly on walker changes.

**Cache key = `(sanitizer_version, format_availability_digest, chosen_source_path, chosen_source_mtime)`** (verdict-integration fix). `format_availability_digest` hashes `(has_html, has_tc_html, has_generated_html)` at render time. A higher-priority format arriving later invalidates the cache even if the currently-chosen source is byte-unchanged. Test: render `.generated.html`-only case → cache → create `.html` sibling → re-request must re-render to native.

**Citation highlighting**: Server-side single-pass walk. Text nodes only (skip `<a>`, `<code>`, `<pre>`). Wrap cites as `<a href="/cite/{normalized}" class="hklii-cite">`. `/cite/{neutral}` handler resolves via DB at click time; 302 to canonical on hit, 200 "unresolved cite — parsed as X" page on miss. NEVER silent 302 to homepage (L5).

**HTML shape dispatch (verdict-integration fix)**: `render_case_body()` dispatches on `case.html_generated_from`. `_render_native_hklii(bytes)` for HKLII shape (full `<html><body>` with `<link>`, `<td>` inline styles). `_render_generated_fragment(bytes)` for pandoc fragments (bare `<p>...`). One test per path.

**Render-time only (L5 clarification)**: `sanitizer.py` NEVER writes files on disk. LRU-cached with `functools.lru_cache(maxsize=256)` keyed on source path + mtime. Rules changes invalidate by bumping `sanitizer_version` in the cache key.

**Risks + mitigations**: (a) Silent unknown-tag drop — mitigated by preserve-unknown behavior + golden fixture failures on walker changes. (b) Over-matched cite regex → non-existent case — 200 "unresolved" page, not silent redirect; audit script counts unresolved rate per court. (c) Path drift — smoke test on 3 real samples.

**Integration notes**: `iter_text_nodes()` in `body_render/text.py` shared with FTS5 indexer. `/cite/{neutral}` shares `url_rewrites.py` dict with the ord-section angle. Cited-by tab renders empty container with `hx-get`; graph angle owns the fill. NO shared `iter_text_nodes` contract test locking RAG's future usage — that's a v2 concern when RAG lands.

## 6. Browse & filter

**DECISION**: Server-render Jinja rows with pure HTML form GETs (no HTMX partial swaps in v1). Offset+limit pagination, size=50 hardcoded. Fixed 5-column table. Filter panel binds directly to querystring.

Routes:
- `GET /court/{slug}` — years-with-counts grid + top hub cases (when hub cache populated)
- `GET /court/{slug}/{year}` — list page
- `GET /browse?court=...` — corpus-wide list; UI requires at least one court prefilter

Columns (v1, 5-wide):
1. Neutral cite (small caps, link to case detail)
2. Parties (`cases.title`, truncated 80 chars)
3. Date (ISO YYYY-MM-DD, `font-variant-numeric: tabular-nums`)
4. Formats (icon strip: html, doc, pdf — skip json/txt)
5. Inbound count (right-aligned; blank when hub cache not populated, "0" when cache says 0)

**No `viewer_meta.db` (verdict-YAGNI fix). All denormalizations held in `app.state` as in-memory dicts, built once at boot**:
- `format_flags: dict[case_key, {has_html, has_pdf, has_doc}]` — one `os.scandir` pass over `output/`
- `hub_counts: dict[case_key, int]` — read from `checkpoint.db.citation_hub_cache` once it exists (per design doc §2.1, not duplicated in a viewer file)

Boot time on 162k rows: ~5-8s. If it slows, pickle. NO SQLite meta store, NO `--incremental`, NO `--allow-drift`, NO drift banner, NO mtime staleness compare. Rebuild at boot is the whole story.

Pagination:
- `?page=N` only, size=50 fixed
- Prev/Next + numeric with ellipsis for large ranges
- NO max-depth guard (localhost, no attacker)
- Full-page reload; no HTMX partial in v1

Filters (v1):
- Court (multi-select checkboxes)
- Year range (two `<input>`)
- Date range (two `<input type="date">`)
- Sort: `date_desc` (default) | `date_asc` | `neutral_asc`
- **NO `has_translation`, `has_summary_en`, `has_summary_zh`, `has_appeal_history` in v1** (defer)

Empty states:
- Unknown court → 404
- Court exists / 0 rows in year → "0 cases in {court} {year}" + years-with-counts sidebar
- Filter matches 0 → "0 matches; clear filters" link (fold with above when overlap)

**Rationale**: The proposal's viewer_meta.db collided with citation-graph-design.md §2.1 which places `citation_hub_cache` in checkpoint.db. Reading from checkpoint.db removes the duplication. `langs_present` and `format_flags` are trivially rederivable and don't need persistence.

**Risks + mitigations**: (a) Format-flags dict stale during active scrape — acceptable; user Ctrl-C, restart. (b) Wide date-range unfiltered scans — indexes on `(court, year)` cover court prefilter; add `(court, year, date)` covering index if profiling shows need. (c) No `has_translation` in v1 — surface a v2 candidate rather than ship a broken half-derivation.

**Integration notes**: Row template `_case_row.html` shared with search-results template. `CaseSummary` dataclass in `viewer/models.py` is the shared row shape (`court, year, number, neutral, title, date, formats, inbound_count`). Sort-by-inbound reads `app.state.hub_counts` (populated at boot from checkpoint.db.citation_hub_cache).

## 7. Citation viz

**DECISION**: Three-tab ranked-list panel on `/case/{court}/{year}/{num}` — "Cited by (N)", "Authorities cited (M)", "Also cited as (K)". Standalone `/authorities` hub index. NO force-directed layout (design-doc §8 explicit ban). NO hover previews (v1 YAGNI — row IS the affordance). NO layered chrome beyond what the design doc §6 enumerates.

**Ships against ACTUAL schema, not aspirational design-doc §2.1** (verdict-integration REJECT fix). Downloader-side prerequisites, in Phase 0 of the build sequence, BEFORE any viewer code:
1. `citation_hub_cache(case_key TEXT PK, inbound_count INTEGER, computed_at TEXT)` — added to `checkpoint.py._SCHEMA`; populated by a new `hklii scrape-noteup` post-hook. Uses `COUNT(DISTINCT from_key)` to dedupe bilingual `citer_lang` double-counting.
2. `citations.from_court TEXT NOT NULL` decomposition column — enables SQL `ORDER BY` on citer court without substring parsing.
3. Appeal chain comes from **per-case JSON files** at `output/{court}/{year}/{court}_{year}_{number}.appeal_history.json` (FLAT layout, per §5), NOT a DB table. `graph.appeal_chain(case_key)` reads the JSON lazily.

**Case-detail composition (top→bottom)**:
(a) Signal strip under title — inbound_count badge (from cache), court, year, "hub" flag if inbound_count ≥ fixed threshold (e.g. 100; per-court p99 deferred)
(b) Appeal-chain strip — horizontal row from appeal_history JSON
(c) Judgment body (per §5)
(d) Citations panel — three tabs at the FOOT

**Ranking of Cited-by**: `ORDER BY court_rank ASC, first_seen DESC`. `court_rank` is a CASE expression covering all 13 court slugs (verdict-integration fix: "curial precedence" that collapses to 4 courts was L3 drift against the actual slug list). `citer_inbound_count DESC` dropped from default sort in v1 (waits for hub cache to be keyed compatibly with citations — same TEXT `case_key`).

**Pagination**: 50 rows/page, explicit "Load next 50" button with `hx-swap="beforeend"`. NOT infinite scroll (a11y + back-button). Court facet is **single-select** `?court=CFA` (verdict-YAGNI: multi-select removed).

**`/authorities` hub index**: Simple table sourced from `citation_hub_cache JOIN cases`. Empty-cache banner: "Hub cache not yet computed. Run `hklii scrape-noteup` first." Court facet is `?court=`. NO sortable header, NO drift-percentage staleness badge (`hklii verify --graph` owns reconciliation).

**Reader DB open**: raw `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`. Never through `CheckpointDB` (fcntl lock → 503 for 22 min during noteup runs).

**Rationale**: Rejecting the design-doc-shaped SQL contract avoided a v1 that would `no such table: citation_hub_cache` on day one. The signal strip / appeal chain / three-tab list is design-doc §6 exactly; the scale strategy (server pagination + court facet + cached tab-header counts) hits the 11,450-inbound hub without special interaction.

**Risks + mitigations**: (a) Hub cache empty on fresh install — banner, no fallback to compute-on-demand. (b) Regex over/under-match on cite highlighting — `/cite/` returns 200 unresolved on miss; L4 test asserts set-equality (both directions) between wrapper output and `authorities_cited()`. (c) Downloader migration order — Phase 0 lands hub cache + from_court schema before viewer imports.

**Integration notes**: `graph.py` exports `cited_by`, `authorities_cited`, `parallel_cites`, `appeal_chain`, `hub_cases`, `inbound_counts`. Signal strip fills a `{% block signal_strip %}` slot in the shell. Browse uses `hub_cases()` for its home-page top-hub list. Search uses `inbound_counts()` for per-hit badge decoration.

## 8. `hklii serve` CLI

**DECISION**: Add `serve` as a subcommand of the existing `hklii` CLI. Implementation lives in `viewer/cli.py`, registered via `main.add_command(viewer_cli.serve)` from `cli.py`. Minimal surface:

```
hklii serve [-o/--output DIR]      # default: ./output
           [--fts DB]              # default: <output>/viewer.db
           [--port PORT]           # default: 8787
           [--dev]                 # uvicorn --reload + browser auto-open
```

**Bind**: `127.0.0.1:8787` hardcoded — no `--host` flag in v1 (verdict-YAGNI). Port 8787 avoids collision with 8000/8080/3000.

**DB open**: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` + `PRAGMA query_only=1`. NO `nolock=1` (verdict-wrong-tool: unsafe under WAL). Per-request connections opened in a FastAPI dependency, closed on response — sidesteps `SQLITE_BUSY_SNAPSHOT`.

**Sync route handlers**: All FastAPI routes are `def` (not `async def`) so blocking sqlite3 calls run in FastAPI's threadpool. No connection pool in v1 — sqlite3 open is microseconds locally.

**FTS lives in separate `viewer.db`**. `hklii serve` verifies its presence at boot; if missing, exits 1 with "run `hklii viewer index` first." NO `--build-index-on-start` (silent 5-10 min startup is worse than a clear error).

**Refresh policy**: NO auto-refresh watcher in v1 (verdict-integration fix). `last_seen_at` is only bumped by `upsert_case` — `mark_html_generated`, `mark_enrichment`, `mark_orphaned` all leave it untouched, so a mtime-triggered "reindex changed rows" path silently starves. Instead, `hklii update` and `hklii scrape-noteup` print a "run `hklii viewer index --incremental` to refresh search" hint at completion. User-triggered refresh only.

**Error surface**: Exit 1 + human-readable stderr on any startup failure (verdict-YAGNI: five enumerated exit codes were for a `hklii doctor` wrapper that doesn't exist). Messages must name the fix:
- "checkpoint DB missing at {path}. Run `hklii scrape` first."
- "FTS index missing at {path}. Run `hklii viewer index`."
- "corpus root missing at {path}. Pass -o /path/to/output."
- "port 8787 in use. Pass --port <other>."

**`--dev`**: `uvicorn --reload=True`, `webbrowser.open()` wrapped in `try/except (webbrowser.Error, OSError): pass`. No `--open` separate flag, no CI/TTY heuristics.

**Rationale**: Sync-def routes + short-lived per-request connections + no watcher is the smallest correct v1 shape. The proposal's WAL-mtime watcher, exit-code enumeration, integrity_check-on-boot, connection pool, and LAN-bind confirmation are all speculative defenses against threat models that don't apply to a single-user localhost tool.

**Risks + mitigations**: (a) WAL truncation during reader txn → `SQLITE_BUSY`(5). Middleware catches, returns 503 briefly. Per-request short-lived connections make this rare. (b) User forgets to `hklii viewer index` after a scrape → search stale until they do. Explicit hint after `hklii update` completion.

**Integration notes**: Search angle owns `viewer.db` schema and `hklii viewer index` command; `hklii serve` just checks its presence. Browse reads `checkpoint.db` through the same read-only handle. Citation viz needs `citation_hub_cache` present or renders a banner. All consumers agree on `case_key: str` typing.

## 9. Styling

**DECISION (Phase 5 revision — 2026-07-07)**: **Pico.css v2 classless** (71 KB minified) vendored into `viewer/static/pico.classless.min.css` + a thin viewer-specific overlay `viewer/static/app.css` (~130 lines). The prior "vanilla, hand-written, ~8 KB" plan (below) was replaced when the actual authoring cost of every grid/table/form primitive turned out to dominate the Phase 5 budget — Pico ships all of that + auto dark mode + form controls + tables in one drop-in file. No build step. No CDN at runtime (file is vendored). MIT-licensed.

**Overlay `app.css` scope**:
- Court-tile grid (home) + year-bucket grid (court landing)
- Court badges (citation panels)
- Citation-row grid layouts (cited-by / authorities panels)
- Legal-doc serif on `.case-body article` — `Charter, Georgia, "Times New Roman", Times, serif` + `max-width: 68ch` (Bringhurst) so the judgment reads like a book while chrome stays sans (Pico's system-ui default)
- FTS `<mark>` snippet highlight
- Pager, empty-state, banner primitives
- `.htmx-indicator` display rules

**Semantic HTML unchanged**: templates continue to emit `<table>/<article>/<nav>/<header>/<main>` without utility-class noise — Pico's classless build styles them.

**BCP-47 language mapping (verdict-wrong-tool fix)**: `bcp47(lang)` Jinja filter maps `'en' → 'en'`, `'tc' → 'zh-Hant'`. Templates use `lang="{{ body_lang | bcp47 }}"` on `<article>`. `body_lang` is derived from **WHICH FILE is being served** (route-level `served_body_lang`), NOT from `case.lang` (which collapses bilingual to 'en' and would render TC bodies in Georgia). A test seeds a bilingual case with DB `lang='en'` + served `.tc.html` and asserts the article gets `lang="zh-Hant"`.

**Dark mode**: `<meta name="color-scheme" content="light dark">` + Pico's own `prefers-color-scheme` remap of `--pico-*` CSS custom properties. No manual toggle. Overlay in `app.css` references only `--pico-*` variables so it flips automatically with the framework.

**Color tokens**: rely on Pico's `--pico-primary`, `--pico-muted-color`, `--pico-muted-border-color`, `--pico-secondary-background`, etc. No custom color palette in v1; per-court badge palette lands only when browse asks for it.

**HKLII HTML sanitization (RENDER-time, not ingest)**: `viewer/body_render/sanitizer.py` runs on demand, LRU-cached by mtime + path. NEVER writes files on disk. Strips `<link href="/lrs/…">`, `<link href="/css/…">` (dead offline), inline `font-family`/`font-size`/`bgcolor` on `<td>`/`<p>`. Preserves `align`, `width`, `valign`, `colspan`, `rowspan`, `href` (**allowlist model**, verdict-integration fix — new HKLII inline attributes fail loudly rather than silent win).

**Two HTML shapes (verdict-integration fix)**: `render_case_body()` dispatches on `case.html_generated_from`. Native HKLII (`<html><body>` with `<link>`/`<td>` styles) → `_render_native_hklii()`. Pandoc fragment (bare `<p>`) → `_render_generated_fragment()`. Fragment path skips the strip step; wraps in `<article>` directly.

**Icons**: Zero library. Unicode where clear (§ ✓ →). Inline SVG for 2-3 gaps at ~500 B each.

**FTS5 marker CSS contract**: `FTS_HIGHLIGHT_START = "<mark>"` / `FTS_HIGHLIGHT_END = "</mark>"` constants in `viewer/routes/search.py`; `app.css` styles `mark { background: var(--pico-mark-background-color, #fef08a); }` referencing Pico's own variable with a static fallback.

**Rationale**: Pico's authoring-cost saving (no manual reinvention of table/form/card/pager primitives) beats the size argument at 71 KB — still single-digit ms locally, still no build step, still no runtime CDN. The Phase 4 shape (`data-testid` selectors + semantic HTML) survived the switch untouched: no route test failed after wiring Pico. Deferring the CJK-font detection heuristic, dark-mode toggle, and split-panes bilingual view keeps v1 surface small enough to review under all 5 lenses in one pass.

**Risks + mitigations**: (a) Legacy Linux without CJK fonts renders tofu — user sees it, adds fonts, or opts into future `--vendor-fonts`. (b) HKLII `<td style="font-family: Arial">` beats stylesheet — sanitizer strips inline `font-family`; fixture test asserts absence. (c) Dark mode inverts `bgcolor="#FFFFFF"` cells — sanitizer strips `bgcolor` except on `<th>`.

**Integration notes**: Router owns `<html lang="{{ body_lang | bcp47 }}">`. Search snippet emits the `<mark>` wrapper that this stylesheet targets. Citation panel's court badges use `.badge` primitives here. Body render is the only consumer of the sanitizer; RAG imports `iter_text_nodes` separately if it ever wants extracted text.

## 10. Testing

**DECISION**: Three tiers, one shared fixture DB, no Playwright in default suite (defer to v2).

**Tier 1 — Unit** (`tests/unit/`): Pure helpers in `graph.py`, `viewer/search.py` query escape, `viewer/body_render/text.py` walker, `viewer/body_render/sanitizer.py`. Sync tests. `<10ms` target.

**Tier 2 — Integration** (`tests/integration/`): FastAPI TestClient over a **session-scoped file-based fixture DB**. Fixture opened via **`viewer_db_ro(path)` helper** shared between test fixture and production route open (verdict-integration L4 fix). NOT `CheckpointDB(fixture_path)` — that would test one open path and prod runs another. Seeded 20-row corpus:
- 3 courts × 2 years (hkcfa 2020/2023, hkca 2021, hkcfi 2022 EN+TC)
- One hub case with 200 seeded inbound edges (mini-analogue)
- One TC-only case, two bilingual pairs (exercises the L2 lang-collapse trap)
- One orphan (status='orphaned'), one doc-fallback (formats=["doc"]), one 3-hop appeal chain via on-disk JSON fixture
- Small FTS5 index over 20 rows built by the shipped index-builder

`<500ms` per test. Default suite `<30s` locally.

**HTMX swap-target helper (verdict-wrong-tool fix)**: `assert_htmx_swap_matches(client, parent_url, htmx_url, target_id)` reads the parent's `hx-swap` attribute and branches — `innerHTML` requires the target container to keep its ID; `outerHTML` requires the fragment ROOT to carry it. `pytest.fail` on unknown swap modes (L1 silent-skip mitigation).

**Contract tests per shared table** — one per table, lives in the **DOWNLOADER package under `tests/contract/`** (verdict-integration fix: catches drift on downloader CI, not later in a viewer worktree). Writes canonical row via `checkpoint.py` public API, reads through `graph.py`, asserts semantic equivalence. Tables: cases, citations, citation_hub_cache, case_parallel_cites, ord_reg_edges, enum_runs, legis_documents. Plus on-disk contracts: appeal_history JSON, press summary HTML, generated HTML.

**Tier 3 — E2E**: NO Playwright in v1 (verdict-YAGNI: 95% caught by TestClient + BS4). If browser-specific bugs appear (rare for a server-rendered HTMX app), add v2.

**Five review-lens tests per angle**:
- L1: `graph.py` raises specific error types (`GraphDBUnavailable`, `GraphCacheMissing`), no bare except. Sanitizer parse-failure raises. WAL watcher (if it ships in v2) catches only `FileNotFoundError`.
- L2: contract tests per shared table. `_case_row.html` shared between browse + search.
- L3: docstring claims → named tests (e.g., `test_cited_by_default_order_matches_docstring` asserts three sort keys separately).
- L4: swap-target-pair coverage on every HTMX route. `render_case_body()` wrapper tested, not just walker.
- L5: NULL vs 0 vs missing-row enumerated per nullable. Bilingual fixtures cover en-only / tc-only / both / neither. `body_source='none'` state distinct from empty `case_bodies.body`.

**FTS5 tokenizer pin** (verdict-wrong-tool fix):
- `test_fts5_tokenizer_is_trigram` — inspects schema, asserts `tokenize='trigram'`
- `test_fts5_tc_three_char_query_matches`
- `test_fts5_tc_two_char_query_returns_empty` (documents the trigram lower bound)
- `test_fts5_en_prefix_and_phrase_queries`
- `assert sqlite3.sqlite_version_info >= (3, 34, 0)` guard in `conftest.py`

**Perf tests use `EXPLAIN QUERY PLAN`, not wall-clock** (verdict-wrong-tool fix): `assert 'idx_cit_to' in plan and 'SCAN' not in plan`. Scale-invariant — catches missing-index at 20 rows and 11,450 rows alike.

**`graph.py` landing sequence**: Changes land on `hklii_downloader/graph.py` on `main` FIRST. Viewer imports from `hklii_downloader.graph`. Contract tests fail on downloader CI when writer/reader agreement breaks.

**Rationale**: HTMX behavior IS HTML — TestClient + BS4 catches structural drift, missing targets, wrong swap-mode assumptions. Playwright's marginal catch (real-browser DOM/JS) doesn't earn its 10x-slower-per-test cost in a TDD loop.

**Risks + mitigations**: (a) Session-scoped fixture polluted by misbehaving test — `mode=ro` opener + authorizer guard rejects writes. (b) Fixture drifts from real checkpoint.db — fixture built via same code path prod uses (`_SCHEMA` module constant). (c) FTS staleness under concurrent write — user-triggered refresh in v1; contract test with background writer thread deferred (verdict-YAGNI: the WAL concurrency test was itself questionable).

**Integration notes**: Every angle's tests share the same `tests/conftest.py`. Every route ships unit + integration coverage. `CaseSummary` dataclass and `BrowseFilters` TypedDict are the shared cross-angle contract.

## 11. Build sequence

**DECISION**: Strict TDD ordering, small atomic commits, test-first, grouped by module. Each pair is `test: add failing test for X` followed by `feat: implement X`. Downloader-side migrations (schema changes, `citation_hub_cache`, `from_court` decomposition) land on `main` BEFORE viewer imports them.

**Phase 0 — Downloader prep** (in `hklii_downloader/` proper):
1. `test/feat: viewer_db_ro(path)` helper opens checkpoint.db with `mode=ro`, no fcntl
2. `test/feat: citation_hub_cache` schema migration + noteup post-hook (`REPLACE INTO ... GROUP BY to_key` with `COUNT(DISTINCT from_key)`)
3. `test/feat: citations.from_court` decomposition column + backfill migration
4. (deferred) cases_reindex_log for future auto-refresh — build only when v2 lands the watcher

**Phase 1 — graph.py** (in `hklii_downloader/graph.py`):
5. `test/feat: cited_by(case_key, court_filter, page, per_page)` court-ranked, first_seen tiebreak
6. `test/feat: authorities_cited`
7. `test/feat: parallel_cites`
8. `test/feat: hub_cases` empty-cache marker + populated ranking
9. `test/feat: inbound_counts` batch dedup
10. `test/feat: appeal_chain` reads on-disk JSON

**Phase 2 — Search infra** (`viewer/search.py`, `viewer/body_render/text.py`):
11. `test/feat: iter_text_nodes` skips `<a>/<code>/<pre>`
12. `test/feat: fts_cases + case_bodies` schema (case_bodies has `title` column)
13. `test/feat: trigram tokenizer pinned; CJK 3-char match; 2-char empty`
14. `test/feat: bilingual sibling probe inserts two fts_cases rows`
15. `test/feat: incremental upsert diff via body_sha256`
16. `test/feat: viewer.db.new atomic swap`

**Phase 3 — Body render** (`viewer/body_render/`):
17. `test/feat: select_body_source discriminator matrix` (8 combos including bilingual, orphaned, TC-only bare .html)
18. `test/feat: sanitizer allowlist + preserves parties/coram/date/representation`
19. `test/feat: render_case_body dispatches on html_generated_from` (native vs generated fragment)
20. `test/feat: cache key with format_availability_digest` (new .html sibling invalidates cache)
21. `test/feat: citation regex wrapper; /cite/ 200-unresolved on miss`

**Phase 4 — Routes** (`viewer/routes/`):
22. `test/feat: /case/{court}/{year}/{num}` 200 with body + upstream_status strip when orphaned
23. `test/feat: /case/.../cited-by` HTMX lazy tab, swap-target correct
24. `test/feat: /case/.../authorities-cited`
25. `test/feat: /case/.../parallel-cites`
26. `test/feat: /cite/{neutral}` 302 hit / 200 miss
27. `test/feat: /search` escape + form-field filter binding
28. `test/feat: /court/{slug}` years grid
29. `test/feat: /court/{slug}/{year}` list page
30. `test/feat: /browse` requires court prefilter
31. `test/feat: /authorities` index (banner when hub_cache empty)

**Phase 5 — CLI + Style**:
32. `test/feat: hklii serve` boots, binds 127.0.0.1:8787, GET / returns 200
33. `test/feat: viewer.db missing exits 1` with actionable message
34. `test/feat: bcp47 filter` maps 'tc' → 'zh-Hant'; `body_lang` drives `<article lang>`
35. `test/feat: app.css` + sanitizer round-trip through golden fixtures

**Phase 6 — Contract + Integration** (in downloader package):
36. Contract tests per shared table (cases, citations, citation_hub_cache, case_parallel_cites, ord_reg_edges, appeal_history JSON, generated HTML, press summary HTML)
37. `assert_htmx_swap_matches` helper in `tests/conftest.py`
38. `test_checkpoint_schema_matches_graph_selects` (EXPLAIN QUERY PLAN smoke test)

Total: ~40 commit pairs. Two-to-three focused working sessions. Every test paste-the-failing-output before implementation per CLAUDE.md non-negotiable #1.

**Rationale**: Downloader migrations first prevents the citation-map angle from writing SQL against tables that don't exist. `graph.py` before routes prevents route code from carrying inline SQL that will later drift. HTMX partials tested with a swap-mode-aware helper closes the L4 wrong-side hazard specific to this UI pattern.

**Risks + mitigations**: (a) A Phase 0 migration slows main-branch iteration — landed as small independent commits, no rebase pain. (b) TDD discipline may slip on Jinja templates — golden-fixture tests catch template drift.

**Integration notes**: This sequence assumes the `worktree-local-viewer` branch stays close to `main` throughout. If `main` advances, rebase Phase 0 first before starting Phase 1.

## 12. Deliberate non-goals & deferred

Recap of hard non-goals from the task brief + citation-graph-design.md §6:
- Online sync, remote sharing, multi-user
- Authentication / authorization
- Force-directed graph explorer
- Treatment labels on citations
- Character-level pinpoints
- Custom search ranking beyond BM25
- External search service (Meilisearch/Typesense/Elasticsearch)
- Client-side React/Vue/Solid/Svelte
- Non-localhost bind

**Explicitly deferred to v2** (adversarial review surfaced across the 7 angles):

**Search**: hand-rolled query DSL parser; `sort:authority` FTS rank; `fts_summaries` (press summaries) and `fts_legis` separate indexes; ICU tokenizer opt-in; 0.1%-failure-ratio ceremony; startup FTS module probe with mocked test; automatic staleness banner comparing checkpoint mtime.

**Body render**: `hklii viewer audit --unknown-tags` CLI; `hklii viewer prerender` cache warmer; `sanitizer_version` middleware auto-invalidation; bilingual codepoint-ratio heuristic with `<span lang="zh-Hant">` per-run wrapping; user-visible source-provenance strip for `has_synth_anchors`; hover previews on citation links; shared `iter_text_nodes` contract test locking RAG's future usage.

**Browse**: persistent `viewer_meta.db`; `has_translation`/`has_summary_en`/`has_summary_zh`/`has_appeal_history` filters; `--incremental`/`--allow-drift` materializer flags; row-count drift banner + mtime staleness detector; max-depth pagination guard; four page-size options; format icons for `.json` and `.txt`; grep-based module-boundary lint test; HTMX partial swap on filter changes; enrichment badges on browse rows (belong to case-detail).

**Citation viz**: court MULTI-select facet; hover-preview subsystem; framework-agnostic AST assertion test; drift-percentage staleness badge (`hklii verify --graph` owns it); per-court hub p99 threshold; sortable-by-court-header on `/authorities`.

**CLI**: `--host` non-loopback bind with confirmation; five-exit-code enumeration; `--refresh-sec` / `--no-refresh`; connection pool; startup `PRAGMA integrity_check`; `--build-index-on-start`; `--open` flag; auto-refresh WAL watcher; `hklii doctor` diagnostic verb.

**Styling**: `--vendor-fonts` Noto Sans TC opt-in; startup CJK-font detection heuristic; manual dark-mode toggle with localStorage; critical CSS inlining + async load; `.split-panes` bilingual side-by-side; Playwright dark-mode screenshot regression; SRI hash on locally-vendored `htmx.min.js`; per-court palette beyond `.badge` + `.badge--apex`.

**Testing**: Playwright e2e (browser download + subprocess uvicorn); WAL concurrency test with background writer thread; `--host 0.0.0.0` rejection test; `--durations=10` CI-lite perf budgets; 5k-row FTS build perf test; Hypothesis property-based tests for graph traversal; full-corpus DB copy for tests (29 GB); docker-compose test harness.

**Rationale**: Each deferred item can land in its own branch when a concrete pain justifies it. The v1 deliverable is deliberately modest — the user is Sean on a laptop, and the corpus is static. Ship the smallest thing that makes 162k judgments browsable + searchable + linkable end-to-end, then iterate on lived friction.

**Risks + mitigations**: (a) Someone re-litigates a non-goal — point at this section. (b) A v2 candidate turns out to be v1-critical for a real workflow — treat it as an unplanned promotion; add the smallest sufficient version.

**Integration notes**: When any v2 candidate lands, revisit the corresponding v1 section for L1-L5 lens review before implementation. Each deferred item was analyzed once in this document; landing it should not skip a fresh look.