# HKLII Downloader — Research Documentation

This directory is the single source of truth for how the HKLII downloader works, why it is built the way it is, and what the target platform actually looks like on the wire. Every non-trivial architectural decision, every anti-detection layer, every empirical probe result, and every operational workflow is documented here — grounded in `file:line` citations into `src/hklii_downloader/` and reproducible probes against the live origins.

The chapters are designed as one-topic-per-file with strict single-source-of-truth ownership. When a topic appears in more than one chapter, one chapter *owns* the material and the others cross-reference it. Cross-references use relative links (`./NN-slug.md`) so the docs render correctly on GitHub, on the local filesystem, and inside any editor. Read the chapters in numerical order for the intended narrative arc; skim by table of contents when you know what you are looking for.

## Table of contents

Read in order for the intended narrative arc.

| # | Chapter | Summary |
|---|---------|---------|
| 01 | [HKLII Platform](./01-hklii-platform.md) | HKU-Law Vue SPA served by bare gunicorn/Django on HTTP/2, no CDN or WAF as of 2026-07-04. Corpus is 118,188 judgments across 13 API-alive court slugs (homepage counter claims 122,460 — arithmetic delta explained in-chapter). |
| 02 | [Judiciary Platform](./02-judiciary-platform.md) | Authoritative `.docx` source at `legalref.judiciary.hk` (F5 load-balanced Apache) with full HTTP caching contract (ETag, Last-Modified, Accept-Ranges) and press-summary hosting. |
| 03 | [HKLII API Endpoint Reference](./03-endpoint-reference.md) | Wire-format truth for every HKLII JSON endpoint we call — request shape, response envelope, per-court corpus counts, and the `itemsPerPage=10000` 500-response quirk on 7 of 14 courts. |
| 04 | [Anti-Detection Strategy](./04-anti-detection-strategy.md) | Threat model, three-tier detection framework, 12-signal suspicion catalogue, and the signal-to-defense map anchoring every layer chapter that follows. |
| 05 | [HTTP Headers Reference](./05-http-headers.md) | Complete header composition — Chrome UA pool, OS matrix, `sec-ch-ua` GREASE format, Sec-Fetch XHR-vs-navigation split, and `parser.referer_for` derivation logic. |
| 06 | [TLS + HTTP/2 Fingerprinting](./06-tls-http2-fingerprinting.md) | Why JA3 died and JA4 replaced it, the curl_cffi profile pool (`chrome/chrome146/142/136/131`), HTTP/2 SETTINGS and pseudo-header order, and the deterministic per-proxy profile assignment. |
| 07 | [Cookies, Sessions, and Warm-Up](./07-cookies-sessions-warmup.md) | Per-proxy cookie jar lifecycle, `_IP_ECHO_URLS` preflight, runtime IP re-check, session warm-up (kills suspicion signal 4), and the `ProxySession` circuit breaker. |
| 08 | [VPN Pool Architecture](./08-vpn-pool.md) | 20-container gluetun + PIA topology across seven Asian regions, `SERVER_NAMES` pinning rationale, `expand_vpn_pool.py`, DNS leak safety, and measured per-region speed data. |
| 09 | [Scraper Architecture](./09-scraper-architecture.md) | Twelve-module code map — CLI, enumeration, download loop, retry backoff, `RequestThrottler`, `CheckpointDB` schema, atomic writes with `fsync`-parent, and the enrichment split. |
| 10 | [Content-Shape Safeguards](./10-content-safeguards.md) | S-1 challenge-page detection (13 bilingual markers), empty-content vs `.docx` fallback branching, the `hklii verify` reconciliation, and the residual validation gaps. |
| 11 | [Operations Runbook](./11-operations-runbook.md) | How to actually run the scraper — `hklii download/scrape/verify/enrich` flag inventories, canary patterns, resume workflows, wall-clock estimates, and the pre-flight checklist. |
| 12 | [Architectural Decisions Log](./12-decisions-log.md) | Every non-trivial architectural choice as a `context / decision / alternatives / data / date / cross-ref` entry — the meta-narrative behind the earlier chapters. |

## Start here (new contributors)

Read **[Chapter 01 — HKLII Platform](./01-hklii-platform.md)** first. It establishes what HKLII is, how it is served, what the corpus looks like, and — critically — what protections the target does *not* have. Everything downstream (why we impersonate Chrome, why we rotate proxies, why we throttle) makes more sense once you know the baseline is a bare gunicorn origin with no CDN, no WAF, and no rate-limit hints.

From there, walk chapters 02 through 12 in numerical order. The reading path is:
- **01, 02, 03** — what we are pulling from
- **04** — the philosophical center: threat model + 12 suspicion signals
- **05, 06, 07, 08** — one chapter per anti-detection layer
- **09, 10** — the actual code (scraper flow + content validation)
- **11** — how to run it
- **12** — every "why did we choose X" answered

If you plan to write code, chapters **04, 09, 12** together carry the load-bearing decisions.

## For reviewers and auditors

**Detection posture:** [Chapter 04 (Anti-Detection Strategy)](./04-anti-detection-strategy.md) is the authoritative statement of the threat model, the three-tier detection framework, the 12 concrete suspicion signals we defend against, and the signal-to-defense map anchoring every layer chapter. Read this first, then follow the cross-references into 05, 06, 07, 08 for the layer implementations, and 10 for content-shape validation.

