# HTTP Headers Reference

Every request the scraper puts on the wire is assembled from three
independent sources: a per-proxy `HeaderRotator` that carries the
navigation-plus-Client-Hints skeleton, a pure function `referer_for(url)`
that fabricates a plausible SPA Referer, and — in the production hot
path — a `curl_cffi` impersonation profile that owns the TLS
fingerprint, HTTP/2 framing, and the exact byte-level ordering of a
subset of headers.

This chapter is the byte-level manifest. Every value, every version, and
every strip rule is anchored to a specific file:line so an operator can
audit a live capture against the code without guessing.

Related chapters:

- Why we send these headers at all when HKLII is not WAF-fronted:
  [Anti-Detection Strategy](./04-anti-detection-strategy.md).
- What `curl_cffi` owns underneath (and why we strip so many headers on
  the way in): [TLS / HTTP/2 Fingerprinting](./06-tls-http2-fingerprinting.md).
- Per-proxy cookie continuity and the warm-up GET that primes each
  session: [Cookies, Sessions, Warm-up](./07-cookies-sessions-warmup.md).

## Where headers come from (HeaderRotator + parser.referer_for + curl_cffi impersonate profile — division of labor)

Three layers decide what actually leaves the socket, in this order:

1. **`HeaderRotator` (`src/hklii_downloader/proxy_pool.py:96-133`)** —
   one instance per proxy, seeded from `random.Random(i + 1000)` at
   `proxy_pool.py:241` so each proxy owns a stable `(Chrome major,
   Chrome full, OS, platform)` tuple across restarts.
   `HeaderRotator._build_headers` runs once in `__init__` and caches the
   full navigation header dict; `generate(url)` returns a shallow copy
   and rewrites the Sec-Fetch quartet if the URL is an API endpoint.

2. **`parser.referer_for(url)`
   (`src/hklii_downloader/parser.py:40-73`)** — a pure function called
   on every request. Given a target URL it returns the SPA page URL
   that would plausibly have fired this XHR, based on the API
   endpoint's query string. `ProxyPool.get()` injects this as `Referer`
   on the request dict at `proxy_pool.py:345`; direct mode uses the
   same function through `_referer_for` imported at `proxy_pool.py:11`.

3. **`curl_cffi` impersonation profile inside `ImpersonateAsyncClient`
   (`src/hklii_downloader/impersonate_client.py:45-75`)** — the client
   is constructed with a random pick from `_IMPERSONATE_PROFILES`
   (`impersonate_client.py:21-23`) and `curl_cffi.AsyncSession` writes
   the TLS ClientHello, HTTP/2 SETTINGS, WINDOW_UPDATE, pseudo-header
   order, and the on-wire values of every header in
   `_FINGERPRINT_HEADERS` (`impersonate_client.py:28-42`). Any value the
   caller supplied for a fingerprint header is silently dropped at
   `impersonate_client.py:67-71` before the request is dispatched.

The consequence of layer 3 is that most of what `HeaderRotator` produces
never survives to the wire in production. It exists to keep the test
suite honest (the `httpx.MockTransport` path at `proxy_pool.py:250-255`
sees the whole dict), to feed direct mode (where `curl_cffi` is not in
the path), and to make the warm-up GET at `proxy_pool.py:292-304`
identical in shape to a subsequent API call. What actually leaves a
production socket is: `curl_cffi`'s baked-in Chrome header block, plus
the two headers `curl_cffi` does *not* claim ownership of — `Referer`
and any `Cookie` — plus anything else the caller passes via `kwargs`
that isn't in `_FINGERPRINT_HEADERS`.

The result is a two-layer contract:

