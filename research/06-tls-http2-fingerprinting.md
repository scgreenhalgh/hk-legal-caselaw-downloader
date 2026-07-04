# TLS + HTTP/2 Fingerprinting

This chapter is the single source of truth for how the scraper produces plausible browser TLS and HTTP/2 fingerprints. It covers the mechanics of JA3/JA4/JA4+, what `curl_cffi` actually controls on the wire, which impersonation profiles we ship, how they are assigned per proxy, and where HTTP/2 shows up in code paths that do not go through `curl_cffi`.

Sibling reads:

- [04 Anti-detection strategy](./04-anti-detection-strategy.md) — the layered posture and the 12 suspicion signals. This chapter implements defenses against signals 6, 7, 8, 10, and 12.
- [05 HTTP headers](./05-http-headers.md) — the HTTP-header layer that sits above the TLS layer, including the Sec-Fetch split and Referer derivation. Header content is owned there; this chapter only says which headers `curl_cffi` strips and why.
- [07 Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md) — per-proxy session model, cookie jar lifecycle, and warm-up flow. This chapter only touches the one line where profiles get pinned to a proxy.
- [11 Operations runbook](./11-operations-runbook.md) — how to invoke `hklii scrape` in the modes that exercise these code paths.
- [12 Decisions log](./12-decisions-log.md) — the historical decision to ship `curl_cffi` at all, the 2026-07-04 profile refresh, and why HTTP/2 in direct mode landed as M-6.

## Why fingerprinting matters

The scraper's threat model (see [04](./04-anti-detection-strategy.md)) puts TLS/HTTP-2 fingerprinting at Tier 3: uncommon on plain gunicorn/Apache origins, common on Cloudflare/Akamai/AWS-WAF/F5 fronts. HKLII is currently plain gunicorn/Django with no CDN or WAF ([01 HKLII platform](./01-hklii-platform.md)), so fingerprint mimicry is insurance against a future flip, not a fix for a current block. That framing matters — it is why the profile pool is not exotic, why we do not chase HTTP/3, and why nothing here is on the critical path for the current corpus grab.

The insurance is worth carrying because the fingerprint layer is uniquely hard to bolt on after a WAF turns up. If HKLII is fronted by Cloudflare mid-run, editing headers and Referers takes minutes; getting a stock `httpx.AsyncClient` past Cloudflare Bot Fight Mode is essentially impossible without swapping the transport. `curl_cffi` gives us that transport swap already in place.

### JA3 died in 2023

JA3 was the dominant TLS-client fingerprint from 2017 to about 2023. It concatenated five fields of the TLS ClientHello (version, cipher suites, extensions, elliptic curves, EC point formats) in the exact order the client sent them, MD5-hashed the result, and gave detection engines a stable per-client-library hash — Python's `requests` had one, `curl` had one, Chrome had one, and they were all distinguishable.

Chrome 110 (January 2023) began randomizing the order of TLS extensions in every ClientHello, deliberately to invalidate JA3 as a targeting mechanism. Every connection from the same Chrome install produced a different JA3 hash. Detection stacks could no longer allowlist "known Chrome" or blocklist "known bot" by a single JA3.

### JA4 replaced it, 2024–2025

FoxIO published JA4 in late 2023. Cloudflare, AWS WAF, Akamai, DataDome, and VirusTotal all shipped JA4 collection by end of 2024 and treat it as primary by 2026. JA4 fixes the randomization problem by **sorting** cipher suites and extensions alphabetically before hashing — the ordering that Chrome deliberately randomizes is normalized away. What remains stable is the *set* of ciphers and extensions, which is still Chrome-specific.

For the scraper, "JA3 was already dead" is why we do not attempt to match a specific JA3 — there is no such thing anymore. "JA4 is primary in 2026" is why the profile pool has to track a modern Chrome (`chrome146` was the newest at the 2026-07-04 refresh) rather than freezing on `chrome104` from 2022.

## JA4 / JA4+ suite

JA4 is not a single hash. It is a family. The six members are:

| Fingerprint | Layer | What it captures | Detection use |
|---|---|---|---|
| **JA4**  | TLS client | ClientHello cipher suites + extensions + ALPN | Identify client library / browser version |
| **JA4S** | TLS server | ServerHello cipher choice + extensions | Identify server library / TLS terminator |
| **JA4H** | HTTP     | Method, version, cookie flag, Referer flag, Accept-Language, header count/order | Distinguish browser vs library HTTP layer |
| **JA4X** | X.509    | Issuer + subject OIDs, extensions, signature algo | Match malware C2 certs, identify CAs |
| **JA4L** | Latency  | TCP handshake / TLS handshake RTT distribution | Passive geolocation / VPN detection |
| **JA4T** | TCP      | SYN window, options ordering, MSS, TCP timestamps | Passive OS fingerprint below TLS |

The scraper's insurance covers **JA4, JA4H, and JA4T** through `curl_cffi`'s impersonation — the library sets the exact ClientHello, HTTP-layer, and (via its libcurl fork) the raw TCP options that a real Chrome would produce. **JA4S** is server-controlled and out of scope. **JA4X** applies to certificates we present as a client, which we do not do (no mTLS). **JA4L** is passive and depends on real network RTTs — we cannot forge it, and it is the one dimension where a determined WAF operator could still infer "this connection has too-uniform timing for a real human."

### JA4 format walkthrough

A JA4 client fingerprint reads `t13d1516h2_8daaf6152771_b0da82dd1658`:

| Segment | Meaning |
|---|---|
| `t`    | Transport: `t` = TCP, `q` = QUIC (HTTP/3) |
| `13`   | TLS version: `13` = 1.3, `12` = 1.2 |
| `d`    | SNI present: `d` = domain in SNI, `i` = IP or absent |
| `15`   | Cipher suite count: 15 offered |
| `16`   | Extension count: 16 offered |
| `h2`   | First ALPN: `h2` = HTTP/2, `h1` = HTTP/1.1, `h3` = HTTP/3 |
| `_8daaf6152771` | Truncated SHA-256 of sorted cipher-suite list |
| `_b0da82dd1658` | Truncated SHA-256 of sorted extension list |

A real Chrome 146 macOS handshake produces `t13d1517h2_8daaf6152771_02713d6af862` (extension count 17 because Chrome added a slot, ciphers stable). A stock `httpx.AsyncClient` on Python 3.11 produces something recognizably different — different cipher count, different ALPN order, different extension set. That is the "you are not Chrome" signal we neutralize by putting `curl_cffi` on the wire instead.

## curl_cffi's role

`curl_cffi` is Python bindings around a fork of libcurl that has been patched to emit the exact TLS ClientHello, HTTP/2 SETTINGS, and TCP-layer options of a specific browser build. It is the closest thing to "run real Chrome without the DOM" available in the Python ecosystem in 2026.

What it owns on the wire:

- **TLS ClientHello**: cipher suite list and ordering (Chrome does randomize order per connection in 2023+, `curl_cffi` matches that), extension list (session ticket, key share, ALPS, GREASE positions), signature algorithms, supported groups, ALPN vector.
- **HTTP/2 SETTINGS frame** sent first from the client: header table size, enable-push, max concurrent streams, initial window size, max frame size, max header list size.
- **HTTP/2 WINDOW_UPDATE** frame on stream 0 with the browser's window size (Chrome ships ~15 MB, curl default is 64 KB — a huge tell).
- **HTTP/2 pseudo-header order** — `:method`, `:authority`, `:scheme`, `:path` for Chrome (order `m,a,s,p`), different orderings for Firefox and Safari, and yet a fourth for stock curl.
- **TCP SYN options** on the underlying socket, insofar as libcurl controls them.

What it does not own:

- Everything above HTTP/2 headers — the actual header names and values are still whatever the caller passes in.
- Cookies — those live in the AsyncSession's jar and are set/echoed by the caller and server, not by the impersonation profile.
- Timing — request cadence, burst pattern, and inter-arrival distribution are the throttler's job (see [07](./07-cookies-sessions-warmup.md) and [04](./04-anti-detection-strategy.md) signal 9).

This means the code has to be careful about **layer boundaries**. The header content is emitted by `HeaderRotator` in [05](./05-http-headers.md). If the caller sends a `user-agent: Mozilla/5.0 ... Chrome/148 ...` while `curl_cffi` is impersonating `chrome131`, the wire shows a Chrome 131 TLS handshake with a Chrome 148 UA — a classic mismatch (suspicion signal 8). The next section describes how the shim prevents that.

## ImpersonateAsyncClient shim

