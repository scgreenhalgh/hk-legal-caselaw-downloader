import asyncio
import logging
import random
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hklii_downloader.proxy_pool import (
    AllProxiesDeadError,
    HeaderRotator,
    IPLeakError,
    PreflightResult,
    ProxyPool,
    ProxySession,
    RequestThrottler,
)


class TestRequestThrottler:
    # Bounds updated 2026-07-06 alongside the throttler retuning — see
    # proxy_pool.py:RequestThrottler for the rationale.

    def test_normal_delay_in_range(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(100)]
        for d in delays:
            assert d >= 0.1
            assert d <= 3.0

    def test_most_delays_are_short(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(1000)]
        short = [d for d in delays if d <= 0.5]
        assert len(short) / len(delays) > 0.70

    def test_some_reading_pauses(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(1000)]
        long = [d for d in delays if d > 0.9]
        assert len(long) > 0, "Expected some longer 'reading' pauses"

    def test_burst_pause_after_cluster(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng, burst_size_range=(3, 3))
        delays = []
        for _ in range(12):
            delays.append(throttler.next_delay())
        pauses = [d for d in delays if d > 0.3]
        assert len(pauses) >= 2, "Expected burst pauses after every 3 requests"

    def test_seeded_rng_is_deterministic(self):
        a = [RequestThrottler(rng=random.Random(99)).next_delay() for _ in range(10)]
        b = [RequestThrottler(rng=random.Random(99)).next_delay() for _ in range(10)]
        assert a == b


class TestHeaderRotator:
    def test_generates_chrome_ua(self):
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate()
        assert "Chrome/" in headers["User-Agent"]
        assert "Mozilla/5.0" in headers["User-Agent"]

    def test_different_seeds_give_different_uas(self):
        a = HeaderRotator(rng=random.Random(1)).generate()
        b = HeaderRotator(rng=random.Random(2)).generate()
        assert a["User-Agent"] != b["User-Agent"]

    def test_includes_standard_browser_headers(self):
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate()
        assert "Accept" in headers
        assert "Accept-Language" in headers
        assert "sec-ch-ua" in headers

    def test_includes_accept_encoding_and_connection(self):
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate()
        assert "Accept-Encoding" in headers
        assert "gzip" in headers["Accept-Encoding"]
        assert "Connection" in headers
        assert headers["Connection"].lower() == "keep-alive"

    def test_includes_sec_fetch_triad(self):
        """Real Chrome always sends sec-fetch-site/mode/dest on every request."""
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate()
        assert "sec-fetch-site" in headers
        assert "sec-fetch-mode" in headers
        assert "sec-fetch-dest" in headers

    def test_referer_for_judgment_url(self):
        rotator = HeaderRotator(rng=random.Random(42))
        referer = rotator.referer_for("https://www.hklii.hk/api/getjudgment?abbr=hkcfi&year=2024&num=1")
        assert "hklii.hk" in referer

    def test_referer_derives_year_list_page_for_getjudgment(self):
        rotator = HeaderRotator(rng=random.Random(42))
        referer = rotator.referer_for(
            "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234"
        )
        assert referer == "https://www.hklii.hk/en/cases/hkcfi/2024/", (
            f"expected URL-derived referer, got {referer!r}"
        )

    def test_referer_derives_court_list_page_for_getcasefiles(self):
        rotator = HeaderRotator(rng=random.Random(42))
        referer = rotator.referer_for(
            "https://www.hklii.hk/api/getcasefiles?caseDb=hkca&lang=tc&itemsPerPage=1000&page=1"
        )
        assert referer == "https://www.hklii.hk/tc/cases/hkca/", (
            f"expected URL-derived referer, got {referer!r}"
        )

    def test_api_url_emits_xhr_sec_fetch(self):
        """Real Chrome XHR to /api/* sends mode:cors, dest:empty, and
        neither sec-fetch-user nor Upgrade-Insecure-Requests. The
        navigation quad (mode:navigate, dest:document, user:?1, UIR:1)
        combined with /api/ is a bulletproof WAF signal."""
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate(
            "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en"
        )
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["sec-fetch-dest"] == "empty"
        assert headers.get("sec-fetch-site") == "same-origin"
        assert "sec-fetch-user" not in headers, (
            f"XHR must not send sec-fetch-user; got {headers.get('sec-fetch-user')!r}"
        )
        assert "Upgrade-Insecure-Requests" not in headers, (
            f"XHR must not send Upgrade-Insecure-Requests; got "
            f"{headers.get('Upgrade-Insecure-Requests')!r}"
        )

    def test_non_api_url_keeps_navigation_sec_fetch(self):
        """Landing-page warm-up (M-4) needs the navigation quad."""
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate("https://www.hklii.hk/en/cases/hkcfi/")
        assert headers["sec-fetch-mode"] == "navigate"
        assert headers["sec-fetch-dest"] == "document"
        assert headers.get("sec-fetch-user") == "?1"
        assert headers.get("Upgrade-Insecure-Requests") == "1"

    def test_generate_without_url_defaults_to_navigation(self):
        """Backward-compat: existing tests call generate() with no args."""
        rotator = HeaderRotator(rng=random.Random(42))
        headers = rotator.generate()
        assert headers["sec-fetch-mode"] == "navigate"
        assert headers["sec-fetch-dest"] == "document"