| Header class                                | Source of truth                                 | Owner        |
|---------------------------------------------|-------------------------------------------------|--------------|
| `User-Agent`, `Accept`, `Accept-Language`, `Accept-Encoding` | `curl_cffi` impersonate profile                 | layer 3      |
| `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`        | `curl_cffi` impersonate profile                 | layer 3      |
| `sec-fetch-site`, `sec-fetch-mode`, `sec-fetch-dest`, `sec-fetch-user` | `curl_cffi` impersonate profile         | layer 3      |
| `Upgrade-Insecure-Requests`, `Connection`   | `curl_cffi` impersonate profile                 | layer 3      |
| `Referer`                                   | `parser.referer_for(url)`                       | layer 2      |
| Anything else in the request `kwargs`        | Caller / not stripped                           | passthrough  |

See [chapter 06](./06-tls-http2-fingerprinting.md) for what the layer-3
profile actually writes on the wire; this chapter documents what the
scraper *proposes* to send and how those proposals are shaped.

## Chrome UA pool (`_CHROME_VERSIONS`, 23 tuples)

`_CHROME_VERSIONS` at `proxy_pool.py:63-87` is the full pool that
`HeaderRotator._build_headers` picks from at
`proxy_pool.py:102`. Each entry is a `(major, full)` tuple. The major
number is inlined into `sec-ch-ua`; the full number is inlined into the
`User-Agent` template.

| Major | Full version   |
|-------|----------------|
| 126   | 126.0.6478.126 |
| 127   | 127.0.6533.72  |
| 128   | 128.0.6613.84  |
| 129   | 129.0.6668.58  |
| 130   | 130.0.6723.69  |
| 131   | 131.0.6778.86  |
| 132   | 132.0.6834.110 |
| 133   | 133.0.6943.98  |
| 134   | 134.0.6998.72  |
| 135   | 135.0.7049.84  |
| 136   | 136.0.7103.92  |
| 137   | 137.0.7151.68  |
| 138   | 138.0.7204.93  |
| 139   | 139.0.7258.54  |
| 140   | 140.0.7310.70  |
| 141   | 141.0.7356.83  |
| 142   | 142.0.7401.67  |
| 143   | 143.0.7450.81  |
| 144   | 144.0.7497.73  |
| 145   | 145.0.7538.62  |
| 146   | 146.0.7580.89  |
| 147   | 147.0.7623.56  |
| 148   | 148.0.7665.93  |

The pool floors at Chrome 126 and ceilings at 148. Chromes older than
126 (from before mid-2024) were removed to keep the UA-age percentile
inside the live-installed-base envelope — the mimicry audit found that
a UA pinned to a version from more than about a year ago flags a
UA-age heuristic (see [chapter 04](./04-anti-detection-strategy.md) for
that signal and [chapter 12](./12-decisions-log.md) for the version-cull
decision).

The pool matters far less than it looks in production: `curl_cffi` will
overwrite the `User-Agent` header with the string baked into the chosen
impersonation profile. The pool is what shows up in the warm-up GET
(where nothing strips it — the warm-up is `curl_cffi`'s but the header
dict is passed identically to any other request), in the test suite,
and in direct mode. But because the direct-mode client hardcodes Chrome
148 (see the direct-mode section below), the only place the *rotating*
pool actually shows up on the wire is inside the `_transport_factory`
test path.

## OS matrix (`_OS_VARIANTS`)

`_OS_VARIANTS` at `proxy_pool.py:89-93` pairs each OS's `User-Agent`
substring with the matching `sec-ch-ua-platform` literal. All three
strings are exactly what real Chrome 148 emits on those OSes:

| OS               | `User-Agent` OS substring             | `sec-ch-ua-platform` |
|------------------|---------------------------------------|----------------------|
| macOS            | `Macintosh; Intel Mac OS X 10_15_7`   | `"macOS"`            |
| Windows          | `Windows NT 10.0; Win64; x64`         | `"Windows"`          |
| Linux            | `X11; Linux x86_64`                   | `"Linux"`            |

`_build_headers` picks the OS string and platform value from the same
tuple (`proxy_pool.py:103`), so a `HeaderRotator` never emits a
Windows UA with `sec-ch-ua-platform: "macOS"` — that specific
consistency-check flags immediately (see the negative-space signals in
[chapter 04](./04-anti-detection-strategy.md)).

