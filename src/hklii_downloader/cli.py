from __future__ import annotations

import asyncio
from pathlib import Path

import click
import httpx

from .client import fetch_judgment, make_async_client, save_judgment
from .enumerator import EnumWindow
from .parser import parse_hklii_url

VALID_FORMATS = {"html", "txt", "json", "doc"}
DEFAULT_CONCURRENCY = 5


class MutuallyExclusiveOption(click.Option):
    def handle_parse_result(self, ctx, opts, args):
        # `download` uses --proxy with dest 'proxy' (single value); scrape,
        # enrich, and recheck-html use --proxy with dest 'proxies'
        # (multiple=True). Check both so the mutex catches the bypass
        # verified in Round 4 review, where the wrong dest name silently
        # let `--proxy X --direct -y` proceed to hit hklii.hk directly.
        if ("proxy" in opts or "proxies" in opts) and "direct" in opts:
            raise click.UsageError("--proxy and --direct are mutually exclusive.")
        return super().handle_parse_result(ctx, opts, args)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """HKLII judgment downloader."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=Path("downloads"),
    help="Output directory (default: ./downloads)",
)
@click.option(
    "-f", "--format",
    "formats",
    multiple=True,
    type=click.Choice(sorted(VALID_FORMATS), case_sensitive=False),
    default=["html", "txt", "json"],
    help="Output format(s). Repeatable. Default: html txt json",
)
@click.option(
    "-p", "--proxy",
    type=str,
    default=None,
    cls=MutuallyExclusiveOption,
    help="Proxy URL, e.g. socks5://127.0.0.1:1080 or http://user:pass@host:port",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "-c", "--concurrency",
    type=int,
    default=DEFAULT_CONCURRENCY,
    help=f"Max concurrent downloads (default: {DEFAULT_CONCURRENCY})",
)
def download(
    urls: tuple[str, ...],
    output: Path,
    formats: tuple[str, ...],
    proxy: str | None,
    direct: bool,
    concurrency: int,
) -> None:
    """Download specific judgments from HKLII.

    Pass one or more HKLII case URLs, e.g.:

      hklii download https://www.hklii.hk/en/cases/hkcfa/2023/32

    Multiple URLs can be provided:

      hklii download URL1 URL2 URL3
    """
    if not proxy and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")

    asyncio.run(_run(urls, output, set(formats), proxy, concurrency))


DEFAULT_COURTS = ["hkcfi", "hkca", "hkdc", "hkcfa"]

# Every non-empty HKLII case-DB slug + ukpc (dormant-but-listed; API
# returns cleanly). Canonical list used by the update command's canary,
# full-corpus reconcile, and orphan_mark guard so those three surfaces
# stay consistent.
ALL_COURTS: list[str] = [
    "hkcfa", "hkca", "hkcfi", "hkdc", "hkldt", "hkfc",
    "hkmagc", "hkct", "hkcrc", "hklat", "hkoat", "hksct",
]
# UKPC (UK Privy Council) removed 2026-07-08: HKLII's ukpc slug is
# currently empty (getmetacase returns count=0 for en, HTTP 500 for tc)
# and every derived table has 0 rows referencing it. UKPC judgments
# live at BAILII (bailii.org/uk/cases/UKPC/) and jcpc.uk — foreign
# jurisdiction from HK's perspective. Re-add here + coordinated
# viewer/courts.py updates if HKLII ever populates the slug.
ALL_LANGS: tuple[str, ...] = ("en", "tc")


def _filter_fresh_case_buckets(
    output: Path,
    court_list: list[str],
    langs: tuple[str, ...],
) -> tuple[list[str], tuple[str, ...]]:
    """Drop (court, lang) triples marked FRESH in db_freshness.

    Consumers: the four scrape subcommands under ``--skip-if-fresh``.
    Kept side-effect-free (read-only on db_freshness) so a no-scrape
    outcome doesn't leave stray writes.

    Semantics — encoded from the design's ``fresh_definition`` rule:

      * A bucket with no db_freshness row (first-run) → NOT FRESH
        → keep in the scrape scope.
      * A bucket with ``probe_error IS NOT NULL`` → NOT FRESH.
      * The scrape command takes courts × langs; this helper returns
        the surviving court list AND the surviving lang tuple. If
        ALL (court, lang) pairs pass through the filter we keep the
        original court+lang split so downstream fan-out is unchanged.
        If some pairs are fresh but others aren't, we drop entire
        court rows whose every lang is fresh — otherwise the scrape's
        court/lang product would over-scrape a fresh (court, other-lang)
        pair. This is a conservative simplification for the initial
        wiring; a more targeted per-pair filter needs BulkScraper to
        accept a set of (court, lang) tuples instead of a court list
        × lang tuple product.
    """
    from .checkpoint import CheckpointDB
    from .freshness import _fresh

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        return court_list, langs
    db = CheckpointDB(str(db_path))
    try:
        surviving_courts: list[str] = []
        for court in court_list:
            all_fresh = True
            for lang in langs:
                rec = db.get_freshness_row("cases", court, lang)
                if rec is None or not _fresh(rec):
                    all_fresh = False
                    break
            if all_fresh:
                click.echo(
                    f"skip-if-fresh: dropping {court} — all langs fresh",
                )
            else:
                surviving_courts.append(court)
        return surviving_courts, langs
    finally:
        db.close()


def _filter_fresh_hopt_buckets(
    output: Path,
    abbrs: tuple[str, ...],
    langs: tuple[str, ...],
    kind: str = "hopt",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Sister of :func:`_filter_fresh_case_buckets` for hopt/legis/ukpc.

    ``kind`` selects the checkpoint dispatch — 'hopt' for the treaty
    family (bacpg / bahkg / hktmc / hktml / hkts + ukpc) and 'legis'
    for the ord / reg / instrument family. Same conservative
    per-abbr filter: drop an abbr only if EVERY lang for it is fresh.
    """
    from .checkpoint import CheckpointDB
    from .freshness import _fresh

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        return abbrs, langs
    db = CheckpointDB(str(db_path))
    try:
        surviving: list[str] = []
        for abbr in abbrs:
            all_fresh = True
            for lang in langs:
                rec = db.get_freshness_row(kind, abbr, lang)
                if rec is None or not _fresh(rec):
                    all_fresh = False
                    break
            if all_fresh:
                click.echo(
                    f"skip-if-fresh: dropping {abbr} — all langs fresh",
                )
            else:
                surviving.append(abbr)
        return tuple(surviving), langs
    finally:
        db.close()


from dataclasses import dataclass, field as _dc_field  # noqa: E402


@dataclass
class ScrapeConfig:
    """Bundled configuration for a `_run_scrape` invocation.

    Motivating design (review finding E — 'ScrapeConfig dataclass'): the
    prior signature was 17+ kwargs, and every new toggle risked a silent
    divergence between the standalone `scrape` command and the update
    dispatcher (`_run_update_scrape`) if the two happened to pass
    different defaults. Bundling into a dataclass:
      * lets callers name their intent as ONE object,
      * adds new knobs in ONE place (with a default),
      * keeps callers who don't opt in on the default,
      * makes it a construction error to omit required fields.
    """
    output: Path
    fmt_set: set[str]
    proxies: list[str]
    direct: bool
    court_list: list[str]
    langs: tuple[str, ...] = ("en", "tc")
    limit: int | None = None
    resume: bool = False
    with_summaries: bool = False
    with_appeal_history: bool = False
    retry_failed: bool = False
    enum_max_age: int = 0
    save_enum_responses: bool = False
    no_events: bool = False
    # EnumWindow() → full corpus, matches legacy default.
    window: EnumWindow = _dc_field(default_factory=EnumWindow)
BULK_FORMATS = {"html", "txt", "json"}


