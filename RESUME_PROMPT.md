ultracode

You are resuming work on the HKLII bulk-judgment downloader (Python, uv, Click, async). Do not reindex the codebase; the memory files carry the load-bearing context.

Before doing anything else, run `/effort max` (the user will have expected to type this — remind them if they didn't).

**Bootstrap via memory only.** Read, in order:
1. `memory/MEMORY.md` (the index)
2. `memory/session-2026-07-04-resume.md` (current-state snapshot)
3. `memory/project-goals.md` (goals + module map)
4. `memory/cli-feature-matrix.md` (flag surface)

Then read `memory/reliability-hardening-log.md` and `memory/enrichment-architecture.md` only if the task touches those areas. The remaining memories are reference material; consult on demand.

**Current status (do not ask):**
- Branch: `main` at `47dcd15`, caught up to `origin/main`. Nothing pending push.
- 293 tests across 14 test files, all passing. `pytest asyncio_mode = auto`.
- 4 CLI subcommands (`download`, `scrape`, `verify`, `enrich`) and 12 modules under `src/hklii_downloader/`.
- 6 gluetun/PIA VPN containers are configured (may or may not be running — check with `docker ps` before assuming).
- Scraper is production-shape: bilingual sweep, proxy pool + curl_cffi impersonation, atomic writes, fcntl DB lock, PRAGMA integrity_check, cooldown/revival, HTTP-status circuit counting, enrichment pipeline (press summaries + appeal history), enumeration caching (`--enum-max-age`, `--save-enum-responses`), `--retry-failed`, `--allow-doc` bulk mode.

**Ready to work on next (pick based on the user's ask):**
1. **Full production run** across hkcfi + hkca + hkdc + hkcfa, both languages. Expect ~13 GB and 20-40h at current throttle. Recommended command:
   `uv run hklii scrape -p ... -p ... --with-summaries --with-appeal-history --save-enum-responses --lang both`
   Bring VPN pool up first (`docker compose up -d` under the compose dir).
2. **RAG index build** on top of the corpus — press summaries are the ideal retrieval chunk (see `memory/hklii-press-summaries.md`).
3. **Live circuit-breaker verification** — kill a gluetun container mid-scrape and confirm cooldown + revive path in real time (only unit-test coverage today).
4. **Feature backlog** — forward-citation extraction from HTML, citation graph across the downloaded corpus.

**House rules that apply:** strict TDD (paste the failing test output before implementing), atomic commits (test + implementation as two commits), no test escape hatches, Context7 for library docs, no secrets in git. See `~/.claude/CLAUDE.md`.

If asked to add a CLI flag, cross-check and update `memory/cli-feature-matrix.md` in the same PR. If touching any of the audit-hardening code paths, first re-read `memory/reliability-hardening-log.md` so you don't accidentally undo an atomic-write / fsync / fcntl-lock / integrity-check / HTTP-status circuit counter.