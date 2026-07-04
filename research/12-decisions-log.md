# Architectural Decisions Log

This chapter is the single source of truth for **why** the scraper is
shaped the way it is. Every non-trivial architectural choice — from
`itemsPerPage=10_000` to `fcntl.flock` to why we do not scrape LawCite —
is recorded here with the alternatives that were considered, the
empirical or theoretical data that justified the pick, and the date on
which the decision was made.

Every other chapter in this manual describes **how** the code works.
This one is where you look when you want to know **why** it works that
way and, more importantly, when a proposed change would violate a load-
bearing assumption.

Sibling chapters (each entry below cross-references at least one):

- [01 HKLII platform](./01-hklii-platform.md)
- [02 Judiciary platform](./02-judiciary-platform.md)
- [03 Endpoint reference](./03-endpoint-reference.md)
- [04 Anti-detection strategy](./04-anti-detection-strategy.md)
- [05 HTTP headers](./05-http-headers.md)
- [06 TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md)
- [07 Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md)
- [08 VPN pool](./08-vpn-pool.md)
- [09 Scraper architecture](./09-scraper-architecture.md)
- [10 Content safeguards](./10-content-safeguards.md)
- [11 Operations runbook](./11-operations-runbook.md)

A note on the timeline. The scraper was written in a compressed two-day
window between the initial commit (`06b36d9`, 2026-07-03) and the end of
the pre-production audit (`985cf02`, 2026-07-04). Most decisions carry
one of those two dates. Where a decision was made in a specific later
commit, its short SHA and audit-item ID (e.g. `M-1`, `S-3`) is cited.

---

## Format of an entry

Each decision below uses the same six-field skeleton. Sub-headings are
inlined into prose rather than repeated as bold labels, but the six
elements are always present.

| Field | What it captures |
|---|---|
| **Context** | The problem that forced the decision, plus what the code looked like before. If the decision was in the initial commit, "the alternative was not shipping" is the honest context. |
| **Decision** | One sentence stating what we chose. |
| **Alternatives** | Other options that were considered, with the specific reason each was rejected. If no alternative was considered, that is called out. |
| **Data** | Empirical evidence (endpoint probes, benchmark results, log analysis) or documented reasoning from the audit files under `scratchpad/`. Anchored to a file path or commit where possible. |
| **Date** | ISO-8601 date, with git commit SHA where it moves the discussion forward. Everything is 2026-07-03 or 2026-07-04 in this repo today. |
| **Cross-ref** | Which sibling chapter owns the running mechanics of the decision. Do not put implementation detail here — put it in the chapter that owns it. |

Decisions are grouped by concern: enumeration first, then fingerprinting
posture, then reliability, then things we deliberately do not do. Within
each group the order roughly follows implementation-layer dependency
(TLS before headers before cookies, and so on).

---

## Enumeration decisions

### Why `itemsPerPage=10_000`

**Context.** The enumerator (`src/hklii_downloader/enumerator.py:103-159`)
walks each court's `/api/getcasefiles` in fixed-size pages. Page size is
the single knob that moves both wire-pattern realism and wall-clock
throughput. It is hard-wired at `scraper.py:140` and defaulted at
`enumerator.py:107`; there is deliberately no CLI flag.

**Decision.** Use `itemsPerPage=10_000` for every court that accepts it,
delivering the full corpus in roughly 13 enumeration calls (one page for
each of the seven ≤10k courts, plus multi-page fetches for HKCFI, HKCA,
HKDC, and HKCFA — see the per-court table in [03](./03-endpoint-reference.md)).

**Alternatives considered.**

