"""Tests for press summary URL extraction from judgment HTML."""
import pytest

from hklii_downloader.enumerator import extract_press_summary_url


class TestExtractPressSummaryUrl:
    def test_finds_english_press_summary(self):
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023ES.htm"
            onclick="window.open(this.href,'popupwindow');return false;">
            Press Summary (English)
           </a>'''
        url = extract_press_summary_url(html)
        assert url == "/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023ES.htm"

    def test_finds_chinese_press_summary_when_no_english(self):
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023CS.htm"
            onclick="window.open(this.href,'popupwindow');return false;">
            Press Summary (Chinese)
           </a>'''
        url = extract_press_summary_url(html)
        assert url == "/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023CS.htm"

    def test_prefers_english_over_chinese(self):
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023ES.htm"
            onclick="window.open(this.href,'popupwindow');return false;">
            Press Summary (English)
           </a>
           <a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023CS.htm"
            onclick="window.open(this.href,'popupwindow');return false;">
            Press Summary (Chinese)
           </a>'''
        url = extract_press_summary_url(html)
        assert url.endswith("ES.htm")

    def test_no_press_summary(self):
        html = "<p>This judgment has no press summary link.</p>"
        assert extract_press_summary_url(html) is None

    def test_empty_html(self):
        assert extract_press_summary_url("") is None

    def test_ignores_non_summary_links(self):
        html = '''<a href="/en/legis/ord/221/s83P">s.83P</a>
                  <a href="/en/legis/ord/221/">Criminal Procedure Ordinance</a>
                  <a class="para" id="p1" name="p1">1.</a>'''
        assert extract_press_summary_url(html) is None

    def test_handles_different_case_types(self):
        html = '''<a href="/doc/judg/html/vetted/other/en/2022/HCAL001234_2022_files/HCAL001234_2022ES.htm"
            onclick="window.open(this.href,'popupwindow');return false;">
            Press Summary (English)
           </a>'''
        url = extract_press_summary_url(html)
        assert url is not None
        assert "HCAL001234" in url


class TestExtractPressSummaryUrls:
    """New plural variant that returns both languages when present."""

    def _import(self):
        try:
            from hklii_downloader.enumerator import extract_press_summary_urls
            return extract_press_summary_urls
        except ImportError:
            return None

    def test_returns_dict_with_both_when_present(self):
        fn = self._import()
        assert fn is not None, "extract_press_summary_urls not implemented yet"
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023ES.htm">
            Press Summary (English)
           </a>
           <a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023_files/FACC000012_2023CS.htm">
            Press Summary (Chinese)
           </a>'''
        result = fn(html)
        assert result.get("English", "").endswith("ES.htm")
        assert result.get("Chinese", "").endswith("CS.htm")

    def test_returns_only_english_when_only_english_present(self):
        fn = self._import()
        assert fn is not None
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023ES.htm">
            Press Summary (English)</a>'''
        result = fn(html)
        assert set(result.keys()) == {"English"}

    def test_returns_only_chinese_when_only_chinese_present(self):
        fn = self._import()
        assert fn is not None
        html = '''<a href="/doc/judg/html/vetted/other/en/2023/FACC000012_2023CS.htm">
            Press Summary (Chinese)</a>'''
        result = fn(html)
        assert set(result.keys()) == {"Chinese"}

    def test_returns_empty_dict_when_none(self):
        fn = self._import()
        assert fn is not None
        assert fn("<p>no summaries here</p>") == {}
        assert fn("") == {}


class TestExtractPressSummaryUrlsRobust:
    """Regex-only extraction silently returns {} on plausible markup tweaks.
    BS4-based extractor should handle case, wrapping tags, single-quoted
    href, and lang name variants."""

    def _fn(self):
        from hklii_downloader.enumerator import extract_press_summary_urls
        return extract_press_summary_urls

    def test_case_insensitive_press_summary(self):
        html = '''<a href="/x/en.htm">press summary (English)</a>'''
        assert "English" in self._fn()(html)

    def test_wrapping_span_inside_anchor(self):
        html = '''<a href="/x/en.htm"><span>Press Summary (English)</span></a>'''
        result = self._fn()(html)
        assert result.get("English", "").endswith("en.htm")

    def test_single_quoted_href(self):
        html = "<a href='/x/en.htm'>Press Summary (English)</a>"
        assert self._fn()(html).get("English", "").endswith("en.htm")

    def test_extra_attrs_before_href(self):
        html = '''<a class="foo" title="prev>next" href="/x/en.htm" onclick="x()">Press Summary (English)</a>'''
        assert self._fn()(html).get("English", "").endswith("en.htm")

    def test_extracts_from_real_hklii_markup(self):
        """Actual anchor from CFA case 25 with newlines and extra whitespace."""
        html = '''<a href="/doc/judg/html/vetted/other/en/2025/FACC000003_2025_files/FACC000003_2025ES.htm" onclick="window.open(this.href,'popupwindow');return false;">
        Press Summary (English)
       </a>'''
        result = self._fn()(html)
        assert result.get("English", "").endswith("ES.htm")