### Mac OS X 10_15_7 UA freeze

The macOS `User-Agent` substring is frozen at `10_15_7` and is not
bumped for newer macOS releases. Google froze the macOS version segment
of Chrome's `User-Agent` in Chrome 100 (2022) as a privacy measure — a
real user on macOS 15 (Sequoia) sends `Intel Mac OS X 10_15_7` from
Chrome, not `Intel Mac OS X 15_0`. Emitting a newer macOS version
number is a positive tell rather than a cover-up, so the string is
pinned. The equivalent Windows freeze (`Windows NT 10.0` regardless of
Windows 11) is captured by the same table entry.

Note: `sec-ch-ua-platform-version` — the high-entropy Client Hint that
*does* carry a real macOS/Windows version — is not sent (see the
Known gaps section at the end of this chapter).

## Navigation default header set

`_build_headers` at `proxy_pool.py:104-122` returns the following full
navigation dict. Values are byte-exact from the code:

```python
{
    "User-Agent":              f"Mozilla/5.0 ({os_string}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36",
    "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language":         "en-US,en-GB;q=0.9,en;q=0.8",
    "Accept-Encoding":         "gzip, deflate, br",
    "Connection":              "keep-alive",
    "sec-ch-ua":               f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile":        "?0",
    "sec-ch-ua-platform":      platform,
    "sec-fetch-site":          "same-origin",
    "sec-fetch-mode":          "navigate",
    "sec-fetch-dest":          "document",
    "sec-fetch-user":          "?1",
    "Upgrade-Insecure-Requests": "1",
}
```

Field-by-field:

- **`User-Agent`** (`proxy_pool.py:105-109`) — templated with the
  chosen OS string and Chrome full-version. This is the navigation
  shape that would go on a top-level page load.
- **`Accept`** (`proxy_pool.py:110`) — the byte-exact string Chrome
  148 sends on a same-origin navigation. Note that `image/avif` sits
  in front of `image/webp` (the Chrome-125+ preference order) and
  `application/signed-exchange;v=b3;q=0.7` is present at the tail.
- **`Accept-Language`** (`proxy_pool.py:111`) — `en-US,en-GB;q=0.9,en;q=0.8`.
  Three languages, en-US first, then en-GB at `q=0.9`, then generic `en` at
  `q=0.8`. This matches a Chrome install on a macOS/Windows machine
  whose language list is `English (US), English (UK), English`.
- **`Accept-Encoding`** (`proxy_pool.py:112`) — `gzip, deflate, br`.
  Note: `zstd` is not present. Chrome 125+ adds `zstd` at the tail;
  the pool's higher-versioned UAs are inconsistent with this
  `Accept-Encoding` string. This is a minor tell (see Known gaps at
  the end) and mostly moot because `curl_cffi` overwrites this header
  anyway. See [chapter 06](./06-tls-http2-fingerprinting.md) for what
  the impersonation profile actually sends.
- **`Connection`** (`proxy_pool.py:113`) — `keep-alive`. HTTP/2 in
  practice ignores it (HTTP/2 has no `Connection: keep-alive`
  semantics; the header is a Chrome-set legacy artifact carried by
  `curl_cffi` for the HTTP/1.1 fallback path).
- **`Upgrade-Insecure-Requests`** (`proxy_pool.py:121`) — `1`.
  Sent only on navigation shape; XHR shape removes it (see below).

The `HTTPS` empirical check on the wire, sent as five distinct probes
from four PIA exits, confirms HKLII does not compress even when
`Accept-Encoding` is offered (a `pair_gzip` probe with
`Accept-Encoding: gzip, deflate, br, zstd` returned an identical byte
count to the `pair_noenc` probe). See [chapter 03](./03-endpoint-reference.md)
for the endpoint-probe data table.

## sec-ch-ua string format (3-brand shape)

