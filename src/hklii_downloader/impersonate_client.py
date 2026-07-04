"""httpx.AsyncClient-compatible wrapper around curl_cffi.AsyncSession.

curl_cffi impersonates real browsers' TLS + HTTP/2 fingerprints. Each
client instance random-picks a profile from a diverse pool (Chrome +
Safari + Edge) so a run spans multiple JA3/JA4 fingerprints instead of
one homogeneous stack. Exceptions are translated to httpx's hierarchy
so the scraper's retry logic works unchanged.
"""
from __future__ import annotations

import random
from typing import Any

import httpx

# curl_cffi impersonation profiles — modern Chrome only. Bare "chrome"
# tracks curl_cffi's newest supported profile automatically; explicit
# version pins give TLS-fingerprint variety across recent Chromes. Older
# profiles (chrome104/110/116/120/124) drop-shipped Chrome UAs from
# 2022-2023 which flag on any UA-age heuristic in 2026.
_IMPERSONATE_PROFILES = (
    "chrome", "chrome146", "chrome142", "chrome136", "chrome131",
)

# Headers that curl_cffi's impersonation controls end-to-end. Passing
# alternative values via .get(headers=…) would create a UA/TLS mismatch
# — the classic "not a real browser" tell — so we strip them.
#
# NOTE: sec-fetch-* and Upgrade-Insecure-Requests are BEHAVIORAL, not
# fingerprint-baked. A real browser flips sec-fetch-mode between
# 'navigate' (top-level nav) and 'cors' (fetch()/XHR) per request, and
# drops UIR on XHR entirely. The M-2 fix in
# ProxyHeadersFactory.generate() shapes these correctly for /api/*
# XHR calls; the wrapper must pass them through so curl_cffi's baked
# chrome navigation defaults don't ship on every JSON API request
# (Signal 6, research/04-anti-detection-strategy.md:511). Do not
# re-add sec-fetch-* / upgrade-insecure-requests to this frozenset.
_FINGERPRINT_HEADERS = frozenset({
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "connection",
})


class ImpersonateAsyncClient:
    def __init__(
        self,
        proxy: str | None = None,
        timeout: float = 30.0,
        rng: random.Random | None = None,
    ):
        rng = rng or random.Random()
        self._impersonate = rng.choice(_IMPERSONATE_PROFILES)
        from curl_cffi.requests import AsyncSession
        self._session = AsyncSession(
            impersonate=self._impersonate,
            timeout=timeout,
            proxy=proxy,
            allow_redirects=True,
        )

    @property
    def impersonate_profile(self) -> str:
        return self._impersonate

    async def get(self, url: str, headers: dict | None = None, **kwargs: Any):
        if headers:
            headers = {
                k: v for k, v in headers.items()
                if k.lower() not in _FINGERPRINT_HEADERS
            }
        try:
            return await self._session.get(url, headers=headers, **kwargs)
        except Exception as exc:
            raise self._translate(exc) from exc

    async def aclose(self) -> None:
        await self._session.close()

    def _translate(self, exc: Exception) -> Exception:
        """Map curl_cffi errors to httpx's hierarchy."""
        code = getattr(exc, "code", None)
        msg = str(exc)
        if code == 28:
            return httpx.TimeoutException(msg)
        if code in (6, 7):
            return httpx.ConnectError(msg)
        if code == 56:
            return httpx.ReadError(msg)
        return httpx.RequestError(msg)
