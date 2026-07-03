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
