# Scraper Architecture

This chapter is the map of the code. Every module in `src/hklii_downloader/`, how they connect, the retry policy, the checkpoint schema, and the atomic-write model — each anchored to the file and line where it lives. Sibling chapters describe *why* individual pieces exist the way they do ([HTTP headers](./05-http-headers.md), [TLS/HTTP-2 fingerprinting](./06-tls-http2-fingerprinting.md), [cookies and warm-up](./07-cookies-sessions-warmup.md), [content safeguards](./10-content-safeguards.md)); this one describes *what* runs.

The scraper is a single Python package installed as the `hklii` CLI. It runs one Python process, opens one SQLite checkpoint DB, holds `N` per-proxy HTTP clients under one asyncio event loop, and writes judgment files into a court/year directory tree. There are no worker processes, no message queues, no external state stores. All coordination lives in `.checkpoint.db`.

## Module inventory

Twelve modules under `src/hklii_downloader/`, totalling ~2,700 lines of Python:

| Module | Lines | Role |
|---|---|---|
| `__init__.py` | 0 | Empty package marker. |
| `atomic_write.py` | 54 | `part → fsync → os.replace → fsync-parent` helpers. |
| `checkpoint.py` | 415 | `CheckpointDB` (SQLite + WAL + fcntl lock), schema, verify, enrichment tracking. |
| `cli.py` | 681 | Click group with four subcommands (`download`, `scrape`, `verify`, `enrich`) and their `_run_*` async entry points. |
| `client.py` | 132 | `httpx.AsyncClient` factory used only by the one-off `download` subcommand, plus `Judgment` dataclass and `save_judgment_local` for the shared serialiser. |
| `enrichment.py` | 226 | Press-summary + appeal-history fetch/save helpers used by BulkScraper inline, plus the standalone `EnrichmentRunner` used by `hklii enrich`. |
| `enumerator.py` | 203 | `enumerate_court` page walk, `_get_json_with_retry`, and `extract_press_summary_urls` (BeautifulSoup). |
| `impersonate_client.py` | 90 | `httpx`-compatible wrapper around `curl_cffi.AsyncSession` — profile pool, exception translation, header sanitisation. |
| `logging_setup.py` | 36 | `FileHandler` attached to the `hklii_downloader` logger, writes to `<output>/<subcommand>.log`. |
| `parser.py` | 99 | `HKLIICase` dataclass, `parse_hklii_url`, `referer_for` (URL-derived Referer), `html_to_text`. |
| `proxy_pool.py` | 408 | `ProxyPool` orchestrator, `RequestThrottler`, `HeaderRotator`, `ProxySession` circuit breaker, IP-leak preflight, warm-up. |
| `scraper.py` | 375 | `BulkScraper` — enumerate, download_all workers, retry loop, doc fallback, challenge-page rejection. |

The line counts add up to `2719` (`wc -l src/hklii_downloader/*.py`), which is small enough to hold in your head and re-read end-to-end during a bug hunt. There are no code-generation tools, no plugin architecture, no framework — the runtime dependency surface is `click`, `httpx`, `curl_cffi`, `beautifulsoup4` + `lxml`, and `rich` for the progress bar.

## Flow overview

The runtime path for `hklii scrape` is a straight line:

```
click(cli.py:189)          — parse flags, validate --proxy vs --direct
    ↓
_run_scrape(cli.py:514)    — setup_logging, open CheckpointDB, build ProxyPool
    ↓
pool.preflight()           — home IP, per-proxy IP echo, warm-up GET
    ↓
BulkScraper(scraper.py:79) — enumerate + download_all
    ↓
scraper.enumerate → enumerate_court → pool.get → ImpersonateAsyncClient
    ↓                                       ↓
scraper.download_all                    curl_cffi.AsyncSession
    ↓ N asyncio workers                     ↓
    _download_one_impl → pool.get → HKLII /api/getjudgment
    ↓
    save_judgment_local (atomic_write_text)
    ↓
    _fetch_doc (atomic_write_bytes) [optional]
    ↓
    enrich_summaries_for_case / enrich_appeal_history_for_case [optional]
    ↓
    checkpoint.mark_downloaded / mark_failed
```

Two invariants worth pinning down before we go module-by-module:

1. **Every network call goes through `pool.get`.** Enumeration, judgment fetch, doc fallback, press-summary fetch, appeal-history fetch, IP echo, and warm-up all funnel through `ProxyPool.get()` (or its direct-mode twin). This is the only place headers, Referer, throttling, IP-leak re-check, and circuit-breaker accounting happen. See [HTTP headers](./05-http-headers.md) for the header composition and [Cookies + warm-up](./07-cookies-sessions-warmup.md) for session/warm-up mechanics.
2. **Every commit-to-disk goes through `atomic_write_*`.** Judgment HTML/TXT/JSON, .doc/.docx binaries, press-summary sidecars, appeal-history JSON, and the raw enumeration cache all use the same `.part → fsync → os.replace → fsync-parent` model in `atomic_write.py`.

## `BulkScraper.enumerate` (single-threaded (court, lang) iteration)

The enumerate pass walks `(court, lang)` pairs one at a time. There is deliberately **no** `asyncio.gather` here — `enumerate_court` itself is a sequential page walk (see next section), and running two courts concurrently would fire enumeration bursts down the same proxy pool while the scrape phase hasn't started yet.

```python
# scraper.py:108-153
async def enumerate(
    self, courts: list[str], langs: tuple[str, ...] = ("en", "tc"),
) -> int:
    ...
    for court in courts:
        for lang in langs:
            ...
            entries = await enumerate_court(
                court, self._get, lang=lang, items_per_page=10_000,
                save_response_to=(
                    self._output_dir / ".enum_cache"
                    if self._save_enum_responses else None
                ),
            )
            for entry in entries:
                self._checkpoint.upsert_case(
                    entry.court, entry.year, entry.number,
                    entry.neutral, entry.title, entry.date,
                    lang=lang, last_seen_at=run_ts,
                )
                seen.add((entry.court, entry.year, entry.number))
    return len(seen)
```