`src/hklii_downloader/impersonate_client.py:45-90` defines `ImpersonateAsyncClient`, a small wrapper that presents a subset of `httpx.AsyncClient`'s surface (`.get(url, headers=..., **kwargs)` and `.aclose()`) while delegating to `curl_cffi.requests.AsyncSession`. It exists for two reasons:

1. **Header hygiene**: strip caller-supplied headers that would collide with the impersonated profile before they hit `curl_cffi`.
2. **Exception translation**: convert `curl_cffi`'s libcurl-derived exception codes into `httpx`'s exception hierarchy so `scraper.py` and `enumerator.py` retry logic works unchanged.

### Header strip list

`_FINGERPRINT_HEADERS` at `src/hklii_downloader/impersonate_client.py:28-42` is the fixed set of header names that `curl_cffi`'s impersonation controls end-to-end:

```python
_FINGERPRINT_HEADERS = frozenset({
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "connection",
})
```

`.get()` at `impersonate_client.py:66-71` filters caller-supplied headers by lowercased name against that set:

```python
async def get(self, url: str, headers: dict | None = None, **kwargs: Any):
    if headers:
        headers = {
            k: v for k, v in headers.items()
            if k.lower() not in _FINGERPRINT_HEADERS
        }
```

Two consequences worth flagging:

- `HeaderRotator._build_headers` (`proxy_pool.py:101-122`, see [05](./05-http-headers.md)) still generates a full Chrome header set including UA, `sec-ch-ua`, `sec-fetch-*`, Accept-Encoding, and Connection. Under `curl_cffi` those are stripped and the profile's own values ship instead. The `HeaderRotator` output is authoritative in direct mode (which uses `httpx.AsyncClient` at `client.py:28-36`) and in the test harness (`_transport_factory`), but a no-op for the fingerprint layer in production.
- Only lowercased names match. That is safe because HTTP/2 mandates lowercased header names on the wire anyway. If someone adds a header with the same semantic role but a different name (e.g. `SEC-CH-UA-Full-Version-List`) the strip list misses it and the caller value ships. There is no high-entropy Client-Hint header currently emitted by `HeaderRotator`, so this is a latent hazard rather than a live bug.

### Session construction

`impersonate_client.py:52-60`:

```python
rng = rng or random.Random()
self._impersonate = rng.choice(_IMPERSONATE_PROFILES)
from curl_cffi.requests import AsyncSession
self._session = AsyncSession(
    impersonate=self._impersonate,
    timeout=timeout,
    proxy=proxy,
    allow_redirects=True,
)
```

Defaults: `timeout=30.0` seconds, `allow_redirects=True`. The `rng` argument lets the caller pin a deterministic profile (see the Deterministic per-proxy assignment section below); without it, every session picks a fresh random profile per process start.

The `impersonate_profile` property at `impersonate_client.py:62-64` exposes the chosen profile string, currently unused in code but useful for logs during triage.

### curl_cffi exception translation table

The scraper's retry loop and circuit breaker in `proxy_pool.py:347-359` and `scraper.py:232-281` catch `httpx.RequestError` and its subclasses. `curl_cffi` raises its own hierarchy rooted at `curl_cffi.requests.errors.RequestsError` with a numeric `.code` field from libcurl's `CURLcode` enum. The `_translate` method at `impersonate_client.py:80-90` maps the important codes:

| curl_cffi code | libcurl name             | httpx exception raised |
|----------------|--------------------------|------------------------|
| `28`           | `CURLE_OPERATION_TIMEDOUT` | `httpx.TimeoutException` |
| `6`            | `CURLE_COULDNT_RESOLVE_HOST` | `httpx.ConnectError` |
| `7`            | `CURLE_COULDNT_CONNECT` | `httpx.ConnectError` |
| `56`           | `CURLE_RECV_ERROR` | `httpx.ReadError` |
| anything else  | (any other libcurl error) | `httpx.RequestError` |

The reason this table matters: `httpx.TimeoutException` is caught at `scraper.py:232-281` as part of the jittered-backoff retry, and `httpx.ConnectError` is what triggers the ProxySession circuit breaker to increment `_failure_count` at `proxy_pool.py:354-356`. If translation dropped a real timeout to a generic `httpx.RequestError`, the retry would still happen but the timeout-specific logging would be wrong. If it dropped a resolve failure to `httpx.RequestError` instead of `ConnectError`, the caller could not distinguish "proxy is down" from "target server rejected the request."

