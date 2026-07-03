from __future__ import annotations

import random


class IPLeakError(Exception):
    pass


class AllProxiesDeadError(Exception):
    pass


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
    def __init__(self, proxy_url: str = "", index: int = 0, max_failures: int = 5):
        self.proxy_url = proxy_url
        self.is_healthy = True
        self.request_count = 0

    def record_success(self) -> None:
        pass

    def record_failure(self) -> None:
        pass

    def kill(self) -> None:
        self.is_healthy = False


class ProxyPool:
    def __init__(self, proxy_urls: list[str] | None = None, direct: bool = False,
                 ip_check_interval: int = 50, **kwargs):
        pass

    async def preflight(self):
        pass

    async def get(self, url: str, **kwargs):
        pass

    def _next_healthy_session(self):
        pass

    async def close(self) -> None:
        pass
