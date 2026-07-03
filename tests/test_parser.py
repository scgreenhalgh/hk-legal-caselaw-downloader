import pytest

from hklii_downloader.parser import parse_hklii_url, HKLIICase, html_to_text


class TestParseHKLIIUrl:
    def test_standard_url(self):
        result = parse_hklii_url("https://www.hklii.hk/en/cases/hkcfa/2023/32")
        assert result == HKLIICase(lang="en", court="hkcfa", year=2023, number=32)

    def test_chinese_url(self):
        result = parse_hklii_url("https://www.hklii.hk/tc/cases/hkcfa/2023/32")
        assert result == HKLIICase(lang="tc", court="hkcfa", year=2023, number=32)

    def test_different_court(self):
        result = parse_hklii_url("https://www.hklii.hk/en/cases/hkca/2022/1909")
        assert result == HKLIICase(lang="en", court="hkca", year=2022, number=1909)

    def test_trailing_slash(self):
        result = parse_hklii_url("https://www.hklii.hk/en/cases/hkcfi/2021/3350/")
        assert result == HKLIICase(lang="en", court="hkcfi", year=2021, number=3350)

    def test_http_url(self):
        result = parse_hklii_url("http://www.hklii.hk/en/cases/hkcfa/2023/32")
        assert result == HKLIICase(lang="en", court="hkcfa", year=2023, number=32)

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Not a valid HKLII case URL"):
            parse_hklii_url("https://example.com/foo")

    def test_missing_parts_raises(self):
        with pytest.raises(ValueError, match="Not a valid HKLII case URL"):
            parse_hklii_url("https://www.hklii.hk/en/cases/hkcfa")

    def test_api_url(self):
        case = HKLIICase(lang="en", court="hkcfa", year=2023, number=32)
        assert case.api_url == "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfa&year=2023&num=32"

    def test_filename_stem(self):
        case = HKLIICase(lang="en", court="hkcfa", year=2023, number=32)
        assert case.filename_stem == "hkcfa_2023_32"


class TestHtmlToText:
    def test_strips_tags(self):
        html = "<p>Hello <b>world</b></p>"
        assert "Hello world" in html_to_text(html)

    def test_preserves_paragraphs(self):
        html = "<p>First paragraph.</p><p>Second paragraph.</p>"
        text = html_to_text(html)
        assert "First paragraph." in text
        assert "Second paragraph." in text

    def test_full_html_document(self):
        html = """<html><head><title>Test</title></head>
        <body><p>Content here.</p></body></html>"""
        text = html_to_text(html)
        assert "Content here." in text
        assert "<html>" not in text
