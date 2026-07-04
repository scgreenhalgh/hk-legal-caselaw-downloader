# HK Legal Caselaw Downloader

A command-line tool to bulk download judgments from the [Hong Kong Legal Information Institute](https://www.hklii.hk/) (HKLII) via its JSON API. Built for RAG-ready legal-corpus construction.

## Features

- **Bulk scraping** — enumerate + download entire courts with SQLite checkpoint/resume
- **Proxy pool** — multi-worker fan-out (one worker per healthy proxy), preflight IP-leak detection, circuit breaker + cooldown, TLS/HTTP2 impersonation via curl_cffi
- **Bilingual** — parallel English + Traditional Chinese sweeps with dedupe by (court, year, number)
- **Enrichment** — press summaries (EN + ZH) and appeal history, inline during scrape or backfilled post-hoc
- **Verification** — reconcile checkpoint DB against on-disk files (catch rsync dotfile skips, accidental deletes)
- **Multiple formats** — HTML, TXT, JSON metadata, DOC (gated behind `--allow-doc` in bulk mode)
- **Safety rails** — atomic writes with fsync, fcntl exclusive DB lock, PRAGMA integrity_check on open, `--direct` requires explicit confirmation
- Portable environment via [uv](https://docs.astral.sh/uv/)

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/scgreenhalgh/hk-legal-caselaw-downloader.git
cd hk-legal-caselaw-downloader
uv sync
```

## Usage

One of `--proxy` or `--direct` is **required** for every network subcommand. `--direct` exposes your home IP and prompts for confirmation (use `-y` to skip).

### `hklii download` — fetch specific case URLs

```bash
# Single case, direct connection
uv run hklii download --direct https://www.hklii.hk/en/cases/hkcfa/2023/32

# Multiple cases through a SOCKS5 proxy, custom formats
uv run hklii download -p socks5://127.0.0.1:1080 -f html -f txt -f json \
  URL1 URL2 URL3

# Higher concurrency
uv run hklii download -c 10 --direct URL1 URL2 URL3
```

### `hklii scrape` — bulk enumerate + download a court

```bash
# Default courts (hkcfi,hkca,hkdc,hkcfa), both languages, checkpoint auto-resume
uv run hklii scrape -o ./downloads \
  -p http://127.0.0.1:8888 -p http://127.0.0.1:8889 \
  --with-summaries --with-appeal-history

# Cap the run
uv run hklii scrape --limit 500 --courts hkcfa --direct -y

# Retry previously-failed rows
uv run hklii scrape --retry-failed --direct -y

# Skip re-enumeration if court was listed within 24h
uv run hklii scrape --enum-max-age 24 --direct -y

# Snapshot raw getcasefiles JSON for provenance
uv run hklii scrape --save-enum-responses --direct -y
```

Key flags: `--courts`, `--limit`, `--lang {en|tc|both}`, `--with-summaries`, `--with-appeal-history`, `--retry-failed`, `--enum-max-age HOURS`, `--save-enum-responses`, `--allow-doc`, `--resume`.

### `hklii verify` — reconcile DB vs disk

```bash
uv run hklii verify -o ./downloads
```

Flips `downloaded` rows whose files are missing or zero-byte back to `pending` so the next `scrape --resume` re-fetches them.

### `hklii enrich` — backfill press summaries + appeal history

```bash
uv run hklii enrich -o ./downloads --direct -y
```

For cases already downloaded, fetches the press summary (both languages, when available) and appeal history JSON.

### URL Format

```
https://www.hklii.hk/{en|tc}/cases/{court}/{year}/{number}
```

### Output Formats

| Format | Description |
|--------|-------------|
| `html` | Raw judgment HTML content |
| `txt`  | Clean plaintext extracted from HTML |
| `json` | Structured metadata (title, date, citations, court, case number) |
| `doc`  | Original Word document from judiciary.hk (bulk requires `--allow-doc`) |

## HKLII Coverage

HKLII contains **122,460+ judgments** across 14 court databases; the major courts go back to 1946:

| Court | Abbreviation | Years |
|-------|-------------|-------|
| Court of First Instance | hkcfi  | 1946-present |
| Court of Appeal         | hkca   | 1946-present |
| District Court          | hkdc   | 1946-present |
| Court of Final Appeal   | hkcfa  | 1997-present |
| Competition Tribunal    | hkct   | 2020-present |
| Lands Tribunal          | hkldt  | 1990-present |
| Family Court            | hkfc   | ~1980-2020  |

## How It Works

HKLII is a Vue.js SPA. This tool uses the underlying JSON API directly:

- **Listing**: `GET /api/getcasefiles?abbr={court}&itemsPerPage={n}&startIndex={i}`
- **Judgment**: `GET /api/getjudgment?lang={en|tc}&abbr={court}&year={year}&num={num}`
- **Appeal history**: `GET /api/getappealhistory?caseno={caseno}`
- **DOC files**: `legalref.judiciary.hk` (requires browser-like headers)

## License

MIT