**Empirical evidence:** the on-the-wire baseline data lives in:
- [Chapter 01](./01-hklii-platform.md) — server-stack probes and the five-probe reproducibility script
- [Chapter 02](./02-judiciary-platform.md) — the Judiciary origin caching-contract probes (ETag round-trip, `Range` request)
- [Chapter 03](./03-endpoint-reference.md) — endpoint envelopes, per-court corpus counts, the `itemsPerPage=10000` per-court 500 quirk with real header/body dumps
- [Chapter 08](./08-vpn-pool.md) — the 6-region speed matrix and the 20-pool canary result

Every code claim across all 12 chapters is anchored to a `file:line` reference in `src/hklii_downloader/` — grep the chapters for `.py:` to find any specific defense's implementation quickly.

**Decisions requiring approval:** [Chapter 12 (Architectural Decisions Log)](./12-decisions-log.md) enumerates every non-trivial choice with the alternatives that were considered, the data that motivated the decision, and the date. The "Deliberate non-decisions" section at the end lists explicitly-deferred items (robots.txt review cadence, LawCite citation graph, canary automation, HKT scheduling) that would benefit from follow-up.

## Topic index — cross-chapter roll-ups

Some topics span more than one chapter by design (each chapter owns a facet). Quick locator:

**Enrichment pipeline (press summaries + appeal history):**
- URL patterns for Judiciary-hosted press summaries — [02](./02-judiciary-platform.md)
- Inline (via scrape flags) vs backfill (via `hklii enrich`), per-kind status columns, `EnrichmentRunner` — [09](./09-scraper-architecture.md)
- Sidecar file layout on disk + missing sidecar verification gap — [10](./10-content-safeguards.md)
- CLI flags `--with-summaries` / `--with-appeal-history` / the `hklii enrich` subcommand — [11](./11-operations-runbook.md)
- Decision to allow both inline and backfill paths — [12](./12-decisions-log.md)

**Content vs doc-fallback branching:**
- Empty `content_html` empirical prevalence on recent 2026 judgments — [01](./01-hklii-platform.md), [02](./02-judiciary-platform.md)
- Scraper branch logic, `--allow-doc`, `.docx` handling at `scraper.py:350` — [09](./09-scraper-architecture.md), [10](./10-content-safeguards.md)

**Retry + throttling:**
- Signal-3 volumetric threat and the pool defense — [04](./04-anti-detection-strategy.md)
- `RequestThrottler` distribution and `_jittered_backoff` numeric parameters — [09](./09-scraper-architecture.md) (authoritative source)
- Per-proxy throttler as a warm-up preamble consideration — [07](./07-cookies-sessions-warmup.md) (references only)

## Related

- [Main repo README](../README.md) — installation, quick-start, CLI overview, and Docker/VPN setup
- `src/hklii_downloader/` — the code these chapters document
- `docker-compose.yml` — the 20-container gluetun + PIA proxy pool
- `scripts/expand_vpn_pool.py` — the tool documented in Chapter 08

## Known documentation follow-ups

Post-authoring accuracy + coverage review completed 2026-07-04. Zero blocker findings; the important items below were addressed inline before publish. What remains is minor.

**Fixed:**
- Chapter 04's per-IP volumetric arithmetic (Signal 3 defense, rule L-6) now correctly states that the throttler is per-proxy — each of 20 proxies runs at ~1730 req/hr independently, aggregate is ~34,600 req/hr but no single source IP crosses the per-IP threshold.
- Chapter 01's CSP block replaced with the verbatim wire bytes from `scratchpad/hdr_s1000.txt:8` (adds `script-src-elem`, `media-src`, corrects `font-src` and `frame-ancestors`).
- Chapter 08's 20-pool canary throughput corrected from 3,000/hr (which was the 6-proxy number) to 6,900/hr peak (100 files / 52 s), matching Chapter 11.
- All 10 dead-anchor cross-refs (4 in Chapter 04, 4 in Chapter 12, 2 in Chapter 06) now use full-chapter links with prose section pointers, so search / ctrl-F lands.
- `file:line` drift on the M-4 warm-up (Chapter 04) corrected to `proxy_pool.py:292-304`.
- Chapter 02's `.doc` hardcode cross-ref now points at Chapter 10 (which owns it), not Chapter 11.
- Chapter 01's corpus-size summary in this README's TOC updated to 118,188 across 13 slugs (matching the empirical probe) — homepage counter of 122,460 is called out as a delta explained in-chapter.
- Enrichment cross-chapter roll-up added to the "Topic index" section above so a reader wanting the end-to-end enrichment story has a single entry point.

**Remaining:** None.

Every accuracy + coverage finding raised by the post-authoring review has been addressed. The four nits from the earlier pass have all been fixed inline:
- Chapter 06's bare-`chrome` alias resolution now correctly attributed to `AsyncSession` construction (`impersonate_client.py:53-55`), not import time.
- Chapter 08's `docker-compose.yml` service range corrected to `38-236` (where `vpn-1:` opens), not `43-236` (which was the first `SERVER_REGIONS` line).
- Chapter 11 now includes a "Consuming `.enum_cache/` snapshots with `jq`" section with six worked recipes (counter drift, page-length sanity, neutral dedupe across pages, freshness monitoring, run-to-run diff, parallel-array probe).
- `RequestThrottler` numeric parameters now live only in Chapter 09 (authoritative). Chapter 01 and Chapter 04 previously duplicated them; both have been reduced to cross-references pointing at Chapter 09's "RequestThrottler formula" section.