Each entry is committed to the checkpoint DB via `upsert_case` *before the next court starts*, so a mid-enum crash or Ctrl-C leaves the rows already enumerated intact. The returned integer is the count of distinct `(court, year, number)` tuples seen — bilingual enumerations of the same case collapse via the `seen` set.

The `_get_path_label()` helper at `scraper.py:155-163` logs a human-readable name of the `get` callable so the log line proves at run-time that enumeration is routed through `ProxyPool.get`, not through a bare httpx client. Example log line: `enumerate court=hkcfi lang=en via ProxyPool.get`.

## `enumerate_court` page walk

`enumerator.py:103-159` implements the actual paginated fetch:

```python
# enumerator.py:103-159 (abridged)
async def enumerate_court(
    court: str, get: Callable, lang: str = "en",
    items_per_page: int = 10_000, ...
) -> list[CaseEntry]:
    ...
    data = await _fetch_and_maybe_save(1)
    total = data.get("totalfiles", 0)
    if total == 0:
        return []
    total_pages = math.ceil(total / items_per_page)
    entries = [parse_case_entry(j, court) for j in data.get("judgments", [])]
    ...
    for page in range(2, total_pages + 1):
        page_data = await _fetch_and_maybe_save(page)
        page_entries = [parse_case_entry(j, court) for j in page_data.get("judgments", [])]
        entries.extend(page_entries)
    return entries
```

The wire call is:

```
GET https://www.hklii.hk/api/getcasefiles?caseDb={court}&lang={en|tc}&itemsPerPage={N}&page={n}
```

Encoded via `urlencode` at `enumerator.py:121-126`. Endpoint semantics and the JSON envelope are documented in [Endpoint reference](./03-endpoint-reference.md).

Page 1 is fetched, `totalfiles` is read, and `math.ceil(totalfiles / items_per_page)` decides how many more pages to walk. Pages 2..N are fetched sequentially, with no `asyncio.gather` and no overlap — each `await _fetch_and_maybe_save` completes (including its throttled delay inside `pool.get`) before the next page begins. For HKCFI at `items_per_page=10_000` and `totalfiles=64226`, this is `math.ceil(64226/10000) = 7` sequential API calls.

## Enumeration defaults (`items_per_page=10_000`; ~13 API calls for the whole corpus)

The `items_per_page=10_000` default is hardcoded at `scraper.py:139-140`:

```python
entries = await enumerate_court(
    court, self._get, lang=lang, items_per_page=10_000,
    ...
)
```

The function default is `items_per_page: int = 10_000` at `enumerator.py:107`. There is no CLI flag to override it. The comment at `scraper.py:130-138` justifies the number:

> `itemsPerPage=10000` — 13 total enumeration calls across the whole corpus. Trades on-wire pattern realism for speed + durability: the smaller values I tried earlier (20-50) turned each court into 2500+ sequential API calls, which pushed enumeration to 40+ min per court and any single mid-enum timeout wiped everything since entries only land in the DB after enumerate_court returns.

Concretely, for the production court set (`hkcfi`, `hkca`, `hkdc`, `hkcfa`) at both langs, page counts are:

| Court | totalfiles | Pages @ 10000 |
|---|---|---|
| hkcfi | 64,226 | 7 |
| hkca | 29,911 | 3 |
| hkdc | 18,118 | 2 |
| hkcfa | 2,143 | 1 |

At two langs each that's `(7+3+2+1) × 2 = 26` calls per full run, or ~13 if bilingual enumeration is skipped. The empirical justification for `10_000` (linear ~234 B/row, flat ~0.5-1.6 s server processing regardless of page size, no server-side cap enforcement) is in [Endpoint reference](./03-endpoint-reference.md); the alternatives-considered log is in [Decisions log](./12-decisions-log.md).

## Enumeration freshness (`--enum-max-age`)

The `--enum-max-age HOURS` flag (default 0) tells the scraper to skip `(court, lang)` enumeration if it was already run within the given window. The window check is at `scraper.py:116-124`:

```python
# scraper.py:116-124
if self._enum_max_age_hours > 0:
    last_ts = self._checkpoint.last_enumeration_ts(court, lang)
    if last_ts is not None and (run_ts - last_ts) < self._enum_max_age_hours * 3600:
        age_h = (run_ts - last_ts) / 3600
        _log.info(
            "skip enumerate court=%s lang=%s (last %.1fh ago, cache window %dh)",
            court, lang, age_h, self._enum_max_age_hours,
        )
        continue
```

`CheckpointDB.last_enumeration_ts(court, lang)` at `checkpoint.py:193-201` returns `MAX(last_seen_at) FROM cases WHERE court=? AND lang=?`. Because `upsert_case` stamps `last_seen_at` on every enumerated row (`scraper.py:150`), the max across a `(court, lang)` bucket is a good proxy for "when did we last enumerate this pair".

