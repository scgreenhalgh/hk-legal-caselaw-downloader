# UKPC manual-download report

Generated 2026-07-08. HKLII lists these 5 UK Privy Council appeals from
Hong Kong but does not host the content: 4 return HTTP 404 on
`getother`, 1 returns HTTP 200 with empty content and no doc pointer.
Confirmed on 2026-07-08 that HKLII has no fallback URL for any of the
five.

Privy Council appeals aren't part of the HK Judiciary organization;
their content lives at:

- **BAILII** — `bailii.org/uk/cases/UKPC/` (primary Commonwealth archive)
- **jcpc.uk** — official Judicial Committee of the Privy Council

Both are behind a Cloudflare-style bot challenge, so an automated
scrape isn't practical. Open each URL in a browser, save the HTML,
drop it into the layout listed below, and re-run the sanity check.

## The five records

The `a`-suffix on the neutral cites means the record is a "supplementary"
or "consequential" decision to the parent — HKLII's URL scheme maps
`/YYYY/N` to whichever record the SPA thinks is authoritative for that
slot. Try `/N.html` first; if it 404s at BAILII, try `/Na.html`.

### 1. [1987] UKPC 3a — Attorney General v Wong Muk Ping

- **Date**: 1987-02-20
- **Title**: *The Attorney General (Appeal No. 16 of 1986) v Wong Muk Ping*
- **HKLII slot**: `ukpc/1987/3`
- **BAILII candidates**:
  - https://www.bailii.org/uk/cases/UKPC/1987/3.html
  - https://www.bailii.org/uk/cases/UKPC/1987/3a.html
- **jcpc.uk search**: https://www.jcpc.uk/decided-cases/index.html?query=Wong+Muk+Ping

### 2. [1988] UKPC 2a — Interlego A.G. v Tyco Industries Inc.

- **Date**: 1988-05-06
- **Title**: *Interlego A.G. v (1) Tyco Industries Inc. (2) Tyco (Hong Kong) Limited (3) The Refined Industry Co. Limited (4) Denifer Technology Limited*
- **HKLII slot**: `ukpc/1988/2`
- **BAILII candidates**:
  - https://www.bailii.org/uk/cases/UKPC/1988/2.html
  - https://www.bailii.org/uk/cases/UKPC/1988/2a.html
- **jcpc.uk search**: https://www.jcpc.uk/decided-cases/index.html?query=Interlego+Tyco

### 3. [1993] UKPC 3a — Philips Hong Kong Ltd v Attorney General

- **Date**: 1993-02-10
- **Title**: *Philips Hong Kong Ltd v The Attorney General of Hong Kong*
- **HKLII slot**: `ukpc/1993/3`
- **BAILII candidates**:
  - https://www.bailii.org/uk/cases/UKPC/1993/3.html
  - https://www.bailii.org/uk/cases/UKPC/1993/3a.html
- **jcpc.uk search**: https://www.jcpc.uk/decided-cases/index.html?query=Philips+Hong+Kong

### 4. [1995] UKPC 4a — Hoecheong Products Co Ltd v Cargill Hong Kong Ltd

- **Date**: 1995-02-03
- **Title**: *Hoecheong Products Co Ltd v Cargill Hong Kong Ltd*
- **HKLII slot**: `ukpc/1995/4`
- **BAILII candidates**:
  - https://www.bailii.org/uk/cases/UKPC/1995/4.html
  - https://www.bailii.org/uk/cases/UKPC/1995/4a.html
- **jcpc.uk search**: https://www.jcpc.uk/decided-cases/index.html?query=Hoecheong+Cargill

### 5. [1997] UKPC 4 — Wharf Properties Ltd v Commissioner of Inland Revenue

- **Date**: 1997-01-28
- **Title**: *Wharf Properties Limited v Commissioner of Inland Revenue*
- **HKLII slot**: `ukpc/1997/4`
- **BAILII candidate**: https://www.bailii.org/uk/cases/UKPC/1997/4.html
- **jcpc.uk search**: https://www.jcpc.uk/decided-cases/index.html?query=Wharf+Properties+Inland+Revenue

## Where to drop the files

Match the case-family layout used by the UKPC scraper (see `hklii/ukpc.py`
`save_ukpc_local`). For each record, save four files under
`output/ukpc/YYYY/`:

```
output/ukpc/1987/ukpc_1987_3.html      # the BAILII/jcpc HTML you saved
output/ukpc/1987/ukpc_1987_3.txt       # plain text — see conversion note
output/ukpc/1987/ukpc_1987_3.json      # metadata sidecar — see schema
output/ukpc/1987/ukpc_1987_3.docx      # optional, if the source is DOCX
```

Text conversion — from the saved HTML:

```bash
uv run python -c "
from hklii_downloader.parser import html_to_text
from pathlib import Path
p = Path('output/ukpc/1987/ukpc_1987_3')
p.with_suffix('.txt').write_text(html_to_text(p.with_suffix('.html').read_text()))
"
```

Metadata sidecar — save this as `output/ukpc/YYYY/ukpc_YYYY_NUM.json`,
substituting the record's values:

```json
{
  "title": "The Attorney General (Appeal No. 16 of 1986) v Wong Muk Ping",
  "neutral_citation": "[1987] UKPC 3a",
  "date": "1987-02-20",
  "abbr": "ukpc",
  "year": 1987,
  "num": 3,
  "lang": "en",
  "url": "https://www.hklii.hk/en/cases/ukpc/1987/3",
  "source": "bailii-manual-download",
  "source_url": "https://www.bailii.org/uk/cases/UKPC/1987/3a.html",
  "backfilled_at": "2026-07-08"
}
```

## Register the row in the checkpoint DB

After the files land on disk, tell the DB the row is downloaded. This
mirrors what `UkpcRunner` does via
`CheckpointDB.upsert_downloaded_case`:

```bash
uv run python -c "
from hklii_downloader.checkpoint import CheckpointDB
db = CheckpointDB('output/.checkpoint.db')
try:
    db.upsert_downloaded_case(
        court='ukpc', year=1987, number=3, lang='en',
        neutral='[1987] UKPC 3a',
        title='The Attorney General (Appeal No. 16 of 1986) v Wong Muk Ping',
        date='1987-02-20',
        formats=['html', 'json', 'txt'],
    )
finally:
    db.close()
"
```

Repeat for each record. Then verify:

```bash
uv run python scripts/freshness_sanity_check.py --output ./output
```

`ukpc/en` should flip from `mismatch(live=242,local=237,delta=+5)` to
FRESH once all five land.

## If BAILII / jcpc.uk truly doesn't have one

Occasionally the Privy Council never published a decision — old
appeals from before the JCPC digitisation drive can be genuinely
un-recoverable. If you exhaust the candidates above without finding
content, leave the record un-fetched and update
`docs/freshness-sanity-check.md` to note the specific record as a
"HKLII lists, no external source" quirk.
