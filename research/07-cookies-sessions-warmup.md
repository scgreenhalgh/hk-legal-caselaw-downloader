# Cookies, Sessions, and Warm-Up

This chapter documents the session lifecycle inside `ProxyPool`: how each
proxy owns an independent cookie jar, how IP leaks are caught before the
first API call and re-verified mid-run, how the M-4 landing-page warm-up
plants a plausible browsing history on every session, and how the
circuit breaker retires bad proxies and revives them after cooldown.

For the anti-detection logic these mechanisms implement, see
[Anti-Detection Strategy](./04-anti-detection-strategy.md). For the VPN
infrastructure that supplies the proxy URLs, see
[VPN Pool](./08-vpn-pool.md).

## Per-proxy client model

`ProxyPool.__init__` allocates one `ImpersonateAsyncClient` per proxy URL
and stashes it in `self._clients[i]` for the process lifetime — nothing
in the request path ever rebuilds a client. The relevant loop
(`src/hklii_downloader/proxy_pool.py:226-242`):

```python
self.sessions: list[ProxySession] = []
self._clients: dict[int, httpx.AsyncClient] = {}
self._throttlers: dict[int, RequestThrottler] = {}
self._headers: dict[int, HeaderRotator] = {}
self._available: asyncio.Queue[int] = asyncio.Queue()

for i, url in enumerate(proxy_urls):
    session = ProxySession(
        proxy_url=url, index=i,
        max_failures=max_failures,
        cooldown_seconds=cooldown_seconds,
    )
    self.sessions.append(session)
    self._clients[i] = self._make_client(url)
    self._throttlers[i] = RequestThrottler(rng=random.Random(i))
    self._headers[i] = HeaderRotator(rng=random.Random(i + 1000))
    self._available.put_nowait(i)
```

Four parallel dicts keyed by proxy index — client, throttler, header
rotator, and a `ProxySession` bookkeeping object — are all created up
front and never replaced. This matters because the client owns the
cookie jar. `_make_client` produces an `ImpersonateAsyncClient` in
production (`proxy_pool.py:258-262`):

```python
from .impersonate_client import ImpersonateAsyncClient
return ImpersonateAsyncClient(
    proxy=proxy_url, timeout=30.0,
    rng=random.Random(hash((proxy_url, "impersonate"))),
)
```

That wrapper holds a `curl_cffi.requests.AsyncSession`
(`src/hklii_downloader/impersonate_client.py:54-60`), and `AsyncSession`
persists cookies from every `Set-Cookie` header into its jar for
subsequent requests. Because the pool holds the client, the jar's
lifetime equals the process lifetime — a cookie set during the
homepage warm-up (see below) is still attached to the first `/api/*`
call the same proxy makes, and to the ten-thousandth. Only shutdown
via `ProxyPool.close()` closes the sessions and drops the jars
(`proxy_pool.py:404-408`).

Related, deterministic per-proxy state:

- `RequestThrottler` is seeded with `random.Random(i)` so a proxy's
  burst/gap pattern is reproducible across restarts.
- `HeaderRotator` is seeded with `random.Random(i + 1000)` so its
  Chrome major/full version and OS pick are stable.
- The `impersonate` profile is chosen from `random.Random(hash((proxy_url, "impersonate")))`
  so the same proxy URL always gets the same TLS/HTTP-2 fingerprint —
  see [TLS + HTTP/2 Fingerprinting](./06-tls-http2-fingerprinting.md).

Together those seeds mean each proxy is a distinct "user" whose
identity — TLS fingerprint, header set, throttle cadence, cookie jar —
is stable for the whole run.

## curl_cffi AsyncSession defaults

The `AsyncSession` that lives inside every `ImpersonateAsyncClient` is
constructed with exactly four kwargs
(`src/hklii_downloader/impersonate_client.py:55-60`):

```python
self._session = AsyncSession(
    impersonate=self._impersonate,
    timeout=timeout,          # 30.0 by default
    proxy=proxy,              # None in direct mode
    allow_redirects=True,
)
```

The two defaults worth naming:

| Kwarg              | Value    | Why                                                                                             |
| ------------------ | -------- | ----------------------------------------------------------------------------------------------- |
| `timeout`          | `30.0`   | Wall-clock ceiling per request. Matches httpx `Timeout(30.0)` used by the mock-transport path. |
| `allow_redirects`  | `True`   | Chrome follows redirects invisibly; a Python client that stops on `302` and returns bare would look wrong. Warm-up + IP-echo relies on this. |

