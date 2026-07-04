# Anti-Detection Strategy

This chapter states the anti-detection posture as a whole. It defines what
we are defending against, why the current defenses look like overkill given
the platform we probed, and which specific access-log or fingerprint signal
each layer of the stack is designed to defeat. Concrete implementation of
every layer lives in a sibling chapter — this chapter links out but does
not restate mechanics.

Sibling implementation chapters:

- [HTTP header composition](./05-http-headers.md) — Accept / UA / sec-ch-ua / Sec-Fetch quartet, HeaderRotator internals, Referer derivation
- [TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) — curl_cffi profile pool, JA3/JA4, HTTP/2 SETTINGS + pseudo-header order
- [Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md) — per-proxy jars, session warm-up mechanics, circuit breaker
- [VPN pool](./08-vpn-pool.md) — gluetun + PIA composition, IP diversity
- [Content safeguards](./10-content-safeguards.md) — S-1 challenge-marker rejection, content-shape validation

---

## Threat model

We assume three distinct adversarial postures and design against all
three. All three are hypothetical — HKLII currently exhibits no observable
countermeasure — but the cost of pre-hardening is small and the cost of
being retroactively fingerprinted is a permanent block against a
low-volume public site with two sysadmins.

### Threat 1: rate-based reactive blocks (current)

The most likely block path today is a simple `nginx limit_req_zone` or an
in-process Django throttle looking at requests per source IP per unit
time. Endpoint probe evidence (see [Endpoint reference](./03-endpoint-reference.md))
shows HKLII returns no `Retry-After`, no `X-RateLimit-*`, and no
RFC-9331 `RateLimit-*` — so a block, if one is added, will present as
either an HTTP 429/503 with no advisory headers or a raw TCP reset. Both
are detectable but neither is currently observed.

