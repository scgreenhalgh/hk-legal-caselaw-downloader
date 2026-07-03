# Bulk Scraper with Safety & Anti-Detection

## Context

v0.1.0 downloads individual cases. Next step: bulk scrape 114,379 judgments across 4 target courts (hkcfi, hkca, hkdc, hkcfa). The `getcasefiles` API eliminates brute-force — full corpus enumerable in ~13 API calls.

Primary concern: **protect user's IP/identity during bulk scraping.** Proxy-only by default, with killswitch, circuit breaker, and anti-detection. Explicit `--direct` flag to opt out.

---

## Safety Architecture (3 Layers)

### Layer 1 — Structural: ProxyPool is the only HTTP path (bulk scraper)

```
CLI (scrape)
 └─ BulkScraper
     └─ ProxyPool.get(url)          ← only way to make HTTP requests
         ├─ session selection (round-robin)
         ├─ throttling (RequestThrottler)
         ├─ header rotation (HeaderRotator)
         ├─ IP verification (runtime)
         ├─ circuit breaker (per-session, with cooldown recovery)
         └─ httpx.AsyncClient (internal, never exposed, trust_env=False)
```

**Scope of invariant**: The bulk scraper's only request path is `ProxyPool`. The legacy `download` command uses a single proxied `httpx.AsyncClient` (via `make_async_client`) with no preflight — acceptable for 1-5 URLs, no fallback to direct. Both commands require `--proxy` or `--direct`.

**`trust_env=False`**: Every `httpx.AsyncClient` in the codebase MUST set `trust_env=False`. Without it, httpx honors `NO_PROXY`/`HTTP_PROXY` env vars, silently bypassing the proxy — the most concrete leak vector.

The enumerator and scraper receive a `get` callable (`pool.get`), not a client or pool. In tests, a simple async mock function replaces it.

### Layer 2 — Verification: Preflight + runtime IP checks

- **Preflight**: Detect home IP via direct request to IP echo service (`httpbin.org/ip`, fallback `ipinfo.io/json`). Then verify each proxy returns a different IP using the **same echo service**. Leaking proxy marked dead. Zero healthy → abort before any HKLII requests.
- **No home IP = no scrape**: If the echo service is unreachable and home IP can't be determined, abort immediately.
- **Runtime**: Every N requests per session, re-check exit IP. If any proxy returns home IP → **re-verify once** (avoid false positives from caching/v4-v6 mismatch) → if confirmed, `IPLeakError` → **full stop**, log both IPs for diagnosis, checkpoint saved.

### Layer 3 — CLI: Explicit opt-in required

- No `--proxy` AND no `--direct` → error: "Must specify --proxy or --direct"
- `scrape --direct` → requires `--yes` or interactive confirmation
- No fallback: if a proxy fails, the system NEVER retries without a proxy

---

## Anti-Detection