There is no per-request cookie or session override — every `get()` on
the wrapper flows through the same `AsyncSession._session`, so any
`Set-Cookie` the origin returns automatically enters the jar and
automatically ships back on the next call. This is different from raw
`httpx.AsyncClient`, where cookies work the same way but the
fingerprint layer is your problem; here curl_cffi owns both cookies and
fingerprint (see [Chapter 06](./06-tls-http2-fingerprinting.md)).

## IP-leak preflight

Before a single scrape request goes out, `ProxyPool.preflight()` proves
each proxy is actually routing traffic through a different exit IP than
the box the scraper is running on. The two-URL echo fallback chain
lives at `proxy_pool.py:188-191`:

```python
_IP_ECHO_URLS: list[tuple[str, str]] = [
    ("https://httpbin.org/ip", "origin"),
    ("https://ipinfo.io/json", "ip"),
]
```

Each tuple pairs a URL with the JSON key that carries the caller IP.
`_fetch_ip` walks the list in order (`proxy_pool.py:306-319`):

```python
async def _fetch_ip(self, client: httpx.AsyncClient) -> str:
    for echo_url, json_key in _IP_ECHO_URLS:
        try:
            resp = await client.get(echo_url)
            # Check status_code directly instead of raise_for_status —
            # curl_cffi raises its own HTTPError class, not
            # httpx.HTTPStatusError, so relying on raise_for_status would
            # let a curl_cffi exception escape the except block.
            if resp.status_code >= 400:
                continue
            return resp.json()[json_key]
        except (httpx.RequestError, KeyError, json.JSONDecodeError):
            continue
    raise httpx.ConnectError("All IP echo services unreachable")
```

httpbin.org is tried first because it is a Postman-run service with
strong uptime; ipinfo.io is the fallback because it is a distinct
provider on a distinct network — the goal is to survive one of them
being down, not to load-balance. Failure semantics:

- A network error against the first echo (`httpx.RequestError`) — falls
  through to the next.
- A malformed JSON response (`json.JSONDecodeError`) or a missing key
  (`KeyError`, e.g. schema change) — falls through.
- HTTP status `>= 400` (rate-limit, service degradation) — falls
  through without raising.
- All echoes fail — a single `httpx.ConnectError('All IP echo services unreachable')`
  is raised so the caller can attribute the failure to the echo layer,
  not to the proxy.

### Why `status_code >= 400` and not `raise_for_status()`

The inline comment above spells the gotcha out. In production, the
client is `ImpersonateAsyncClient`, and `curl_cffi.requests.Response`
implements its own `raise_for_status()` that raises `curl_cffi`'s
`HTTPError`, not `httpx.HTTPStatusError`. The `except (httpx.RequestError, KeyError, json.JSONDecodeError)`
handler would not catch that — the exception would escape `_fetch_ip`
into the preflight caller and be reported as a hard failure rather than
falling through to the next echo. Checking `status_code` directly
sidesteps the exception hierarchy mismatch entirely.

### Preflight decision

`ProxyPool.preflight()` orchestrates the whole dance
(`proxy_pool.py:264-290`):

```python
async def preflight(self) -> PreflightResult:
    home_ip = await self._fetch_ip(self._make_client(None))
    self._home_ip = home_ip
    result = PreflightResult(home_ip=home_ip)

    for session in self.sessions:
        client = self._clients[session.index]
        try:
            proxy_ip = await self._fetch_ip(client)
        except (httpx.RequestError, KeyError) as exc:
            result.failed_proxies.append(
                f"{session.proxy_url} unreachable: {exc}"
            )
            session.kill()
            continue

        if proxy_ip == home_ip:
            result.leaked_proxies.append(
                f"{session.proxy_url} returned home IP {home_ip}"
            )
            session.kill()
        else:
            result.healthy_proxies.append(session.proxy_url)
            await self._warm_up_target(session, client)

    self._preflight_done = True
    return result
```

The decision table:

| Outcome                       | Action                                              |
| ----------------------------- | --------------------------------------------------- |
| Home echo throws              | `preflight()` itself raises — nothing to compare against; bail out. |
| Proxy echo throws             | Proxy URL appended to `failed_proxies`; `session.kill()`; skip warm-up. |
| Proxy IP equals home IP       | Proxy URL appended to `leaked_proxies`; `session.kill()`; skip warm-up. The tunnel is up but not routing traffic through it. |
| Proxy IP differs from home IP | Proxy URL appended to `healthy_proxies`; **warm-up runs** on that session. |

Killing a session on preflight failure means it will never enter the
availability queue for scraping. It is not a candidate for cooldown
revival either — `cooldown_elapsed` requires a `_killed_at` timestamp
that was set by `kill()`, but the availability queue was never seeded
for that index (the `_available.put_nowait(i)` in `__init__` fires
before preflight can kill anything, so dead sessions get their slot but
`_acquire_session` filters them out — see below).

The `_preflight_done = True` gate at the end enforces order: any
`get()` call before preflight raises `RuntimeError("Must call preflight() before making requests")`
at `proxy_pool.py:322-323`. This is what the CLI relies on
(`src/hklii_downloader/cli.py:399-400`, `:548-549`):

```python
click.echo("Running preflight IP checks...")
result = await pool.preflight()
```

Both the `scrape` and `enrich` subcommands run preflight before
kicking off workers, and both refuse to continue if no healthy proxy
survives.

### Runtime IP re-check

VPN tunnels drop, exit IPs rotate mid-session, and PIA occasionally
moves a client to a different endpoint under load — a proxy that
passed preflight can start returning the home IP hours later. The
guard against that lives inside `ProxyPool.get()` at
`proxy_pool.py:340-342`:

```python
if (session.request_count > 0
        and session.request_count % self._ip_check_interval == 0):
    await self._runtime_ip_check(session, client)
```

`ip_check_interval` defaults to `50` (constructor at
`proxy_pool.py:211`). Every fiftieth request per session, the pool
re-fetches the exit IP through that session's client — meaning it
piggybacks on the same TLS/HTTP-2 fingerprint and cookie jar, not a
side channel. The check is a double-confirmation
(`proxy_pool.py:381-402`):

```python
async def _runtime_ip_check(
    self, session: ProxySession, client: httpx.AsyncClient,
) -> None:
    try:
        current_ip = await self._fetch_ip(client)
    except httpx.RequestError:
        return

    if current_ip != self._home_ip:
        return

    try:
        verify_ip = await self._fetch_ip(client)
    except httpx.RequestError:
        return

    if verify_ip == self._home_ip:
        session.kill()
        raise IPLeakError(
            f"Proxy {session.proxy_url} leaking home IP {self._home_ip} "
            f"(verified twice)"
        )
```

Two consecutive echoes must both come back as the home IP before the
session is killed and `IPLeakError` propagates to the scraper worker.
That extra fetch guards against a single flaky echo response — one of
the two echo providers occasionally returns a cached response or an
edge misroute that resembles the home IP for a moment.

Silent early returns matter: if either `_fetch_ip` call throws
`httpx.RequestError`, `_runtime_ip_check` returns without killing the
session. Refusing to kill on echo-service failure is deliberate — a
transient outage of httpbin.org and ipinfo.io simultaneously is more
likely than a real VPN failure that produces exactly two clean echoes
saying "you are the home box." The session gets the benefit of the
doubt until the next interval.

`IPLeakError` is caught explicitly by `scraper._download_one`
(`src/hklii_downloader/scraper.py:184-230`) — the case is marked failed
with the exception string, and the worker moves on. The session
staying dead means `_acquire_session` will never hand it out again;
the next request pulls a different proxy.

## Session warm-up (M-4)

`_warm_up_target` is the last thing preflight does for a healthy
proxy, and it exists for one reason: to make sure the very first HKLII
request on that TCP session is a landing page, not an API call. The
implementation (`proxy_pool.py:292-304`, constant at `:197`):

```python
_WARMUP_URL = "https://www.hklii.hk/"

async def _warm_up_target(self, session: ProxySession, client) -> None:
    """Fire one landing-page GET so the first API call from this proxy
    has a plausible browsing history (session cookies, Referer chain).
    Best-effort — failure here does not disqualify the proxy since IP
    echo already confirmed routability."""
    headers = self._headers[session.index]
    req_headers = headers.generate(_WARMUP_URL)
    req_headers["Referer"] = headers.referer_for(_WARMUP_URL)
    try:
        await client.get(_WARMUP_URL, headers=req_headers)
    except (httpx.RequestError, Exception):
        # Best-effort — do not fail preflight if the origin blips.
        pass
```

