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

    def next_delay(self) -> float:
        return 0.0


class HeaderRotator:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()

    def generate(self) -> dict[str, str]:
        return {}

    def rotate(self) -> None:
        pass

    def referer_for(self, url: str) -> str:
        return ""


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
