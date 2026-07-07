"""Search index build helpers over the on-disk corpus + viewer.db.

Owns:
- BodySource dataclass: one entry per (case, language) on disk
- discover_body_sources: bilingual sibling probe
- (Phase 2.5+) extract_plaintext, body_sha256, upsert_case, rebuild_index

See docs/viewer-design.md §4.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from hklii_downloader.viewer.body_render.text import iter_text_nodes


#: Any run of whitespace (including newlines) collapses to a single space
#: in the extracted plaintext. Matches source-format churn that would
#: otherwise perturb body_sha256.
_WHITESPACE_RUN = re.compile(r"\s+")


def extract_plaintext(html_content: str | bytes) -> str:
    """Extract normalized plaintext for FTS indexing.

    Uses iter_text_nodes with DEFAULT_SKIP_TAGS (a/code/pre) plus the
    always-skip infrastructure set (script/style/head/…). Concatenates
    yielded text nodes with a single space, then collapses whitespace
    runs and strips the ends.

    Empty body → empty string. Callers may skip writing an FTS row for
    an empty body, or write a row whose body_sha256 is the empty-string
    sentinel — either is valid; the point is that the two states are
    distinguishable.
    """
    nodes = list(iter_text_nodes(html_content))
    if not nodes:
        return ""
    joined = " ".join(nodes)
    return _WHITESPACE_RUN.sub(" ", joined).strip()


def body_sha256(plaintext: str) -> str:
    """Return the SHA-256 hex digest of ``plaintext`` (utf-8 encoded).

    Basis of the incremental-index diff: a case's body_sha256 unchanged
    since the last index run means we can skip the reindex.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BodySource:
    """One indexable body for a (case, language) pair.

    Attributes:
      lang: 'en' or 'tc' — the language the body is written in
      path: absolute or relative Path to the file on disk
      source_kind: one of 'html', 'tc.html', 'generated.html' — the
        physical file variant. Downstream (Phase 3 render) uses this
        to pick the right dispatch branch (native HKLII shape vs
        pandoc fragment).
    """

    lang: str
    path: Path
    source_kind: str


def discover_body_sources(
    output_root: str | Path,
    case_key: str,
    case_lang: str,
) -> list[BodySource]:
    """Enumerate the on-disk body sources for a case.

    Rules (design §4 line 82, INDEX-time enumeration):
      - ``{stem}.tc.html`` is unambiguously TC (regardless of case.lang)
      - ``{stem}.html`` is EN when a .tc.html sibling exists (bilingual
        pair); otherwise it reflects case.lang
      - ``{stem}.generated.html`` fills the case.lang slot as a fallback
        for cases without a real .html body; it never overrides a real
        .html

    Returns one BodySource per language present on disk. Empty list if
    the case has no body files (L5: distinct from a raise — the case
    simply has nothing to index yet).

    Raises ValueError for a malformed case_key (< 2 slashes).
    """
    parts = case_key.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"case_key must be 'court/year/number', got: {case_key!r}"
        )
    court, year, num = parts[0], parts[1], parts[2]
    stem = f"{court}_{year}_{num}"
    d = Path(output_root) / court / year

    html_path = d / f"{stem}.html"
    tc_html_path = d / f"{stem}.tc.html"
    gen_html_path = d / f"{stem}.generated.html"

    sources: list[BodySource] = []

    # .tc.html: always TC
    if tc_html_path.exists():
        sources.append(
            BodySource(lang="tc", path=tc_html_path, source_kind="tc.html")
        )

    # .html: EN if bilingual, else case.lang
    if html_path.exists():
        html_lang = "en" if tc_html_path.exists() else case_lang
        sources.append(
            BodySource(lang=html_lang, path=html_path, source_kind="html")
        )

    # .generated.html: covers case.lang if that language has no source yet
    covered_langs = {s.lang for s in sources}
    if case_lang not in covered_langs and gen_html_path.exists():
        sources.append(
            BodySource(
                lang=case_lang,
                path=gen_html_path,
                source_kind="generated.html",
            )
        )

    return sources