@main.command()
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=Path("downloads"),
    help="Output directory (default: ./downloads)",
)
@click.option(
    "-f", "--format",
    "formats",
    multiple=True,
    type=click.Choice(sorted(BULK_FORMATS | {"doc"}), case_sensitive=False),
    default=sorted(BULK_FORMATS),
    help="Output format(s). Default: html json txt",
)
@click.option(
    "-p", "--proxy",
    "proxies",
    multiple=True,
    type=str,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--courts",
    type=str,
    default=None,
    help=f"Comma-separated court codes. Default: {','.join(DEFAULT_COURTS)}",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N downloads (smoke test).",
)
@click.option(
    "--allow-doc",
    is_flag=True,
    default=False,
    help="Enable .doc format in bulk mode (disabled by default).",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Re-enumerate and download remaining cases.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct mode.",
)
@click.option(
    "--with-summaries",
    is_flag=True,
    default=False,
    help="Fetch Press Summary (English + Chinese) alongside each judgment.",
)
@click.option(
    "--with-appeal-history",
    is_flag=True,
    default=False,
    help="Fetch appeal history JSON for each judgment.",
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "both"]),
    default="both",
    help="Which language(s) to enumerate. Default: both (English + Chinese, en wins for bilingual cases).",
)
@click.option(
    "--retry-failed",
    is_flag=True,
    default=False,
    help="Flip previously-failed cases back to pending before this run.",
)
@click.option(
    "--enum-max-age",
    type=int,
    default=0,
    help="Skip (court, lang) enumeration if it happened within HOURS (default 0 = always re-enumerate).",
)
@click.option(
    "--save-enum-responses",
    is_flag=True,
    default=False,
    help="Save raw getcasefiles JSON responses to <output>/.enum_cache/ for provenance / audit.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging to <output>/events.jsonl (storage-constrained runs).",
)
@click.option(
    "--skip-if-fresh",
    is_flag=True,
    default=False,
    help=(
        "Consult db_freshness before enumerating and drop (court, lang) "
        "buckets already marked FRESH. Opt-in: default OFF preserves "
        "the current full-scrape semantic for explicit invocations."
    ),
)
def scrape(
    output: Path,
    formats: tuple[str, ...],
    proxies: tuple[str, ...],
    direct: bool,
    courts: str | None,
    limit: int | None,
    allow_doc: bool,
    resume: bool,
    yes: bool,
    with_summaries: bool,
    with_appeal_history: bool,
    lang: str,
    retry_failed: bool,
    enum_max_age: int,
    save_enum_responses: bool,
    no_events: bool,
    skip_if_fresh: bool,
) -> None:
    """Bulk scrape judgments from HKLII courts.

    Enumerates all cases in target courts, then downloads pending cases
    with retry logic and checkpoint-based resume.

    \b
    Examples:
      hklii scrape --proxy http://localhost:8888
      hklii scrape --courts hkcfi,hkca --proxy http://localhost:8888
      hklii scrape --direct --yes --limit 10
      hklii scrape --resume --proxy http://localhost:8888
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")

    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )

    fmt_set = set(formats)
    if "doc" in fmt_set and not allow_doc:
        fmt_set.discard("doc")
        click.secho("Note: .doc disabled in bulk mode. Use --allow-doc to enable.", fg="yellow", err=True)

    court_list = courts.split(",") if courts else DEFAULT_COURTS
    langs: tuple[str, ...] = ("en", "tc") if lang == "both" else (lang,)

    if skip_if_fresh:
        court_list, langs = _filter_fresh_case_buckets(
            output, court_list, langs,
        )
        if not court_list:
            click.echo("skip-if-fresh: every requested bucket is fresh.")
            return

    asyncio.run(_run_scrape(ScrapeConfig(
        output=output,
        fmt_set=fmt_set,
        proxies=list(proxies),
        direct=direct,
        court_list=court_list,
        limit=limit,
        resume=resume,
        with_summaries=with_summaries,
        with_appeal_history=with_appeal_history,
        langs=langs,
        retry_failed=retry_failed,
        enum_max_age=enum_max_age,
        save_enum_responses=save_enum_responses,
        no_events=no_events,
    )))


@main.command()
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./downloads"),
    help="Directory containing existing downloads + .checkpoint.db.",
)
def verify(output: Path) -> None:
    """Reconcile the checkpoint against on-disk files.

    Iterates rows with status='downloaded' and checks each expected
    format file exists and is non-zero-byte. Rows with missing or
    empty files are flipped back to status='pending' so a subsequent
    `hklii scrape --resume` re-downloads them.

    Fixes the silent-file-loss scenario: rm accident, incomplete
    rsync (`.checkpoint.db` is a dotfile — rsync -r skips by default),
    bit-rot, or partial disk writes.
    """
    from .checkpoint import CheckpointDB

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(f"No checkpoint DB at {db_path}.")
    db = CheckpointDB(str(db_path))
    try:
        broken = db.verify_downloaded_against_files(output)
        click.echo(f"Verified {output}. Broken rows flipped to pending: {broken}")
        stats = db.stats()
        click.echo(f"Post-verify stats: {stats}")
    finally:
        db.close()


@main.command()
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory containing existing downloads + .checkpoint.db.",
)
@click.option(
    "--sample",
    type=int,
    default=None,
    help="Validate a random sample of N downloaded rows (default: all).",
)
@click.option(
    "--seed",
    type=int,
    default=None,
    help="RNG seed for reproducible --sample selection.",
)
@click.option(
    "--checks",
    "checks_str",
    type=str,
    default=None,
    help=(
        "Comma-separated subset of checks to run — "
        "presence,magic,challenge_html,stem_coords,neutral_in_body,"
        "enrichment,orphans,html_pending. Default: all."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON to stdout. Default when stdout is not a TTY.",
)
@click.option(
    "--text",
    "as_text",
    is_flag=True,
    default=False,
    help="Emit human-readable text to stdout. Default when stdout is a TTY.",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the JSON report to FILE (regardless of --json/--text).",
)
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Apply remediations for fatal check-1/2/3 discrepancies.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --fix.",
)
def validate(
    output: Path,
    sample: int | None,
    seed: int | None,
    checks_str: str | None,
    as_json: bool,
    as_text: bool,
    report_path: Path | None,
    fix: bool,
    yes: bool,
) -> None:
    """Audit DB ↔ on-disk agreement across eight reconciliation checks.

    Read-only by default; --fix applies remediations for fatal check-1
    (presence), check-2 (magic), and check-3 (challenge_html) — see
    scratchpad/VALIDATOR_SPEC.md §5 for semantics.

    \b
    Exit codes:
      0  clean
      1  warn-tier discrepancies only (orphans, missing citation)
      2  any fatal discrepancy
      3  cannot open the checkpoint DB / IO error
    """
    import sys

    if as_json and as_text:
        raise click.UsageError("--json and --text are mutually exclusive.")

    from .checkpoint import CheckpointDB
    from .validate import Validator, render_text

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        click.echo(f"No checkpoint DB at {db_path}.", err=True)
        sys.exit(3)

    try:
        db = CheckpointDB(str(db_path))
    except OSError as e:
        click.echo(f"Failed to open {db_path}: {e}", err=True)
        sys.exit(3)

    try:
        check_list = None
        if checks_str:
            check_list = [c.strip() for c in checks_str.split(",") if c.strip()]

        # 0 → full corpus (matches the update dispatcher's semantic
        # convention). Pre-fix, direct `hklii validate --sample 0`
        # examined ZERO rows while the update path treated the same
        # value as unlimited. Aligning at the CLI edge preserves
        # Validator's `sample=None` "no cap" contract.
        sample_arg = None if sample == 0 else sample
        try:
            validator = Validator(
                db, output,
                checks=check_list, sample=sample_arg, seed=seed,
            )
        except ValueError as e:
            raise click.UsageError(str(e))

        report = validator.run()

        if fix:
            # --fix touches fatal check-1/2/3 rows AND orphan warns
            # (spec §5(a)); other severities/checks are left alone.
            actionable = sum(
                1 for d in report.discrepancies
                if (d.severity == "fatal"
                    and d.check in ("presence", "magic", "challenge_html"))
                or (d.severity == "warn" and d.check == "orphans")
            )
            if actionable > 0:
                if not yes:
                    click.confirm(
                        f"Apply --fix remediations for {actionable} "
                        "actionable discrepancy(ies)?",
                        abort=True,
                    )
                applied = validator.apply_fixes(report)
                click.echo(f"Applied {applied} remediation(s).", err=True)
                report = validator.run()

        if report_path:
            report_path.write_text(report.to_json())

        stdout = click.get_text_stream("stdout")
        emit_json = as_json or (not as_text and not stdout.isatty())
        click.echo(report.to_json() if emit_json else render_text(report))

        counts = report.counts["discrepancies_by_severity"]
        if counts["fatal"] > 0:
            sys.exit(2)
        if counts["warn"] > 0:
            sys.exit(1)
        sys.exit(0)
    finally:
        db.close()


@main.command("generate-html")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory containing downloaded artifacts + .checkpoint.db.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N candidates (default: process all).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report candidate count without converting or updating DB.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Retry rows previously marked failed (html_generated_error).",
)
def generate_html(
    output: Path,
    limit: int | None,
    dry_run: bool,
    force: bool,
) -> None:
    """Convert doc-family files to .generated.html for empty-content rows.

    Targets rows with formats=["doc"] only — the empty-content-at-HKLII
    cases where no html/txt/json was ever available upstream. Writes
    {stem}.generated.html alongside the original doc file, and records
    the source extension in html_generated_from.

    Uses pandoc for .docx and .rtf. Plain OLE .doc trampolines through
    `soffice --headless --convert-to docx`; if libreoffice is missing,
    those rows are recorded as failed with an install hint.

    \b
    Examples:
      hklii generate-html
      hklii generate-html --limit 10 --dry-run
      hklii generate-html --force   # retry previously-failed rows
    """
    from .checkpoint import CheckpointDB
    from .html_generator import HtmlGenerator

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(f"No checkpoint DB at {db_path}.")

    db = CheckpointDB(str(db_path))
    try:
        generator = HtmlGenerator(
            db, output,
            limit=limit, include_failed=force, dry_run=dry_run,
        )
        result = generator.generate_all()

        if dry_run:
            click.echo(f"Would process {result.candidates} candidate(s).")
        else:
            click.echo(
                f"Processed {result.candidates}: "
                f"generated={result.generated} failed={result.failed}"
            )
            stats = db.html_generation_stats()
            click.echo(
                f"Overall: generated={stats['generated']} "
                f"failed={stats['failed']} pending={stats['pending']}"
            )
            if stats["by_source_ext"]:
                by = ", ".join(
                    f"{k}={v}" for k, v in sorted(stats["by_source_ext"].items())
                )
                click.echo(f"By source ext: {by}")
    finally:
        db.close()


@main.command()
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Scrape output directory (containing .checkpoint.db).",
)
@click.option(
    "--window-min",
    type=int,
    default=30,
    help="Look back N minutes for \"recent\" events (default: 30).",
)
@click.option(
    "--workers",
    type=int,
    default=20,
    help="Configured worker count, for the in_progress alert (default: 20).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON summary instead of the table.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress all output; just exit with the severity code.",
)
def monitor(
    output: Path,
    window_min: int,
    workers: int,
    as_json: bool,
    quiet: bool,
) -> None:
    """Snapshot the health of a running scrape from its output directory.

    Reads .checkpoint.db + events.jsonl + scrape.log; prints a compact
    summary; exits 0 (healthy) / 1 (warn) / 2 (critical) so a cron job or
    /loop wrapper can escalate during a long production run. Pure reader —
    it never writes to the artifacts it observes.

    \b
    Examples:
      hklii monitor -o ./downloads
      hklii monitor -o ./downloads --json
      hklii monitor -o ./downloads --window-min 60 --workers 20 --quiet
    """
    import sys

    from .monitor import MonitorRunner

    runner = MonitorRunner(output, window_min=window_min, workers=workers)
    summary = runner.run()
    if not quiet:
        click.echo(
            runner.render_json(summary) if as_json
            else runner.render_text(summary)
        )
    sys.exit({"HEALTHY": 0, "WARN": 1, "CRITICAL": 2}[summary["severity"]])


@main.command()
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./downloads"),
    help="Directory containing existing downloads + .checkpoint.db.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--summaries/--no-summaries",
    default=True,
    help="Backfill Press Summary (English + Chinese). Default: on.",
)
@click.option(
    "--appeal-history/--no-appeal-history",
    default=True,
    help="Backfill appeal history JSON. Default: on.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N cases (smoke test).",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct mode.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging to <output>/events.jsonl (storage-constrained runs).",
)
@click.option(
    "--retry-failed",
    is_flag=True,
    default=False,
    help="Flip {kind}_status='failed' back to 'pending' before this run. "
         "Only touches the enrichment kinds this run is doing "
         "(--summaries / --appeal-history).",
)
def enrich(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    summaries: bool,
    appeal_history: bool,
    limit: int | None,
    yes: bool,
    no_events: bool,
    retry_failed: bool,
) -> None:
    """Backfill press summaries + appeal history for already-downloaded cases.

    Reads existing judgment HTML/JSON from the output directory and fetches
    the missing enrichment artifacts. Useful when you scraped without
    --with-summaries / --with-appeal-history and want to add them later,
    or when re-running after extraction logic changed.

    \b
    Examples:
      hklii enrich --proxy http://localhost:8888
      hklii enrich --no-appeal-history --proxy http://localhost:8888
      hklii enrich --direct --yes --limit 10
      hklii enrich --retry-failed --proxy ...   # retry previously-failed rows
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")

    if direct and not yes:
        click.confirm(
            "Enriching without a proxy exposes your IP. Continue?",
            abort=True,
        )

    if not summaries and not appeal_history:
        raise click.UsageError(
            "Nothing to do — pass --summaries or --appeal-history (or both)."
        )

    asyncio.run(_run_enrich(
        output=output,
        proxies=list(proxies),
        direct=direct,
        do_summaries=summaries,
        do_appeal_history=appeal_history,
        limit=limit,
        no_events=no_events,
        retry_failed=retry_failed,
    ))