Points worth naming:

- **Navigation shape, not XHR.** `_WARMUP_URL` contains no `/api/`,
  so `HeaderRotator.generate()` returns the navigation quartet
  (`sec-fetch-mode: navigate`, `sec-fetch-dest: document`,
  `sec-fetch-user: ?1`, `Upgrade-Insecure-Requests: 1`) — see
  [HTTP Headers](./05-http-headers.md). This is what Chrome sends when
  a human types `www.hklii.hk` into the URL bar.
- **Referer is derived.** `headers.referer_for(_WARMUP_URL)` runs the
  same `parser.referer_for()` used everywhere else. For the homepage
  it returns `https://www.hklii.hk/` (the URL is not `/api/*`, is on
  `www.hklii.hk`, and does not match the `/en/cases/...` path pattern
  — see `parser.py:40-73`).
- **Cookie plant.** If the HKLII origin issues a session cookie on the
  landing page — or a CSRF token, or a language preference — the jar
  captures it here. Every subsequent `/api/*` call from this same
  proxy client automatically carries it.
- **Best-effort swallow.** Any exception (`httpx.RequestError` or
  otherwise) is caught silently. Preflight has already proved the
  proxy routes; a transient blip on the HKLII origin should not
  disqualify an otherwise-healthy exit. The proxy still ends up in
  `healthy_proxies`.

### Warm-up rationale

The direct target of this warm-up is
[suspicion signal 4](./04-anti-detection-strategy.md) — "first request
from IP is `/api/*` with no prior HTML shell load." An access-log
one-liner catches it trivially:

```sql
SELECT src_ip, MIN(uri) FROM access_log
GROUP BY src_ip, TRUNC(ts, 30min)
HAVING FIRST_URI LIKE '/api/%' AND count>5;
```

For a real Chrome user, `FIRST_URI` is `/`, `/en/`, `/en/cases/`, or a
static asset like `/favicon.ico`. Without warm-up, every fresh proxy
posts an `/api/getcasefiles` or `/api/getjudgment` as its very first
byte of traffic — a cold-cache XHR that has no legitimate analogue.
With warm-up, the log line for that proxy's first minute reads:

```
GET / HTTP/2 200 text/html ...
GET /api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10000&page=1 HTTP/2 200 application/json ...
```

Which is exactly the shape a Chrome-loading-the-SPA-and-then-triggering-XHR
session produces. It does not defeat volumetric detection (signal 3)
or cadence detection (signal 9), but it removes the "first-request-is-API"
tell for free.

There is one asymmetry: warm-up fires once per proxy per **process
lifetime**, not once per HKT hour or per session-cookie expiry. If a
run lives for 24 hours and HKLII's cookies TTL out at 12, the cookie
jar goes stale and there is no code path that refreshes it. In
practice HKLII does not appear to set session cookies at all (the
scraper works with an empty jar), so this is a hypothetical risk. The
warm-up is worth doing anyway because the "first URI is `/`" pattern
is the observable win, not the cookie.

## ProxySession circuit breaker

`ProxySession` (defined `proxy_pool.py:139-185`) is the bookkeeping
object that decides when a proxy has failed too many times to keep
using. The constructor defaults matter:

```python
def __init__(
    self,
    proxy_url: str = "",
    index: int = 0,
    max_failures: int = 5,
    cooldown_seconds: float = 300.0,
):
```

- **`max_failures = 5`** consecutive failures kills the session.
  Consecutive is key — a single success resets the counter to zero
  (`record_success` at `:160-163`). Five 502s in a row and the
  session is out; four 502s and a 200 leaves it healthy.
- **`cooldown_seconds = 300.0`** (five minutes) is how long a killed
  session waits before `_revive_cooled_down_sessions` puts it back
  into rotation. Long enough for a rate-limit window to slide off,
  short enough that the next courthouse's rollup doesn't miss it.

The bookkeeping:

```python
def record_success(self) -> None:
    if not self._killed:
        self._failure_count = 0
        self.request_count += 1

def record_failure(self) -> None:
    if self._killed:
        return
    self._failure_count += 1
    if self._failure_count >= self._max_failures:
        self.kill()

def kill(self) -> None:
    self._killed = True
    self._killed_at = _monotonic()

@property
def cooldown_elapsed(self) -> bool:
    if not self._killed or self._killed_at is None:
        return False
    return (_monotonic() - self._killed_at) >= self._cooldown_seconds
```

`_monotonic()` (`proxy_pool.py:13`) is `time.monotonic`, which is
immune to wall-clock jumps (NTP corrections, DST). `kill()` also stops
`record_success` from ever bumping `request_count` — the session is
frozen until `revive()` clears both flags.

Revival happens inside `_acquire_session` on every wait
(`proxy_pool.py:375-379`):

```python
def _revive_cooled_down_sessions(self) -> None:
    for session in self.sessions:
        if session.cooldown_elapsed:
            session.revive()
            self._available.put_nowait(session.index)
```

Note the `put_nowait` — a revived session is re-enqueued for the
availability queue immediately, and the next `get()` call can pick it
up. `revive()` clears `_killed`, `_killed_at`, and `_failure_count`,
so the session comes back with a full five-failure budget.

### Failure-status set

Not every HTTP error counts against the breaker. The rule is at
`proxy_pool.py:198-203`:

```python
# Status codes that count as a soft failure against the proxy's circuit
# breaker. 429/403/5xx all indicate the proxy (or the exit IP) is having
# trouble; if this repeats we should stop using it. 4xx client errors
# other than 403/429 (e.g. 404) are about the resource, not the proxy.
_PROXY_FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}
```

The distinction is important because a scrape run will produce 404s
(a case number never issued, or a bilingual page that only exists in
one language). 404 is a fact about the URL, not the exit IP, so
punishing the session for it would eventually kill perfectly good
proxies with a slow-drip of legitimate not-found responses.

The recording logic in `ProxyPool.get()` (`proxy_pool.py:347-359`):

```python
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

Two things worth naming:

- **Any exception is a failure.** `httpx.RequestError` (which
  `ImpersonateAsyncClient._translate` maps `curl_cffi` errors into —
  see [Chapter 06](./06-tls-http2-fingerprinting.md)) counts, and then
  re-raises so the scraper's retry loop can see it. Connection
  timeouts, TLS handshake failures, and read errors all decrement the
  budget.
- **Response is returned to the caller regardless.** Even a
  `_PROXY_FAILURE_STATUSES` hit still returns the response; the
  scraper's own `_RETRYABLE_STATUSES` logic
  (`src/hklii_downloader/scraper.py:26`) is what decides whether to
  loop or give up. The pool and the scraper both look at the same set
  of codes (`{403, 429, 500, 502, 503, 504}`) for different reasons —
  the pool for its breaker, the scraper for retry with jittered
  backoff.

The `finally` block only re-enqueues the session if it is still
healthy. If the fifth consecutive failure just killed it, the session
does not return to `_available`; the next `_acquire_session` will
either revive it after cooldown or skip past it.

### Availability queue and `_acquire_session`

The availability queue is an `asyncio.Queue[int]` seeded once with
every proxy index. `_acquire_session` is the loop that borrows one
(`proxy_pool.py:361-373`):

```python
async def _acquire_session(self) -> int:
    while True:
        self._revive_cooled_down_sessions()
        if not any(s.is_healthy for s in self.sessions):
            raise AllProxiesDeadError("All proxy sessions are dead")
        try:
            idx = await asyncio.wait_for(
                self._available.get(), timeout=0.5,
            )
        except asyncio.TimeoutError:
            continue
        if self.sessions[idx].is_healthy:
            return idx
