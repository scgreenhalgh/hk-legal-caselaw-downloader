# Review patterns — the 5 bug shapes we keep shipping

Derived from the 2026-07-07 retro on the `hklii update` ship. First review
round caught 17 real bugs, meta-review of the fixes caught 3 more —
including a CONFIRMED critical regression I introduced in the fix. All 20
findings sort into these five recurring shapes.

Use this doc as:
1. **Review lenses** — when running a manual or workflow-based review, walk
   each lens across the affected code.
2. **Pre-commit checklist** — before shipping, self-audit against them.
3. **Test-design checklist** — when writing tests, verify you're covering
   the failure modes each lens implies.

## Lens 1 — Silent skip / over-broad exception handling

**The shape:** `except Exception: continue`, `except: pass`, `if not X:
return None`, `try: ... except FooError: return 0`. Any code path that
returns to the caller as if the requested operation completed, when in
fact it was skipped.

**Why it hides:** silent skips are invisible in tests unless a test
explicitly asserts on side-effect counts (calls made, rows written).
Two failure modes produce identical observable output — success and
skip — and the caller can't distinguish them.

**Examples in this repo:**
- `coverage_canary` per-bucket `except Exception: continue` swallowed
  `AllProxiesDeadError` identically to a benign ukpc/tc 500 → wrapper
  printed "all buckets within tolerance" when 100 % of probes had
  failed.
- Canary escalation loop `except Exception:` printed red but didn't
  count failures → dispatcher marked the step `ok` after N crashes.
- `reset_relatedcap_fetches` caught all `sqlite3.OperationalError`
  as "table missing" → disk-full and lock-contention paths silently
  returned 0.

**How to catch:**
- Grep for every `except` in the affected code. For each, enumerate the
  exception types that COULD hit it. If you can't enumerate, narrow the
  catch to what you actually mean to handle.
- Any per-item error tolerance needs an aggregate signal. Track
  `succeeded_count` / `failed_count`; raise or return a distinguishable
  sentinel if the ratio crosses a floor.
- If a function returns `[]` / `None` / `0` on both "clean but empty"
  and "silently skipped due to error," redesign the return to
  distinguish (tuple, exception, sentinel).

**Test lens:** for every `except: continue` path, write a test that
seeds a real exception source and asserts the function surfaces the
degradation. If the test would pass under the silent-skip
implementation, it's not testing the lens.

## Lens 2 — Semantic drift between components added at different times

**The shape:** a component uses shared state (a DB row, a file, a lock,
a config key) with an implicit assumption about what the state means.
Later, another component writes to or reads from the same state with a
different assumption. Both components in isolation are correct; the
composition is broken.

**Why it hides:** each author only reviews the code path they're
touching. Nobody looks at "what other code writes / reads this same
state and does it agree with my assumption?"

**Examples in this repo:**
- Bilingual case UPSERT collapses `lang='tc'` into `lang='en'` (rule
  added early). The coverage canary was added later and probed
  `WHERE lang='tc'` — undercount by N_bilingual per court, 3 phantom
  escalations every run.
- `enum_runs` table added for `orphan_mark`'s "clean full-corpus
  sweep" signal. `BulkScraper.enumerate()` ALSO writes this table
  from narrow-window daily/weekly/monthly scrapes — the writes are
  byte-identical to full-corpus writes, breaking orphan_mark's read
  contract.
- `_migrate_enum_runs_window_columns` filled the new columns with
  NULL for pre-existing rows. `latest_completed_enum_run` reads
  `WHERE min_date_text IS NULL` — indistinguishable from post-fix
  full-corpus rows.

**How to catch:**
- For every write to shared state, grep every read of the same field
  or key. Verify each reader's assumption is consistent with what the
  writer produces.
- Especially watch for state whose meaning is "the absence of a
  value" (NULL, empty, missing key). New writers using the same
  absence for a different meaning is the classic form.
- When a component consumes state produced by ANY OTHER component,
  the contract needs to be explicit (a status enum, a version column,
  a "produced by" tag) — not implicit ("we both know NULL means X").

**Test lens:** for every read-write pair on shared state, write a test
where component A writes and component B reads, verifying the
semantics agree. If B has multiple valid writers, test one per writer.

## Lens 3 — Docstring / promise drift

**The shape:** a docstring, block comment, or type hint describes
behaviour that the code does not (or no longer) implement. Sometimes
the docstring is aspirational; sometimes it described the previous
implementation; sometimes it's just wrong from the start.

**Why it hides:** docstrings are not executed. Static-analysis tools
don't verify them. Reviewers skim past docstrings assuming they match
the code below.

**Examples in this repo:**
- `format_plan`'s comment: "Snapshot the plan + HKT date ONCE."
  Code read the clock twice.
- `_run_update_orphan_mark` docstring: "refuse to run unless EVERY
  (court, lang) has last_enumeration_ts within max_ts - 1 hour."
  Code used `latest_completed_enum_run` — a completely different
  mechanism.
- `_dispatch_update_plan` docstring: "Returns the count of steps
  that raised; the CLI turns non-zero into a non-zero exit code."
  Escalation loop's `except Exception:` swallowed silently, so a
  step could report `ok` despite N crashes.

**How to catch:**
- At refactor time: read every docstring in the function you're
  editing. Verify each claim still holds.
- At review time: for every docstring longer than one line, grep
  the function body for the terms the docstring uses. Missing
  terms = probable drift.
- If a docstring describes a guard, invariant, or contract, treat
  it as a spec — the code must be provably compliant, and a test
  should assert it.