async def _run_enrich(
    output: Path,
    proxies: list[str],
    direct: bool,
    do_summaries: bool,
    do_appeal_history: bool,
    limit: int | None,
    no_events: bool = False,
    retry_failed: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .enrichment import EnrichmentRunner
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(
            f"No checkpoint DB at {db_path}. Run `hklii scrape` first."
        )
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight — every proxy was "
                    "leaked or unreachable."
                )
            workers = max(1, len(result.healthy_proxies))

        runner = EnrichmentRunner(
            get=pool.get,
            checkpoint=db,
            output_dir=output,
            do_summaries=do_summaries,
            do_appeal_history=do_appeal_history,
            workers=workers,
            limit=limit,
            events=events,
        )

        pending_kinds = []
        if do_summaries:
            pending_kinds += ["summary_en", "summary_zh"]
        if do_appeal_history:
            pending_kinds.append("appeal_history")
        if retry_failed:
            reset_n = db.reset_enrichment_failed_to_pending(pending_kinds)
            click.echo(
                f"--retry-failed: flipped {reset_n} failed enrichment "
                "row(s) to pending."
            )
        pending_cases = db.pending_any_enrichment(pending_kinds)
        target = len(pending_cases) if limit is None else min(limit, len(pending_cases))
        click.echo(f"Pending enrichment for {len(pending_cases)} case(s); target {target}.")

        if target == 0:
            click.echo("Nothing to enrich.")
        else:
            result = await _enrich_with_progress(runner, target)
            click.echo(
                f"\nDone. Processed: {result.processed}, "
                f"Failed: {result.failed}"
            )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _enrich_with_progress(runner, target: int):
    from rich.progress import (
        Progress,
        TextColumn,
        BarColumn,
        MofNCompleteColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    with Progress(
        TextColumn("[bold blue]enrich"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[processed]}"),
        TextColumn("[red]fail {task.fields[failed]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "cases", total=target, processed=0, failed=0,
        )

        def on_progress(stats: dict) -> None:
            progress.update(
                task_id,
                completed=stats["processed"] + stats["failed"],
                processed=stats["processed"],
                failed=stats["failed"],
            )

        return await runner.enrich_all(on_progress=on_progress)


async def _download_with_progress(scraper, target: int):
    from rich.progress import (
        Progress,
        TextColumn,
        BarColumn,
        MofNCompleteColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    with Progress(
        TextColumn("[bold blue]scrape"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[downloaded]}"),
        TextColumn("[red]fail {task.fields[failed]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "downloads", total=target, downloaded=0, failed=0,
        )

        def on_progress(stats: dict) -> None:
            progress.update(
                task_id,
                completed=stats["downloaded"] + stats["failed"],
                downloaded=stats["downloaded"],
                failed=stats["failed"],
            )

        return await scraper.download_all(on_progress=on_progress)


async def _run_scrape(config: ScrapeConfig) -> None:
    """Execute a bulk scrape using the fields in `config`.

    Prior signature had 17+ kwargs; every new toggle risked silent
    divergence between the standalone `scrape` command and
    `_run_update_scrape`. ScrapeConfig is now the single source of intent.
    """
    # Unpack once so the body doesn't have to re-type `config.`; keeps
    # the diff surface small vs. touching every reference below.
    output = config.output
    fmt_set = config.fmt_set
    proxies = config.proxies
    direct = config.direct
    court_list = config.court_list
    limit = config.limit
    resume = config.resume
    with_summaries = config.with_summaries
    with_appeal_history = config.with_appeal_history
    langs = config.langs
    retry_failed = config.retry_failed
    enum_max_age = config.enum_max_age
    save_enum_responses = config.save_enum_responses
    no_events = config.no_events
    window = config.window

    from .logging_setup import setup_logging
    log_path = setup_logging(output, "scrape")
    click.echo(f"Logging to {log_path}")
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool
    from .scraper import BulkScraper

    db_path = output / ".checkpoint.db"
    output.mkdir(parents=True, exist_ok=True)
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
        click.echo(f"Structured events -> {output / 'events.jsonl'}")

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if result.leaked_proxies:
                click.secho(f"Leaked proxies: {result.leaked_proxies}", fg="red", err=True)
            if result.failed_proxies:
                click.secho(f"Failed proxies: {result.failed_proxies}", fg="yellow", err=True)
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight — every proxy was leaked "
                    "or unreachable. Fix the pool (or use --direct) and retry."
                )
            workers = max(1, len(result.healthy_proxies))

        scraper = BulkScraper(
            get=pool.get,
            checkpoint=db,
            output_dir=output,
            formats=fmt_set,
            limit=limit,
            workers=workers,
            with_summaries=with_summaries,
            with_appeal_history=with_appeal_history,
            enum_max_age_hours=enum_max_age,
            save_enum_responses=save_enum_responses,
            events=events,
            window=window,
        )

        if retry_failed:
            n = db.reset_failed_to_pending()
            click.echo(f"Reset {n} failed case(s) to pending for retry.")

        # --resume skips the enumerate pass IF there are already pending
        # rows to work on — the operator is restarting a partial run and
        # doesn't want to burn API budget re-listing every court.
        pre_stats = db.stats()
        if resume and pre_stats["pending"] > 0:
            click.echo(f"Resume: skipping enumeration; {pre_stats['pending']} pending cases already in DB.")
        else:
            click.echo(f"Enumerating courts: {', '.join(court_list)}  langs: {', '.join(langs)}")
            total = await scraper.enumerate(court_list, langs=langs)
            click.echo(f"Found {total} cases.")

        db.release_in_progress()
        stats = db.stats()
        click.echo(f"Pending: {stats['pending']}, Downloaded: {stats['downloaded']}, Failed: {stats['failed']}")

        if stats["pending"] == 0:
            click.echo("Nothing to download.")
        else:
            target = (
                min(limit, stats["pending"]) if limit is not None
                else stats["pending"]
            )
            result = await _download_with_progress(scraper, target)
            click.echo(f"\nDone. Downloaded: {result.downloaded}, Failed: {result.failed}")
        # Freshness ledger close-out: every (court, lang) bucket we
        # just swept gets its last_scrape_completed_at bumped so
        # ``_fresh`` can flip the bucket to FRESH on the next probe.
        # Without this, --skip-if-fresh never skips and hklii check-
        # freshness can never exit 0 — see adversarial D2 finding #1.
        # Placed inside the try so a preflight failure doesn't reach
        # it; placed OUTSIDE the ``stats['pending'] == 0`` branch so
        # a full-scrape run with no pending queue (already downloaded
        # everything) still marks the buckets fresh.
        _mark_case_buckets_scraped(db, court_list, langs)
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


def _mark_case_buckets_scraped(
    db, court_list: list[str], langs: tuple[str, ...],
) -> None:
    """Bump ``last_scrape_completed_at`` on every (court, lang) pair
    just swept. Called by ``_run_scrape`` at clean-completion.

    Extracted so scrape / update-scrape share a single call site — if
    a future scrape helper needs to skip specific buckets (e.g. one
    that failed enum), it can call this helper with the surviving
    list rather than duplicating the freshness wiring.

    Kept small and side-effect-focused: no wire cost, purely a
    db_freshness UPSERT per (court, lang). Errors are caller-visible
    because ``mark_bucket_scraped`` raises rather than swallows.
    """
    import time as _time
    now = int(_time.time())
    for court in court_list:
        for lang in langs:
            db.mark_bucket_scraped(
                "cases", court, lang, completed_at=now,
            )


_print_lock = asyncio.Lock()


async def _download_one(
    url: str,
    output: Path,
    fmt_set: set[str],
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> bool:
    try:
        case = parse_hklii_url(url)
    except ValueError as e:
        async with _print_lock:
            click.secho(f"Skipping invalid URL: {url} ({e})", fg="yellow", err=True)
        return False

    label = f"{case.court.upper()} {case.year}/{case.number}"

    async with sem:
        try:
            judgment = await fetch_judgment(case, client=client)
        except httpx.HTTPStatusError as e:
            async with _print_lock:
                click.secho(f"{label} FAILED ({e.response.status_code})", fg="red", err=True)
            return False
        except httpx.RequestError as e:
            async with _print_lock:
                click.secho(f"{label} FAILED ({e})", fg="red", err=True)
            return False

        try:
            saved = await save_judgment(judgment, output, fmt_set, client=client)
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            async with _print_lock:
                click.secho(f"{label} .doc download failed: {e}", fg="yellow", err=True)
            saved = await save_judgment(judgment, output, fmt_set - {"doc"}, client=client)

    async with _print_lock:
        click.secho(f"{label} ", fg="green", nl=False)
        click.echo(judgment.title)
        click.echo(f"  {judgment.neutral_citation} | {judgment.date[:10]}")
        for path in saved:
            click.echo(f"  -> {path}")

    return True


async def _run(
    urls: tuple[str, ...],
    output: Path,
    fmt_set: set[str],
    proxy: str | None,
    concurrency: int,
) -> None:
    if proxy:
        click.echo(f"Using proxy: {proxy}")

    sem = asyncio.Semaphore(concurrency)

    async with make_async_client(proxy=proxy) as client:
        tasks = [
            _download_one(url, output, fmt_set, client, sem)
            for url in urls
        ]
        results = await asyncio.gather(*tasks)

    ok = sum(results)
    click.echo(f"\nDone. {ok}/{len(urls)} case(s) downloaded.")


@main.command("recheck-html")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./downloads"),
    help="Directory containing existing downloads + .checkpoint.db.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of pending rows rechecked in this pass.",
)
@click.option(
    "--max-age-days",
    type=int,
    default=None,
    help=(
        "Bound the recheck queue by case date. Cases older than N days "
        "(by publication date) are skipped. 0 = unlimited. Default: unbounded."
    ),
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct mode.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging to <output>/events.jsonl (storage-constrained runs).",
)
def recheck_html(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    limit: int | None,
    max_age_days: int | None,
    yes: bool,
    no_events: bool,
) -> None:
    """Re-check rows captured via doc-fallback for newly-available HTML.

    HKLII shows "Only the Word format is available at the moment" on
    very recent judgments; `scrape --allow-doc` captures the .doc/.docx
    and stamps html_pending_at_hklii on those rows. This pass walks
    those rows, re-fetches getjudgment, and saves html/txt/json when
    HKLII has extracted the HTML. Rows still empty at HKLII get their
    timestamp bumped so the FIFO order rechecks them again next pass.

    \b
    Examples:
      hklii recheck-html --proxy http://localhost:8888
      hklii recheck-html --proxy http://localhost:8888 --limit 500
      hklii recheck-html --direct --yes --limit 10
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")

    if direct and not yes:
        click.confirm(
            "Rechecking without a proxy exposes your IP. Continue?",
            abort=True,
        )

    asyncio.run(_run_recheck_html(
        output=output,
        proxies=list(proxies),
        direct=direct,
        limit=limit,
        max_age_days=max_age_days,
        no_events=no_events,
    ))


async def _run_recheck_html(
    output: Path,
    proxies: list[str],
    direct: bool,
    limit: int | None,
    max_age_days: int | None = None,
    no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .html_recheck import HtmlRecheckRunner
    from .proxy_pool import ProxyPool

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(
            f"No checkpoint DB at {db_path}. Run `hklii scrape` first."
        )
    db = CheckpointDB(str(db_path))

    pending_count = len(db.pending_html_recheck(
        limit=None, max_age_days=max_age_days,
    ))
    if pending_count == 0:
        click.echo("No rows are flagged html_pending_at_hklii. Nothing to do.")
        db.close()
        return

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight — every proxy was "
                    "leaked or unreachable."
                )
            workers = max(1, len(result.healthy_proxies))

        target = pending_count if limit is None else min(limit, pending_count)
        click.echo(
            f"Pending html_pending_at_hklii rows: {pending_count}; "
            f"target this pass: {target}."
        )

        runner = HtmlRecheckRunner(
            get=pool.get,
            checkpoint=db,
            output_dir=output,
            workers=workers,
            limit=limit,
            events=events,
            max_age_days=max_age_days,
        )
        counts = await runner.recheck_all()
        click.echo(
            f"\nDone. Newly captured: {counts['newly_captured']}, "
            f"still pending: {counts['still_pending']}, "
            f"failed: {counts['failed']}."
        )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


@main.command("scrape-legis")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + legis artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--abbr",
    "abbr_str",
    type=str,
    default=None,
    help=(
        "Comma-separated capTypes to scrape. Default: ord,reg,instrument. "
        "These are the three non-empty legislation databases per the "
        "2026-07-05 API probe."
    ),
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "sc", "all"]),
    default="all",
    help=(
        "Language(s) to enumerate. Default: all — HKLII serves EN, "
        "TC AND SC for the trilingual legis slugs (ord / reg / "
        "instrument). Pass a specific lang to narrow the sweep."
    ),
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N document fetches (smoke test).",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
@click.option(
    "--skip-if-fresh",
    is_flag=True,
    default=False,
    help=(
        "Consult db_freshness before enumerating and drop (capType, "
        "lang) buckets already marked FRESH. Default OFF."
    ),
)
def scrape_legis(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    abbr_str: str | None,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
    skip_if_fresh: bool,
) -> None:
    """Backup HKLII legislation — ordinances, regulations, instruments.

    Two-phase run:
      1. Enumerate: page through /api/getlegisfiles for each
         (capType, lang) and upsert into legis_documents.
      2. Fetch: drain pending rows through N workers, calling
         getcapversions + getcapversiontoc, writing
         {stem}.versions.json + {stem}.content.json under
         output/legis/{abbr}/{num}/.

    \b
    Examples:
      hklii scrape-legis --proxy http://localhost:8888
      hklii scrape-legis --abbr ord --lang en --limit 5 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")

    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )

    from .legis import LEGIS_CAP_TYPES, LEGIS_LANGS

    cap_types = tuple(
        s.strip() for s in (abbr_str or ",".join(LEGIS_CAP_TYPES)).split(",")
        if s.strip()
    )
    langs = LEGIS_LANGS if lang == "all" else (lang,)

    if skip_if_fresh:
        cap_types, langs = _filter_fresh_hopt_buckets(
            output, cap_types, langs, kind="legis",
        )
        if not cap_types:
            click.echo("skip-if-fresh: every requested capType is fresh.")
            return

    asyncio.run(_run_scrape_legis(
        output=output,
        proxies=list(proxies),
        direct=direct,
        cap_types=cap_types,
        langs=langs,
        limit=limit,
        no_events=no_events,
    ))


async def _run_scrape_legis(
    output: Path,
    proxies: list[str],
    direct: bool,
    cap_types: tuple[str, ...],
    langs: tuple[str, ...],
    limit: int | None,
    no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .legis import LegisRunner
    from .proxy_pool import ProxyPool

    output.mkdir(parents=True, exist_ok=True)
    db_path = output / ".checkpoint.db"
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight — every proxy was "
                    "leaked or unreachable."
                )
            workers = max(1, len(result.healthy_proxies))

        runner = LegisRunner(
            get=pool.get,
            checkpoint=db,
            output_dir=output,
            cap_types=cap_types,
            langs=langs,
            workers=workers,
            limit=limit,
        )

        click.echo(
            f"Enumerating capTypes={list(cap_types)} langs={list(langs)}..."
        )
        upserted = await runner.enumerate_all()
        click.echo(f"Upserted {upserted} legis rows.")

        pending_stats = db.legis_stats()
        target = pending_stats["pending"] if limit is None else min(
            limit, pending_stats["pending"],
        )
        click.echo(
            f"Pending: {pending_stats['pending']}, "
            f"downloaded: {pending_stats['downloaded']}, "
            f"failed: {pending_stats['failed']}. "
            f"target this pass: {target}."
        )

        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            result = await _legis_with_progress(runner, target)
            click.echo(
                f"\nDone. Downloaded: {result.downloaded}, "
                f"Failed: {result.failed}."
            )
            click.echo(f"By capType: {db.legis_stats_by_abbr()}")
        # Freshness ledger close-out — see the equivalent block in
        # _run_scrape (finding #1). Marks every (cap_type, lang) pair
        # so the freshness gate can eventually flip these buckets
        # FRESH.
        import time as _time
        now = int(_time.time())
        for cap_type in cap_types:
            for lang in langs:
                db.mark_bucket_scraped(
                    "legis", cap_type, lang, completed_at=now,
                )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _legis_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )

    with Progress(
        TextColumn("[bold blue]legis"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task("chapters", total=target, ok=0, fail=0)

        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )

        return await runner.fetch_pending(on_progress=on_progress)


@main.command("backfill-legis-history")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + legis artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N version fetches (smoke test).",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
def backfill_legis_history(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    limit: int | None,
    yes: bool,
    no_events: bool,
) -> None:
    """Fill in historical versions for already-scraped legislation.

    Prerequisite: `hklii scrape-legis` has already run, so every row's
    {stem}.versions.json is on disk. This subcommand walks each
    downloaded row, upserts every non-latest vid into legis_versions,
    then drains the queue via getcapversiontoc?id=<vid>. Idempotent —
    vids whose {stem}.v{vid}.content.json already exists are skipped.

    \b
    Examples:
      hklii backfill-legis-history --proxy http://127.0.0.1:8888
      hklii backfill-legis-history --limit 10 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    asyncio.run(_run_backfill_legis_history(
        output=output, proxies=list(proxies), direct=direct,
        limit=limit, no_events=no_events,
    ))


async def _run_backfill_legis_history(
    output: Path, proxies: list[str], direct: bool,
    limit: int | None, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .legis import LegisHistoryRunner
    from .proxy_pool import ProxyPool

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(f"No checkpoint DB at {db_path}.")
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight — every proxy was "
                    "leaked or unreachable."
                )
            workers = max(1, len(result.healthy_proxies))

        runner = LegisHistoryRunner(
            get=pool.get, checkpoint=db, output_dir=output,
            workers=workers, limit=limit,
        )

        click.echo("Enumerating historical versions from on-disk "
                   "versions.json files...")
        upserted = runner.enumerate_pending()
        click.echo(f"Upserted {upserted} pending version rows.")

        stats = db.legis_version_stats()
        target = stats["pending"] if limit is None else min(
            limit, stats["pending"],
        )
        click.echo(
            f"Pending: {stats['pending']}, "
            f"downloaded: {stats['downloaded']}, "
            f"failed: {stats['failed']}. "
            f"target this pass: {target}."
        )

        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            result = await _legis_history_with_progress(runner, target)
            click.echo(
                f"\nDone. Downloaded: {result.downloaded}, "
                f"Failed: {result.failed}."
            )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


@main.command("scrape-relatedcaps")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + legis artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--cap-range",
    "cap_range_str",
    type=str,
    default="1-1200",
    help="Inclusive integer cap range (default: 1-1200).",
)
@click.option(
    "--abbr",
    "abbrs_str",
    type=str,
    default="ord,reg",
    help="Comma-separated abbrs (default: ord,reg).",
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "both"]),
    default="both",
    help="Language(s) (default: both).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N fetches.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
@click.option(
    "--fresh-diff",
    is_flag=True,
    default=False,
    help=(
        "Reset every relatedcap_fetches row to pending before the "
        "sweep so the scraper re-fetches every combo. Matches the "
        "`hklii update --profile quarterly` semantic. Default resumes "
        "from any prior pending/error rows only."
    ),
)
def scrape_relatedcaps(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    cap_range_str: str,
    abbrs_str: str,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
    fresh_diff: bool,
) -> None:
    """Scrape HKLII getrelatedcaps for the ord → reg cross-reference graph.

    Populates ord_reg_edges. Alpha-suffix caps (32A, 622J) are excluded
    at enumeration — HKLII returns 500 on them because num_int can't
    parse letter suffixes.

    Default resume behaviour: only rows currently at status='pending' or
    'error' are re-fetched. Pass --fresh-diff to reset every combo back
    to pending first — mirrors the update dispatcher's
    scrape_relatedcaps semantic (see cli.py:_dispatch_update_plan).

    \b
    Examples:
      hklii scrape-relatedcaps --proxy http://127.0.0.1:8888
      hklii scrape-relatedcaps --cap-range 1-50 --direct --yes
      hklii scrape-relatedcaps --fresh-diff -p ...  # quarterly refresh
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    try:
        lo, hi = (int(x) for x in cap_range_str.split("-", 1))
    except Exception:
        raise click.UsageError(
            f"Bad --cap-range {cap_range_str!r}; expected LO-HI"
        )
    abbrs = tuple(
        s.strip() for s in abbrs_str.split(",") if s.strip()
    )
    langs = ("en", "tc") if lang == "both" else (lang,)
    asyncio.run(_run_scrape_relatedcaps(
        output=output, proxies=list(proxies), direct=direct,
        cap_range=(lo, hi), abbrs=abbrs, langs=langs,
        limit=limit, no_events=no_events,
    ))


async def _run_scrape_relatedcaps(
    output: Path, proxies: list[str], direct: bool,
    cap_range: tuple[int, int],
    abbrs: tuple[str, ...], langs: tuple[str, ...],
    limit: int | None, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool
    from .related_caps import RelatedCapsRunner

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(f"No checkpoint DB at {db_path}.")
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError("No healthy proxies.")
            workers = max(1, len(result.healthy_proxies))

        runner = RelatedCapsRunner(
            get=pool.get, checkpoint=db, output_dir=output,
            cap_range=cap_range, abbrs=abbrs, langs=langs,
            workers=workers, limit=limit,
        )
        if fresh_diff:
            n_reset = db.reset_relatedcap_fetches()
            click.echo(
                f"Reset {n_reset} relatedcap_fetches row(s) to pending "
                "(--fresh-diff)."
            )
        click.echo(
            f"Enumerating cap_range={cap_range} "
            f"abbrs={list(abbrs)} langs={list(langs)}..."
        )
        upserted = runner.enumerate_pending()
        click.echo(f"Upserted {upserted} relatedcaps rows.")

        stats = db.relatedcap_stats()
        target = stats["pending"] if limit is None else min(
            limit, stats["pending"],
        )
        click.echo(
            f"relatedcap_fetches — total={stats['total']} "
            f"pending={stats['pending']} "
            f"in_progress={stats.get('in_progress', 0)} "
            f"ok={stats['ok']} error={stats['error']}. "
            f"target: {target}."
        )
        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            outcome = await _relatedcaps_with_progress(runner, target)
            click.echo(
                f"\nDone. Downloaded: {outcome.downloaded}, "
                f"Failed: {outcome.failed}."
            )
            edge_count = db._conn.execute(
                "SELECT COUNT(*) FROM ord_reg_edges"
            ).fetchone()[0]
            click.echo(f"ord→reg edges in DB: {edge_count:,}")
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _relatedcaps_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )
    with Progress(
        TextColumn("[bold blue]relatedcaps"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "caps", total=target, ok=0, fail=0,
        )
        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )
        return await runner.fetch_pending(on_progress=on_progress)


@main.command("scrape-noteup")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + case artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--court", "courts_str",
    type=str,
    default=None,
    help="Comma-separated court slugs (e.g. hkcfa,hkca). Default: all.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N fetches.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
def scrape_noteup(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    courts_str: str | None,
    limit: int | None,
    yes: bool,
    no_events: bool,
) -> None:
    """Scrape HKLII getcasenoteup for every downloaded case.

    Populates the citations edges table (from_key → to_key), the
    case_parallel_cites de-dup table, and the noteup_fetches per-source
    tracker. Idempotent — resumes cleanly from partial runs.

    \b
    Examples:
      hklii scrape-noteup --proxy http://127.0.0.1:8888
      hklii scrape-noteup --court hkcfa --limit 100 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    courts = None
    if courts_str:
        courts = tuple(c.strip() for c in courts_str.split(",") if c.strip())
    asyncio.run(_run_scrape_noteup(
        output=output, proxies=list(proxies), direct=direct,
        courts=courts, limit=limit, no_events=no_events,
    ))


async def _run_scrape_noteup(
    output: Path, proxies: list[str], direct: bool,
    courts: tuple[str, ...] | None,
    limit: int | None, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .citations import NoteupRunner
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(f"No checkpoint DB at {db_path}.")
    db = CheckpointDB(str(db_path))

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)

    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError("No healthy proxies after preflight.")
            workers = max(1, len(result.healthy_proxies))

        # Court filter enumerates only via a scope-limited SELECT — build
        # a small runner override.
        runner = NoteupRunner(
            get=pool.get, checkpoint=db, output_dir=output,
            workers=workers, limit=limit,
        )
        if courts:
            # Enumerate manually for court subset
            click.echo(
                f"Enumerating noteup targets for courts={list(courts)}..."
            )
            for court in courts:
                rows = db._conn.execute(
                    "SELECT court, year, number FROM cases "
                    "WHERE status='downloaded' AND court=?",
                    (court,),
                ).fetchall()
                for c, y, n in rows:
                    db.upsert_noteup_fetch(c, y, n)
        else:
            click.echo("Enumerating noteup targets across all courts...")
            runner.enumerate_pending()

        stats = db.noteup_stats()
        target = stats["pending"] if limit is None else min(
            limit, stats["pending"],
        )
        click.echo(
            f"noteup_fetches — total={stats['total']}, "
            f"pending={stats['pending']}, "
            f"in_progress={stats.get('in_progress', 0)}, "
            f"ok={stats['ok']}, error={stats['error']}. "
            f"target this pass: {target}."
        )
        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            outcome = await _noteup_with_progress(runner, target)
            click.echo(
                f"\nDone. Downloaded: {outcome.downloaded}, "
                f"Failed: {outcome.failed}."
            )
            edge_count = db._conn.execute(
                "SELECT COUNT(*) FROM citations"
            ).fetchone()[0]
            click.echo(f"Citation edges in DB: {edge_count:,}")
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _noteup_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )
    with Progress(
        TextColumn("[bold blue]noteup"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "cases", total=target, ok=0, fail=0,
        )
        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )
        return await runner.fetch_pending(on_progress=on_progress)


@main.command("backfill-case-translations")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + case artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N fetches (smoke test).",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
def backfill_case_translations(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    limit: int | None,
    yes: bool,
    no_events: bool,
) -> None:
    """Fetch TC counterparts for EN judgments with has_translation=True.

    Original scrape used --lang both with EN-wins semantics, so some
    bilingual cases lost their TC translation. This subcommand walks
    disk, reads each JSON's has_translation flag, and fills the gap by
    fetching getjudgment?lang=tc + saving {stem}.tc.{html,txt,json}
    sidecars alongside the EN files.

    Idempotent — sidecar-existence check makes re-runs skip what's on
    disk. No DB migration; state lives on disk.

    \b
    Examples:
      hklii backfill-case-translations --proxy http://127.0.0.1:8888
      hklii backfill-case-translations --limit 5 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    asyncio.run(_run_backfill_case_translations(
        output=output, proxies=list(proxies), direct=direct,
        limit=limit, no_events=no_events,
    ))


async def _run_backfill_case_translations(
    output: Path, proxies: list[str], direct: bool,
    limit: int | None, no_events: bool = False,
) -> None:
    from .case_translations import (
        CaseTranslationRunner, find_translation_targets,
    )
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool

    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)
    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight."
                )
            workers = max(1, len(result.healthy_proxies))

        click.echo("Scanning disk for has_translation=True judgments...")
        target_count = sum(1 for _ in find_translation_targets(output))
        click.echo(f"Found {target_count} pending translation(s).")
        effective = min(limit, target_count) if limit is not None \
            else target_count

        if effective == 0:
            click.echo("Nothing to fetch.")
            return

        runner = CaseTranslationRunner(
            get=pool.get, output_dir=output,
            workers=workers, limit=limit,
        )
        outcome = await _translations_with_progress(runner, effective)
        click.echo(
            f"\nDone. Downloaded: {outcome.downloaded}, "
            f"Failed: {outcome.failed}."
        )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()


