# 13 — Observability: `events.jsonl` + failure samples

The human `scrape.log` (Chapter 11) answers *"is anything on fire right now?"*.
This chapter covers the machine-readable companion — `<output>/events.jsonl`
plus `<output>/failure_samples/` — which answers *"what actually happened
across 228K requests and 20 proxies over the last 18 hours?"*. Grep cannot
give you per-proxy success rates, hourly failure trajectories, or a WAF
fingerprint diff; `jq` over a structured event stream can.

Both artifacts are written by `StructuredEventLogger`
(`src/hklii_downloader/events.py`), constructed by the `scrape` / `enrich` /
`recheck-html` subcommands from the `-o` output directory. Pass `--no-events`
to disable it entirely on storage-constrained runs.

## Where it comes from

| Artifact | Path | Written by |
|---|---|---|
| Event stream | `<output>/events.jsonl` | every hook site below, via `emit()` |
| Raw WAF/error bodies | `<output>/failure_samples/<sig>.html` | `sample_failure()` |
| Sample metadata + headers | `<output>/failure_samples/<sig>.headers.json` | `sample_failure()` |

## Durability + backpressure contract

`events.jsonl` is append-only. Each row is one full line (`payload` + `\n`)
written in a single `os.write` to an `O_APPEND` file descriptor, so a
SIGKILL mid-run lands *between* lines — never inside one — and `jq -c` never
chokes on a torn final row. Writes are drained by a single background writer
coroutine behind a bounded queue: a slow disk stalls the writer, not the
scraper's worker coroutines. If the disk cannot keep up and the queue
overflows, rows are **dropped** (count-only) with a throttled `WARNING` in
`scrape.log` rather than blocking the event loop. In other words, under disk
pressure the analytics degrade before the scrape does — treat a non-zero drop
warning as "trust the checkpoint DB over `events.jsonl` for exact counts".

## Row schema

Every row carries `ts` (ISO-8601, UTC) and `kind`. All other fields are
optional and **omitted when null** (so `jq` `select(.field)` is a presence
test). The superset:

```
ts            ISO-8601 UTC timestamp (always present)
kind          event kind (always present; table below)
court year num  case identity (hkcfi / 2023 / 1)
proxy_url     the serving proxy ("direct" in --direct mode)
url           request URL
http_status   HTTP status code
elapsed_ms    request wall time in ms
error_class   short greppable failure bucket (first token up to :;,)
error_msg     full failure string
response_len  content_html length (challenge events)
retry_attempt attempt index when the failure was terminal
extra         object — kind-specific payload (e.g. {"observed_ip": ...})
```

### Event kinds

| kind | Emitted at | Key fields |
|---|---|---|
| `request_success` | `ProxyPool.get` — acceptable status | `proxy_url`, `url`, `http_status`, `elapsed_ms` |
| `request_failed` | `ProxyPool.get` — 403/429/5xx or `RequestError` | `proxy_url`, `url`, `http_status` **or** `error_class`, `elapsed_ms` |
| `case_failed` | `BulkScraper._fail` — terminal case failure | `court/year/num`, `error_class`, `error_msg`, (`url`, `http_status`, `retry_attempt`) |
| `challenge_detected` | scraper — WAF interstitial in `content_html` | `court/year/num`, `proxy_url`, `url`, `http_status`, `response_len` (+ failure sample) |
| `pool_exhausted` | scraper — `AllProxiesDeadError` mid-run | `court/year/num`, `error_class` = `pool-exhausted` |
| `warmup` | `ProxyPool._warm_up_target` | `proxy_url`, `url`, `elapsed_ms` |
| `ip_echo` | `ProxyPool._fetch_ip` | `proxy_url` (or `direct`), `url`, `extra.observed_ip` |
| `degraded` | `ProxyPool._runtime_ip_check` — both echoes blipped | `proxy_url`, `error_class`, `error_msg` |
| `enrichment_challenge` | `enrich_summaries_for_case` — WAF on press summary | `court/year/num`, `url`, `extra.enrichment_kind` (+ failure sample) |

The proxy layer is the one chokepoint every HTTP request flows through, so it
owns the per-request signal (`request_success` / `request_failed`) carrying
`proxy_url` + `elapsed_ms`. The scraper owns case-level *outcomes*
(`case_failed`, `challenge_detected`, `pool_exhausted`). A WAF interstitial
returns HTTP 200, so it shows up as a proxy-layer `request_success` **and** a
scraper-layer `challenge_detected` for the same case — filter by `kind` to
avoid double-counting.

## `failure_samples/` layout

For post-run WAF signature analysis, the raw response body + headers of
challenge-page hits and failed requests are dumped to `failure_samples/`.
Hard caps keep a WAF loop from writing hundreds of thousands of files:

- **20** challenge-page samples per run (global budget).
- **5** per distinct error prefix (e.g. `HTTP_503`, `JSONDecodeError_HTTP_200`).
- **200 KB** max per sample body (truncated beyond that; `truncated: true` in
  the metadata).

Each sample is a pair. `<sig>.html` is the raw body; `<sig>.headers.json` is:

```json
{
  "signature": "challenge_hkcfi_2023_3",
  "captured_at": "2026-07-04T06:45:28.204856+00:00",
  "is_challenge": true,
  "truncated": false,
  "body_bytes": 240,
  "headers": { "Server": "cloudflare", "CF-Ray": "…", "Set-Cookie": "cf_clearance=…" }
}
```

