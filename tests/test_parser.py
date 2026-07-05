import pytest

from hklii_downloader.parser import parse_hklii_url, HKLIICase, html_to_text, referer_for


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


class TestRefererFor:
    """referer_for(url) derives a plausible SPA Referer for HKLII API/doc URLs.

    Motivation: hardcoding Referer to the homepage on every request is a
    one-line log-analysis signal any HKLII operator could catch. A real
    browser's Referer is set to the URL that fired the request.
    """

    def test_getjudgment_url_returns_year_list_page(self):
        url = "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234"
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfi/2024/"

    def test_getjudgment_url_tc_lang(self):
        url = "https://www.hklii.hk/api/getjudgment?lang=tc&abbr=hkca&year=2022&num=99"
        assert referer_for(url) == "https://www.hklii.hk/tc/cases/hkca/2022/"

    def test_getcasefiles_url_returns_court_list_page(self):
        url = "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=1000&page=1"
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfi/"

    def test_getcasefiles_url_tc_lang(self):
        url = "https://www.hklii.hk/api/getcasefiles?caseDb=hkdc&lang=tc&itemsPerPage=10000&page=2"
        assert referer_for(url) == "https://www.hklii.hk/tc/cases/hkdc/"

    def test_case_page_url_returns_year_list_page(self):
        # If we're fetching /en/cases/hkcfi/2024/1234 (the SPA page URL), the
        # Referer would be the year listing.
        url = "https://www.hklii.hk/en/cases/hkcfi/2024/1234"
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfi/2024/"

    def test_unknown_hklii_path_returns_homepage(self):
        assert referer_for("https://www.hklii.hk/some/other/path") == "https://www.hklii.hk/"

    def test_non_hklii_url_returns_homepage(self):
        assert referer_for("https://example.com/foo") == "https://www.hklii.hk/"

    def test_api_url_missing_query_params_returns_homepage(self):
        # Malformed API URL with no useful query params — fall back safely.
        assert referer_for("https://www.hklii.hk/api/getjudgment") == "https://www.hklii.hk/"
        assert referer_for("https://www.hklii.hk/api/getcasefiles") == "https://www.hklii.hk/"

    def test_getappealhistory_url_returns_case_page_referer(self):
        # A real SPA user viewing appeal history is on /{lang}/cases/{court}/{year}/{num}/,
        # so the Referer for the XHR should be a case-page URL, not the homepage.
        # Mirrors the getjudgment branch: falls back to the year-listing page.
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=HKCFA%205%2F2024"
        )
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfa/2024/"

    def test_getappealhistory_url_tc_lang(self):
        # If the URL carries an explicit lang= query param, honor it.
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=HKCA%2099%2F2022&lang=tc"
        )
        assert referer_for(url) == "https://www.hklii.hk/tc/cases/hkca/2022/"

    def test_getappealhistory_url_missing_caseno_returns_homepage(self):
        # No caseno at all — fall back to homepage rather than raising.
        assert (
            referer_for("https://www.hklii.hk/api/getappealhistory")
            == "https://www.hklii.hk/"
        )

    def test_getappealhistory_compact_facc_resolves_to_hkcfa(self):
        # Task #62: real production caseno shape is compact
        # (no space between court prefix and number). Historic behaviour
        # fell to homepage. Post-fix: COURT_PREFIX_MAP lets us derive
        # the URL slug from the act prefix (FACC → hkcfa).
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=FACC3%2F2025"
        )
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfa/2025/"

    def test_getappealhistory_compact_hcmp_resolves_to_hkcfi(self):
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=HCMP2265%2F2025"
        )
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkcfi/2025/"

    def test_getappealhistory_compact_cacv_resolves_to_hkca(self):
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=CACV45%2F2024&lang=tc"
        )
        assert referer_for(url) == "https://www.hklii.hk/tc/cases/hkca/2024/"

    def test_getappealhistory_compact_dccc_resolves_to_hkdc(self):
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=DCCC12%2F2023"
        )
        assert referer_for(url) == "https://www.hklii.hk/en/cases/hkdc/2023/"

    def test_getappealhistory_unknown_prefix_still_returns_homepage(self):
        # A prefix outside COURT_PREFIX_MAP can't be resolved to a slug,
        # so fall back to homepage — same as pre-fix behaviour for these.
        url = (
            "https://www.hklii.hk/api/getappealhistory"
            "?caseno=ZZZZ99%2F2024"
        )
        assert referer_for(url) == "https://www.hklii.hk/"


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