`sec-ch-ua` at `proxy_pool.py:114` is:

```
"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"
```

This is the 3-brand UA Client Hints shape Chrome ships. The first two
brands (`Chromium` and `Google Chrome`) share the major version; the
third brand slot is a GREASE token — a deliberately fake brand whose
exact string rotates in real Chrome to prevent detection stacks from
whitelisting a specific value. The scraper pins `Not/A)Brand` with
version `99`, which is one of the historical Chrome GREASE tokens; live
Chrome shuffles this slot across releases, so a static value is a
persistence tell over long spans. See the negative-space signals in
[chapter 04](./04-anti-detection-strategy.md).

The `Chromium` and `Google Chrome` versions must equal the `User-Agent`
major (both templated from the same `major` variable at
`proxy_pool.py:102`). A mismatch — `sec-ch-ua` claiming v146 while
`User-Agent` claims Chrome/148 — is a one-line log rule (Chrome keeps
UA and CH version in lockstep). Because both fields are drawn from the
same tuple in the same call, the scraper cannot generate a mismatched
pair from its own code — but a caller who passes `headers={"User-Agent":
...}` into `ProxyPool.get()` can create one via `httpx`'s header-merge
semantics. In production this cannot occur because `curl_cffi` strips
both fields before either can reach the wire.

## Sec-Fetch quartet — navigation shape

For the initial `HeaderRotator._build_headers` dict — the default —
`Sec-Fetch-*` matches a top-level document navigation from the address
bar or a same-origin `<a href>` click:

| Header                     | Value            | File anchor             |
|----------------------------|------------------|-------------------------|
| `sec-fetch-site`           | `same-origin`    | `proxy_pool.py:117`     |
| `sec-fetch-mode`           | `navigate`       | `proxy_pool.py:118`     |
| `sec-fetch-dest`           | `document`       | `proxy_pool.py:119`     |
| `sec-fetch-user`           | `?1`             | `proxy_pool.py:120`     |
| `Upgrade-Insecure-Requests`| `1`              | `proxy_pool.py:121`     |

`sec-fetch-user: ?1` is Chrome's marker that the navigation was
user-initiated (a click, address-bar type, or `Enter` in a form).
`Upgrade-Insecure-Requests: 1` signals the browser is willing to receive
`https` alternatives for `http` resources. Both are only sent by real
Chrome on navigation requests.

This shape is the *only* one that leaves the wire from the warm-up GET
at `proxy_pool.py:298-301`, whose target `_WARMUP_URL =
"https://www.hklii.hk/"` (`proxy_pool.py:197`) is a document, not an
API endpoint. See [chapter 07](./07-cookies-sessions-warmup.md) for the
warm-up mechanics.

## Sec-Fetch quartet — XHR shape (M-2)

`HeaderRotator.generate(url)` at `proxy_pool.py:124-133` mutates the
default dict when the URL contains `/api/`:

```python
def generate(self, url: str | None = None) -> dict[str, str]:
    headers = dict(self._headers)
    if url is not None and "/api/" in url:
        # XHR: Chrome sends mode:cors, dest:empty on fetch()/XHR to
        # same-origin JSON APIs, and never sec-fetch-user or UIR.
        headers["sec-fetch-mode"] = "cors"
        headers["sec-fetch-dest"] = "empty"
        headers.pop("sec-fetch-user", None)
        headers.pop("Upgrade-Insecure-Requests", None)
    return headers
```

The resulting XHR-shape quartet:

| Header           | Navigation value | XHR value | Note                    |
|------------------|------------------|-----------|-------------------------|
| `sec-fetch-site` | `same-origin`    | `same-origin` (unchanged) | The XHR is same-origin because it's from `www.hklii.hk` to `www.hklii.hk`. |
| `sec-fetch-mode` | `navigate`       | `cors`    | XHR/`fetch()` calls default to `cors` mode. |
| `sec-fetch-dest` | `document`       | `empty`   | JSON XHR destinations are the empty string, not `document`. |
| `sec-fetch-user` | `?1`             | *removed* | Only sent on user-initiated navigations. |
| `Upgrade-Insecure-Requests` | `1`  | *removed* | Only sent on navigations. |

