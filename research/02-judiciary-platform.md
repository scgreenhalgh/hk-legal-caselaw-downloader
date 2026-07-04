# Judiciary Platform (legalref.judiciary.hk)

The Hong Kong Judiciary's Legal Reference System (`legalref.judiciary.hk`) is the origin from which every HKLII judgment ultimately derives. HKLII is a derivative index; the Judiciary is the authoritative source. This chapter documents that origin — where the `.docx` binaries live, where press summaries live, how the caching contract differs from HKLII's zero-cache JSON API, and what the scraper actually pulls from it.

Read [`01-hklii-platform.md`](./01-hklii-platform.md) first for the HKLII stack; this chapter is written as the second half of the two-origin picture.

## Judiciary's role in the pipeline

The Judiciary owns the raw material. Every published judgment starts life as a Microsoft Word document produced inside the Court, then flows to two public destinations:

1. **`legalref.judiciary.hk`** — the Judiciary's own Legal Reference System, which hosts the vetted `.docx` (Word 2007+ Open XML) and, for a subset of notable cases, HTML press summaries written by the Court's Judicial Assistants.
2. **`www.hklii.hk`** — the Hong Kong Legal Information Institute (HKU Law), which ingests those Judiciary documents, extracts HTML content, adds metadata, and exposes everything through a JSON API.

Both platforms serve the same underlying judgments. When a case is present on HKLII, an authoritative `.docx` is present on the Judiciary at a URL you can derive from the judgment record. The `/api/getjudgment` response's `doc` field points straight at it (see [`03-endpoint-reference.md`](./03-endpoint-reference.md) for the wire format).

For our scraper the split matters because:

- The `.docx` is the only source of truth for the ~4% of recent judgments where HKLII's derived `content` field is empty (see [`10-content-safeguards.md`](./10-content-safeguards.md) for the empty-content branch).
- The press summaries never appear in HKLII's own database as first-class records; they are anchor tags embedded inside HKLII's judgment HTML that point back at the Judiciary origin.
- The Judiciary exposes some artefacts HKLII does not — Reasons for Verdict, Reasons for Sentence, Specimen Jury Directions — but we do not fetch these (see below).

## URL patterns

### `.docx` judgment binaries

Every getjudgment response with a non-null `doc` field points at:

```
https://legalref.judiciary.hk/doc/judg/word/vetted/other/{lang}/{year}/{ACT_ID}.docx
```

