"""Tests for ImpersonateAsyncClient — curl_cffi wrapper with TLS impersonation."""
from __future__ import annotations

import random
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

    def test_profile_pool_includes_diverse_browsers(self):
        """Pool should include Chrome, Safari, Edge — not just one vendor."""
        from hklii_downloader.impersonate_client import _IMPERSONATE_PROFILES
        prefixes = {p[:3] for p in _IMPERSONATE_PROFILES}
        assert len(prefixes) >= 2, (
            f"expected diverse browser vendors in pool, got {prefixes}"
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
