from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import httpx

from .parser import referer_for as _referer_for

_monotonic = time.monotonic


class IPLeakError(Exception):
    pass


class AllProxiesDeadError(Exception):
    pass


@dataclass
class PreflightResult:
    home_ip: str
    healthy_proxies: list[str] = field(default_factory=list)
    leaked_proxies: list[str] = field(default_factory=list)
    failed_proxies: list[str] = field(default_factory=list)


class RequestThrottler:
    def __init__(
        self,
        rng: random.Random | None = None,
        base_range: tuple[float, float] = (0.5, 1.5),
        pause_range: tuple[float, float] = (3.0, 8.0),
        pause_chance: float = 0.05,
        burst_size_range: tuple[int, int] = (2, 5),
        burst_gap_range: tuple[float, float] = (2.0, 4.0),
    ):
        self._rng = rng or random.Random()
        self._base_range = base_range
        self._pause_range = pause_range
        self._pause_chance = pause_chance
        self._burst_size_range = burst_size_range
        self._burst_gap_range = burst_gap_range
        self._burst_remaining = self._rng.randint(*burst_size_range)

    def next_delay(self) -> float:
        if self._burst_remaining <= 0:
            self._burst_remaining = self._rng.randint(*self._burst_size_range)
            return self._rng.uniform(*self._burst_gap_range)

        self._burst_remaining -= 1

        if self._rng.random() < self._pause_chance:
            return self._rng.uniform(*self._pause_range)

        return self._rng.uniform(*self._base_range)


_CHROME_VERSIONS = [
    ("126", "126.0.6478.126"),
    ("127", "127.0.6533.72"),
    ("128", "128.0.6613.84"),
    ("129", "129.0.6668.58"),
    ("130", "130.0.6723.69"),
    ("131", "131.0.6778.86"),
    ("132", "132.0.6834.110"),
    ("133", "133.0.6943.98"),
    ("134", "134.0.6998.72"),
    ("135", "135.0.7049.84"),
    ("136", "136.0.7103.92"),
    ("137", "137.0.7151.68"),
    ("138", "138.0.7204.93"),
    ("139", "139.0.7258.54"),
    ("140", "140.0.7310.70"),
    ("141", "141.0.7356.83"),
    ("142", "142.0.7401.67"),
    ("143", "143.0.7450.81"),
    ("144", "144.0.7497.73"),
    ("145", "145.0.7538.62"),
    ("146", "146.0.7580.89"),
    ("147", "147.0.7623.56"),
    ("148", "148.0.7665.93"),
]

_OS_VARIANTS = [
    ("Macintosh; Intel Mac OS X 10_15_7", '"macOS"'),
    ("Windows NT 10.0; Win64; x64", '"Windows"'),
    ("X11; Linux x86_64", '"Linux"'),
]


