# HK Legal Caselaw Downloader

A command-line tool to bulk download judgments from the [Hong Kong Legal Information Institute](https://www.hklii.hk/) (HKLII) via its JSON API.

## Features

- Download judgments in multiple formats: **HTML**, **TXT**, **JSON** metadata, and **DOC** (from judiciary.hk)
- Concurrent async downloads with configurable parallelism
- SOCKS5 and HTTP proxy support
- Clean plaintext extraction from judgment HTML
- Portable environment via [uv](https://docs.astral.sh/uv/)

## Installation

Requires Python 3.11+.

```bash
# Clone and install with uv
git clone https://github.com/scgreenhalgh/hk-legal-caselaw-downloader.git
cd hk-legal-caselaw-downloader
uv sync
```

## Usage

```bash
# Download a single case
uv run hklii https://www.hklii.hk/en/cases/hkcfa/2023/32

# Download multiple cases concurrently
uv run hklii URL1 URL2 URL3

# Specify output directory and formats
uv run hklii -o ./judgments -f html -f txt -f json URL

# Use a proxy
uv run hklii -p socks5://127.0.0.1:1080 URL

# Control concurrency (default: 5)
uv run hklii -c 10 URL1 URL2 URL3
```

### URL Format

HKLII case URLs follow this pattern:

```
https://www.hklii.hk/{en|tc}/cases/{court}/{year}/{number}
```

For example:
- `https://www.hklii.hk/en/cases/hkcfa/2023/32` (Court of Final Appeal)
- `https://www.hklii.hk/en/cases/hkcfi/2021/3350` (Court of First Instance)

### Output Formats

| Format | Description |
|--------|-------------|
| `html` | Raw judgment HTML content |
| `txt` | Clean plaintext extracted from HTML |
| `json` | Structured metadata (title, date, citations, court, case number) |
| `doc` | Original Word document from judiciary.hk |

## HKLII Coverage

HKLII contains **122,460+ judgments** across 14 court databases, with the major courts going back to 1946:

| Court | Abbreviation | Years |
|-------|-------------|-------|
| Court of First Instance | hkcfi | 1946-present |
| Court of Appeal | hkca | 1946-present |
| District Court | hkdc | 1946-present |
| Court of Final Appeal | hkcfa | 1997-present |
| Competition Tribunal | hkct | 2020-present |
| Lands Tribunal | hkldt | 1990-present |
| Family Court | hkfc | ~1980-2020 |

## How It Works

HKLII is a Vue.js single-page application. This tool uses its underlying JSON API directly rather than scraping rendered HTML:

- **Judgment API**: `GET https://www.hklii.hk/api/getjudgment?lang={en|tc}&abbr={court}&year={year}&num={num}`
- **Appeal history**: `GET https://www.hklii.hk/api/getappealhistory?caseno={caseno}`
- **DOC files**: Hosted on `legalref.judiciary.hk` (requires browser-like User-Agent headers)

## License

MIT