The defense is per-proxy throttling with jitter and burst structure
([Throttler](#the-request-throttler-signal-9) — see also
`src/hklii_downloader/proxy_pool.py:32-60`) plus IP diversity across the
VPN pool ([08](./08-vpn-pool.md)).

### Threat 2: WAF flip (future)

HKLII sits directly on gunicorn today (see [01](./01-hklii-platform.md)).
A future decision to front the site with Cloudflare, Akamai, AWS WAF, or
similar would introduce Tier-3 detection — JA4 TLS fingerprints, HTTP/2
SETTINGS-frame matching, JS challenges — overnight. Because those checks
run at connection setup they are unrecoverable in-flight; any Python
default client would start returning `403` or a JS-challenge HTML page
uniformly, and the scraper would burn through the entire proxy pool
before a human noticed.

The defense is curl_cffi TLS + HTTP/2 impersonation
([06](./06-tls-http2-fingerprinting.md)). Once wired, the incremental
cost of running curl_cffi on a non-WAF origin is ~zero, so we run it
today as insurance.

### Threat 3: retrospective log analysis (any time)

Even without any live blocking, an operator reviewing access logs weeks
after a run can flag the source IP range and block the corresponding
ASN. A single well-known SQL query on the log store is often enough
(`SELECT src_ip WHERE COUNT(DISTINCT referer)=1 AND count>500`). Because
these queries key on aggregate patterns, not per-request features, TLS
mimicry does nothing for them; only header/Referer/cadence variety
matters.

The defense is scattered across [05](./05-http-headers.md),
[06](./06-tls-http2-fingerprinting.md), [07](./07-cookies-sessions-warmup.md).
This chapter's [Log-analysis one-liners we deliberately kill](#log-analysis-one-liners-we-deliberately-kill)
section catalogues the specific queries.

### Threat scope: local artifacts vs. the wire

The three threats above are all "the origin (HKLII, Judiciary) or
someone with access to its logs learns something they can act on."
That is the axis this chapter's defenses target. The corollary matters
as much as the axis itself: what happens *only* on the operator's own
machine is out of scope.

**In-scope (defenses required).**

- Anything on the wire to `www.hklii.hk` or `legalref.judiciary.hk`.
  If a packet leaves the machine bound for either origin and reveals
  the operator's true source IP, the proxy identity is burned. This is
  why the proxy architecture is load-bearing (all requests via `httpx`
  through gluetun, DNS via Unbound over the tunnel — see
  [08 VPN pool](./08-vpn-pool.md) and
  [07 Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md)),
  and why `IPLeakError` in `proxy_pool.py` kills a session the moment
  runtime IP check sees the real IP echoed back.
- Anything in third-party services the operator hands artifacts to
  (bug trackers, gists, screenshots posted publicly). This is
  *behavioral* — the operator redacts before publishing — not a
  code-side defense concern.

**Out of scope (no code-side defense).**

- CLI `stdout` (including the `Home IP: <ip>` preflight line).
- `<output>/scrape.log` file contents.
- `<output>/events.jsonl` file contents.
- `<output>/.checkpoint.db` `cases.error` column contents.
- `hklii monitor --json` output.

Every one of those sits on the operator's filesystem and cannot burn
the proxy identity. Redacting the scraper's own home IP from its own
logs solves a threat that does not exist under this model, at the cost
of removing diagnostic information the operator legitimately uses
(`grep 'Home IP' scrape.log` confirms the direct preflight probe ran
and what it saw).

**Some redactions we shipped anyway.** Commits `b61b673` (B7 — drop
`events.jsonl` `ip_echo.extra.observed_ip` when `via="direct"`) and
`af9abfa` (B8 — split `scrape.log` INFO to `via direct -> OK` sentinel)
are documented no-op hygiene, not threat mitigation. Future reviews
should not treat local-artifact IP exposure as a blocker; see
[12 Decisions log](./12-decisions-log.md) § "Why we don't redact home
IP from local artifacts" for the full rationale, and for why the
V3-flagged B10 (CLI stdout) + B11 (`IPLeakError` message) fixes were
abandoned rather than merged.

---

## Empirical baseline

HKLII is a bare gunicorn origin. This is not an assumption; it is what
`curl -v` returned on 2026-07-04 from four different PIA exit IPs:

```
HTTP/2 200
server: gunicorn
allow: GET, HEAD, OPTIONS
vary: Cookie,origin
x-frame-options: SAMEORIGIN
x-content-type-options: nosniff
x-frame-options: ALLOWALL           # duplicate — bare-Python fingerprint
x-content-type-options: nosniff     # duplicate — bare-Python fingerprint
content-type: application/json
content-length: 234043
referrer-policy: same-origin
cross-origin-opener-policy: same-origin
```

Full header dump: `scratchpad/hdr_s1000.txt`. The duplicate
`x-frame-options` (values `SAMEORIGIN` and `ALLOWALL`) is diagnostic —
two Django middleware layers both stamp the header and no downstream CDN
edge normalizes them (a Cloudflare or Akamai edge would collapse
duplicates). Details in [01](./01-hklii-platform.md).

What this means for defense design:

- **No CDN / no WAF today.** No `cf-ray`, `cf-cache-status`, `x-served-by`, `via`, `age`, `x-cache*`, or Akamai `x-akamai-*` headers on any of the 4 pageSize probes or 14 court probes.
- **No advertised rate limits.** No `Retry-After`, no `X-RateLimit-*`, no RFC-9331 `RateLimit-*`. If throttling exists it is silent and behavioural.
- **No compression negotiated.** Requesting `Accept-Encoding: gzip, deflate, br, zstd` returned the same byte count as no header at all (357 B both, files `scratchpad/probes/pair_gzip.metrics` and `pair_noenc.metrics`).
- **No conditional-GET support.** No `ETag`, no `Last-Modified`, no `Cache-Control`. Every request is a full-fetch.
- **HTTP/2 via ALPN, TLS 1.3.** ALPN negotiates `h2` and the cipher is `TLS_CHACHA20_POLY1305_SHA256`.

So the current TLS mimicry, HTTP/2 SETTINGS matching, and Client Hints
work are all precautionary. Nothing observably breaks without them on
2026-07-04. But we ship them anyway because: (a) they cost near-zero at
runtime, (b) the log-analysis threat pattern is retrospective and does
key on some of the same signals (UA + Referer + cadence), and (c) the
WAF flip could happen at any point and we would rather not discover it
mid-24-hour run.

---

## Detection tier catalog

Every real detection stack in 2026 layers three tiers of checks. Tier 1
is unavoidable — it runs by default on every nginx and Apache install.
Tier 2 requires a couple hours of custom log analysis. Tier 3 requires a
WAF or the operator writing per-connection C code. HKLII exhibits Tier 1
at most; the defenses below cover all three so the same code works if
the site upgrades.

### Tier 1 — sysadmin heuristics (universal)

Runs on any server without special software. All are one-line default
configurations or three-line log grep patterns.

| Check | Trigger | Our defense |
|---|---|---|
| UA regex block | UA matches `curl\|wget\|python-requests\|urllib\|scrapy\|bot` | HeaderRotator emits Chrome 126-148 UA. `src/hklii_downloader/proxy_pool.py:63-87,101-122` |
| Per-IP rate limit | > 5 req/s from single IP for > 60 s | Throttler + proxy pool. See [Signal 3](#signal-3-1700-reqhr-volumetric). |
| Missing Accept | Empty or absent `Accept:` header | HeaderRotator sets `text/html,application/xhtml+xml,application/xml;q=0.9,…` — full Chrome value. `proxy_pool.py:110` |
| Missing Accept-Language | Empty or absent header | HeaderRotator sets `en-US,en-GB;q=0.9,en;q=0.8`. `proxy_pool.py:111` |
| Missing Accept-Encoding | Empty or absent header | HeaderRotator sets `gzip, deflate, br`. `proxy_pool.py:112` |

### Tier 2 — common heuristics (few hours of custom work)

Runs when an operator gets suspicious enough to grep logs by session.

| Check | Trigger | Our defense |
|---|---|---|
| Empty Referer to deep pages | `/api/*` GET with no or homepage-only Referer | `parser.referer_for(url)` derives plausible SPA Referer per URL. `src/hklii_downloader/parser.py:40-73` |
| Asset-request drought | 100% API-endpoint hit ratio, no CSS/JS/font/img | Partially defended: warm-up GET on `https://www.hklii.hk/` per proxy sits in the log next to the API hits. See [Signal 1](#signal-1-100-api-hit-ratio). |
| Perfect cadence | Inter-arrival stddev/mean < 0.3 | RequestThrottler emits bursts of 2-5, then 2-4 s gaps, plus 5% chance of 3-8 s pause. `proxy_pool.py:32-60` |
| No cookies on repeat visits | Session never echoes any Set-Cookie value | Per-proxy `curl_cffi.AsyncSession` persists cookies for the process lifetime. `proxy_pool.py:227,239,259-262`. See [07](./07-cookies-sessions-warmup.md). |

### Tier 3 — uncommon heuristics (need a WAF or bespoke code)

Runs only in front of Cloudflare/Akamai/AWS WAF/DataDome or when the
operator has written a custom nginx module.

| Check | Trigger | Our defense |
|---|---|---|
| TLS JA3 / JA4 fingerprint | Fingerprint hash matches known Python `requests`, `httpx`, `aiohttp`, or `Go net/http` clients | curl_cffi profile pool: `chrome`, `chrome146`, `chrome142`, `chrome136`, `chrome131`. `src/hklii_downloader/impersonate_client.py:21-23` |
| HTTP/2 SETTINGS-frame fingerprint | SETTINGS values or WINDOW_UPDATE size does not match a real Chrome | curl_cffi impersonation covers HTTP/2 pseudo-header order (`m,a,s,p` for Chrome) and SETTINGS values (`1:65536;2:0;4:6291456;6:262144`) |
| Client Hints missing on XHR | `Sec-CH-UA` / `Sec-CH-UA-Mobile` / `Sec-CH-UA-Platform` absent | HeaderRotator emits all three low-entropy hints unconditionally. `proxy_pool.py:114-116` |
| JS challenge / body-shape probe | Response contains challenge markers | S-1 content-shape validation with 7 English + 6 Traditional Chinese markers. `src/hklii_downloader/scraper.py:30-70,286-291`. See [10](./10-content-safeguards.md). |

curl_cffi handles all three fingerprint-layer Tier-3 checks by design;
we do not write TLS or HTTP/2 code ourselves. Detail on which values
curl_cffi controls end-to-end vs which we pass through is in
[06](./06-tls-http2-fingerprinting.md).

---

## Layer stack overview

Every outbound request from `ProxyPool.get()` passes through six layers.
Each layer is owned by a different sibling chapter for implementation
detail but all six sit inside `proxy_pool.py` at runtime.

```
+--------------------------------------------------------------------+
|  Layer 6:  Request cadence      throttler.next_delay()             |
|            (signal 3, 9)        proxy_pool.py:337-338              |
+--------------------------------------------------------------------+
|  Layer 5:  Cookies              curl_cffi.AsyncSession per proxy   |
|            (signal 5)           proxy_pool.py:227,239,259-262      |
+--------------------------------------------------------------------+
|  Layer 4:  HTTP headers         HeaderRotator.generate(url)        |
|            (signals 2,6,12)     + parser.referer_for(url)          |
|                                 proxy_pool.py:344-345              |
+--------------------------------------------------------------------+
|  Layer 3:  Client Hints         HeaderRotator emits sec-ch-ua*     |
|            (signal 12)          proxy_pool.py:114-116              |
+--------------------------------------------------------------------+
|  Layer 2:  TLS + HTTP/2         curl_cffi impersonate profile      |
|            (signals 7,8,10)     impersonate_client.py:21-23        |
+--------------------------------------------------------------------+
|  Layer 1:  Exit IP              gluetun + PIA proxy pool           |
|            (signals 3,4,11)     proxy_pool.py:260-262 (per-proxy   |
|                                  client), see [08]                 |
+--------------------------------------------------------------------+
```

Layer order matters:

1. **IP first** — nothing else matters if the exit IP is fingerprinted. Handled entirely outside Python by the [VPN pool](./08-vpn-pool.md).
2. **TLS/HTTP2 second** — negotiated at connection time, before any application data. curl_cffi owns this end-to-end and strips headers we might send that would collide with the impersonated profile (`impersonate_client.py:28-42`).
3. **Client Hints third** — set once per session by HeaderRotator, unchanged per request.
4. **Headers fourth** — vary per request (Referer follows URL; Sec-Fetch quartet switches for `/api/*`).
5. **Cookies fifth** — accumulate per-proxy session. Warm-up seeds them.
6. **Cadence sixth** — the sleep before each request is the last thing we control before the socket write.

---

## 12 suspicion signals catalog

This is the source-of-truth list. Sibling chapters reference these
signals by number without re-defining them. Each entry gives the raw
log-analysis pattern, what a naive Python client would produce, and which
layer defeats it.

### Signal 1: 100% API hit ratio

**Log rule.** `count(uri LIKE '/api/%') / count(*) > 0.95 AND count(*) > 100`
grouped by `src_ip` per hour.

**Naive Python behaviour.** A scraper that iterates `getcasefiles` then
`getjudgment` has ratio exactly 1.0. Real Chrome loading the same
judgment triggers 20-50 subresource requests for CSS, JS, fonts, images,
and favicon — API calls are 5-15% of session volume.

**Our defense.** Partial. The per-proxy warm-up
(`proxy_pool.py:292-304` — see [07](./07-cookies-sessions-warmup.md))
fires one GET on `https://www.hklii.hk/` after IP preflight, which
credits the exit IP with a landing-page hit before the API stream
begins. It does not credit any CSS/JS/font requests; those would require
parsing the returned HTML and following subresource links, which we
choose not to do (see [Deliberate non-defenses](#deliberate-non-defenses-and-residual-signals)).

### Signal 2: Hardcoded Referer

**Log rule.** `SELECT src_ip WHERE COUNT(DISTINCT referer)=1 AND MAX(referer)='https://www.hklii.hk/' AND COUNT(*)>30`.

**Naive Python behaviour.** Every `/api/*` XHR sends `Referer:
https://www.hklii.hk/`. Real Chrome sets Referer to the URL of the
document that fired the XHR — `/en/cases/hkcfi/2024/` for a
`getcasefiles?caseDb=hkcfi&lang=en` call, `/en/cases/hkcfi/2024/1234` for
a `getjudgment?abbr=hkcfi&year=2024&num=1234` call. Real users produce
> 3 distinct Referers per session.

**Our defense.** `parser.referer_for(url)`
(`src/hklii_downloader/parser.py:40-73`) derives a URL-appropriate
Referer. `/api/getjudgment` gets `/{lang}/cases/{court}/{year}/`;
`/api/getcasefiles` gets `/{lang}/cases/{court}/`. See
[05](./05-http-headers.md) § "Referer derivation".

### Signal 3: 1700 req/hr volumetric

**Log rule.** `count(uri LIKE '/api/%') per src_ip per hour > 200 = weak,
> 1000 = strong, > 1500 with no > 30-minute gap over 6 hours = confirmed
automation`.

**Naive Python behaviour.** A single client hitting the RequestThrottler
average of 2.08 s/request produces ~1730 req/hr from one exit IP,
sustained for the entire download window.

**Our defense.** IP diversity — the VPN pool spreads volume across 20
distinct exit IPs (see [08](./08-vpn-pool.md)). Each proxy still runs at
its own throttler cadence (~1730 req/hr per proxy, `proxy_pool.py:32-60`,
one throttler per session at `proxy_pool.py:234`), so the aggregate
pool volume against HKLII is ~34,000 req/hr — but *no single source IP*
exceeds ~1730 req/hr, which is what per-IP log rules key on. Throttler
burst structure additionally prevents perfect uniformity within a proxy
— specific numeric parameters live in [09](./09-scraper-architecture.md)
§ "RequestThrottler formula", which is the authoritative source. The rule
catches per-IP volume, not aggregate origin
volume, and HKLII's plain-gunicorn stack has no header-advertised
throttle on total-request rate ([03](./03-endpoint-reference.md)).

### Signal 4: First-request-is-/api/*

**Log rule.** `SELECT src_ip, MIN(uri) FROM access_log GROUP BY src_ip,
TRUNC(ts, 30min) HAVING FIRST_URI LIKE '/api/%' AND count>5`.

**Naive Python behaviour.** From a fresh proxy exit IP, the first packet
HKLII sees is a hot XHR to `/api/getcasefiles`. A real Chrome session's
first request is always `GET /`, then CSS/JS/favicon, then XHR ~200-2000
ms later once the SPA JS has executed.

**Our defense.** M-4 preflight warm-up. After the IP echo confirms the
proxy exit is not leaking, `_warm_up_target` fires
`GET https://www.hklii.hk/` through that proxy's client
(`proxy_pool.py:193-197,292-304`). The API stream that follows is now the
second request from the exit IP, not the first.

### Signal 5: No session cookie echoed

**Log rule.** Join access log against known `Set-Cookie` values. Flag
IPs that (a) send cookies the server never gave them, or (b) never send
any cookie for > 50 requests.

**Naive Python behaviour.** A per-request `httpx.get()` with no shared
jar sends no cookies. Even with a jar, restart-time state loss means
every process start looks like a fresh browser.

**Our defense.** Each proxy owns its own `ImpersonateAsyncClient`
wrapping a `curl_cffi.AsyncSession` (`proxy_pool.py:227,239,259-262`).
`AsyncSession` persists cookies for the client's lifetime, so any
`Set-Cookie` from the warm-up GET or the first `getcasefiles` call flows
into all subsequent requests through that proxy. See [07](./07-cookies-sessions-warmup.md).

**Known residual signal.** Restarting the scraper process resets all
jars. A patient log analyst comparing across days would see fresh
cookie-less sessions on every startup. Full state persistence across
runs is out of scope — see [Deliberate non-defenses](#deliberate-non-defenses-and-residual-signals).

### Signal 6: Sec-Fetch-Mode=navigate on /api/*

**Log rule.** `sec-fetch-mode='navigate' AND uri MATCHES '^/api/' -> flag`.
Zero legitimate flow produces top-level navigation to a JSON endpoint.

**Naive Python behaviour.** A scraper that emits the same header set for
every URL sends `sec-fetch-mode: navigate`, `sec-fetch-dest: document`,
`sec-fetch-user: ?1`, `Upgrade-Insecure-Requests: 1` on all requests
including `/api/*`. Real Chrome sends `sec-fetch-mode: cors`,
`sec-fetch-dest: empty` (with no `Sec-Fetch-User` and no
`Upgrade-Insecure-Requests`) on same-origin fetch/XHR to a JSON API.

**Our defense.** `HeaderRotator.generate(url)` inspects the URL and
rewrites the Sec-Fetch quartet to XHR shape when `/api/` is in the path
(`proxy_pool.py:124-133`):

```python
if url is not None and "/api/" in url:
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-dest"] = "empty"
    headers.pop("sec-fetch-user", None)
    headers.pop("Upgrade-Insecure-Requests", None)
```

See [05](./05-http-headers.md) § "Sec-Fetch quartet — XHR shape".

### Signal 7: HTTP/1.1 with Chrome 148 UA

**Log rule.** `http_version = 'HTTP/1.1' AND user_agent MATCHES 'Chrome/1[3-4][0-9]'`.
Chrome 108+ opportunistically negotiates HTTP/3 (Alt-Svc); Chrome 148 on
HTTP/1.1 to an HTTP/2-capable origin never happens in real traffic.

**Naive Python behaviour.** `httpx.AsyncClient()` defaults to HTTP/1.1
unless `http2=True` is passed. A hardcoded Chrome 148 UA on HTTP/1.1 is
diagnostic.

**Our defense.** Two paths:

1. **Proxy mode (production).** curl_cffi negotiates HTTP/2 as part of the impersonation profile. `impersonate_client.py:54-60`.
2. **Direct mode (canary + one-off `hklii download`).** `client.make_async_client` passes `http2=True` explicitly (`src/hklii_downloader/client.py:28-36`).

### Signal 8: TLS fingerprint says old Chrome + UA says new

**Log rule.** JA4 hash resolves to Chrome 104-116 (2022-2023 build)
while `user_agent` claims Chrome 140+. Consistency check on the WAF
side.

**Naive Python behaviour.** A scraper pinning an older curl_cffi profile
(e.g. `chrome104` or `chrome116`) while forwarding a modern UA header
produces this exact mismatch. Less than 0.5% of live Chrome traffic runs
pre-140 builds as of mid-2026.

**Our defense.** The impersonate profile pool is `("chrome",
"chrome146", "chrome142", "chrome136", "chrome131")`
(`src/hklii_downloader/impersonate_client.py:21-23`). Bare `chrome`
tracks curl_cffi's newest supported profile (currently chrome146). Old
profiles (chrome104/110/116/120) were removed during the 2026-07-04
audit.

The UA is a random draw from `_CHROME_VERSIONS` — 23 entries from
126.0.6478.126 through 148.0.7665.93 (`proxy_pool.py:63-87`). There is
no cross-check ensuring the picked profile version and the picked UA
version are within N releases of each other; both are recent enough that
the mismatch stays inside the ~20-release rolling window that WAFs
tolerate. Details in [06](./06-tls-http2-fingerprinting.md).

### Signal 9: Uniform [0.5, 8] s inter-arrival

**Log rule.**

```sql
SELECT src_ip,
       STDDEV(delta_ms) / AVG(delta_ms)         AS cv,
       MAX(delta_ms) / NULLIF(MIN(delta_ms), 0) AS spread,
       SUM(CASE WHEN delta_ms > 30000 THEN 1 END) / COUNT(*)::float
                                                AS long_pause_ratio
FROM (SELECT src_ip, ts - LAG(ts) OVER (PARTITION BY src_ip ORDER BY ts) AS delta_ms
      FROM access_log)
GROUP BY src_ip
HAVING spread < 20 AND long_pause_ratio < 0.02;
```

Both `spread < 20` and `long_pause_ratio < 0.02` true simultaneously is
the scraper; real users always break at least one.

**Naive Python behaviour.** Uniform `asyncio.sleep(random.uniform(0.5, 1.5))`
produces CV ~0.3, spread ~3, `long_pause_ratio` = 0. Even with the
throttler's 3-8 s pauses and 2-4 s inter-burst gaps the spread stays
under 16.

**Our defense.** Partial. RequestThrottler adds three distributions on
top of the base range: 5% chance of a 3-8 s pause, mandatory 2-4 s
inter-burst gap after every 2-5 requests, and a per-proxy PRNG seeded
`random.Random(i)` so different proxies do not synchronize
(`proxy_pool.py:32-60,240`). The `spread` metric still tops out around
16 (8.0 / 0.5); the `long_pause_ratio` stays well below 0.02 unless we
add > 30 s pauses. This is a known residual signal (see
[Deliberate non-defenses](#deliberate-non-defenses-and-residual-signals)).

### Signal 10: Deterministic per-proxy fingerprint stability

**Log rule.** Cross-day join by exit IP → same TLS fingerprint, same UA
draw, same throttle CV every session. Real users get a new Chrome build
every 4-6 weeks and rebuild the profile PRNG state; a residential IP
sees this variation.

**Naive Python behaviour.** Scrapers seed PRNGs deterministically for
test reproducibility (e.g. `random.Random(i)`). Every restart re-draws
identical values.

**Our design choice.** The current code makes this signal
worse-than-random on purpose:

- Throttler seed: `random.Random(i)` where `i` is the proxy index (`proxy_pool.py:240`).
- HeaderRotator seed: `random.Random(i + 1000)` (`proxy_pool.py:241`).
- Impersonate profile seed: `random.Random(hash((proxy_url, "impersonate")))` (`proxy_pool.py:260-262`).

All three are deterministic functions of `(proxy index, proxy URL)`.
Restart the process and every proxy gets the same TLS profile, same UA
draw sequence, and same throttle jitter sequence.

**Trade-off.** Deterministic seeding was chosen for reproducibility
during development and debugging; the same crash produces the same
sequence, which makes bugs isolatable. Explicitly out of scope for now
— see [Deliberate non-defenses](#deliberate-non-defenses-and-residual-signals).

### Signal 11: Enumeration burst signature

**Log rule.** At process start, up to 8 sequential large-payload
requests to `/api/getcasefiles` on 4 courts × 2 langs (default courts
`hkcfi,hkca,hkdc,hkcfa`), each returning ~2.3 MB JSON with
`itemsPerPage=10_000`. Then N-way parallel `/api/getjudgment` fan-out.
No browser event loop produces this shape — a browser session serializes
per-page navigation with human-scale pauses between clicks.

**Naive Python behaviour.** `BulkScraper.enumerate` iterates
`(court, lang)` pairs single-threaded (`src/hklii_downloader/scraper.py:108-153`)
and inside each call `enumerate_court` fetches pages sequentially
(`src/hklii_downloader/enumerator.py:139-159`). The default corpus
enumeration is ~13 API calls in ~90 s, all against the same endpoint,
all bursty-close-together.

**Design choice.** The enumeration burst is not defended. `--enum-max-age
HOURS` skips re-enumeration when a recent cache exists
(`src/hklii_downloader/scraper.py:116-124`), which reduces frequency but
does not smooth the pattern. See [Deliberate non-defenses](#deliberate-non-defenses-and-residual-signals).

### Signal 12: Missing Client Hints / Origin / Priority

**Log rule.**

- `sec-ch-ua-platform='"macOS"' AND user_agent MATCHES 'Windows|Linux'` → mismatch
- `user_agent MATCHES 'Chrome/146' AND sec-ch-ua NOT LIKE '%v="146"%'` → Chrome-keeps-UA-and-CH-in-lockstep violation
- Missing `Sec-CH-UA`, `Sec-CH-UA-Mobile`, `Sec-CH-UA-Platform` on XHR from a UA claiming Chrome ≥ 89

**Naive Python behaviour.** `httpx` or `requests` sends the UA header
you passed and nothing else. Chrome sends three low-entropy UA-CH
headers on every request (XHR, fetch, cross-origin, preflight) without
server opt-in.

**Our defense.** HeaderRotator emits all three low-entropy hints in
lockstep with the UA — same `_build_headers` call picks the Chrome major
from `_CHROME_VERSIONS` and the OS from `_OS_VARIANTS` and formats
`sec-ch-ua`, `sec-ch-ua-mobile: ?0`, `sec-ch-ua-platform` off the same
draw (`proxy_pool.py:101-122`):

```python
def _build_headers(self) -> dict[str, str]:
    major, full = self._rng.choice(_CHROME_VERSIONS)
    os_string, platform = self._rng.choice(_OS_VARIANTS)
    return {
        "User-Agent": f"Mozilla/5.0 ({os_string}) ... Chrome/{full} ...",
        # ...
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        # ...
    }
```

The high-entropy hints (`Sec-CH-UA-Full-Version-List`, `-Platform-Version`,
`-Arch`, `-Model`, `-Bitness`, `-Wow64`) are intentionally not sent —
sending them unprompted (without a prior `Accept-CH` server response) is
itself anomalous. See [05](./05-http-headers.md).

---

## Signal-to-defense map

| # | Signal | Layer | Owner file | Owner chapter |
|---|---|---|---|---|
| 1 | 100% API hit ratio | Warm-up | `proxy_pool.py:193-197,292-304` | [07](./07-cookies-sessions-warmup.md) |
| 2 | Hardcoded Referer | Headers | `parser.py:40-73`, `proxy_pool.py:326-328,344-345` | [05](./05-http-headers.md) |
| 3 | 1700 req/hr volumetric | Cadence + IP | `proxy_pool.py:32-60,240-241` + VPN pool | [08](./08-vpn-pool.md), throttler [07](./07-cookies-sessions-warmup.md) |
| 4 | First-request-is-/api/* | Warm-up | `proxy_pool.py:292-304` | [07](./07-cookies-sessions-warmup.md) |
| 5 | No session cookie echoed | Cookies | `proxy_pool.py:227,239,259-262`, `impersonate_client.py:54-60` | [07](./07-cookies-sessions-warmup.md) |
| 6 | Sec-Fetch=navigate on /api/* | Headers | `proxy_pool.py:124-133` | [05](./05-http-headers.md) |
| 7 | HTTP/1.1 + Chrome UA | TLS/HTTP2 | `impersonate_client.py:54-60`, `client.py:28-36` | [06](./06-tls-http2-fingerprinting.md) |
| 8 | Old TLS + new UA | TLS/HTTP2 | `impersonate_client.py:21-23` | [06](./06-tls-http2-fingerprinting.md) |
| 9 | Uniform inter-arrival | Cadence | `proxy_pool.py:32-60` | [07](./07-cookies-sessions-warmup.md) (partial defense) |
| 10 | Deterministic per-proxy fingerprint | — | Seeds at `proxy_pool.py:240-241,260-262` | Not defended — see below |
| 11 | Enumeration burst | — | `enumerator.py:139-159`, `scraper.py:108-153` | Not defended — see below |
| 12 | Missing Client Hints / mismatched | Client Hints | `proxy_pool.py:114-116` | [05](./05-http-headers.md) |

Actively defended: 1, 2, 4, 5, 6, 7, 8, 12 (eight signals, all layered).
Partially defended: 3 (limited by pool size), 9 (limited by throttler
range).
Not defended: 10, 11 (see next section).

---

## Log-analysis one-liners we deliberately kill

Every rule below is a small SQL or `awk` query an operator could run
against gunicorn or Django access logs. Each has a specific defense
component or is a residual signal we chose to accept.

**Rule L-1 (defends signal 2).** Referer variety per source IP:
```sql
SELECT src_ip, COUNT(DISTINCT referer) AS refv, COUNT(*) AS n
FROM access_log
WHERE ts > NOW() - INTERVAL '1 hour'
GROUP BY src_ip
HAVING n > 30 AND refv = 1 AND MAX(referer) = 'https://www.hklii.hk/';
```
Killed by `parser.referer_for` returning a URL-derived Referer, so
`refv > 3` per real workload.

**Rule L-2 (defends signal 4).** First request from IP:
```sql
SELECT src_ip, MIN(uri) FIRST_URI, COUNT(*) n
FROM access_log
GROUP BY src_ip, TRUNC(ts, INTERVAL '30 minute')
HAVING FIRST_URI LIKE '/api/%' AND n > 5;
```
Killed by warm-up GET on `https://www.hklii.hk/` — first request is
always `/`, second is the API stream.

**Rule L-3 (defends signal 6).** Sec-Fetch on API:
```
awk '$request_uri ~ /^\/api\// && $sec_fetch_mode == "navigate"' access.log
```
Killed by `HeaderRotator.generate(url)`'s `/api/` branch flipping
`mode=cors, dest=empty`.

**Rule L-4 (defends signal 12).** Sec-CH-UA / UA lockstep:
```sql
SELECT src_ip, user_agent, sec_ch_ua
FROM access_log
WHERE user_agent LIKE '%Chrome/1[0-9][0-9]%'
  AND sec_ch_ua NOT LIKE '%v="' || regexp_match(user_agent, 'Chrome/(\d+)') || '"%';
```
Killed by the `_build_headers` single-draw pattern: the UA `major` and
`sec-ch-ua` v-strings are the same variable, so a scan always finds
them matched.

**Rule L-5 (defends signal 5).** Cookie echo:
```sql
SELECT src_ip, COUNT(*) n
FROM access_log
WHERE cookie IS NULL OR cookie = ''
GROUP BY src_ip
HAVING n > 50;
```
Killed by per-proxy `AsyncSession` cookie persistence — after the
warm-up GET establishes cookies, every subsequent request from that
proxy echoes them.

**Rule L-6 (defends signal 3, partial).** Volumetric:
```
awk '$request_uri ~ /^\/api\//' access.log | \
  cut -d' ' -f<src_ip_col> | \
  sort | uniq -c | \
  awk '$1 > 200 {print}'
```
Partial: each proxy independently hits ~1730 req/hr — that's already
above the strong-signal threshold of 1000 and near the confirmed 1500.
Per-IP volumetric detection still catches each proxy in isolation; what
the 20-endpoint pool buys is that the *number* of flagged source IPs is
20 rather than 1, spread across at least 7 PIA regions (see
[08](./08-vpn-pool.md)). An operator joining by ASN would see the whole
pool as PIA infrastructure; joining by /24 subnet would too. Full defense
against a per-IP rate rule would require lowering the throttler cadence,
not just adding IPs — deliberately deferred (see [12](./12-decisions-log.md)).

**Rule L-9 (attacks signal 9).** Inter-arrival CV:
```sql
SELECT src_ip,
       STDDEV(delta_ms) / AVG(delta_ms) cv,
       MAX(delta_ms) / MIN(delta_ms) spread
FROM (SELECT src_ip, ts - LAG(ts) OVER w AS delta_ms
      FROM access_log
      WINDOW w AS (PARTITION BY src_ip ORDER BY ts))
GROUP BY src_ip
HAVING spread < 20 AND cv < 0.5;
```
Only partially defeated. Throttler bursts and pauses raise `cv` to
~0.5-0.7 but `spread` stays under 16 (throttler max/min = 8/0.5 = 16).
Adding > 30 s pauses at ~1% rate would defeat this rule but would slow
production runs by 5-10%; not implemented.

---

## Deliberate non-defenses and residual signals

Some detection signals are cheap to detect but expensive to fully hide.
Below is what we chose to leave alone and why.

### Deterministic per-proxy fingerprint (signal 10)

Every seed is a deterministic function of the proxy index or URL:

- `RequestThrottler(rng=random.Random(i))` at `proxy_pool.py:240`
- `HeaderRotator(rng=random.Random(i + 1000))` at `proxy_pool.py:241`
- `ImpersonateAsyncClient(rng=random.Random(hash((proxy_url, "impersonate"))))` at `proxy_pool.py:260-262`

**Consequence.** Restart the scraper against the same VPN pool and each
proxy gets the same TLS impersonate profile, same UA draw sequence, and
same throttle jitter sequence. An operator storing per-IP fingerprints
across days would see identical fingerprints — a stronger signal than
"one Chrome build for six months" because a real Chrome build actually
rotates its high-entropy UA-CH values slightly across restarts.

**Why we accept it.** Reproducibility during development is worth more
than the marginal detection cost against a WAF-less origin. The seed
scheme is trivial to switch to `random.SystemRandom()` if a WAF flips
on. Cross-referenced in [12](./12-decisions-log.md).

### Enumeration burst at process start (signal 11)

`BulkScraper.enumerate` iterates courts sequentially and each
`enumerate_court` walks pages sequentially at `itemsPerPage=10_000`
(`src/hklii_downloader/scraper.py:108-153`,
`src/hklii_downloader/enumerator.py:139-159`). This means the first 30-90
seconds of a run is a bursty stream of 8-13 large-payload requests to
`/api/getcasefiles`, all against the same endpoint, from a small number
of IPs (worst case 1 IP for the whole enum).

**Why we accept it.** Alternatives are worse:

- Interleaving `pageSize=10` would generate ~6423 hits for HKCFI alone (see [12 Decisions log](./12-decisions-log.md) § "Why `itemsPerPage=10_000`" — 300× more traffic, 1.5-2× slower wall-clock).
- Adding jitter between enum pages adds ~10-30 s per court for a benefit that dissolves in a 20-hour download run.
- Running enumeration through a different IP than downloads does not help; the operator can join by ASN.

The `--enum-max-age HOURS` flag (`src/hklii_downloader/scraper.py:116-124`)
lets a re-run skip enumeration entirely if the cache is fresh, so a
resume operation does not repeat the burst. See
[11](./11-operations-runbook.md).

### Perfect UA rotation across runs

`HeaderRotator._build_headers()` picks one UA at HeaderRotator
construction time (`proxy_pool.py:99,101-122`) and reuses it for every
request from that proxy. Real Chrome pins UA per browser install for
weeks, so this is fine intra-session; across process restarts a proxy
gets a new UA, which is more browser-like than pinning by IP.

### Non-defenses that are actually gluetun/PIA constraints

- No residential IP diversity beyond PIA's data-center exit ranges. PIA IPs are known IP ranges an operator can grep for. See [08](./08-vpn-pool.md) for pool composition and known limits.
- No IPv6 exit paths. PIA is IPv4-only for the regions we use.

### Non-defenses that are content-layer, not header-layer

The scraper does not fetch CSS, JS, fonts, favicons, or images (signal 1
partial). Fixing this would require a full SPA render (Playwright /
undetected-chromedriver / nodriver) which is a different architecture
and ~5-10× the resource cost per request. Since the current design does
not need JavaScript execution — HKLII's `/api/getjudgment` returns
pre-rendered HTML in the `content` field — the trade-off does not pay
back. See [12](./12-decisions-log.md) § "Why `curl_cffi` over httpx alone".

---

## Cross-reference to implementation chapters

Every defense listed above has a home in one of these sibling chapters.
This chapter names the signal and points at the defense; the sibling
gives the code, the wire-level values, and the rationale.

- **[05 — HTTP headers](./05-http-headers.md)** owns: full HeaderRotator behaviour, the Accept/UA/sec-ch-ua/Sec-Fetch quartet, XHR-vs-navigation split (signal 6), Referer derivation (signal 2), Client Hints emission (signal 12).
- **[06 — TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md)** owns: curl_cffi profile pool composition and rotation strategy (signal 8), JA3/JA4/JA4+ background, HTTP/2 SETTINGS and pseudo-header order (signal 7), which headers curl_cffi strips vs which we control (`_FINGERPRINT_HEADERS` at `impersonate_client.py:28-42`).
- **[07 — Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md)** owns: per-proxy cookie jar lifecycle (signal 5), warm-up mechanics (signals 1, 4), throttler burst structure (signals 3, 9), ProxySession circuit breaker + cooldown revival, IP-leak preflight and runtime re-check.
- **[08 — VPN pool](./08-vpn-pool.md)** owns: gluetun + PIA composition, `SERVER_NAMES` pinning, `expand_vpn_pool.py`, regional distribution and speed data, DNS leak safety — the substrate for signal 3 (volumetric) and signal 4 (first-request diversity across pool).
- **[10 — Content safeguards](./10-content-safeguards.md)** owns: S-1 challenge-marker rejection (7 English + 6 Traditional Chinese markers at `src/hklii_downloader/scraper.py:30-70,286-291`), empty-content vs doc-fallback branching, `verify` subcommand semantics — the last-line defense that catches Tier-3 JS challenges even if TLS mimicry gets us past the WAF handshake.

Cadence and retry backoff details — how `RequestThrottler.next_delay()`
gets called from `ProxyPool.get()`, how the exponential jittered
backoff at `scraper.py:50-58` de-correlates retries — are in
[07](./07-cookies-sessions-warmup.md) and
[09](./09-scraper-architecture.md) § "Jittered exponential backoff" respectively.

Every architectural choice above ("why the deterministic seed", "why
curl_cffi and not Playwright", "why bare 'chrome' plus four pins",
"why not fetch subresources", "why 4-court default") is anchored in
[12 — Decisions log](./12-decisions-log.md) with dates and data.