The `_fetch_ip` helper at `proxy_pool.py:306-319` also documents in-code the reason for checking `resp.status_code >= 400` rather than calling `resp.raise_for_status()`:

```python
# Check status_code directly instead of raise_for_status —
# curl_cffi raises its own HTTPError class, not
# httpx.HTTPStatusError, so relying on raise_for_status would
# let a curl_cffi exception escape the except block.
```

That is another symptom of the same impedance mismatch: response-object methods are not translated, only session-level exceptions.

## Profile pool

`_IMPERSONATE_PROFILES` at `impersonate_client.py:21-23`:

```python
_IMPERSONATE_PROFILES = (
    "chrome", "chrome146", "chrome142", "chrome136", "chrome131",
)
```

Five entries. One is the alias `"chrome"`, which `curl_cffi` resolves to its newest supported profile at `AsyncSession` construction (`src/hklii_downloader/impersonate_client.py:53-55` picks a string from the tuple; `AsyncSession(impersonate=…)` maps `"chrome"` to a concrete profile then) — currently `chrome146` (June 2026). Four are explicit version pins.

The pool intentionally spans a version window. If a WAF is silly enough to allowlist a single JA4, we do not care — we spread across five hashes. If it flags "any Chrome older than 140" (which no WAF currently does — see the [04](./04-anti-detection-strategy.md) suspicion signal 8 discussion), we still have three modern entries in the pool.

### The 2026-07-04 refresh

Before the pre-production audit landed on 2026-07-04, the pool was `("chrome104", "chrome110", "chrome116")` — Chrome versions from July 2022, January 2023, and August 2023 respectively. In July 2026 that translates to a TLS fingerprint claiming to be a browser build three-plus years old, coming from an IP with a fresh Client-Hints pool and no cookies. That combination is exactly the "TLS says Chrome 104, UA says Chrome 148" mismatch (suspicion signal 8) that any post-2024 detection stack would flag.

M-3 in the pre-production audit swapped the pool for the current five entries. The rationale, decision, and evidence-of-staleness live in [12 Decisions log](./12-decisions-log.md) § "Why `chrome146/142/136/131` (plus bare `\"chrome\"` alias) and not `chrome104/110/116`."

### curl_cffi's full profile inventory (2026-07)

For context, `curl_cffi` 0.15.1b2 (June 2026) ships these Chrome profiles: `chrome99, chrome100, chrome101, chrome104, chrome107, chrome110, chrome116, chrome119, chrome120, chrome123, chrome124, chrome131, chrome133a, chrome136, chrome142, chrome145, chrome146`. It also ships Safari (`safari15_3`, `safari15_5`, `safari17_0`, `safari17_2_ios`) and Edge profiles. We do not use Safari or Edge — the HKLII 2026-07-04 traffic mix is heavily Chrome, and mixing Safari fingerprints with Chrome UA and Chrome Client Hints would produce mismatches that are worse than a homogeneous Chrome pool.

`chrome145` and `chrome146` are the two profiles that also carry HTTP/3 fingerprints. We do not currently opt into HTTP/3 (see below).

## Deterministic per-proxy profile assignment

`proxy_pool.py:260-262` is the one line where the pool interacts with the proxy pool:

```python
return ImpersonateAsyncClient(
    proxy=proxy_url, timeout=30.0,
    rng=random.Random(hash((proxy_url, "impersonate"))),
)
```

The RNG is seeded from `hash((proxy_url, "impersonate"))`, meaning:

- The profile assigned to `http://localhost:8888` is stable across restarts — the same URL always hashes to the same value in a single Python process, and `PYTHONHASHSEED` is unset in production so hashes are stable across process starts of the same interpreter version.
- Different proxies get different profiles. Twenty proxies map onto five profiles — pigeonhole says at least four proxies share every profile, but the *assignment* is stable per proxy URL.
- Same-proxy profile stability means a WAF that logs the JA4 sees "this IP always presents JA4 X" — plausibly a real Chrome user's stable install fingerprint. If we re-rolled the profile every process start the WAF would see "this IP presented four different Chrome fingerprints in a week," which is not what a real user does.

The determinism has a cost. Every restart of the scraper against the same 20-proxy pool produces the same (IP, JA4) tuples. That is suspicion signal 10 — deterministic per-proxy fingerprint stability across restarts, catalogued in [04](./04-anti-detection-strategy.md). The reasoning for accepting that trade-off is in [12](./12-decisions-log.md).