---

## Post-run analytics via `jq`

All recipes below were validated against a real `events.jsonl`. They read a
completed run's `events.jsonl` — safe to run mid-run too, since the file is
append-only. `cd` into the output directory first.

### 1. Per-error-class counts (sorted desc)

The single highest-value triage query — what failed, and how much. A single
dominant class that was not in the canary is the earliest structural-drift
signal (mirrors the checkpoint-DB error-prefix breakdown in the Chapter 11
Hour-4 monitor, but spans every failure kind, not just `status='failed'`).

```bash
jq -s -r 'map(select(.error_class)) | group_by(.error_class)
  | map({class: .[0].error_class, n: length}) | sort_by(.n) | reverse
  | .[] | "\(.n)\t\(.class)"' events.jsonl
```

```
3	HTTP 503 after 1 retries
2	challenge-page detected in content_html
1	empty-content
```

### 2. Per-proxy success rate + total requests

Catches a single exit IP going bad (early ban, gluetun tunnel flap) while the
aggregate still looks healthy. A proxy whose rate craters relative to its
peers is the one to quarantine.

```bash
jq -s -r 'map(select(.kind=="request_success" or .kind=="request_failed"))
  | group_by(.proxy_url)
  | map({proxy: .[0].proxy_url, total: length,
         ok: (map(select(.kind=="request_success")) | length)})
  | map(. + {rate: (.ok/.total*100 | floor)})
  | sort_by(.rate) | .[] | "\(.proxy)\t\(.ok)/\(.total)\t\(.rate)%"' events.jsonl
```

```
http://p2:2	6/9	66%
http://p3:3	7/9	77%
http://p1:1	9/10	90%
```

### 3. Hourly request / failure trajectory

Throughput and failure rate per clock hour. A failure count that climbs
hour-over-hour is the classic slow-ban curve; a request count that falls off
without a matching failure climb is a throttle or a dying pool.

```bash
jq -s -r 'map(select(.kind|test("request_")))
  | group_by(.ts[0:13])
  | map({hour: .[0].ts[0:13], req: length,
         fail: (map(select(.kind=="request_failed"))|length)})
  | .[] | "\(.hour)  req=\(.req)  fail=\(.fail)"' events.jsonl
```

```
2026-07-04T06  req=28  fail=6
```

### 4. Challenge-page hits by proxy (early-ban detection)

If challenge hits cluster on one or two proxies, those exit IPs are burning —
kill them and quarantine for 24h (Chapter 11 Hour-1 monitor). Even distribution
means a global rate problem, not a per-IP ban.

```bash
jq -r 'select(.kind=="challenge_detected") | .proxy_url // "unknown"' events.jsonl \
  | sort | uniq -c | sort -rn
```

```
   2 http://p3:3
```

### 5. WAF fingerprint diffing via `failure_samples/` headers

Distinct WAF response fingerprints tell you *which* defense tripped. First,
the `Server` header distribution across every sample; then a direct header
diff between two challenge hits to see whether they came from the same edge.

```bash
# Server-header (or content-type) distribution across all samples
jq -r '.headers.Server // .headers["content-type"] // "?"' \
  failure_samples/*.headers.json | sort | uniq -c

# diff two challenge fingerprints — Set-Cookie / CF-Ray / Server drift
diff <(jq -S .headers failure_samples/challenge_hkcfi_2023_3.headers.json) \
     <(jq -S .headers failure_samples/challenge_hkcfi_2023_9.headers.json)
```

A `cf-ray` / `cf-mitigated` header or a `cf_clearance` `Set-Cookie` in these
samples is the unambiguous "Cloudflare turned on" signal that Chapter 01's
"no CDN, no WAF as of 2026-07-04" baseline would need revising for.

### 6. Retry-attempt distribution (throttle detection)

Retries live in the scraper's backoff loop; a terminal failure records the
attempt index it died on. A spike in max-attempt failures — combined with a
climbing `HTTP 429` / `HTTP 503` count from recipe 1 — is throttling, not
random flakiness.

```bash
# how many cases died at each retry depth
jq -r 'select(.retry_attempt != null) | "retry_attempt=\(.retry_attempt)"' \
  events.jsonl | sort | uniq -c

# the throttle status codes behind the retries (proxy layer)
jq -r 'select(.kind=="request_failed" and (.http_status|IN(429,503)))
  | .http_status' events.jsonl | sort | uniq -c
```

```
   3 retry_attempt=1
```

---

## Cross-references

- **Human log + checkpoint SQL** — [Chapter 11 → Logging locations](./11-operations-runbook.md#logging-locations). Use the checkpoint DB for exact status counts; use `events.jsonl` for per-proxy / per-hour / per-fingerprint slices the DB cannot express.
- **The failure classes** these recipes bucket originate at the hook sites documented in [Chapter 09 (Scraper Architecture)](./09-scraper-architecture.md) and [Chapter 10 (Content-Shape Safeguards)](./10-content-safeguards.md).
- **What "healthy" looks like hour-by-hour** — the alert thresholds in [Chapter 11 → Monitoring plan](./11-operations-runbook.md) pair directly with recipes 1, 3, and 4.