This is the M-2 audit fix from the pre-production audit. Before M-2,
`HeaderRotator.generate` returned the navigation-shape dict for every
request including `/api/*`, and that produced the log-rule tell
"`sec-fetch-mode=navigate` on `/api/*` — no legitimate flow does this"
(see suspicion signal 6 in [chapter 04](./04-anti-detection-strategy.md)).
As above, `curl_cffi` normally strips all five headers in production; the
XHR shape reaches the wire only through the direct-mode path and the
test transport.

### HeaderRotator.generate(url) branch logic

The trigger is the substring `/api/` in the URL. It's substring-based
rather than path-based (no `urlparse`), so any URL that contains
`/api/` — including hypothetical future endpoints under `/api/v2/`
or the exact URLs `https://www.hklii.hk/api/getcasefiles?...` and
`https://www.hklii.hk/api/getjudgment?...` — takes the XHR branch.
`https://www.hklii.hk/` and `https://www.hklii.hk/en/cases/hkcfi/`
take the navigation branch. The warm-up URL is always the homepage
(navigation).

The dead `HeaderRotator.rotate()` method that existed pre-audit was
deleted as part of the M-7 audit fix — see
[chapter 12](./12-decisions-log.md) for that decision. The current
`HeaderRotator` exposes `generate(url)` and `referer_for(url)` only;
the latter is a thin forward to `_referer_for` at
`proxy_pool.py:135-136`.

## Referer derivation (M-1 `parser.referer_for`)

`referer_for(url)` at `parser.py:40-73` computes a plausible SPA
Referer from the target URL. The function is pure and deterministic —
given the same URL it always returns the same Referer.

| Target path                                    | Query params required   | Returned Referer                        | File anchor          |
|------------------------------------------------|-------------------------|-----------------------------------------|----------------------|
| `/api/getjudgment`                             | `lang`, `abbr`, `year`  | `https://www.hklii.hk/{lang}/cases/{abbr}/{year}/` | `parser.py:51-58` |
| `/api/getcasefiles`                            | `caseDb`, `lang`        | `https://www.hklii.hk/{lang}/cases/{caseDb}/`      | `parser.py:60-66` |
| `/{lang}/cases/{court}/{year}` or `/{lang}/cases/{court}/{year}/{n}` | (none — matched from path) | `https://www.hklii.hk/{lang}/cases/{court}/{year}/` | `parser.py:68-71` |
| Any other `www.hklii.hk` path                  | —                       | `https://www.hklii.hk/`                            | `parser.py:73`    |
| Non-`www.hklii.hk` host                        | —                       | `https://www.hklii.hk/`                            | `parser.py:47-49` |

`_CASE_PATH_PATTERN` at `parser.py:15` is the compiled regex used by
the third branch:

```python
_CASE_PATH_PATTERN = re.compile(r"^/(en|tc)/cases/([a-z]+)/(\d{4})(?:/\d+/?)?$")
```

The trailing `(?:/\d+/?)?` makes the case number optional, so both
`/en/cases/hkcfi/2026/` and `/en/cases/hkcfi/2026/3816` collapse to the
same year-listing Referer. This mirrors what Chrome does when a user
clicks a case tile from the year index (the Referer is the year-index
page, not the tile), and what happens when a user follows a bookmark to
a specific case (the Referer is the year-index if navigating from the
listing, or is absent for a fresh tab).

Concrete examples:

| Request URL                                                                    | Referer                                              |
|--------------------------------------------------------------------------------|------------------------------------------------------|
| `https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2026&num=3816`   | `https://www.hklii.hk/en/cases/hkcfi/2026/`          |
| `https://www.hklii.hk/api/getjudgment?lang=tc&abbr=hkca&year=2024&num=17`     | `https://www.hklii.hk/tc/cases/hkca/2024/`           |
| `https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10000&page=1` | `https://www.hklii.hk/en/cases/hkcfi/`       |
| `https://www.hklii.hk/api/getcasefiles?caseDb=hkca&lang=tc&itemsPerPage=10000&page=1`  | `https://www.hklii.hk/tc/cases/hkca/`        |
| `https://www.hklii.hk/` (warm-up target)                                       | `https://www.hklii.hk/`                              |
| `https://www.hklii.hk/en/cases/hkcfi/2026/3816`                                | `https://www.hklii.hk/en/cases/hkcfi/2026/`          |
| `https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2025/HCMP002265_2025.docx` | `https://www.hklii.hk/`                     |

The Judiciary-host fallback is deliberate: a real user viewing an HKLII
case page and clicking the "Download DOCX" link on the vetted judgment
page navigates from an HKLII page to a `legalref.judiciary.hk` URL, so
the Referer would be an HKLII URL. Falling back to the HKLII homepage
is a coarser approximation than the correct HKLII case URL would be,
but the current `doc_url` reads the URL directly from the getjudgment
JSON and does not carry the source HKLII case URL through
(see `client.py:126-129`). The `doc` fetch is only triggered by
`--allow-doc` and inside `_fetch_doc` in `scraper.py`; a follow-up to
tighten Judiciary Referer to the source case URL is discussed in
[chapter 12](./12-decisions-log.md).

Before the M-1 audit fix, every request went out with `Referer:
https://www.hklii.hk/` regardless of URL. That's suspicion signal 2 in
[chapter 04](./04-anti-detection-strategy.md): a one-line log rule
(`COUNT(DISTINCT referer)=1 AND MAX(referer)=homepage`) flags any
scraper-shaped session.

## How proxy mode wires Referer

`ProxyPool.get()` at `proxy_pool.py:344-345` overwrites the Referer on
every proxied request:

```python
req_headers = headers.generate(url)
req_headers["Referer"] = headers.referer_for(url)
```

There is no way for a caller to suppress or override the Referer in
proxy mode — the assignment is unconditional, and it happens *after*
`generate(url)` returns. A caller-supplied `Referer` would be
overwritten. This is deliberate: the whole point of the M-1 fix is that
the scraper cannot be caught emitting a hardcoded homepage Referer
because it is the only code path that sets Referer at all.

Direct mode at `proxy_pool.py:326-327` uses `setdefault`, not
overwrite:

```python
direct_headers = dict(kwargs.pop("headers", None) or {})
direct_headers.setdefault("Referer", _referer_for(url))
return await self._direct_client.get(url, headers=direct_headers, **kwargs)
```

Direct mode is only used by the CLI `download` subcommand for
targeted URL fetches (see [chapter 11](./11-operations-runbook.md)),
and it lets a caller override Referer — useful if a caller is manually
chaining requests. Bulk scraping is proxy mode, so this `setdefault`
does not come into play at production scale.

## Direct-mode `_BROWSER_HEADERS` in `client.py`

`client.py:14-25` hardcodes a Chrome-148-on-macOS navigation header
block that is completely separate from `HeaderRotator`:

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

`make_async_client` at `client.py:28-36` builds an `httpx.AsyncClient`
with these headers plus `http2=True`, `follow_redirects=True`,
`timeout=30`, `trust_env=False`. This client is *not* wired through
`curl_cffi`, so the impersonation profile does not exist for it — what
you see in `_BROWSER_HEADERS` is what leaves the socket.

Two things to notice:

1. The pinned Chrome version — `148.0.0.0` in the UA, `v="148"` in
   `sec-ch-ua` — is fixed, not rotating. Every direct-mode fetch from
   the same install looks identical. This is fine because direct mode
   is only used by `hklii download` for a small handful of URLs
   supplied by the operator (see [chapter 11](./11-operations-runbook.md));
   it is not used by `hklii scrape`.