async def _translations_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )
    with Progress(
        TextColumn("[bold blue]tc"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "translations", total=target, ok=0, fail=0,
        )
        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )
        return await runner.run(on_progress=on_progress)


@main.command("scrape-hopt")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + hopt artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--abbr", "abbr_str",
    type=str,
    default=None,
    help="Comma-separated abbrs. Default: bacpg,bahkg,hktmc,hktml,hkts.",
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "both"]),
    default="both",
    help="Language(s). Default: both.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N document fetches.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
@click.option(
    "--skip-if-fresh",
    is_flag=True,
    default=False,
    help=(
        "Consult db_freshness before enumerating and drop (abbr, lang) "
        "buckets already marked FRESH. Default OFF."
    ),
)
def scrape_hopt(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    abbr_str: str | None,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
    skip_if_fresh: bool,
) -> None:
    """Backup HKLII HOPT databases — treaties, gazettes, consultation papers.

    Two-phase run: enumerate via gethoptfiles → fetch via gettreaty.
    Note bacpg + bahkg share the wire abbr "hktba" but are stored under
    their SPA-route abbr on disk (output/hopt/bacpg/... vs bahkg/...).

    \b
    Examples:
      hklii scrape-hopt --proxy http://127.0.0.1:8888
      hklii scrape-hopt --abbr hkts --lang en --limit 5 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    from .hopt import HOPT_ABBRS, HOPT_LANGS
    abbrs = tuple(
        s.strip() for s in (abbr_str or ",".join(HOPT_ABBRS)).split(",")
        if s.strip()
    )
    langs = HOPT_LANGS if lang == "both" else (lang,)
    if skip_if_fresh:
        abbrs, langs = _filter_fresh_hopt_buckets(
            output, abbrs, langs, kind="hopt",
        )
        if not abbrs:
            click.echo("skip-if-fresh: every requested abbr is fresh.")
            return
    asyncio.run(_run_scrape_hopt(
        output=output, proxies=list(proxies), direct=direct,
        abbrs=abbrs, langs=langs, limit=limit, no_events=no_events,
    ))


async def _run_scrape_hopt(
    output: Path, proxies: list[str], direct: bool,
    abbrs: tuple[str, ...], langs: tuple[str, ...],
    limit: int | None, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .hopt import HoptRunner
    from .proxy_pool import ProxyPool

    output.mkdir(parents=True, exist_ok=True)
    db_path = output / ".checkpoint.db"
    db = CheckpointDB(str(db_path))
    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)
    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight."
                )
            workers = max(1, len(result.healthy_proxies))

        runner = HoptRunner(
            get=pool.get, checkpoint=db, output_dir=output,
            abbrs=abbrs, langs=langs, workers=workers, limit=limit,
        )

        click.echo(
            f"Enumerating abbrs={list(abbrs)} langs={list(langs)}..."
        )
        upserted = await runner.enumerate_all()
        click.echo(f"Upserted {upserted} hopt rows.")

        stats = db.hopt_stats()
        target = stats["pending"] if limit is None else min(
            limit, stats["pending"],
        )
        click.echo(
            f"Pending: {stats['pending']}, "
            f"downloaded: {stats['downloaded']}, "
            f"failed: {stats['failed']}. target this pass: {target}."
        )
        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            result = await _hopt_with_progress(runner, target)
            click.echo(
                f"\nDone. Downloaded: {result.downloaded}, "
                f"Failed: {result.failed}."
            )
            click.echo(f"By abbr: {db.hopt_stats_by_abbr()}")
        # Freshness ledger close-out — see finding #1.
        import time as _time
        now = int(_time.time())
        for abbr in abbrs:
            for lang in langs:
                db.mark_bucket_scraped(
                    "hopt", abbr, lang, completed_at=now,
                )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _hopt_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )
    with Progress(
        TextColumn("[bold blue]hopt"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "hopt", total=target, ok=0, fail=0,
        )
        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )
        return await runner.fetch_pending(on_progress=on_progress)


@main.command("scrape-d3")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + d3 artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--slug", "slug_str",
    type=str,
    default=None,
    help=(
        "Comma-separated D3 slugs. Default: histlaw,hkiac,hklrccp,"
        "hklrcr,pcpdaab,pcpdc."
    ),
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "sc", "all"]),
    default="all",
    help="Language(s). Default: all (en+tc+sc).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N document fetches.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
@click.option(
    "--skip-if-fresh",
    is_flag=True,
    default=False,
    help=(
        "Consult db_freshness (kind='hopt') and drop (slug, lang) "
        "buckets already marked FRESH. Default OFF."
    ),
)
def scrape_d3(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    slug_str: str | None,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
    skip_if_fresh: bool,
) -> None:
    """Scrape HKLII D3 families (historical laws, HKIAC, HKLRC, PCPD).

    Two-hop for PDF slugs (histlaw, hkiac, pcpdaab): metadata JSON →
    PDF binary → optional pdftotext sidecar. Single-hop for HTML slugs
    (hklrccp, hklrcr, pcpdc). Rows land in ``hopt_documents`` under
    ``abbr={slug}`` so the D2 freshness ledger auto-inherits.

    \b
    Examples:
      hklii scrape-d3 --proxy http://127.0.0.1:8888
      hklii scrape-d3 --slug hklrccp --lang en --limit 5 --direct --yes
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    from .d3 import D3_FAMILIES, D3_LANGS
    default_slugs = tuple(f.slug for f in D3_FAMILIES)
    slugs = tuple(
        s.strip() for s in (slug_str or ",".join(default_slugs)).split(",")
        if s.strip()
    )
    langs = D3_LANGS if lang == "all" else (lang,)
    if skip_if_fresh:
        slugs, langs = _filter_fresh_hopt_buckets(
            output, slugs, langs, kind="hopt",
        )
        if not slugs:
            click.echo("skip-if-fresh: every requested slug is fresh.")
            return
    asyncio.run(_run_scrape_d3(
        output=output, proxies=list(proxies), direct=direct,
        slugs=slugs, langs=langs, limit=limit, no_events=no_events,
    ))