```

The 500 ms wait timeout is what keeps the loop reactive. Reading step
by step:

1. Revive any session whose cooldown has elapsed and put its index
   back on the queue.
2. If every session is dead — including cooled-down ones that would
   have been revived above — raise `AllProxiesDeadError` and let the
   scraper stop the run. This is a total failure signal, not a per-case
   error.
3. Wait up to 500 ms for an index off the queue. Timeout means every
   healthy session is currently busy; loop back and re-check for
   revives.
4. If the borrowed index is for a session that got killed while
   sitting in the queue (e.g. a `_runtime_ip_check` killed it between
   put and get), discard the index and loop.

The alternative — a blocking `await self._available.get()` — would
never notice when a cooled-down proxy is ready to come back. The
polling loop trades a bit of CPU (twice a second per idle worker) for
prompt revival.

## No cross-proxy cookie continuity by design

Every design decision in this chapter reinforces one property: each
proxy is a completely independent "user" from HKLII's point of view.

- Each has its own `AsyncSession` cookie jar (never shared).
- Each has its own `impersonate` TLS/HTTP-2 fingerprint (seeded by
  proxy URL hash).
- Each has its own `HeaderRotator` (Chrome major version + OS pair).
- Each has its own `RequestThrottler` state (burst counter,
  inter-request delay pattern).
- Each does its own preflight IP echo, its own warm-up GET, and its
  own runtime IP re-check every 50 requests.
- Each has its own five-failure circuit breaker.

There is no code path that reads a `Set-Cookie` from proxy A's client
and installs it into proxy B's jar. There is no code path that shares
a warm-up response, a cached Referer, or a session cookie across
proxies. `ProxyPool` deliberately does not expose the cookie jar for
external inspection.

The rationale is defensive. If HKLII's log-analysis rules were smart
enough to notice "a session cookie we issued to IP X is now being
echoed from IP Y" — signal 5 in
[the suspicion catalog](./04-anti-detection-strategy.md) — cross-proxy
cookie leakage would light up like a beacon. Keeping the jars
isolated means the worst you can catch us on is "we have no session
cookie at all," which is much harder to distinguish from a legitimate
first-visit Chrome. HKLII's `Set-Cookie` behavior has not been
observed in the wild (the API responses do not include one), so this
is protection against a future behavior change more than a current
threat.

The one place the isolation is intentionally weakened is the direct
mode of `ProxyPool.get()` (`proxy_pool.py:325-328`):

```python
if self.direct:
    direct_headers = dict(kwargs.pop("headers", None) or {})
    direct_headers.setdefault("Referer", _referer_for(url))
    return await self._direct_client.get(url, headers=direct_headers, **kwargs)
```

Direct mode has one shared `_direct_client` (`proxy_pool.py:244-245`),
so cookies persist within the process but there is only "one user."
Direct mode is not intended for volume — see
[Anti-Detection Strategy](./04-anti-detection-strategy.md) for what it
still fails to hide.

## Where each defense lands

| Layer | Defends against | Detail chapter |
| ----- | --------------- | -------------- |
| Preflight IP echo | Silently broken VPN tunnel (data leaks over home ISP) | This chapter |
| Runtime IP re-check | Mid-run tunnel drop / VPN endpoint rotation | This chapter |
| Warm-up GET `/` | Signal 4: first request is `/api/*` with no HTML shell | [Ch. 04](./04-anti-detection-strategy.md) §4 |
| Per-proxy `AsyncSession` | Signal 5: no session cookie ever echoed | [Ch. 04](./04-anti-detection-strategy.md) §5 |
| `_PROXY_FAILURE_STATUSES` counter | Long-run pool bleed on a persistent-403 exit | This chapter |
| 300 s cooldown + revive | Not permanently retiring proxies over a 5-minute rate-limit spike | This chapter |
| Availability queue polling | Prompt reuse when a proxy comes back healthy | This chapter |

## Cross-references

- **VPN URLs and gluetun containers** that back every proxy in
  `self._clients[i]`: see [VPN Pool](./08-vpn-pool.md).
- **TLS fingerprint and header stripping** inside the
  `ImpersonateAsyncClient` this chapter treats as a black box: see
  [TLS + HTTP/2 Fingerprinting](./06-tls-http2-fingerprinting.md).
- **XHR-vs-navigation header split** that warm-up depends on to emit
  the correct `sec-fetch-*` quartet on the landing-page GET: see
  [HTTP Headers](./05-http-headers.md).
- **The suspicion signals catalogue and access-log rules** the warm-up
  and cookie-isolation choices exist to defeat: see
  [Anti-Detection Strategy](./04-anti-detection-strategy.md).
- **Why `_PROXY_FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}`,
  why `max_failures=5`, why `cooldown_seconds=300`**, and why warm-up
  fires per proxy per process (not per hour): see
  [Decisions Log](./12-decisions-log.md).
