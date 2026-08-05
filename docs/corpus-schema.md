# Corpus schema — on-disk artifact reference

Complete map of the artifacts under `output/` and how to read them. Written
2026-08-05 against the corpus produced by the 2026-07 scrape run
(162,713 cases · 9,464 in-force legis · 30,943 legis history · 3,014 hopt+D3 ·
~33 GB on disk).

Every artifact type here has a matching JSON Schema in
[`docs/schemas/`](schemas/) — machine-readable, JSON Schema draft 2020-12.
Object schemas use `additionalProperties: false` so that new wire fields
surface as validation failures rather than pass silently; the discriminated-
union `events-log.schema.json` uses `unevaluatedProperties: false` at the
outer level, which is the draft-2020-12 idiom for the same effect across
a `oneOf`. Schemas are validated against real samples on every commit via
`scripts/validate_corpus_schemas.py`.

## Encoding

| Type | Encoding |
|---|---|
| All JSON files | UTF-8, `ensure_ascii=False` (Chinese written literally, not escaped) |
| All HTML files | UTF-8 |
| All `.txt` | UTF-8, LF line endings |
| Word originals | Binary — validated by magic bytes: `PK\x03\x04` (docx), `\xd0\xcf\x11\xe0` (OLE doc), `\xdb\xa5\x2d\x00` (pre-OLE Word 6/95), `{\rt` (RTF). The on-disk extension is chosen from the magic bytes, not from the Judiciary URL suffix, so a `.doc` URL that actually serves RTF lands as `.rtf`. |
| PDFs (pcpdaab only) | Standard PDF |
| SQLite | `viewer.db` (WAL mode — `.wal`/`.shm` sidecars are normal) |
| Timestamps | ISO 8601. Court-case and legis-version dates carry the Hong Kong offset (e.g. `2020-03-20T00:00:00+08:00`), with three offsets observed on court cases: `+08:00` (98.9%), `+09:00` (~0.9%, mostly post-1972 rows tagged with an anomalous offset), and `+07:36:42` (~0.1%, pre-1904 rows using Hong Kong Local Mean Time). UKPC, HOPT, and D3 dates are bare `YYYY-MM-DD` with no timezone component. Events log entries use UTC (`+00:00`) with microsecond precision. |

## Directory layout

```
output/
  {court}/{year}/                     — 12 case-metadata courts (see §1)
    {court}_{year}_{num}.{json,html,txt,doc,docx,rtf,generated.html}
    {court}_{year}_{num}.{noteup,appeal_history}.json
    {court}_{year}_{num}.summary_{en,zh}.html          (when HKLII publishes one)
    {court}_{year}_{num}.tc.{json,html,txt,docx}       (case-translations sidecar)
  ukpc/{year}/                        — UKPC uses ukpc-metadata schema (§1.5)
    ukpc_{year}_{num}.{json,html,txt}                  (metadata differs; html/txt are HKLII-served)
  hopt/{abbr}/{year}/{num}/
    {abbr}_{year}_{num}_{lang}.json                    (5 databases · 2 langs)
  d3/{family}/{year}/{num}/
    {family}_{year}_{num}_{lang}.json                  (hklrccp, hklrcr, pcpdc — 3 langs)
    pcpdaab_{year}_{num}_{lang}.{json,pdf,txt}         (pcpdaab: JSON + PDF + PDF-extracted TXT triplet, en/tc only)
  legis/{abbr}/{cap}/
    {abbr}_{cap}_{lang}.content.json                   (currently in force)
    {abbr}_{cap}_{lang}.v{vid}.content.json            (historical versions)
    {abbr}_{cap}_{lang}.versions.json                  (version index)
    relatedcaps_{lang}.json                            (ord→reg mapping)
  events.jsonl                        — pipeline event log (161 MB, 741k lines)
  scrape.log · run.stdout             — human-readable text logs
  failure_samples/                    — captured challenge/WAF pages
    challenge_{court}_{year}_{num}[_{seq}].{html,headers.json}   (seq suffix disambiguates repeat captures)
  .enum_cache/{court}_{lang}/         — raw getcasefiles snapshots (provenance)
    {unix_ts}_page{NNNN}.json
  .checkpoint.db                      — scraper state DB (SQLite, 11 tables, §9)
  .hklii.lock · .checkpoint.db.lock   — fcntl exclusive locks (zero-byte)
  viewer.db (+ .db-wal, .db-shm)      — SQLite search index for the local viewer
```

