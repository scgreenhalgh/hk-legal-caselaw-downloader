# D3 Runner Architecture — Design (task 23)

## Context

Six HKLII slugs remain unmapped after D2 (live counts from `db_freshness`
as of 2026-07-08 D2 probe):

| slug     | dbcat | fetch endpoint  | content | EN    | TC  | SC  |
|----------|-------|-----------------|---------|------:|----:|----:|
| histlaw  | H     | `gethistlaw`    | PDF     | 3,836 |   0 |   0 |
| pcpdaab  | O     | `getother`      | PDF     |   368 | 368 |   0 |
| hkiac    | O     | `getother`      | PDF     |   190 |   0 |   0 |
| pcpdc    | O     | `getother`      | HTML    |   165 | 165 | 165 |
| hklrcr   | O     | `getother`      | HTML    |   137 | 137 | 137 |
| hklrccp  | O     | `getother`      | HTML    |    78 |  72 |  72 |
| pd       | P     | (n/a)           | —       |     0 |   0 |   0 |

**Scale note.** `histlaw` is 5× larger than the biggest existing HOPT DB
(`hkts` = 266 rows). Enum will paginate meaningfully — see resume /
checkpoint handling in the TDD slice plan.

**Language note.** Three D3 slugs are en-only (`histlaw`, `hkiac`, and
`pcpdaab`'s SC lane); the freshness gate expects the runner to stamp
those buckets FRESH with `local=live=0` after enumeration. Bilingual
coverage varies per slug; `D3_LANGS` includes SC because three slugs
publish Simplified Chinese.

Task 22 confirmed every endpoint responds through the pool. Every row is
currently permanently STALE in `db_freshness` because there is no local
runner filling `hopt_documents` for these abbrs.

`freshness.py` already probes them:

- `legis-histlaw`, `other-O`, `other-P` categories exist
  (freshness.py:81–82).
- All three are stored under `kind='hopt'` in `db_freshness`
  (freshness.py:99–101, checkpoint.py:294).
- Local count is computed via `recompute_local_count(kind='hopt', scope=abbr)`
  which joins on `hopt_documents.abbr = scope`.

**Load-bearing constraint**: any new runner must land its rows in
`hopt_documents WHERE abbr={slug}` so the existing freshness signal flips
STALE → FRESH once local == live. Any other layout requires touching
`recompute_local_count`, and the design goal is to keep that untouched.

`pd` is out of scope — HKLII is empty for it (parity holds vacuously).

## Wire response shapes (observed 2026-07-08)

Every fetch endpoint returns `content-type: application/json`. There is
no raw-bytes response path anywhere. What varies is what's inside the
JSON. Three distinct shapes observed:

### Shape A — `gethistlaw` (histlaw only)

```json
{
  "id": 2148,
  "db": {...},
  "date": "1964",
  "title": "Companies Ordinance(32)",
  "neutral": "[1964] HKHistLaws 1",
  "pdf": "/static/en/histlaw/1964/1.pdf",   // RELATIVE — on hklii.hk
  "path": "/1964/1/",
  "has_translation": false
}
```

No `content` field. No `file_type` field. Fetch is two-hop: metadata
JSON → follow relative `pdf` URL back to `https://www.hklii.hk/static/…`.

### Shape B — `getother` with `file_type=1` (HTML: hklrccp, hklrcr, pcpdc)

```json
{
  "id": 5338,
  "title": "Outcome Related Fee Structures for Arbitration",
  "neutral": "[2020] HKLRCCP 2",
  "date": "2020-12-01",
  "path": "/2020/2/",
  "db": {...},
  "file_type": 1,
  "content": "<script...>...<h3>...</h3><p>On 17 December 2020, ...</p>..."
}
```

`content` holds the embedded document HTML — legally significant text
including page headers, subtitles, prose paragraphs, footnotes. Same
shape as `hopt.gettreaty` responses. No second hop.

### Shape C — `getother` with `file_type=2` (external PDF: hkiac, pcpdaab)

```json
{
  "id": 5400,
  "title": "Playboy Enterprises v. E-MODE LIMITED (playboy.com.hk)",
  "neutral": "[2021] HKIAC 183",
  "date": "2021-10-10",
  "path": "/2021/183/",
  "db": {...},
  "file_type": 2,
  "content": "",
  "pdf": "https://www.hkiac.org/sites/default/files/ck_filebrowser/IP/hk/decision/DHK-2100183_Decision.pdf",
  "has_translation": false
}
```

`content` empty; `pdf` points at an **external** source-org host
(`hkiac.org`, presumably some `pcpd.org.hk` equivalent for pcpdaab).
Fetch is two-hop, and the second hop crosses origins.

### Runtime discriminator

`file_type` only appears on `getother` responses (`gethistlaw` omits
it). The robust discriminator is **presence of a `pdf` field**:

- `pdf` field present → PDF slug, do second-hop mirror.
- Else → HTML slug, save the response JSON as-is (embedded `content`).

Family-label pre-classification (`content_format="pdf"|"html"` on
`D3Family`) is a hint that lets the runner short-circuit lookup and
lets tests parametrise cleanly, but the runtime check is `if "pdf" in
response`. Belt-and-suspenders against a slug that adds a second file
type later.

## The five decisions

### 1. Runner shape — **one parameterised `D3Runner`**

Alternatives considered:

- **A. Fold into `HoptRunner`.** `hopt.py` hard-codes `gettreaty` as
  the fetch endpoint (hopt.py:116) and only speaks JSON-wrapped HTML.
  Adding `gethistlaw` / `getother` + PDF handling would require an
  endpoint dispatch inside a class already covering 5 production abbrs
  (517 rows). Risky for a rewrite; two independent classes is cheaper
  than one class with two personalities.
- **B. Ship 2–3 dedicated runners** (`HistLawRunner` / `OtherORunner` /
  `OtherPRunner`). The shape is genuinely identical — listing via
  `gethoptfiles`, single-fetch per row via a per-family endpoint, save
  under `output/{slug}/…`. Splitting by category would duplicate
  ~200 LOC of loop/checkpoint/retry glue for cosmetic separation.
- **C (chosen). One `D3Runner` parameterised by a family spec.** All
  variation is data: `dbcat`, fetch endpoint, wire-abbr rewrite,
  content format, save extension. Same structural pattern as
  `HoptRunner` (single class, multiple abbrs).

Family spec (frozen dataclass):

```python
# src/hklii_downloader/d3.py

@dataclass(frozen=True)
class D3Family:
    slug: str              # histlaw, hkiac, hklrccp, hklrcr, pcpdaab, pcpdc
    dbcat: str             # H | O
    fetch_endpoint: str    # gethistlaw | getother
    wire_abbr: str         # slug for identity, "hkhistlaws" for histlaw
    content_format: str    # "pdf" | "html" — LISTING-FAMILY HINT ONLY

D3_FAMILIES: tuple[D3Family, ...] = (
    D3Family("histlaw",  "H", "gethistlaw", "hkhistlaws", "pdf"),
    D3Family("hkiac",    "O", "getother",   "hkiac",      "pdf"),
    D3Family("hklrccp",  "O", "getother",   "hklrccp",    "html"),
    D3Family("hklrcr",   "O", "getother",   "hklrcr",     "html"),
    D3Family("pcpdaab",  "O", "getother",   "pcpdaab",    "pdf"),
    D3Family("pcpdc",    "O", "getother",   "pcpdc",      "html"),
)
D3_LANGS = ("en", "tc", "sc")
```

`content_format` is a hint used to size expectations (test parametrisation,
per-family throttle) — the **runtime discriminator** is the presence of a
`pdf` field in the metadata response (see "Wire response shapes"). Belt-
and-suspenders against a slug that starts mixing types.

`D3_LANGS` includes `sc` because `hklrccp`, `hklrcr`, and `pcpdc` publish
Simplified Chinese (mirrors the `LEGIS_LANGS += ("sc",)` decision).

Same wire-abbr pattern as `hopt.wire_abbr()` (hopt.py:96), inline on the
family record instead of a lookup map — only `histlaw` needs a rewrite,
so a map would be single-key and unmotivated.

### 2. Storage — **reuse `hopt_documents` unchanged**

Column-by-column fit:

| column         | value for D3                                 |
|----------------|----------------------------------------------|
| `abbr`         | slug (`histlaw`, `hkiac`, ...) — PK, no clash|
| `year`         | 4-digit int; `nd` never observed on D3 slugs |
| `num`          | int                                          |
| `lang`         | `en` or `tc`                                 |
| `title`        | from listing                                 |
| `neutral`      | from listing (may be NULL for hklrc)         |
| `doc_date`     | from listing                                 |
| `status`       | `pending` → `in_progress` → `downloaded`/`failed` |
| `formats`      | JSON — `["json"]` for HTML slugs; `["json","pdf","txt"]` for PDF slugs (subset if extraction fails) |
| `error`        | error text on failure                        |
| `last_seen_at` | epoch of most recent listing entry           |

Zero schema change. Freshness `recompute_local_count` auto-inherits.
Existing `release_in_progress_hopt` / `claim_pending_hopt` /
`mark_hopt_downloaded` accessors are reusable — the new runner just
calls them.

**Rejected — new `d3_documents` table.** Would require: extending
`_KIND_TO_TABLE` (checkpoint.py:294) with a `d3` kind, adding a
`db_freshness.kind='d3'` bucket, and re-wiring `_rederive_category`
(freshness.py:804). Every one of those is a real change to a table with
a hazard doc (checkpoint.py:238–275, "column ownership is split three
ways"). The single-column-plus-abbr fit is cleaner.

### 3. PDF handling — **two-hop fetch, mirror binary + text sidecar**

Every fetch is JSON. PDF slugs (shape A / shape C) always require a
**second hop** to pull the actual PDF bytes:

1. Hop 1: `gethistlaw` or `getother` returns metadata JSON with a `pdf`
   URL (relative for histlaw, absolute for hkiac/pcpdaab).
2. Hop 2: GET the PDF URL through the pool, mirror the bytes to
   `output/d3/…/{stem}.pdf`.
3. Optional local step: extract text via `pdftotext -layout` to
   `output/d3/…/{stem}.txt` for FTS + RAG.

Alternatives on hop 2 rejected:

- **Don't mirror; store just the metadata `pdf` URL.** External hosts
  (`hkiac.org`, `pcpd.org.hk`) can and will rotate URLs. A stale URL
  in our archive is worse than useless — it lies about coverage.
  Mirroring at scrape time is the only way to keep the archive
  self-contained. `histlaw` PDFs live on hklii.hk itself so the risk
  is lower, but consistency wins.
- **Convert PDF → `.generated.html`** via LibreOffice/pandoc. Layout
  fidelity is bad enough that a viewer would still fall back to
  iframing the PDF. Adds a slow post-process to no benefit.

Save layout:

```
# HTML slugs (hklrccp, hklrcr, pcpdc): shape B, no second hop
output/d3/{slug}/{year}/{num}/{slug}_{year}_{num}_{lang}.json   # full JSON — content embedded

# PDF slugs (histlaw, hkiac, pcpdaab): shapes A / C, two-hop
output/d3/{slug}/{year}/{num}/{slug}_{year}_{num}_{lang}.json   # metadata + original pdf URL
output/d3/{slug}/{year}/{num}/{slug}_{year}_{num}_{lang}.pdf    # mirrored binary
output/d3/{slug}/{year}/{num}/{slug}_{year}_{num}_{lang}.txt    # pdftotext sidecar
```

Rationale for splitting metadata JSON out on PDF slugs (rather than
embedding it): keeps the original `pdf` URL, `id`, `neutral`, `date`,
and `db` provenance grepable without shelling into a binary. Also
lets a viewer render metadata without loading the PDF.

`formats` column tracks which of `pdf` / `txt` / `json` landed. For
HTML slugs: `["json"]`. For PDF slugs with successful mirror +
extraction: `["json", "pdf", "txt"]`. Extraction failure yields
`["json", "pdf"]` — text sidecar is best-effort (see below).

`output/d3/` vs `output/hopt/`: use `output/d3/` as the on-disk root
even though DB rows live in `hopt_documents`. Storage-layer reuse ≠
disk-layout reuse. Prevents a 6-slug append to the `output/hopt` tree
that already documents "5 production DBs".

**Row status semantics**:

- `downloaded` — metadata JSON landed AND, for PDF slugs, PDF binary
  landed. Extraction is best-effort — no `.txt` still counts as
  `downloaded`.
- `failed` — metadata call failed OR (for PDF slugs) PDF binary GET
  failed. `error` records which hop.

Text extraction options considered:

- `pdftotext` (poppler-utils): fast, layout mode preserves columns,
  system dep. Preferred.
- `pypdf` / `pdfminer.six`: pure Python, no system dep, slower and
  weaker on tables.

Wire the runner to `pdftotext` if present, fall back to `pypdf`. A
separate audit CLI (`hklii extract-pdf-text --backfill`) can
regenerate `.txt` later if the extractor changes.

**External-host politeness**: hop 2 for `hkiac` / `pcpdaab` goes to
their source-org hosts. The pool still rotates the exit IP, so no
same-second-thundering-herd, but per-host throttling is worth its own
knob rather than reusing the HKLII rate limit blindly. Default to the
same throttle as HKLII for now; add a per-host override in task 24 if
we hit 429s.

### 4. CLI — **new `hklii scrape-d3` subcommand**

Not `--family` on `scrape-hopt`. Reasons:

- `scrape-hopt` targets treaties / consultation papers (5 abbrs, well
  known to the operator). Overloading it with the D3 family blurs the
  contract.
- The existing shape is one CLI verb per data family (`scrape-cases`,
  `scrape-hopt`, `scrape-legis`, `scrape-ukpc`). `scrape-d3` extends
  the pattern, doesn't break it.
- The dispatcher wires each subcommand independently — merging would
  force a hidden `--family=d3` flag on every `scrape_hopt` step invocation.

Flag surface (mirrors `scrape-hopt` at cli.py:2413):

```
hklii scrape-d3 [-o output] [-p PROXY]... [--direct]
                [--slug histlaw,hkiac,...] [--lang en|tc|both]
                [--limit N] [--yes] [--no-events] [--skip-if-fresh]
```

Defaults: all D3 slugs, both langs. `--skip-if-fresh` reads
`db_freshness` where `kind='hopt' AND scope IN (D3_SLUGS)`. Reuses
`_filter_fresh_hopt_buckets` from cli.py (no new filter function; D3
slugs are just more `kind='hopt'` scopes to the freshness layer).

### 5. Dispatcher — **new `include_d3` flag, monthly + quarterly**

D3 data movement expectations:

- `histlaw` — archive of superseded ordinances; changes only on rare
  errata. Quarterly is enough.
- `hkiac` — arbitration awards; occasional adds.
- `hklrccp`, `hklrcr` — Law Reform Commission papers/reports; occasional.
- `pcpdaab`, `pcpdc` — Privacy Commissioner appeals/cases; occasional.

None warrant daily/weekly. Monthly + quarterly matches `include_hopt`
and `include_legis_history`.

`update.py::PROFILE_DEFAULTS` additions:

```python
"daily":     { ..., "include_d3": False },
"weekly":    { ..., "include_d3": False },
"monthly":   { ..., "include_d3": True  },
"quarterly": { ..., "include_d3": True  },
"custom":    { ..., "include_d3": False },
```

`_STEP_EST["scrape_d3"] = "~14 enum + new-row fetches (10k first run,
deltas after)"`. First-run cost is dominated by `histlaw` (3,836 EN
rows × 2 hops = ~7,700 calls) + `pcpdaab` (368 × 2 langs × 2 hops =
~1,500) + smaller slugs. Steady-state after first full mirror is
delta-only via `--skip-if-fresh`.

Plan step generation (update.py:418 pattern):

```python
if base["include_d3"]:
    plan.append(Step(
        name="scrape_d3",
        kwargs={"skip_if_fresh": True, ...},
    ))
```

`UpdateRunner.dispatch()` learns a `scrape_d3` handler calling into
`cli._run_scrape_d3` — parallel to `_run_scrape_hopt`.

## Freshness gate flow (no change required)

Existing behaviour, verified against freshness.py:

1. `hklii check-freshness` already probes every D3 (slug, lang) pair
   via `getmetahopt?dbcat={H|O}&abbr={slug}` (freshness.py:648–650).
2. Probe writes `live_count` / `live_updated_at` under
   `kind='hopt', scope={slug}, lang={en|tc}`.
3. `recompute_local_count(kind='hopt', scope={slug})` counts
   `hopt_documents WHERE abbr={slug} AND status='downloaded'`.
4. Bucket is FRESH when `local_count == live_count` and
   `last_scrape_completed_at IS NOT NULL`.
5. On first D3Runner run, `mark_bucket_scraped(kind='hopt',
   scope={slug}, langs=[...])` stamps the run — matches how
   `HoptRunResult.langs_enumerated` reports back (Fork C confirmed
   the CLI, not the runner, calls `mark_bucket_scraped`).

Nothing else to wire.

## Runner surface (proposed)

```python
# src/hklii_downloader/d3.py

import re

# Accepts /en|tc/legis/... (histlaw) OR /en|tc/other/... (getother slugs).
# `nd` year token kept for defensive parity with hopt; not observed on
# D3 during probe.
_PATH_RE = re.compile(
    r"^/(?:en|tc)/(?:legis|other)/[a-z]+/(nd|\d{4})/(\d+)/?"
)

@dataclass
class D3Entry:
    year: int
    num: int
    title: str
    neutral: str | None = None
    date: str | None = None

@dataclass
class D3Listing:
    total: int
    entries: list[D3Entry] = field(default_factory=list)

@dataclass
class D3RunResult:
    downloaded: int = 0
    failed: int = 0
    langs_enumerated: dict[str, set[str]] = field(default_factory=dict)
    # slug → set of langs whose enum returned a successful listing.
    # CLI reads this to scope mark_bucket_scraped.

def wire_abbr(family: D3Family) -> str: ...
def gethoptfiles_url(family: D3Family, lang: str, page: int, ...) -> str: ...
def fetch_url(family: D3Family, year: int, num: int, lang: str) -> str:
    """Hop-1 URL — metadata JSON."""

def pdf_url(family: D3Family, response: dict) -> str | None:
    """Hop-2 URL — resolves the ``pdf`` field in the metadata response.

    Absolute for hkiac/pcpdaab (already full URL). Relative for
    histlaw (`/static/en/histlaw/1964/1.pdf` → joins to
    `https://www.hklii.hk/static/…`). Returns None if the response
    has no `pdf` field (HTML slugs, shape B).
    """

def save_d3_html(output_dir: Path, family: D3Family,
                 year: int, num: int, lang: str,
                 response: dict) -> list[str]:
    """Shape B — write the metadata JSON as-is. Returns ['json']."""

def save_d3_pdf(output_dir: Path, family: D3Family,
                year: int, num: int, lang: str,
                metadata: dict, pdf_bytes: bytes,
                extracted_text: str | None) -> list[str]:
    """Shapes A/C — write metadata JSON, PDF binary, optional .txt.
    Returns some subset of ['json', 'pdf', 'txt']."""

def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """pdftotext preferred, pypdf fallback, returns None on failure."""

class D3Runner:
    def __init__(self, output: Path, pool: ProxyPool,
                 db: CheckpointDB, ...): ...
    async def enumerate(self, families, langs) -> None: ...
    async def fetch(self, limit: int | None) -> None:
        """Two-hop per row on PDF slugs; single-hop on HTML slugs."""
    async def run(self, families, langs, limit) -> D3RunResult: ...
```

Two-phase (enumerate → fetch), matching `hopt.HoptRunner`. Two save
helpers (`save_d3_html` / `save_d3_pdf`) instead of a bytes-or-dict
union — the discriminator is discovered at fetch time (`pdf` field
present), and splitting the two write paths keeps each signature
type-tight.

## TDD slice plan for task 24

Each slice is one failing test → one impl commit; every commit runs
through the pool.

1. **`d3.wire_abbr` per family** — one param test spanning the 6 slugs.
2. **`d3.gethoptfiles_url` per family** — dbcat correctness (H vs O).
3. **`d3.fetch_url` per family** — endpoint routing + histlaw rewrite
   (`abbr=hkhistlaws`).
4. **`d3._PATH_RE` accepts `/legis/` OR `/other/`** — param test with
   histlaw + hklrccp + hkiac fixtures. NOT a reuse of hopt's regex
   (which is `/legis/` only).
5. **`d3.parse_files_response`** — 3 real fixtures (histlaw, hklrccp,
   hkiac), skip-log invariant on malformed paths.
6. **`d3.pdf_url` — external absolute** — hkiac response returns
   full `https://www.hkiac.org/...pdf` unchanged.
7. **`d3.pdf_url` — hklii-relative** — histlaw response `/static/…` is
   joined to `https://www.hklii.hk/static/…`.
8. **`d3.pdf_url` — HTML slug returns None** — hklrccp fixture with no
   `pdf` field.
9. **`d3.save_d3_html`** — writes only `.json`, returns `["json"]`.
10. **`d3.save_d3_pdf`** — writes `.json`, `.pdf`, `.txt` when
    extraction succeeds; `["json", "pdf"]` when text is None.
11. **`d3.extract_pdf_text`** — pdftotext-preferred, pypdf fallback,
    returns None on failure.
12. **`D3Runner.enumerate`** — single-slug, single-lang, replay probe
    fixture, upserts `hopt_documents` rows.
13. **`D3Runner.fetch` — HTML slug happy path** — hklrccp fixture,
    single hop, marks `downloaded`, `formats=["json"]`.
14. **`D3Runner.fetch` — PDF slug happy path** — histlaw fixture, two
    hops (metadata + PDF bytes), marks `downloaded`,
    `formats=["json", "pdf", "txt"]`.
15. **`D3Runner.fetch` — hop-1 failure** — 404 on metadata JSON,
    marks `failed`, `error` records hop.
16. **`D3Runner.fetch` — hop-2 failure** — metadata OK but PDF URL
    404s, marks `failed`, error identifies hop-2 with URL.
17. **`D3Runner.fetch` — non-JSON body / empty content** — marks
    `failed` with descriptive error.
18. **`D3Runner.run` result surface** — `langs_enumerated` reflects
    which (slug, lang) actually yielded a listing (including
    `totalfiles=0` for known en-only slugs).
19. **CLI `hklii scrape-d3`** — flag parsing, `--skip-if-fresh` drops
    fresh buckets, `--direct` confirmation, no-proxy guard.
20. **CLI `--skip-if-fresh` short-circuit** — returns without a wire
    call when every requested slug is fresh.
21. **Dispatcher wiring** — `PROFILE_DEFAULTS` monthly/quarterly
    `include_d3=True`; plan contains `scrape_d3` step; `_STEP_EST`
    row exists.
22. **Freshness end-to-end via test doubles** — after a D3Runner run,
    `db_freshness` row for that (slug, lang) has both `local_count`
    and `last_scrape_completed_at` populated; state transitions
    STALE → FRESH. Include a `local=live=0` bucket (e.g. histlaw/tc)
    to cover en-only slugs.
23. **Live gate flip** (post-code) — real scrape via pool of one small
    slug (`hklrccp` en, 78 rows), confirm `hklii check-freshness
    --report` shows the row FRESH. Follow with a `histlaw` sample of
    ~20 rows to smoke-test the two-hop path on same-origin PDFs, then
    an `hkiac` sample of ~20 to smoke-test cross-origin PDFs.

## Non-goals for this task

- **`pd`** — HKLII empty; parity holds vacuously. No runner scaffolding.
- **Auto text extraction backfill** — separate CLI, out of task 24.
- **PDF-to-HTML conversion path** — deliberately rejected; do not add.
- **Viewer surfaces** — new PDF rendering happens in the viewer repo,
  not here. Downloader ships the artifacts.
- **UKPC generalisation** — the `getother` helper is shared with UKPC
  at the wire level, but UKPC's checkpoint model is single-pass
  (`cases` table); do NOT refactor UKPC to share the D3 runner. The
  overlap is one URL builder; extract that only.

## Open questions to resolve before task 24 starts

- **`getother` shared helper location**: put `getother_url` in
  `d3.py`, or promote to a shared `_endpoints.py` module and let
  `ukpc.py` import it? The UKPC callsite (ukpc.py:109) is already
  written; refactoring UKPC to import is a behaviour-neutral cleanup.
  Recommendation: keep `getother_url` in `d3.py` initially, migrate
  UKPC in a follow-up commit if desired. Preserves task-24 scope.
- **External-host throttle for hop-2 PDFs**: `hkiac.org` and (presumed)
  `pcpd.org.hk` may have different rate limits than hklii.hk. First
  run defaults to HKLII's throttle; if we see 429s on hop-2, add a
  `_HOST_THROTTLE` map and per-host token bucket. Do not ship the
  override until we've observed a real throttling event.
- **SC as default lang**: `D3_LANGS` includes `sc`, matching the recent
  `LEGIS_LANGS += ("sc",)` change. Three D3 slugs publish SC
  (`hklrccp`, `hklrcr`, `pcpdc`); the others will mark SC buckets
  FRESH with `local=live=0`. Recommendation: ship as default (no
  `--include-sc` gate) since the freshness gate handles empty-lang
  cleanly and the wire cost is one enum call per empty slug×lang.
- **`pypdf` as optional dep**: hard-require or soft? Recommendation:
  soft — extractor tries pdftotext, falls back to pypdf, logs a
  warning if neither is available. The row still lands as
  `downloaded` (the `.pdf` binary is the source of truth); missing
  `.txt` becomes a viewer / FTS problem, not a runner problem.
- **Metadata JSON on PDF rows — full or trimmed?** The `getother`
  shape-C response has 8 fields we'd store verbatim. For consistency
  with hopt's `save_hopt_local` (which stores the full response), do
  the same. Do not filter fields at save time — a viewer / audit tool
  may need `db.id` or the original `path` string later.