Log line format: `skip enumerate court=hkcfi lang=en (last 4.2h ago, cache window 12h)`. Because `_log` is `hklii_downloader.scraper` (see the [Logging](#logging) section below) these skip lines appear in `<output>/scrape.log`.

Default is `0` — enumeration always runs. This matches the operator's usual expectation on a resume: pick up any newly-published judgments before the download phase.

## Enumeration cache (`--save-enum-responses`)

When `--save-enum-responses` is set, every raw `getcasefiles` page response is written to disk as JSON before parsing. `enumerator.py:120-137` wires this in through a per-page inner helper:

```python
# enumerator.py:120-137
async def _fetch_and_maybe_save(page_num: int) -> dict:
    params = urlencode({
        "caseDb": court,
        "lang": lang,
        "itemsPerPage": items_per_page,
        "page": page_num,
    })
    data = await _get_json_with_retry(
        get, f"{_BASE_URL}/api/getcasefiles?{params}",
        max_retries, backoff_base,
    )
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        out = save_dir / f"{ts}_page{page_num:04d}.json"
        atomic_write_text(
            out, json.dumps(data, ensure_ascii=False), encoding="utf-8",
        )
    return data
```

Files land at `<output>/.enum_cache/{court}_{lang}/{run_ts}_page{NNNN}.json`. The `run_ts` is captured once at the top of `enumerate_court` (`enumerator.py:116`) so all pages from a single run share the same timestamp prefix — greppable, sortable, and safe to keep multiple runs side-by-side.

Why `atomic_write_text` instead of `Path.write_text`? A Ctrl-C mid-write would otherwise leave a truncated JSON file at the final path, and a later `hklii verify` or a manual re-parse would misread it as authoritative. The atomic-write model closes that window (see [`atomic_write` model](#atomic_write-model) below).

## `download_all` flow (`release_in_progress` reclaim → `self._workers` coroutines)

`scraper.py:165-206` runs the download phase:

```python
# scraper.py:165-206 (abridged)
async def download_all(self, on_progress=None) -> ScrapeResult:
    self._checkpoint.release_in_progress()

    counter_lock = asyncio.Lock()
    stats = {"downloaded": 0, "failed": 0, "dispatched": 0}

    async def worker() -> None:
        while True:
            async with counter_lock:
                if (self._limit is not None
                        and stats["dispatched"] >= self._limit):
                    return
                record = self._checkpoint.claim_pending()
                if record is None:
                    return
                stats["dispatched"] += 1
            try:
                success = await self._download_one(record)
            except Exception:
                # Belt-and-braces: _download_one catches known errors
                # already; this guard prevents an unforeseen bug from
                # cancelling sibling workers via asyncio.gather.
                success = False
            async with counter_lock:
                if success:
                    stats["downloaded"] += 1
                else:
                    stats["failed"] += 1
                ...

    await asyncio.gather(
        *[worker() for _ in range(self._workers)],
        return_exceptions=True,
    )
    return ScrapeResult(...)
```

Three details matter:

1. **`release_in_progress()` on entry** (`scraper.py:169`, implementation at `checkpoint.py:255-259`) flips any `status='in_progress'` row back to `'pending'`. A prior run crashed while holding cases; this reclaims them so a resume actually resumes.
2. **`asyncio.gather(..., return_exceptions=True)`** (`scraper.py:200-203`) means an uncaught exception in one worker does not cancel siblings. Combined with the belt-and-braces `try/except Exception` inside `worker()` (`scraper.py:187-191`), a single bad case cannot poison the whole run.
3. **`self._workers` count comes from `cli.py:561`**, which sets `workers = max(1, len(result.healthy_proxies))` in proxy mode and `workers = 1` in `--direct` mode. One worker per healthy proxy — the pool never has more concurrent request holders than it has sessions.

`claim_pending()` at `checkpoint.py:147-173` does the atomic pick: `SELECT ... WHERE status='pending' LIMIT 1` followed by `UPDATE ... SET status='in_progress'`. Because SQLite is in WAL mode with `busy_timeout=5000`, the concurrent workers serialise on the write cleanly — no explicit application-side locking needed for the pick.

## `_download_one` belt-and-braces

`scraper.py:208-230` wraps the retry-loop implementation with catches for two specific exception classes:

```python
# scraper.py:208-230
async def _download_one(self, record: CaseRecord) -> bool:
    try:
        return await self._download_one_impl(record)
    except IPLeakError as e:
        _log.warning(...)
        self._checkpoint.mark_failed(
            record.court, record.year, record.number,
            f"IPLeakError: {e}",
        )
        return False
    except OSError as e:
        _log.error(...)
        self._checkpoint.mark_failed(
            record.court, record.year, record.number,
            f"OSError during save: {e}",
        )
        return False
```

`IPLeakError` (`proxy_pool.py:16-17`) is raised from `_runtime_ip_check` when a proxy's exit IP matches the home IP twice in a row. `OSError` covers disk-full and permission-denied cases from atomic writes. Everything else — the ordinary retry-exhausted failures — is handled inside `_download_one_impl` by marking `mark_failed` and returning `False`, so it never reaches this outer catch.

The outer `worker()` in `download_all` has a *further* `try/except Exception` (`scraper.py:187-191`) as a second belt: any unforeseen bug that escapes `_download_one` is caught before it can cancel the `asyncio.gather` sibling workers.

## `_download_one_impl` retry loop (`max_retries+1` iterations)

`scraper.py:232-333` is the load-bearing body of the scrape. It runs a single retry loop that handles `httpx.RequestError`, `json.JSONDecodeError`, and any `_RETRYABLE_STATUSES` response uniformly:

```python
# scraper.py:238-281 (retry loop skeleton)
for attempt in range(self._max_retries + 1):
    try:
        resp = await self._get(case.api_url)
    except httpx.RequestError as e:
        if attempt < self._max_retries:
            await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
            continue
        self._checkpoint.mark_failed(..., f"{type(e).__name__} after {self._max_retries} retries: {e}")
        return False

    if resp.status_code in _PERMANENT_ERRORS:
        self._checkpoint.mark_failed(..., f"HTTP {resp.status_code}")
        return False

    if resp.status_code in _RETRYABLE_STATUSES:
        if attempt < self._max_retries:
            await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
            continue
        preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
        self._checkpoint.mark_failed(..., f"HTTP {resp.status_code} after {self._max_retries} retries; body: {preview}")
        return False

    try:
        data = resp.json()
    except json.JSONDecodeError:
        if attempt < self._max_retries:
            await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
            continue
        preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
        self._checkpoint.mark_failed(..., f"JSONDecodeError after {self._max_retries} retries; HTTP {resp.status_code}; body: {preview}")
        return False
```

Default `max_retries=3` (`scraper.py:87`) gives four total attempts. `_BODY_PREVIEW_LEN=200` (`scraper.py:27`) determines how much of a failed response body is preserved in the checkpoint `error` column (newlines flattened to spaces so a `sqlite3` `.dump` stays on one line).

After the loop successfully lands JSON, the code parses via `parse_judgment_response` (`client.py:57-72`), runs the challenge-page test (see [Content safeguards](./10-content-safeguards.md)), applies the empty-content and doc-fallback branches, saves the files, and marks the case downloaded — all inside a single `for attempt` iteration. There is no per-branch retry: once JSON parses, we commit to that response.

## Jittered exponential backoff

The same formula appears in both `scraper.py:50-58` and `enumerator.py:61-67`:

```python
# scraper.py:50-58 == enumerator.py:61-67
def _jittered_backoff(base: float, attempt: int) -> float:
    """Exponential backoff with multiplicative uniform jitter in [0.5, 1.5]."""
    return base * (2 ** attempt) * random.uniform(0.5, 1.5)
```

With the default `base=1.0`, sleep durations across four attempts (0..3) fall in these bands (seconds):

| attempt | `2**attempt` | jittered range |
|---|---|---|
| 0 | 1 | 0.5 – 1.5 |
| 1 | 2 | 1.0 – 3.0 |
| 2 | 4 | 2.0 – 6.0 |
| 3 | 8 | 4.0 – 12.0 |

The multiplicative `random.uniform(0.5, 1.5)` decorrelates concurrent workers. Without jitter, six healthy proxies all hitting HTTP 503 at second `T` would retry in lockstep at `T+1`, `T+2`, `T+4`, `T+8` — six identical retry patterns from six subnets, which is a log-analysis one-liner. With multiplicative jitter, the six retries scatter across a window of a few seconds and stop looking like automation.

The docstring at `scraper.py:51-57` explains the choice; the corresponding docstring at `enumerator.py:63-67` cross-references it.

## Retryable status sets

Two separate sets, deliberately different:

**Scraper** (`scraper.py:25-26`):
```python
_PERMANENT_ERRORS = {404, 410}
_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}
```

**Enumerator** (`enumerator.py:18-19`):
```python
_PERMANENT_STATUSES = {404, 410}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
```

Both share `{404, 410}` as permanent. Permanent errors call `mark_failed` immediately without retry (`scraper.py:251-256`) or `resp.raise_for_status()` at enumeration (`enumerator.py:86-87`).

The scraper retries **403** but the enumerator does not. Rationale: at scrape time we're hitting `/api/getjudgment?...` per-case, and a 403 there can be a transient proxy edge-case worth one or two retries. At enumeration time, a 403 on `/api/getcasefiles` after a bilingual sweep is more likely a positive block signal — retrying could aggravate the block. Both behaviours are current as of `2026-07-04`; neither has been stress-tested against a real block on HKLII (see [Decisions log](./12-decisions-log.md)).

Enumerator also has an escape clause at `enumerator.py:88`:
```python
if status in _RETRYABLE_STATUSES or status >= 500:
```
Any `>= 500` status is retried, so `599 Network Connect Timeout Error` or a bespoke 5xx from a middlebox will trip retry even if not explicitly enumerated.

The proxy pool has a **third** related set at `proxy_pool.py:203`:
```python
_PROXY_FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}
```

These are the codes that increment the `ProxySession` circuit-breaker counter (`proxy_pool.py:349-352`). Matches the scraper's retryable set — a code worth retrying is also a code worth counting against the proxy's health.

## `ProxyPool.get()` flow

`proxy_pool.py:321-359`:

```python
# proxy_pool.py:321-359 (abridged)
async def get(self, url: str, **kwargs) -> httpx.Response:
    if not self._preflight_done:
        raise RuntimeError("Must call preflight() before making requests")
    if self.direct:
        direct_headers = dict(kwargs.pop("headers", None) or {})
        direct_headers.setdefault("Referer", _referer_for(url))
        return await self._direct_client.get(url, headers=direct_headers, **kwargs)

    idx = await self._acquire_session()
    session = self.sessions[idx]
    client = self._clients[idx]
    throttler = self._throttlers[idx]
    headers = self._headers[idx]
    try:
        delay = throttler.next_delay()
        await asyncio.sleep(delay)
        if (session.request_count > 0
                and session.request_count % self._ip_check_interval == 0):
            await self._runtime_ip_check(session, client)
        req_headers = headers.generate(url)
        req_headers["Referer"] = headers.referer_for(url)
        try:
            resp = await client.get(url, headers=req_headers, **kwargs)
            if resp.status_code in _PROXY_FAILURE_STATUSES:
                session.record_failure()
            else:
                session.record_success()
            return resp
        except httpx.RequestError:
            session.record_failure()
            raise
    finally:
        if session.is_healthy:
            self._available.put_nowait(idx)
```

The steps:

1. **`_acquire_session()`** (`proxy_pool.py:361-373`) revives cooled-down sessions, checks any healthy remain (or raises `AllProxiesDeadError`), then `asyncio.wait_for` on the availability queue with 0.5 s timeout — a timeout loops back to re-check for revive/deadness.
2. **`throttler.next_delay()`** (see [`RequestThrottler` formula](#requestthrottler-formula) below) computes a per-request sleep that mixes the base jitter, occasional long pauses, and inter-burst gaps.
3. **Runtime IP re-check** every `ip_check_interval` requests (default 50, `proxy_pool.py:211`). Detail at `proxy_pool.py:381-402`.
4. **Header generation** — `headers.generate(url)` produces the navigation-vs-XHR-appropriate header set (see [HTTP headers](./05-http-headers.md)), and `headers.referer_for(url)` sets a URL-derived Referer.
5. **`client.get`** — this is either `ImpersonateAsyncClient.get` (production) or `httpx.AsyncClient.get` (tests). Response status is inspected: `_PROXY_FAILURE_STATUSES` → `record_failure()`, else `record_success()`. Exceptions bubble after `record_failure()`.
6. **`finally: put_nowait(idx)`** returns the session to the queue if it's still healthy. A killed session (5 consecutive failures) is dropped — it re-enters via `_revive_cooled_down_sessions` after its 300 s cooldown.

## `RequestThrottler` formula

`proxy_pool.py:32-60`:

```python
class RequestThrottler:
    def __init__(
        self,
        rng: random.Random | None = None,
        base_range: tuple[float, float] = (0.5, 1.5),
        pause_range: tuple[float, float] = (3.0, 8.0),
        pause_chance: float = 0.05,
        burst_size_range: tuple[int, int] = (2, 5),
        burst_gap_range: tuple[float, float] = (2.0, 4.0),
    ):
        ...
        self._burst_remaining = self._rng.randint(*burst_size_range)

    def next_delay(self) -> float:
        if self._burst_remaining <= 0:
            self._burst_remaining = self._rng.randint(*self._burst_size_range)
            return self._rng.uniform(*self._burst_gap_range)
        self._burst_remaining -= 1
        if self._rng.random() < self._pause_chance:
            return self._rng.uniform(*self._pause_range)
        return self._rng.uniform(*self._base_range)
```

Concretely:

- **Base delay**: `uniform(0.5, 1.5)` seconds — the ordinary pause between requests.
- **5% chance** each request pauses `uniform(3.0, 8.0)` seconds — simulates a user reading a document.
- **Burst discipline**: after every `randint(2, 5)` requests, the throttler waits `uniform(2.0, 4.0)` seconds before starting the next burst. `_burst_remaining` counts down inside a burst.

Average request cadence is ~2.08 s/request, i.e. ~1730 requests/hour/proxy. Six healthy proxies gives ~10,400 req/h; twenty (the full VPN pool, see [VPN pool](./08-vpn-pool.md)) gives ~34,600 req/h — enough headroom that the ~120K-request scrape phase finishes in under 5 hours on paper. Real wall-clock is dominated by retries and warm-up.

The delay is applied at `proxy_pool.py:337-338` inside `pool.get`:
```python
delay = throttler.next_delay()
await asyncio.sleep(delay)
```
Burst state persists across requests — the counter lives on the throttler instance and each proxy has one instance for the process lifetime.

## Per-proxy seeding

Deterministic per-proxy behaviour comes from three separately-seeded RNGs, all initialised at `proxy_pool.py:240-241` and `proxy_pool.py:259-262`:

```python
# proxy_pool.py:240-241 (throttler + header rotator seeding, per proxy index i)
self._throttlers[i] = RequestThrottler(rng=random.Random(i))
self._headers[i] = HeaderRotator(rng=random.Random(i + 1000))

# proxy_pool.py:259-262 (impersonate profile seeding, per proxy URL)
return ImpersonateAsyncClient(
    proxy=proxy_url, timeout=30.0,
    rng=random.Random(hash((proxy_url, "impersonate"))),
)
```

- **Throttler**: `random.Random(i)` — deterministic per proxy index. `i=0` throttler will produce the exact same delay sequence on every run. That's fine: the pool's global entropy comes from the concurrent interleaving of many workers, not from within-worker randomness.
- **Header rotator**: `random.Random(i + 1000)` — same-per-index but decoupled from throttler seeding so its Chrome-version / OS pick doesn't correlate with the delay pattern.
- **Impersonate profile**: `random.Random(hash((proxy_url, "impersonate")))` — keyed on the **URL string** not the index. If proxy 3 today is `http://localhost:8891` and proxy 3 next week is `http://localhost:8892`, the impersonate profile follows the URL, not the slot number. That preserves TLS-fingerprint stability across VPN-pool reshuffles.

The rationale (why deterministic assignment is safer than fresh randomness for TLS fingerprints) is in [TLS/HTTP-2 fingerprinting](./06-tls-http2-fingerprinting.md).

## Checkpoint schema

`checkpoint.py:33-54` — the sole SQLite table:

```sql
CREATE TABLE IF NOT EXISTS cases (
    court    TEXT NOT NULL,
    year     INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    neutral  TEXT NOT NULL,
    title    TEXT NOT NULL,
    date     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    formats  TEXT,
    error    TEXT,
    lang     TEXT NOT NULL DEFAULT 'en',
    last_seen_at INTEGER,
    summary_en_status     TEXT NOT NULL DEFAULT 'pending',
    summary_en_error      TEXT,
    summary_zh_status     TEXT NOT NULL DEFAULT 'pending',
    summary_zh_error      TEXT,
    appeal_history_status TEXT NOT NULL DEFAULT 'pending',
    appeal_history_error  TEXT,
    PRIMARY KEY (court, year, number)
);
```

Column meanings:

| Column | Type | Purpose |
|---|---|---|
| `court` | TEXT | HKLII slug: `hkcfi`, `hkca`, `hkdc`, `hkcfa`, etc. |
| `year` | INTEGER | Judgment year from neutral citation. |
| `number` | INTEGER | Judgment number from neutral citation. |
| `neutral` | TEXT | Full neutral citation (e.g. `[2026] HKCFI 3816`). |
| `title` | TEXT | Party names uppercased (from `getcasefiles`). |
| `date` | TEXT | ISO-8601 with `+08:00` offset. |
| `status` | TEXT | State machine: `pending` → `in_progress` → `downloaded`/`failed`. |
| `formats` | TEXT | JSON-encoded list of formats actually written (e.g. `["html","txt","json"]`). |
| `error` | TEXT | Failure reason for `status='failed'`, includes 200-char body preview for HTTP failures. |
| `lang` | TEXT | Language of the enumerated row: `en` or `tc`. Bilingual UPSERT collision rule prefers `en` (see [Upsert lang collision](#upsert-lang-collision) below). |
| `last_seen_at` | INTEGER | Unix timestamp of most recent enumeration; feeds `--enum-max-age` and `find_orphans`. |
| `summary_en_status`, `summary_en_error` | TEXT | Enrichment state for the English press summary. |
| `summary_zh_status`, `summary_zh_error` | TEXT | Enrichment state for the Chinese press summary. |
| `appeal_history_status`, `appeal_history_error` | TEXT | Enrichment state for appeal history JSON. |

Primary key is `(court, year, number)` — one row per case regardless of language. All three enrichment `*_status` columns default to `'pending'` and are cleared by `mark_enrichment` (`checkpoint.py:296-315`).

## Enrichment kinds and statuses

Enumerated at `checkpoint.py:56-57`:

```python
_ENRICHMENT_KINDS = ("summary_en", "summary_zh", "appeal_history")
_ENRICHMENT_STATUSES = ("pending", "downloaded", "na", "failed")
```

Status meanings:

| Status | Meaning |
|---|---|
| `pending` | Not yet attempted (schema default). |
| `downloaded` | Sidecar file written successfully. |
| `na` | Not applicable — the judgment HTML did not contain a press-summary anchor for this language (`enrichment.py:76-77`). Distinguishes "not published" from "we haven't tried". |
| `failed` | Fetch or save raised an exception; `*_error` column holds `f"{type(e).__name__}: {e}"`. |

`mark_enrichment` validates both `kind` and `status` against these tuples (`checkpoint.py:300-309`) — an unknown kind or status raises `ValueError`, not a silent SQL error.

## `CheckpointDB` PRAGMAs

Three PRAGMAs, applied in order at `checkpoint.py:66-79`:

```python
self._conn = sqlite3.connect(path)
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA busy_timeout=5000")
self._check_integrity(path)
self._conn.execute(_SCHEMA)
self._migrate_enrichment_columns()
self._conn.commit()
```

1. **`journal_mode=WAL`** — Write-Ahead Logging so concurrent readers (progress-bar refresh, `stats()`) don't block the writer. Also faster commit-per-row workload than the default rollback journal.
2. **`busy_timeout=5000`** — 5-second timeout on locked writes. With N async workers all committing `mark_downloaded` in quick succession, one occasionally hits a lock; a 5 s wait is far more than any commit takes.
3. **`integrity_check`** at `checkpoint.py:73-79`:
    ```python
    def _check_integrity(self, path: str) -> None:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] != "ok":
            self._conn.close()
            raise CheckpointCorruptError(
                f"integrity_check failed for {path}: {row[0]}"
            )
    ```
    Runs *before* schema apply. A corrupted DB from an earlier crash raises `CheckpointCorruptError` at open time instead of silently trying to write to it and cascading a mess.

## `CheckpointDB` lock (fcntl `LOCK_EX|LOCK_NB`)

`checkpoint.py:81-102` acquires a POSIX advisory lock on a sidecar file at `{path}.lock`:

```python
def _acquire_lock(self, path: str) -> None:
    lock_path = str(path) + ".lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _log.warning(
            "Could not create checkpoint lock file at %s (%s: %s); "
            "running without cross-process protection. Concurrent "
            "scrape runs against this DB WILL race and can corrupt "
            "state.",
            lock_path, type(e).__name__, e,
        )
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise CheckpointLockError(
            f"Another process holds the checkpoint lock at {lock_path}. "
            "Wait for it to finish or kill the stale process."
        )
    self._lock_fd = fd
```

**Two branches**:

1. **Happy path** — `os.open` succeeds, `fcntl.flock(LOCK_EX | LOCK_NB)` succeeds, the fd is stored on `self._lock_fd` for later release. If another process holds the lock, `flock` raises `BlockingIOError`, which is translated to `CheckpointLockError` — the second concurrent scrape aborts before it opens the DB.
2. **S-4 fallback** — if `os.open` itself fails (e.g. read-only filesystem, permissions), the code logs a WARNING and *continues without a lock*. Two concurrent scrape processes on such a mount will race. The design choice: fail-open with an explicit log line rather than fail-closed on filesystems that don't support the lock file. The mechanical behaviour is documented here; the rationale is in [Decisions log](./12-decisions-log.md).

Release path at `checkpoint.py:407-415`:
```python
def close(self) -> None:
    self._conn.close()
    if self._lock_fd is not None:
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._lock_fd)
        self._lock_fd = None
```

`fcntl.LOCK_UN` is issued before `os.close`; an `OSError` from the unlock is swallowed (the fd is closed either way, and the lock releases on close in any case).

## Migration (`_migrate_enrichment_columns`)

`checkpoint.py:104-126`:

```python
def _migrate_enrichment_columns(self) -> None:
    existing = {
        row[1]
        for row in self._conn.execute("PRAGMA table_info(cases)").fetchall()
    }
    if "lang" not in existing:
        self._conn.execute(
            "ALTER TABLE cases ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'"
        )
    if "last_seen_at" not in existing:
        self._conn.execute(
            "ALTER TABLE cases ADD COLUMN last_seen_at INTEGER"
        )
    for kind in _ENRICHMENT_KINDS:
        if f"{kind}_status" not in existing:
            self._conn.execute(
                f"ALTER TABLE cases ADD COLUMN {kind}_status "
                "TEXT NOT NULL DEFAULT 'pending'"
            )
        if f"{kind}_error" not in existing:
            self._conn.execute(
                f"ALTER TABLE cases ADD COLUMN {kind}_error TEXT"
            )
```

`PRAGMA table_info(cases)` returns one row per column with the column name in position `[1]`. The migration is idempotent: it only issues `ALTER TABLE` for columns not present. A checkpoint DB from an earlier version (pre-`lang`, pre-`last_seen_at`, pre-enrichment) opens cleanly and gains the new columns with their `DEFAULT`s applied to existing rows.

There is no explicit schema-version column — the columns themselves are the version. This is fine while the schema only grows and never renames. If a rename is ever needed, this file is where a proper migration ladder gets added.

## Upsert lang collision

`checkpoint.py:128-145`:

```python
def upsert_case(
    self, court: str, year: int, number: int,
    neutral: str, title: str, date: str, lang: str = "en",
    last_seen_at: int | None = None,
) -> None:
    self._conn.execute(
        "INSERT INTO cases (court, year, number, neutral, title, date, lang, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (court, year, number) DO UPDATE SET "
        "neutral=excluded.neutral, title=excluded.title, date=excluded.date, "
        "lang=CASE "
        "  WHEN cases.lang='en' OR excluded.lang='en' THEN 'en' "
        "  ELSE excluded.lang "
        "END, "
        "last_seen_at=COALESCE(excluded.last_seen_at, cases.last_seen_at)",
        (court, year, number, neutral, title, date, lang, last_seen_at),
    )
    self._conn.commit()
```

The two collision rules that matter:

1. **`lang` prefers English**: `CASE WHEN cases.lang='en' OR excluded.lang='en' THEN 'en' ELSE excluded.lang END`. If either the existing row *or* the new insert has `lang='en'`, the row stays/becomes `en`. Bilingual enumeration hits every case twice — once in the EN pass, once in the TC pass. This rule ensures the row is downloaded in English if English exists, matching the "EN wins for bilingual cases" behaviour advertised by `--lang both` help text (`cli.py:169`).
2. **`last_seen_at` uses `COALESCE`**: `COALESCE(excluded.last_seen_at, cases.last_seen_at)`. If the new insert passes `None` for `last_seen_at`, the existing timestamp is preserved. This means callers who don't care about enumeration timestamps (test code, one-off migrations) can't accidentally wipe them.

The other three columns (`neutral`, `title`, `date`) are always overwritten with the incoming values — HKLII's authoritative metadata wins over anything we might have stored earlier.

## `verify_downloaded_against_files`

`checkpoint.py:220-245`:

```python
def verify_downloaded_against_files(self, output_dir) -> int:
    from pathlib import Path
    output_dir = Path(output_dir)
    rows = self._conn.execute(
        "SELECT court, year, number, formats FROM cases WHERE status='downloaded'"
    ).fetchall()
    broken = 0
    for court, year, number, formats_json in rows:
        formats = json.loads(formats_json) if formats_json else []
        stem = f"{court}_{year}_{number}"
        case_dir = output_dir / court / str(year)
        for fmt in formats:
            ext = "docx" if fmt == "doc" and (case_dir / f"{stem}.docx").exists() else fmt
            path = case_dir / f"{stem}.{ext}"
            if not path.exists() or path.stat().st_size == 0:
                self._conn.execute(
                    "UPDATE cases SET status='pending', formats=NULL "
                    "WHERE court=? AND year=? AND number=?",
                    (court, year, number),
                )
                broken += 1
                break
    self._conn.commit()
    return broken
```

For every row marked `downloaded`, iterate its `formats` list, build the expected path `<output>/court/year/{court}_{year}_{number}.{ext}`, and check the file exists **and** is non-zero-byte. If either check fails, flip the row back to `status='pending'` with `formats=NULL`.

The `.doc`/`.docx` extension is negotiated: if `fmt == 'doc'` and a `.docx` exists at that stem, treat it as satisfying the `doc` format. This handles the fact that HKLII's judgment doc field on the Judiciary origin is now `.docx` for post-~2018 content but the format token in the CLI is still `doc` (see [Judiciary platform](./02-judiciary-platform.md)).

Semantics of the return value: number of rows flipped to `pending`. `hklii verify` (`cli.py:259-283`) prints this count alongside post-verify stats. The `_break` after the first missing format means each broken row is only counted once even if multiple formats are missing.

Limits: this check does not validate content (no HTML parse, no size threshold beyond >0), does not check enrichment sidecars, and does not detect files that exist but were mutated after write. Those gaps are covered in [Content safeguards](./10-content-safeguards.md) and [Operations runbook](./11-operations-runbook.md).

## `atomic_write` model

`atomic_write.py:13-25` is the four-step commit primitive:

```python
def _fsync_and_replace(part: Path, dest: Path) -> None:
    fd = os.open(part, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, dest)
    # fsync the parent directory so the rename survives an unclean reboot.
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
```

Step-by-step:

1. **Write to `{dest}.part`** (via `Path.write_text` or `Path.write_bytes` inside the outer helpers). The final path never sees partial data.
2. **`os.fsync(fd)`** on the `.part` file — bytes go from OS buffer cache to disk. If we crash between steps 2 and 3, the `.part` is durable but not yet at the final path.
3. **`os.replace(part, dest)`** — POSIX atomic rename. Either fully-old-content or fully-new-content is visible at `dest`, never a partial mix.
4. **`os.fsync` on the parent directory fd** — the directory entry's rename is flushed. Without this, a crash after step 3 could leave the rename in the kernel's directory-inode cache but not on disk, so a reboot would show the file back under `.part`.

The `.part` suffix is appended via `path.with_suffix(path.suffix + '.part')` at `atomic_write.py:30,45`, so `a.html` writes to `a.html.part` while in flight.

Failure path — both `atomic_write_text` and `atomic_write_bytes` wrap the write in `try/except BaseException`:

```python
# atomic_write.py:28-54
def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    part = path.with_suffix(path.suffix + ".part")
    try:
        part.write_text(content, encoding=encoding)
        _fsync_and_replace(part, path)
    except BaseException:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise
```

`BaseException` catches `KeyboardInterrupt` too — a Ctrl-C mid-write cleans up the `.part` before propagating. `unlink()` swallows `FileNotFoundError` (the `.part` might not have been created yet if `open()` itself failed).

Called from every serialiser: `save_judgment_local` at `client.py:75-108`, `save_press_summary_local` at `enrichment.py:46-54`, `save_appeal_history_local` at `enrichment.py:57-63`, `_fetch_doc` at `scraper.py:353` (via `atomic_write_bytes`), and enumeration cache writes at `enumerator.py:134-136`.

## Enrichment split (inline in scraper, backfill via `EnrichmentRunner`)

Enrichment has two entry points:

1. **Inline during `scrape`** — when `--with-summaries` and/or `--with-appeal-history` are set, `_download_one_impl` calls `enrich_summaries_for_case` / `enrich_appeal_history_for_case` immediately after `mark_downloaded` succeeds (`scraper.py:326-329`). The judgment HTML is already in memory, so no re-read is needed:
    ```python
    # scraper.py:359-375 (dispatchers)
    async def _enrich_summaries(self, record, judgment, output_dir):
        await enrich_summaries_for_case(
            self._get, self._checkpoint,
            record.court, record.year, record.number,
            judgment.case.filename_stem, output_dir, judgment.content_html,
        )
    ```
    A failure here calls `mark_enrichment(..., 'failed', error=...)` (`enrichment.py:83-87`) but does *not* mark the judgment row failed — the judgment itself downloaded successfully; only the sidecar didn't.

2. **Backfill via `hklii enrich`** — `EnrichmentRunner` (`enrichment.py:109-227`) iterates over rows where `status='downloaded'` and at least one enrichment kind is still `'pending'` (`checkpoint.py:366-394`, `pending_any_enrichment`). For each case, it re-reads the on-disk sidecars:
    - Press-summary URLs come from re-parsing the saved `.html` file (`enrichment.py:188-197`).
    - Appeal-history case number comes from the saved `.json` sidecar's `case_number` field (`enrichment.py:205-221`).

    Missing preconditions have distinct failure messages: `"html file missing on disk"`, `"json sidecar missing on disk"`, `"case_number missing in json sidecar"`. This lets the operator distinguish "we tried and the origin returned nothing" from "we can't try because the input file's gone".

Both paths call the same `enrich_summaries_for_case` / `enrich_appeal_history_for_case` helpers, so the fetch/save/mark logic is identical regardless of entry point. The split is only in *how* the trigger fires.

Files landing on disk:

- `<output>/<court>/<year>/<stem>.summary_en.html` (English press summary)
- `<output>/<court>/<year>/<stem>.summary_zh.html` (Chinese press summary)
- `<output>/<court>/<year>/<stem>.appeal_history.json` (indented JSON, `ensure_ascii=False`)

Where `<stem>` is `<court>_<year>_<number>` from `HKLIICase.filename_stem` (`parser.py:36-37`).

## Logging

`logging_setup.py:14-32`:

```python
def setup_logging(output_dir: Path, subcommand: str) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{subcommand}.log"

    logger = logging.getLogger(_ROOT_LOGGER_NAME)   # 'hklii_downloader'
    logger.setLevel(logging.INFO)

    # Clear pre-existing handlers so re-invocation doesn't duplicate lines
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    return log_path
```

Behaviour:

- Root logger is `'hklii_downloader'` (`logging_setup.py:11`). All module loggers use dotted-child names — `hklii_downloader.scraper` at `scraper.py:13`, `hklii_downloader.checkpoint` at `checkpoint.py:10`. Records propagate up to the root and hit the file handler.
- Log level `INFO`. `_log.info(...)` calls in `scraper.py` (enumerate/skip lines) and `_log.warning(...)` / `_log.error(...)` from `_download_one` all land in the file.
- Log path is `<output>/<subcommand>.log` — `scrape.log` for `hklii scrape`, `enrich.log` for `hklii enrich`. `verify` and `download` do not currently call `setup_logging` (only `_run_scrape` at `cli.py:530` and — via analogous plumbing — `_run_enrich`). So `hklii verify` doesn't produce a log file.
- The handler-clear step at `logging_setup.py:22-24` prevents duplicate log lines when the same process invokes `setup_logging` more than once (mostly a concern for tests, not real CLI use).
- Format: `2026-07-04 13:47:03,142 INFO    hklii_downloader.scraper: enumerate court=hkcfi lang=en via ProxyPool.get`. The `%(levelname)-7s` left-pad keeps columns aligned.

Encoding is UTF-8 so bilingual challenge markers and Chinese titles from `error` columns log correctly. There is no rotation (`RotatingFileHandler` is not used); a multi-day run appends indefinitely to a single file. For long runs this is a known operational gap tracked in [Operations runbook](./11-operations-runbook.md).

## Cross-references

- [HTTP headers](./05-http-headers.md) — what `HeaderRotator._build_headers` and `HeaderRotator.generate(url)` produce for navigation vs XHR requests, what `parser.referer_for` derives per URL.
- [TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) — what `ImpersonateAsyncClient` and its `_IMPERSONATE_PROFILES` cover that we don't do ourselves.
- [Cookies + sessions + warm-up](./07-cookies-sessions-warmup.md) — how per-proxy `self._clients[i]` retain cookies, how `_warm_up_target` fires one landing-page GET, how `ProxySession` circuit-breaker + cooldown revive works.
- [Content safeguards](./10-content-safeguards.md) — the `_CHALLENGE_MARKERS` denylist, empty-content-with-doc-fallback branching, and what `verify_downloaded_against_files` doesn't catch.
- [Operations runbook](./11-operations-runbook.md) — how to invoke each subcommand and how to read the checkpoint DB and log lines.
- [Decisions log](./12-decisions-log.md) — why `10_000`, why `WAL`, why fsync-parent, why the fcntl warning-and-continue fallback, why the bilingual UPSERT rule.
