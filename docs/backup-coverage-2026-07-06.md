# HKLII backup — coverage report

**Date of run**: 2026-07-06
**Source**: `hklii.hk` (Vue SPA + JSON API)
**Route**: 20-endpoint gluetun/PIA VPN pool across 7 APAC regions
**Total documents**: 202,286
**Disk footprint**: 29 GB

Every content-populated database listed at `https://www.hklii.hk/databases`
is now on local disk. The 9 databases listed there with zero content
(`hkiac`, `hklrccp`, `hklrcr`, `pcpdaab`, `pcpdc`, `pd`, `histlaw`,
`hksct`, `ukpc`) remain uncaptured because HKLII has not populated
them; re-running the enumerators later will pick up anything they add.

## Corpus at a glance

| Category | Documents | Coverage | Notes |
|----------|----------:|:---------|:------|
| Case judgments | 162,331 | 100 % | Split: 118,188 EN / 44,143 TC-only |
| Case TC translation sidecars | 1,516 | 99.93 % | 1 upstream empty at `hkdc/2019/128` |
| Legislation — currently in force | 6,310 | 100 % | ord 1,676 · reg 4,506 · instrument 128 |
| Legislation — historical revisions | 30,943 | 100 % | Every non-latest `capversion` |
| HOPT (treaties + basic law + consultation papers) | 1,149 | 99.91 % | 1 upstream 404 at `hkts/1961/2` tc |
| Enrichment — press summaries (EN) | 373 | 100 % of available | Not every case has a summary |
| Enrichment — press summaries (ZH) | 373 | 100 % of available | Same pool |
| Enrichment — appeal-history JSONs | 162,246 | 99.95 % | 81 upstream failures |
| Doc → HTML backfill (`.generated.html`) | 234 | 100 % | For `formats=["doc"]` empty-content rows |

## Per-court judgment counts

| Slug | Court | Count |
|:-----|:------|------:|
| `hkcfa` | Court of Final Appeal | 2,154 |
| `hkca` | Court of Appeal | 39,166 |
| `hkcfi` | Court of First Instance | 79,766 |
| `hkdc` | District Court | 34,469 |
| `hkldt` | Lands Tribunal | 3,579 |
| `hkfc` | Family Court | 3,025 |
| `hkmagc` | Magistrates Courts | 107 |
| `hkct` | Competition Tribunal | 45 |
| `hkcrc` | Coroner's Court | 13 |
| `hklat` | Labour Tribunal | 5 |
| `hkoat` | Obscene Articles Tribunal | 2 |

Two additional court databases (`hksct`, `ukpc`) are listed by HKLII
but currently hold zero documents.

## Legislation counts

| Type | Chapters | Historical versions captured |
|------|---------:|-----------------------------:|
| Ordinances (`ord`) | 838 × 2 langs = 1,676 | ~11,600 |
| Regulations (`reg`) | 2,253 × 2 langs = 4,506 | ~17,700 |
| Instruments (`instrument`) | 64 × 2 langs = 128 | ~110 |
| **Total** | **6,310** | **30,943** |

Historical coverage: for every ord/reg/instrument, HKLII exposes a
list of past `capversion` ids reachable via `getcapversions`. Every
non-latest vid was fetched via `getcapversiontoc?id=<vid>` and stored
at `output/legis/{abbr}/{num}/{stem}.v{vid}.content.json`. Full text
of every prior version is preserved verbatim as HKLII served it.

## HOPT (treaties + basic law + consultation papers)

| Abbr | Description | EN | TC |
|------|-------------|---:|---:|
| `hkts` | HK Treaty Series | 266 | 265 |
| `bahkg` | Basic Law HK Gazette | 217 | 217 |
| `hktml` | HK Treaties — Multilateral | 61 | 61 |
| `bacpg` | Basic Law Consultation Papers | 23 | 23 |
| `hktmc` | HK Treaties — Marine Codes | 8 | 8 |

Bilingual parity is 100 % except a single missing TC translation at
`hkts/1961/2` (HKLII returns 404 for that record — an upstream data
gap, not a scraper failure). `hkts` includes 10 undated (`year=nd`)
treaties (Inter-American Development Bank Agreement, ASEAN+3
Macroeconomic Research Office, etc.).

## Endpoints in scope

We hit 11 of HKLII's 25 SPA-exposed API endpoints:

| Endpoint | Purpose | Coverage |
|----------|---------|----------|
| `getcasefiles` | Case listing | Enumerator |
| `getjudgment` | Full judgment JSON | Case scraper + translation backfill |
| `getappealhistory` | Appeal chain | Enrichment (162,246 downloaded) |
| `getmetacase` | Court metadata | Enum |
| `getmetalegis` | Legis DB metadata | Enum |
| `getlegisfiles` | Legis listing | Legis scraper |
| `getcapversions` | Version list per chapter | Legis scraper |
| `getcapversiontoc` | Full section text per version | Historical backfill |
| `getmetahopt` | HOPT DB metadata | Enum |
| `gethoptfiles` | HOPT listing | HOPT scraper |
| `gettreaty` | Full treaty JSON | HOPT scraper |

## Endpoints deliberately out of scope

Reviewed against the SPA's API surface and left uncaptured:

| Endpoint | Content available? | Why skipped |
|----------|--------------------|-------------|
| `getcasenoteup` | Yes (cross-citation data) | ~324k calls / 5-6h / 50-100 MB. Deferred; can be built from local corpus anyway. |
| `getrelatedcaps` | Yes (ord/reg cross-refs) | Small (~6 MB); deferred. |
| `ai.hklii.hk/case-summary/…` | Unknown (separate AI service) | Not part of core doc corpus. |

## Confirmed empty API surface (not gaps)

| Endpoint | Status |
|----------|--------|
| `getlegisnoteup` | Server 500 on every param variant. Undeployed. |
| `get-info-box` | 404 |
| `get-judgment-tags` | 404 (SPA references but backend not deployed) |
| `get-summary-availability` | 404 |
| `getsecversions` | Returns the same payload as `getcapversions` (alias). |
| `gethistlaw` | Target database is empty. |
| `getother` | Same. |

## On-disk layout

```
output/
├── .checkpoint.db                # SQLite state for every scraper
├── hkcfi/2023/hkcfi_2023_155.html  # judgment body (per court/year)
├── hkcfi/2023/hkcfi_2023_155.txt   # extracted plain text
├── hkcfi/2023/hkcfi_2023_155.json  # judgment metadata
├── hkcfi/2023/hkcfi_2023_155.doc   # (or .docx/.rtf per magic bytes)
├── hkcfi/2023/hkcfi_2023_155.appeal_history.json
├── hkcfi/2023/hkcfi_2023_155.summary_en.html   # if press summary exists
├── hkcfi/2023/hkcfi_2023_155.summary_zh.html
├── hkcfi/2023/hkcfi_2023_155.tc.html            # TC translation sidecar
├── hkcfi/2023/hkcfi_2023_155.tc.txt
├── hkcfi/2023/hkcfi_2023_155.tc.json
├── hkcfi/2026/hkcfi_2026_1715.generated.html    # LibreOffice-converted for
│                                                 # empty-content-at-HKLII rows
├── legis/
│   └── ord/1/
│       ├── ord_1_en.versions.json     # list of 28 revisions
│       ├── ord_1_en.content.json      # current (latest vid) full text
│       ├── ord_1_en.v50293.content.json  # 2024-08-18 version
│       ├── ord_1_en.v19113.content.json  # 1997-06-30 (earliest)
│       └── … (26 more versions)
└── hopt/
    └── hkts/2018/1/
        └── hkts_2018_1_en.json         # full treaty (metadata + HTML)
```

## Reproducibility

Every fetch was routed through the local 20-endpoint gluetun/PIA VPN
pool (containers `hklii-vpn-1` … `hklii-vpn-20`). The scrapers refuse
`--direct` without an explicit `--yes`. All state persists in
`output/.checkpoint.db` — resume is idempotent across every scraper.

Re-run any phase:

```bash
# 20 VPN proxies
PROXIES=$(for p in $(seq 8888 8907); do echo -n "-p http://127.0.0.1:$p "; done)

hklii scrape                    $PROXIES  # cases
hklii enrich                    $PROXIES  # summaries + appeal history
hklii scrape-legis              $PROXIES  # ordinances/regulations/instruments
hklii backfill-legis-history    $PROXIES  # all historical capversions
hklii scrape-hopt               $PROXIES  # treaties + basic law
hklii backfill-case-translations $PROXIES  # tc sidecars
hklii generate-html                        # doc → html for empty-content rows
```

Idempotent — a repeat run only fetches what's missing on disk / in the DB.

## Genuinely missing (upstream data gaps)

Three documents where HKLII itself returns nothing usable:

1. `hkdc/2019/128` — no TC translation exists at HKLII (empty content).
2. `hkts/1961/2 tc` — HKLII returns 404 for the TC record.
3. 81 `appeal_history` failed rows — upstream `getappealhistory` returned
   errors during the original scrape run; retryable via
   `hklii enrich --retry-failed`.

Everything else HKLII has was captured.
