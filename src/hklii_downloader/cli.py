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
        if "proxy" in opts and "direct" in opts:
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
    default=Path("./downloads"),
    help="Directory containing existing downloads + .checkpoint.db.",
)
@click.option(
    "-p", "--proxy", "proxies",
    multiple=True,
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
def enrich(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    summaries: bool,
    appeal_history: bool,
    limit: int | None,
    yes: bool,
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
    ))


async def _run_enrich(
    output: Path,
    proxies: list[str],
    direct: bool,
    do_summaries: bool,
    do_appeal_history: bool,
    limit: int | None,
) -> None:
    from .checkpoint import CheckpointDB
    from .enrichment import EnrichmentRunner
    from .proxy_pool import ProxyPool

    db_path = output / ".checkpoint.db"
    if not db_path.exists():
        raise click.UsageError(
            f"No checkpoint DB at {db_path}. Run `hklii scrape` first."
        )
    db = CheckpointDB(str(db_path))

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies)

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
        )

        pending_kinds = []
        if do_summaries:
            pending_kinds += ["summary_en", "summary_zh"]
        if do_appeal_history:
            pending_kinds.append("appeal_history")
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
) -> None:
    from .logging_setup import setup_logging
    log_path = setup_logging(output, "scrape")
    click.echo(f"Logging to {log_path}")
    from .checkpoint import CheckpointDB
    from .proxy_pool import ProxyPool
    from .scraper import BulkScraper

    db_path = output / ".checkpoint.db"
    output.mkdir(parents=True, exist_ok=True)
    db = CheckpointDB(str(db_path))

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies)

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
def recheck_html(
    output: Path,
    proxies: tuple[str, ...],
    direct: bool,
    limit: int | None,
    yes: bool,
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
    ))


async def _run_recheck_html(
    output: Path,
    proxies: list[str],
    direct: bool,
    limit: int | None,
) -> None:
    from .checkpoint import CheckpointDB
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

    if direct:
        pool = ProxyPool(proxy_urls=[], direct=True)
        workers = 1
    else:
        pool = ProxyPool(proxy_urls=proxies)

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
        )
        counts = await runner.recheck_all()
        click.echo(
            f"\nDone. Newly captured: {counts['newly_captured']}, "
            f"still pending: {counts['still_pending']}, "
            f"failed: {counts['failed']}."
        )
    finally:
        await pool.close()
        db.close()


if __name__ == "__main__":
    main()
