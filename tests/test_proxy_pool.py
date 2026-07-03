import asyncio
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
    def test_normal_delay_in_range(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(100)]
        for d in delays:
            assert d >= 0.5
            assert d <= 10.0

    def test_most_delays_are_short(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(1000)]
        short = [d for d in delays if d <= 2.0]
        assert len(short) / len(delays) > 0.70

    def test_some_reading_pauses(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng)
        delays = [throttler.next_delay() for _ in range(1000)]
        long = [d for d in delays if d > 2.0]
        assert len(long) > 0, "Expected some longer 'reading' pauses"

    def test_burst_pause_after_cluster(self):
        rng = random.Random(42)
        throttler = RequestThrottler(rng=rng, burst_size_range=(3, 3))
        delays = []
        for _ in range(12):
            delays.append(throttler.next_delay())
        pauses = [d for d in delays if d > 2.0]
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

    def test_referer_for_judgment_url(self):
        rotator = HeaderRotator(rng=random.Random(42))
        referer = rotator.referer_for("https://www.hklii.hk/api/getjudgment?abbr=hkcfi&year=2024&num=1")
        assert "hklii.hk" in referer

    def test_rotate_gives_new_headers(self):
        rotator = HeaderRotator(rng=random.Random(42))
        first = rotator.generate()
        rotator.rotate()
        second = rotator.generate()
        assert first["User-Agent"] != second["User-Agent"]


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

    def test_round_robin_cycles(self):
        pool = ProxyPool(
            proxy_urls=["http://a:1", "http://b:2", "http://c:3"],
            _transport_factory=_noop_transport,
        )
        picks = [pool._next_healthy_session() for _ in range(6)]
        urls = [s.proxy_url for s in picks]
        assert urls == ["http://a:1", "http://b:2", "http://c:3"] * 2

    def test_round_robin_skips_dead(self):
        pool = ProxyPool(
            proxy_urls=["http://a:1", "http://b:2", "http://c:3"],
            _transport_factory=_noop_transport,
        )
        pool.sessions[1].kill()
        picks = [pool._next_healthy_session() for _ in range(4)]
        urls = [s.proxy_url for s in picks]
        assert urls == ["http://a:1", "http://c:3", "http://a:1", "http://c:3"]

    def test_all_dead_raises(self):
        pool = ProxyPool(
            proxy_urls=["http://a:1", "http://b:2"],
            _transport_factory=_noop_transport,
        )
        pool.sessions[0].kill()
        pool.sessions[1].kill()
        with pytest.raises(AllProxiesDeadError):
            pool._next_healthy_session()

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

    def test_client_uses_generous_timeout(self):
        """Real getcasefiles requests via VPN take 10+s; httpx default of 5s
        times out. Client must be built with a larger timeout."""
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
        )
        client = pool._clients[0]
        connect_t = client.timeout.connect
        read_t = client.timeout.read
        assert connect_t is not None and connect_t >= 20, (
            f"connect timeout {connect_t}s too tight for slow proxy handshake"
        )
        assert read_t is not None and read_t >= 20, (
            f"read timeout {read_t}s too tight for slow enumeration API"
        )

    async def test_close_cleans_up(self):
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888"],
            _transport_factory=_noop_transport,
        )
        await pool.close()
