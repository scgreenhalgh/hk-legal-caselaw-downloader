# HKLII API Endpoint Reference

Single source of truth for every HKLII API endpoint the scraper touches or has probed: the request shape, the response envelope, per-court corpus counts, and the wire-level quirks that the retry / enumeration code has to work around.

For the platform this API sits on (server software, TLS, WAF status, homepage counter) see [HKLII platform](./01-hklii-platform.md). For the Judiciary origin that hosts `.docx` fallback bodies referenced by `getjudgment.doc`, see [Judiciary platform](./02-judiciary-platform.md). For the code paths that call these endpoints, see [Scraper architecture](./09-scraper-architecture.md).

Base URL for the whole API: `https://www.hklii.hk/api/`.

## Endpoint index

HKLII is a Vue.js SPA — nothing is server-rendered. All judgment metadata, listing data, and full-text bodies are served from the `/api/` prefix as JSON. The endpoints below are drawn from the front-end JS bundle plus the scraper's own use; only the four in bold are exercised by production code today.

| Endpoint | Verb | Params | Purpose | Called by |
|---|---|---|---|---|
| **`/api/getcasefiles`** | GET | `caseDb`, `lang`, `itemsPerPage`, `page`, optional `sort=-date`, `act`, `neutral`, `title`, `minDateText`, `maxDateText` | Paginated corpus listing per court | `enumerator.enumerate_court` at `src/hklii_downloader/enumerator.py:103-159` |
| **`/api/getjudgment`** | GET | `lang`, `abbr`, `year`, `num` | Full judgment JSON (metadata + HTML content) | `scraper._download_one_impl` via `HKLIICase.api_url` at `src/hklii_downloader/parser.py:26-33` |
| **`/api/getappealhistory`** | GET | `caseno` (URL-encoded) | The same matter as it moved up court levels | `enrichment.fetch_appeal_history` at `src/hklii_downloader/enrichment.py:39-43` |
| `/api/getmetacase` | GET | `caseDb`, `lang` | Cheap `{count, timestamp}` — a totals-only alternative to fetching `getcasefiles` page 1 | Not called (scraper reads `totalfiles` off the first `getcasefiles` page instead) |
| `/api/getlatest` | GET | `court` | 20 most-recent judgments for a court | Not called |
| `/api/simplesearch` | GET | `searchstring`, `disablefuzzy`, ... | Full-text search entry point | Not called |
| `/api/advancedsearch` | GET | dynamic | Multi-field search | Not called |
| `/api/getcasenoteup` | GET | `abbr`, `year`, `num` | Case note-up (subsequent-citation) info | Not called |
| `/api/getmetalegis` | GET | `cap_type`, `lang` | Legislation metadata | Not called |
| `/api/getmetahopt` | GET | `dbcat`, `abbr`, `lang` | HOPT metadata | Not called |
| `/api/gethoptfiles` | GET | HOPT-family params | HOPT file listing | Not called |
| `/api/gettreaty` | GET | treaty params | Treaties collection | Not called |
| `/api/gethistlaw` | GET | historical-law params | Historical laws | Not called |
| `/api/getother` | GET | catch-all | Other document types | Not called |

There is also `https://ai.hklii.hk/case-summary/` for the front-end's LLM summary/tags feature — separate origin, not part of the scraping surface.

Nothing on the primary bulk-scrape path (enumerate → download → enrich) touches any of the non-primary endpoints. The reference is here for completeness so future work does not have to re-grep the JS bundle.

## `getcasefiles`: URL and params

Full URL shape built by the enumerator (`src/hklii_downloader/enumerator.py:120-128`):

```
GET https://www.hklii.hk/api/getcasefiles?caseDb={court}&lang={en|tc}&itemsPerPage={N}&page={p}
```

Only four params are sent by the scraper. Note the exact spelling of the size param:

- `caseDb` — court slug (lowercase, see [Court corpus by slug](#court-corpus-by-slug-2026-07-04) below). Required.
- `lang` — `en` or `tc`. Required. `tc` is Traditional Chinese; there is no `zh` or `zh-hant` alias.
- `itemsPerPage` — the size param is spelled `itemsPerPage`, **not** `pageSize`. Front-end and back-end both use camelCase. Sending `pageSize=1000` is silently ignored (the server falls back to the default page size). Confirmed by probe: `probe.sh` in the scratchpad and `enumerator.py:124`.
- `page` — 1-indexed page number.

Optional params observed in the JS bundle but not used by the scraper: `act`, `neutral`, `title`, `minDateText` (DD/MM/YYYY), `maxDateText`, `sort=-date`. The default sort is already newest-first (see [Sort behavior](#getcasefiles-sort-behavior)), so `-date` is redundant.

The scraper hard-codes `itemsPerPage=10000` at `src/hklii_downloader/scraper.py:139-145` — one call per 10k judgments, ~13 calls to enumerate the full ~118k corpus. The rationale (endpoint probe showed processing time flat at 0.5–1.6 s across `itemsPerPage=10/50/100/1000` — a smaller page size costs 60× more requests with zero latency win) lives in [Decisions log](./12-decisions-log.md).

## `getcasefiles` envelope: two top-level keys

Every 200 response has exactly two top-level keys:

```json
{"totalfiles": 64226, "judgments": [ ... ]}
```

Real snippet from `probes/body_s10.json` (2026-07-04, HKCFI, `itemsPerPage=10`, `page=1`):

```json
{
  "totalfiles": 64226,
  "judgments": [
    {
      "neutral": "[2026] HKCFI 3816",
      "path": "/en/cases/hkcfi/2026/3816",
      "date": "2026-07-03T00:00:00+08:00",
      "parallel": [],
      "cases": [{"title": "LAM HEI KIU V. LAM KIT FUNG", "act": "HCMP2265/2025"}]
    },
    ...
  ]
}
```

Semantics of the two keys:

- **`totalfiles`** — corpus-wide count for the `caseDb` + `lang` combination. It does not decrease as you paginate; it is the same value on every page. The enumerator reads it on page 1 and computes `total_pages = math.ceil(totalfiles / items_per_page)` at `src/hklii_downloader/enumerator.py:141-145`.
- **`judgments`** — array of exactly `itemsPerPage` records on every non-final page. The server does not silently downgrade or cap the batch: all four probes at `itemsPerPage=10/50/100/1000` returned exactly the requested count. The last page returns `totalfiles mod itemsPerPage` records.

Total-vs-page consistency is a hard invariant: the four probes at four different page sizes returned identical `totalfiles=64226` and identical first record (`[2026] HKCFI 3816`, dated `2026-07-03`), proving zero data drift over the ~90 s probe window.

## `getcasefiles` judgment record: five keys

Each element of `judgments[]` has exactly five keys:

| Key | Type | Example | Notes |
|---|---|---|---|
| `neutral` | string | `"[2026] HKCFI 3816"` | Bracketed neutral citation — year + court abbreviation + running number, matches the HK court convention. |
| `path` | string | `"/en/cases/hkcfi/2026/3816"` | Site-relative URL of shape `/{lang}/cases/{court}/{year}/{number}`. Maps directly to a scrapable HTML page. |
| `date` | string | `"2026-07-03T00:00:00+08:00"` | ISO-8601 with fixed HKT offset. See [Sort behavior](#getcasefiles-sort-behavior). |
| `parallel` | array | `[]` | Empirically always empty across every one of the 1000 records in `probes/body_s1000.json`. Purpose in the schema is unclear. See caveat below. |
| `cases` | array of objects | see below | Party titles + case-reference numbers. Can hold 2+ entries for consolidated matters. |

Each `cases[]` object holds exactly two string fields: `title` (uppercased party names) and `act` (the case-reference / action number, e.g. `HCMP2265/2025`). The enumerator only reads the first entry's `title` at `src/hklii_downloader/enumerator.py:48-49`:

```python
cases_list = data.get("cases", [])
title = cases_list[0].get("title", "") if cases_list else ""
```

`cases[]` is not always length-1. Consolidated matters return multiple entries. Two real examples from `probes/body_s1000.json`:

```json
"cases": [
  {"title": "CHEN YUNG NGAI KENNETH AND ANOTHER V. ALAN CHUNG WAH TANG AND ANOTHER", "act": "HCB3819/2011"},
  {"title": "CHEN YUNG NGAI KENNETH AND ANOTHER V. ALAN CHUNG WAH TANG AND ANOTHER", "act": "HCMP631/2022"}
]
```

```json
"cases": [
  {"title": "DANGYACH, ASHISH ... TRADING AS COLORJEWELS V. BEIJING KUANGSHI ...", "act": "HCA1182/2025"},
  {"title": "DANGYACH, ASHISH ... TRADING AS GEMS TRADING CO V. BEIJING KUANGSHI ...", "act": "HCA1183/2025"}
]
```

The scraper's current title assignment (first entry only) is intentional but not lossless — the extra `act` numbers are visible only via the raw enum-cache JSON when `--save-enum-responses` is on. For downstream RAG citation graphs this is a gap and would need a schema change (title + act should probably be an array on `CaseEntry`).

**`parallel[]` caveat.** Across every one of the ~1000 records in the sampled probes the array was `[]`. A grep for `"parallel":\[[^]]` in `probes/body_s1000.json` returned zero non-empty matches. What a populated `parallel[]` looks like is not empirically confirmed — treat any parser that assumes a specific shape here as unverified until we see one in the wild.

## `getcasefiles` sort behavior

Records are returned newest-first by `date`. All four page-size probes' first record was the same one: `[2026] HKCFI 3816` dated `2026-07-03T00:00:00+08:00`.

The `date` field has two hard invariants observed across every record probed:

- **Fixed Hong Kong offset `+08:00`** — no `Z`, no daylight-saving variation (HKT doesn't observe DST).
- **Fixed `T00:00:00` time-of-day** — no hour/minute granularity. Judgments are dated, not timestamped.

That means date arithmetic on this field is safe as long as you preserve the offset. Stripping the timezone and parsing as naive dates would silently interpret the value as UTC and shift dates by one day near the boundary.

The default sort makes `sort=-date` redundant. Passing `sort=date` (ascending) is offered by the SPA but is not exercised by the scraper.

## `getcasefiles` per-court HTTP 500 quirk

Not all 14 courts documented in `[HKLII court databases]` respond `200` to `itemsPerPage=10000`. Seven of them return a Django HTTP 500 error page at that page size, empirically confirmed 2026-07-04 by probing `?caseDb={slug}&lang=en&itemsPerPage=10000&page=1` across all 14 slugs:

| HTTP 200 (7 courts) | HTTP 500 (7 courts) |
|---|---|
| `hkcfi`, `hkca`, `hkcfa`, `hkdc`, `hkfc`, `hkmagc`, `hkoat` | `hkcompet`, `hkcoroners`, `hkfamc`, `hklab`, `hklndtri`, `hkmc`, `hkstsc` |

The 500 response is diagnostic. Real dump from `probes/court_hkcompet.hdr`:

```
HTTP/2 500
server: gunicorn
content-type: text/html; charset=utf-8
content-length: 145
x-frame-options: SAMEORIGIN
x-frame-options: ALLOWALL
x-content-type-options: nosniff
x-content-type-options: nosniff
```

Header set is otherwise identical to a 200 (same duplicate `x-frame-options`, same permissive CSP, same `gunicorn` server, no CDN in path). The distinguishing markers are `content-type: text/html; charset=utf-8` and the absence of an `allow: GET, HEAD, OPTIONS` header. Body is 145 bytes of Django default error HTML:

```html
<!doctype html>
<html lang="en">
<head><title>Server Error (500)</title></head>
<body><h1>Server Error (500)</h1><p></p></body>
</html>
```

**Important gotcha.** The 7 courts in the 500 column overlap exactly with the "invented slugs" from an earlier session's recon that all 500'd — see [Zero-row slugs and invented-slug 500s](#zero-row-slugs-and-invented-slug-500s). At the time we assumed those slugs were fake because they returned 500. The 2026-07-04 probe shows the 500 is not a "slug doesn't exist" signal — several of those slugs are documented HKLII courts (e.g. `hkcompet` is Competition Tribunal, `hkfamc` is Family Court proceedings before the reorg, `hklab` is Labour Tribunal). The 500 fires only at large `itemsPerPage` and may correspond to a per-court query timeout on the Django/gunicorn side.

Because these seven courts also have small corpora, dropping `itemsPerPage` for them (e.g. `itemsPerPage=1000` or the endpoint's default) is expected to return a 200. That per-court fallback is a known operational gap — the current enumerator hard-codes `10000` and would surface these as `httpx.HTTPStatusError` at `src/hklii_downloader/enumerator.py:88-92` after the `_RETRYABLE_STATUSES` retries exhaust. See [Operations runbook](./11-operations-runbook.md) for the workaround (currently: don't add these slugs to `--courts` until an override lands).

The scraper's default `--courts hkcfi,hkca,hkdc,hkcfa` misses all seven; production runs are unaffected until someone widens the court list.

## `getjudgment`: URL and response

Full URL shape (`src/hklii_downloader/parser.py:26-33`):

```
GET https://www.hklii.hk/api/getjudgment?lang={en|tc}&abbr={court}&year={year}&num={number}
```

Real 200 response body from `probes/pair_gzip.body` (`[2026] HKCFI 3816`, 2026-07-04):

```json
{
  "date": "2026-07-03T00:00:00+08:00",
  "db": "Court of First Instance",
  "neutral": "[2026] HKCFI 3816",
  "content": "",
  "doc": "https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2025/HCMP002265_2025.docx",
  "cases": [{"title": "LAM HEI KIU V. LAM KIT FUNG", "act": "HCMP2265/2025"}],
  "corrs": [],
  "parallel_citation": [],
  "is_translation": false,
  "has_translation": false
}
```

The shape is different from `getcasefiles` — no `path`, no `parallel` (but there is `parallel_citation`), plus five extra keys. Full field inventory as consumed by `client.parse_judgment_response` at `src/hklii_downloader/client.py:57-72`:

| Field | Type | Read as | Notes |
|---|---|---|---|
| `db` | string | `court_name` | Human-readable court name, e.g. `"Court of First Instance"`. Not a slug. |
| `neutral` | string | `neutral_citation` | Same shape as in `getcasefiles`. |
| `date` | string | `date` | ISO-8601 with `+08:00` and `T00:00:00`. Same invariants as `getcasefiles`. |
| `cases[]` | array of `{title, act}` | `title` = first entry's title; `case_number` = first entry's `act` | First entry only; see the multi-entry caveat under [judgment record](#getcasefiles-judgment-record-five-keys). |
| `content` | string | `content_html` | The judgment body as an HTML fragment. Can be `""` — see [content='' fallback](#getjudgment-content-fallback-pattern). |
| `doc` | string or null | `doc_url` | Absolute URL to the `.doc`/`.docx` source on `legalref.judiciary.hk`. Present when the Judiciary Word file exists. |
| `parallel_citation` | array of strings | `parallel_citations` | Cross-references to non-neutral citations of the same judgment (e.g. law reports). Empirically empty in the sample. |
| `has_translation` | bool | `has_translation` | Whether a translation is available in the other language. |
| `is_translation` | bool | not read | Whether this response IS the translation. Not currently persisted. |
| `corrs` | array | not read | "Corrections" — subsequent errata attached to the judgment. Not currently persisted. |

The scraper's own metadata JSON sidecar (written by `save_judgment_local` at `src/hklii_downloader/client.py:92-105`) captures the subset it actually reads (`title`, `case_number`, `court`, `date`, `neutral_citation`, `parallel_citations`, `doc_url`, `has_translation`, `url`). `corrs` and `is_translation` are visible only via `--save-enum-responses` on the enumeration path, and via raw response inspection during `getjudgment` calls (not persisted anywhere on that path).

Response headers on `getjudgment` were not independently probed at scale in the 2026-07-04 audit — they were assumed identical to `getcasefiles` because both are served by the same gunicorn/Django origin. This is a known gap; see [Operations runbook](./11-operations-runbook.md) for what an independent probe would need to cover.

## `getjudgment` `content=""` fallback pattern

Recent (~2026) judgments consistently return `content: ""` with a non-null `doc` URL. The `[2026] HKCFI 3816` example above is representative: the HTML body is empty; the entire text is only available as the `.docx` on the Judiciary origin.

The scraper handles this at `src/hklii_downloader/scraper.py:293-319`:

```python
content_ok = bool(judgment.content_html.strip())
can_try_doc = "doc" in self._formats and judgment.doc_url

if not content_ok and not can_try_doc:
    doc_hint = f", doc_url={judgment.doc_url}" if judgment.doc_url else ""
    self._checkpoint.mark_failed(
        record.court, record.year, record.number,
        f"empty-content{doc_hint}",
    )
    return False
```

Behavior matrix:

| `content` | `doc` | `doc` in `--format` set? | Result |
|---|---|---|---|
| non-empty | any | any | saved as `html`/`txt`/`json` per requested formats |
| empty | present | yes (`--allow-doc` used) | `.docx` fetched from `legalref.judiciary.hk`; if 200, saved and marked downloaded |
| empty | present | no | marked failed as `"empty-content, doc_url=..."` — the case is not lost, the `doc_url` is captured in the failure reason for later inspection |
| empty | present | yes but fetch failed | marked failed as `"empty-content, doc-fetch-failed, doc_url=..."` |
| empty | null | any | marked failed as `"empty-content"` |

Downloading recent-era 2026 judgments requires both `--allow-doc` and `-f doc`. Without them the whole 2026 cohort correctly fails with `empty-content` reasons — the case rows remain in the checkpoint and can be re-tried later with the right flags.

The `doc_url` shape is stable: `https://legalref.judiciary.hk/doc/judg/word/vetted/other/{lang}/{year}/{ACT_ID}.docx`. The scraper decides `.doc` vs `.docx` extension at write time based on the URL's actual suffix (`src/hklii_downloader/scraper.py:350`):

```python
ext = ".docx" if judgment.doc_url.lower().endswith(".docx") else ".doc"
```

For how this ties into session flow and Judiciary's HTTP caching semantics (ETag, Last-Modified, Accept-Ranges — all present, unlike HKLII API), see [Judiciary platform](./02-judiciary-platform.md) and [Cookies, sessions, warm-up](./07-cookies-sessions-warmup.md).

## `getappealhistory`: URL and semantics

Full URL shape (`src/hklii_downloader/enrichment.py:39-43`):

```
GET https://www.hklii.hk/api/getappealhistory?caseno={url-encoded caseno}
```

The `caseno` param is URL-encoded via `urllib.parse.quote(caseno, safe='')` — an empty `safe` set means every non-alphanumeric char (including `/`) is percent-encoded. For `caseno=HCMP2265/2025`, the wire query becomes `caseno=HCMP2265%2F2025`. This matters because case numbers routinely contain `/` (year separator) and sometimes `.`, `-`, or `,` — leaving any of those unquoted would break parsing on some caseno formats.

**Semantics — the important part.** `getappealhistory` returns the same matter as it moved up court levels. It is **not** a citation graph; it does not list judgments that cite this one, nor judgments that this one cites. It lists the CFI decision, the CA appeal from it, and (if reached) the CFA appeal — all decisions on the same underlying dispute.

Response shape is a JSON array of related-judgment objects. The scraper does not parse the array — it saves it verbatim as `{stem}.appeal_history.json` via `atomic_write_text` with `indent=2, ensure_ascii=False` at `src/hklii_downloader/enrichment.py:57-63`.

Errors are handled at `src/hklii_downloader/enrichment.py:90-106`:

- `httpx.RequestError`, `httpx.HTTPStatusError`, `json.JSONDecodeError`, `OSError` → mark `appeal_history` as `"failed"` with error string.
- Successful save → mark `"downloaded"`.
- There is no `"na"` (not-available) status for appeal history — every downloaded judgment gets an attempt. Compare `enrich_summaries_for_case` at `src/hklii_downloader/enrichment.py:66-87` which does mark `"na"` when no press summary URL is found in the judgment HTML.

Cross-case citation data (LawCite / austlii.edu.au) is a separate deliverable that is deferred indefinitely due to Cloudflare Managed Challenge on that origin — see [Decisions log](./12-decisions-log.md).

## URL derivation helpers

Three code paths derive URLs. They are worth reading side-by-side because they use slightly different rules.

### `parser.parse_hklii_url` — the input side

`src/hklii_downloader/parser.py:9-11,76-81` compiles the regex once and enforces exact-match:

```python
_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?hklii\.hk/(en|tc)/cases/([a-z]+)/(\d{4})/(\d+)/?$"
)

def parse_hklii_url(url: str) -> HKLIICase:
    m = _URL_PATTERN.match(url)
    if not m:
        raise ValueError(f"Not a valid HKLII case URL: {url}")
    lang, court, year, number = m.groups()
    return HKLIICase(lang=lang, court=court, year=int(year), number=int(number))
```

Constraints enforced by the regex:

- Scheme `http` or `https`, host `hklii.hk` with optional `www.`.
- Lang literal `en` or `tc` (not `zh`, not `zh-hant`).
- Court is one or more lowercase letters (no digits, no hyphens).
- Year is exactly 4 digits.
- Number is one or more digits.
- Optional trailing `/` but nothing after it. A URL with a fragment or query string will fail to parse.

Used by the `hklii download` subcommand for one-off URL inputs (`src/hklii_downloader/cli.py:31-88`).

### `HKLIICase.api_url` — the general derivation

`src/hklii_downloader/parser.py:25-33`:

```python
@property
def api_url(self) -> str:
    params = urlencode({
        "lang": self.lang,
        "abbr": self.court,
        "year": self.year,
        "num": self.number,
    })
    return f"{BASE_URL}/api/getjudgment?{params}"
```

Uses the `HKLIICase.lang` field verbatim — a `tc` case yields `?lang=tc&...`. This is the path the bulk scraper's `_download_one_impl` uses (via `record.lang` on the `CaseRecord` at `src/hklii_downloader/scraper.py:232-236`), so bilingual runs correctly hit the TC endpoint for TC-only rows.

### `CaseEntry.api_url` — the enumeration-side derivation

`src/hklii_downloader/enumerator.py:31-39`:

```python
@property
def api_url(self) -> str:
    params = urlencode({
        "lang": "en",
        "abbr": self.court,
        "year": self.year,
        "num": self.number,
    })
    return f"{_BASE_URL}/api/getjudgment?{params}"
```

**Note the `lang="en"` hard-code.** `CaseEntry` is a lightweight record produced by the enumerator; it doesn't carry the enumeration's own lang. Any caller that reads `CaseEntry.api_url` and hits the API will always fetch the English body regardless of which language enumeration produced the entry. This is fine for the current flow because the actual download path uses `HKLIICase.api_url` off the checkpoint's `CaseRecord.lang`, not `CaseEntry.api_url` — but it's a foot-gun for future code that iterates `CaseEntry` objects directly.

### `parser.referer_for` — the Referer for XHR

Not URL derivation strictly, but same file: `src/hklii_downloader/parser.py:40-73` computes a plausible SPA Referer per URL. For `/api/getcasefiles?caseDb=hkcfi&lang=en&...` it returns `https://www.hklii.hk/en/cases/hkcfi/`; for `/api/getjudgment?lang=en&abbr=hkcfi&year=2026&...` it returns `https://www.hklii.hk/en/cases/hkcfi/2026/`. Full mechanics live in [HTTP headers](./05-http-headers.md).

## Court corpus by slug 2026-07-04

Empirically confirmed via `getcasefiles?caseDb={slug}&lang=en&itemsPerPage=10000&page=1` for every documented slug on 2026-07-04. The `totalfiles` field is the source of truth.

### Major courts (~97% of corpus)

| Slug | Court | `totalfiles` |
|---|---|---:|
| `hkcfi` | Court of First Instance | 64,226 |
| `hkca` | Court of Appeal | 29,911 |
| `hkdc` | District Court | 18,118 |
| `hkcfa` | Court of Final Appeal | 2,143 |
| **subtotal** | | **114,398** |

### Smaller courts and tribunals

| Slug | Court | `totalfiles` |
|---|---|---:|
| `hkldt` | Lands Tribunal | 1,917 |
| `hkfc` | Family Court | 1,789 |
| `hkct` | Competition Tribunal | 42 |
| `hkmagc` | Magistrates' Courts | 24 |
| `hkcrc` | Coroner's Court | 11 |
| `hklat` | Labour Tribunal | 5 |
| `hkoat` | Obscene Articles Tribunal | 2 |

Total across all 11 confirmed non-zero slugs: **118,188** judgments.

The HKLII homepage counter shows **122,460** as of 2026-07-01. The ~4.3k delta (~3.5%) is likely a counter-vs-API arithmetic gap (the counter appears to add press summaries and/or bilingual duplicates); no listed slug refused an API call. The scraper's default `--courts hkcfi,hkca,hkdc,hkcfa` captures the 114,398 in the top four = 97% of confirmed corpus. See [Operations runbook](./11-operations-runbook.md) for how to widen the court list.

## Zero-row slugs and invented-slug 500s

### Confirmed zero-row slugs (2026-07-04)

Both of these responded with `200 OK` + `totalfiles=0` — the slug exists in the routing table but has never been populated:

| Slug | Court | `totalfiles` |
|---|---|---:|
| `hksct` | Small Claims Tribunal | 0 |
| `ukpc` | UK Privy Council (Hong Kong appeals) | 0 |

The `ukpc` zero is expected: post-1997 there are no new appeals to the Privy Council from Hong Kong, and the pre-1997 corpus is not part of HKLII's dataset (BAILII carries it). `hksct`'s zero suggests Small Claims Tribunal rulings simply aren't published to HKLII — likely because they are typically delivered orally.

### Invented-slug 500s

A prior recon session (`bd7d19`, pre-2026-07-04) tried seven slugs that were not documented in the HKLII UI: `hklndtri`, `hklab`, `hkcompet`, `hkcoroners`, `hkstsc`, `hkfamc`, `hkmc`. All seven returned HTTP 500 at large `itemsPerPage`.

Follow-up 2026-07-04: those seven overlap **exactly** with the seven courts in the [per-court HTTP 500 quirk](#getcasefiles-per-court-http-500-quirk) table above. Several of them are real HKLII slugs (`hkcompet` = Competition Tribunal, `hkfamc` = Family Court proceedings, `hklab` = Labour Tribunal); a 500 at `itemsPerPage=10000` is not proof the slug is invalid — it's a per-court server-side limit issue.

Concrete guidance: do not treat `500` as "slug does not exist". A `200 OK` with `totalfiles=0` is the real "no such corpus" signal. If future work needs one of the 500-ing courts, fall back to a smaller `itemsPerPage` (the endpoint probe accepted `10/50/100/1000` cleanly on courts that were 200 at `10000`).

## Request minima

The API has no authentication and no session requirements. Empirical minimum request shape that returns `200 OK` with full data (curl-shape probe from `probe.sh` in scratchpad, trace at `scratchpad/trace_s10.txt`):

```
GET /api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10&page=1 HTTP/2
Host: www.hklii.hk
User-Agent: curl/8.7.1
Accept: */*
```

That's it. No cookie, no Referer, no Origin, no Sec-Fetch-*, no Accept-Language, no Accept-Encoding. Origin still returns 200 with the full JSON body. Confirmed against four different PIA exit IPs on ports 8888–8891, 3.5 s apart per probe — no rate-limiting, no CAPTCHA, no 429.

**What this means for scraping:** the API layer is not gated. The scraper does send a full real-Chrome header set and does derive a plausible Referer, but that is a defensive posture against a hypothetical future WAF flip, not a requirement of the current API. See [Anti-detection strategy](./04-anti-detection-strategy.md) for why we bother, and [HTTP headers](./05-http-headers.md) / [TLS + HTTP/2 fingerprinting](./06-tls-http2-fingerprinting.md) for what the layer actually sends.

**Absent from response:** no `Retry-After`, no `X-RateLimit-*`, no RFC-9331 `RateLimit-*`, no `ETag`, no `Last-Modified`, no `Cache-Control`, no `Age`, no compression (even when `Accept-Encoding: gzip, deflate, br, zstd` is offered — see `probes/pair_gzip.metrics` vs `pair_noenc.metrics`, both 357 bytes). The client cannot rely on any server-provided pacing hint and must own retry/backoff entirely. Retry-status sets and jittered backoff live at `src/hklii_downloader/scraper.py:25-58` and `src/hklii_downloader/enumerator.py:16-19,61-67`; see [Scraper architecture](./09-scraper-architecture.md) for the loop that consumes them.

**Present on every response:** `server: gunicorn`, HTTP/2 via TLS ALPN, `content-type: application/json` (200s) or `text/html; charset=utf-8` (500s), permissive CSP, `vary: Cookie,origin`, `x-robots-tag: noindex`, `referrer-policy: same-origin`, `cross-origin-opener-policy: same-origin`, plus the diagnostic duplicate `x-frame-options` (`SAMEORIGIN` + `ALLOWALL`) and duplicate `x-content-type-options` (`nosniff` + `nosniff`) — two Django/gunicorn middleware layers each stamp a value with no CDN downstream to normalize them. Full platform-level treatment of this middleware-duplication fingerprint is in [HKLII platform](./01-hklii-platform.md).