2. The comment at `client.py:13` says "The judiciary.hk F5 WAF blocks
   any UA containing 'python' (silent connection hang)". This is
   pre-2026 lore. The 2026-07-04 Judiciary probe found no F5/CF/Akamai
   challenge markers — the `python` UA block appears to have been
   retired or was misdiagnosed. The hardcoded Chrome UA is retained as
   belt-and-suspenders (a Chrome UA is never wrong for `judiciary.hk`,
   whereas `python-requests/2.x` would fail if the WAF were ever
   re-enabled). See [chapter 02](./02-judiciary-platform.md) for the
   Judiciary origin probe results, and [chapter 12](./12-decisions-log.md)
   for the decision to keep the hardcoded UA.

`make_async_client` is imported by the `download` subcommand in
`cli.py`; the bulk `scrape`/`enrich`/`verify` subcommands never touch
it. See [chapter 11](./11-operations-runbook.md) for who calls what.

## `curl_cffi` header stripping in `ImpersonateAsyncClient`

`ImpersonateAsyncClient.get()` at `impersonate_client.py:66-75` filters
out every header the impersonation profile owns before dispatching:

```python
async def get(self, url: str, headers: dict | None = None, **kwargs: Any):
    if headers:
        headers = {
            k: v for k, v in headers.items()
            if k.lower() not in _FINGERPRINT_HEADERS
        }
    try:
        return await self._session.get(url, headers=headers, **kwargs)
    except Exception as exc:
        raise self._translate(exc) from exc
```

`_FINGERPRINT_HEADERS` at `impersonate_client.py:28-42` is a frozen set
of lower-cased header names:

| Stripped header               | Why it's stripped                                                    |
|-------------------------------|----------------------------------------------------------------------|
| `user-agent`                  | Chrome's UA is baked into the impersonation profile.                 |
| `accept`                      | Real Chrome's `Accept` string depends on the request destination; the profile emits the right one. |
| `accept-language`             | Profile owns this; caller values would desync from the UA locale.    |
| `accept-encoding`             | Profile emits Chrome's actual encoding list (which includes `zstd` for Chrome 125+). |
| `sec-ch-ua`                   | Profile owns Client Hints; caller value would desync from the UA major. |
| `sec-ch-ua-mobile`            | As above.                                                            |
| `sec-ch-ua-platform`          | As above.                                                            |
| `sec-fetch-site`              | Real Chrome computes this from the request context; the profile matches. |
| `sec-fetch-mode`              | As above.                                                            |
| `sec-fetch-dest`              | As above.                                                            |
| `sec-fetch-user`              | Only Chrome-set on navigations; profile handles.                      |
| `upgrade-insecure-requests`   | Only Chrome-set on navigations; profile handles.                      |
| `connection`                  | HTTP/2 does not use it; HTTP/1.1 profile handles the value.          |

The comparison is case-insensitive because `curl_cffi` normalizes to
lower-case internally (HTTP/2 mandates lower-case header names anyway
— see [chapter 06](./06-tls-http2-fingerprinting.md)). Any header
passed by the caller whose lower-cased name is *not* in this set is
forwarded unchanged. In practice the only headers the scraper passes
in that aren't stripped are `Referer` (from `parser.referer_for`) and
occasionally `Cookie` (managed by `curl_cffi.AsyncSession` internally
for cookie continuity — see [chapter 07](./07-cookies-sessions-warmup.md)).

The strip rule at `impersonate_client.py:70` uses `k.lower()`, so any
casing works: `HeaderRotator`'s `User-Agent`, `Accept`, `Connection`
plus its lower-cased `sec-ch-ua`, `sec-fetch-*` are all stripped
correctly. Callers do not need to worry about capitalization.

The set does *not* include `Referer`, `Cookie`, `Origin`, or the
`Priority` header. `Referer` is the one header the scraper deliberately
sets and forwards; `Cookie` is managed inside `curl_cffi.AsyncSession`;
`Origin` and `Priority` are not sent by the scraper today (see Known
gaps below).