class TestProxySession:
    def test_starts_healthy(self):
        session = ProxySession(proxy_url="http://localhost:8888", index=0)
        assert session.is_healthy

    def test_circuit_breaker_marks_dead(self):
        session = ProxySession(proxy_url="http://localhost:8888", index=0, max_failures=3)
        session.record_failure()
        session.record_failure()
        assert session.is_healthy
        session.record_failure()
        assert not session.is_healthy

    def test_success_resets_failure_count(self):
        session = ProxySession(proxy_url="http://localhost:8888", index=0, max_failures=3)
        session.record_failure()
        session.record_failure()
        session.record_success()
        session.record_failure()
        session.record_failure()
        assert session.is_healthy

    def test_tracks_request_count(self):
        session = ProxySession(proxy_url="http://localhost:8888", index=0)
        assert session.request_count == 0
        session.record_success()
        assert session.request_count == 1
        session.record_success()
        assert session.request_count == 2

    def test_dead_proxy_stays_dead(self):
        session = ProxySession(proxy_url="http://localhost:8888", index=0, max_failures=1)
        session.record_failure()
        assert not session.is_healthy
        session.record_success()
        assert not session.is_healthy


def _noop_transport(proxy_url):
    return httpx.MockTransport(lambda r: httpx.Response(200))


class _StubThrottler:
    def __init__(self, delay: float):
        self._delay = delay

    def next_delay(self) -> float:
        return self._delay


