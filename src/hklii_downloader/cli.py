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

    asyncio.run(_run_scrape(
        output=output,
        fmt_set=fmt_set,
        proxies=list(proxies),
        direct=direct,
        court_list=court_list,
        limit=limit,
        resume=resume,
    ))


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
) -> None:
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
        )

        click.echo(f"Enumerating courts: {', '.join(court_list)}")
        total = await scraper.enumerate(court_list)
        click.echo(f"Found {total} cases.")

        stats = db.stats()
        click.echo(f"Pending: {stats['pending']}, Downloaded: {stats['downloaded']}, Failed: {stats['failed']}")

        if stats["pending"] == 0:
            click.echo("Nothing to download.")
        else:
            target = limit if limit is not None else stats["pending"]
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


if __name__ == "__main__":
    main()
