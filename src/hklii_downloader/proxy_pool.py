from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

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
            "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": platform,
            "Upgrade-Insecure-Requests": "1",
        }

    def generate(self) -> dict[str, str]:
        return dict(self._headers)

    def rotate(self) -> None:
        self._headers = self._build_headers()

    def referer_for(self, url: str) -> str:
        return "https://www.hklii.hk/"


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
        self._round_robin_index = 0

        self.sessions: list[ProxySession] = []
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._throttlers: dict[int, RequestThrottler] = {}
        self._headers: dict[int, HeaderRotator] = {}
        self._locks: dict[int, asyncio.Lock] = {}

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
            self._locks[i] = asyncio.Lock()

        if direct:
            self._direct_client = self._make_client(None)

    def _make_client(self, proxy_url: str | None) -> httpx.AsyncClient:
        if self._transport_factory:
            return httpx.AsyncClient(
                transport=self._transport_factory(proxy_url),
                trust_env=False,
            )
        kwargs: dict = {"trust_env": False, "follow_redirects": True}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

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

        self._preflight_done = True
        return result

    async def _fetch_ip(self, client: httpx.AsyncClient) -> str:
        for echo_url, json_key in _IP_ECHO_URLS:
            try:
                resp = await client.get(echo_url)
                resp.raise_for_status()
                return resp.json()[json_key]
            except (httpx.RequestError, httpx.HTTPStatusError, KeyError):
                continue
        raise httpx.ConnectError("All IP echo services unreachable")

    async def get(self, url: str, **kwargs) -> httpx.Response:
        if not self._preflight_done:
            raise RuntimeError("Must call preflight() before making requests")

        if self.direct:
            return await self._direct_client.get(url, **kwargs)

        session = self._next_healthy_session()
        client = self._clients[session.index]
        throttler = self._throttlers[session.index]
        headers = self._headers[session.index]

        async with self._locks[session.index]:
            delay = throttler.next_delay()
            await asyncio.sleep(delay)

            if (session.request_count > 0
                    and session.request_count % self._ip_check_interval == 0):
                await self._runtime_ip_check(session, client)

            req_headers = headers.generate()
            req_headers["Referer"] = headers.referer_for(url)

            try:
                resp = await client.get(url, headers=req_headers, **kwargs)
                session.record_success()
                return resp
            except httpx.RequestError:
                session.record_failure()
                raise

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

    def _next_healthy_session(self) -> ProxySession:
        start = self._round_robin_index
        n = len(self.sessions)
        for _ in range(n):
            session = self.sessions[self._round_robin_index % n]
            self._round_robin_index = (self._round_robin_index + 1) % n
            if session.is_healthy:
                return session
        raise AllProxiesDeadError("All proxy sessions are dead")

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        if hasattr(self, "_direct_client"):
            await self._direct_client.aclose()