async def _run_scrape_d3(
    output: Path, proxies: list[str], direct: bool,
    slugs: tuple[str, ...], langs: tuple[str, ...],
    limit: int | None, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .d3 import D3_FAMILIES, D3Runner
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool

    output.mkdir(parents=True, exist_ok=True)
    db_path = output / ".checkpoint.db"
    db = CheckpointDB(str(db_path))
    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)
    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            preflight = await pool.preflight()
            click.echo(f"Home IP: {preflight.home_ip}")
            click.echo(
                f"Healthy proxies: {len(preflight.healthy_proxies)}",
            )
            if not preflight.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight."
                )
            workers = max(1, len(preflight.healthy_proxies))

        families = tuple(
            f for f in D3_FAMILIES if f.slug in slugs
        )
        runner = D3Runner(
            get=pool.get, checkpoint=db, output_dir=output,
            families=families, langs=langs, workers=workers, limit=limit,
        )

        click.echo(
            f"Enumerating slugs={list(slugs)} langs={list(langs)}...",
        )
        upserted = await runner.enumerate_all()
        click.echo(f"Upserted {upserted} d3 rows.")

        stats = db.hopt_stats()
        target = stats["pending"] if limit is None else min(
            limit, stats["pending"],
        )
        click.echo(
            f"Pending: {stats['pending']}, "
            f"downloaded: {stats['downloaded']}, "
            f"failed: {stats['failed']}. target this pass: {target}.",
        )
        if target == 0:
            click.echo("Nothing to fetch.")
        else:
            result = await runner.fetch_pending(limit=limit)
            click.echo(
                f"\nDone. Downloaded: {result.downloaded}, "
                f"Failed: {result.failed}.",
            )
            click.echo(f"By abbr: {db.hopt_stats_by_abbr()}")

        # Freshness ledger close-out — mark only (slug, lang) pairs
        # whose enum actually returned a valid wire read. En-only slugs
        # whose TC/SC bucket returned totalfiles=0 STILL flip FRESH
        # (langs_enumerated captures them).
        import time as _time
        now = int(_time.time())
        for slug, enum_langs in runner.langs_enumerated.items():
            for lang in enum_langs:
                db.mark_bucket_scraped(
                    "hopt", slug, lang, completed_at=now,
                )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