class TestProxyPool:
    def test_requires_proxies_or_direct(self):
        with pytest.raises(ValueError, match="proxy.*--direct"):
            ProxyPool(proxy_urls=[], direct=False)

    def test_direct_mode_no_proxies_ok(self):
        pool = ProxyPool(proxy_urls=[], direct=True, _transport_factory=_noop_transport)
        assert pool.direct

    def test_proxy_mode_creates_sessions(self):
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888", "http://localhost:8889"],
            _transport_factory=_noop_transport,
        )
        assert len(pool.sessions) == 2

    async def test_all_dead_raises(self):
        pool = ProxyPool(
            proxy_urls=["http://a:1", "http://b:2"],
            _transport_factory=_noop_transport,
        )
        pool._preflight_done = True
        pool.sessions[0].kill()
        pool.sessions[1].kill()
        with pytest.raises(AllProxiesDeadError):
            await pool.get("https://example.com")

    async def test_preflight_detects_ip_leak(self):
        home_ip = "203.0.113.1"

        def make_transport(proxy_url):
            def handler(request):
                return httpx.Response(200, json={"origin": home_ip})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()

        assert not pool.sessions[0].is_healthy
        assert home_ip in result.leaked_proxies[0]

    async def test_preflight_marks_healthy(self):
        home_ip = "203.0.113.1"
        proxy_ip = "198.51.100.5"

        def make_transport(proxy_url):
            def handler(request):
                ip = home_ip if proxy_url is None else proxy_ip
                return httpx.Response(200, json={"origin": ip})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()

        assert pool.sessions[0].is_healthy
        assert len(result.leaked_proxies) == 0
        assert result.home_ip == home_ip

    async def test_preflight_required_before_requests(self):
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=_noop_transport,
        )
        with pytest.raises(RuntimeError, match="preflight"):
            await pool.get("https://example.com")

    async def test_direct_mode_skips_preflight(self):
        def make_transport(proxy_url):
            def handler(request):
                return httpx.Response(200, json={"data": "test"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=[], direct=True,
            _transport_factory=make_transport,
        )
        resp = await pool.get("https://example.com")
        assert resp.status_code == 200

    async def test_direct_mode_sets_referer_derived_from_url(self):
        """Direct mode had NO Referer at all — pure API-first-hit signal.
        Fix: derive Referer from URL context per request."""
        captured = []

        def make_transport(proxy_url):
            def handler(request):
                captured.append(dict(request.headers))
                return httpx.Response(200, json={"data": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=[], direct=True,
            _transport_factory=make_transport,
        )
        await pool.get(
            "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234"
        )
        assert captured, "direct-mode fetch never fired"
        assert captured[0].get("referer") == "https://www.hklii.hk/en/cases/hkcfi/2024/", (
            f"expected URL-derived Referer, got headers={captured[0]}"
        )

    async def test_direct_mode_ships_full_headerrotator_headers(self):
        """W5 / task #61 — post-W2's default_headers=False regime,
        direct mode was shipping bare {Referer, Host, accept-encoding}
        — a much stronger bot signal than the proxy branch (which
        already runs HeaderRotator.generate()). Fix: apply the same
        HeaderRotator to the direct branch."""
        captured = []

        def make_transport(proxy_url):
            def handler(request):
                captured.append(dict(request.headers))
                return httpx.Response(200, json={"data": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=[], direct=True,
            _transport_factory=make_transport,
        )
        await pool.get(
            "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234"
        )
        h = captured[0]
        assert h.get("user-agent", "").startswith("Mozilla/5.0"), (
            f"missing HeaderRotator UA on direct branch — got headers={h}"
        )
        assert "chrome/" in h.get("user-agent", "").lower()
        assert "accept-language" in h
        assert h.get("accept-language", "").startswith("en-")
        assert "sec-ch-ua" in h
        assert h.get("sec-ch-ua-mobile") == "?0"
        # For /api/ URLs, HeaderRotator downgrades to XHR shape
        assert h.get("sec-fetch-mode") == "cors"
        assert h.get("sec-fetch-dest") == "empty"
        # sec-fetch-user + UIR are Chrome navigation-only; XHRs never
        # send them (W2 baseline).
        assert "sec-fetch-user" not in h
        assert "upgrade-insecure-requests" not in h

    async def test_direct_mode_kwargs_headers_override_rotator(self):
        """Per-call header overrides win over HeaderRotator defaults —
        callers still have final say (used e.g. by _fetch_ip's noop
        transport probe)."""
        captured = []

        def make_transport(proxy_url):
            def handler(request):
                captured.append(dict(request.headers))
                return httpx.Response(200, json={"data": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=[], direct=True,
            _transport_factory=make_transport,
        )
        await pool.get(
            "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfi&year=2024&num=1234",
            headers={"X-Custom": "1", "User-Agent": "override/1"},
        )
        h = captured[0]
        assert h.get("x-custom") == "1"
        assert h.get("user-agent") == "override/1"

    async def test_proxy_mode_referer_derived_from_url(self):
        """Proxy mode was hardcoded to homepage — every request advertised the
        same Referer. Fix: derive per-URL."""
        captured = []

        def make_transport(proxy_url):
            def handler(request):
                captured.append(dict(request.headers))
                return httpx.Response(200, json={"data": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        pool._preflight_done = True
        pool._home_ip = "203.0.113.1"

        await pool.get(
            "https://www.hklii.hk/api/getcasefiles?caseDb=hkcfi&lang=en&itemsPerPage=10000&page=1"
        )
        assert captured, "proxy-mode fetch never fired"
        assert captured[0].get("referer") == "https://www.hklii.hk/en/cases/hkcfi/", (
            f"expected URL-derived Referer, got headers={captured[0]}"
        )

    async def test_preflight_warms_up_hklii_origin_after_ip_check(self):
        """M-4: after each proxy's IP echo confirms it's routable + non-
        leaking, fire a warm-up GET to hklii.hk homepage. This breaks the
        'first request from this IP is /api/*' cold-XHR signature (rule 4)
        and lets curl_cffi's session pick up any cookies HKLII sets."""
        urls_seen: list[str] = []
        counter = [0]

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                urls_seen.append(url)
                if "httpbin" in url or "ipinfo" in url:
                    counter[0] += 1
                    return httpx.Response(200, json={"origin": f"1.2.3.{counter[0]}", "ip": f"1.2.3.{counter[0]}"})
                return httpx.Response(200, text="<html>HKLII homepage</html>")
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()
        assert result.leaked_proxies == []
        assert result.failed_proxies == []

        hklii_warmups = [
            u for u in urls_seen
            if "www.hklii.hk" in u and "/api/" not in u
        ]
        assert hklii_warmups, (
            f"expected a warm-up GET to https://www.hklii.hk/ (or a "
            f"non-API HKLII page) after IP echo; saw only {urls_seen}"
        )

    async def test_preflight_logs_warmup_and_ip_echo_per_proxy(self, caplog):
        """B4: the runbook's mandatory pre-production canary greps
        scrape.log for the warm-up landing-page fetch and IP echoes.
        Without INFO records from _warm_up_target and _fetch_ip, the
        canary check returns exit 1 even against a healthy run. This
        extends the M-4 warm-up test (see
        test_preflight_warms_up_hklii_origin_after_ip_check) by asserting
        the logger emits observable evidence."""
        urls_seen: list[str] = []
        counter = [0]

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                urls_seen.append(url)
                if "httpbin" in url or "ipinfo" in url:
                    counter[0] += 1
                    ip = f"1.2.3.{counter[0]}"
                    return httpx.Response(
                        200, json={"origin": ip, "ip": ip},
                    )
                return httpx.Response(200, text="<html>HKLII homepage</html>")
            return httpx.MockTransport(handler)

        proxy_url = "http://localhost:8888"
        pool = ProxyPool(
            proxy_urls=[proxy_url],
            _transport_factory=make_transport,
        )
        with caplog.at_level(
            logging.INFO, logger="hklii_downloader.proxy_pool"
        ):
            await pool.preflight()

        infos = [
            r for r in caplog.records
            if r.name == "hklii_downloader.proxy_pool"
            and r.levelno == logging.INFO
        ]
        messages = [r.getMessage() for r in infos]

        warmups = [
            m for m in messages
            if "warmup GET https://www.hklii.hk/" in m
        ]
        assert len(warmups) == 1, (
            f"expected exactly one INFO 'warmup GET https://www.hklii.hk/' "
            f"for the sole proxy so the runbook canary grep can find it; "
            f"got {len(warmups)} in {messages}"
        )
        assert proxy_url in warmups[0], (
            f"warmup INFO must include proxy_url {proxy_url!r} so per-proxy "
            f"warmup can be distinguished; got: {warmups[0]!r}"
        )

        # Two IP echoes fire during preflight: one for home_ip via the
        # direct client, one for the proxy. Both must log.
        ip_echoes = [m for m in messages if "IP echo" in m]
        assert len(ip_echoes) >= 2, (
            f"expected at least two INFO 'IP echo' records (home + proxy); "
            f"got {len(ip_echoes)} in {messages}"
        )
        # At least one IP echo INFO must reference an observed IP so
        # scrape.log grep can confirm the proxy actually reported an IP.
        assert any("1.2.3." in m for m in ip_echoes), (
            f"at least one IP echo INFO must include the observed IP "
            f"(mock returns 1.2.3.*); got: {ip_echoes}"
        )

    async def test_runtime_ip_check_detects_leak(self):
        home_ip = "203.0.113.1"

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    return httpx.Response(200, json={"origin": home_ip})
                return httpx.Response(200, json={"content": "test"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            ip_check_interval=2,
            _transport_factory=make_transport,
        )
        pool._preflight_done = True
        pool._home_ip = home_ip

        with patch("hklii_downloader.proxy_pool.asyncio.sleep", new_callable=AsyncMock):
            await pool.get("https://www.hklii.hk/api/test")
            await pool.get("https://www.hklii.hk/api/test")
            with pytest.raises(IPLeakError):
                await pool.get("https://www.hklii.hk/api/test")

    async def test_runtime_ip_check_logs_when_echoes_unreachable(self, caplog):
        """B3: when both IP echo services blip mid-run, the runtime leak
        check silently returns. Without a log signal there's no way to
        distinguish 'both echoes blipped' from 'proxy is healthy' — and
        if gluetun's kill-switch simultaneously fails, home IP leaks
        with zero warning. The swallow path MUST emit a WARNING with
        the proxy_url so the operator can grep scrape.log."""
        proxy_url = "http://localhost:8888"
        home_ip = "203.0.113.1"
        proxy_ip = "198.51.100.5"

        # Both echoes return 503 → _fetch_ip exhausts its list and raises
        # httpx.ConnectError("All IP echo services unreachable"), which is
        # caught by the swallow at proxy_pool.py:386-387.
        def make_transport(_proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin.org" in url or "ipinfo.io" in url:
                    return httpx.Response(503, text="Service unavailable")
                return httpx.Response(200, json={"content": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=[proxy_url],
            ip_check_interval=1,
            _transport_factory=make_transport,
        )
        pool._preflight_done = True
        pool._home_ip = home_ip
        # Bypass preflight, but ensure request_count > 0 so the runtime
        # check fires on the next get(). The check runs BEFORE the API
        # call when count > 0 and count % interval == 0.
        pool.sessions[0].record_success()  # count → 1

        with patch(
            "hklii_downloader.proxy_pool.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with caplog.at_level(
                logging.WARNING, logger="hklii_downloader.proxy_pool"
            ):
                await pool.get("https://www.hklii.hk/api/test")

        warnings = [
            r for r in caplog.records
            if r.name == "hklii_downloader.proxy_pool"
            and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, (
            f"expected exactly one WARNING when both echoes fail, got "
            f"{len(warnings)}: {[r.getMessage() for r in warnings]}"
        )
        msg = warnings[0].getMessage()
        assert "runtime IP check" in msg, (
            f"WARNING must mention 'runtime IP check' so operators can "
            f"grep scrape.log; got: {msg!r}"
        )
        assert proxy_url in msg, (
            f"WARNING must include proxy_url {proxy_url!r} so the "
            f"degraded proxy is identifiable; got: {msg!r}"
        )
        assert proxy_ip is not None  # silence unused-var linters

    async def test_preflight_handles_unreachable_proxy(self):
        home_ip = "203.0.113.1"

        def make_transport(proxy_url):
            def handler(request):
                if proxy_url == "http://localhost:8889":
                    raise httpx.ConnectError("connection refused")
                ip = home_ip if proxy_url is None else "198.51.100.5"
                return httpx.Response(200, json={"origin": ip})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888", "http://localhost:8889"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()

        assert pool.sessions[0].is_healthy
        assert not pool.sessions[1].is_healthy
        assert len(result.failed_proxies) == 1
        assert "8889" in result.failed_proxies[0]

    async def test_preflight_falls_back_when_primary_echo_is_down(self):
        home_ip = "203.0.113.1"
        proxy_ip = "198.51.100.5"

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin.org" in url:
                    return httpx.Response(503, text="Service unavailable")
                if "ipinfo.io" in url:
                    ip = home_ip if proxy_url is None else proxy_ip
                    return httpx.Response(200, json={"ip": ip})
                return httpx.Response(404)
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()

        assert result.home_ip == home_ip
        assert result.healthy_proxies == ["http://localhost:8888"]

    async def test_fetch_ip_falls_back_on_non_httpx_http_error(self):
        """curl_cffi's response.raise_for_status raises curl_cffi's own
        HTTPError, not httpx.HTTPStatusError. In production, one httpbin
        502 across 20 concurrent proxies aborted the whole preflight
        because the curl_cffi exception wasn't in _fetch_ip's except
        clause. Fix: check status_code directly, don't rely on
        raise_for_status's exception class."""
        class NotHttpxHTTPError(Exception):
            """Simulates curl_cffi.requests.exceptions.HTTPError."""

        class FakeResponse:
            def __init__(self, status, payload=None):
                self.status_code = status
                self._payload = payload
            def json(self):
                return self._payload
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise NotHttpxHTTPError(f"HTTP {self.status_code}")

        async def fake_get(url, **kwargs):
            if "httpbin.org" in url:
                return FakeResponse(502)
            if "ipinfo.io" in url:
                return FakeResponse(200, {"ip": "10.0.0.5"})
            return FakeResponse(404)

        class FakeClient:
            get = staticmethod(fake_get)

        pool = ProxyPool(
            proxy_urls=[], direct=True,
            _transport_factory=lambda p: httpx.MockTransport(
                lambda r: httpx.Response(200)
            ),
        )
        ip = await pool._fetch_ip(FakeClient())
        assert ip == "10.0.0.5", (
            f"expected fallback to ipinfo when httpbin raises a non-httpx "
            f"exception; got {ip!r}"
        )

    async def test_preflight_falls_back_when_primary_returns_non_json(self):
        """Corporate SSL-intercepting proxies and captive portals often return
        200 + HTML. Preflight must fall through to ipinfo.io."""
        home_ip = "203.0.113.1"
        proxy_ip = "198.51.100.5"

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin.org" in url:
                    return httpx.Response(
                        200,
                        text="<html><title>Captive portal login</title></html>",
                    )
                if "ipinfo.io" in url:
                    ip = home_ip if proxy_url is None else proxy_ip
                    return httpx.Response(200, json={"ip": ip})
                return httpx.Response(404)
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        result = await pool.preflight()
        assert result.home_ip == home_ip
        assert result.healthy_proxies == ["http://localhost:8888"]

    def test_client_uses_generous_timeout(self):
        """Real getcasefiles requests via VPN take 10+s; the client must
        be built with a timeout comfortably above that. Works for both the
        production (curl_cffi) and test (httpx.MockTransport) paths."""
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
        )
        client = pool._clients[0]
        if hasattr(client, "timeout") and hasattr(client.timeout, "connect"):
            # httpx.AsyncClient — inspect Timeout object
            assert client.timeout.connect >= 20
            assert client.timeout.read >= 20
        else:
            # ImpersonateAsyncClient (curl_cffi) — timeout is on the session
            session_timeout = getattr(client._session, "timeout", None)
            assert session_timeout is not None
            # curl_cffi timeout is a plain float
            assert float(session_timeout) >= 20

    async def test_close_cleans_up(self):
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=_noop_transport,
        )
        await pool.close()

    async def test_repeated_5xx_trips_circuit_breaker(self):
        """A proxy returning 5xx on every request should be counted as a
        failing session, not treated as healthy because HTTP completed."""
        def make_transport(proxy_url):
            def handler(request):
                if "httpbin" in str(request.url) or "ipinfo" in str(request.url):
                    ip = "10.0.0.1" if proxy_url is None else "1.1.1.1"
                    return httpx.Response(200, json={"origin": ip})
                return httpx.Response(503, text="")
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://a:1"],
            max_failures=3,
            _transport_factory=make_transport,
        )
        await pool.preflight()
        for _ in range(3):
            try:
                await pool.get("https://example.com/x")
            except httpx.RequestError:
                pass
        assert not pool.sessions[0].is_healthy, (
            "session that returned 503 three times should be killed by "
            "circuit breaker, not treated as healthy"
        )

    async def test_repeated_403_trips_circuit_breaker(self):
        """403 (Cloudflare / WAF challenge) also counts as a soft failure —
        a poisoned proxy shouldn't stay in rotation forever."""
        def make_transport(proxy_url):
            def handler(request):
                if "httpbin" in str(request.url) or "ipinfo" in str(request.url):
                    ip = "10.0.0.1" if proxy_url is None else "1.1.1.1"
                    return httpx.Response(200, json={"origin": ip})
                return httpx.Response(403, text="Cloudflare")
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://a:1"],
            max_failures=3,
            _transport_factory=make_transport,
        )
        await pool.preflight()
        for _ in range(3):
            await pool.get("https://example.com/x")
        assert not pool.sessions[0].is_healthy

    async def test_success_after_5xx_still_resets_counter(self):
        """A real 200 after some 5xx must reset failure_count so a
        transient blip doesn't leak into a later kill."""
        state = {"stage": 0}

        def make_transport(proxy_url):
            def handler(request):
                if "httpbin" in str(request.url) or "ipinfo" in str(request.url):
                    ip = "10.0.0.1" if proxy_url is None else "1.1.1.1"
                    return httpx.Response(200, json={"origin": ip})
                if state["stage"] < 2:
                    state["stage"] += 1
                    return httpx.Response(503, text="")
                return httpx.Response(200, json={"ok": True})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://a:1"],
            max_failures=3,
            _transport_factory=make_transport,
        )
        await pool.preflight()
        for _ in range(2):
            await pool.get("https://example.com/x")
        await pool.get("https://example.com/x")
        for _ in range(2):
            state["stage"] = 0
            await pool.get("https://example.com/x")
        assert pool.sessions[0].is_healthy, (
            "success should have reset failure_count so subsequent 5xx "
            "don't accumulate over the threshold"
        )

    async def test_killed_session_revived_after_cooldown(self):
        """cooldown_elapsed + revive() are dead code today. On next
        _acquire_session poll, killed sessions whose cooldown has passed
        must be re-added to the available queue and served again."""
        import time
        pool = ProxyPool(
            proxy_urls=["http://a:1", "http://b:2"],
            cooldown_seconds=0.01,   # tiny for the test
            _transport_factory=_noop_transport,
        )
        pool._preflight_done = True
        # Kill session 0
        pool.sessions[0].kill()
        # Give the cooldown time to elapse (10ms + a bit)
        await asyncio.sleep(0.05)

        # Session 0 should be revived on next acquire attempt.
        # Drain the queue of session 1 (in use), then acquire again.
        # The pool has both sessions in queue at init. Let's just verify
        # that after cooldown, pool.sessions[0].is_healthy becomes True
        # after a pool.get()-like acquire call.
        try:
            idx1 = await asyncio.wait_for(pool._acquire_session(), timeout=2.0)
        except asyncio.TimeoutError:
            idx1 = None
        try:
            idx2 = await asyncio.wait_for(pool._acquire_session(), timeout=2.0)
        except asyncio.TimeoutError:
            idx2 = None

        indices = {idx1, idx2}
        assert 0 in indices, (
            f"session 0 should be revived after cooldown, got acquisitions {indices}"
        )
        assert pool.sessions[0].is_healthy, "session 0 should be healthy again"

    async def test_killed_session_not_revived_before_cooldown(self):
        pool = ProxyPool(
            proxy_urls=["http://a:1"],
            cooldown_seconds=60.0,   # long
            _transport_factory=_noop_transport,
        )
        pool._preflight_done = True
        pool.sessions[0].kill()
        # Try to acquire — should raise AllProxiesDeadError, not revive
        raised = None
        try:
            await asyncio.wait_for(pool._acquire_session(), timeout=1.0)
        except AllProxiesDeadError as e:
            raised = e
        assert raised is not None, (
            "sole session killed, cooldown not elapsed, must raise "
            "AllProxiesDeadError"
        )

    async def test_queue_routes_work_to_fast_session(self):
        """With 2 sessions (fast and slow) and 4 concurrent gets, a queue-based
        dispatcher must route 3 requests to the fast session (which is free 3x
        during the slow session's single request) and 1 to the slow one.

        Round-robin, by contrast, statically assigns 2 requests to each session
        and blocks 2 workers waiting on the slow one — half as much fast-session
        usage. This test discriminates queue vs round-robin under uneven
        latency.
        """
        counts = {"fast": 0, "slow": 0}

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = "10.0.0.1" if proxy_url is None else (
                        "1.1.1.1" if "fast" in proxy_url else "2.2.2.2"
                    )
                    return httpx.Response(200, json={"origin": ip})
                # Skip preflight warm-up GETs to hklii.hk (M-4). We only
                # want to count actual pool.get() work below.
                if "hklii.hk" in url:
                    return httpx.Response(200, text="<html>warmup</html>")
                key = "fast" if "fast" in (proxy_url or "") else "slow"
                counts[key] += 1
                return httpx.Response(200, json={})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://fast:1", "http://slow:2"],
            _transport_factory=make_transport,
        )
        await pool.preflight()

        # Simulate uneven latency by making the "slow" session's throttler
        # return long delays and the "fast" one's short ones.
        pool._throttlers[0] = _StubThrottler(delay=0.001)   # fast
        pool._throttlers[1] = _StubThrottler(delay=0.500)   # slow: 500x fast

        results = await asyncio.gather(*[
            pool.get(f"https://example.com/{i}") for i in range(4)
        ])
        assert len(results) == 4
        assert counts["fast"] + counts["slow"] == 4
        assert counts["fast"] >= 3, (
            f"queue should reuse the fast session while the slow one is busy; "
            f"got fast={counts['fast']}, slow={counts['slow']}"
        )


def _read_events(out_dir) -> list[dict]:
    import json
    from pathlib import Path
    p = Path(out_dir) / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


class TestProxyPoolEvents:
    """The pool is the one chokepoint every HTTP request flows through, so it
    owns the per-request / per-proxy signal: request_success, request_failed,
    warmup, ip_echo, degraded. EventLogger is optional (None = no-op)."""

    async def test_warmup_and_ip_echo_events_emitted_during_preflight(
        self, tmp_path,
    ):
        from hklii_downloader.events import StructuredEventLogger

        counter = [0]

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    counter[0] += 1
                    ip = f"9.9.9.{counter[0]}"
                    return httpx.Response(200, json={"origin": ip, "ip": ip})
                return httpx.Response(200, text="<html>HKLII</html>")
            return httpx.MockTransport(handler)

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            events=ev, _transport_factory=make_transport,
        )
        await pool.preflight()
        await ev.aclose()

        rows = _read_events(tmp_path)
        warmups = [r for r in rows if r["kind"] == "warmup"]
        assert len(warmups) == 1, f"expected 1 warmup event, got {rows}"
        assert warmups[0]["proxy_url"] == "http://localhost:8888"

        echoes = [r for r in rows if r["kind"] == "ip_echo"]
        assert len(echoes) >= 2, f"expected home + proxy ip_echo, got {echoes}"
        # The observed IP rides in extra so per-proxy IP drift is auditable.
        assert any(
            e.get("extra", {}).get("observed_ip", "").startswith("9.9.9.")
            for e in echoes
        ), f"an ip_echo must carry the observed IP in extra, got {echoes}"

    async def test_request_success_event_carries_proxy_and_status(
        self, tmp_path,
    ):
        from hklii_downloader.events import StructuredEventLogger

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = "1.1.1.1" if proxy_url is None else "2.2.2.2"
                    return httpx.Response(200, json={"origin": ip, "ip": ip})
                return httpx.Response(200, json={"content": "ok"})
            return httpx.MockTransport(handler)

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            events=ev, _transport_factory=make_transport,
        )
        await pool.preflight()
        with patch("hklii_downloader.proxy_pool.asyncio.sleep",
                   new_callable=AsyncMock):
            await pool.get("https://www.hklii.hk/api/getjudgment?x=1")
        await ev.aclose()

        rows = _read_events(tmp_path)
        successes = [
            r for r in rows
            if r["kind"] == "request_success"
            and "getjudgment" in r.get("url", "")
        ]
        assert len(successes) == 1, (
            f"expected 1 request_success for the getjudgment call, got "
            f"{[r for r in rows if r['kind']=='request_success']}"
        )
        s = successes[0]
        assert s["proxy_url"] == "http://localhost:8888"
        assert s["http_status"] == 200
        assert "elapsed_ms" in s, "request events must carry elapsed_ms"

    async def test_request_failed_event_on_failure_status(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = "1.1.1.1" if proxy_url is None else "2.2.2.2"
                    return httpx.Response(200, json={"origin": ip, "ip": ip})
                return httpx.Response(503, text="Service Unavailable")
            return httpx.MockTransport(handler)

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            events=ev, _transport_factory=make_transport,
        )
        await pool.preflight()
        with patch("hklii_downloader.proxy_pool.asyncio.sleep",
                   new_callable=AsyncMock):
            await pool.get("https://www.hklii.hk/api/getjudgment?x=2")
        await ev.aclose()

        rows = _read_events(tmp_path)
        failed = [
            r for r in rows
            if r["kind"] == "request_failed"
            and "getjudgment" in r.get("url", "")
        ]
        assert len(failed) == 1, f"expected 1 request_failed, got {rows}"
        assert failed[0]["proxy_url"] == "http://localhost:8888"
        assert failed[0]["http_status"] == 503

    async def test_degraded_event_when_runtime_ip_echoes_fail(self, tmp_path):
        from hklii_downloader.events import StructuredEventLogger

        def make_transport(_proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin.org" in url or "ipinfo.io" in url:
                    return httpx.Response(503, text="down")
                return httpx.Response(200, json={"content": "ok"})
            return httpx.MockTransport(handler)

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            ip_check_interval=1, events=ev,
            _transport_factory=make_transport,
        )
        pool._preflight_done = True
        pool._home_ip = "203.0.113.1"
        pool.sessions[0].record_success()  # count -> 1 so runtime check fires

        with patch("hklii_downloader.proxy_pool.asyncio.sleep",
                   new_callable=AsyncMock):
            await pool.get("https://www.hklii.hk/api/test")
        await ev.aclose()

        rows = _read_events(tmp_path)
        degraded = [r for r in rows if r["kind"] == "degraded"]
        assert len(degraded) == 1, f"expected 1 degraded event, got {rows}"
        assert degraded[0]["proxy_url"] == "http://localhost:8888"

    async def test_ip_echo_event_redacts_home_ip_when_via_direct(
        self, tmp_path,
    ):
        """B7: preflight's direct-mode _fetch_ip captures the operator's
        home WAN IP so later proxy exits can be compared for silent
        misrouting. That IP must NOT land in events.jsonl — the file is
        the artifact operators share/jq/dashboard on. Redaction rule:
        for the direct probe (via=='direct'), the ip_echo event must
        omit observed_ip (or set it to None); for proxy probes the IP
        MUST still ride in extra so silent-misrouting detection works
        (see test_warmup_and_ip_echo_events_emitted_during_preflight)."""
        from hklii_downloader.events import StructuredEventLogger

        home_ip = "203.0.113.99"
        proxy_ip = "198.51.100.7"

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = home_ip if proxy_url is None else proxy_ip
                    return httpx.Response(
                        200, json={"origin": ip, "ip": ip},
                    )
                return httpx.Response(200, text="<html>HKLII</html>")
            return httpx.MockTransport(handler)

        ev = StructuredEventLogger(tmp_path)
        await ev.start()
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            events=ev, _transport_factory=make_transport,
        )
        await pool.preflight()
        await ev.aclose()

        rows = _read_events(tmp_path)
        echoes = [r for r in rows if r["kind"] == "ip_echo"]
        assert len(echoes) >= 2, (
            f"expected home + proxy ip_echo events; got {echoes}"
        )

        # No emitted ip_echo may carry the home IP anywhere in extra.
        for e in echoes:
            extra = e.get("extra") or {}
            assert extra.get("observed_ip") != home_ip, (
                f"ip_echo event leaks home WAN IP {home_ip!r} to "
                f"events.jsonl (proxy_url={e.get('proxy_url')!r}, "
                f"extra={extra!r})"
            )

        # The direct-probe event must EITHER omit observed_ip OR
        # explicitly null it — never expose the home IP octets.
        direct_echoes = [e for e in echoes if e["proxy_url"] == "direct"]
        assert direct_echoes, (
            f"expected at least one direct-probe ip_echo (from the "
            f"home-IP capture); got {echoes}"
        )
        for e in direct_echoes:
            observed = (e.get("extra") or {}).get("observed_ip")
            assert observed is None, (
                f"direct-probe ip_echo must not disclose the observed "
                f"IP; got observed_ip={observed!r} in {e!r}"
            )

        # Canary preservation: the proxy exit IP MUST still ride in
        # extra so silent-misrouting detection downstream still works.
        proxy_echoes = [
            e for e in echoes if e["proxy_url"] == "http://localhost:8888"
        ]
        assert proxy_echoes, (
            f"expected a proxy ip_echo event; got {echoes}"
        )
        assert any(
            (e.get("extra") or {}).get("observed_ip") == proxy_ip
            for e in proxy_echoes
        ), (
            f"proxy ip_echo must still carry observed_ip={proxy_ip!r} "
            f"so silent-misrouting detection works; got {proxy_echoes}"
        )

    async def test_fetch_ip_log_omits_home_ip_when_via_direct(self, caplog):
        """B8: preflight's direct-mode _fetch_ip logs the observed IP at
        INFO on the stdlib logger, which lands in scrape.log — the file
        operators tail during long runs and share screenshots of. The
        home WAN IP must NOT appear on that path. Proxy IPs MUST still
        print so silent-misrouting detection from log tails still works.
        This is orthogonal to B7 (the structured event) because
        --no-events disables events.jsonl but NEVER the stdlib log."""
        home_ip = "203.0.113.99"
        proxy_ip = "198.51.100.7"

        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = home_ip if proxy_url is None else proxy_ip
                    return httpx.Response(
                        200, json={"origin": ip, "ip": ip},
                    )
                return httpx.Response(200, text="<html>HKLII</html>")
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=make_transport,
        )
        with caplog.at_level(
            logging.INFO, logger="hklii_downloader.proxy_pool"
        ):
            await pool.preflight()

        pp_records = [
            r for r in caplog.records
            if r.name == "hklii_downloader.proxy_pool"
        ]
        pp_messages = [r.getMessage() for r in pp_records]

        # No proxy_pool log record — INFO or otherwise — may contain the
        # home IP octets. scrape.log is the operator-facing audit trail.
        for m in pp_messages:
            assert home_ip not in m, (
                f"scrape.log record leaks home WAN IP {home_ip!r}: {m!r}"
            )

        # Canary preservation: at least one IP echo INFO record MUST
        # contain the proxy exit IP so operators eyeballing scrape.log
        # can spot silent misrouting (a proxy printing the home IP).
        ip_echoes = [m for m in pp_messages if "IP echo" in m]
        assert len(ip_echoes) >= 2, (
            f"expected at least two IP echo records (direct + proxy); "
            f"got {ip_echoes}"
        )
        assert any(proxy_ip in m for m in ip_echoes), (
            f"proxy IP {proxy_ip!r} must still appear in an IP echo "
            f"INFO so silent-misrouting checks from log tails work; "
            f"got {ip_echoes}"
        )

    async def test_events_none_is_a_valid_noop(self, tmp_path):
        def make_transport(proxy_url):
            def handler(request):
                url = str(request.url)
                if "httpbin" in url or "ipinfo" in url:
                    ip = "1.1.1.1" if proxy_url is None else "2.2.2.2"
                    return httpx.Response(200, json={"origin": ip, "ip": ip})
                return httpx.Response(200, json={"content": "ok"})
            return httpx.MockTransport(handler)

        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            events=None, _transport_factory=make_transport,
        )
        await pool.preflight()
        with patch("hklii_downloader.proxy_pool.asyncio.sleep",
                   new_callable=AsyncMock):
            resp = await pool.get("https://www.hklii.hk/api/test")
        assert resp.status_code == 200
        assert not (tmp_path / "events.jsonl").exists()
