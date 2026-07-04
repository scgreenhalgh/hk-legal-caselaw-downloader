"""Tests for ImpersonateAsyncClient — curl_cffi wrapper with TLS impersonation."""
from __future__ import annotations

import random
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


class TestProfileSelection:
    def test_picks_a_known_profile(self):
        from hklii_downloader.impersonate_client import (
            ImpersonateAsyncClient, _IMPERSONATE_PROFILES,
        )
        c = ImpersonateAsyncClient(rng=random.Random(0))
        assert c.impersonate_profile in _IMPERSONATE_PROFILES

    def test_different_rngs_can_pick_different_profiles(self):
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient
        picks = set()
        for seed in range(30):
            c = ImpersonateAsyncClient(rng=random.Random(seed))
            picks.add(c.impersonate_profile)
        assert len(picks) >= 2, (
            f"expected multiple profiles across seeds, got {picks}"
        )

    def test_profile_pool_is_modern_chrome_only(self):
        """Policy (item M-3, 2026-07-04 audit): pool must be modern Chrome
        only. Previous policy mixed vendors + old versions for TLS-fingerprint
        diversity, but curl_cffi sets User-Agent per-profile, so a chrome104
        profile in 2026 sent a Chrome/104 UA the origin's access-log parser
        would flag as stale. UA freshness dominates vendor diversity — Chrome
        is 65%+ of real browser share anyway. Bare 'chrome' alias is allowed
        because it auto-tracks curl_cffi's newest profile."""
        from hklii_downloader.impersonate_client import _IMPERSONATE_PROFILES
        for profile in _IMPERSONATE_PROFILES:
            if profile == "chrome":
                continue
            assert profile.startswith("chrome"), (
                f"profile {profile!r} is not chrome — pool must be Chrome-only"
            )
            m = re.match(r"^chrome(\d+)$", profile)
            assert m, f"profile {profile!r} has non-numeric suffix"
            version = int(m.group(1))
            assert version >= 131, (
                f"profile {profile!r} is version {version}; must be >=131 "
                f"(late-2024 Chrome release). Stale UAs are a detection signal."
            )

    def test_bare_chrome_alias_included(self):
        """The bare 'chrome' alias auto-tracks the newest supported profile —
        picking it a fraction of the time keeps the fleet current as
        curl_cffi ships new releases without needing pool updates."""
        from hklii_downloader.impersonate_client import _IMPERSONATE_PROFILES
        assert "chrome" in _IMPERSONATE_PROFILES, (
            "expected bare 'chrome' alias in pool for auto-tracking"
        )

    def test_no_stale_profiles(self):
        """Explicit guard against known-stale profiles sneaking back in
        via a copy-paste. chrome104 = July 2022, chrome116 = Aug 2023 —
        both send 3-4 year old Chrome UAs in 2026."""
        from hklii_downloader.impersonate_client import _IMPERSONATE_PROFILES
        stale = {"chrome104", "chrome110", "chrome116", "chrome120", "chrome124"}
        intersect = set(_IMPERSONATE_PROFILES) & stale
        assert not intersect, (
            f"stale profiles found in pool: {intersect}. Drop these — they "
            f"send 2022-2023 Chrome UAs that no real 2026 user emits."
        )


class TestHeaderStripping:
    async def test_fingerprint_conflict_headers_stripped(self):
        """Passing UA / sec-ch-ua / Accept-Language headers to .get()
        would contradict curl_cffi's baked TLS+HTTP/2 impersonation
        fingerprint. Wrapper strips them.

        sec-fetch-* is intentionally NOT stripped — see
        TestSecFetchForwarding — because it's behavioral (per-request
        XHR vs navigation), not fingerprint-baked."""
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient

        captured = {}

        class FakeSession:
            async def get(self, url, headers=None, **kw):
                captured["headers"] = headers or {}
                resp = MagicMock()
                resp.status_code = 200
                return resp
            async def close(self):
                pass

        c = ImpersonateAsyncClient(rng=random.Random(0))
        c._session = FakeSession()
        await c.get("https://example.com", headers={
            "User-Agent": "not-really-chrome",
            "sec-ch-ua": "spoofed",
            "Referer": "https://x.com/",
            "Accept-Language": "en-US",   # also strip: curl_cffi handles it
        })
        sent = captured["headers"]
        assert "User-Agent" not in sent and "user-agent" not in sent
        assert "sec-ch-ua" not in sent
        assert "Accept-Language" not in sent
        assert "Referer" in sent, "non-fingerprint headers should pass through"


