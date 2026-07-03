#!/usr/bin/env python3
"""
VPN proxy connectivity and DNS leak test.

Auto-discovers proxies from VPN_REGION_* entries in .env, or accepts
explicit proxy URLs via --proxies.

Usage:
    uv run python scripts/test_vpn.py
    uv run python scripts/test_vpn.py --proxies http://localhost:8888
"""

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path

import httpx

BASE_PORT = 8887

IP_CHECK_URL = "https://ipinfo.io/json"
HKLII_TEST_URL = (
    "https://www.hklii.hk/api/getjudgment?lang=en&abbr=hkcfa&year=2023&num=32"
)


def discover_proxies() -> dict[str, dict]:
    env_path = Path(".env")
    if not env_path.exists():
        print("  No .env found, using defaults (vpn-1/2/3 on 8888-8890)")
        return {
            f"vpn-{i}": {
                "url": f"http://localhost:{BASE_PORT + i}",
                "container": f"hklii-vpn-{i}",
            }
            for i in range(1, 4)
        }

    proxies = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'VPN_REGION_(\d+)\s*=\s*["\']?(.+?)["\']?\s*$', line)
        if m:
            i = int(m.group(1))
            region = m.group(2)
            proxies[f"vpn-{i}"] = {
                "url": f"http://localhost:{BASE_PORT + i}",
                "container": f"hklii-vpn-{i}",
                "region": region,
            }

    if not proxies:
        print("  No VPN_REGION_* in .env, using defaults (vpn-1/2/3 on 8888-8890)")
        return {
            f"vpn-{i}": {
                "url": f"http://localhost:{BASE_PORT + i}",
                "container": f"hklii-vpn-{i}",
            }
            for i in range(1, 4)
        }

    return proxies


def container_health(name: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = r.stdout.strip()
        return status == "healthy", status
    except Exception as e:
        return False, str(e)


def container_dns(name: str) -> tuple[bool, list[str]]:
    try:
        r = subprocess.run(
            ["docker", "exec", name, "cat", "/etc/resolv.conf"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = r.stdout.strip().splitlines()
        nameservers = [
            l.split()[1] for l in lines if l.strip().startswith("nameserver")
        ]
        uses_unbound = "127.0.0.1" in nameservers
        return uses_unbound, nameservers
    except Exception as e:
        return False, [str(e)]


async def ip_info(client: httpx.AsyncClient) -> dict:
    resp = await client.get(IP_CHECK_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def hklii_test(client: httpx.AsyncClient) -> tuple[bool, str]:
    try:
        resp = await client.get(HKLII_TEST_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        title = (data.get("cases") or [{}])[0].get("title", "")
        return bool(data.get("content")), title
    except Exception as e:
        return False, str(e)


def fmt_ip(info: dict) -> None:
    print(f"       IP:       {info.get('ip', '?')}")
    print(f"       Location: {info.get('city', '?')}, {info.get('country', '?')}")
    print(f"       ISP:      {info.get('org', '?')}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test VPN proxy connectivity and DNS leaks"
    )
    parser.add_argument(
        "--proxies",
        nargs="+",
        metavar="URL",
        help="Proxy URLs to test (default: auto-detect from .env)",
    )
    args = parser.parse_args()

    if args.proxies:
        proxies = {
            f"proxy-{i + 1}": {"url": url, "container": None}
            for i, url in enumerate(args.proxies)
        }
    else:
        proxies = discover_proxies()

    passed = 0
    failed = 0

    def ok(msg: str) -> None:
        nonlocal passed
        passed += 1
        print(f"  PASS  {msg}")

    def fail(msg: str) -> None:
        nonlocal failed
        failed += 1
        print(f"  FAIL  {msg}")

    n = len(proxies)
    print("=" * 64)
    print(f"  VPN Proxy & DNS Leak Test ({n} proxy/proxies)")
    print("=" * 64)

    # --- 1. Home IP baseline ---
    print("\n[1/4] Home IP (direct connection)")
    home_ip = None
    try:
        async with httpx.AsyncClient() as client:
            home = await ip_info(client)
        home_ip = home["ip"]
        fmt_ip(home)
    except Exception as e:
        print(f"       Could not determine home IP: {e}")

    # --- 2. Container health + DNS config ---
    print("\n[2/4] Container health & DNS configuration")
    for name, cfg in proxies.items():
        container = cfg.get("container")
        region = cfg.get("region", "")
        label = f"{name} ({region})" if region else name

        if not container:
            print(f"       {label}: no container name, skipping Docker checks")
            continue

        healthy, status = container_health(container)
        if healthy:
            ok(f"{label}: container healthy")
        else:
            fail(f"{label}: container status = {status}")

        dns_ok, nameservers = container_dns(container)
        if dns_ok:
            ok(f"{label}: DNS -> Unbound (127.0.0.1, resolves via DoT through VPN)")
        else:
            fail(f"{label}: DNS nameservers = {nameservers} (expected 127.0.0.1)")

    # --- 3. Proxy exit IP + HKLII connectivity ---
    print("\n[3/4] Proxy exit IP & HKLII connectivity")
    for name, cfg in proxies.items():
        proxy_url = cfg["url"]
        region = cfg.get("region", "")
        label = f"{name} ({region})" if region else name
        print(f"\n  --- {label} @ {proxy_url} ---")
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=20) as client:
                info = await ip_info(client)
                vpn_ip = info["ip"]
                fmt_ip(info)

                if home_ip and vpn_ip != home_ip:
                    ok(f"{name}: exit IP differs from home IP")
                elif home_ip:
                    fail(f"{name}: exit IP MATCHES home — traffic not tunneled")
                else:
                    ok(f"{name}: exit IP = {vpn_ip} (home IP unknown)")

                hklii_ok, detail = await hklii_test(client)
                if hklii_ok:
                    ok(f"{name}: HKLII API reachable ({detail[:60]})")
                else:
                    fail(f"{name}: HKLII API failed ({detail})")
        except httpx.ProxyError:
            fail(f"{name}: proxy refused connection — is the container running?")
        except Exception as e:
            fail(f"{name}: {type(e).__name__}: {e}")

    # --- 4. Summary ---
    print(f"\n{'=' * 64}")
    print("[4/4] Results")
    print(f"{'=' * 64}")
    print(f"\n  Passed: {passed}")
    print(f"  Failed: {failed}")

    if failed == 0:
        print("\n  All automated checks passed.")
        print("\n  DNS leak analysis:")
        print("    httpx sends hostnames to the HTTP proxy (not resolved locally).")
        print("    Gluetun's Unbound resolves via DNS-over-TLS through the VPN.")
        print("    No DNS queries for target domains leave the host network.")
    else:
        print(f"\n  {failed} check(s) failed. See details above.")

    print("\n  Optional manual DNS leak verification:")
    print("  Terminal 1:  sudo tcpdump -i en0 -n port 53 | grep -i hklii")
    print("  Terminal 2:  uv run python scripts/test_vpn.py")
    print("  If no tcpdump output appears, DNS is confirmed not leaking.\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
