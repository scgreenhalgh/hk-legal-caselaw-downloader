from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from .parser import HKLIICase, html_to_text

# The judiciary.hk F5 WAF blocks any UA containing "python" (silent connection hang).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en-GB;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}


def make_async_client(timeout: int = 30, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        proxy=proxy,
        trust_env=False,
    )


@dataclass
class Judgment:
    case: HKLIICase
    title: str
    case_number: str
    court_name: str
    date: str
    neutral_citation: str
    parallel_citations: list[str]
    content_html: str
    doc_url: str | None
    has_translation: bool

    @property
    def content_text(self) -> str:
        return html_to_text(self.content_html)


def parse_judgment_response(case: HKLIICase, data: dict) -> Judgment:
    cases_list = data.get("cases", [])
    first_case = cases_list[0] if cases_list else {}

    return Judgment(
        case=case,
        title=first_case.get("title", ""),
        case_number=first_case.get("act", ""),
        court_name=data.get("db", ""),
        date=data.get("date", ""),
        neutral_citation=data.get("neutral", ""),
        parallel_citations=data.get("parallel_citation", []),
        content_html=data.get("content", ""),
        doc_url=data.get("doc") or None,
        has_translation=data.get("has_translation", False),
    )


def save_judgment_local(
    judgment: Judgment, output_dir: Path, formats: set[str],
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = judgment.case.filename_stem
    saved: list[Path] = []

    if "html" in formats:
        path = output_dir / f"{stem}.html"
        path.write_text(judgment.content_html, encoding="utf-8")
        saved.append(path)

    if "txt" in formats:
        path = output_dir / f"{stem}.txt"
        path.write_text(judgment.content_text, encoding="utf-8")
        saved.append(path)

    if "json" in formats:
        path = output_dir / f"{stem}.json"
        meta = {
            "title": judgment.title,
            "case_number": judgment.case_number,
            "court": judgment.court_name,
            "date": judgment.date,
            "neutral_citation": judgment.neutral_citation,
            "parallel_citations": judgment.parallel_citations,
            "doc_url": judgment.doc_url,
            "has_translation": judgment.has_translation,
            "url": f"https://www.hklii.hk/{judgment.case.lang}/cases/{judgment.case.court}/{judgment.case.year}/{judgment.case.number}",
        }
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        saved.append(path)

    return saved


async def fetch_judgment(case: HKLIICase, client: httpx.AsyncClient) -> Judgment:
    resp = await client.get(case.api_url)
    resp.raise_for_status()
    return parse_judgment_response(case, resp.json())


async def save_judgment(
    judgment: Judgment,
    output_dir: Path,
    formats: set[str],
    client: httpx.AsyncClient,
) -> list[Path]:
    saved = save_judgment_local(judgment, output_dir, formats)

    if "doc" in formats and judgment.doc_url:
        resp = await client.get(judgment.doc_url)
        resp.raise_for_status()
        path = output_dir / f"{judgment.case.filename_stem}.doc"
        path.write_bytes(resp.content)
        saved.append(path)

    return saved