## HTTP/2 fingerprint contents

`curl_cffi`'s Chrome profiles emit exactly what a real Chrome build sends in its HTTP/2 preface. The Akamai canonical HTTP/2 fingerprint format `S[;]|WU|P[,]|PS[,]` decodes as:

- `S[;]` — SETTINGS frame values, `id:value` pairs separated by `;`
- `WU`   — WINDOW_UPDATE increment on stream 0
- `P[,]` — PRIORITY frames sent before any HEADERS (comma-separated `stream_id:exclusive:dep:weight`)
- `PS[,]` — pseudo-header order in the first HEADERS frame

### Chrome 144–146 SETTINGS

Real Chrome 144–146 sends `1:65536;2:0;4:6291456;6:262144`:

| Setting ID | Name                             | Chrome value | Default per RFC 7540 |
|------------|----------------------------------|--------------|---------------------|
| `1`        | `SETTINGS_HEADER_TABLE_SIZE`     | 65536 (64 KiB) | 4096 |
| `2`        | `SETTINGS_ENABLE_PUSH`           | 0 (disabled) | 1 |
| `4`        | `SETTINGS_INITIAL_WINDOW_SIZE`   | 6291456 (6 MiB) | 65535 |
| `6`        | `SETTINGS_MAX_HEADER_LIST_SIZE`  | 262144 (256 KiB) | unlimited |