`_translate` at `impersonate_client.py:80-90` maps `curl_cffi`
exceptions to the `httpx` hierarchy so the retry logic in `scraper.py`
and `enumerator.py` works unchanged. See
[chapter 06](./06-tls-http2-fingerprinting.md) for what the
impersonation profiles actually put on the wire once the headers reach
`curl_cffi`.

## Known gaps flagged in audit

Two header-layer items were surfaced in the pre-production audit and
deferred:

### The `Priority` header is not implemented

Chrome 108+ sends the `Priority` header (RFC 9218) on every request
with an urgency plus `i` for incremental:

- Document navigation: `Priority: u=0, i`
- XHR/fetch: `Priority: u=1, i`
- CSS/image subresources: `Priority: u=3, i`

The scraper sends no `Priority` header at all. Real Chrome 108+ *does*
send it on every request. A WAF checking for its presence gets a
negative-space hit: "UA claims Chrome 148, no `Priority` header —
inconsistent". Adding it correctly requires branching on request type
(document vs XHR), which mirrors the branch that already exists in
`HeaderRotator.generate` for `Sec-Fetch-*`. The gap is deferred because
(a) `curl_cffi`'s newer impersonation profiles (`chrome142`,
`chrome145`, `chrome146`) may already send it themselves — this has
not been verified from a wire capture — and (b) HKLII does not
currently seem to key on it (the endpoint-probe data in
[chapter 03](./03-endpoint-reference.md) confirms no rate-limit or
`Priority`-hint behavior). See [chapter 12](./12-decisions-log.md) for
the deferral rationale.

### High-entropy Client Hints are not opted into

Chrome exposes low-entropy UA Client Hints (`sec-ch-ua`, `sec-ch-ua-mobile`,
`sec-ch-ua-platform`) on every request by default. High-entropy hints
(`sec-ch-ua-full-version-list`, `sec-ch-ua-platform-version`,
`sec-ch-ua-arch`, `sec-ch-ua-model`, `sec-ch-ua-bitness`, `sec-ch-ua-wow64`)
are only sent after the server responds with `Accept-CH:` listing which
ones it wants — and then Chrome sends them on every subsequent request
in that origin.

The scraper does not track `Accept-CH` responses and does not send any
high-entropy Client Hint. HKLII does not appear to emit `Accept-CH` in
its response headers (the endpoint probe in
[chapter 03](./03-endpoint-reference.md) captured the full response
header set — no `Accept-CH`), so an honest Chrome would also not send
the high-entropy hints against `www.hklii.hk`. This means the gap does
not currently produce a detectable tell for HKLII. It *would* produce a
tell if the scraper were pointed at a site that requests high-entropy
hints, so a future generalization of the client should track
`Accept-CH` per origin and echo the requested hints back on the next
request. See [chapter 12](./12-decisions-log.md) for why this was left
out.

Other minor items covered in adjacent chapters:

- `Accept-Encoding` in the `HeaderRotator` dict is missing `zstd`. The
  impersonation profile overwrites this in production, so it does not
  matter for the wire. See [chapter 06](./06-tls-http2-fingerprinting.md).
- The scraper does not send `Origin` on cross-origin requests. In
  practice every request is same-origin to `www.hklii.hk`, so this is
  moot — but a real Chrome sends `Origin` on `POST`/`fetch()` requests
  even same-origin. The scraper does not use `POST` today. See
  [chapter 03](./03-endpoint-reference.md) for the endpoint list.
- The `Chrome` GREASE token (`Not/A)Brand`, `v="99"`) is pinned rather
  than rotating. Chrome rotates its GREASE brand across releases. A
  static token flags on long-term persistence heuristics. See
  [chapter 04](./04-anti-detection-strategy.md) suspicion signal 10
  and 12 for detection framing, and
  [chapter 12](./12-decisions-log.md) for the deferral rationale.
