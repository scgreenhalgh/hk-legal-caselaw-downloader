import random
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from hklii_downloader.proxy_pool import (
    AllProxiesDeadError,
    HeaderRotator,
    IPLeakError,
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


class TestProxyPool:
    def test_requires_proxies_or_direct(self):
        with pytest.raises(ValueError, match="proxy.*--direct"):
            ProxyPool(proxy_urls=[], direct=False)

    def test_direct_mode_no_proxies_ok(self):
        pool = ProxyPool(proxy_urls=[], direct=True)
        assert pool.direct

    def test_proxy_mode_creates_sessions(self):
        pool = ProxyPool(proxy_urls=["http://localhost:8888", "http://localhost:8889"])
        assert len(pool.sessions) == 2

    def test_round_robin_cycles(self):
        pool = ProxyPool(proxy_urls=["http://a:1", "http://b:2", "http://c:3"])
        picks = [pool._next_healthy_session() for _ in range(6)]
        urls = [s.proxy_url for s in picks]
        assert urls == ["http://a:1", "http://b:2", "http://c:3"] * 2

    def test_round_robin_skips_dead(self):
        pool = ProxyPool(proxy_urls=["http://a:1", "http://b:2", "http://c:3"])
        pool.sessions[1].kill()
        picks = [pool._next_healthy_session() for _ in range(4)]
        urls = [s.proxy_url for s in picks]
        assert urls == ["http://a:1", "http://c:3", "http://a:1", "http://c:3"]

    def test_all_dead_raises(self):
        pool = ProxyPool(proxy_urls=["http://a:1", "http://b:2"])
        pool.sessions[0].kill()
        pool.sessions[1].kill()
        with pytest.raises(AllProxiesDeadError):
            pool._next_healthy_session()

    @pytest.mark.asyncio
    async def test_preflight_detects_ip_leak(self):
        pool = ProxyPool(proxy_urls=["http://localhost:8888"])
        home_ip = "203.0.113.1"

        mock_response = AsyncMock()
        mock_response.json.return_value = {"origin": home_ip}
        mock_response.raise_for_status = lambda: None

        with patch.object(pool, "_fetch_home_ip", return_value=home_ip):
            with patch("httpx.AsyncClient.get", return_value=mock_response):
                result = await pool.preflight()

        assert not pool.sessions[0].is_healthy
        assert home_ip in result.leaked_proxies[0]

    @pytest.mark.asyncio
    async def test_preflight_marks_healthy(self):
        pool = ProxyPool(proxy_urls=["http://localhost:8888"])
        home_ip = "203.0.113.1"
        proxy_ip = "198.51.100.5"

        mock_response = AsyncMock()
        mock_response.json.return_value = {"origin": proxy_ip}
        mock_response.raise_for_status = lambda: None

        with patch.object(pool, "_fetch_home_ip", return_value=home_ip):
            with patch("httpx.AsyncClient.get", return_value=mock_response):
                result = await pool.preflight()

        assert pool.sessions[0].is_healthy
        assert len(result.leaked_proxies) == 0

    @pytest.mark.asyncio
    async def test_preflight_required_before_requests(self):
        pool = ProxyPool(proxy_urls=["http://localhost:8888"])
        with pytest.raises(RuntimeError, match="preflight"):
            await pool.get("https://example.com")

    @pytest.mark.asyncio
    async def test_direct_mode_skips_preflight(self):
        pool = ProxyPool(proxy_urls=[], direct=True)
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.get", return_value=mock_response):
            resp = await pool.get("https://example.com")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_runtime_ip_check_kills_leaking_proxy(self):
        pool = ProxyPool(
            proxy_urls=["http://localhost:8888", "http://localhost:8889"],
            ip_check_interval=2,
        )
        pool._preflight_done = True
        pool._home_ip = "203.0.113.1"

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            if "httpbin" in url or "ifconfig" in url:
                resp.json.return_value = {"origin": "203.0.113.1"}
            else:
                resp.status_code = 200
                resp.json.return_value = {"content": "test"}
            return resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            await pool.get("https://www.hklii.hk/api/test")
            await pool.get("https://www.hklii.hk/api/test")
            with pytest.raises(IPLeakError):
                await pool.get("https://www.hklii.hk/api/test")