@main.command("scrape-ukpc")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB + ukpc artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--lang",
    type=click.Choice(["en", "tc", "both"]),
    default="en",
    help=(
        "Language(s) to enumerate. Default: en (UKPC is EN-only per "
        "/databases; --lang both stays best-effort in case HKLII "
        "later ships a TC translation)."
    ),
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after N document fetches (smoke test).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Re-fetch and overwrite already-downloaded rows. Default: skip "
        "any (year, num) already at status='downloaded' in the cases "
        "table (idempotent resume)."
    ),
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
@click.option(
    "--skip-if-fresh",
    is_flag=True,
    default=False,
    help=(
        "Consult db_freshness before enumerating; short-circuit if "
        "every requested lang for ukpc is FRESH. Default OFF."
    ),
)
def scrape_ukpc(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    lang: str,
    limit: int | None,
    force: bool,
    yes: bool,
    no_events: bool,
    skip_if_fresh: bool,
) -> None:
    """Scrape HKLII UKPC — Privy Council judgments (hopt-C family).

    UKPC lives on the ``gethoptfiles?dbcat=C`` enumeration endpoint and
    ``getother`` fetch endpoint — a distinct wire family from the 12
    case-family courts in ``ALL_COURTS``. Judgments land at
    ``output/ukpc/YYYY/ukpc_YYYY_NUM.{html,txt,json}`` (case-family
    layout so the viewer's render pipeline treats UKPC identically) and
    are written to the cases table at ``court='ukpc'``,
    ``status='downloaded'`` immediately — never sit at 'pending'
    because ``BulkScraper.claim_pending()`` is unscoped and would hit
    the wrong endpoint family.

    \b
    Examples:
      hklii scrape-ukpc --proxy http://127.0.0.1:8888
      hklii scrape-ukpc --direct --yes --limit 5
      hklii scrape-ukpc --force -p ...  # re-fetch even if downloaded
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Scraping without a proxy exposes your IP. Continue?",
            abort=True,
        )
    from .ukpc import HOPT_C_LANGS
    langs = HOPT_C_LANGS if lang == "both" else (lang,)
    if skip_if_fresh:
        # UKPC is stored under kind='cases' with scope='ukpc' — its rows
        # live in the cases table (see ukpc.py). Reuse the case-family
        # helper on a synthetic one-court list so freshness dispatch
        # follows the checkpoint kind for reads.
        surviving, langs = _filter_fresh_case_buckets(
            output, ["ukpc"], langs,
        )
        if not surviving:
            click.echo("skip-if-fresh: ukpc is fresh — nothing to do.")
            return
    asyncio.run(_run_scrape_ukpc(
        output=output, proxies=list(proxies), direct=direct,
        langs=langs, limit=limit, force=force, no_events=no_events,
    ))


async def _run_scrape_ukpc(
    output: Path, proxies: list[str], direct: bool,
    langs: tuple[str, ...],
    limit: int | None, force: bool = False, no_events: bool = False,
) -> None:
    from .checkpoint import CheckpointDB
    from .events import StructuredEventLogger
    from .proxy_pool import ProxyPool
    from .ukpc import UkpcRunner

    output.mkdir(parents=True, exist_ok=True)
    db_path = output / ".checkpoint.db"
    db = CheckpointDB(str(db_path))
    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)
    try:
        if not direct:
            click.echo("Running preflight IP checks...")
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}")
            click.echo(f"Healthy proxies: {len(result.healthy_proxies)}")
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight."
                )
            workers = max(1, len(result.healthy_proxies))

        runner = UkpcRunner(
            get=pool.get, checkpoint=db, output_dir=output,
            langs=langs, workers=workers, limit=limit, force=force,
        )

        # Pre-count already-downloaded UKPC rows so the operator can
        # tell "skipped resume" from "nothing to fetch" in the final
        # summary line.
        pre_count = db._conn.execute(
            "SELECT COUNT(*) FROM cases "
            "WHERE court='ukpc' AND status='downloaded'"
        ).fetchone()[0]
        click.echo(
            f"Enumerating ukpc langs={list(langs)} "
            f"(pre-existing downloaded rows: {pre_count})..."
        )

        outcome = await _ukpc_with_progress(runner)
        click.echo(
            f"\nDone. Downloaded: {outcome.downloaded}, "
            f"Failed: {outcome.failed}."
        )
        total_count = db._conn.execute(
            "SELECT COUNT(*) FROM cases "
            "WHERE court='ukpc' AND status='downloaded'"
        ).fetchone()[0]
        click.echo(f"UKPC rows now in cases table: {total_count}")
        # Freshness ledger close-out — see finding #1. UKPC rows live
        # in the cases table so kind='cases' matches its DatabaseMatrix
        # classification (see freshness.classify's cases-ukpc branch).
        #
        # Iterate ``outcome.langs_enumerated`` — the langs whose
        # ``gethoptfiles`` enum actually completed — rather than the
        # user-passed ``langs`` tuple. UKPC's TC endpoint 500's at
        # HKLII today, so passing ``--lang both`` would previously
        # stamp ``cases/ukpc/tc.last_scrape_completed_at`` even though
        # no wire read confirmed the state; the freshness ledger then
        # treated tc as "swept" on the next evaluation, hiding a
        # phantom row. See docs/freshness-sanity-check.md for the
        # 2026-07-08 retro.
        import time as _time
        now = int(_time.time())
        for lang in outcome.langs_enumerated:
            db.mark_bucket_scraped(
                "cases", "ukpc", lang, completed_at=now,
            )
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


async def _ukpc_with_progress(runner):
    """Rich progress bar around ``UkpcRunner.run``. Total is unknown
    up-front (enum happens inside ``run``), so total=None → indeterminate
    bar that just shows ok/fail counters + elapsed."""
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
    )
    with Progress(
        TextColumn("[bold blue]ukpc"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
    ) as progress:
        task_id = progress.add_task(
            "ukpc", total=None, ok=0, fail=0,
        )

        def on_progress(stats):
            progress.update(
                task_id,
                ok=stats.downloaded, fail=stats.failed,
            )

        return await runner.run(on_progress=on_progress)


async def _legis_history_with_progress(runner, target: int):
    from rich.progress import (
        Progress, TextColumn, BarColumn,
        MofNCompleteColumn, TaskProgressColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )

    with Progress(
        TextColumn("[bold blue]history"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[green]ok {task.fields[ok]}"),
        TextColumn("[red]fail {task.fields[fail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "versions", total=target, ok=0, fail=0,
        )

        def on_progress(stats):
            progress.update(
                task_id,
                completed=stats.downloaded + stats.failed,
                ok=stats.downloaded, fail=stats.failed,
            )

        return await runner.fetch_pending(on_progress=on_progress)


# -------- hklii update -------------------------------------------------

_UPDATE_PROFILES = ("daily", "weekly", "monthly", "quarterly", "custom")


@main.command("update")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding checkpoint + artifacts.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable.",
)
@click.option(
    "--direct", is_flag=True, default=False,
    help="Skip proxy pool (prompts unless --yes).",
)
@click.option(
    "--profile",
    type=click.Choice(_UPDATE_PROFILES, case_sensitive=False),
    default="daily",
    help="Cadence preset: daily | weekly | monthly | quarterly | custom.",
)
@click.option("--recent-days", type=int, default=None,
              help="Override profile's date window (today HKT - N days).")
@click.option("--items-per-page", type=int, default=None,
              help="Override profile's page size (default 500).")
@click.option("--recheck-max-age-days", type=int, default=None,
              help="Override profile's recheck queue age bound.")
@click.option("--generate-html-limit", type=int, default=None,
              help="Cap local doc→HTML per run.")
@click.option("--enrich-retry-limit", type=int, default=None,
              help="Cap enrichment retries per run.")
@click.option("--canary-divergence-threshold", type=int, default=None,
              help="Row-count divergence per bucket that escalates the canary.")
@click.option("--validate-sample", type=int, default=None,
              help="Row count for validate sampling (default 2000; 0 = full corpus).")
@click.option("--yes-narrow", is_flag=True, default=False,
              help="Required guard to allow --recent-days < 2.")
@click.option("--include-freshness-check/--no-freshness-check", default=None)
@click.option("--include-scrape/--no-scrape", default=None)
@click.option("--include-recheck-html/--no-recheck-html", default=None)
@click.option("--include-generate-html/--no-generate-html", default=None)
@click.option("--include-noteup/--no-noteup", default=None)
@click.option("--include-enrich/--no-enrich", default=None)
@click.option("--include-canary/--no-canary", default=None)
@click.option("--include-hopt/--no-hopt", default=None)
@click.option("--include-ukpc/--no-ukpc", default=None)
@click.option("--include-legis/--no-legis", default=None)
@click.option("--include-legis-history/--no-legis-history", default=None)
@click.option("--include-relatedcaps/--no-relatedcaps", default=None)
@click.option("--include-backfill-translations/--no-backfill-translations",
              default=None)
@click.option("--include-validate/--no-validate", default=None)
@click.option("--include-full-reconcile/--no-full-reconcile", default=None)
@click.option("--include-orphan-mark/--no-orphan-mark", default=None)
@click.option("--dry-run", is_flag=True, default=False,
              help="Print planned steps + estimated call counts. No wire, no DB writes.")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip --direct IP-leak confirmation.")
@click.option("--no-events", is_flag=True, default=False,
              help="Skip structured event logging to <output>/events.jsonl.")
def update_command(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    profile: str,
    recent_days: int | None,
    items_per_page: int | None,
    recheck_max_age_days: int | None,
    generate_html_limit: int | None,
    enrich_retry_limit: int | None,
    canary_divergence_threshold: int | None,
    validate_sample: int | None,
    yes_narrow: bool,
    include_freshness_check: bool | None,
    include_scrape: bool | None,
    include_recheck_html: bool | None,
    include_generate_html: bool | None,
    include_noteup: bool | None,
    include_enrich: bool | None,
    include_canary: bool | None,
    include_hopt: bool | None,
    include_ukpc: bool | None,
    include_legis: bool | None,
    include_legis_history: bool | None,
    include_relatedcaps: bool | None,
    include_backfill_translations: bool | None,
    include_validate: bool | None,
    include_full_reconcile: bool | None,
    include_orphan_mark: bool | None,
    dry_run: bool,
    yes: bool,
    no_events: bool,
) -> None:
    """Profile-driven incremental refresh.

    Composes existing subcommands (scrape / recheck-html / generate-html /
    scrape-noteup / enrich / …) into a cadence-appropriate plan. Every
    profile is idempotent — running a heavier profile than needed just
    burns a few extra enum calls.

    \b
    Examples:
      hklii update -p http://127.0.0.1:8888          # daily default
      hklii update --profile weekly -p http://127.0.0.1:8888
      hklii update --profile monthly -p http://127.0.0.1:8888
      hklii update --profile daily --dry-run -p http://127.0.0.1:8888
    """
    from .update import UpdateLockHeldError, UpdateRunner, UpdateRunnerError

    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    if direct and not yes:
        click.confirm(
            "Running update without a proxy exposes your IP. Continue?",
            abort=True,
        )

    try:
        runner = UpdateRunner(
            profile=profile,
            output=output,
            proxies=list(proxies),
            direct=direct,
            recent_days=recent_days,
            items_per_page=items_per_page,
            recheck_max_age_days=recheck_max_age_days,
            generate_html_limit=generate_html_limit,
            enrich_retry_limit=enrich_retry_limit,
            canary_divergence_threshold=canary_divergence_threshold,
            validate_sample=validate_sample,
            include_freshness_check=include_freshness_check,
            include_scrape=include_scrape,
            include_recheck_html=include_recheck_html,
            include_generate_html=include_generate_html,
            include_noteup=include_noteup,
            include_enrich=include_enrich,
            include_canary=include_canary,
            include_hopt=include_hopt,
            include_ukpc=include_ukpc,
            include_legis=include_legis,
            include_legis_history=include_legis_history,
            include_relatedcaps=include_relatedcaps,
            include_backfill_translations=include_backfill_translations,
            include_validate=include_validate,
            include_full_reconcile=include_full_reconcile,
            include_orphan_mark=include_orphan_mark,
            yes_narrow=yes_narrow,
        )
    except UpdateRunnerError as exc:
        raise click.UsageError(str(exc))

    # Advisory lock: acquire before any wire calls or DB writes.
    # Dry-run STILL takes the lock so a concurrent live run can't sneak
    # in while the operator is inspecting a plan.
    try:
        lock_fd = runner.acquire_lock()
    except UpdateLockHeldError as exc:
        click.secho(f"lock held: {exc}", fg="red", err=True)
        raise click.exceptions.Exit(code=2)

    # Preflight: peek at the shared CheckpointDB lock so we fail fast
    # if a standalone writer (`hklii scrape`, `hklii enrich`, etc.) is
    # already running. Without this, update's steps would each open
    # CheckpointDB and trip a CheckpointLockError mid-run — noisier and
    # harder to diagnose than a single "peer writer running" exit at
    # start. This is a peek only; the actual lock is acquired per-step
    # by CheckpointDB.__init__ as before.
    from .checkpoint import CheckpointDB
    db_path = output / ".checkpoint.db"
    if db_path.exists() and CheckpointDB.is_locked_by_peer(str(db_path)):
        click.secho(
            "checkpoint db lock is held by another writer "
            f"({db_path}.lock). Wait for the peer scrape/enrich/etc. "
            "to finish or kill it.",
            fg="red", err=True,
        )
        runner.release_lock(lock_fd)
        raise click.exceptions.Exit(code=2)

    try:
        if dry_run:
            click.echo(runner.format_plan())
            click.echo("")
            click.echo("(dry-run — no wire calls or DB writes performed)")
            return

        # Live-run dispatch — walk the plan and delegate.
        # NOTE: live execution is deliberately minimal in this initial
        # ship. Each step is dispatched to its corresponding _run_* helper.
        # Coverage-canary and orphan-mark are internal to update.py.
        failures = asyncio.run(
            _dispatch_update_plan(runner, no_events=no_events)
        )
        if failures:
            click.secho(
                f"update: {failures} step(s) failed", fg="red", err=True,
            )
            raise click.exceptions.Exit(code=1)
    finally:
        runner.release_lock(lock_fd)


async def _dispatch_update_plan(runner, no_events: bool) -> int:
    """Execute an UpdateRunner plan against the real subcommand helpers.

    Each step calls the same _run_* helper the standalone subcommand uses;
    steps that don't map cleanly (coverage_canary, orphan_mark) run inline.

    Returns the count of steps that raised; the CLI turns non-zero into
    a non-zero exit code so `hklii update && …` chaining works. Also
    prints an end-of-run summary line grouping per-step ok/failed state.
    """
    plan = runner.plan()
    failures = 0
    step_states: list[tuple[str, str]] = []  # (name, "ok" | "FAIL: <err>")
    for step in plan:
        click.echo(f"→ {step.name}")
        try:
            if step.name == "check_freshness":
                await _run_update_check_freshness(runner, step, no_events)
            elif step.name == "scrape":
                await _run_update_scrape(runner, step, no_events)
            elif step.name == "recheck_html":
                await _run_recheck_html(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    limit=step.kwargs.get("limit"),
                    max_age_days=step.kwargs.get("max_age_days"),
                    no_events=no_events,
                )
            elif step.name == "generate_html":
                _run_update_generate_html(runner, step)
            elif step.name == "scrape_noteup":
                await _run_scrape_noteup(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    courts=None,
                    limit=None, no_events=no_events,
                )
            elif step.name == "enrich":
                await _run_enrich(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    do_summaries=True, do_appeal_history=True,
                    limit=step.kwargs.get("retry_limit"),
                    no_events=no_events,
                )
            elif step.name == "coverage_canary":
                await _run_coverage_canary(runner, step, no_events)
            elif step.name == "scrape_hopt":
                # Consult db_freshness the same way _run_update_scrape
                # does for case-family. Freshness-check is on by default
                # for every profile that has scrape_hopt in its plan;
                # if it was disabled (custom profile without
                # include_freshness_check), the filter helper falls
                # through and returns the full input untouched.
                hopt_abbrs, hopt_langs = _filter_fresh_hopt_buckets(
                    runner.output,
                    ("bacpg", "bahkg", "hktmc", "hktml", "hkts"),
                    ("en", "tc"),
                    kind="hopt",
                ) if runner.settings.get("include_freshness_check") else (
                    ("bacpg", "bahkg", "hktmc", "hktml", "hkts"),
                    ("en", "tc"),
                )
                if not hopt_abbrs:
                    click.echo(
                        "  update scrape_hopt: every abbr FRESH — "
                        "skipping (freshness-scoped)."
                    )
                else:
                    await _run_scrape_hopt(
                        output=runner.output,
                        proxies=runner.proxies,
                        direct=runner.direct,
                        abbrs=hopt_abbrs,
                        langs=hopt_langs,
                        limit=None, no_events=no_events,
                    )
            elif step.name == "scrape_d3":
                # D3 rows live under kind='hopt' in db_freshness so the
                # same hopt freshness filter applies — pass the D3
                # slugs and langs explicitly.
                from .d3 import D3_FAMILIES, D3_LANGS
                d3_all_slugs = tuple(f.slug for f in D3_FAMILIES)
                if runner.settings.get("include_freshness_check"):
                    d3_slugs, d3_langs = _filter_fresh_hopt_buckets(
                        runner.output, d3_all_slugs, D3_LANGS,
                        kind="hopt",
                    )
                else:
                    d3_slugs, d3_langs = d3_all_slugs, D3_LANGS
                if not d3_slugs:
                    click.echo(
                        "  update scrape_d3: every slug FRESH — "
                        "skipping (freshness-scoped)."
                    )
                else:
                    await _run_scrape_d3(
                        output=runner.output,
                        proxies=runner.proxies,
                        direct=runner.direct,
                        slugs=d3_slugs,
                        langs=d3_langs,
                        limit=None, no_events=no_events,
                    )
            elif step.name == "scrape_ukpc":
                # UKPC is EN-only per /databases (2026-07-08). The
                # runner's TC enum is best-effort and no-ops with a
                # WARNING log if HKLII still 500s on that endpoint.
                # Freshness dispatch: UKPC lives under kind='cases'
                # (see _CATEGORY_TO_KIND) so ``_filter_fresh_case_buckets``
                # is the right dispatcher — not the hopt one.
                from .ukpc import HOPT_C_LANGS
                if runner.settings.get("include_freshness_check"):
                    # UKPC is EN-only per /databases; freshness only
                    # tracks 'en'. Pass ("en",) so all_fresh isn't
                    # falsified by a TC row that was never expected
                    # to exist. The scrape helper's own runner still
                    # attempts both langs and no-ops on TC 500.
                    ukpc_courts, _ = _filter_fresh_case_buckets(
                        runner.output, ["ukpc"], ("en",),
                    )
                else:
                    ukpc_courts = ["ukpc"]
                if not ukpc_courts:
                    click.echo(
                        "  update scrape_ukpc: ukpc FRESH — "
                        "skipping (freshness-scoped)."
                    )
                else:
                    await _run_scrape_ukpc(
                        output=runner.output,
                        proxies=runner.proxies,
                        direct=runner.direct,
                        langs=HOPT_C_LANGS,
                        limit=None, force=False, no_events=no_events,
                    )
            elif step.name == "scrape_legis":
                # LEGIS_LANGS is the source of truth for lang coverage.
                # As of 2026-07-08 it's (en, tc, sc) — HKLII serves SC
                # for the three trilingual legis slugs.
                from .legis import LEGIS_LANGS
                if runner.settings.get("include_freshness_check"):
                    legis_types, legis_langs = _filter_fresh_hopt_buckets(
                        runner.output,
                        ("ord", "reg", "instrument"),
                        LEGIS_LANGS,
                        kind="legis",
                    )
                else:
                    legis_types, legis_langs = (
                        ("ord", "reg", "instrument"), LEGIS_LANGS,
                    )
                if not legis_types:
                    click.echo(
                        "  update scrape_legis: every cap_type FRESH — "
                        "skipping (freshness-scoped)."
                    )
                else:
                    await _run_scrape_legis(
                        output=runner.output,
                        proxies=runner.proxies,
                        direct=runner.direct,
                        cap_types=legis_types,
                        langs=legis_langs,
                        limit=None,
                        no_events=no_events,
                    )
            elif step.name == "backfill_legis_history":
                await _run_backfill_legis_history(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    limit=None,
                    no_events=no_events,
                )
            elif step.name == "backfill_case_translations":
                await _run_backfill_case_translations(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    limit=None,
                    no_events=no_events,
                )
            elif step.name == "scrape_relatedcaps":
                # Fresh-diff pattern: reset relatedcap_fetches to pending so
                # the scraper re-fetches every combo and updates ord_reg_edges
                # via INSERT OR IGNORE. Idempotent w.r.t. edges themselves.
                _reset_relatedcap_fetches_via_checkpoint(runner.output)
                await _run_scrape_relatedcaps(
                    output=runner.output,
                    proxies=runner.proxies,
                    direct=runner.direct,
                    cap_range=(1, 1200),
                    abbrs=("ord", "reg"),
                    langs=("en", "tc"),
                    limit=None,
                    no_events=no_events,
                )
            elif step.name == "validate":
                _run_update_validate(runner, step)
            elif step.name == "full_reconcile":
                await _run_update_scrape(runner, step, no_events)
            elif step.name == "orphan_mark":
                _run_update_orphan_mark(runner)
            else:
                # A step name from plan() with no matching branch here
                # is a wiring bug — must not pass green in the summary
                # (adversarial review: else was previously followed by
                # step_states.append(..., "ok"), so a rename typo would
                # ship as a successful step).
                raise RuntimeError(
                    f"unknown update step {step.name!r} — plan() emitted "
                    "a name the dispatcher does not handle"
                )
            step_states.append((step.name, "ok"))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            step_states.append(
                (step.name, f"FAIL: {type(exc).__name__}"),
            )
            click.secho(f"  step {step.name} failed: {exc}", fg="red", err=True)

    # End-of-run summary — one line + a per-step tally so an operator
    # reading `hklii update` output (or logs of a scheduled run) sees
    # the aggregate outcome without scanning intermediate step output.
    ok = len(step_states) - failures
    total = len(step_states)
    click.echo("")
    click.echo(
        f"Summary: {ok}/{total} step(s) ok"
        + (f", {failures} failed" if failures else "")
    )
    for name, state in step_states:
        marker = "✓" if state == "ok" else "✗"
        click.echo(f"  {marker} {name} — {state}")
    return failures


async def _run_update_scrape(runner, step, no_events: bool) -> None:
    """Delegate to the same helper as `hklii scrape`, threading through
    the narrow-window kwargs from the plan as a single EnumWindow value
    object so the four coupled fields can't drift apart at hop layers.

    Freshness-aware scoping (adversarial D2 finding #2): the freshness
    step runs FIRST in the plan (when include_freshness_check is on),
    populating db_freshness with wire counts + probe status + local
    counts. Before dispatching the scrape, consult db_freshness and
    drop any court whose EN AND TC are both FRESH — the ~28 probe
    cost was spent to answer exactly this question. If every court is
    fresh, skip the scrape entirely rather than burning enum + fetch
    on nothing.

    The freshness step's absence (include_freshness_check=False) is
    respected: without a check_freshness step ahead of us, db_freshness
    is stale/empty, and we default back to the original full
    ALL_COURTS × en/tc sweep. This preserves the pre-D2 behaviour for
    profiles that opt out.
    """
    kw = step.kwargs
    window = EnumWindow(
        min_date_text=kw.get("min_date"),
        max_date_text=kw.get("max_date"),
        sort=kw.get("sort"),
        items_per_page=kw.get("items_per_page") or 10_000,
    )
    court_list = list(ALL_COURTS)
    langs: tuple[str, ...] = ("en", "tc")
    if runner.settings.get("include_freshness_check"):
        court_list, langs = _filter_fresh_case_buckets(
            runner.output, court_list, langs,
        )
        if not court_list:
            click.echo(
                "  update scrape: every case bucket is FRESH — "
                "skipping (freshness-scoped)."
            )
            return
    await _run_scrape(ScrapeConfig(
        output=runner.output,
        fmt_set={"html", "json", "txt", "doc"} if kw.get("allow_doc") else {"html", "json", "txt"},
        proxies=runner.proxies,
        direct=runner.direct,
        court_list=court_list,
        limit=None,
        resume=False,
        with_summaries=kw.get("with_summaries", True),
        with_appeal_history=kw.get("with_appeal_history", True),
        langs=langs,
        retry_failed=False,
        enum_max_age=0,
        save_enum_responses=False,
        no_events=no_events,
        window=window,
    ))


def _run_update_generate_html(runner, step) -> None:
    """Call the generate-html helper synchronously with the plan's limit."""
    from .checkpoint import CheckpointDB
    from .html_generator import HtmlGenerator
    db_path = runner.output / ".checkpoint.db"
    if not db_path.exists():
        click.echo("  no checkpoint db — skipping generate-html")
        return
    db = CheckpointDB(str(db_path))
    try:
        gen = HtmlGenerator(
            db, runner.output,
            limit=step.kwargs.get("limit") or None,
            include_failed=False, dry_run=False,
        )
        result = gen.generate_all()
        click.echo(
            f"  generate_html: candidates={result.candidates} "
            f"generated={result.generated} failed={result.failed}"
        )
    finally:
        db.close()


async def _run_coverage_canary(runner, step, no_events: bool) -> None:
    """13-bucket `getmetacase` probe to detect silent drift, then AUTO-ESCALATE.

    Canaries EN buckets only (not EN × TC). Bilingual cases are collapsed
    to lang='en' by CheckpointDB.upsert_case's UPSERT rule, so a
    per-lang tc count from HKLII would exceed the local
    `WHERE lang='tc'` count by N_bilingual on every court that has any
    bilingual case — 3 permanent false-positive escalations per run on
    the current corpus. TC-only databases (hksct/tc, hkts/tc) lose
    canary coverage in exchange; full_reconcile catches them quarterly.

    For every bucket whose live vs local row-count divergence is
    ≥ threshold, run a targeted `_run_scrape` for THAT (court, lang)
    inline (no date window → full backfill of anything missing).
    Capped at max_escalations to bound wire cost on a catastrophic
    divergence day. Only-DB reads if nothing diverged.

    Failure honesty:
    - coverage_canary itself raises CoverageCanaryBlindError if every
      probe failed; the wrapper lets it propagate so the dispatch marks
      the step FAIL. Silent-continue would print 'all N within
      tolerance' on a blind sweep.
    - Escalation failures are counted; if any escalation raised, the
      wrapper raises RuntimeError so the dispatch marks the step FAIL
      per the module's non-zero-exit contract.

    Pool lifecycle:
    - `pool = None` before the try; the outer finally closes it
      unconditionally so a raise from `pool.preflight()` doesn't leak
      the 20 curl_cffi clients created in ProxyPool.__init__.
    """
    from .checkpoint import CheckpointDB
    from .proxy_pool import ProxyPool
    from .update import coverage_canary

    db_path = runner.output / ".checkpoint.db"
    if not db_path.exists():
        click.echo("  coverage_canary: no checkpoint db — skipping")
        return

    db = CheckpointDB(str(db_path))
    pool = None
    divergent: list[dict] = []
    try:
        if runner.direct:
            pool = ProxyPool(proxy_urls=[], direct=True)
        else:
            pool = ProxyPool(proxy_urls=runner.proxies)
            await pool.preflight()

        canary_langs = ["en"]  # See docstring — bilingual UPSERT rule.
        divergent = await coverage_canary(
            get=pool.get,
            checkpoint=db,
            courts=ALL_COURTS,
            langs=canary_langs,
            threshold=step.kwargs.get("threshold", 5),
            max_escalations=step.kwargs.get("max_escalations", 3),
        )

        total_buckets = len(ALL_COURTS) * len(canary_langs)
        if not divergent:
            click.echo(
                f"  coverage_canary: all {total_buckets} buckets within "
                f"tolerance (threshold={step.kwargs.get('threshold', 5)})"
            )
            return

        click.echo(
            f"  coverage_canary: {len(divergent)} divergent bucket(s), "
            "escalating each to a targeted scrape:"
        )
        for b in divergent:
            sign = "+" if b["diff"] > 0 else ""
            click.echo(
                f"    - {b['court']}/{b['lang']}: "
                f"live={b['live']} local={b['local']} "
                f"delta={sign}{b['diff']}"
            )
    finally:
        # Close pool AND db regardless of what raised — a preflight
        # failure previously leaked all 20 curl_cffi clients.
        # Nested try/finally so a raise from pool.close() (e.g.
        # curl_cffi refusing aclose after an aborted transfer) can't
        # skip db.close() — leaking the CheckpointDB fcntl lock would
        # cascade CheckpointLockError across every subsequent step.
        try:
            if pool is not None:
                await pool.close()
        finally:
            db.close()

    # Escalation phase — one targeted scrape per divergent bucket.
    # Each runs full-corpus for its single (court, lang) so backdated
    # rows that the 30-day narrow window missed get picked up.
    escalation_failures = 0
    for b in divergent:
        click.echo(
            f"  ↳ escalating {b['court']}/{b['lang']} → full scrape"
        )
        try:
            await _run_scrape(ScrapeConfig(
                output=runner.output,
                fmt_set={"html", "json", "txt", "doc"},
                proxies=runner.proxies,
                direct=runner.direct,
                court_list=[b["court"]],
                langs=(b["lang"],),
                with_summaries=True,
                with_appeal_history=True,
                enum_max_age=0,
                no_events=no_events,
            ))
        except Exception as exc:  # noqa: BLE001
            escalation_failures += 1
            click.secho(
                f"    escalation for {b['court']}/{b['lang']} failed: {exc}",
                fg="red", err=True,
            )

    if escalation_failures:
        # Contract per _dispatch_update_plan docstring: non-zero failures
        # translate to non-zero exit. Silent-swallow would report the
        # step as ok and mask the operator's need to rerun.
        raise RuntimeError(
            f"{escalation_failures} of {len(divergent)} coverage_canary "
            "escalation(s) failed — see stderr above"
        )


async def _run_update_check_freshness(
    runner, step, no_events: bool,
) -> None:
    """`check_freshness` step handler for the update dispatcher.

    Opens the CheckpointDB + ProxyPool, loads the /databases matrix
    fixture, runs :class:`FreshnessRunner.probe_all`, then computes
    stale + first-run buckets for a one-line summary. The pool + DB
    lifecycle mirrors ``_run_coverage_canary``'s nested-finally
    discipline — a raise from pool.close() must not skip db.close()
    or the checkpoint lock fd leaks and cascades to every subsequent
    step.

    Does NOT scope downstream scrape steps here — that's the
    dispatcher's job in a follow-up wiring pass. This handler
    populates db_freshness so the SUBSEQUENT scrape steps can read
    it. Kept small on purpose so the freshness data flow is one-file
    to read.
    """
    from .checkpoint import CheckpointDB
    from .discovery import load_default_matrix
    from .freshness import FreshnessRunner
    from .proxy_pool import ProxyPool

    db_path = runner.output / ".checkpoint.db"
    runner.output.mkdir(parents=True, exist_ok=True)
    db = CheckpointDB(str(db_path))
    pool = None
    try:
        if runner.direct:
            pool = ProxyPool(proxy_urls=[], direct=True)
        else:
            pool = ProxyPool(proxy_urls=runner.proxies)
            await pool.preflight()
        matrix = load_default_matrix()
        freshness = FreshnessRunner(
            get=pool.get, checkpoint=db, matrix=matrix,
            output_dir=runner.output,
        )
        outcomes = await freshness.probe_all()
        stale = freshness.stale_buckets()
        first_run = freshness.first_run_missing()
        healthy = sum(1 for o in outcomes if o.ok)
        click.echo(
            f"  check_freshness: probed={len(outcomes)} healthy={healthy} "
            f"stale={len(stale)} first_run={len(first_run)}"
        )
    finally:
        try:
            if pool is not None:
                await pool.close()
        finally:
            db.close()


@main.command("check-freshness")
@click.option(
    "-o", "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./output"),
    help="Directory holding the checkpoint DB.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
    cls=MutuallyExclusiveOption,
    help="Proxy URL(s). Repeatable for multiple proxies.",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Connect directly without a proxy.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit stale-bucket report as JSON on stdout.",
)
@click.option(
    "--text",
    "as_text",
    is_flag=True,
    default=False,
    help="Emit stale-bucket report as human-readable text on stdout.",
)
@click.option(
    "--report",
    "as_report",
    is_flag=True,
    default=False,
    help=(
        "Emit the full fill-in-blanks Markdown table (English + Chinese "
        "names, local/live counts + updated per lang) rather than the "
        "stale-buckets summary."
    ),
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation for --direct.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Skip structured event logging.",
)
def check_freshness(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    as_json: bool,
    as_text: bool,
    as_report: bool,
    yes: bool,
    no_events: bool,
) -> None:
    """Probe every mapped HKLII slug × lang bucket and report freshness.

    Runs the D2 freshness gate against the /databases fixture matrix,
    upserts wire columns into db_freshness, recomputes local_count for
    each bucket, and prints either:

    - the STALE-buckets summary (default),
    - a JSON payload (``--json``), or
    - the full fill-in-blanks Markdown table (``--report``).

    Exits 0 iff every bucket is FRESH — cron scripts can chain
    ``hklii check-freshness && ...`` to gate on a healthy corpus.
    ``--report`` always exits 0 — it's a rendering mode, not a gate.

    \b
    Examples:
      hklii check-freshness --proxy http://127.0.0.1:8888
      hklii check-freshness --direct --yes --json
      hklii check-freshness --proxy http://127.0.0.1:8888 --report
    """
    if not proxies and not direct:
        raise click.UsageError("Must specify --proxy or --direct.")
    modes = [as_json, as_text, as_report]
    if sum(1 for m in modes if m) > 1:
        raise click.UsageError(
            "--json, --text, and --report are mutually exclusive.",
        )
    if direct and not yes:
        click.confirm(
            "Probing without a proxy exposes your IP. Continue?",
            abort=True,
        )
    asyncio.run(_run_check_freshness(
        output=output, proxies=list(proxies), direct=direct,
        as_json=as_json, as_report=as_report, no_events=no_events,
    ))


