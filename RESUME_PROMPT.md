/effort max
/ultracode

Continue HKLII D3 runner work — 6 unmapped slugs (`histlaw`, `hkiac`,
`hklrccp`, `hklrcr`, `pcpdaab`, `pcpdc`). Endpoint probe complete;
next step is the architecture design (task 23).

## Load context via subagents in parallel

Do NOT read these on the main thread — spawn three forks in one
message so the main thread stays lean and cached. Each fork gets a
narrow directive; you get three summaries back.

1. **Fork A — session & corpus state.** Read
   `~/.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/MEMORY.md`
   and the newest entry
   `memory/session-close-2026-07-08-D2-D3-probe.md`. Summarise:
   what D2 shipped, what changed in the corpus, standing rules
   (gateway-only, TDD, no push), the D3 probe findings verbatim,
   and where task 23 left off. Under 250 words.

2. **Fork B — quirks & downloads.** Read
   `docs/freshness-sanity-check.md` and
   `docs/ukpc-manual-download.md`. Summarise the known-permanent
   HKLII quirks + the UKPC external-download plan. Under 200 words.

3. **Fork C — runner patterns.** Read
   `src/hklii_downloader/ukpc.py` (single-pass runner + case-family
   dual write), `src/hklii_downloader/hopt.py` (two-phase runner +
   `hopt_documents` state + `wire_abbr` rewrite), and
   `src/hklii_downloader/legis.py` (chapter+version two-phase).
   Report the three shared shapes we'd build D3 against: URL
   builders, save layouts, checkpoint accessors,
   `mark_bucket_scraped` wire. Under 300 words.

Wait for all three notifications. Do not answer the operator until
grounded.

## Baseline (verify before starting)

```
cd ~/Developer/hklii_downloader
git status                                            # clean apart from scratchpad
git log --oneline origin/main..HEAD | wc -l           # 52 (or 53 if this file is committed)
uv run pytest -q 2>&1 | tail -1                       # 1021 passed

cd ~/Developer/hklii_viewer
git status                                            # RESUME_PROMPT.md dirty, ignore
git log --oneline origin/worktree-local-viewer..HEAD | wc -l  # 59
```

If either drifts, stop and surface.

## D3 probe findings (already done — do NOT re-derive)

| slug     | listing                                | fetch                                       | content |
|----------|----------------------------------------|---------------------------------------------|---------|
| histlaw  | `gethoptfiles?dbcat=H&abbr=histlaw`    | `gethistlaw?abbr=hkhistlaws&year&num`       | PDF |
| hkiac    | `gethoptfiles?dbcat=O&abbr=hkiac`      | `getother?abbr=hkiac&year&num`              | PDF |
| hklrccp  | `gethoptfiles?dbcat=O&abbr=hklrccp`    | `getother?abbr=hklrccp&year&num`            | HTML |
| hklrcr   | `gethoptfiles?dbcat=O&abbr=hklrcr`     | `getother?abbr=hklrcr&year&num`             | HTML |
| pcpdaab  | `gethoptfiles?dbcat=O&abbr=pcpdaab`    | `getother?abbr=pcpdaab&year&num`            | PDF |
| pcpdc    | `gethoptfiles?dbcat=O&abbr=pcpdc`      | `getother?abbr=pcpdc&year&num`              | HTML |
| pd       | `gethoptfiles?dbcat=P&abbr=pd`         | (n/a — HKLII empty)                          | — |

Notes:
- `gethistlaw` needs abbr rewrite `histlaw → hkhistlaws` (mirror
  `hopt.py::wire_abbr` for `bacpg/bahkg → hktba`).
- `getother` is heavily shared — ukpc + all four dbcat=O slugs.
  Pull a shared helper.
- **PDFs are new territory** — existing pipeline handles doc/docx/rtf
  via `generate-html`. Decide in the design: raw `.pdf` save +
  `pdftotext` for search, or `.pdf → .generated.html` conversion.

## Standing rules (non-negotiable)

- **ALWAYS use the 20-proxy VPN pool (`127.0.0.1:8888-8907`)** for
  any HKLII / Judiciary probe. Never direct curl. Runners always
  route through `ProxyPool`.
- TDD strict: failing test → paste output → implement. Two commits
  per pair.
- Never modify a failing test to make it pass. Correct wrong-
  expectation tests in their own `test: correct expectation` commit.
- Do NOT push to origin without ask.
- Docstrings explain WHY, not WHAT. No emojis. Match surrounding
  style.

## Task 23 (in progress) — Design D3 runner architecture

Decide:
1. **One `HoptFamilyRunner`** parameterised by `dbcat` + fetch
   endpoint + PDF-vs-HTML flag, OR **2–3 runners**
   (`HistLawRunner` / `OtherORunner` / `OtherPRunner`).
2. **Storage**: extend `hopt_documents` with a `family` column,
   OR ship a new `d3_documents` table.
3. **PDF handling**: raw save + `pdftotext` for search, OR
   `.pdf → .generated.html` via a converter. Freshness gate counts
   the PDF file as content for those slugs.
4. **CLI**: new `hklii scrape-d3` OR extend `scrape-hopt` with
   `--family` / `--dbcat`.
5. **Update dispatcher**: which `PROFILE_DEFAULTS` includes it,
   how the freshness gate flows through.

Write the design as `docs/d3-runner-design.md` before coding.

## Task 24 (blocked on 23) — Ship the runners TDD

New checkpoint schema. TDD each runner slice. New CLI. Wire into
update dispatcher. Verify freshness buckets flip STALE → FRESH after
a live scrape via the pool.

## Deliberate non-goals this session

- Do NOT ship a BAILII scraper for UKPC — report is manual (see
  `docs/ukpc-manual-download.md`).
- Do NOT generalise the `hkdc/2019/128` Judiciary-DOCX manual fix
  into a global fallback yet — it's one row, documented.
- Do NOT push to `origin`.

## Ready

Run the three forks in one message. When they return, run baseline,
then say "**D2 shipped, D3 probe done — task 23 next**" and start
the design pass.
