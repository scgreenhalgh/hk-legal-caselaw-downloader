"""On-disk contract for press-summary HTML sidecars.

The downloader's enrichment step writes press summaries next to the
judgment body via ``enrichment.save_press_summary_local``. HKLII ships
summaries in up to two languages (English + Chinese), so the shipped
filename convention is bilingual:

    output/{court}/{year}/{court}_{year}_{n}.summary_{lang}.html

where ``lang`` in {"en", "zh"}. A future viewer phase will render these
on the case-detail page; this contract locks the on-disk shape so any
future reader — viewer, RAG, audit script — can rely on it.

Five-lens angles pinned by this file:

L1 (silent skip): the shipped writer raises ``ValueError`` on invalid
    lang (asserted in ``tests/test_enrichment.py``). The reader defined
    below returns ``None`` on missing files — the sentinel is explicit,
    not a swallowed exception.
L2 (semantic drift): a lookup for the press-summary sidecar must not
    resolve to sibling files at the same path — the primary body
    (``{stem}.html``), the TC translation (``{stem}.tc.html``), the
    LibreOffice fallback (``{stem}.generated.html``), or the
    other-language summary. The full "all five siblings present" test
    covers the drift.
L3 (docstring drift): the reader's docstring claim about 0-byte files
    is asserted by a dedicated test, not a comment.
L4 (wrong-side test): both the writer (``save_press_summary_local``) and
    the reader (``read_press_summary`` below) are exercised. A
    helper-only test would drift the moment the writer's suffix changed.
L5 (ambiguous state): missing file and 0-byte file are distinct disk
    states. Both surface as ``None`` from the reader, but the reader's
    behaviour on each is asserted separately — the caller can still
    distinguish via ``Path.exists()`` if it later needs to.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helper — mimics the future viewer's read pattern.
#
# There is no shipped reader for press-summary HTML sidecars yet: the
# enrichment runner only cares about the *checkpoint* status (downloaded /
# na / failed), never re-reads the file. This helper defines the contract
# the future viewer will honour.
# ---------------------------------------------------------------------------


def read_press_summary(stem_dir: Path, stem: str, lang: str) -> str | None:
    """Read a press-summary HTML sidecar off disk.

    Returns:
        The file's text content, or ``None`` if the file is missing OR
        is 0 bytes on disk. A 0-byte file is a legitimate "no summary"
        signal — an early enrichment run may have touched the path without
        populating it. Callers who need to distinguish "not tried" from
        "tried and empty" can still check ``Path.exists()`` themselves.

    Never falls back to sibling names — the caller must supply a lang.
    """
    path = stem_dir / f"{stem}.summary_{lang}.html"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return text if text else None


# ---------------------------------------------------------------------------
# Contract #1 — write→read round-trip via the shipped downloader writer.
# ---------------------------------------------------------------------------


class TestRoundTripThroughShippedWriter:
    def test_english_summary_round_trips(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local

        body = "<html><body><p>Press summary body</p></body></html>"
        path = save_press_summary_local(body, tmp_path, "hkcfa_2026_25", "en")

        # Filename matches the shipped convention exactly.
        assert path.name == "hkcfa_2026_25.summary_en.html"

        # Reader recovers byte-identical content.
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") == body

    def test_chinese_summary_round_trips_utf8(self, tmp_path):
        from hklii_downloader.enrichment import save_press_summary_local

        body = "<html><body><p>新聞摘要</p></body></html>"
        save_press_summary_local(body, tmp_path, "hkcfa_2026_25", "zh")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") == body

    def test_html_fragment_partial_doc_round_trips(self, tmp_path):
        """Design §5 admits partial fragments (bare `<p>...`), not just
        full ``<html>`` docs. The reader must not care about doc shape."""
        from hklii_downloader.enrichment import save_press_summary_local

        fragment = "<p>Standalone paragraph — no &lt;html&gt; wrapper.</p>"
        save_press_summary_local(fragment, tmp_path, "hkcfa_2026_25", "en")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") == fragment

    def test_writer_and_reader_agree_on_suffix(self, tmp_path):
        """L4 wrong-side pin: the writer's on-disk path and the reader's
        lookup path share the ``{stem}.summary_{lang}.html`` template.
        If either side drifts (writer changes suffix, reader looks in
        different spot), the round-trip is byte-different."""
        from hklii_downloader.enrichment import save_press_summary_local

        marker = "<p>writer-reader-agreement-marker</p>"
        written = save_press_summary_local(
            marker, tmp_path, "hkcfa_2026_25", "en",
        )
        assert written.name == "hkcfa_2026_25.summary_en.html"

        # The reader looks up the same suffix template — reading back the
        # marker proves both sides agree on the on-disk path.
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") == marker


# ---------------------------------------------------------------------------
# Contract #2 — filename convention is strict; siblings never resolve.
# ---------------------------------------------------------------------------


class TestFilenameConventionIsStrict:
    """A (court, year) directory in the shipped corpus can hold up to
    five HTML files per case:

        - ``{stem}.html``            primary body
        - ``{stem}.tc.html``         TC translation sidecar
        - ``{stem}.generated.html``  LibreOffice fallback for empty rows
        - ``{stem}.summary_en.html`` press summary (English)
        - ``{stem}.summary_zh.html`` press summary (Chinese)

    The press-summary reader must resolve only its own suffix.
    """

    def test_skips_primary_html_body(self, tmp_path):
        # Only the primary body exists; no summary sidecar.
        (tmp_path / "hkcfa_2026_25.html").write_text("<html>body</html>")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") is None
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") is None

    def test_skips_tc_translation_sibling(self, tmp_path):
        # TC translation sidecar exists — but no press summary.
        (tmp_path / "hkcfa_2026_25.tc.html").write_text("<html>tc body</html>")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") is None
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") is None

    def test_skips_generated_html_sibling(self, tmp_path):
        # LibreOffice-generated fallback exists — not a press summary.
        (tmp_path / "hkcfa_2026_25.generated.html").write_text("<p>x</p>")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") is None
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") is None

    def test_english_lookup_does_not_return_chinese_sibling(self, tmp_path):
        # A ZH summary exists; asking for EN must NOT fall back.
        (tmp_path / "hkcfa_2026_25.summary_zh.html").write_text("<p>zh</p>")

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") is None
        assert (
            read_press_summary(tmp_path, "hkcfa_2026_25", "zh") == "<p>zh</p>"
        )

    def test_chinese_lookup_does_not_return_english_sibling(self, tmp_path):
        # Mirror of the above — EN present, asking for ZH must not fall back.
        (tmp_path / "hkcfa_2026_25.summary_en.html").write_text("<p>en</p>")

        assert (
            read_press_summary(tmp_path, "hkcfa_2026_25", "en") == "<p>en</p>"
        )
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") is None

    def test_all_five_siblings_present_resolves_only_own_summary(self, tmp_path):
        # Full corpus row: primary + TC + generated + both summaries.
        # Distinct bodies so a wrong-file bug returns wrong content
        # rather than silently matching a lookalike.
        (tmp_path / "hkcfa_2026_25.html").write_text("<html>body</html>")
        (tmp_path / "hkcfa_2026_25.tc.html").write_text("<html>tc</html>")
        (tmp_path / "hkcfa_2026_25.generated.html").write_text("<p>gen</p>")
        (tmp_path / "hkcfa_2026_25.summary_en.html").write_text(
            "<p>en-summary</p>",
        )
        (tmp_path / "hkcfa_2026_25.summary_zh.html").write_text(
            "<p>zh-summary</p>",
        )

        assert read_press_summary(tmp_path, "hkcfa_2026_25", "en") == (
            "<p>en-summary</p>"
        )
        assert read_press_summary(tmp_path, "hkcfa_2026_25", "zh") == (
            "<p>zh-summary</p>"
        )

    def test_prefix_collision_across_cases_does_not_leak(self, tmp_path):
        """L2 semantic drift: two cases where one stem is a prefix of
        the other (``hkcfa_2026_2`` vs ``hkcfa_2026_25``). Looking up
        the shorter stem must not pick up the longer one's sidecar."""
        (tmp_path / "hkcfa_2026_25.summary_en.html").write_text(
            "<p>case 25</p>",
        )
        # Case 2 has no summary on disk.
        assert read_press_summary(tmp_path, "hkcfa_2026_2", "en") is None