async def _run_check_freshness(
    output: Path,
    proxies: list[str],
    direct: bool,
    as_json: bool,
    no_events: bool,
    as_report: bool = False,
) -> None:
    """Standalone check-freshness runner. Same pool/DB lifecycle as
    the update-step handler; separate so operators can invoke either
    surface without the other being loaded.

    Prints a one-line summary + a per-stale-bucket table in text mode,
    or a JSON object with schema
    ``{stale: [...], first_run: [...], probed: int, healthy: int}``
    in --json mode. Exits nonzero (via ``click.exceptions.Exit``) iff
    any bucket is stale so a shell chain can escalate.
    """
    import json as _json

    from .checkpoint import CheckpointDB
    from .discovery import load_default_matrix
    from .events import StructuredEventLogger
    from .freshness import FreshnessRunner, render_report_markdown
    from .proxy_pool import ProxyPool

    output.mkdir(parents=True, exist_ok=True)
    db_path = output / ".checkpoint.db"
    db = CheckpointDB(str(db_path))
    events = None if no_events else StructuredEventLogger(output)
    if events is not None:
        await events.start()
    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True, events=events)
    else:
        pool = ProxyPool(proxy_urls=proxies, events=events)
    try:
        if not direct:
            click.echo("Running preflight IP checks...", err=True)
            result = await pool.preflight()
            click.echo(f"Home IP: {result.home_ip}", err=True)
            click.echo(
                f"Healthy proxies: {len(result.healthy_proxies)}", err=True,
            )
            if not result.healthy_proxies:
                raise click.UsageError(
                    "No healthy proxies after preflight."
                )
        matrix = load_default_matrix()
        freshness = FreshnessRunner(
            get=pool.get, checkpoint=db, matrix=matrix,
            output_dir=output,
        )
        outcomes = await freshness.probe_all()
        stale = freshness.stale_buckets()
        first_run = freshness.first_run_missing()
        healthy = sum(1 for o in outcomes if o.ok)

        if as_report:
            # Full markdown table — no stale gate, always exit 0.
            rows = list(db.iter_freshness_rows())
            click.echo(render_report_markdown(rows=rows, matrix=matrix))
            return
        if as_json:
            payload = {
                "probed": len(outcomes),
                "healthy": healthy,
                "stale": [
                    {"kind": r.kind, "scope": r.scope, "lang": r.lang}
                    for r in stale
                ],
                "first_run": [
                    {"kind": r.kind, "scope": r.scope, "lang": r.lang}
                    for r in first_run
                ],
            }
            click.echo(_json.dumps(payload, sort_keys=True))
        else:
            # Human table — stderr is used for progress noise above so
            # the table on stdout can still be piped/redirected cleanly
            # by an operator who ran without --json.
            click.echo(
                f"check-freshness: probed={len(outcomes)} "
                f"healthy={healthy} stale={len(stale)} "
                f"first_run={len(first_run)}"
            )
            for r in stale:
                click.echo(f"  STALE   {r.kind}/{r.scope}/{r.lang}")
            for r in first_run:
                click.echo(f"  FIRST-RUN {r.kind}/{r.scope}/{r.lang}")

        # Nonzero exit iff any stale — cron chains can gate on this.
        # First-run buckets are ALSO stale for scrape-scoping (per
        # first_run_semantics rule 1), so include them in the exit test.
        if stale or first_run:
            raise click.exceptions.Exit(code=1)
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


