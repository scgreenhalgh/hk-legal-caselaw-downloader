"""Fetch + save press summaries and appeal history for downloaded judgments.

Press summary URLs come out of the judgment HTML as relative paths on
hklii.hk (e.g. `/doc/judg/html/vetted/other/en/2025/.../ES.htm`). Appeal
history is at `/api/getappealhistory?caseno={caseno}` and returns a JSON
array of related judgments across the appeal chain.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

_BASE_URL = "https://www.hklii.hk"
_VALID_LANGS = ("en", "zh")


async def fetch_press_summary(url_or_path: str, get: Callable) -> str:
    if not url_or_path.startswith("http"):
        url_or_path = _BASE_URL + url_or_path
    resp = await get(url_or_path)
    resp.raise_for_status()
    return resp.text


async def fetch_appeal_history(caseno: str, get: Callable) -> list[dict]:
    url = f"{_BASE_URL}/api/getappealhistory?caseno={quote(caseno, safe='')}"
    resp = await get(url)
    resp.raise_for_status()
    return resp.json()


def save_press_summary_local(
    html: str, output_dir: Path, stem: str, lang: str,
) -> Path:
    if lang not in _VALID_LANGS:
        raise ValueError(f"unknown lang {lang!r}; expected one of {_VALID_LANGS}")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stem}.summary_{lang}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_appeal_history_local(
    data: list[dict], output_dir: Path, stem: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stem}.appeal_history.json"
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


async def enrich_summaries_for_case(
    get: Callable, checkpoint,
    court: str, year: int, number: int,
    stem: str, output_dir: Path, content_html: str,
) -> None:
    from .enumerator import extract_press_summary_urls
    urls = extract_press_summary_urls(content_html)
    for lang_label, lang_short in (("English", "en"), ("Chinese", "zh")):
        kind = f"summary_{lang_short}"
        url = urls.get(lang_label)
        if url is None:
            checkpoint.mark_enrichment(court, year, number, kind, "na")
            continue
        try:
            html = await fetch_press_summary(url, get)
            save_press_summary_local(html, output_dir, stem, lang_short)
            checkpoint.mark_enrichment(court, year, number, kind, "downloaded")
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            checkpoint.mark_enrichment(
                court, year, number, kind, "failed",
                error=f"{type(e).__name__}: {e}",
            )


async def enrich_appeal_history_for_case(
    get: Callable, checkpoint,
    court: str, year: int, number: int,
    stem: str, output_dir: Path, case_number: str,
) -> None:
    try:
        data = await fetch_appeal_history(case_number, get)
        save_appeal_history_local(data, output_dir, stem)
        checkpoint.mark_enrichment(
            court, year, number, "appeal_history", "downloaded",
        )
    except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e:
        checkpoint.mark_enrichment(
            court, year, number, "appeal_history", "failed",
            error=f"{type(e).__name__}: {e}",
        )
