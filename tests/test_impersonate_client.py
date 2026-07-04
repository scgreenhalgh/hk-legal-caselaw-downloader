"""Tests for ImpersonateAsyncClient — curl_cffi wrapper with TLS impersonation."""
from __future__ import annotations

import random
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def echo_server():
    """Local HTTP/1.1 server that captures the exact wire headers seen.

    Returns (port, captured) where captured is a list[dict[str, str]] of
    lowercase header dicts, one per request. Used to verify what
    curl_cffi actually put on the wire versus what the caller passed in
    Python — the difference is the C-level bake we're checking.
    """
    captured: list[dict[str, str]] = []

    class _EchoHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(
                {k.lower(): v for k, v in self.headers.items()}
            )
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # silence
            pass

    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


class TestWireLevelBakeSuppressed:
    """Ship level (Round 4 review): curl_cffi bakes Sec-Fetch-User: ?1
    and Upgrade-Insecure-Requests: 1 into every impersonated request at
    the C layer. Popping them from the caller's Python dict does nothing
    — they still reach the wire because `c.impersonate(profile,
    default_headers=True)` in curl_cffi/requests/utils.py adds them
    regardless of what the caller passes in .get(headers=…).

    Real Chrome fetch()/XHR to a same-origin JSON API sends neither.
    Every /api/getjudgment (~114K calls) and /api/getappealhistory
    (~114K calls) shipping UIR:1 + Sec-Fetch-User:?1 is ModSecurity
    Signal 6 (research/04-anti-detection-strategy.md:511) — the classic
    "not an XHR from JS" tell across all 20 exit IPs.

    Fix: construct AsyncSession(default_headers=False, ...) so the
    C-level bake is suppressed and only the caller's dict reaches the
    wire. The wrapper must then stop stripping the "fingerprint"
    headers (UA, Accept, Accept-Language, etc.) since with
    default_headers=False, curl_cffi no longer supplies them — the
    caller (HeaderRotator) owns the full header block.
    """

    @pytest.mark.parametrize("profile", ["chrome136", "chrome146"])
    async def test_sec_fetch_user_and_uir_absent_on_wire(
        self, profile, echo_server
    ):
        """Wire-echoed headers must NOT include sec-fetch-user or
        upgrade-insecure-requests when the caller passed XHR-shape
        headers to /api/*. The check is at the wire level (via a
        local echo server) — not the Python-side dict — because
        curl_cffi's C-level bake happens after the caller's dict
        is composed."""
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient

        port, captured = echo_server
        c = ImpersonateAsyncClient(rng=random.Random(0))
        c._impersonate = profile
        # Rebuild the underlying session with our chosen profile so the
        # C-level bake happens for the profile we're asserting on.
        from curl_cffi.requests import AsyncSession
        await c._session.close()
        c._session = AsyncSession(impersonate=profile, timeout=5.0)

        try:
            # Caller passes XHR-shape headers with Chrome-consistent
            # UA + Accept-Language so mimicry is preserved even after
            # the C-level defaults are suppressed.
            await c.get(
                f"http://127.0.0.1:{port}/api/getjudgment",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.7103.92 Safari/537.36"
                    ),
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-site": "same-origin",
                },
            )
        finally:
            await c.aclose()

        assert captured, "echo server captured nothing"
        wire = captured[-1]
        assert "sec-fetch-user" not in wire, (
            f"Sec-Fetch-User leaked to wire for {profile}. Caller did "
            f"not send it — curl_cffi's C-level bake did. Full wire "
            f"headers: {wire!r}"
        )
        assert "upgrade-insecure-requests" not in wire, (
            f"Upgrade-Insecure-Requests leaked to wire for {profile}. "
            f"Caller did not send it — curl_cffi's C-level bake did. "
            f"Full wire headers: {wire!r}"
        )

    @pytest.mark.parametrize("profile", ["chrome136", "chrome146"])
    async def test_caller_ua_and_accept_language_preserved(
        self, profile, echo_server
    ):
        """Suppressing default_headers means the caller must own UA +
        Accept-Language for mimicry. Verify they still reach the wire
        (i.e., the wrapper does not strip them anymore)."""
        from hklii_downloader.impersonate_client import ImpersonateAsyncClient

        port, captured = echo_server
        c = ImpersonateAsyncClient(rng=random.Random(0))
        c._impersonate = profile
        from curl_cffi.requests import AsyncSession
        await c._session.close()
        c._session = AsyncSession(impersonate=profile, timeout=5.0)

        caller_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.92 Safari/537.36"
        )
        try:
            await c.get(
                f"http://127.0.0.1:{port}/api/getjudgment",
                headers={
                    "User-Agent": caller_ua,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-site": "same-origin",
                },
            )
        finally:
            await c.aclose()

        assert captured, "echo server captured nothing"
        wire = captured[-1]
        assert wire.get("user-agent") == caller_ua, (
            f"caller UA did not reach wire for {profile}; got "
            f"{wire.get('user-agent')!r}. Wrapper is stripping the "
            f"caller's UA, but default_headers=False means curl_cffi "
            f"no longer supplies one. Full wire headers: {wire!r}"
        )
        assert wire.get("accept-language") == "en-US,en;q=0.9", (
            f"caller Accept-Language did not reach wire for {profile}; "
            f"got {wire.get('accept-language')!r}. Full wire headers: "
            f"{wire!r}"
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
