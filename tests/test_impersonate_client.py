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
        """Passing UA / sec-ch-ua / sec-fetch-* headers to .get() would
        contradict curl_cffi's impersonation. Wrapper strips them."""
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
            "sec-fetch-mode": "cors",
            "Referer": "https://x.com/",
            "Accept-Language": "en-US",   # also strip: curl_cffi handles it
        })
        sent = captured["headers"]
        assert "User-Agent" not in sent and "user-agent" not in sent
        assert "sec-ch-ua" not in sent
        assert "sec-fetch-mode" not in sent
        assert "Accept-Language" not in sent
        assert "Referer" in sent, "non-fingerprint headers should pass through"


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
