"""Legislation scraper — stub. Real impl in the feat commit."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class LegisFetchError(RuntimeError):
    pass


@dataclass
class LegisEntry:
    num: str
    title: str


@dataclass
class LegisListing:
    total: int
    entries: list[LegisEntry]


@dataclass
class LegisDocument:
    abbr: str
    num: str
    lang: str
    latest_vid: int
    latest_version_date: str
    versions: list[dict]
    content: list[dict]


def getlegisfiles_url(cap_type, lang, page, items_per_page):
    raise NotImplementedError


def getcapversions_url(cap, lang):
    raise NotImplementedError


def getcapversiontoc_url(vid):
    raise NotImplementedError


def parse_files_response(body):
    raise NotImplementedError


def pick_latest_version(versions):
    raise NotImplementedError


def save_legis_local(output_dir, abbr, num, lang, versions, content):
    raise NotImplementedError


async def fetch_legis_document(get, abbr, num, lang):
    raise NotImplementedError