- `itemsPerPage=10` interleaved with judgment downloads. Rejected. See
  the next section, ["Why we do NOT interleave enumeration"](#why-we-do-not-interleave-enumeration).
- `itemsPerPage=1000` (the "medium" middle ground). Would work — the
  server returns 1000 rows fine — but would produce 65 requests to
  HKCFI alone versus 7 at 10_000, with no measurable server-processing
  saving.
- `itemsPerPage=20-50` (an earlier experiment recorded in commit `6ddcccd`
  before being reverted at `13e82d0`). Rejected because it turned each
  court into 2500+ sequential API calls at 40+ minutes per court, and
  any single mid-enumeration timeout wiped every entry (rows are only
  persisted to the checkpoint after `enumerate_court` returns — see the
  inline comment at `scraper.py:130-138`).
- `itemsPerPage=50_000`. The homepage's own listing view uses this
  ceiling. Untested for our purposes; kept at 10_000 to leave a factor-
  of-5 headroom before the server appears to bother.

**Data.** Endpoint probe on 2026-07-04 across `pageSize=10/50/100/1000`
(files `scratchpad/hdr_s{10,50,100,1000}.txt` and `metrics_s*.txt`):

| pageSize | rows returned | body bytes | server processing | bytes/row |
|---|---|---|---|---|
| 10   | 10   | 2 354   | 1.66 s | 235 |
| 50   | 50   | 11 697  | 0.50 s | 233 |
| 100  | 100  | 23 429  | 0.56 s | 234 |
| 1000 | 1000 | 234 043 | 0.87 s | 234 |

Two things fall out. First, response size is linear at ~234 B/row with
~30 B fixed envelope — there is no per-row processing penalty visible at
the origin. Second, `pageSize=1000` took less server-side time (0.87 s)
than `pageSize=10` (1.66 s), i.e. the server is happier with fewer,
larger fetches than with many small ones. Nothing in the probe data
argues for smaller pages.

**Date.** 2026-07-03 initial commit; revisited and confirmed 2026-07-04
after the pre-production audit re-probed the endpoint.

**Cross-ref.** [03 Endpoint reference](./03-endpoint-reference.md) owns
the probe data; [09 Scraper architecture](./09-scraper-architecture.md)
owns the enumerator code.

### Why we do NOT interleave enumeration

**Context.** During the pre-production audit an alternative pattern was
proposed: enumerate a small batch (say 10 items) then hand those 10 to
the download workers, and only fetch the next enumeration page once the
current batch drains. Framed as "streaming" versus "batch" enumeration.

**Decision.** Do not interleave. Stay with the current pattern:
enumerate the entire (court, lang) pair to completion, commit every row
to SQLite, then dispatch `download_all` workers.

**Alternatives considered.**

- **H1: pageSize=10 interleaved, strict-sequential** (workers wait for
  next enum page before drawing). Rejected.
- **H2: pageSize=1000 interleaved.** Rejected.
- **H3: pageSize=10_000 batched (status quo).** Kept.
- **H4: pageSize=1000 streaming.** The enumerator feeds a queue that
  workers drain in parallel. The only proposal that beats status quo
  materially, and only by ~7 minutes over an ~11-hour download window.
  Deferred to Tier C as a moderate refactor with a small payoff.

**Data.** From `scratchpad/patternCritique.json`:

| Pattern | Enum requests (HKCFI, per lang) | Wall clock estimate |
|---|---|---|
| Status quo (10_000, batch) | 7 | 7 min enum + 11.3 hr downloads = ~11.4 hr |
| H1 (pageSize=10, interleaved) | 6 423 | ~20.4 hr |
| H2 (pageSize=1000, interleaved, strict-sequential) | 65 | ~14.1 hr |
| H4 (pageSize=1000, streaming) | 65 | ~11.3 hr |

The interleaved-10 proposal generates 300× to 900× more enumeration
requests than the pageSize=1000 baseline and takes 1.5-2× longer wall-
clock (workers idle while waiting for the next 10-item page). It is not
merely worse — the sign is wrong on suspicion: 900× the enum-endpoint
volume from the same source IPs is itself a bot signal.

Additionally, from `scrapyPatterns.json`, no canonical Scrapy source
argues that 1000-item batches are impolite. F5 Labs, Cloudflare, and
DataDome key on request cadence, header uniformity, and volume — not on
payload size. Interleaving would trade a resilience benefit we already
have (rows commit per-batch to SQLite before the next batch begins, and
`release_in_progress` reclaims mid-flight work on restart — see
`scraper.py:169`) for a failure-surface expansion of 600× (a permanent
5xx on any one interleaved page halts the whole court mid-enumeration).

**Date.** Decided 2026-07-04 during the pattern critique step of the
pre-production audit.

**Cross-ref.** [09 Scraper architecture](./09-scraper-architecture.md)
owns the enumeration flow; [11 Operations runbook](./11-operations-runbook.md)
covers timing estimates for the full corpus grab.

### Why `itemsPerPage=10_000` has a per-court override caveat

**Context.** The pageSize probe used HKCFI, the largest court. The
recommendation "keep it at 10_000" was made on that basis. The
completeness audit called out that this had been validated for one
court out of fourteen (`completeness.json:gap 3`).

**Decision.** Ship `itemsPerPage=10_000` as the default but note that
seven of the fourteen documented courts return HTTP 500 at that page
size and either require a smaller value or should be fetched at their
actual corpus count.

**Alternatives considered.**

- Lower the global default to a value that all courts accept
  (`itemsPerPage=1000` returned 200 for every court probed). Rejected
  because it would inflate HKCFI enumeration from 7 to 65 requests for
  no benefit — the affected courts are all tiny (24-1789 rows) and hit
  the origin bug for other reasons.
- Add per-court page-size overrides in a dict. Deferred; the code today
  passes `10_000` unconditionally at `scraper.py:140`.

**Data.** The 14-court probe on 2026-07-04 (files
`scratchpad/probes/court_*.hdr` and `court_*.body`) split evenly by
status:

| Status | Courts |
|---|---|
| HTTP 200 at itemsPerPage=10_000 | hkcfi, hkca, hkcfa, hkdc, hkfc, hkmagc, hkoat |
| HTTP 500 at itemsPerPage=10_000 | hkcompet, hkcoroners, hkfamc, hklab, hklndtri, hkmc, hkstsc |

The 500 responses returned a Django default error HTML page
(`court_hkcompet.body`) with the same middleware headers as the 200s
(same duplicate `x-frame-options`, same gunicorn `server`, same CSP) —
just `content-type: text/html; charset=utf-8` and a 145-byte body. This
looks like a Django `ORM.count()` or memory-limit fault triggered on
tiny corpora when the paginator computes `total_pages` for a value much
larger than the row count. It is not a rate-limit or WAF response; the
same request at `itemsPerPage=1000` returns 200.

The current default set of courts (`hkcfi,hkca,hkdc,hkcfa` — see the
next decision) is exactly the four that succeed at 10_000, so the
production run does not see the bug. Only an operator running
`--courts hkcompet,hkfamc,...` from the CLI would encounter it, and the
enumerator's error path would surface the 500 as a JSONDecodeError with
a body preview (`scraper.py:275`) rather than silently misbehave.

**Date.** Discovered 2026-07-04 during the endpoint probe stage of the
pre-production audit; the per-court override work is deferred (audit
`completeness.json` gap 3).

**Cross-ref.** [03 Endpoint reference](./03-endpoint-reference.md) owns
the per-court data.

---

## TLS and header decisions

### Why `curl_cffi` over `httpx` alone

**Context.** The original client at initial commit was `httpx.AsyncClient`
with a hand-rolled Chrome UA (`client.py` before commit `2138365`). By
mid-audit the concern was that a future HKLII WAF flip would immediately
break every request — JA3/JA4/HTTP-2 mismatches between an `httpx.AsyncClient`
and a claimed "Chrome 148" UA are unrecoverable at connection time.

**Decision.** Wrap `curl_cffi.requests.AsyncSession` in an
`httpx.AsyncClient`-shaped shim (`src/hklii_downloader/impersonate_client.py`)
and route every production request through it. `curl_cffi` gives us
matching TLS ClientHello, HTTP/2 SETTINGS, WINDOW_UPDATE, and pseudo-
header order for a real Chrome build in one dependency.

**Alternatives considered.**

- Stock `httpx.AsyncClient` with `http2=True`. Rejected for the
  production path: `httpx[http2]` uses the `h2` Python library, which
  produces `h2`'s SETTINGS values and pseudo-header order — recognizably
  not Chrome. Retained for direct mode only (see ["Why HTTP/2 in direct mode"](#why-http2-in-direct-mode)).
- `tls-client` (Bogdanfinn's Go-bindings library). Rejected as redundant —
  it produces the same JA4 output that `curl_cffi` produces, with a
  larger dependency (bundled Go runtime) and no cleaner API. Documented
  in `hklii-waf-status.md:27-28` as "do not try".
- Full-browser automation (Playwright / Patchright / nodriver). Rejected
  as overkill. The 2026 Paterson benchmark
  (`mimicryStateArt.json`) shows `curl_cffi` ties `CloakBrowser` at 26/31
  Cloudflare-protected targets, versus 24-28 for the browser-driven
  tools. HKLII is not Cloudflare-protected today, so we are comfortably
  inside `curl_cffi`'s envelope with a fraction of the resource cost.

**Data.** HKLII is currently plain gunicorn/Apache with no CDN or WAF
(see [01](./01-hklii-platform.md)). The current defense is precautionary:
the incremental cost of running `curl_cffi` on a non-WAF origin is near
zero (one dependency, one shim, deterministic profile pick per proxy),
and adding `curl_cffi` retroactively — after a WAF flip breaks a live
run — would take substantially longer than pre-hardening does.

**Date.** 2026-07-04 in commit `2138365` (`feat: curl_cffi TLS+HTTP2
fingerprint impersonation in ProxyPool`).

**Cross-ref.** [06 TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md)
owns the mechanics.

### Why `chrome146/142/136/131` (plus bare `"chrome"` alias) and not `chrome104/110/116`

**Context.** The first `curl_cffi` pool shipped in commit `2138365` was
ten profiles across three vendors:

```python
_IMPERSONATE_PROFILES = (
    "chrome124", "chrome120", "chrome116", "chrome110", "chrome104",
    "safari17_0", "safari15_5", "safari15_3",
    "edge101", "edge99",
)
```

Audit `mimicryStateArt.json` flagged `chrome104/110/116/120/124` as
2-4 years stale in July 2026 — they carry the Chrome UA from 2022-2023
and the `Not/A)Brand;v="99"` GREASE token from before Google started
rotating it. That combination desynchronizes the JA4 fingerprint from
the `sec-ch-ua` version, which is a Tier-3 tell (audit signal 8).

**Decision.** Replace the pool with a five-entry all-Chrome list:

```python
_IMPERSONATE_PROFILES = (
    "chrome", "chrome146", "chrome142", "chrome136", "chrome131",
)
```

The bare `"chrome"` alias auto-selects `curl_cffi`'s newest profile at
import time — currently `chrome146`. Explicit version pins spread the
JA4 hash across four modern Chromes while keeping every profile within
Chrome's late-2024-to-mid-2026 release window.

**Alternatives considered.**

- Keep multi-vendor diversity (Safari, Edge). Rejected. Mixing Safari
  with the Chrome-shaped `HeaderRotator` in [05](./05-http-headers.md) or
  with `client.py`'s Chrome-hardcoded `_BROWSER_HEADERS` produces cross-
  layer inconsistency (a Safari TLS handshake carrying Chrome's
  `sec-ch-ua` is a tell in its own right). Would require making the
  header layer profile-aware, which is not on the critical path.
- Pin only to bare `"chrome"`. Rejected because it collapses fingerprint
  diversity: every proxy would emit the same JA4 hash. The four explicit
  pins let a JA4-collecting detector see four distinct fingerprints and
  register the run as browser-population diversity, not a single client.
- Keep `chrome104` as a "legacy Chrome" fallback. Rejected. Real-world
  July 2026 Chrome telemetry says < 0.5% of live traffic is pre-Chrome-140.

**Data.** From `mimicryStateArt.json`, `curl_cffi` 0.15.1b2 (June 2026)
supports profiles chrome99, 100, 101, 104, 107, 110, 116, 119, 120, 123,
124, 131, 133a, 136, 142, 145, 146. `chrome145`/`chrome146` include
HTTP/3 fingerprints (which we do not use — see
[06 TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) — the closing note on QUIC / HTTP/3 covers this). The specific
five-profile set was picked to span the version window at roughly one-
per-quarter granularity: Chrome 131 (late 2024), 136 (early 2025),
142 (mid-2025), 146 (mid-2026), plus the bare alias for automatic
tracking.

**Date.** 2026-07-04 in commit `ee7124a` (`feat: refresh curl_cffi
impersonate pool to modern Chrome only (item M-3)`).

**Cross-ref.** [06 TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) § "Profile freshness policy"
owns the refresh cadence.

### Why `parser.referer_for` over a hardcoded homepage `Referer`

**Context.** Before the audit, every proxied request sent
`Referer: https://www.hklii.hk/` — the exact homepage URL for every
`/api/*` XHR. That is audit suspicion signal 2:

> `SELECT src_ip WHERE COUNT(DISTINCT referer)=1 AND MAX(referer)='https://www.hklii.hk/' AND COUNT(*)>30`

A one-line log-analysis query flags any source IP that sent > 30 API
calls all with an identical homepage-string Referer, because real Chrome
XHR sets Referer to the document URL that fired the XHR — different for
each API path.

**Decision.** Introduce `parser.referer_for(url)`
(`src/hklii_downloader/parser.py:40-73`) as a pure function that maps a
target URL to a plausible SPA page URL:

| Target URL | Derived Referer |
|---|---|
| `/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234` | `https://www.hklii.hk/en/cases/hkcfi/2024/` |
| `/api/getcasefiles?caseDb=hkcfi&lang=en&...` | `https://www.hklii.hk/en/cases/hkcfi/` |
| `/en/cases/hkcfi/2024/1234` | `https://www.hklii.hk/en/cases/hkcfi/2024/` |
| anything else on `www.hklii.hk` | `https://www.hklii.hk/` |
| anything not `www.hklii.hk` | `https://www.hklii.hk/` |

Every proxied request (`proxy_pool.py:345`) and every direct-mode
request (`proxy_pool.py:327`) sets `Referer` via `referer_for(url)`.

**Alternatives considered.**

- Track the actual browsing sequence and set `Referer` to the previous
  URL fetched. Rejected as over-engineering: it would require modelling
  a fake user's page traversal per proxy, and the SPA-derived Referer
  above already produces > 3 distinct values per court (satisfying the
  log-analysis distinctness check).
- Drop `Referer` entirely. Rejected — an absent `Referer` on `/api/*` is
  Tier-2 heuristic ("empty Referer to deep pages"), audit signal 2.

**Data.** Audit `suspicionCritique.json` documents the log rule verbatim.
The fix is item M-1 in `synth.json` and shipped in commit `4af4d95`.

**Date.** 2026-07-04 in commit `4af4d95` (`feat: derive Referer from URL
context in ProxyPool + direct mode (M-1)`).

**Cross-ref.** [05 HTTP headers § Referer derivation](./05-http-headers.md)
owns the code; [04 Anti-detection strategy](./04-anti-detection-strategy.md)
signal 2 is what this defeats.

### Why the Sec-Fetch XHR split

**Context.** The initial `HeaderRotator._build_headers`
(`src/hklii_downloader/proxy_pool.py:101-122`) emitted a Chrome
*navigation* Sec-Fetch quartet on every request:

```
sec-fetch-site: same-origin
sec-fetch-mode: navigate
sec-fetch-dest: document
sec-fetch-user: ?1
Upgrade-Insecure-Requests: 1
```

But `/api/*` calls are not navigations — they are XHR/fetch from an
already-loaded page. Real Chrome sends a different quartet on XHR:

```
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
(no sec-fetch-user)
(no Upgrade-Insecure-Requests)
```

Audit `mimicryStateArt.json` calls this "the most common automation
bug": sending `Sec-Fetch-Mode: navigate` on XHR is trivially detected by
a ModSecurity rule (`sec-fetch-mode == 'navigate' AND uri MATCHES '^/api/'` — audit signal 6).

**Decision.** In `HeaderRotator.generate(url)`
(`src/hklii_downloader/proxy_pool.py:124-133`), detect `/api/` in the
URL and rewrite the quartet to the XHR shape, dropping `sec-fetch-user`
and `Upgrade-Insecure-Requests` entirely. Non-API URLs still get the
navigation quartet — the warm-up GET on `https://www.hklii.hk/` is a
real navigation, so it emits `navigate`/`document`/`?1`/`UIR:1`.

**Alternatives considered.**

- Emit only the navigation quartet everywhere and accept the tell.
  Rejected — it is the highest-signal single header pattern for
  distinguishing browser XHR from Python XHR.
- Emit only the XHR quartet everywhere. Rejected — the warm-up GET
  would then look like an XHR to `/`, which is impossible from a real
  browser (a `/` navigation never uses `fetch()`).

**Data.** ModSecurity rule verbatim in `suspicionCritique.json` signal 6.
`curl_cffi`'s impersonation profile also emits the correct Sec-Fetch
quartet on the production hot path (that is what `_FINGERPRINT_HEADERS`
strips at `impersonate_client.py:28-42` — see [06](./06-tls-http2-fingerprinting.md)),
but the `HeaderRotator` fix matters for the test suite path
(`httpx.MockTransport`) and for warm-up assembly.

**Date.** 2026-07-04 in commit `e94ccfd` (`feat: correct Sec-Fetch for
XHR vs navigation + delete rotate (M-2 + M-7)`).

**Cross-ref.** [05 HTTP headers § Sec-Fetch split](./05-http-headers.md)
owns the mechanics.

### Why we deleted `HeaderRotator.rotate`

**Context.** The pre-audit `HeaderRotator` shipped with two entry points:
`generate(url)` (called on every request from `ProxyPool.get`) and
`rotate()` (a public method that would swap the cached `_headers` dict
for a fresh randomization). `rotate()` was never called from anywhere in
the codebase — dead code from an early sketch of "rotate identity
periodically".

**Decision.** Delete `rotate()`.

**Alternatives considered.**

- Keep `rotate()` and wire it up (rotate identity every N requests).
  Rejected — it would break the deterministic per-proxy identity model
  (see ["Why per-proxy session with deterministic profile"](#why-per-proxy-session-with-deterministic-profile)).
  A proxy whose UA changes mid-session while its TLS fingerprint stays
  constant is itself a suspicion signal.
- Keep `rotate()` as dead code with a `# TODO` comment. Rejected — dead
  code without a caller is a maintenance hazard, and pytest coverage
  would silently flag it as untested forever.

**Data.** `grep -r 'rotate\b' src/` returned zero call sites at
audit time.

**Date.** 2026-07-04 in commit `e94ccfd` (joint with the Sec-Fetch fix,
audit item M-7).

**Cross-ref.** [05 HTTP headers](./05-http-headers.md) covers what
`HeaderRotator` does now that `rotate()` is gone.

### Why session warm-up

**Context.** Without warm-up, the first request from any freshly-spun-up
PIA proxy exit IP that HKLII sees is a hot XHR to `/api/getcasefiles`.
That is audit suspicion signal 4:

> `SELECT src_ip, MIN(uri) FROM access_log GROUP BY src_ip, TRUNC(ts, 30min) HAVING FIRST_URI LIKE '/api/%' AND count>5`

Every real Chrome session's first request is `GET /`, then CSS/JS/font/
favicon, then XHR ~200-2000 ms later once the SPA JS has executed. A
cold-cache API hit from a fresh IP is a browser-population outlier.

**Decision.** After `ProxyPool.preflight()` confirms a proxy is not
leaking home IP, fire one landing-page `GET https://www.hklii.hk/`
through that proxy's `ImpersonateAsyncClient`. Constant is
`_WARMUP_URL = "https://www.hklii.hk/"`
(`src/hklii_downloader/proxy_pool.py:197`); implementation is
`_warm_up_target` at `proxy_pool.py:292-304`. Best-effort — failure
does not disqualify the proxy since IP echo already confirmed
routability.

**Alternatives considered.**

- Warm up on every N-th request during the run, not just at preflight.
  Rejected — a landing-page GET after every ~30 minutes of API calls
  would be another cost centre with unclear payoff. Most real users do
  not re-navigate to the homepage mid-session either.
- Warm up on `/en/cases/hkcfi/` (a court landing page) instead of `/`.
  Rejected as unnecessary specificity; a `/` GET is the least
  suspicious first request possible and produces one Set-Cookie in the
  jar if the SPA sets any.
- Warm up on the SPA route the first API call belongs to. Rejected as
  over-engineering — that requires modelling which court the next
  request will hit before the queue is even drained.

**Data.** Log rule from `suspicionCritique.json` signal 4; implementation
is audit item M-4 in `synth.json`. The warm-up also seeds the per-proxy
cookie jar so subsequent `/api/*` requests echo back any Set-Cookie the
homepage returned, which is a partial defence against signal 5.

**Date.** 2026-07-04 in commit `16d6408` (`feat: per-proxy HKLII homepage
warm-up in preflight (item M-4)`).

**Cross-ref.** [07 § Session warm-up](./07-cookies-sessions-warmup.md)
owns the mechanics.

### Why per-proxy session with deterministic profile

**Context.** Each of the 20 proxies represents a distinct "user" from
HKLII's perspective — different exit IP, different (potentially) cookie
jar, different TLS fingerprint. Two ways to organise that: rebuild
identity per request (rotate everything every call) or pin identity per
proxy (each proxy gets one identity for the whole run).

**Decision.** Pin identity per proxy. Each proxy index gets a
deterministically-seeded RNG and a stable pick from every rotation pool:

- `random.Random(i)` seeds the `RequestThrottler` at `proxy_pool.py:240`.
- `random.Random(i + 1000)` seeds the `HeaderRotator` at
  `proxy_pool.py:241` (so Chrome major, Chrome full, OS, and
  `sec-ch-ua-platform` are stable across restarts).
- `random.Random(hash((proxy_url, "impersonate")))` seeds the
  `ImpersonateAsyncClient`'s profile pick at `proxy_pool.py:260-262` (so
  each proxy URL always gets the same `curl_cffi` profile).

The result: proxy 3 is always Chrome-131 on macOS with throttler
schedule X, proxy 7 is always Chrome-146 on Windows with throttler
schedule Y, and so on — for the whole run, and across restarts.

**Alternatives considered.**

- Per-request rotation. Rejected — cookie continuity breaks (a
  Set-Cookie from request N is invisible to request N+1 if the
  underlying `AsyncSession` is rebuilt), and rotating TLS fingerprint
  every call is itself a signal (a "real user" whose ClientHello cipher
  order changes between packets is not real).
- Non-deterministic per-proxy pick (seeded from process start time or
  `os.urandom`). Rejected — deterministic seeds make bug reports
  reproducible: "this exact combination of profile and throttle produced
  the failure" is testable.

**Data.** Audit suspicion signal 10 (`suspicionCritique.json`) actually
flags deterministic per-proxy stability as a tell:

> Same (IP, TLS, UA, throttle) tuple forever means a JA4-collecting
> detector can bake in "these 20 fingerprints are the same client fleet"
> after one long run.

We accept the trade-off. The countervailing benefit — cookie continuity,
reproducibility, and coherent per-proxy identity — outweighs the
retrospective log-analysis risk in our threat model. If HKLII ever
introduced a JA4-fingerprint-plus-time correlation heuristic (Tier 3+),
we would revisit and introduce per-session profile re-rolls; the code
change is small.

**Date.** 2026-07-03 in the initial `ProxyPool` implementation
(commit `7dd1373`); confirmed 2026-07-04 during the audit.

**Cross-ref.** [07 Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md)
owns the per-proxy client model.

### Why HTTP/2 in direct mode

**Context.** `hklii download` (the one-off URL-fetch subcommand — see
[11](./11-operations-runbook.md)) bypasses `curl_cffi` and uses stock
`httpx.AsyncClient` (`src/hklii_downloader/client.py:28-36`) so that
`--proxy` and `--direct` behave uniformly against non-HKLII origins.
Before audit item M-6, `make_async_client` defaulted to HTTP/1.1 because
`httpx.AsyncClient()` does not set `http2=True` unless explicitly asked.
Combined with `_BROWSER_HEADERS` at `client.py:13-25`, which hardcodes
`Chrome/148.0.0.0`, the on-wire behaviour was Chrome-148 over HTTP/1.1.
No real Chrome speaks HTTP/1.1 to an HTTP/2-capable origin in 2026.

**Decision.** Pass `http2=True` at `client.py:35`. Dependency
`httpx[socks,http2]>=0.28.1` is already pinned in `pyproject.toml:11`.

**Alternatives considered.**

- Drop direct mode entirely. Considered but rejected. Direct mode is the
  only path for canary tests against non-HKLII origins (e.g. `curl -v
  https://tls.browserleaks.com/json` through the same client) and for
  one-off targeted downloads. Removing it would force operators to boot
  the whole proxy pool for a single URL fetch — a poor ergonomic.
- Route direct mode through `curl_cffi` too. Would work but doubles the
  code path for a subcommand that already has smaller reliability
  guarantees than `hklii scrape`. Not worth the refactor.

**Data.** Audit `suspicionCritique.json` signal 7 documents "HTTP/1.1 on
wire with UA claiming Chrome 148" as a per-frame suspicion signal.
Direct mode is still not `curl_cffi`-fingerprinted — the HTTP/2 preface
`httpx` emits comes from the `h2` library, so SETTINGS values and
pseudo-header order are `h2`'s defaults, not Chrome's — but at least the
HTTP-version-vs-UA mismatch is fixed.

**Date.** 2026-07-04 in commit `df42dce` (`feat: enable HTTP/2 in
direct-mode client (item M-6)`).

**Cross-ref.** [06 TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) § "HTTP/2 in direct mode"
owns the mechanics; [11 Operations runbook](./11-operations-runbook.md)
covers when direct mode is actually invoked.

---

## Reliability decisions

### Why `fcntl` exclusive lock on `{path}.lock`

**Context.** Two concurrent `hklii scrape` processes against the same
`--output` directory would both open the same `CheckpointDB` and race:
duplicate `INSERT` attempts, `claim_pending` returning the same row to
both, orphaned `in_progress` rows. SQLite's WAL journal mode does not
prevent this at the row level for the workload we run (long-lived
connections; concurrent writers; no serialization from a coordinator).

**Decision.** On `CheckpointDB.__init__`, acquire an exclusive
non-blocking `fcntl.flock` on a sibling `{path}.lock` file
(`src/hklii_downloader/checkpoint.py:81-102`). A `BlockingIOError`
translates to `CheckpointLockError`, which aborts the run before any
SQL is executed. The lock is released on `close()` at
`checkpoint.py:407-415`.

**Alternatives considered.**

- SQLite's own `BEGIN EXCLUSIVE` on open. Rejected — that guards a
  single transaction, not the process lifetime, so the second process
  would start and race as soon as the first commit lands.
- Lockfile with `O_EXCL`-create-and-write-PID. Rejected — race window
  between `stat` and `unlink` if a prior process crashed without cleaning
  up, and requires stale-PID detection code we do not want to maintain.
- `fasteners` package's cross-platform lock. Rejected as an unnecessary
  dependency; POSIX `fcntl.flock` is stdlib and adequate.

**Data.** No empirical race event on record — this is preventative. The
guard exists because the checkpoint DB is the single source of truth for
work-in-progress and a race there would leak permanent state corruption.

**Notable fallback.** When `os.open(lock_path, ...)` itself raises
`OSError` (e.g. filesystem that does not support the `.lock` create),
the guard degrades to a **warning log line** at `checkpoint.py:85-93`
and continues without cross-process protection. This is an explicit
S-4 trade-off (audit `synth.json`): we chose portability over hard-fail
because a hard-fail on any filesystem that cannot create the lock file
would break the scraper for legitimate users on constrained mounts (SMB,
some FUSE stacks). Anyone reading the warning is being told they need to
serialize runs manually.

**Date.** Lock introduced 2026-07-04 in commit `04b0ad6` (`feat: fcntl
exclusive lock on checkpoint DB (imp 7)`); warning fallback added
2026-07-04 in commit `b12d4bf` (item S-4).

**Cross-ref.** [09 § CheckpointDB](./09-scraper-architecture.md) owns
the schema and lock code.

### Why WAL journal_mode + `busy_timeout=5000`

**Context.** `download_all` runs N workers concurrently
(`scraper.py:200-203`). Each worker calls `claim_pending` (`UPDATE`),
runs the download, then calls `mark_downloaded` or `mark_failed`
(another `UPDATE`). SQLite's default `rollback` journal mode locks
the whole database on any write, so concurrent reads (from other
workers' `claim_pending`) block. Under 6+ workers, that stalls
progress every commit.

**Decision.** Two PRAGMAs applied at connection open in
`CheckpointDB.__init__` (`src/hklii_downloader/checkpoint.py:66-67`):

```python
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA busy_timeout=5000")
```

WAL journal mode allows concurrent reads while a writer is committing;
`busy_timeout=5000` (5 seconds) tells SQLite to retry on `SQLITE_BUSY`
for up to 5 s before returning the error, absorbing the tiny commit
windows that WAL still exclusive-locks.

**Alternatives considered.**

- Default rollback journal + a Python-level per-connection lock. Rejected —
  serializes all workers behind a single lock, defeating the parallelism
  we care about.
- `PRAGMA synchronous=FULL` on top of WAL. Deferred as a follow-up
  (audit `completeness.json` gap 8). `synchronous=FULL` guarantees each
  commit is durable across power loss; the current default `NORMAL` can
  lose the last committed transaction on SIGKILL during a WAL
  checkpoint. HKLII network latency dominates the workload so the
  performance penalty would be negligible, but this has not been
  verified under load and no commit has landed yet.

**Data.** `PRAGMA integrity_check` also runs at connection open
(`checkpoint.py:73-79`, added commit `07ff7b6` for audit item `8a`). If
it returns anything other than `"ok"` the DB is refused with
`CheckpointCorruptError` — cheap insurance against loading a corrupted
checkpoint after a crash.

**Date.** WAL + busy_timeout in the initial CheckpointDB commit
`73a8935` (2026-07-03); integrity_check on open 2026-07-04 (`07ff7b6`).

**Cross-ref.** [09 § CheckpointDB PRAGMAs](./09-scraper-architecture.md)
owns the mechanics.

### Why atomic writes with fsync-parent

**Context.** Two crash-consistency windows existed in the original save
paths (`client.save_judgment_local`, enrichment sidecars, `.enum_cache`
JSON):

1. `Path.write_text` returns before the file's bytes reach disk. The
   checkpoint row can commit `status='downloaded'` before the on-disk
   file exists.
2. Even after `os.fsync` on the file descriptor, POSIX crash
   consistency requires an `fsync` on the *parent directory* to make
   the rename durable. `os.replace` alone can be lost on unclean reboot
   even if the file's own bytes are safely on disk.

**Decision.** All on-disk saves route through
`atomic_write.atomic_write_text` / `atomic_write_bytes`
(`src/hklii_downloader/atomic_write.py`). The four-step model is:

```
1. Write bytes to {path}.part
2. os.fsync the .part fd
3. os.replace(.part, path)
4. Open the parent directory, os.fsync it, close
```

All four steps live in `_fsync_and_replace` at
`atomic_write.py:13-25`. On any `BaseException` between steps 1 and 3
the `.part` file is removed via `unlink` (swallowing `FileNotFoundError`)
so no half-written files litter the output directory.

**Alternatives considered.**

- `Path.write_text` and hope. Rejected — WAL commits land in SQLite
  before `write_text`'s bytes reach disk. Post-crash `hklii verify` would
  find rows marked `downloaded` whose files do not exist.
- `Path.write_text` + `os.fsync` on the file only, no parent-dir fsync.
  This is what shipped before audit item S-3. Still leaves the second
  crash-consistency window open — `os.replace` publishes a directory
  entry that is not durable until the parent dir metadata syncs.
- Use `NamedTemporaryFile` and `os.rename`. Same shape but leaks
  tmpfile suffix onto the filesystem instead of matching `.part`
  convention, and the cleanup path on exception is less obvious.

**Data.** POSIX rationale in the module docstring; concrete four-step
model from audit synth item S-3. `.enum_cache` writes also use this
path since audit item S-5 (`enumerator.py:134-136`).

**Date.** Atomic writes introduced 2026-07-04 in commit `fc8ffd5` (imp
6); parent-dir fsync added same day in commit `e69207c` (item S-3);
`.enum_cache` switch to atomic writes in commit `8282ff5` (item S-5).

**Cross-ref.** [09 § Atomic writes](./09-scraper-architecture.md) owns
the mechanics.

### Why bilingual UPSERT prefers EN

**Context.** HKLII serves each judgment in both English and Traditional
Chinese when a translation exists (`has_translation: true` in the API
response). `hklii scrape --lang both` (the default) enumerates both
languages and produces two rows per case worth of enumeration data.
The `cases` table's primary key is `(court, year, number)` — one row
per case, not per language.

**Decision.** In `CheckpointDB.upsert_case`
(`src/hklii_downloader/checkpoint.py:128-145`), the `ON CONFLICT` clause
uses this rule for `lang`:

```sql
lang=CASE
  WHEN cases.lang='en' OR excluded.lang='en' THEN 'en'
  ELSE excluded.lang
END
```

If either the existing row or the new insert has `lang='en'`, the row
stays / becomes `'en'`. Otherwise the incoming `lang` wins. Effect: when
both languages are enumerated for the same case, the EN version is what
we actually download.

**Alternatives considered.**

- Composite primary key `(court, year, number, lang)`. Rejected because
  it changes the row semantics — every downstream query (`claim_pending`,
  `find_orphans`, `verify_downloaded_against_files`) would need
  bilingual awareness, and the RAG pipeline consumers would need to
  merge back to one canonical judgment per matter.
- Prefer TC over EN. Rejected because the target use case (SFC / business
  / employment / accounting research — the project goals live in the top-level [`README.md`](../README.md) and `memory/project-goals.md`)
  is anglophone. EN is the practical default.
- Download both and store TC in a sibling file (`{stem}_tc.html`).
  Deferred. Would double corpus size for cases where both languages
  exist (roughly 20-30% of the corpus per `has_translation`), and adds
  no immediate downstream benefit — the RAG index will use EN.

**Consequence.** TC-only judgments (no EN translation) are downloaded in
TC. Cases with both languages are downloaded EN-only. The `has_translation`
field in the JSON metadata records which case was which.

**Date.** 2026-07-04 in commit `c24d4fb` (`feat: add lang column to
CheckpointDB (blocker 1 — schema)`).

**Cross-ref.** [09 § UPSERT rules](./09-scraper-architecture.md) owns
the SQL.

---

## Infrastructure decisions

### Why gluetun + PIA

**Context.** The scraper needs an IP-diverse egress path to (a) spread
volume below the audit signal-3 threshold (~1700 req/hr from any single
IP), (b) survive the loss of any one exit without dropping the run.

**Decision.** 20 gluetun Docker containers, each running Private
Internet Access OpenVPN, exposing an HTTP proxy on `localhost:8888..8907`.
Generator script is `scripts/expand_vpn_pool.py` (commit `866001d`).

**Alternatives considered.**

- **WireGuard-based provider (Mullvad, IVPN).** Rejected: PIA's
  WireGuard control plane requires configs generated externally through
  `pia-foss/manual-connections`, whereas PIA + OpenVPN authenticates with
  a plain `OPENVPN_USER`/`OPENVPN_PASSWORD` pair that gluetun handles
  natively. WireGuard would be faster (kernel-space) but requires either
  a config-generation preproc step or a different provider.
- **Residential proxy service (Bright Data, Oxylabs).** Rejected on
  budget and ethics. Residential proxies inherit consent problems (users
  do not know their IPs are being sublet); gluetun+PIA uses IPs the
  scraper owns via subscription.
- **Datacenter proxy service.** Rejected — datacenter IP ranges are
  Tier-1 heuristic (any IP-quality scorer flags them). PIA VPN exit IPs
  are also datacenter, but distribution across 6 Asia-Pacific regions
  and pinning per container (`SERVER_NAMES` in `docker-compose.yml`)
  gives us some regional diversity within the datacenter bucket.
- **Manual OpenVPN + iptables policy routing.** Rejected as an ops
  burden. Namespace isolation via Docker eliminates the "tunnel drops
  and traffic leaks onto host route" failure mode without any per-
  container systemd unit or route table gymnastics.

**Data.** From `vpn-infrastructure.md`, 2026-07-04 canary against the
20-pool completed 100 files in 52 s — 2.3× faster than the same canary
against a 6-pool. Regional distribution: SG 7, JP 3, HK 3, TW 2, MY 2,
MO 2, KR 1. All 20 healthy; PIA lifted the 10-device concurrent-
connection cap in 2023-2024 so all 20 containers on one account works.

**Date.** Initial 6-container pool 2026-07-03 (commit `671f23c`);
expansion to 20 via `expand_vpn_pool.py` 2026-07-04 (commit `866001d`).

**Cross-ref.** [08 VPN pool](./08-vpn-pool.md) owns everything about
the pool topology and provisioning.

### Why 4 target courts by default

**Context.** HKLII exposes 14 court databases (see [03 § Court corpus](./03-endpoint-reference.md)).
The full corpus is 122,460 files per the homepage counter; API-level
sum is 118,188. Downloading all fourteen would take substantially
longer than downloading a focused subset.

**Decision.** Default `--courts hkcfi,hkca,hkdc,hkcfa`
(`src/hklii_downloader/cli.py`). Four courts, in that order.

**Alternatives considered.**

- All 14 courts. Would be a superset of the current default. Rejected
  because seven of the fourteen are in the low-tens-of-cases range and
  are subject-matter irrelevant to the SFC / business / employment /
  accounting focus (Coroner's Court, Obscene Articles Tribunal, etc.).
- Top 3 courts only (hkcfi, hkca, hkcfa). Rejected because it drops the
  District Court, which handles substantial civil disputes below the
  CFI floor and is the natural target for "small business dispute" RAG
  queries.
- User-configurable at CLI, no default. Rejected as poor ergonomics —
  a default that covers 97% of the useful corpus is a better fallback
  than requiring every operator to know the slugs.

**Data.** 2026-07-04 API probe returned the following per-court totals:

| Slug | Total judgments | Cumulative | Cumulative % of 118 188 |
|---|---:|---:|---:|
| hkcfi | 64 226 | 64 226 | 54.3% |
| hkca | 29 911 | 94 137 | 79.6% |
| hkdc | 18 118 | 112 255 | 95.0% |
| hkcfa | 2 143 | 114 398 | 96.8% |

Everything else is 1789 or fewer per court and does not move the needle
for anglophone commercial legal research.

**Date.** Default set in the initial `hklii scrape` implementation
2026-07-03 (commit `1b41711`); confirmed 2026-07-04 after the API probe.

**Cross-ref.** [03 § Court corpus](./03-endpoint-reference.md) owns
per-court totals; [11 Operations runbook](./11-operations-runbook.md)
covers the `--courts` flag.

---

## Deliberate non-decisions

The following sections document things we chose **not** to do and the
reasons behind that choice. They are recorded here so a future
contributor does not spend cycles rediscovering the rationale.

### Why we skip robots.txt / ToS review as an ongoing check

**Context.** Audit `completeness.json` gap 13 flagged that HKLII's
`robots.txt`, terms of use, and any bulk-access policy have never been
reviewed as part of the operational discipline. All the anti-detection
work is about avoiding *technical* blocks; the *policy* layer is
untouched.

**Decision.** Do a one-time review before the first production run
(fetch `https://www.hklii.hk/robots.txt`, skim Terms of Use / About
page, search for a `data@` or research-access contact). Do not automate
recurring re-checks.

**Alternatives considered.**

- Wire a scheduled ToS re-check as a cron job. Rejected as premature
  automation. The operator triggering each production run is already in
  a good position to eyeball the site once before starting.
- Skip the ToS review entirely. Rejected — five minutes of due
  diligence against a five-hour scrape is not a bad trade, and HKLII
  might well offer legitimate bulk access to academic requests, which
  would obsolete the fingerprint work entirely.

**Data.** Not yet done. This is a known open item.

**Date.** Deferred 2026-07-04 during the completeness audit.

**Cross-ref.** [11 Operations runbook](./11-operations-runbook.md)
covers the preflight checklist that operators should append this to.

### Why we skip citation graph and LawCite

**Context.** HKLII's `/api/getcasenoteup` provides subsequent-citation
data for a case, but the *cross-case* citation graph (who cites whom
across the corpus) lives on LawCite (`austlii.edu.au`), which is
Cloudflare-fronted with Managed Challenge on scraping traffic. Getting
LawCite data requires either headless browser automation with
Cloudflare Turnstile solvers, an official research API partnership, or
building the citation graph ourselves from regex extraction of neutral-
citation patterns in judgment HTML text (e.g. `[2024]\s+HKCFI\s+\d+`).

**Decision.** Ship without a citation graph. Extract forward citations
from the judgment HTML at RAG-index build time using a regex like
`\[\d{4}\]\s+[A-Z]{2,10}\s+\d+`; do not scrape LawCite.

**Alternatives considered.**

- Automate LawCite scraping. Rejected. Cloudflare Managed Challenge
  requires JavaScript execution to solve; that requires Playwright or
  equivalent, which contradicts the "no browser automation" architecture.
  Even with Playwright, Cloudflare Turnstile is designed to catch
  headless clients and would need per-run manual solves or a paid
  solver-API dependency.
- Contact AustLII for research API access. Deferred until we know
  whether the corpus-first pipeline actually needs the citation graph
  for its use case.
- Rely on `getcasenoteup` alone. Rejected because it only returns cases
  citing the specific case being queried — an inverted view of the
  graph, and it requires per-case API calls that would multiply the
  corpus run wall-clock by an order of magnitude.

**Data.** LawCite Cloudflare status confirmed in
`hklii-api-structure.md:61`. `getcasenoteup` documented in
`hklii-api-structure.md:35` but not called from any production code
path.

**Date.** Deferred 2026-07-03 during initial scoping; reconfirmed
2026-07-04.

**Cross-ref.** [03 § Endpoint index](./03-endpoint-reference.md) marks
`getcasenoteup` as "Not called".

### Why no canary before every production run yet

**Context.** Audit `completeness.json` gap 1 recommends a `hklii scrape
--limit 100` canary run as a mandatory preflight before any
production-scale invocation, to catch WAF flips, dependency
regressions, or Chrome-profile availability changes before wasting 20+
hours on a broken configuration.

**Decision.** Ran a one-off canary on 2026-07-04 (`--courts hkfc
--limit 100 --lang en`; 100/100 downloaded, 0 failed, 6/6 proxies
healthy, S-1 zero false positives, warm-up fired, empty-content with
doc-fallback captured, appeal_history 100/100). Do not require a canary
before every subsequent run yet; document it as a recommended pre-flight
in [11 Operations runbook](./11-operations-runbook.md).

**Alternatives considered.**

- Hard-require a canary via a CLI gate (`hklii scrape` refuses to
  operate on > 500 rows without a passing canary run recorded in the
  last N hours). Rejected as premature — the audit itself was the
  canary for the initial production configuration; adding a gate before
  we know how often the shape changes would just create ceremony.
- Recommend but do not implement. This is what shipped.

**Data.** Canary results recorded in `session-2026-07-04-resume.md:62`.

**Date.** Recommended but not enforced 2026-07-04.

**Cross-ref.** [11 Operations runbook](./11-operations-runbook.md)
carries the canary recipe.

### Why no gzip retry

**Context.** During the pre-production audit, audit synth item M-5
proposed sending `Accept-Encoding: gzip, deflate, br, zstd` on every
API request and expecting a 5-10× on-wire byte saving. The hypothesis
was that HKLII's `curl` probe had defaulted to no `Accept-Encoding`
header, so compression was untested rather than absent.

**Decision.** Do not retry. `HeaderRotator._build_headers` already sets
`Accept-Encoding: gzip, deflate, br` (`proxy_pool.py:112`). Empirical
follow-up probes show the origin does not compress even when asked.
This closes M-5 as "no action needed"; the hypothesis is retired.

**Alternatives considered.**

- Escalate to `curl_cffi`-specific Accept-Encoding manipulation.
  Rejected — the raw byte counts are identical between with-header and
  without-header probes.
- File a bug with HKLII asking for gzip. Deferred; not on the critical
  path for the corpus grab.

**Data.** Endpoint probe pair `pair_noenc` vs `pair_gzip`:

| Probe | `Accept-Encoding` header | Body bytes |
|---|---|---|
| `pair_noenc` | not sent | 357 |
| `pair_gzip` | `gzip, deflate, br, zstd` | 357 |

Identical byte counts confirm the origin does not compress, even when
asked. Recorded in `scratchpad/probes/pair_gzip.metrics` and
`pair_noenc.metrics`.

**Date.** Hypothesis retired 2026-07-04 after endpoint re-probe.

**Cross-ref.** [03 § Absent headers](./03-endpoint-reference.md) records
the empirical no-compression fact.

### Why HKT time-of-day is a choice, not an automation

**Context.** Audit `completeness.json` gap 15 raised the question of
when to schedule the production run. HKLII peak is roughly 09:00-18:00
HKT (01:00-10:00 UTC). Two extremes:

- **22:00 HKT start** — low platform traffic, so a rate-limit block is
  easier to notice (nothing else is on the origin), fewer legitimate
  users share the fingerprint bucket, and the whole run finishes before
  anyone in the HKLII team is at their desk.
- **09:00 HKT start** — blend into the peak so per-hour request
  volumetric percentile detection is diluted by legitimate traffic.

**Decision.** Neither. Time-of-day scheduling is an operator decision
per run, not an automatic knob. The runbook records both options; the
operator picks based on which risk they care more about that night.

**Alternatives considered.**

- Hard-code a `sleep-until 22:00 HKT` in the CLI. Rejected as
  paternalistic — the operator may have a real reason to run mid-day
  (e.g. testing after code change, immediate follow-up run after a
  fix).
- Schedule via cron with a fixed hour. Rejected for the same reason.

**Data.** No empirical rate-limit event has been observed at either
window. This is a risk-model preference, not an optimization for
throughput.

**Date.** Documented as an operator choice 2026-07-04.

**Cross-ref.** [11 Operations runbook § HKT scheduling](./11-operations-runbook.md)
carries the two options for the operator to pick from.

---

## Cross-cutting notes

Two facts touch nearly every entry above and deserve to be called out
once here so subsequent entries do not have to reground them:

- **HKLII is not currently WAF-fronted.** Every entry that talks about
  a "future WAF flip" or "insurance" defence is real work being done
  against a hypothetical adversary. The empirical baseline
  (2026-07-04 origin probes; [01](./01-hklii-platform.md)) shows plain
  gunicorn/Apache, HTTP/2 via TLS ALPN, no `cf-*`, no `x-served-by`,
  no `Retry-After`, no `X-RateLimit-*`. This means today's defences are
  precautionary. It also means the cost of removing any of them is a
  step change if HKLII flips (adding fingerprinting after a live block
  is much harder than pre-hardening), so we ship them.
- **Everything except the initial architecture is a 2026-07-04
  decision.** The initial commit was 2026-07-03; the pre-production
  audit and every subsequent item shipped 2026-07-04. That compressed
  timeline is why nearly every "Date" field above resolves to a single
  day. It is also why we do not yet have "we changed our mind six months
  later" entries; when we do, the pattern will be to keep the original
  entry as-is and append an "Update YYYY-MM-DD" block underneath rather
  than rewrite history.

The full commit-to-decision map is available via `git log --grep='item
[MS]-[0-9]'` for the audit items and via `git log --oneline
src/hklii_downloader/{module}.py` for the specific module a decision
touches.
