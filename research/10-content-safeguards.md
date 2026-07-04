# Content-Shape Safeguards

Everything upstream of this chapter is about getting bytes off HKLII's origin without being blocked. This chapter is about the opposite question: once the bytes arrive, are they what we think they are? A 200 OK with a JSON envelope is not the same as a judgment. Files that landed on disk yesterday are not the same as files that are still there today. This chapter documents the three failure shapes the scraper actively defends against, the exact check that fires for each one, and what those checks still miss.

The retry loop that surrounds all of this — the exponential backoff, the retryable/permanent status split, the JSONDecodeError handling — lives one layer up, in [Scraper Architecture](./09-scraper-architecture.md). The motivation for content-shape checks in the first place (why 200 OK is not enough) is drawn from the log-analysis rules in [Anti-Detection Strategy](./04-anti-detection-strategy.md).

## The three failure shapes we defend against

A judgment can go wrong in three different places, each with a different signature.

| Failure shape | Where it originates | Symptom | Defense |
| --- | --- | --- | --- |
| **Challenge / interstitial page** | Origin, CDN, or middlebox intercepts the request and returns HTML that looks like a browser challenge or WAF block, wrapped inside HKLII's JSON envelope's `content` field. | HTTP 200 + valid JSON + non-empty `content` — but the HTML says "Just a moment" or "請稍候", not a judgment. | S-1: `_looks_like_challenge_page` denylist match (scraper.py:61-70, 286-291). |
| **Empty content** | Legitimate — HKLII sometimes ships a judgment with `content: ""` and a Judiciary `.docx` URL as the only body. Also fires for genuine origin bugs where neither `content` nor `doc` is set. | HTTP 200 + valid JSON + `content` strips to empty. | Empty-content branch (scraper.py:293-302) with `.docx` fallback if `--allow-doc -f doc` was passed. |
| **Bit-rotted local file** | Bytes were on disk when `mark_downloaded` ran, but a subsequent `rm`, incomplete `rsync -r` (skips the `.checkpoint.db` dotfile by default), disk full during a partial write, or filesystem corruption removed or zeroed them. | Checkpoint row says `status='downloaded'`, but the expected file at `output/{court}/{year}/{stem}.{ext}` is missing or 0 bytes. | `hklii verify` (`verify_downloaded_against_files` at checkpoint.py:220-245) flips broken rows back to `pending`. |

The first two fire inline during the download; the third is a separate reconciliation pass run manually or in a cron.

Note the failure modes we don't defend against and treat explicitly in [Known gaps](#known-gaps) below: SHA-256 mismatch (someone edited the file), positive schema assertion (title/date/neutral in the downloaded body match the enumeration-time metadata), enrichment sidecar verification, and English-only-marker leakage of Traditional Chinese challenge pages that don't happen to hit our six-entry TC list.

## S-1 challenge-page rejection

The audit assigned this defense the label S-1 because it closes the highest-severity gap in the pre-audit `empty-check`: `content_html` non-empty was the only success test (see [Session 2026-07-04](../.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/session-2026-07-04-resume.md)), which happily accepts a valid-JSON envelope carrying a challenge page inside `data["content"]`.

The predicate lives at `scraper.py:61-70`:

```python
def _looks_like_challenge_page(content_html: str) -> bool:
    """True if the HTML looks like a WAF/challenge/error interstitial.

    ASCII markers matched case-insensitively; CJK markers matched exactly
    (Python str.lower() is a no-op on CJK characters).
    """
    if not content_html:
        return False
    haystack = content_html.lower()
    return any(marker.lower() in haystack for marker in _CHALLENGE_MARKERS)
```

Two design choices worth calling out:

1. **Empty input returns `False`**, not `True`. Empty content is a legitimate shape on HKLII (see [Recent-2026 empty-content-with-doc pattern](#recent-2026-empty-content-with-doc-pattern) below) and it is not the challenge-page branch's job to reject it. That's the empty-content branch's job (`scraper.py:293-302`).
2. **`str.lower()` is applied to both haystack and needle.** For ASCII markers this normalizes "Just a moment" and "JUST A MOMENT" onto one match. For CJK the operation is a no-op — Han characters have no case — so the Chinese markers must be stored in the exact literal form the origin would send.

## The marker list

The full denylist lives at `scraper.py:30-47`. As of 2026-07-04 it holds 13 entries: 7 English and 6 Traditional Chinese.

```python
_CHALLENGE_MARKERS = (
    # English — Cloudflare / generic WAF / rate-limit interstitials.
    "just a moment",
    "cf-challenge",
    "cloudflare",
    "please enable javascript",
    "verify you are human",
    "access denied",
    "too many requests",
    # Traditional Chinese — HKLII serves bilingual content, any localized
    # challenge would slip past an English-only denylist.
    "請稍候",
    "驗證您是人類",
    "請啟用 JavaScript",
    "訪問受限",
    "系統維護",
    "拒絕存取",
)
```

Provenance of each entry:

| Marker | Language | Origin of the tell |
| --- | --- | --- |
| `just a moment` | EN | Cloudflare's "Just a moment..." interstitial `<title>`. |
| `cf-challenge` | EN | Cloudflare's `<div id="cf-challenge…">` shell around JS-based challenges. |
| `cloudflare` | EN | Falls back to catch any Cloudflare-branded error page (500, 503, rate limit). |
| `please enable javascript` | EN | Universal challenge-page string; hits Cloudflare, DataDome, Akamai, and generic bot managers. |
| `verify you are human` | EN | Cloudflare Turnstile challenge label. |
| `access denied` | EN | F5 BIG-IP ASM default response body; also generic WAF vocabulary. |
| `too many requests` | EN | 429 error pages that render at the origin instead of surfacing as an HTTP status. |
| `請稍候` | TC | Direct translation of "Just a moment". |
| `驗證您是人類` | TC | Direct translation of "verify you are human". |
| `請啟用 JavaScript` | TC | Direct translation of "please enable JavaScript". |
| `訪問受限` | TC | "Access restricted" — F5-style challenge page localized. |
| `系統維護` | TC | "System maintenance" — soft-block or throttled response localized. |
| `拒絕存取` | TC | Direct translation of "access denied". |

Bilingual coverage is required because HKLII serves both `lang=en` and `lang=tc` cases (the `--lang both` default of `hklii scrape`, see [Operations Runbook](./11-operations-runbook.md)). A challenge page returned to a `tc` request would almost certainly be Chinese-localized. This is why completeness gap #9 in the pre-production audit was flagged and closed as part of S-1: an English-only denylist would silently accept every TC challenge.

## How S-1 fires

The check runs on every successful download attempt, right after `parse_judgment_response` produces a `Judgment` and immediately before the empty-content branch (`scraper.py:283-291`):

```python
judgment = parse_judgment_response(case, data)
output_dir = self._output_dir / record.court / str(record.year)

if _looks_like_challenge_page(judgment.content_html):
    self._checkpoint.mark_failed(
        record.court, record.year, record.number,
        "challenge-page detected in content_html",
    )
    return False
```

The failure string is exactly `challenge-page detected in content_html` — no body preview, no status code, no marker hit. That was deliberate: the raw HTML that tripped the check may be many kilobytes long and would flood the log; the fact that it tripped at all is enough to warrant a manual look at the response. The row's `status` becomes `'failed'` and `error` is set to that string. Subsequent `hklii scrape --retry-failed` will reset all failed rows to `pending` and try again (`checkpoint.py:247-253`).

The tests that lock this behavior in are at `tests/test_scraper.py:483-538` (unit tests for the predicate) and `tests/test_scraper.py:541-593` (end-to-end BulkScraper tests confirming both English and Chinese challenge pages leave zero HTML on disk and land the row as `failed` with `challenge` in the error string).

## Empty-content branch

`content_html.strip()` being empty is a distinct failure mode from a challenge page. The branch at `scraper.py:293-302` handles it:

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

Two cases:

1. **No content and no doc fallback available**: `content_html` is empty AND (`doc` was not in `--format`, OR the JSON envelope's `doc` field is `None`). Row is marked failed with reason `empty-content` (plus a `doc_url=...` hint if the doc URL exists but the operator didn't opt into `-f doc`).
2. **No content but doc fallback IS available**: fall through to `_fetch_doc`. See below.

The `doc_hint` matters: it turns a mysterious failure into a diagnostic. `error='empty-content, doc_url=https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2026/HCA000123_2026.docx'` tells the operator "re-run with `--allow-doc -f doc` and this case will succeed".

## Doc fallback

When `content_ok` is False but `can_try_doc` is True, the scraper attempts to fetch the Judiciary-hosted `.docx` as the substantive body. The relevant path (`scraper.py:304-319`):

```python
actually_saved: set[str] = set()
if content_ok:
    save_judgment_local(judgment, output_dir, self._formats)
    actually_saved = set(self._formats) - {"doc"}

if can_try_doc:
    output_dir.mkdir(parents=True, exist_ok=True)
    if await self._fetch_doc(judgment, output_dir):
        actually_saved.add("doc")
    elif not content_ok:
        # Empty content AND doc fetch failed — nothing on disk
        self._checkpoint.mark_failed(
            record.court, record.year, record.number,
            f"empty-content, doc-fetch-failed, doc_url={judgment.doc_url}",
        )
        return False
```

The three-way outcome:

| `content_ok` | `_fetch_doc` result | `actually_saved` | Row status |
| --- | --- | --- | --- |
| True | True | html/txt/json + doc | `downloaded` |
| True | False | html/txt/json (no doc) | `downloaded` (doc failure is silent when we already have HTML) |
| False | True | doc only | `downloaded` |
| False | False | nothing | `failed` with `empty-content, doc-fetch-failed, doc_url=...` |

The important subtlety: when `content_ok` is True and the doc fetch fails, we do NOT surface an error. We already have the substantive body in HTML and users who asked for `-f doc` also opted into "best effort". Silently omitting doc from `actually_saved` (via the `sorted(actually_saved)` list persisted to `formats`) means `hklii verify` will not later complain about a missing `.doc` file.

## `_fetch_doc` retry logic

`_fetch_doc` (`scraper.py:335-357`) mirrors the main download loop's structure but with different termination semantics:

```python
async def _fetch_doc(self, judgment: Judgment, output_dir: Path) -> bool:
    from .atomic_write import atomic_write_bytes
    for attempt in range(self._max_retries + 1):
        try:
            resp = await self._get(judgment.doc_url)
        except httpx.RequestError:
            if attempt >= self._max_retries:
                return False
            await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
            continue
        if resp.status_code != 200:
            if attempt < self._max_retries and resp.status_code >= 500:
                await asyncio.sleep(_jittered_backoff(self._backoff_base, attempt))
                continue
            return False
        ext = ".docx" if judgment.doc_url.lower().endswith(".docx") else ".doc"
        path = output_dir / f"{judgment.case.filename_stem}{ext}"
        try:
            atomic_write_bytes(path, resp.content)
            return True
        except OSError:
            return False
    return False
```

Compared to the main JSON download loop:

| Behavior | Main loop (`_download_one_impl`) | `_fetch_doc` |
| --- | --- | --- |
| Retry on `httpx.RequestError` | Yes, with jittered backoff. | Yes, with jittered backoff. |
| Retry on `status >= 500` | Yes (specifically `_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}`). | Yes, but only on the broad `>= 500` predicate. |
| Retry on `status in {403, 429}` | Yes. | **No** — these fall through to `return False` on the first attempt. |
| Retry on `JSONDecodeError` | Yes. | Not applicable — doc bytes aren't JSON. |
| Failure on max retries | Marks row failed with body preview. | Returns `False` and lets the caller decide. |

The 403/429 asymmetry is deliberate: a Judiciary origin 403 is nearly always a stable outcome (referrer mismatch, WAF flip), not a transient one, and burning retries on it slows down the whole run. Compare with the main loop, where 403 is retryable because HKLII returns 403 during rate-limit warm-ups.

**Extension detection**: `ext = ".docx" if judgment.doc_url.lower().endswith(".docx") else ".doc"`. Judiciary URLs currently all end in `.docx` (confirmed 2026-07-04 probe, see [Judiciary Platform](./02-judiciary-platform.md)), but pre-2020 casebase may still carry `.doc`. The check falls back to `.doc` if the URL doesn't declare `.docx` — never the other way around. Note that `hklii download` (the one-off URL fetcher) still hardcodes `.doc` at `client.py:128`; only `_fetch_doc` in the bulk path handles both.

## Recent-2026 empty-content-with-doc pattern

Empty `content` fields are not an error mode. They are a shipping pattern: recent (roughly 2026-onward) HKLII judgments come back as `{"content": "", "doc": "https://legalref.judiciary.hk/…"}`. This is confirmed by the 2026-07-04 canary run (`--courts hkfc --limit 100 --lang en`), which downloaded 100/100 cases with 99 HTML + 55 `.doc` + 45 `.docx` files on disk. The 45 `.docx`-only cases hit exactly this path: empty `content_html`, doc URL present, `_fetch_doc` succeeded, row marked downloaded with `formats=["doc"]`.

Operationally this means: **to capture the full recent corpus, the operator must run with `--allow-doc -f doc` in addition to `-f html`.** Without it, every empty-content case will be marked failed as `empty-content, doc_url=…`. See [Operations Runbook](./11-operations-runbook.md) for the exact production command and [Judiciary Platform](./02-judiciary-platform.md) for why HKLII does this (they receive .docx from the Judiciary and don't consistently transcribe it to HTML for recent files).

### Follow-up: `html_pending_at_hklii` tracker + `hklii recheck-html`

Rows captured on the empty-content-with-doc path are not *actually* done — HKLII typically extracts the HTML weeks or months later, and re-fetching those cases at that point produces a legitimate HTML render. The scraper records this state so a follow-up pass can find them:

- **Schema:** `checkpoint.py` adds a nullable `html_pending_at_hklii INTEGER` column (unix ts) via the existing idempotent migration path.
- **Set-point:** in `_download_one_impl` (`scraper.py:321-333`), right before `mark_downloaded`, the scraper stamps `html_pending_ts = int(time.time())` iff `content_ok is False AND "doc" in actually_saved` — i.e. exactly the doc-fallback path. Otherwise `html_pending_ts=None` clears any prior stamp.
- **Consumer:** `hklii recheck-html` (`cli.py`, `html_recheck.HtmlRecheckRunner`) walks `checkpoint.pending_html_recheck()` rows in FIFO ts order, re-fetches `getjudgment` for each, and either (a) saves the HTML + clears the flag if content is now non-empty and passes the `_looks_like_challenge_page` check, or (b) bumps `html_pending_at_hklii` to now so the row moves to the back of the queue for the next pass. Uses the standard proxy pool + preflight so wire behavior is identical to the main `scrape` command.
- **Format union:** on successful re-capture, `mark_downloaded` is called with `set(existing_formats) | {"html", "txt", "json"}` — the original `.doc` or `.docx` stays on disk, the new HTML/txt/json are added.

Reporting: the CLI prints `Newly captured: X, still pending: Y, failed: Z.` at the end. See [Operations Runbook](./11-operations-runbook.md) for cadence recommendations (weekly or monthly re-checks against the pending pool).

## Failure error string format

Every `mark_failed` call in the scraper writes a compact, greppable error string to the `cases.error` column. When the failure includes body content (retryable-status exhaustion or JSONDecodeError), the body is truncated to `_BODY_PREVIEW_LEN = 200` characters and newlines are replaced with spaces (`scraper.py:27, 262, 275`):

```python
_BODY_PREVIEW_LEN = 200
# ...
preview = resp.text[:_BODY_PREVIEW_LEN].replace("\n", " ")
self._checkpoint.mark_failed(
    record.court, record.year, record.number,
    f"HTTP {resp.status_code} after {self._max_retries} retries; body: {preview}",
)
```

Complete failure-string catalog for the content-safeguards paths:

| Origin | Format | Example |
| --- | --- | --- |
| Challenge page | `challenge-page detected in content_html` | (no body preview by design) |
| Empty content, no doc | `empty-content` or `empty-content, doc_url=<url>` | `empty-content, doc_url=https://legalref.judiciary.hk/doc/judg/word/vetted/other/en/2026/HCA000123_2026.docx` |
| Empty content, doc fetch failed | `empty-content, doc-fetch-failed, doc_url=<url>` | `empty-content, doc-fetch-failed, doc_url=https://…` |
| Retryable status exhausted | `HTTP {code} after {n} retries; body: <preview>` | `HTTP 503 after 3 retries; body: <html><body>upstream timeout</body></html>` |
| JSONDecodeError | `JSONDecodeError after {n} retries; HTTP {code}; body: <preview>` | `JSONDecodeError after 3 retries; HTTP 200; body: <html>...` |
| Permanent status | `HTTP {code}` | `HTTP 404` |
| Request exception | `{ExceptionClass} after {n} retries: {msg}` | `ReadTimeout after 3 retries: read timed out` |
| IP leak | `IPLeakError: {msg}` | `IPLeakError: exit IP matches home IP` |
| Save error | `OSError during save: {msg}` | `OSError during save: [Errno 28] No space left on device` |

Newline replacement keeps SQLite rows human-readable when `sqlite3 .checkpoint.db 'select error from cases where status=\"failed\" limit 20'` is used to triage a run.

## Atomic-write guarantees vs what "downloaded" actually means

`atomic_write.py` gives us a strong file-level guarantee (`atomic_write.py:13-25`):

```python
def _fsync_and_replace(part: Path, dest: Path) -> None:
    fd = os.open(part, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, dest)
    # fsync the parent directory so the rename survives an unclean reboot.
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
```

The four-step model — write to `{name}.part`, `fsync` the file, `os.replace` to the target, `fsync` the parent directory — closes the crash-consistency window: after `_fsync_and_replace` returns, a hard power loss cannot leave the file half-written, cannot leave the `.part` sitting orphaned, and cannot lose the rename (because the parent directory's dirent has been flushed to stable storage). Every operator-visible output goes through this: `save_judgment_local` at `client.py:75-108` calls `atomic_write_text` for HTML/TXT/JSON, and `_fetch_doc` calls `atomic_write_bytes` for the .doc/.docx blob (`scraper.py:353`).

But this guarantee is narrower than "the file contains a real judgment". The atomic write proves:

- **A file exists at the destination path.**
- **Its bytes are the exact bytes we handed to `atomic_write_*`.**
- **The rename survived a crash.**

It does not prove:

- The file is a real judgment (S-1 already fired upstream — but S-1 is a denylist, not an allowlist).
- The file is bit-identical to some canonical version we can re-verify.
- The bytes match the enumeration-time metadata (title, date, neutral) stored on the checkpoint row.
- Nothing has touched the file since it landed.

The moment `mark_downloaded` commits, "downloaded" means exactly: "the atomic-write returned successfully, and the JSON envelope's `content` didn't look like a challenge page or empty content." Everything else — long-term integrity, tamper detection, semantic correctness — is not in scope for the download-time checks.

## `hklii verify` subcommand

`hklii verify` closes the gap between "downloaded means we wrote atomically" and "downloaded means the file is still on disk". It is a manual reconciliation pass over the checkpoint DB; run it whenever the download tree has been moved, restored, or touched by anything outside the scraper.

CLI surface (`cli.py:252-283`):

```
Usage: hklii verify [OPTIONS]

  Reconcile the checkpoint against on-disk files.

  Iterates rows with status='downloaded' and checks each expected
  format file exists and is non-zero-byte. Rows with missing or
  empty files are flipped back to status='pending' so a subsequent
  `hklii scrape --resume` re-downloads them.

  Fixes the silent-file-loss scenario: rm accident, incomplete
  rsync (`.checkpoint.db` is a dotfile — rsync -r skips by default),
  bit-rot, or partial disk writes.

Options:
  -o, --output DIRECTORY  Directory containing existing downloads +
                          .checkpoint.db.
```

Only one flag: `--output`, defaulting to `./downloads`. The command errors out if `.checkpoint.db` isn't under that path (`cli.py:274-275`) and otherwise runs `verify_downloaded_against_files`, printing the count of broken rows plus the post-verify stats.

The underlying check (`checkpoint.py:220-245`):

```python
def verify_downloaded_against_files(self, output_dir) -> int:
    """Scan status='downloaded' rows; flip any whose expected files are
    missing or 0-byte back to status='pending'. Returns broken count."""
    from pathlib import Path
    output_dir = Path(output_dir)
    rows = self._conn.execute(
        "SELECT court, year, number, formats FROM cases WHERE status='downloaded'"
    ).fetchall()
    broken = 0
    for court, year, number, formats_json in rows:
        formats = json.loads(formats_json) if formats_json else []
        stem = f"{court}_{year}_{number}"
        case_dir = output_dir / court / str(year)
        for fmt in formats:
            ext = "docx" if fmt == "doc" and (case_dir / f"{stem}.docx").exists() else fmt
            path = case_dir / f"{stem}.{ext}"
            if not path.exists() or path.stat().st_size == 0:
                self._conn.execute(
                    "UPDATE cases SET status='pending', formats=NULL "
                    "WHERE court=? AND year=? AND number=?",
                    (court, year, number),
                )
                broken += 1
                break
    self._conn.commit()
    return broken
```

## verify semantics

For each `downloaded` row, the check iterates `formats` (the JSON list persisted at `mark_downloaded` time — see `checkpoint.py:175-183`). For each format:

1. Compute the expected path: `output/{court}/{year}/{stem}.{ext}`.
2. For `fmt == 'doc'`, first probe `{stem}.docx` — if it exists, treat that as the doc file. Otherwise fall back to `{stem}.doc`.
3. If the resolved path does not exist OR its size is zero, mark the row broken.

A single missing/zero-byte format breaks the row: `formats` is set to `NULL` and `status` flips to `'pending'`. The row's neutral/title/date/lang are preserved so `hklii scrape --resume` re-downloads exactly the same case.

Concrete failure modes this rescues:

| Scenario | What `verify` sees |
| --- | --- |
| `rsync -r downloads/ backup:` (no `-a`, no explicit dotfile) | Sync copied everything EXCEPT `.checkpoint.db`. On the destination, all rows still say `downloaded` but files are correct. `verify` is a no-op. On the origin after `rsync --delete`, files are gone but checkpoint remains — `verify` flips them all to `pending`. |
| `rm downloads/hkcfi/2024/hkcfi_2024_123.html` | Row still says `downloaded` with `formats=["html","txt","json"]`. `verify` sees the missing `.html`, flips to `pending`. |
| Disk full during atomic write, `.part` cleanup succeeded but `mark_downloaded` never fired | Row never became `downloaded` in the first place — this scenario doesn't touch `verify`. Correctness comes from the transaction ordering, not the reconciliation. |
| Partial `rm -rf downloads/hkcfi/2024/` | Row still says `downloaded`. `verify` flips every 2024 CFI case in the checkpoint back to `pending` so `--resume` re-fetches them. |
| Bit-flip zeroing the file (extremely rare on modern SSDs) | `stat().st_size == 0` fires, row flips to `pending`. Non-zero corruption is not detected — that would need SHA-256. |
| Enrichment sidecar deleted (`hkcfi_2024_123.summary_en.html`, `.appeal_history.json`) | Not detected — `verify` reads `formats` (which never contains enrichment kinds) and never inspects the enrichment sidecar files. |

The test suite locks in three cases at `tests/test_checkpoint.py:233-284`: missing file flips to `pending`, zero-byte file flips to `pending`, intact files are left alone with `downloaded` preserved.

## Known gaps

The safeguards described above are not a complete integrity story. What follows is the honest tally of what they miss, matched to the pre-production audit's tier labels (see [Decisions Log](./12-decisions-log.md) for the full framework).

**Gap: no SHA-256 (Tier C, deferred — audit label S-2).** Neither the download-time write nor `verify` records or checks a content hash. A file that got silently mangled (bit-flip that flipped a byte instead of zeroing it, malicious edit, mid-transfer corruption on rsync without checksum) is invisible. Closing this needs a schema column `content_sha256`, a hash computation at atomic-write time, and an extension of `verify_downloaded_against_files` that re-hashes and compares. Effort estimated at 4-6 hours; deferred because the failure mode is rare on modern hardware and the scraper can always re-run.

**Gap: no positive assertion vs enumeration-stored metadata.** The `cases` table has `title`, `date`, and `neutral` populated at enumeration time (`checkpoint.py:128-145`, `upsert_case`). The scraper never reads them back after download to confirm the parsed `Judgment` matches. A judgment JSON envelope with the right shape but the wrong contents (a very unlikely origin bug, but not impossible) would pass. This was flagged in completeness gap #9 and remains open; the fix is a `parse_judgment_response` postcondition that compares `judgment.title`, `judgment.date`, `judgment.neutral_citation` against the row's enum-time values before `mark_downloaded`.

**Gap: no enrichment sidecar verification.** Both `hklii verify` and the audit's S-2/S-3 safeguards ignore enrichment files entirely. If `{stem}.summary_en.html` or `{stem}.appeal_history.json` are deleted from disk, the `summary_en_status='downloaded'` and `appeal_history_status='downloaded'` columns remain set. The status columns become a lie. The fix requires extending `verify_downloaded_against_files` to also inspect the `_ENRICHMENT_KINDS` columns and check the corresponding sidecar filenames. See [Enrichment Architecture](../.claude/projects/-Users-seangreenhalgh-Developer-hklii-downloader/memory/enrichment-architecture.md) for the sidecar file layout.

**Gap: English-heavy denylist, bilingually incomplete.** The denylist covers 7 English strings and 6 Traditional Chinese strings. Real-world challenge pages come in many more shapes: Simplified Chinese (HKLII does not serve `zh-hans` but a middlebox might inject one), Cantonese-vernacular error text, F5 BIG-IP default templates in other languages, generic HTTP error pages that don't happen to say "cloudflare" or "just a moment". The denylist is a soft filter, not a proof. A high-tier fix would flip the model to an allowlist ("this content is a judgment iff it has `<html>`, `<body>`, at least one `<p>`, and one field matching the enum-stored metadata") — approximately the same fix as the positive-assertion gap above, and effort-estimated similarly.

**Gap: challenge markers do not have `<title>`/`<h1>` scope preference.** `_looks_like_challenge_page` searches the entire lower-cased haystack. If a legitimate judgment quotes a marker string in its body — a case discussing Cloudflare's rate-limiting practices, for instance — S-1 would false-positive. As of 2026-07-04 the canary run had zero false positives (100/100 downloads succeeded), but the risk is nonzero. Scoping to `<title>` or the first `<h1>` would tighten this, at the cost of more parsing.

**Gap: S-1 marker list is source-of-truth free-form text.** No version-controlled schema, no formal specification of what a "marker" is. Adding markers is a code change and a test change; there is no runtime `--extra-marker` flag or config file. This is fine for a small denylist but does not scale.

**Gap: no per-file size sanity check.** `verify` accepts any file with `st_size > 0` as intact. A 1-byte file is fine by this test. Real judgment HTML is >5 KB, JSON is >200 bytes, .docx is >20 KB. Adding a per-format minimum size (or better, a size-range plausibility check against the enum-time metadata) would close this.

## See also

- [Scraper Architecture](./09-scraper-architecture.md) — the retry loop, backoff formula, and status classification (`_RETRYABLE_STATUSES`, `_PERMANENT_ERRORS`) that surround every content check. The main download loop in `_download_one_impl` fires S-1 and the empty-content branch as post-conditions once a 200-JSON response has been extracted.
- [Anti-Detection Strategy](./04-anti-detection-strategy.md) — the log-analysis rules and 12-signal catalog that motivate why "HTTP 200 + valid JSON" isn't a completion criterion in the first place. The challenge-page category exists because tier-2/3 detection stacks can and do serve interstitials inside otherwise-valid envelopes.
- [Judiciary Platform](./02-judiciary-platform.md) — the `.docx` origin (`legalref.judiciary.hk`), URL derivation for the doc fallback, and the HTTP caching semantics (ETag / Last-Modified / Accept-Ranges) that S-2 would exploit if implemented.
- [Operations Runbook](./11-operations-runbook.md) — how to invoke `hklii verify`, when to run it (after rsync, after a crash, before archiving a corpus), and how to interpret its output.
- [Decisions Log](./12-decisions-log.md) — the audit tier assignments (S-1/S-2/S-3/S-4/S-5) and the rationale for deferring S-2 (SHA-256) to a later cycle.