**Test lens:** for every non-trivial contract stated in a
docstring, name a test after the contract clause and assert it.
`test_format_plan_reads_clock_once` was named for exactly this
reason — it pins the docstring's promise.

## Lens 4 — Testing the wrong side of the contract

**The shape:** a test verifies something related to the failure
mode but not the failure mode itself. Common variants:

- Signature test: asserts a callee accepts kwarg X, when the
  regression is at the caller passing wrong kwarg X.
- Constructor test: verifies the object initialises correctly,
  when the failure mode is a method behaviour under state.
- `:memory:` test: exercises the fast path only, when the
  interesting path is a migration ALTER on an existing table.
- Pure-function test: covers the wrapped implementation but not
  the wrapper that composes it.

**Why it hides:** the test passes green, so nobody notices the
gap. And the test's name often SAYS it tests the failure mode
("test_dispatch_arg_contract") — so a reviewer glancing at the
suite feels covered.

**Examples in this repo:**
- `TestUpdateDispatchArgContract` inspected `_run_enrich`'s
  signature. Regression was at the CALLER's call site. Fixed to
  inspect `_dispatch_update_plan`'s source directly.
- `TestEnumRunFullCorpusFiltering` used `:memory:` only. Migration
  ALTER path was untested — where the CONFIRMED regression lived.
- `_run_coverage_canary` had zero tests; every test targeted the
  pure `coverage_canary`. Escalation-failure count, pool-close
  ordering, and DB-close-on-pool-raise all untested.

**How to catch:**
- For every test with a docstring claim of the form "regression:
  earlier code did X," verify the test would fail if the
  regression were reintroduced. If the assertion is upstream of
  the regression point, it's the wrong side.
- When you add a wrapper that composes a pure function with I/O,
  add at least one integration test at the wrapper level.
- When a schema has a migration path, one test must exercise the
  pre-existing shape → migrate → new-code interaction. `:memory:`
  alone doesn't count.

**Test lens:** ask "if I revert the fix I'm about to ship, which
test in the suite fails?" If none, the test is missing.

## Lens 5 — State with multiple valid readings

**The shape:** a value can mean different things depending on
context. Common forms: NULL that means "not applicable" AND "not
yet computed" AND "not disclosed"; `0` that means "unlimited" AND
"disabled" AND "explicitly zero"; an enum value that was legal in
an earlier version but not now.

**Why it hides:** the ambiguity is often the point at design time
("we can use NULL for the missing case"). The problem surfaces
later when a THIRD reading is needed and the field can't
disambiguate.

**Examples in this repo:**
- `enum_runs.min_date_text = NULL` — post-fix means "full-corpus
  sweep"; on legacy rows migrated in, means "column didn't
  exist." Same column, two meanings.
- `formats = NULL` on a case row — means "never downloaded" OR
  "downloaded but formats data lost." Currently handled but a
  hazard.
- `recent_days = 0` — means "unlimited window" (matches None) but
  is different from `recent_days = 1` which is "very narrow."
  The `0` case had to be added specially to the guard.

**How to catch:**
- When adding a schema column or config value, list every reading
  ("what does None mean? what does 0 mean? what does the empty
  string mean?"). If more than one, introduce a distinguishing
  field.
- When adding a migration, ask "for every pre-existing row where
  this new column defaults to NULL, what does that NULL mean?" If
  the answer is "we can't tell," the migration needs a
  disambiguation step.
- Prefer explicit enums or `is_X` booleans over sentinel values.

**Test lens:** for every column or config value that can be NULL,
write a test that seeds NULL in each of the ways it can arise
and verifies each is handled correctly (or that at least one path
raises loudly).

## Meta-lens — When you're fixing a bug

The retro also identified a class of miss that only surfaces after
a fix ships: **the fix for one failure mode introduces another in
the same block.** Both times it happened this session, the
introduced bug was worse than the one being fixed.

Examples:
- Fix for the preflight pool-leak (Cluster E) restructured the
  try/finally. New shape leaked the DB lock on `pool.close()`
  raise.
- Fix for canary silent-green (Cluster C) added
  `probes_ok == 0` check. Missed the majority-blind case
  (1-of-13 successes reports green).
- The migration hook itself, added for backwards compat,
  reintroduced the mass-orphan hazard for legacy DBs.

**Prevention:** before committing a fix, explicitly walk the OTHER
failure modes the changed block enables. For a try/finally
change, enumerate every callable that could raise in each block
and confirm cleanup order is safe under each. For a guard
threshold, list the edge values (0, 1, N-1, N).

**Test lens:** for every fix commit, one of its tests should be
adversarial toward the fix itself — "does the fix hold if the
input crosses a boundary I didn't originally consider?"

## Review checklist (short form)

Before merging code that touches:

- [ ] **Any `except`** — enumerate types caught; narrow if you can't
- [ ] **Shared state** — grep both writers and readers; contract explicit?
- [ ] **Docstring** — every claim still true? every promise a test?
- [ ] **New test** — would the fix's absence make it fail?
- [ ] **Nullable field** — every valid reading enumerated?
- [ ] **Fix commit** — one test adversarial toward the fix?

## Test checklist (short form)

Before writing a test:

- [ ] Am I testing the CALLER or the CALLEE? Match to where the bug lives.
- [ ] Am I testing the WRAPPER or the WRAPPED? Both usually need coverage.
- [ ] Am I testing the `:memory:` fast path or the migration/ALTER path?
- [ ] If the fix I'm shipping is reverted, does this test fail?
- [ ] Are my assertions on OBSERVABLE behaviour (calls made, rows changed,
      return values) or on IMPLEMENTATION DETAILS (mock spies, private attrs)?