# 1. Court case records

**Locations:** `output/{court}/{year}/`. The 12 slugs that use the
canonical case-metadata schema: `hkcfa, hkca, hkcfi, hkdc, hkfc, hkldt,
hkmagc, hkct, hkcrc, hklat, hksct, hkoat`. `ukpc` uses a **different
metadata shape** — see [§1.5](#15-ukpc-different-metadata-shape) — but
still has the same `.html`/`.txt` body siblings as the 12 mainline slugs.

**Naming:** stem = `{court}_{year}_{number}` (e.g. `hkcfa_2020_10`). No lang
suffix on the primary file — that is EN unless HKLII has TC only. TC
sidecars carry a `.tc` infix.

## 1.1 Primary metadata `{stem}.json`

Schema: [`case-metadata.schema.json`](schemas/case-metadata.schema.json).
Produced by `client.py:save_judgment_local`.

Real example (`output/hkcfa/2020/hkcfa_2020_10.json`, verbatim):

```json
{
  "title": "HKSAR V. YUONG HO CHEUNG AND OTHERS",
  "case_number": "FAMC58/2019",
  "court": "Court of Final Appeal",
  "date": "2020-03-20T00:00:00+08:00",
  "neutral_citation": "[2020] HKCFA 10",
  "parallel_citations": [],
  "doc_url": "https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2019/FAMC000058_2019.doc",
  "has_translation": false,
  "url": "https://www.hklii.hk/en/cases/hkcfa/2020/10"
}
```

Notes:
- `case_number` is the primary docket for **this row**. A judgment covering
  N dockets appears N times with different `case_number` values and
  identical body text — group by `neutral_citation` or by `body_sha256`
  from `viewer.db` to dedupe. 14 rows in the current corpus have
  `case_number == ""` (paired with empty `title`) — HKLII phantom entries
  where no docket was ever assigned.
- `neutral_citation` for pre-2018 judgments is assigned retrospectively by
  HKLII as catalogue metadata; it is not necessarily present in the
  original body text.
- `has_translation:true` means "a counterpart in the other language exists
  at the same `(court, year, number)`" — not that a sidecar file has been
  downloaded. Match on `(court, year, number)` to pair language versions.
- `doc_url` is typed nullable in the schema (defensive against future
  HKLII rows with no source doc) but 0 of 162,248 rows in the current
  corpus are null.

## 1.2 Body files

| File | Encoding | Purpose |
|---|---|---|
| `{stem}.html` | UTF-8 HTML | Raw `content` field from HKLII's `/api/getjudgment`. This IS the source that `{stem}.txt` is derived from. |
| `{stem}.txt` | UTF-8 text | `BeautifulSoup(html, "lxml").get_text()` — script/style/link/meta stripped, block tags separated by newlines, blank lines squeezed. Table structure NOT preserved. |
| `{stem}.doc` \| `.docx` \| `.rtf` | Binary (magic-validated) | Judiciary-hosted Word/RTF original, fetched from `legalref.judiciary.hk` (separate host, not HKLII). Extension follows the file's magic bytes. |
| `{stem}.generated.html` | UTF-8 HTML | Locally converted from the `.doc`/`.docx` via pandoc / `soffice --convert-to`. Only ~290 rows — a fallback when HKLII returned an empty `content` (very recent judgments HKLII has not rendered yet). |

For substantive work where table structure matters, read the
`.doc`/`.docx` — that is the Judiciary's authoritative original. `{stem}.txt`
is a convenience rendering.

## 1.3 Enrichment sidecars

| File | Schema | Purpose |
|---|---|---|
| `{stem}.noteup.json` | [`noteup.schema.json`](schemas/noteup.schema.json) | Array of judgments that cite this one — from `/api/getcasenoteup`. Empty array = never cited. |
| `{stem}.appeal_history.json` | [`appeal-history.schema.json`](schemas/appeal-history.schema.json) | Chain of dockets across the appeal — from `/api/getappealhistory`. Typically 1–4 entries (99.1% of 162,373 files); observed max 43. Zero empty arrays in the current corpus. |
| `{stem}.summary_en.html`, `.summary_zh.html` | (raw HTML, no schema) | Press summary — only exists when HKLII publishes one (mostly hkmagc/hkcfa). |

## 1.4 TC sidecar files

Files with a `.tc` infix (`{stem}.tc.json`, `.tc.html`, `.tc.txt`, `.tc.docx`)
are written by `case_translations.py` when the primary scrape ran with
`--lang both` under EN-wins-for-bilingual semantics and the TC counterpart
was missed. Only 1,517 exist. All 1,517 `.tc.json` files validate against
[`case-metadata.schema.json`](schemas/case-metadata.schema.json) — same
shape as the primary metadata. For most TC judgments the TC copy is a
**separate primary** at the same `(court, year, number)` under
`output/{court}/{year}/{stem}.json` with `url` pointing at `/tc/cases/...`
— that is where the bulk of the 45k+ Chinese records live.

### Optional backfill provenance fields

One row in the current corpus (`hkdc/2019/128.tc.json`) carries three
additional fields recording that it was fetched outside the normal
`getjudgment` path:

- `source` — e.g. `"judiciary-docx-fallback"`
- `source_note` — human-readable explanation of the fallback route
- `backfilled_at` — YYYY-MM-DD

These are optional in [`case-metadata.schema.json`](schemas/case-metadata.schema.json).
`hkdc/2019/128` is the sole documented instance — HKLII's
`getjudgment?lang=tc` returned empty `content` while including a doc
pointer; the row was fetched from Judiciary and locally converted via
pandoc. Per project standing rules, this fallback path is not
generalised — see `RESUME_PROMPT.md` Deliberate non-goals.

## 1.5 UKPC (different metadata shape)

UKPC (Judicial Committee of the Privy Council appeals from HK) comes
through HKLII's `/api/getother` (hopt-C category), not `/api/getjudgment`.
`ukpc.py:save_ukpc_local` therefore writes a distinct **metadata** shape
from the 12 mainline courts. The body files (`.html`, `.txt`) are still
present and identical in semantics — HKLII's `getother` response also
carries `content` HTML, which is stripped to `.txt` the same way as the
mainline courts.

Schema for the metadata JSON: [`ukpc-metadata.schema.json`](schemas/ukpc-metadata.schema.json).

**Files per row:** `ukpc_{year}_{num}.{json,html,txt}` — 237 of each on
disk (one triplet per row). No `.doc`/`.docx`, no `.noteup.json`, no
`.appeal_history.json`, no press summaries or TC sidecars.

Metadata example (real row, `output/ukpc/1996/ukpc_1996_40.json`):

```json
{
  "title": "Mak v. Wocom Commodities Limited",
  "neutral_citation": "[1996] UKPC 40",
  "date": "1996-11-12",
  "abbr": "ukpc",
  "year": 1996,
  "num": 40,
  "lang": "en",
  "url": "https://www.hklii.hk/en/cases/ukpc/1996/40"
}
```

**Missing** vs case-metadata: `case_number`, `court`, `doc_url`,
`has_translation`, `parallel_citations`. **Extras**: `abbr`, `year`,
`num`, `lang`. 237 rows in the corpus; every one uses this shape. The
sibling `.html` + `.txt` files are not schema-checked (raw HTML / stripped
plaintext) but every row has both.

# 2. HOPT — treaties + bilateral agreements

**Locations:** `output/hopt/{abbr}/{year}/{num}/` where `{abbr}` ∈ `bacpg`
(Bilateral Agreements Concluded by the Central People's Government),
`bahkg` (Bilateral Agreements Concluded by the HKSAR Government), `hktmc`
(Arrangements with the Macao SAR), `hktml` (Arrangements with the
Mainland), `hkts` (HK Treaty Series — "Treaties"). The parenthetical
labels are the actual `db` field values HKLII writes; the hopt.py module
docstring uses older labels ("Basic Law Consultation Papers" etc.) that
do not match the current corpus.

**Naming:** `{abbr}_{year}_{num}_{lang}.json` — one file per (row, lang).
`year` is 4-digit or the literal string `nd` ("no date" — ~10 old treaties).

**Only file type: JSON.** No `.txt`, no `.html` — the document text is
embedded in the `content` field.

Schema: [`hopt-entry.schema.json`](schemas/hopt-entry.schema.json).

```json
{
  "db": "Treaties",
  "title": "AGREEMENT ESTABLISHING THE INTER-AMERICAN DEVELOPMENT BANK",
  "date": "1964-08-03",
  "neutral": "[1964] HKTS 1",
  "category": "Labour",
  "body": "International Labour Organisation",
  "content": "<!--make_database header end-->\n<p align=\"center\">...</p>",
  "inforce": true,
  "has_translation": true
}
```

`db` distinct values across the whole HOPT corpus: `Treaties` / `公約`
(hkts); `Bilateral Agreements Concluded by the Central People's Government`
/ `中央人民政府達成的雙邊協定` (bacpg); `Bilateral Agreements Concluded by the
HKSAR Government` / `香港特別行政區政府達成的雙邊協定` (bahkg); `Arrangements with
the Macao SAR` / `香港特別行政區與澳門特別行政區之間的安排` (hktmc); `Arrangements with
the Mainland` / `香港特別行政區與內地之間的安排` (hktml). Note the earlier `"db":
"HKTS"` example in previous versions of this doc was fabricated — HKLII
does not use the abbrev as the `db` value.

Nullability observed in the corpus:
- `date`: null on 20/1,149 rows (HKLII has no promulgation date).
- `body`: null on 762/1,149 rows (HKLII has no issuing body).

To get plain text: run `parser.html_to_text(content)` — same function used
for court cases' `.txt` files.

# 3. D3 secondary corpora — LRC, PCPD

Four families, two shapes. Locations: `output/d3/{family}/{year}/{num}/`.

Design ref: [`d3-runner-design.md`](d3-runner-design.md).

## 3.1 `d3/hklrccp`, `d3/hklrcr`, `d3/pcpdc` — HTML-in-JSON

Schema: [`d3-html.schema.json`](schemas/d3-html.schema.json).

**Files:** `{family}_{year}_{num}_{lang}.json`. Langs observed: `en`, `tc`,
`sc`. No separate text file — the document HTML is in `content`.

Real example excerpt (`output/d3/hklrcr/2003/1/hklrcr_2003_1_tc.json`,
`content` truncated for brevity):

```json
{
  "id": 2748,
  "title": "排解家庭糾紛程序",
  "neutral": "[2003] HKLRCR 1",
  "date": "2003-03-02",
  "path": "/2003/1/",
  "db": {"id": 45, "name": "法律改革委員會報告書", "abbr": "hklrcr", "lang": "TC", "path": "/tc/other/hklrcr/"},
  "file_type": 1,
  "content": "<script charset=\"UTF-8\" language=\"JavaScript\">\n <!--\n        genHeader();\n    //-->\n</script>\n<div id=\"pageTitleDIV\">\n</div>\n<p class=\"pageTitle2\">\n 《排解家庭糾紛程序》\n ...",
  "pdf": "",
  "has_translation": true
}
```

`file_type=1` means "HTML in `content`". HKLII's `file_type=2` means
"external PDF" (used for pcpdaab — see 3.2 — and for `hkiac`/`histlaw`
which are disabled in `ACTIVE_D3_FAMILIES`).

## 3.2 `d3/pcpdaab` — Administrative Appeals Board decisions

Schema: [`d3-pcpdaab.schema.json`](schemas/d3-pcpdaab.schema.json).

**Files:** `pcpdaab_{year}_{num}_{lang}.{json,pdf,txt}` — every row has
all three (735 of each on disk). The `.txt` is PDF-extracted plain text
via `d3.save_d3_pdf`'s extract-text contract, mirroring the same pattern
the other D3 families use for HTML-derived text.

**Langs:** en/tc only — unlike the other three D3 families (which are
en/tc/sc), pcpdaab is bilingual. This is by construction: the
`_PCPD_LANG_URL_PREFIX` map in `pcpdaab.py` has only `en → english` and
`tc → tc_chi`, so no `_sc` sidecar can ever be produced.

HKLII does not serve pcpdaab PDFs directly (its `/static/` URLs return SPA
HTML). The `pcpdaab` runner resolves each row against pcpd.org.hk's public
decisions listing and fetches the real PDF from there — see
`src/hklii_downloader/pcpdaab.py`.

```json
{
  "hklii": {
    "title": "AAB 36-2012 (This decision provides Chinese version only)",
    "neutral": "[2012] HKPCPDAAB 36",
    "date": "2012-01-02"
  },
  "pcpd": {
    "filename": "AAB_36_2012.pdf",
    "anchor_text": "AAB 36-2012 (This decision provides Chinese version only)",
    "chinese_only": true,
    "shares_pdf_with": [],
    "resolved_year": 2012,
    "resolved_num": 36
  }
}
```

Language quirk: `pcpd.chinese_only:true` means the pcpd.org.hk listing
marks this PDF as `"provides Chinese version only"`. The flag is an
intrinsic property of the PDF (not of the row's language) and is written
identically to both the `_en` and `_tc` sidecars for that decision.
**Trust this flag over the `_en` filename** when determining the language
of the underlying decision. 197 of 368 EN sidecars and 197 of 367 TC
sidecars carry `chinese_only: true`.

`shares_pdf_with` is `[[year, num], …]` — a list of AAB `(year, num)`
partner tuples that share the same PDF (batched decisions). Empty on every
row in the current corpus.

# 4. Legislation

**Locations:** `output/legis/{abbr}/{cap}/` where `{abbr}` ∈ `ord`
(ordinances), `reg` (subsidiary regulations), `instrument` (statutory
instruments).

**Three artifact types per (cap, lang):**

| File | Schema | Purpose |
|---|---|---|
| `{abbr}_{cap}_{lang}.versions.json` | [`legis-versions.schema.json`](schemas/legis-versions.schema.json) | Version index — newest-first list. First entry is the currently-in-force version. |
| `{abbr}_{cap}_{lang}.content.json` | [`legis-content.schema.json`](schemas/legis-content.schema.json) | TOC + section HTML of the **currently in force** version (no `vid` in filename). |
| `{abbr}_{cap}_{lang}.v{vid}.content.json` | (same as `.content.json`) | Historical version TOCs — one per `vid` from `.versions.json`. ~31k files. |
| `relatedcaps_{lang}.json` | [`legis-relatedcaps.schema.json`](schemas/legis-relatedcaps.schema.json) | Ord→reg mapping. For `abbr=ord`, returns a single self-lookup entry; for `abbr=reg`, returns all subsidiary regulations. |

To find the currently in-force text of Cap X in EN:
`output/legis/ord/X/ord_X_en.content.json` — an array of section objects
with inline HTML in each `content` field. Sort by `internal_order` to
reconstruct reading order.

# 5. Pipeline event log

`output/events.jsonl` — newline-delimited JSON, ~741k lines in the current
run.

Schema: [`events-log.schema.json`](schemas/events-log.schema.json) — a
`oneOf` discriminated on the `kind` field. Nine kinds observed in the
current corpus, plus one kind (`enrichment_challenge`) declared in the
schema for a code path (`enrichment.py`) that emits it on press-summary
WAF hits but hasn't fired in the current run:

| Kind | Count (2026-07 run) | Fields beyond `ts, kind` |
|---|---:|---|
| `request_success` | 715,787 | `proxy_url, url, http_status, elapsed_ms` |
| `ip_echo` | 15,597 | `proxy_url, url, elapsed_ms, extra.observed_ip` |
| `pool_exhausted` | 7,730 | `court, year, num, error_class, error_msg` |
| `warmup` | 1,401 | `proxy_url, url, elapsed_ms` |
| `request_failed` | 670 | Two variants — see note below |
| `doc_invalid_magic` | 68 | `court, year, num, url, error_class, error_msg` |
| `challenge_detected` | 39 | `court, year, num, proxy_url, url, http_status, error_class, error_msg, response_len` |
| `case_failed` | 26 | `court, year, num, error_class, error_msg` |
| `degraded` | 5 | `proxy_url, error_class, error_msg` |
| `enrichment_challenge` | 0 | `court, year, num, url, error_class, error_msg, extra.enrichment_kind` |

`request_failed` has two producer sites and two shapes:
- **Exception path** (275/670 rows) — `proxy_pool.py` on `httpx.RequestError`.
  Includes `error_class` + `error_msg`, no `http_status`.
- **HTTP-status path** (395/670 rows) — `proxy_pool.py._emit_request_event`
  on any response with status in `_PROXY_FAILURE_STATUSES` (403/429/5xx).
  Includes `http_status`, no `error_class`/`error_msg`.

The schema branch accepts both variants (`error_class`, `error_msg`, and
`http_status` are all optional on this branch). If you consume the log,
either variant may appear.

`ts` is ISO 8601 UTC with microsecond precision. `proxy_url` is `direct`
or `http://localhost:88XX`. `error_class` is a greppable bucket for
per-error analytics.

# 6. Enumeration cache snapshots

**Location:** `output/.enum_cache/{court}_{lang}/{unix_ts}_page{NNNN}.json`.

Schema: [`enum-cache-snapshot.schema.json`](schemas/enum-cache-snapshot.schema.json).

Raw responses from HKLII's `/api/getcasefiles` endpoint, saved as
provenance when the scraper ran with `--save-enum-responses`. One file per
paginated page. Consumers of the primary corpus should ignore this
directory — it is enumeration provenance, not case data.

# 7. Failure samples

**Location:** `output/failure_samples/challenge_{court}_{year}_{num}[_{seq}].{html,headers.json}`.
The optional `_{seq}` suffix disambiguates repeat captures of the same
(court, year, num).

Paired capture of WAF/challenge pages that arrived instead of a case. The
`.html` file is the challenge body; the `.headers.json` file is the
response headers plus capture metadata.

Schema for the sidecar: [`failure-sample-headers.schema.json`](schemas/failure-sample-headers.schema.json).

```json
{
  "signature": "challenge_hkca_1983_60",
  "captured_at": "2026-07-05T07:27:44.853747+00:00",
  "is_challenge": true,
  "truncated": false,
  "body_bytes": 60112,
  "headers": {"server": "gunicorn", "content-type": "application/json"}
}
```

Sampled evidence for tuning proxy/UA behaviour — not case data.

# 8. Local viewer index

`output/viewer.db` — SQLite database populated by `hklii viewer index`
(in the viewer worktree, not in this repo). Not the case store — an
index over the on-disk files. Row count varies with how much of the
corpus has been indexed; use `sqlite3 output/viewer.db "SELECT COUNT(*)
FROM fts_cases"` to check the current state.

WAL-mode; `.db-wal` / `.db-shm` sidecars are normal.

## Tables

| Table | Columns | Purpose |
|---|---|---|
| `fts_cases` | `case_key, lang, court, year, number, neutral, title, date, body_source, body_sha256, indexed_at` | Case metadata + body hash. PK `(case_key, lang)`. `case_key` is `"hkcfa/2020/10"`. |
| `case_bodies` | `id, case_key, lang, title, body` | Full body text. Unique `(case_key, lang)`. |
| `fts_body` | FTS5 virtual (trigram tokenizer, case-insensitive) | Full-text search over `case_bodies`. Requires `fts5` module in the SQLite build. |
| `viewer_hub_cache` | `case_key, inbound_count, computed_at` | Cached inbound-citation counts. Empty when not populated. |

Indexes: `idx_fts_cases_court_year (court, year)`, `idx_fts_cases_lang_court (lang, court)`.

`body_sha256` is the natural dedup key for text-identical judgments across
multiple dockets. `body_source` records provenance — `html` = HKLII HTML,
`generated_html` = local pandoc conversion.

# 9. Scraper state DB — `.checkpoint.db`

`output/.checkpoint.db` — the SQLite DB where the scraper stores its
authoritative state: every fetch attempt, every citation edge, every
freshness signal. The on-disk JSON/HTML files under `output/` are
**derived** from this DB — if they get deleted, `hklii verify` reconciles
against the DB and re-scrapes what's missing.

Full table reference (11 tables, ~632k rows including 162,713 cases and
242,488 citation edges): [`docs/schemas/checkpoint-db.md`](schemas/checkpoint-db.md).

The top-level `checkpoint.db` at the repo root is a stub from an earlier
layout — the live DB is the one under `output/`.

# Text logs

| File | Contents |
|---|---|
| `output/scrape.log` | Standard Python logger — INFO-level, human-readable |
| `output/run.stdout` | Rich-progress console output + summary lines from the CLI |

# Validating a corpus against these schemas

```bash
uv run --with jsonschema python scripts/validate_corpus_schemas.py output/
```

The script samples files per artifact type (via random-with-fixed-seed
sampling so a fail is reproducible), validates against the corresponding
schema, and exits non-zero on any failure. It also validates every line
of `events.jsonl` — with 741k lines in the current corpus a full sweep
takes ~90s. Extend `SAMPLES_PER_TYPE` (currently 20) for a deeper JSON
sweep.