def _reset_relatedcap_fetches_via_checkpoint(output: Path) -> None:
    """Thin CLI-side wrapper over CheckpointDB.reset_relatedcap_fetches().

    Kept as a helper so the dispatcher doesn't have to open+close the DB
    inline. All actual SQL lives on the CheckpointDB accessor.
    """
    from .checkpoint import CheckpointDB
    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        return
    db = CheckpointDB(str(db_path))
    try:
        db.reset_relatedcap_fetches()
    finally:
        db.close()


def _run_update_orphan_mark(runner) -> None:
    """Flip stale downloaded rows to status='orphaned'.

    Safety guard: a partial `full_reconcile` (say, VPN degrades after
    4/12 courts) leaves the unenumerated buckets with stale
    `last_seen_at`. Naive orphan_mark would flag every downloaded row
    in those buckets — silent corpus damage.

    Guard consumes the `enum_runs` generation marker:
    - `latest_completed_enum_run()` returns the newest full-corpus
      sweep whose `completed_at` is populated; narrow-window scrapes
      (daily/weekly/monthly) are filtered out because their cutoff
      would mass-orphan out-of-window rows.
    - `covered_courts`/`covered_langs` must include every entry in
      ALL_COURTS × ALL_LANGS; anything missing means the reference
      sweep didn't touch that bucket and orphan_mark aborts.
    - Cutoff = started_at - 60s (1s grace against the per-row upsert
      timestamps being slightly ahead of the row-level clock read).

    Files on disk are NEVER touched — status flips only.
    """
    from .checkpoint import CheckpointDB

    db_path = runner.output / ".checkpoint.db"
    if not db_path.exists():
        click.echo("  orphan_mark: no checkpoint db — skipping")
        return
    db = CheckpointDB(str(db_path))
    try:
        # Enum-generation marker: full_reconcile's BulkScraper.enumerate()
        # stamps `enum_runs.completed_at` when the whole (courts × langs)
        # sweep finishes cleanly. If the run raised mid-way, completed_at
        # stays NULL and this row is silently skipped by
        # latest_completed_enum_run() → orphan_mark aborts. Cleaner than
        # the timestamp-heuristic ('bucket last_seen_at within N hours')
        # that previously guarded this path.
        gen = db.latest_completed_enum_run()
        if gen is None:
            click.secho(
                "  orphan_mark: ABORTED — no completed enum run recorded. "
                "Rerun full_reconcile before orphan-marking.",
                fg="yellow", err=True,
            )
            return
        # The completed enum must cover every (court, lang) we intend to
        # orphan-mark. Anything the enum didn't touch could be a live
        # bucket whose rows would be spuriously flagged.
        covered_courts = set(gen["courts"])
        covered_langs = set(gen["langs"])
        missing_courts = [c for c in ALL_COURTS if c not in covered_courts]
        missing_langs = [l for l in ALL_LANGS if l not in covered_langs]
        if missing_courts or missing_langs:
            click.secho(
                "  orphan_mark: ABORTED — latest completed enum "
                f"(generation={gen['generation_id']}) did not cover: "
                f"courts={missing_courts} langs={missing_langs}. "
                "Rerun full_reconcile with the full court/lang set.",
                fg="yellow", err=True,
            )
            return

        # Cutoff = when the sweep started. Rows the sweep enumerated will
        # have last_seen_at >= started_at; anything older wasn't in the
        # sweep and is therefore not currently listed upstream. A small
        # grace protects against clock skew between the sweep's per-row
        # upsert_case timestamps and started_at.
        cutoff = int(gen["started_at"]) - 60
        n = db.mark_orphaned_below_ts(cutoff)
        if n == 0:
            click.echo(
                f"  orphan_mark: no orphans found "
                f"(generation={gen['generation_id']})"
            )
            return
        click.echo(
            f"  orphan_mark: flagged {n} row(s) as orphaned "
            f"(generation={gen['generation_id']}, files preserved)"
        )
    finally:
        db.close()


def _run_update_validate(runner, step) -> None:
    """Run the Validator inline; print a one-line summary. Never sys.exit.

    Defaults to sample=2000 (30s vs 4min per fork research) — the
    critical file-integrity checks (stem_coords, orphans, html_pending)
    always walk the full tree regardless of sample. Per-row checks
    (presence, magic, challenge_html, neutral_in_body, enrichment) are
    the only ones affected by sampling. Pass --validate-sample 0 to run
    full corpus.
    """
    from .checkpoint import CheckpointDB
    from .validate import Validator
    db_path = runner.output / ".checkpoint.db"
    if not db_path.exists():
        click.echo("  no checkpoint db — skipping validate")
        return
    db = CheckpointDB(str(db_path))
    try:
        raw_sample = step.kwargs.get("sample")
        # 0 → full corpus (None in Validator); N > 0 → sample size.
        sample = None if raw_sample in (None, 0) else int(raw_sample)
        validator = Validator(db, runner.output, sample=sample)
        report = validator.run()
        counts = report.counts["discrepancies_by_severity"]
        sample_note = "full corpus" if sample is None else f"sample={sample}"
        click.echo(
            f"  validate ({sample_note}): "
            f"fatal={counts.get('fatal', 0)} "
            f"warn={counts.get('warn', 0)} "
            f"info={counts.get('info', 0)}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
