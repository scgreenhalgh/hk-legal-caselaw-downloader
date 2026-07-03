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
