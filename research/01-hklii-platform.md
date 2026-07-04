# HKLII Platform

This chapter establishes what HKLII is, how the site is built and served, what protections it does (and doesn't) have, and the shape of the corpus we're pulling from. It is the platform-side anchor for every other chapter in this manual: the scraper's design decisions (curl_cffi impersonation, per-proxy warm-up, jittered throttling) only make sense against the concrete origin baseline captured here.

The baseline snapshot in this chapter was taken on **2026-07-04** from four PIA VPN exits (Singapore, Malaysia, Taiwan, Hong Kong). All curl outputs, header dumps, and body samples are on disk under `scratchpad/probes/` and cited inline.

## What HKLII is

HKLII — the **Hong Kong Legal Information Institute** — is a free-access legal database run out of the **Faculty of Law, University of Hong Kong**. It is the local member of the **Free Access to Law Movement** (WorldLII, AustLII, BAILII, CanLII, LII Cornell). It publishes Hong Kong court judgments, legislation, treaties, law reform reports, and secondary materials. For this project we consume only the judgments corpus at `https://www.hklii.hk/`.

The important architectural facts, from the scraper's point of view:

- **HKLII is a Vue.js single-page application.** The homepage returns a nearly empty HTML shell; every case listing, judgment body, and metadata block is fetched by client-side JavaScript from a JSON API at `https://www.hklii.hk/api/*`. There is no server-side rendering of judgment content into the initial HTML response.
- **Because the SPA fetches JSON, the scraper can skip the browser entirely.** We hit the API directly and parse the same JSON the SPA parses — no Playwright, no headless Chromium, no DOM. See [Endpoint reference](./03-endpoint-reference.md) for the wire format.
- **It is a small academic project, not a commercial platform.** Staffing is on the order of two people at HKU Law. There is no ops team on call, no CDN contract, no WAF vendor. That posture is directly visible in the response headers described below.
- **AI features live on a separate host** (`https://ai.hklii.hk/case-summary/`). They are not part of the judgments corpus and are ignored by this scraper.

## Server stack

Every probe of `https://www.hklii.hk/api/*` on 2026-07-04 returned the following stack signature:

| Layer | Value |
|---|---|
| Application server | `server: gunicorn` |
| Framework (inferred) | Django (based on error-page shape and middleware duplication — see below) |
| Wire protocol | HTTP/2, negotiated by TLS ALPN (`h2`) |
| TLS version | TLSv1.3 |
| Cipher | `AEAD-CHACHA20-POLY1305-SHA256` |
| Certificate issuer | DigiCert (RapidSSL TLS RSA CA G1) |

The trace of the negotiated TLS session is preserved in `scratchpad/trace_s10.txt:38,45`.

A representative full response-header block for `GET /api/getcasefiles?...` (page 1, `itemsPerPage=1000`):

```
HTTP/2 200
date: Fri, 04 Jul 2026 05:12:47 GMT
server: gunicorn
content-type: application/json
content-length: 234043
allow: GET, HEAD, OPTIONS
vary: Cookie, origin
x-frame-options: SAMEORIGIN
x-content-type-options: nosniff
content-security-policy: default-src * data: mediastream: blob: filesystem: about: ws: wss: 'unsafe-eval' 'wasm-unsafe-eval' 'unsafe-inline'; ...
referrer-policy: same-origin
cross-origin-opener-policy: same-origin
x-robots-tag: noindex
x-frame-options: ALLOWALL
x-content-type-options: nosniff
```

Full dump: `scratchpad/hdr_s1000.txt`.

Two things to notice up front:

1. The `server: gunicorn` line is **not proxied through anything**. There is no CDN header set on top of it (no `cf-ray`, no `x-served-by`, no `via`, no `age`, no `x-cache*`). What answered your request is a Python gunicorn worker directly terminating TLS at the edge, most likely behind a thin Apache/nginx reverse proxy that does not rewrite headers.
2. Two response headers appear **twice** in the same response (`x-frame-options`, `x-content-type-options`). That duplication is the strongest empirical fingerprint we have of the origin — see the "Duplicate-middleware fingerprint" section.

Our scraper uses HTTP/2 for API calls both in proxied mode (via `curl_cffi` — see [TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md)) and in direct mode via `httpx`:

- `src/hklii_downloader/client.py:28-36` — `make_async_client` passes `http2=True` to `httpx.AsyncClient`.
- `src/hklii_downloader/impersonate_client.py:52-60` — the impersonation wrapper delegates to `curl_cffi.AsyncSession`, which negotiates HTTP/2 by default under Chrome profiles.

## No CDN, no WAF, no rate-limit hints (bare Python origin baseline 2026-07-04)

The HKLII API responded to every probe as a **plain Python origin with no CDN, no WAF, and no rate-limit metadata**. Concretely, the response headers were missing every signal that a modern edge would set:

| Would indicate | Header we probed for | Present? |
|---|---|---|
| Cloudflare | `cf-ray`, `cf-cache-status`, `server: cloudflare` | No |
| Fastly | `x-served-by`, `x-cache`, `x-timer` | No |
| Akamai | `x-akamai-request-id`, `akamai-*` | No |
| Google Cloud CDN | `via: 1.1 google` | No |
| Nginx-layer rate limiting | `X-RateLimit-Limit`, `X-RateLimit-Remaining`, RFC 9331 `RateLimit-*` | No |
| Retry hint | `Retry-After` | No |
| Server-side rate advertisement of any kind | anything nonstandard `x-*rate*`, `x-quota-*` | No |

Evidence: `scratchpad/endpointProbe.json:13` (finding c) and the raw `scratchpad/hdr_s{10,50,100,1000}.txt` files. All 4 pageSize probes, all 14 court probes, and 10 `getjudgment` probes returned zero rate-limit-related headers.

### What this means for the scraper

- We **cannot** rely on `Retry-After` to pace ourselves. When a 429 or 503 arrives (rare — see below) there is nothing to read back.
- We **must** implement our own pacing. That's what `RequestThrottler` at `src/hklii_downloader/proxy_pool.py:32-60` does: the exact base delay, burst-and-gap structure, and long-pause probabilities are documented as the canonical source in [09 Scraper architecture](./09-scraper-architecture.md) § "RequestThrottler formula".
- Because there is no CDN in the path, TLS fingerprint blocks and JA4/JA4H WAF rules are **not currently** what would stop us. That's why the F5-WAF myth (below) is a myth. But we still ship curl_cffi as belt-and-suspenders in case that changes — see [Anti-detection strategy](./04-anti-detection-strategy.md).

### Rate-limit probes (empirical)

Four sequential probes 3.5 s apart from four distinct PIA exit IPs (`localhost:8888..8891`) all returned HTTP 200 with no rate-limit push-back. Evidence: `scratchpad/probe.sh:45-51` and the metrics files. Reproducing this pattern is a five-second test any time you're wondering if HKLII has flipped:

```bash
for port in 8888 8889 8890 8891; do
  curl --http2 -x "http://127.0.0.1:$port" \
    -w "%{http_code} %{time_total}s\n" -o /dev/null -s \
    "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10&page=1"
  sleep 3.5
done
```

If four sequential calls from four VPN exits all return 200, HKLII is still in the same posture. If any return 4xx or 5xx, or if new headers appear, the platform has changed and this chapter needs updating.

## Duplicate-middleware fingerprint (x-frame-options, x-content-type-options each twice)

The single strongest empirical evidence that HKLII is bare gunicorn/Django with no downstream normalization is that **two headers appear twice in every response**:

```
x-frame-options: SAMEORIGIN
x-content-type-options: nosniff
...
x-frame-options: ALLOWALL
x-content-type-options: nosniff
```

Line references in the raw dump: `scratchpad/hdr_s1000.txt:6,7,14,17`.

The cause is two Django middleware layers each stamping the same header without checking whether it's already set:

1. Django's built-in `django.middleware.clickjacking.XFrameOptionsMiddleware` (default `SAMEORIGIN`).
2. An application-level middleware that overrides to `ALLOWALL` — likely so the SPA can be embedded in academic frames.

Both fire; neither removes the other's header; and no downstream layer collapses duplicates. **A CDN would collapse them.** Fastly, Cloudflare, Akamai, CloudFront, and Google Cloud CDN all deduplicate identical-value response headers and typically dedupe conflicting-value ones as well. That we still see two conflicting values proves nothing sits between gunicorn and the wire.

The Judiciary origin (`legalref.judiciary.hk`) shows the **same** duplicate-x-frame-options tell, which is why we conclude both platforms are running the same Django stack from the same shop. See [Judiciary platform](./02-judiciary-platform.md).

### Downstream consequence

The scraper does not read `x-frame-options` — it's a browser-side directive. But when triaging "is HKLII still the same origin we designed for?" the duplicate-header presence is the fastest single-request check:

```bash
curl --http2 -sI "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1" \
  | grep -c -i "^x-frame-options:"
# Expected: 2
```

If that returns `1`, a CDN or WAF has been inserted since 2026-07-04 and the whole anti-detection posture needs re-audit.

## The no-op CSP

The Content-Security-Policy header returned by HKLII is a full no-op:

Exact bytes as returned on the wire, one directive per line:

```
content-security-policy:
  default-src * data: mediastream: blob: filesystem: about: ws: wss:
    'unsafe-eval' 'wasm-unsafe-eval' 'unsafe-inline';
  script-src * data: blob: 'unsafe-inline' 'unsafe-eval';
  script-src-elem * data: blob: 'unsafe-inline' 'unsafe-eval';
  connect-src * data: blob: 'unsafe-inline';
  img-src * data: blob: 'unsafe-inline';
  media-src * data: blob: 'unsafe-inline';
  frame-src * data: blob:;
  style-src * data: blob: 'unsafe-inline';
  font-src * data: blob: 'unsafe-inline';
  frame-ancestors * data: blob:;
```

Raw form on disk: `scratchpad/hdr_s1000.txt:8` (this reproduces those bytes verbatim, only the whitespace/line-breaks are reformatted for readability — the actual header is a single line of semicolon-separated directives).

Every directive uses the wildcard `*`. Every directive except `frame-src` adds `'unsafe-inline'`; `default-src`/`script-src`/`script-src-elem` also add `'unsafe-eval'`. `frame-ancestors * data: blob:;` disables framing protection. The header exists but forbids nothing.

This is not our problem to solve; it's a signal about the origin's operational maturity. A team that would set a proper CSP would also, plausibly, have rate-limit headers, have a CDN, and have a WAF. HKLII does not. That is consistent with the small-academic-project assessment.

## Cache-hint absences (no ETag, no Last-Modified, no Cache-Control, no Age)

HKLII does not send **any** HTTP cache hints on API responses:

| Header | Present on any `/api/*` probe? |
|---|---|
| `ETag` | No |
| `Last-Modified` | No |
| `Cache-Control` | No |
| `Age` | No |
| `Accept-Ranges` | No |
| `Expires` | No |

Evidence: full `scratchpad/hdr_s{10,50,100,1000}.txt` dumps contain none of these lines. `scratchpad/endpointProbe.json:13` (finding c) confirms.

### Consequences for the scraper

1. **Conditional GET is impossible.** We cannot send `If-None-Match` or `If-Modified-Since` to re-fetch only when a case has changed. Every re-download is a full re-download.
2. **Range resume is impossible.** No `Accept-Ranges: bytes`, so partial responses to a byte-range request are not offered. This is not a problem for JSON API calls (they're small), but it does contrast with the Judiciary origin (`legalref.judiciary.hk`), which DOES send `ETag`, `Last-Modified`, and `Accept-Ranges: bytes` on `.docx` responses. See [Judiciary platform](./02-judiciary-platform.md) for the docx-fetch semantics.
3. **The freshness-skip logic is client-side.** `BulkScraper` handles enumeration freshness via the `last_enumeration_ts` column and `--enum-max-age HOURS` flag rather than HTTP cache headers. See `src/hklii_downloader/scraper.py:116-124`.

The response body does carry a `date` field per judgment (ISO-8601, always `T00:00:00+08:00`), and `getmetacase` exposes a corpus-level `timestamp`. Those are the only "how fresh is this data?" signals the API offers.

## Compression behavior (server sends no gzip/br/zstd even when Accept-Encoding is offered)

We confirmed empirically with a paired probe: HKLII does not compress API responses **even when the client asks for compression**.

### Paired probe

Two identical `GET /api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1` requests were sent, differing only in `Accept-Encoding`:

| Probe | `Accept-Encoding` sent | Response `Content-Encoding` | Body bytes |
|---|---|---|---|
| `pair_noenc` | *(none)* | *(absent)* | 357 |
| `pair_gzip` | `gzip, deflate, br, zstd` | *(absent)* | 357 |

Raw metrics: `scratchpad/probes/pair_noenc.metrics:1` and `scratchpad/probes/pair_gzip.metrics:1`.

Body sizes are identical to the byte. The server ignores `Accept-Encoding` entirely. No `Content-Encoding` header is set in either response.

### Consequence

- There is **no compression win available** by advertising `Accept-Encoding`. This was a Tier B fix proposed pre-audit (M-5); the empirical probe retires it. The scraper does still send `Accept-Encoding: gzip, deflate, br` in the `HeaderRotator` navigation header set at `src/hklii_downloader/proxy_pool.py:112` — but only because a real Chrome would send that header and its **absence** is itself a suspicion signal (see [Anti-detection strategy](./04-anti-detection-strategy.md), signal 12). The header buys us fingerprint match, not bandwidth.
- Wire-size estimates for the enumeration and download passes must assume **full 234 B/row** for `getcasefiles` and full JSON size for `getjudgment`. No compression fudge factor applies.

### Byte-per-row linear scaling

For the enumeration endpoint at four pageSize probes:

| itemsPerPage | Body bytes | Bytes/row |
|---|---|---|
| 10 | 2354 | 235 |
| 50 | 11697 | 233 |
| 100 | 23429 | 234 |
| 1000 | 234043 | 234 |

Evidence: `scratchpad/metrics_s{10,50,100,1000}.txt` (`SIZE_DOWNLOAD` field). Fixed envelope wrapper is ~30 B amortized across page size. The 234 B/row constant is the number cited by [Decisions log](./12-decisions-log.md) as the basis of the `itemsPerPage=10_000` choice.

## Corpus scale (13 slug-count sum 118,188 vs homepage counter 122,460)

HKLII's homepage prominently displays a case-count number. On 2026-07-01 it read **122,460**. On 2026-07-04 we probed `getcasefiles` for every documented court slug from a VPN pool and got the following per-slug totals:

| Court slug | totalfiles (2026-07-04) | Notes |
|---|---|---|
| `hkcfi` | 64,226 | Court of First Instance |
| `hkca` | 29,911 | Court of Appeal |
| `hkdc` | 18,118 | District Court |
| `hkcfa` | 2,143 | Court of Final Appeal |
| `hkldt` | 1,917 | Lands Tribunal |
| `hkfc` | 1,789 | Family Court |
| `hkct` | 42 | Competition Tribunal |
| `hkmagc` | 24 | Magistrates' Court |
| `hkcrc` | 11 | Coroner's Court |
| `hklat` | 5 | Labour Tribunal |
| `hkoat` | 2 | Obscene Articles Tribunal |
| `hksct` | 0 | Small Claims Tribunal (zero rows) |
| `ukpc` | 0 | UK Privy Council HK cases (zero rows) |
| **Sum** | **118,188** | |
| Homepage counter | 122,460 | |
| **Delta** | **4,272** | ~3.5 % |

Evidence for per-slug totals: `scratchpad/probes/court_*.body` (each contains a `"totalfiles":N` field extractable via `grep`); collated in [Endpoint reference](./03-endpoint-reference.md) and the memory note `hklii-court-databases.md:26-44`.

### Arithmetic delta explanation

The ~4,272-row delta between the homepage counter and the API-summed corpus is **not** missing court slugs. Prior recon (session `bd7d19`) probed seven invented slugs (`hklndtri`, `hklab`, `hkcompet`, `hkcoroners`, `hkstsc`, `hkfamc`, `hkmc`) that all returned HTTP 500 — those slugs do not exist. The 13 slugs above are the complete set.

The most plausible explanations for the delta are:

1. **Press-summary rows in the homepage counter but not in `getcasefiles`.** Press summaries (Judiciary-authored abstracts of notable judgments) may be counted separately in the homepage total. Our enumeration deliberately skips them at enumeration time — they are fetched via [enrichment](./09-scraper-architecture.md) instead.
2. **Bilingual duplicate rows.** Some cases have both an English and a Traditional Chinese version. If the homepage counter double-counts translations while `getcasefiles` returns one row per unique `(court, year, number)`, that alone can plausibly account for a few thousand rows across a 120k-row corpus.
3. **Counter staleness.** The homepage counter update cadence is unknown; it may reflect a different snapshot than the API.

We do not need to resolve the delta to run production. The 4 "target courts" (`hkcfi`, `hkca`, `hkdc`, `hkcfa`) together sum to **114,398 judgments** — 97% of the confirmed corpus. See [Endpoint reference](./03-endpoint-reference.md) for the per-court table and [Operations runbook](./11-operations-runbook.md) for how court selection interacts with production flags.

### Response consistency across the probe window

Four probes at four different `itemsPerPage` values (10, 50, 100, 1000) all returned:

- Identical `totalfiles`: **64,226** for `hkcfi`.
- Identical first record: `[2026] HKCFI 3816` dated `2026-07-03T00:00:00+08:00`.

Evidence: `scratchpad/body_s{10,50,100,1000}.json`. This proves all four probes hit the same live snapshot with zero data drift over the ~90 s window, and that the server does not silently downgrade `itemsPerPage` (it honors 10, 50, 100, and 1000 exactly). See [Decisions log](./12-decisions-log.md) for why we ended up choosing `itemsPerPage=10_000`.

### Not all 14 slugs accept `itemsPerPage=10_000` today

A follow-up probe found that **7 of 14** court slugs returned HTTP 500 (Django default error page, 145 bytes, `text/html`) when queried with `itemsPerPage=10000`: `hkcompet`, `hkcoroners`, `hkfamc`, `hklab`, `hklndtri`, `hkmc`, `hkstsc`. Evidence: `scratchpad/probes/court_hkcompet.hdr:5,11,14`.

Six of those seven are the invented slugs from the earlier session — they legitimately do not exist and returning 500 is a defensible-if-unfortunate Django behavior. The seventh (`hkstsc`, Small Claims Tribunal) returned `totalfiles=0` in the "confirmed 13 slugs" probe but 500 at `itemsPerPage=10000`, which suggests the 500 fires on **an empty result set at a large `itemsPerPage`**, not on the slug itself. Either way, the four target courts (`hkcfi`, `hkca`, `hkdc`, `hkcfa`) all responded 200 across the full pageSize range and are not affected in production.

## Historical F5-WAF myth vs current empirical status

Prior sessions of this project carried a folk belief that HKLII (or Judiciary, or both) was protected by an F5 BIG-IP WAF that dropped any request whose User-Agent contained "python", and that silent connection hangs were the WAF's signature block. That belief is preserved in a stale comment at `src/hklii_downloader/client.py:13`:

```python
# The judiciary.hk F5 WAF blocks any UA containing "python" (silent connection hang).
```

The 2026-07-04 probe suite finds **no evidence of any F5 or other WAF on HKLII**:

- No F5-specific response headers (`x-frame-options` duplication is a Django tell, not an F5 tell; F5 typically strips `server` or replaces with `BigIP`).
- No JS challenge on any probe (no "Just a moment", no "verify you are human", no `cf-mitigated`).
- No connection resets, no silent hangs, no 502/503 challenge pages.
- `curl 8.7.1` with default headers (`User-Agent: curl/8.7.1`, `Accept: */*`, no cookies, no Referer) received a full HTTP 200 with correct JSON on every attempted call. Evidence: `scratchpad/trace_s10.txt:54-58`.

The comment in `client.py` is a leftover from an era when a User-Agent regex block was assumed to be in play. It is stale and should be reworded (Tier C follow-up; not currently done). The **behavior** the scraper implements — hardcoding a Chrome UA on the direct-mode client — is still correct because:

- A `python-requests/` or `curl/` UA is a Tier 1 sysadmin heuristic that any operator could add at any time.
- The `HeaderRotator` at `src/hklii_downloader/proxy_pool.py:101-122` sends a real Chrome UA regardless.
- The impersonation wrapper at `src/hklii_downloader/impersonate_client.py:28-42` strips the UA and lets `curl_cffi` set a matching browser UA at the TLS/HTTP-2 layer.

Judiciary shows the same posture (see [Judiciary platform](./02-judiciary-platform.md)) — no F5 markers today. The pre-2026 F5 story was not backed by observed data on 2026-07-04.

### Why we still fingerprint-mimic

Even though HKLII is bare gunicorn and there is no WAF to defeat right now, we ship a full anti-detection layer. The rationale is covered in [Anti-detection strategy](./04-anti-detection-strategy.md) in detail; the short form is:

1. **Detection can flip at any time.** A small academic team can add nginx `limit_req_zone` in an hour. Being caught unprepared during a 20–40 hour production run is more costly than the fixed engineering cost of already having curl_cffi + jitter + warm-up in place.
2. **Even without a WAF, sysadmin-heuristic Tier 1 detection is real.** A `python-requests/` UA on 118k requests from one IP over 20 hours would trip any competent operator's log-analysis rules.
3. **HKLII shares data with Judiciary**, whose posture we care about for `.docx` fetches. The fingerprint we present has to look plausible on both.

Signal-by-signal defense mapping (which layer defends against what) is documented in [Anti-detection strategy](./04-anti-detection-strategy.md) sections "12 suspicion signals catalog" and "Signal-to-defense map."

## Cross-references

- **Per-court corpus counts, `getcasefiles` wire format, `getjudgment` shape, and error behavior** — [Endpoint reference](./03-endpoint-reference.md).
- **Why we still fingerprint-mimic despite the bare origin** — [Anti-detection strategy](./04-anti-detection-strategy.md).
- **The sibling Judiciary origin (docx source, ETag caching, docx URL pattern)** — [Judiciary platform](./02-judiciary-platform.md).
- **Concrete header composition sent per request** — [HTTP headers](./05-http-headers.md).
- **TLS/HTTP-2 fingerprinting mechanics and the `curl_cffi` profile pool** — [TLS and HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md).
- **Why `itemsPerPage=10_000`, why 4 courts, and every other architectural choice** — [Decisions log](./12-decisions-log.md).

## Reproducing this chapter's baseline

Anyone re-verifying HKLII's posture should run the following five probes and compare against the values recorded above. If any diverge, this chapter is stale.

```bash
# 1. Server + duplicate-header fingerprint (should see 2 x-frame-options, 2 x-content-type-options)
curl --http2 -sI "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1" \
  | grep -E "^(server|x-frame-options|x-content-type-options|content-encoding|etag|last-modified|cache-control|retry-after|x-ratelimit):" -i

# 2. Compression (should return 357 both, no Content-Encoding either time)
curl --http2 -s -o /dev/null -w "no-enc: %{size_download}\n" \
  "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1"
curl --http2 -s -o /dev/null -w "gzip: %{size_download}\n" \
  -H "Accept-Encoding: gzip, deflate, br, zstd" \
  "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1"

# 3. Corpus scale: expect totalfiles=64226 for hkcfi (may drift up over time)
curl --http2 -s "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1" \
  | python3 -c 'import json,sys; print("totalfiles:", json.load(sys.stdin)["totalfiles"])'

# 4. Rate-limit push-back (four sequential from different exits should all be 200)
for port in 8888 8889 8890 8891; do
  curl --http2 -x "http://127.0.0.1:$port" \
    -w "%{http_code} %{time_total}s\n" -o /dev/null -s \
    "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10&page=1"
  sleep 3.5
done

# 5. WAF/challenge-page markers (should return nothing)
curl --http2 -s "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1&page=1" \
  | grep -Ei "just a moment|cf-challenge|cloudflare|please enable javascript|verify you are human"
```

If probes 1, 2, and 5 all match; probe 3 returns a `totalfiles` that has grown but is within a few percent of 64,226; and probe 4 returns `200` on all four exits, then HKLII is unchanged and this chapter is current.
