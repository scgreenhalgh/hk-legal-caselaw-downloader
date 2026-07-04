"""httpx.AsyncClient-compatible wrapper around curl_cffi.AsyncSession.

curl_cffi impersonates real browsers' TLS + HTTP/2 fingerprints. Each
client instance random-picks a profile from a diverse pool (Chrome +
Safari + Edge) so a run spans multiple JA3/JA4 fingerprints instead of
one homogeneous stack. Exceptions are translated to httpx's hierarchy
so the scraper's retry logic works unchanged.

Header-supply policy (Round 4 fix, W2)
--------------------------------------
The AsyncSession is constructed with ``default_headers=False`` so
curl_cffi's C-level bake is suppressed. That bake is what
``c.impersonate(profile, default_headers=True)`` in
``curl_cffi/requests/utils.py`` does — it stamps navigation-shape
headers (Sec-Fetch-User: ?1, Upgrade-Insecure-Requests: 1,
sec-ch-ua*, Chrome UA, Accept, Accept-Language, Accept-Encoding) onto
every request, regardless of whether the Python-side ``headers`` dict
contains them. Popping them from the caller's dict does nothing at the
wire layer — they still ship.

With ``default_headers=False``:

*   The TLS/HTTP/2 fingerprint (JA3/JA4, ALPN order, HTTP/2 SETTINGS,
    frame priorities) is STILL baked at the socket layer by
    ``c.impersonate()``. That is the fingerprint that actually
    identifies the browser to a WAF — headers are cosmetic mimicry on
    top of it.
*   No headers are baked. The caller (``HeaderRotator.generate()`` in
    proxy_pool.py) owns the full header block: UA, Accept,
    Accept-Language, sec-ch-ua*, sec-fetch-*, Connection, etc. That
    layer already reshapes sec-fetch-* per URL (navigate for landing,
    cors/empty for /api/* XHR) and drops sec-fetch-user + UIR on XHR
    — which is what real Chrome fetch()/XHR emits and what avoids
    ModSecurity Signal 6 (research/04-anti-detection-strategy.md:511)
    across ~228K API calls in a full corpus run.

The wrapper therefore no longer strips "fingerprint conflict" headers.
Under the old ``default_headers=True`` regime that stripping was
harmless (curl_cffi supplied its own values anyway); under
``default_headers=False`` it would erase the caller's UA + Accept-*
entirely, leaving the request with no UA — a worse tell than the
C-level bake. Consistency between the caller's UA and the impersonated
TLS profile is the caller's responsibility; HeaderRotator only ever
emits Chrome UAs, so no cross-vendor mismatch is possible in practice.
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
        # default_headers=False suppresses curl_cffi's C-level header
        # bake — see module docstring. TLS/HTTP/2 fingerprint is still
        # applied via impersonate; only the header block is left to the
        # caller.
        self._session = AsyncSession(
            impersonate=self._impersonate,
            timeout=timeout,
            proxy=proxy,
            allow_redirects=True,
            default_headers=False,
        )

    @property
    def impersonate_profile(self) -> str:
        return self._impersonate

    async def get(self, url: str, headers: dict | None = None, **kwargs: Any):
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