class TestSecFetchForwarding:
    @pytest.mark.parametrize("profile", ["chrome136", "chrome146"])
    async def test_forwards_sec_fetch_and_uir_to_wire(self, profile):
        """XHR-shape sec-fetch/UIR overrides must reach curl_cffi.

        Signal 6 (research/04-anti-detection-strategy.md:511): every
        /api/getcasefiles, /api/getjudgment, /api/getappealhistory call
        must ship XHR-shape headers (Sec-Fetch-Mode: cors,
        Sec-Fetch-Dest: empty, Sec-Fetch-Site: same-origin, no
        Sec-Fetch-User, no Upgrade-Insecure-Requests) because that's what
        a real browser's fetch()/XHR to a same-origin JSON API emits.

        The M-2 fix in ProxyHeadersFactory.generate() produces exactly
        those headers for /api/* URLs. But if the wrapper strips them
        as "fingerprint conflicts", curl_cffi's baked chrome136/146
        navigation defaults win — every /api/* call ships with
        Sec-Fetch-Mode: navigate + UIR: 1, which is the classic
        "not an XHR from JS" tell across 20 exit IPs and ~228K calls.

        sec-fetch-* and UIR are BEHAVIORAL (per-request) not
        fingerprint-baked: caller overrides must pass through.
        """
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient

        captured = {}

        class FakeSession:
            async def get(self, url, headers=None, **kw):
                captured["headers"] = headers or {}
                resp = MagicMock()
                resp.status_code = 200
                return resp
            async def close(self):
                pass

        c = ImpersonateAsyncClient(rng=random.Random(0))
        c._impersonate = profile  # exercise chrome136 and chrome146
        c._session = FakeSession()

        await c.get(
            "https://www.hklii.hk/api/getjudgment",
            headers={
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "sec-fetch-site": "same-origin",
                "sec-fetch-user": "?0",
            },
        )

        sent = captured["headers"]
        assert sent.get("sec-fetch-mode") == "cors", (
            f"sec-fetch-mode override must pass through for XHR shape "
            f"({profile}); got: {sent!r}"
        )
        assert sent.get("sec-fetch-dest") == "empty", (
            f"sec-fetch-dest override must pass through for XHR shape "
            f"({profile}); got: {sent!r}"
        )
        assert sent.get("sec-fetch-site") == "same-origin", (
            f"sec-fetch-site override must pass through for XHR shape "
            f"({profile}); got: {sent!r}"
        )
        assert sent.get("sec-fetch-user") == "?0", (
            f"sec-fetch-user override must pass through for XHR shape "
            f"({profile}); got: {sent!r}"
        )
        # Caller did not send UIR — wrapper must not inject it.
        lower_keys = {k.lower() for k in sent}
        assert "upgrade-insecure-requests" not in lower_keys, (
            f"UIR must not appear when caller did not send it ({profile}); "
            f"got: {sent!r}"
        )


class TestExceptionTranslation:
    async def test_curl_timeout_becomes_httpx_timeout(self):
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient
        c = ImpersonateAsyncClient(rng=random.Random(0))

        class FakeCurlError(Exception):
            def __init__(self, msg, code):
                super().__init__(msg)
                self.code = code

        class FakeSession:
            async def get(self, url, **kw):
                raise FakeCurlError("timed out", 28)
            async def close(self):
                pass

        c._session = FakeSession()
        with pytest.raises(httpx.TimeoutException):
            await c.get("https://example.com")

    async def test_curl_connect_becomes_httpx_connect(self):
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient
        c = ImpersonateAsyncClient(rng=random.Random(0))

        class FakeCurlError(Exception):
            def __init__(self, msg, code):
                super().__init__(msg)
                self.code = code

        class FakeSession:
            async def get(self, url, **kw):
                raise FakeCurlError("connect failed", 7)
            async def close(self):
                pass

        c._session = FakeSession()
        with pytest.raises(httpx.ConnectError):
            await c.get("https://example.com")

    async def test_other_curl_becomes_httpx_requesterror(self):
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient
        c = ImpersonateAsyncClient(rng=random.Random(0))

        class FakeCurlError(Exception):
            def __init__(self, msg, code):
                super().__init__(msg)
                self.code = code

        class FakeSession:
            async def get(self, url, **kw):
                raise FakeCurlError("something else", 42)
            async def close(self):
                pass

        c._session = FakeSession()
        with pytest.raises(httpx.RequestError):
            await c.get("https://example.com")