# ---------------------------------------------------------------------------
# Contract #3 — 0-byte file is a legitimate "no summary" signal.
# ---------------------------------------------------------------------------


class TestEmptyFileIsNoSummarySignal:
    """An early enrichment run may have touched the path without populating
    it. The reader must handle this without raising."""

    def test_zero_byte_file_returns_none_or_empty(self, tmp_path):
        path = tmp_path / "hkcfa_2026_25.summary_en.html"
        path.touch()
        assert path.stat().st_size == 0

        # The contract admits either None or "" as the no-summary sentinel;
        # this helper picks None but downstream readers may pick "".
        result = read_press_summary(tmp_path, "hkcfa_2026_25", "en")
        assert result in (None, ""), (
            f"empty file must return None or empty string, got {result!r}"
        )

    def test_zero_byte_file_does_not_raise(self, tmp_path):
        path = tmp_path / "hkcfa_2026_25.summary_zh.html"
        path.touch()

        try:
            read_press_summary(tmp_path, "hkcfa_2026_25", "zh")
        except (OSError, ValueError) as exc:
            pytest.fail(f"reader raised on 0-byte file: {type(exc).__name__}: {exc}")

    def test_missing_and_zero_byte_both_none_but_disk_distinguishable(
        self, tmp_path,
    ):
        """L5 ambiguous-state pin: the reader collapses "missing" and
        "0-byte" to the same sentinel, but the disk itself still tells
        them apart. Downstream code (later viewer phase, audit script)
        can ``Path.exists()`` to recover the distinction."""
        missing = tmp_path / "case_a.summary_en.html"
        empty = tmp_path / "case_b.summary_en.html"
        empty.touch()

        assert read_press_summary(tmp_path, "case_a", "en") is None
        assert read_press_summary(tmp_path, "case_b", "en") in (None, "")

        # Disk still distinguishes the states.
        assert not missing.exists()
        assert empty.exists()
        assert empty.stat().st_size == 0
