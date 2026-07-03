"""Tests for press summary URL extraction from judgment HTML."""
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
