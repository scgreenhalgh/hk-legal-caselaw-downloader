"""Tests for doc_convert — pandoc-based doc → html conversion.

Uses subprocess mocks for unit tests plus one integration test that
shells out to real pandoc. All doc-family formats supported by pandoc
(docx, rtf) go direct. OLE .doc bytes go via a libreoffice --headless
trampoline; the trampoline is only exercised when soffice is on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _write_bytes(path: Path, head: bytes, size: int = 32) -> Path:
    """Write a file whose first `len(head)` bytes match `head`, padded
    with zeros to reach `size`. Used to fake different doc-family magics
    without needing valid document bodies for mock-based tests."""
    body = head + b"\x00" * max(0, size - len(head))
    path.write_bytes(body)
    return path


class TestConvertViaPandoc:
    def test_docx_dispatches_to_pandoc_docx(self, tmp_path):
        from hklii_downloader.doc_convert import convert_to_html

        path = _write_bytes(tmp_path / "s.docx", b"PK\x03\x04")

        with patch(
            "hklii_downloader.doc_convert.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="<p>hello</p>", stderr="",
            )
            html = convert_to_html(path)

        run.assert_called_once()
        call_args = run.call_args.args[0]
        assert "pandoc" in call_args[0]
        assert "-f" in call_args
        assert "docx" in call_args
        assert str(path) in call_args
        assert html == "<p>hello</p>"

    def test_rtf_dispatches_to_pandoc_rtf(self, tmp_path):
        from hklii_downloader.doc_convert import convert_to_html

        path = _write_bytes(tmp_path / "s.rtf", b"{\\rt")

        with patch(
            "hklii_downloader.doc_convert.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="<p>rtf</p>", stderr="",
            )
            html = convert_to_html(path)

        assert "rtf" in run.call_args.args[0]
        assert html == "<p>rtf</p>"

    def test_pandoc_failure_raises_conversion_error(self, tmp_path):
        from hklii_downloader.doc_convert import (
            convert_to_html, ConversionError,
        )

        path = _write_bytes(tmp_path / "s.docx", b"PK\x03\x04")

        with patch(
            "hklii_downloader.doc_convert.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="pandoc: whoops",
            )
            with pytest.raises(ConversionError) as excinfo:
                convert_to_html(path)
            assert "pandoc" in str(excinfo.value).lower()

    def test_unknown_magic_raises_unsupported(self, tmp_path):
        from hklii_downloader.doc_convert import (
            convert_to_html, UnsupportedSourceError,
        )

        path = _write_bytes(tmp_path / "s.bin", b"\xde\xad\xbe\xef")

        with pytest.raises(UnsupportedSourceError):
            convert_to_html(path)


class TestConvertOleDocViaLibreOffice:
    def test_ole_doc_shells_out_when_soffice_present(self, tmp_path):
        """When soffice is on PATH, .doc (OLE) bytes trampoline through
        `soffice --headless --convert-to docx` and pandoc reads the docx."""
        from hklii_downloader import doc_convert

        path = _write_bytes(tmp_path / "s.doc", b"\xd0\xcf\x11\xe0", size=64)

        # First subprocess.run: soffice trampoline (creates s.docx in
        # the tempdir arg).  Second: pandoc.
        def run_side_effect(argv, *args, **kwargs):
            if "soffice" in argv[0] or "libreoffice" in argv[0]:
                # Simulate soffice creating {stem}.docx in outdir.
                outdir_idx = argv.index("--outdir") + 1
                outdir = Path(argv[outdir_idx])
                (outdir / f"{path.stem}.docx").write_bytes(b"PK\x03\x04data")
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr="",
                )
            # pandoc
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="<p>from doc</p>", stderr="",
            )

        with patch.object(
            doc_convert, "_find_soffice", return_value="/usr/bin/soffice"
        ):
            with patch.object(
                doc_convert.subprocess, "run", side_effect=run_side_effect,
            ):
                html = doc_convert.convert_to_html(path)

        assert html == "<p>from doc</p>"

    def test_ole_doc_without_soffice_raises_unsupported(self, tmp_path):
        from hklii_downloader import doc_convert
        from hklii_downloader.doc_convert import UnsupportedSourceError

        path = _write_bytes(tmp_path / "s.doc", b"\xd0\xcf\x11\xe0", size=64)

        with patch.object(doc_convert, "_find_soffice", return_value=None):
            with pytest.raises(UnsupportedSourceError) as excinfo:
                doc_convert.convert_to_html(path)
        msg = str(excinfo.value)
        assert "libreoffice" in msg.lower() or "soffice" in msg.lower()


class TestConvertRealPandoc:
    """Integration: shell out to real pandoc on a genuine .docx. Skipped
    if pandoc isn't installed so the suite still runs on minimal boxes."""

    def test_real_docx_produces_nonempty_html(self, tmp_path):
        pandoc = shutil.which("pandoc")
        if pandoc is None:
            pytest.skip("pandoc not installed")

        from hklii_downloader.doc_convert import convert_to_html

        # Build a minimal valid .docx from scratch — an OOXML zip with
        # the two files any docx reader requires.
        import zipfile
        doc_path = tmp_path / "s.docx"
        with zipfile.ZipFile(doc_path, "w") as z:
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>',
            )
            z.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                '</Relationships>',
            )
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>'
                '<w:p><w:r><w:t>HELLO PANDOC WORLD</w:t></w:r></w:p>'
                '</w:body>'
                '</w:document>',
            )

        html = convert_to_html(doc_path)
        assert "HELLO PANDOC WORLD" in html
