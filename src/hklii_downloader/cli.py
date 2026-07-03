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