class HeaderRotator:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._headers = self._build_headers()

    def _build_headers(self) -> dict[str, str]:
        major, full = self._rng.choice(_CHROME_VERSIONS)
        os_string, platform = self._rng.choice(_OS_VARIANTS)
        return {
            "User-Agent": (
                f"Mozilla/5.0 ({os_string}) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{full} Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en-GB;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": platform,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-user": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def generate(self, url: str | None = None) -> dict[str, str]:
        headers = dict(self._headers)
        if url is not None and "/api/" in url:
            # XHR: Chrome sends mode:cors, dest:empty on fetch()/XHR to
            # same-origin JSON APIs, and never sec-fetch-user or UIR.
            headers["sec-fetch-mode"] = "cors"
            headers["sec-fetch-dest"] = "empty"
            headers.pop("sec-fetch-user", None)
            headers.pop("Upgrade-Insecure-Requests", None)
        return headers

    def referer_for(self, url: str) -> str:
        return _referer_for(url)


class ProxySession:
    def __init__(
        self,
        proxy_url: str = "",
        index: int = 0,
        max_failures: int = 5,
        cooldown_seconds: float = 300.0,
    ):
        self.proxy_url = proxy_url
        self.index = index
        self.request_count = 0
        self._max_failures = max_failures
        self._cooldown_seconds = cooldown_seconds
        self._failure_count = 0
        self._killed = False
        self._killed_at: float | None = None

    @property
    def is_healthy(self) -> bool:
        return not self._killed

    def record_success(self) -> None:
        if not self._killed:
            self._failure_count = 0
            self.request_count += 1

    def record_failure(self) -> None:
        if self._killed:
            return
        self._failure_count += 1
        if self._failure_count >= self._max_failures:
            self.kill()

    def kill(self) -> None:
        self._killed = True
        self._killed_at = _monotonic()

    def revive(self) -> None:
        self._killed = False
        self._killed_at = None
        self._failure_count = 0

    @property
    def cooldown_elapsed(self) -> bool:
        if not self._killed or self._killed_at is None:
            return False
        return (_monotonic() - self._killed_at) >= self._cooldown_seconds


_IP_ECHO_URLS: list[tuple[str, str]] = [
    ("https://httpbin.org/ip", "origin"),
    ("https://ipinfo.io/json", "ip"),
]

# Per-proxy session warm-up target (M-4). Fired after IP echo so the
# first HKLII request from each proxy has a plausible browsing history:
# a landing-page GET establishes a session cookie (if any) and puts
# something in the Referer chain before /api/* calls hit the wire.
_WARMUP_URL = "https://www.hklii.hk/"

# Status codes that count as a soft failure against the proxy's circuit
# breaker. 429/403/5xx all indicate the proxy (or the exit IP) is having
# trouble; if this repeats we should stop using it. 4xx client errors
# other than 403/429 (e.g. 404) are about the resource, not the proxy.
_PROXY_FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}


class ProxyPool:
    def __init__(
        self,
        proxy_urls: list[str] | None = None,
        direct: bool = False,
        ip_check_interval: int = 50,
        max_failures: int = 5,
        cooldown_seconds: float = 300.0,
        _transport_factory=None,
    ):
        proxy_urls = proxy_urls or []
        if not proxy_urls and not direct:
            raise ValueError("Must provide proxy URLs or use --direct")

        self.direct = direct
        self._ip_check_interval = ip_check_interval
        self._transport_factory = _transport_factory
        self._preflight_done = direct
        self._home_ip: str | None = None

        self.sessions: list[ProxySession] = []
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._throttlers: dict[int, RequestThrottler] = {}
        self._headers: dict[int, HeaderRotator] = {}
        self._available: asyncio.Queue[int] = asyncio.Queue()

        for i, url in enumerate(proxy_urls):
            session = ProxySession(
                proxy_url=url, index=i,
                max_failures=max_failures,
                cooldown_seconds=cooldown_seconds,
            )
            self.sessions.append(session)
            self._clients[i] = self._make_client(url)
            self._throttlers[i] = RequestThrottler(rng=random.Random(i))
            self._headers[i] = HeaderRotator(rng=random.Random(i + 1000))
            self._available.put_nowait(i)

        if direct:
            self._direct_client = self._make_client(None)

    def _make_client(self, proxy_url: str | None):
        # Tests inject an httpx.MockTransport via _transport_factory; keep
        # that path on httpx so existing test infrastructure works.
        if self._transport_factory:
            return httpx.AsyncClient(
                transport=self._transport_factory(proxy_url),
                trust_env=False,
                timeout=httpx.Timeout(30.0),
            )
        # Production: curl_cffi with a browser TLS/HTTP2 fingerprint,
        # random-picked per session for diversity.
        from .impersonate_client import ImpersonateAsyncClient
        return ImpersonateAsyncClient(
            proxy=proxy_url, timeout=30.0,
            rng=random.Random(hash((proxy_url, "impersonate"))),
        )

    async def preflight(self) -> PreflightResult:
        home_ip = await self._fetch_ip(self._make_client(None))
        self._home_ip = home_ip
        result = PreflightResult(home_ip=home_ip)

        for session in self.sessions:
            client = self._clients[session.index]
            try:
                proxy_ip = await self._fetch_ip(client)
            except (httpx.RequestError, KeyError) as exc:
                result.failed_proxies.append(
                    f"{session.proxy_url} unreachable: {exc}"
                )
                session.kill()
                continue

            if proxy_ip == home_ip:
                result.leaked_proxies.append(
                    f"{session.proxy_url} returned home IP {home_ip}"
                )
                session.kill()
            else:
                result.healthy_proxies.append(session.proxy_url)
                await self._warm_up_target(session, client)

        self._preflight_done = True
        return result

    async def _warm_up_target(self, session: ProxySession, client) -> None:
        """Fire one landing-page GET so the first API call from this proxy
        has a plausible browsing history (session cookies, Referer chain).
        Best-effort — failure here does not disqualify the proxy since IP
        echo already confirmed routability."""
        headers = self._headers[session.index]
        req_headers = headers.generate(_WARMUP_URL)
        req_headers["Referer"] = headers.referer_for(_WARMUP_URL)
        try:
            await client.get(_WARMUP_URL, headers=req_headers)
        except (httpx.RequestError, Exception):
            # Best-effort — do not fail preflight if the origin blips.
            pass

    async def _fetch_ip(self, client: httpx.AsyncClient) -> str:
        for echo_url, json_key in _IP_ECHO_URLS:
            try:
                resp = await client.get(echo_url)
                resp.raise_for_status()
                return resp.json()[json_key]
            except (
                httpx.RequestError,
                httpx.HTTPStatusError,
                KeyError,
                json.JSONDecodeError,
            ):
                continue
        raise httpx.ConnectError("All IP echo services unreachable")

    async def get(self, url: str, **kwargs) -> httpx.Response:
        if not self._preflight_done:
            raise RuntimeError("Must call preflight() before making requests")

        if self.direct:
            direct_headers = dict(kwargs.pop("headers", None) or {})
            direct_headers.setdefault("Referer", _referer_for(url))
            return await self._direct_client.get(url, headers=direct_headers, **kwargs)

        idx = await self._acquire_session()
        session = self.sessions[idx]
        client = self._clients[idx]
        throttler = self._throttlers[idx]
        headers = self._headers[idx]

        try:
            delay = throttler.next_delay()
            await asyncio.sleep(delay)

            if (session.request_count > 0
                    and session.request_count % self._ip_check_interval == 0):
                await self._runtime_ip_check(session, client)

            req_headers = headers.generate(url)
            req_headers["Referer"] = headers.referer_for(url)

            try:
                resp = await client.get(url, headers=req_headers, **kwargs)
                if resp.status_code in _PROXY_FAILURE_STATUSES:
                    session.record_failure()
                else:
                    session.record_success()
                return resp
            except httpx.RequestError:
                session.record_failure()
                raise
        finally:
            if session.is_healthy:
                self._available.put_nowait(idx)

    async def _acquire_session(self) -> int:
        while True:
            self._revive_cooled_down_sessions()
            if not any(s.is_healthy for s in self.sessions):
                raise AllProxiesDeadError("All proxy sessions are dead")
            try:
                idx = await asyncio.wait_for(
                    self._available.get(), timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue
            if self.sessions[idx].is_healthy:
                return idx

    def _revive_cooled_down_sessions(self) -> None:
        for session in self.sessions:
            if session.cooldown_elapsed:
                session.revive()
                self._available.put_nowait(session.index)

    async def _runtime_ip_check(
        self, session: ProxySession, client: httpx.AsyncClient,
    ) -> None:
        try:
            current_ip = await self._fetch_ip(client)
        except httpx.RequestError:
            return

        if current_ip != self._home_ip:
            return

        try:
            verify_ip = await self._fetch_ip(client)
        except httpx.RequestError:
            return

        if verify_ip == self._home_ip:
            session.kill()
            raise IPLeakError(
                f"Proxy {session.proxy_url} leaking home IP {self._home_ip} "
                f"(verified twice)"
            )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        if hasattr(self, "_direct_client"):
            await self._direct_client.aclose()