Setting 3 (`MAX_CONCURRENT_STREAMS`) and Setting 5 (`MAX_FRAME_SIZE`) are omitted by Chrome, both a stable Chrome-ism. Stock Python HTTP/2 clients (`hyper`, `h2` directly, and `httpx[http2]`'s default) send different SETTINGS — most tellingly, they usually send Setting 3 and often use RFC-default window sizes.

### WINDOW_UPDATE 15663105

Chrome then immediately sends `WINDOW_UPDATE stream=0 increment=15663105`. That value is `2^24 - 1 - 65535 - 1` bytes — Chrome bumps the connection-level window to almost 16 MiB. Stock curl's default WINDOW_UPDATE is a 64 KiB increment. The gap is more than two orders of magnitude and is one of the single most reliable HTTP/2 tells — a "Chrome 146" UA that sends a 64 KiB WINDOW_UPDATE is provably not Chrome.

`curl_cffi`'s Chrome profiles emit the 15663105 increment. Stock `httpx[http2]` in [08](./08-vpn-pool.md) — no, that is not right; direct-mode `httpx[http2]` (see below) — does *not*, and that is one of the reasons direct mode is a fallback rather than a primary code path.

### Pseudo-header order

The HTTP/2 spec permits any ordering of the four pseudo-headers as long as they all precede regular headers. Browsers pick a stable ordering per implementation:

| Client              | Order       |
|---------------------|-------------|
| Chrome (all recent) | `m,a,s,p` — `:method, :authority, :scheme, :path` |
| Firefox             | `m,p,a,s` — `:method, :path, :authority, :scheme` |
| Safari              | `m,s,p,a` — `:method, :scheme, :path, :authority` |
| curl (default)      | `m,p,s,a` — `:method, :path, :scheme, :authority` |

Chrome's `m,a,s,p` is the unique giveaway. `curl_cffi`'s Chrome profiles emit that exact order. Every non-`curl_cffi` Python HTTP/2 client emits either curl's `m,p,s,a` or the h2 library's own `m,s,p,a`. This is a single-frame signal that a JA4H-collecting detector can key on.

### What our runs actually emit

We have not captured HTTP/2 SETTINGS off the wire from the scraper for verification — that would need `tshark` or a mitmproxy transparent replay. What we can assert from the library layer is:

- Under `curl_cffi` with `chrome146` (or the `chrome` alias) selected, SETTINGS, WINDOW_UPDATE, and pseudo-header order all match real Chrome 146 per the profile bundled in `curl_cffi` 0.15.
- Under `chrome131`, `chrome136`, `chrome142`, or `chrome146` we get five distinct signature sets (SETTINGS values are largely stable across Chrome majors, WINDOW_UPDATE is stable, pseudo-header order is stable — the JA4 hash of the *TLS* layer moves per version).

A future task to add a `--verify-fingerprint` mode that captures the first HTTP/2 preface from each proxy and diffs it against a bundled Chrome baseline is on the "would be nice" list but not currently scheduled. It would take real packet capture, not just source inspection, to close that gap.

## Client Hints

Client Hints (Sec-CH-UA family) are HTTP headers, not TLS. They live in [05 HTTP headers](./05-http-headers.md) for the content of what we send. This section covers the fingerprint-relevant properties that constrain the profile pool.

### Low-entropy hints are always on

Chrome sends three Sec-CH-UA headers by default on every request (navigation, XHR, cross-origin, preflight), without any server opt-in:

- `Sec-CH-UA: "Chromium";v="146", "Google Chrome";v="146", "Not/A)Brand";v="99"`
- `Sec-CH-UA-Mobile: ?0`
- `Sec-CH-UA-Platform: "macOS"`

These three are the "low-entropy hints" — they leak brand, mobile-ness, and platform family. Every real 2026 Chrome ships them. `HeaderRotator._build_headers` at `proxy_pool.py:114-116` generates them from the same `(_CHROME_VERSIONS, _OS_VARIANTS)` picks that build the User-Agent, so the Sec-CH-UA version and UA version are consistent under `HeaderRotator`. Under `curl_cffi` the shim strips them and the impersonation profile emits its own — again, consistent by construction because the profile's UA and Sec-CH-UA both come from the same real Chrome build.

The third brand slot in Sec-CH-UA is intentional decoy noise, called **GREASE** (Generate Random Extensions And Sustain Extensibility). Google shuffles the exact GREASE token string periodically to prevent WAFs from allowlisting on it. `HeaderRotator` uses a fixed `"Not/A)Brand";v="99"` — the token that shipped with Chrome around late 2023. Real 2026 Chrome uses a different GREASE format, and a WAF that keeps its list of "known-good" GREASE tokens fresh could theoretically flag a stale token. In practice HKLII does not do this. `curl_cffi`'s bundled profiles carry the GREASE that shipped with the corresponding real Chrome build, so the `chrome146` profile emits a 2026 GREASE.

### High-entropy hints must be opted into

Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, Sec-CH-UA-Model, Sec-CH-UA-Bitness, and Sec-CH-UA-Wow64 are the "high-entropy hints." Chrome does not send them by default. The server must first respond with an `Accept-CH: Sec-CH-UA-Platform-Version, ...` header on an earlier request, and only then does Chrome start sending them.

We do not send high-entropy hints. HKLII does not send `Accept-CH` in the response, so real Chrome would not send them either. If we sent them unprompted, that would itself be a signal ("client sending headers no server asked for" — real Chrome does not do that). This constraint applies both under `curl_cffi` (which does not send them because the profile does not) and under `HeaderRotator` (which does not include them in `_build_headers`).

## HTTP/3

`curl_cffi` 0.15.0 added HTTP/3 fingerprints for `chrome145` and `chrome146` — the QUIC ClientHello and the underlying UDP fingerprint that Chrome uses when it upgrades a connection to HTTP/3 via Alt-Svc.

We do not opt into HTTP/3. Three reasons:

1. HKLII does not advertise HTTP/3 via Alt-Svc — probes at 2026-07-04 saw HTTP/2 only ([01](./01-hklii-platform.md)). A client that unilaterally opens QUIC to a server that has not advertised it is anomalous.
2. Even if HKLII did advertise HTTP/3, VPN proxies over PIA OpenVPN (see [08](./08-vpn-pool.md)) proxy TCP, not UDP. The QUIC packets would either fail to route or leak around the VPN, depending on the client's socket options. Neither is acceptable.
3. There is no `impersonate="chrome146_h3"`-style opt-in switch in `curl_cffi`'s API; HTTP/3 is negotiated by the profile's ALPN preference and the server's Alt-Svc. As long as ALPN offers `h2` first, we stay on HTTP/2.

If HKLII ever ships HTTP/3, we would need to (a) switch VPN transport to something that carries UDP (WireGuard would work), (b) verify `curl_cffi`'s QUIC fingerprint matches, and (c) add a `--http3` toggle. None of that is on the current roadmap.

## HTTP/2 in direct mode

`hklii download` (the one-off URL fetch subcommand — see [11](./11-operations-runbook.md)) does not go through the proxy pool or `curl_cffi`. It uses `client.make_async_client` at `client.py:28-36`:

```python
def make_async_client(timeout: int = 30, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        proxy=proxy,
        trust_env=False,
        http2=True,
    )
```

`http2=True` is M-6 from the 2026-07-04 audit. Before M-6, `httpx.AsyncClient()` defaulted to HTTP/1.1, and `_BROWSER_HEADERS` hardcoded a Chrome 148 UA. That combination sent "Chrome 148" over HTTP/1.1 — a per-frame suspicion signal (no real Chrome speaks HTTP/1.1 to an HTTP/2-capable origin in 2026). M-6 fixed it by turning on HTTP/2 in the direct client. The dependency for `http2=True` to work is `httpx[socks,http2]>=0.28.1`, pinned in `pyproject.toml:11`.

Direct mode is **not** using `curl_cffi`. The HTTP/2 preface `httpx` produces here comes from the `h2` library, so the SETTINGS values, WINDOW_UPDATE increment, and pseudo-header order will all be `h2`'s defaults — recognizably not-Chrome to a JA4H collector. Direct mode is the fallback path for canary testing and CLI one-offs; it is not what the production scrape uses.

What `_BROWSER_HEADERS` covers at `client.py:13-25`:

```python
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en-GB;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}
```

Design intent: a fixed Chrome-148-macOS identity that gets past judiciary.hk's historical F5-WAF UA blocklist (the "python" substring block — see [02 Judiciary](./02-judiciary-platform.md)). The header set is intentionally minimal — no Sec-Fetch-*, no Accept-Encoding, no Referer — because direct mode is for pointed URL fetches where "look like a legitimate direct navigation" is enough. Sending a full Chrome header set from an httpx-h2 transport would just create more mismatch surface, not less.

## Profile freshness policy

curl_cffi's Chrome profile releases have historically lagged real Chrome by 2–6 months. The alias `"chrome"` (first entry in `_IMPERSONATE_PROFILES`) tracks whatever `curl_cffi`'s newest is at import time, so upgrading `curl_cffi` picks up the newest profile automatically — no code change needed.

The four pinned entries (`chrome146`, `chrome142`, `chrome136`, `chrome131`) exist to spread across the version window while still keeping all of them modern-enough to blend. The refresh cadence:

1. **On every `curl_cffi` upgrade**, verify the alias resolves as expected. Trivial check: `python -c "from curl_cffi.requests import AsyncSession; s = AsyncSession(impersonate='chrome'); print(s._impersonate)"`. That flushes out cases where a new `curl_cffi` release renames or removes profiles.
2. **When any pinned profile is more than a year stale** (real Chrome is ~15 releases ahead of it), drop it and bump. `chrome131` was Chrome late-2024; by mid-2026 it is roughly two years stale, still fine because Chrome's TLS extension list has moved only incrementally, but it is the next candidate to age out.
3. **Never pin below `chrome131`.** Everything older carries the `Not/A)Brand;v="99"` GREASE that flags on any 2026-aware Sec-CH-UA validator. `chrome131` is the boundary where GREASE started rotating.
4. **Do not add Safari or Edge.** Mixing browser families with Chrome's `sec-ch-ua` from `HeaderRotator` (see [05](./05-http-headers.md)) or with Chrome-shaped `_BROWSER_HEADERS` (in `client.py`) produces cross-layer inconsistency. If we ever want browser diversity beyond Chrome versions, the header layer has to become profile-aware first.

The rationale for the specific five-entry pool sits in [12 Decisions log](./12-decisions-log.md) § "Why `chrome146/142/136/131` (plus bare `\"chrome\"` alias) and not `chrome104/110/116`."

### Not currently monitored

Two freshness issues we know about but do not have monitoring for:

- **GREASE token rotation.** Google shuffles the GREASE token in `sec-ch-ua`'s third slot. If `curl_cffi`'s bundled profile falls behind Chrome's current GREASE, the `sec-ch-ua` header shipped by the impersonation would be stale. No alert wired up; would need real Chrome traffic captures to compare.
- **HTTP/2 SETTINGS drift.** Chrome has changed SETTINGS values before (e.g. bumping `MAX_HEADER_LIST_SIZE`). If real Chrome moves from `6:262144` to some other value, `curl_cffi`'s profile lags until upstream refreshes. Again — no alert; needs real captures.

Both would flip into "urgent" only if HKLII (or a mirror we scrape) turned up JA4H-based defenses. As of the 2026-07-04 canary, HKLII shows none of the header-order or SETTINGS-based signals that would suggest such defenses are live.
