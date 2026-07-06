from __future__ import annotations

import asyncio
from pathlib import Path

import click
import httpx

from .client import fetch_judgment, make_async_client, save_judgment
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

    asyncio.run(_run_scrape(
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
    ))


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

        try:
            validator = Validator(
                db, output,
                checks=check_list, sample=sample, seed=seed,
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


async def _run_scrape(
    output: Path,
    fmt_set: set[str],
    proxies: list[str],
    direct: bool,
    court_list: list[str],
    limit: int | None,
    resume: bool,
    with_summaries: bool = False,
    with_appeal_history: bool = False,
    langs: tuple[str, ...] = ("en", "tc"),
    retry_failed: bool = False,
    enum_max_age: int = 0,
    save_enum_responses: bool = False,
    no_events: bool = False,
) -> None:
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
    finally:
        if events is not None:
            await events.aclose()
        await pool.close()
        db.close()


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
        no_events=no_events,
    ))


async def _run_recheck_html(
    output: Path,
    proxies: list[str],
    direct: bool,
    limit: int | None,
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

    pending_count = len(db.pending_html_recheck(limit=None))
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
    type=click.Choice(["en", "tc", "both"]),
    default="both",
    help="Language(s) to enumerate. Default: both.",
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
def scrape_legis(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    abbr_str: str | None,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
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
    langs = LEGIS_LANGS if lang == "both" else (lang,)

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
            f"pending={stats['pending']}, ok={stats['ok']}, "
            f"error={stats['error']}. target this pass: {target}."
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
def scrape_hopt(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    abbr_str: str | None,
    lang: str,
    limit: int | None,
    yes: bool,
    no_events: bool,
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


if __name__ == "__main__":
    main()