| Component | Behavior |
|-----------|----------|
| **RequestThrottler** | Base delay `uniform(0.5, 1.5)`, ~5% reading pauses `uniform(3, 8)`, burst clusters of 2-5 then gap |
| **HeaderRotator** | Pool of Chrome UA variants (versions, OS). One UA per proxy for the entire run (no mid-session rotation — that's itself a bot tell). Different UA per proxy. |
| **Referer headers** | Simulate navigation from court listing → judgment page |
| **Per-session locking** | asyncio.Lock per proxy — natural 1 req/sec per proxy |

All use injectable `random.Random` for deterministic testing.

---

## Module Architecture

### New files

| File | Purpose |
|------|---------|
| `src/hklii_downloader/proxy_pool.py` | `ProxyPool`, `ProxySession`, `RequestThrottler`, `HeaderRotator`, `IPLeakError`, `AllProxiesDeadError`, `PreflightResult` |
| `src/hklii_downloader/enumerator.py` | `CaseEntry`, `parse_case_entry()`, `enumerate_court()`, `enumerate_courts()`, `extract_press_summary_url()` |
| `src/hklii_downloader/checkpoint.py` | `CheckpointDB` (SQLite with WAL mode + `busy_timeout=5000`), `CaseRecord` |
| `src/hklii_downloader/scraper.py` | `BulkScraper` orchestrator, `ScrapeResult` |

### Modified files

| File | Changes |
|------|---------|
| `src/hklii_downloader/client.py` | Extract `parse_judgment_response()` (pure dict→Judgment, no I/O) and `save_judgment_local()` (no network, excludes .doc). Add `trust_env=False` to `make_async_client`. Existing functions unchanged for backward compat. |
| `src/hklii_downloader/cli.py` | Convert from `@click.command()` to `@click.group()` with `download` and `scrape` subcommands. **Breaking change**: `hklii URL1` becomes `hklii download URL1`. |
| `pyproject.toml` | Add `[tool.pytest.ini_options] asyncio_mode = "auto"` |

### New test files

`tests/test_client.py` (characterization tests first), `tests/test_proxy_pool.py` (exists, needs mocking fixes), `tests/test_checkpoint.py`, `tests/test_enumerator.py`, `tests/test_scraper.py`, `tests/test_cli.py`

---

## Key Signatures

### ProxyPool (safety core)

```python
@dataclass
class PreflightResult:
    home_ip: str
    healthy_proxies: list[str]     # proxy URLs that passed
    leaked_proxies: list[str]      # "http://...:8888 returned home IP 203.0.113.1"
    failed_proxies: list[str]      # "http://...:8889 unreachable: ConnectionError"

class ProxyPool:
    def __init__(self, proxy_urls, direct=False, ip_check_interval=50,
                 max_failures=5, cooldown_seconds=300,
                 _transport_factory=None):  # test seam
    async def preflight(self) -> PreflightResult
    async def get(self, url, **kwargs) -> httpx.Response  # the ONE request path
    def _next_healthy_session(self) -> ProxySession  # round-robin, skips dead
    async def close(self) -> None
```

All internal `httpx.AsyncClient` created with `trust_env=False`.

### Enumerator (decoupled from proxy infra)

```python
async def enumerate_court(court, get, lang="en", items_per_page=10_000,
                          on_page=None) -> list[CaseEntry]
# `get` is a callable (pool.get or test mock) — no httpx dependency
# Pagination: compute pages = ceil(totalfiles / items_per_page) from first response

def extract_press_summary_url(html: str) -> str | None
# Pure function, regex for press summary links in judgment HTML
```

### CheckpointDB (SQLite)

```python
class CheckpointDB:
    # Schema: PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
    # Status values: pending, in_progress, downloaded, failed
    def upsert_case(self, court, year, number, neutral, title, date)
    def claim_pending(self, court=None) -> CaseRecord | None  # atomic pending→in_progress
    def mark_downloaded(self, court, year, number, formats)
    def mark_failed(self, court, year, number, error)
    def release_in_progress(self)  # reset in_progress→pending (on startup/resume)
    def pending_cases(self, courts=None) -> list[CaseRecord]
    def stats(self) -> dict[str, int]  # {total, pending, in_progress, downloaded, failed}
```

### BulkScraper

```python
class BulkScraper:
    async def enumerate(self, courts) -> int     # Phase 1: populate checkpoint
    async def download_all(self) -> ScrapeResult # Phase 2: download pending cases
    # Workers pull from asyncio.Queue, fed by single pending_cases() call
    # Errors: 429/5xx → backoff+retry (bounded), then mark_failed
    #         404/410/JSONDecodeError → mark_failed immediately
```

---

## CLI Design

```bash
# Download specific cases (requires --proxy or --direct)
hklii download URL1 URL2 --proxy http://localhost:8888
hklii download URL1 --direct

# Bulk scrape (requires --proxy or --direct)
# Default formats: html,txt,json (no doc — .doc disabled by default in bulk mode)
hklii scrape --proxy http://localhost:8888 --proxy http://localhost:8889
hklii scrape --courts hkcfi,hkca --proxy http://localhost:8888
hklii scrape --direct --yes        # skips confirmation
hklii scrape --resume              # re-enumerates (upsert), downloads remaining
hklii scrape --limit 10            # smoke test: stop after N downloads
hklii scrape --format html,txt,json --allow-doc  # explicitly enable .doc
```

Output structure for bulk scrape: `downloads/{court}/{year}/{court}_{year}_{num}.{ext}`

Progress display: periodic stats to stderr (done/pending/failed, req/sec, proxy health, ETA).

---

## Data Flow (single judgment through bulk scraper)

```
1. Worker pulls case from asyncio.Queue (fed by checkpoint.pending_cases())
2. pool.get(case.api_url):
   a. _next_healthy_session() → round-robin pick (skips dead, checks cooldown recovery)
   b. session.throttler.next_delay() → await asyncio.sleep(1.2s)
   c. Runtime IP check every N requests → compare to home_ip → re-verify if match
   d. Apply session headers + referer
   e. resp.raise_for_status() first, then resp.json()
   f. record_success() or record_failure()
3. judgment = parse_judgment_response(case, resp.json())
4. save_judgment_local(judgment, output_dir/court/year/, formats)
5. press_url = extract_press_summary_url(judgment.content_html)
   → if found: checkpoint.set_press_summary_url(court, year, number, url)
6. checkpoint.mark_downloaded(court, year, number)
```

Error handling per request:
- **429** → exponential backoff + retry same case (max 3 retries), global slowdown
- **5xx / timeout / ConnectionError** → retry with backoff (max 3), then mark_failed
- **404 / 410** → mark_failed immediately (case doesn't exist)
- **JSONDecodeError** → mark_failed (got HTML error page, not JSON)

Concurrency: per-session asyncio.Lock → N concurrent workers where N = healthy proxy count.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Proxy dies mid-download | `record_failure()` → circuit breaker may kill it → cooldown timer starts → after K seconds, re-test and revive if healthy |
| All proxies dead | `AllProxiesDeadError` → checkpoint saved → exit with resume instructions |
| SIGINT during download | try/finally → release in_progress claims → checkpoint.close() → WAL prevents corruption |
| IP leak at runtime | Re-verify once → if confirmed: `IPLeakError` → **full stop** → log both IPs → checkpoint saved |
| Home IP unreachable | Preflight aborts: "Cannot determine home IP — refusing to start" |
| Checkpoint DB corrupted | PRAGMA integrity_check → rename to `.corrupt` → create fresh → warn user |
| 429 rate limit | Backoff + retry same case (bounded) + global slowdown. NOT mark_failed. |
| 5xx / timeout | Backoff + retry (bounded), mark_failed only on exhaustion |
| 404 / JSONDecodeError | mark_failed immediately (permanent error) |
| Resume after interruption | Re-enumerate (upsert adds new cases), release stale in_progress → pending, download remaining |
| .doc requested in bulk | Rejected unless `--allow-doc` passed. Different host needs different throttling. |

---

## Implementation Order (TDD)

Each step: failing test → paste assertion failure → implement → verify.

| # | Component | Type | Notes |
|---|-----------|------|-------|
| 0 | Characterization tests for client.py | Tests | Lock existing `fetch_judgment`, `save_judgment`, `make_async_client` behavior before refactoring. |
| 1 | `parse_judgment_response` + `save_judgment_local` + `trust_env=False` | Refactor client.py | Extract pure functions under characterization test coverage. |
| 2 | `RequestThrottler` | Sync, no deps | Stub class first so tests reach assertions (not ImportError). |
| 3 | `HeaderRotator` | Sync, no deps | One UA per proxy, no mid-session rotation. |
| 4 | `ProxySession` | Sync state | Circuit breaker with cooldown recovery. |
| 5 | `ProxyPool` + fix test mocking | Async | Replace class-level patches with `_transport_factory` + `httpx.MockTransport`. Define `PreflightResult` dataclass. |
| 6 | `CheckpointDB` | Sync SQLite | WAL + busy_timeout=5000. `claim_pending()` for atomic dispatch. In-memory SQLite for tests. |
| 7 | `CaseEntry` + `parse_case_entry` | Sync | Parse getcasefiles response, extract year/num from path. |
| 8 | `enumerate_court` | Async | Pagination via `ceil(totalfiles / items_per_page)`. Mock `get` callable. |
| 9 | `extract_press_summary_url` | Sync | Regex for press summary links in judgment HTML. |
| 10 | `BulkScraper` | Async | asyncio.Queue dispatch, backoff+retry for transient errors. |
| 11 | CLI: Click group + `download` | CLI | CliRunner tests. Retrofit --proxy/--direct. |
| 12 | CLI: `scrape` subcommand | CLI | --limit, --allow-doc, --resume, progress display. |
| 13 | Integration test | End-to-end | MockTransport, enumerate → download → checkpoint → resume. |

---

## Dependencies

No new runtime deps. sqlite3 is stdlib. pytest-asyncio already in dev deps.

## Verification

1. Unit tests for all modules (pytest)
2. Integration test: mock HTTP, full scrape pipeline with 3 fake cases
3. Manual: `hklii scrape --limit 10` against real VPN proxies, verify IP check + output
4. Manual: Ctrl+C during scrape, verify checkpoint saves and resume works
5. Manual: kill a gluetun container mid-scrape, verify circuit breaker + cooldown recovery