Where:
- `{lang}` is `en` or `tc` (matches HKLII's `lang` param, though Judiciary URLs use `en`/`tc` directly).
- `{year}` is the four-digit year of the judgment.
- `{ACT_ID}` is the Judiciary action identifier — the same string that appears in HKLII's `cases[0].act` field but with a leading-zero-padded number and underscore, e.g. `HCMP2265/2025` on HKLII becomes `HCMP002265_2025` in the Judiciary URL.

Real examples observed in `downloads/enrich-test/hkcfa/2026/*.json`:

```
https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2025/FACC000003_2025.docx
https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2026/FACV000002_2026.docx
https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2026/FACV000001A_2026.docx  (note trailing 'A' variant)
https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2026/FAMV000048_2026.docx
```

The scraper does not derive these URLs itself. It reads whatever URL appears in the getjudgment `doc` field (`src/hklii_downloader/client.py:70`) and treats that as opaque — meaning the scraper is robust to Judiciary URL-shape changes as long as HKLII's `doc` field stays in sync.

### Press summary HTML pages

Notable judgments ship with a bilingual pair of press-summary HTML pages at:

```
https://legalref.judiciary.hk/doc/judg/html/vetted/other/{lang}/{year}/{ACT_ID}_files/{ACT_ID}{ES|CS}.htm
```

Where:
- `ES` = English Summary
- `CS` = Chinese Summary (Traditional)

Example verified live on 2026-07-04:

```
https://legalref.judiciary.hk/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm
https://legalref.judiciary.hk/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025CS.htm
```

Both returned `HTTP/2 200` with `content-type: text/html; charset=UTF-8` and Content-Length ~10 KB.

### The HKLII 302 hop

The anchors HKLII embeds in its judgment HTML are relative paths on `www.hklii.hk`, not absolute Judiciary URLs. A live probe of the site-relative path shows HKLII serves a 302 to Judiciary:

```
$ curl -sI 'https://www.hklii.hk/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm'
HTTP/2 302
location: https://legalref.judiciary.hk/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm
server: Apache
```

This is invisible to the scraper — `fetch_press_summary` prepends `_BASE_URL = "https://www.hklii.hk"` when the URL is relative (`src/hklii_downloader/enrichment.py:21,31-33`) and the underlying `curl_cffi` client follows the redirect. But it means enrichment traffic touches two origins per summary: one 302 out of HKLII, one 200 from Judiciary. It also means the referring page in tcpdump reads `www.hklii.hk` even though the payload comes from `legalref.judiciary.hk`.

## Server stack

The Judiciary origin sits behind an F5 BIG-IP load balancer fronting bare Apache. HTTP/2 is negotiated via TLS ALPN over TLSv1.3. There is no CDN, no Cloudflare, no Akamai — same shape as HKLII's plain-gunicorn stack, different flavour.

### Response header dump (live, 2026-07-04, from a `.docx` GET)

```
HTTP/2 200
date: Sat, 04 Jul 2026 03:47:01 GMT
content-type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
content-length: 76462
x-frame-options: SAMEORIGIN
x-frame-options: SAMEORIGIN
last-modified: Fri, 03 Jul 2026 08:33:50 GMT
etag: "12aae-655b0c8bf4939"
accept-ranges: bytes
x-xss-protection: 0; mode=block
x-content-type-options: nosniff
strict-transport-security: max-age=31536000; includeSubDomains; preload
content-security-policy: default-src https: 'unsafe-eval' 'unsafe-inline'; object-src 'none'
cache-control: s-maxage=300
set-cookie: BIGipServerpool_dc2_legalref.judiciary.hk_ext01_http=!uuZbwsbz8IpzZipSMysHBCKZQ/dGnpahjTl5d5e+swXBY4KYZAsa7LdzKPpJnGVMK4wVB20uLp3/xw==; path=/; Httponly; Secure; SameSite=Lax
set-cookie: TS013185fb=01589106eb44f529096ef1cd8ce81a0f833f36355eb7a0066ee31f2b76944d3e83a10c2f66b721aa238d6d920c890d55df7d1a70bee77d56978933dc6c3862ed984c4aee84; Path=/; HttpOnly; Secure; SameSite=Lax
```

### What each line tells you

- **No `server:` header.** Apache is scrubbed. This is a deliberate hardening step — the HKLII redirect page still leaks `server: Apache`, but the actual Judiciary origin does not. HKLII's origin, by contrast, cheerfully advertises `server: gunicorn`.
- **`x-frame-options: SAMEORIGIN` appears twice.** This is the same bare-application-server-with-two-middleware-layers fingerprint that HKLII's Django stack shows (see [`01-hklii-platform.md`](./01-hklii-platform.md) § "Duplicate-middleware fingerprint") — no downstream CDN or reverse proxy is collapsing duplicate headers into one. On the Judiciary side the two middleware layers are (a) Apache mod_headers and (b) the application code itself; the values agree (both `SAMEORIGIN`), unlike HKLII where the two values disagree (`SAMEORIGIN` and `ALLOWALL`).
- **`x-content-type-options: nosniff`** appears once here, unlike HKLII where it also duplicates.
- **`content-security-policy: default-src https: 'unsafe-eval' 'unsafe-inline'; object-src 'none'`** — meaningfully more restrictive than HKLII's `default-src *` free-for-all, but still permissive by 2026 standards. `object-src 'none'` blocks Flash/Java plugins.
- **`strict-transport-security: max-age=31536000; includeSubDomains; preload`** — one year HSTS with subdomain coverage and preload eligibility. HKLII does not send HSTS at all.
- **`cache-control: s-maxage=300`** — 5-minute shared-cache directive. This is aimed at downstream caches (proxy, CDN) — private caches (browser) ignore `s-maxage`. Combined with ETag/Last-Modified, the origin is telling downstream layers "you can cache this for 5 min and revalidate cheaply after that." HKLII sends no `cache-control` at all.
- **Two `set-cookie` headers.** `BIGipServerpool_dc2_legalref.judiciary.hk_ext01_http` is the F5 BIG-IP session-persistence cookie — it names the backend pool (`dc2_legalref.judiciary.hk_ext01_http`), which is how we know an F5 load balancer sits in front. `TS013185fb` is F5 TMOS's traffic-management session tracker. Both are `HttpOnly; Secure; SameSite=Lax`. The scraper's `curl_cffi` session jar retains these across the ~76 KB `.docx` fetch, so subsequent doc-fallback requests from the same proxy get sticky routing to the same backend. See [`07-cookies-sessions-warmup.md`](./07-cookies-sessions-warmup.md) for how per-proxy `ImpersonateAsyncClient` cookie jars persist across requests.

### HTTP version and TLS

- HTTP/2 negotiated via ALPN on every probe. HKLII is also HTTP/2 but is served by gunicorn; Judiciary's HTTP/2 comes from an F5 BIG-IP terminating TLS in front of Apache.
- No HTTP/3 offered on the observed response set; no `alt-svc` header was returned.
- TLSv1.3 with modern AEAD ciphers.

### Rate-limit behavior

No `Retry-After`, no `X-RateLimit-*` headers, no `RateLimit-*` (RFC 9331) headers on any of the four probes we ran on 2026-07-04. The origin does not advertise limits at the HTTP layer. The F5 BIG-IP in front may still enforce hidden per-IP quotas — nothing empirical says otherwise — but at the response header level the Judiciary is as quiet about limits as HKLII is.

## File format

The Judiciary hosts Word 2007+ Open XML, not legacy `.doc`:

- **Content-Type**: `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- **Extension**: `.docx`
- **Magic**: `Microsoft Word 2007+` (verified with `file(1)` on `canary_output/hkfc/2025/hkfc_2025_114.docx`)
- **Typical size**: 75-140 KB. Live probes on 2026-07-04:
  - `HCMP002265_2025.docx` — 76,462 bytes
  - `FACC000003_2025.docx` — 75,963 bytes
  - `hkfc_2025_114.docx` (canary sample) — 140,935 bytes on disk

The scraper does not assume the `.docx` extension. `_fetch_doc` inspects the URL and picks the right extension:

```python
ext = ".docx" if judgment.doc_url.lower().endswith(".docx") else ".doc"
```
(`src/hklii_downloader/scraper.py:350`)

This handles the small tail of older judgments that still ship `.doc` binaries. Empirically, all doc URLs seen on 2026-recent cases point at `.docx`. The `hklii download` subcommand still hardcodes `.doc` at `src/hklii_downloader/client.py:128` — that is a known minor bug flagged in [`10-content-safeguards.md`](./10-content-safeguards.md) and only affects targeted single-URL fetches, not bulk scraping.

## HTTP caching contract

This is the single biggest operational difference between the two origins. HKLII's API sends no cache metadata at all. The Judiciary sends the full HTTP-caching contract, verified live on 2026-07-04:

| Header | HKLII API | Judiciary origin |
|---|---|---|
| `ETag` | absent | `"12aae-655b0c8bf4939"` |
| `Last-Modified` | absent | `Fri, 03 Jul 2026 08:33:50 GMT` |
| `Accept-Ranges` | absent | `bytes` |
| `Cache-Control` | absent | `s-maxage=300` |

### Conditional GET works

A follow-up request with `If-None-Match: "128bb-6546e888e0861"` on `FACC000003_2025.docx` returned:

```
HTTP/2 304
etag: "128bb-6546e888e0861"
last-modified: Wed, 17 Jun 2026 08:06:14 GMT
```

Zero-byte body, 304 status. `If-Modified-Since` would work symmetrically. This means an eventual incremental-refresh mode could store ETags in the checkpoint DB and revalidate cheaply — but the scraper does not currently do this. Every `.docx` fetch is a full body download.

### Byte-range resume works

A `Range: bytes=0-1023` request returned:

```
HTTP/2 206
content-length: 1024
content-range: bytes 0-1023/75963
```

Byte-range resume on interrupted `.docx` downloads is available. Not currently used — `_fetch_doc` treats every attempt as a full-body GET (`src/hklii_downloader/scraper.py:335-357`) — but the origin will support it if the scraper ever grows a resume path.

### Why this matters

For a full-corpus run that repeats across days or weeks, the caching contract is the difference between fetching all ~4,000 recent-`content=''` `.docx` files cold every time versus fetching once and validating with 304s afterward. That is deferred because (a) our current scale is small enough that a cold refetch is cheap, and (b) the scraper's `.docx` code path is a fallback for the sub-5% of cases where HKLII's `content` is empty, not the mainline. If we ever mirror the Judiciary directly, cache validation becomes essential; today it is unused capacity.

## Press summaries hosted here

Press summaries are the Judiciary's plain-language abstracts of significant judgments, prepared by the Court's Judicial Assistants. A typical summary is ~5,500 characters (versus ~30,000+ for a full judgment) and structured with:

- Parties, judges, representation
- Background
- Reasoning (numbered paragraphs)
- Decision / disposition

They are ideal first-pass RAG retrieval chunks because they encode the disposition in a fraction of the tokens.

### Delivery model: anchor-in-judgment-HTML

The Judiciary does not expose a press-summary index. HKLII does not surface press summaries as first-class records. Instead, the getjudgment response's `content` field embeds `<a>` tags whose visible text matches `Press Summary (English)` or `Press Summary (Chinese)`, with hrefs pointing at `/doc/judg/html/vetted/other/{lang}/{year}/{ACT_ID}_files/{ACT_ID}{ES|CS}.htm`.

Real example, from `downloads/enrich-test/hkcfa/2026/hkcfa_2026_25.html`:

```html
<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm">Press Summary (English)</a>
<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025CS.htm">Press Summary (Chinese)</a>
```

The scraper extracts these via BeautifulSoup + a regex over the visible anchor text:

```python
_PRESS_SUMMARY_TEXT_RE = re.compile(r"press\s+summary\s*\(([^)]+)\)", re.IGNORECASE)
```
(`src/hklii_downloader/enumerator.py:162-197`)

Extraction is tolerant of tag-wrapping, single-quoted hrefs, case variations, and extra attributes — the previous regex-only approach broke on markup variants and was swapped for BeautifulSoup in the reliability hardening pass.

### Coverage

Press summaries exist only for notable cases. Not all 122,460 judgments in HKLII have them. Verified coverage on 2026-07-04:

- Court of Final Appeal (`hkcfa`) — yes
- High Court (Court of Appeal + Court of First Instance) — yes
- District Court (`hkdc`) — yes
- Magistrates' (`hkmagc`) — some
- Coroner's Court (`hkcrc`) — some
- Smaller tribunals — generally not

Year range: Judiciary's own press-summary year dropdown goes back to 2012.

### File format

Summary pages are plain HTML, XHTML 1.0 Transitional doctype, ~8-10 KB. Sample header from `downloads/enrich-test/hkcfa/2026/hkcfa_2026_26.summary_en.html` (8,418 bytes):

```html
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<link rel="stylesheet" type="text/css" href="/css/elrs.css" />
<script language="JavaScript" src="/js/judgment.js" type="text/JavaScript"></script>
<title></title>
</head>
<body>
<p style="text-align:center">THE HONG KONG COURT OF FINAL APPEAL</p>
<p style="text-align:center"><i>This Summary is prepared by the Court's Judicial Assistants</i></p>
<p style="text-align:center"><i>and is not part of the Judgment.</i></p>
```

The disclaimer paragraph is stable across summaries and provides a clean fingerprint if downstream tooling wants to distinguish a summary HTML from a full judgment HTML. See [`09-scraper-architecture.md`](./09-scraper-architecture.md) for how the enrichment flow saves these to `{stem}.summary_{en|zh}.html` sidecars.

## Reasons for Verdict / Reasons for Sentence — Judiciary-only

The Judiciary hosts several artefact classes that HKLII does not index at all:

- **Reasons for Verdict** — the trial magistrate's reasons for a criminal conviction/acquittal. Mostly Magistrates' Court.
- **Reasons for Sentence** — the trial magistrate's reasons for the sentence imposed.
- **Specimen Jury Directions** — model directions to juries.
- **Miscellaneous** — a catch-all for judicial-office correspondence and administrative rulings.

None of these appear as anchors in HKLII's judgment HTML or as first-class HKLII records. They are indexed only through the Judiciary's own Legal Reference System UI. Fetching them would require:

1. Enumerating the Judiciary's own listing pages (a completely separate flow — HKLII's `/api/getcasefiles` does not surface these).
2. Parsing the Judiciary's ELRS HTML index pages (different DOM shape from anything the current scraper handles).
3. Deriving Judiciary-native URLs from that index.

The scraper does not do any of this. For the project's focus areas — SFC enforcement, startup and small-business disputes, employment, accounting — Reasons for Verdict and Reasons for Sentence are of narrow value (mostly criminal, mostly Magistrates') and the volume that would be added is small relative to the full CFI/CA/DC/CFA corpus already captured. See [`12-decisions-log.md`](./12-decisions-log.md) for the "Reasons for Verdict out of scope" decision rationale.

## Pre-2026 "F5 WAF blocks python UA" belief vs 2026-07-04 empirical

There is a widely-repeated piece of scraper folklore that `legalref.judiciary.hk` runs an aggressive F5 WAF that silently drops any request whose User-Agent contains the substring `python`. This belief is baked into the codebase as a comment above `_BROWSER_HEADERS` at `src/hklii_downloader/client.py:13`:

```python
# The judiciary.hk F5 WAF blocks any UA containing "python" (silent connection hang).
```

The 2026-07-04 empirical evidence does not confirm this claim in its strong form. It also does not fully refute it — the truth appears more nuanced.

### What the probes actually showed

Live probes on 2026-07-04 against `legalref.judiciary.hk` with a plain Chrome-on-macOS User-Agent and a `Referer: https://www.hklii.hk/en/cases/hkcfi/2025/2265` header returned `HTTP/2 200` with the full `.docx` body. No challenge page, no captcha, no JS interstitial. No Cloudflare markers (`cf-ray`, `cf-cache-status`), no Akamai markers, no F5 challenge-page HTML.

Detection markers that would flag a modern WAF challenge — the seven English and six Traditional Chinese markers documented in [`10-content-safeguards.md`](./10-content-safeguards.md) — did not appear.

### What we know for certain

- **There IS an F5 BIG-IP in the path**, but as a load balancer, not a WAF challenge system. The `BIGipServerpool_dc2_legalref.judiciary.hk_ext01_http` cookie in every response header proves it — that cookie is F5's session-persistence mechanism and it names the backend pool it selected (`dc2_legalref.judiciary.hk_ext01_http`).
- **F5 TMOS traffic-management is active**, evidenced by the `TS013185fb` session cookie. TMOS modules can include WAF (ASM), but nothing in the observed response set — no challenge page, no `X-BLOCK` response header, no unusual latency — indicates ASM is actively challenging requests today.
- **The `Referer: https://www.hklii.hk/...` header on Judiciary requests is likely load-bearing.** The scraper's `parser.referer_for(url)` (`src/hklii_downloader/parser.py:40-73`) already derives a plausible HKLII case-page Referer for API calls, but for `legalref.judiciary.hk` fetches the ProxyPool code uses the same generated Referer. That means every `.docx` GET has a Referer pointing at an HKLII case page. Whether the F5 checks this is unverified; the probes that succeeded all had it set.

### What we do not know

- Whether User-Agent containing `python` still trips a hidden block. The probe methodology used only Chrome UAs; a controlled A/B against a `python-requests/2.31.0` UA has not been re-run against `legalref.judiciary.hk` in 2026. The comment at `client.py:13` was inherited lore, not a fresh test.
- Whether ASM rules exist for very high per-IP request rates. Our observed volume from PIA exit IPs is low — the scraper's `.docx` fetch is a fallback that fires only when HKLII's `content` is empty — so we have never provoked whatever quota might exist.

### Practical stance

We ship a plausible Chrome-on-macOS UA on every direct request via `_BROWSER_HEADERS` (`src/hklii_downloader/client.py:13-25`) and let the ProxyPool's `HeaderRotator` do the same for bulk-mode traffic. This is belt-and-suspenders — cheap insurance against a UA-substring rule that may or may not still exist. See [`04-anti-detection-strategy.md`](./04-anti-detection-strategy.md) for the broader posture and [`05-http-headers.md`](./05-http-headers.md) for the exact header composition.

## What the scraper pulls from Judiciary

The scraper touches `legalref.judiciary.hk` in exactly two places:

### 1. `.docx` fallback when HKLII content is empty

For roughly the last four months of judgments (typically 2026-recent), HKLII's `/api/getjudgment` returns an empty `content` field with a populated `doc` field. This is HKLII's ingestion pipeline still catching up — the Judiciary has published the `.docx` but HKLII has not yet extracted the HTML.

The scraper handles this in `_download_one_impl` at `src/hklii_downloader/scraper.py:293-319`:

```python
content_ok = bool(judgment.content_html.strip())
can_try_doc = "doc" in self._formats and judgment.doc_url

if not content_ok and not can_try_doc:
    # empty-content, doc-fetch not requested → mark_failed
    return False

...

if can_try_doc:
    if await self._fetch_doc(judgment, output_dir):
        actually_saved.add("doc")
    elif not content_ok:
        # empty-content AND doc-fetch failed → mark_failed
        return False
```

This path requires `--allow-doc` at the CLI (which flips `doc` into the format set) — without it, `content=''` cases are marked failed with error `"empty-content, doc_url=..."` and the `.docx` is not attempted. See [`11-operations-runbook.md`](./11-operations-runbook.md) for the `--allow-doc` recommendation on production runs.

`_fetch_doc` (`src/hklii_downloader/scraper.py:335-357`) is a full-body GET with jittered exponential backoff on `RequestError` and on `status >= 500`. Byte-range resume is not used. Conditional GET (`If-None-Match`) is not used. The saved file lands at `{output}/{court}/{year}/{stem}.docx` via `atomic_write_bytes`.

### 2. Press-summary HTML

When `--with-summaries` is on, `enrich_summaries_for_case` at `src/hklii_downloader/enrichment.py:66-87` extracts `Press Summary (English|Chinese)` anchors from the judgment HTML and fetches each. The URLs it extracts are relative HKLII paths (`/doc/judg/html/vetted/other/...`); `fetch_press_summary` prepends `https://www.hklii.hk` and follows the 302 to `legalref.judiciary.hk` transparently. Saved to `{stem}.summary_{en|zh}.html`.

The per-kind status columns (`summary_en_status`, `summary_zh_status`) track outcome:
- `pending` — not attempted yet
- `downloaded` — success
- `na` — no anchor found in the judgment HTML for this lang (perfectly normal — most cases have neither)
- `failed` — attempted, network/HTTP/OS error

See [`09-scraper-architecture.md`](./09-scraper-architecture.md) for the checkpoint schema and [`07-cookies-sessions-warmup.md`](./07-cookies-sessions-warmup.md) for how the same per-proxy `ImpersonateAsyncClient` session handles both HKLII and Judiciary traffic — the cookies HKLII sets and the cookies Judiciary sets accumulate in the same jar.

### What the scraper does not fetch

- Reasons for Verdict, Reasons for Sentence — out of scope per project focus.
- Specimen Jury Directions — out of scope.
- Judiciary's own search / listing pages — HKLII's `/api/getcasefiles` is the enumeration source of truth.
- Any Judiciary-native metadata (e.g. Judiciary's own "About this judgment" pages) — HKLII's `/api/getjudgment` metadata is sufficient.

## Corpus-size implications

`.docx` is 7-10x larger than the equivalent HTML content on average.

| Format | Typical per-judgment size | Corpus (~114k mainline) |
|---|---|---|
| HTML (`content` field) | ~10 KB | ~1.1 GB |
| Plaintext (derived) | ~7 KB | ~0.8 GB |
| JSON metadata | ~1 KB | ~0.1 GB |
| `.docx` (Judiciary) | ~76 KB | ~8.7 GB |

Some rough consequences:

- A default `--format html,txt,json` scrape lands in the low single-digit GB range on disk.
- Adding `--allow-doc -f doc` on top adds ~8-9 GB for the mainline four-court set, because the scraper only fetches the `.docx` as a fallback for `content=''` cases — not for every judgment.
- If the fallback logic changed to fetch `.docx` for every case (currently it does not, because HKLII's HTML is authoritative when non-empty), the disk footprint would blow up by roughly the same 8-9 GB.
- The disparity is why press summaries are a natural first-pass RAG retrieval unit — they encode disposition in ~5.5 KB, versus ~76 KB of `.docx` or ~30 KB of judgment HTML. See [`09-scraper-architecture.md`](./09-scraper-architecture.md) for how the sidecar files line up on disk.

The per-`.docx` transfer time depends more on the proxy region than on file size — the F5 BIG-IP's `s-maxage=300` shared-cache hint suggests downstream caches (VPN pop caches, ISP caches) may serve repeat fetches from cache within a five-minute window. We have not measured this empirically. See [`08-vpn-pool.md`](./08-vpn-pool.md) for observed per-region speeds against HKLII (Judiciary transfer speeds should be similar since both are HK-hosted).
