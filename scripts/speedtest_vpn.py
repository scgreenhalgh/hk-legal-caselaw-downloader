#!/usr/bin/env python3
"""
Speed test for VPN proxy endpoints against HKLII.

Measures latency and download throughput through each proxy.

Usage:
    uv run python scripts/speedtest_vpn.py
    uv run python scripts/speedtest_vpn.py --samples 10 --downloads 20
    uv run python scripts/speedtest_vpn.py --proxies http://localhost:8888
"""

import argparse
import asyncio
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

BASE_PORT = 8887
DEFAULT_LATENCY_SAMPLES = 5
DEFAULT_DOWNLOADS = 10
LATENCY_GAP = 0.5
DOWNLOAD_GAP = 0.1

HKLII_API = "https://www.hklii.hk/api/getjudgment"

LATENCY_CASES = [
    {"lang": "en", "abbr": "hkcfa", "year": "2023", "num": "32"},
    {"lang": "en", "abbr": "hkcfa", "year": "2022", "num": "20"},
    {"lang": "en", "abbr": "hkcfa", "year": "2021", "num": "15"},
    {"lang": "en", "abbr": "hkcfa", "year": "2024", "num": "10"},
    {"lang": "en", "abbr": "hkcfa", "year": "2020", "num": "25"},
    {"lang": "en", "abbr": "hkcfa", "year": "2019", "num": "30"},
    {"lang": "en", "abbr": "hkcfa", "year": "2023", "num": "10"},
    {"lang": "en", "abbr": "hkcfa", "year": "2022", "num": "15"},
    {"lang": "en", "abbr": "hkcfa", "year": "2021", "num": "25"},
    {"lang": "en", "abbr": "hkcfa", "year": "2024", "num": "5"},
]

THROUGHPUT_CASES = [
    {"lang": "en", "abbr": "hkcfi", "year": "2024", "num": str(n)}
    for n in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
              1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000]
]


def discover_proxies() -> dict[str, dict]:
    env_path = Path(".env")
    if not env_path.exists():
        return {
            f"vpn-{i}": {
                "url": f"http://localhost:{BASE_PORT + i}",
                "region": "",
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
                "region": region,
            }

    return proxies or {
        f"vpn-{i}": {"url": f"http://localhost:{BASE_PORT + i}", "region": ""}
        for i in range(1, 4)
    }


async def measure_latency(
    client: httpx.AsyncClient, samples: int, label: str
) -> dict | None:
    times_ms = []
    sizes = []

    for i in range(samples):
        params = LATENCY_CASES[i % len(LATENCY_CASES)]
        try:
            start = time.perf_counter()
            resp = await client.get(HKLII_API, params=params, timeout=20)
            elapsed_ms = (time.perf_counter() - start) * 1000
            times_ms.append(elapsed_ms)
            sizes.append(len(resp.content))
        except Exception as e:
            print(f"\n    {label} sample {i + 1}: {e}")

        if i < samples - 1:
            await asyncio.sleep(LATENCY_GAP)

    if not times_ms:
        return None

    return {
        "median": statistics.median(times_ms),
        "min": min(times_ms),
        "max": max(times_ms),
        "stdev": statistics.stdev(times_ms) if len(times_ms) > 1 else 0,
        "avg_size_kb": statistics.mean(sizes) / 1024,
        "ok": len(times_ms),
    }


async def measure_throughput(
    client: httpx.AsyncClient, downloads: int, label: str
) -> dict | None:
    total_bytes = 0
    successful = 0
    individual_kbps = []

    for i in range(downloads):
        params = THROUGHPUT_CASES[i % len(THROUGHPUT_CASES)]
        try:
            start = time.perf_counter()
            resp = await client.get(HKLII_API, params=params, timeout=30)
            elapsed = time.perf_counter() - start

            if resp.status_code == 200 and len(resp.content) > 100:
                size = len(resp.content)
                total_bytes += size
                successful += 1
                individual_kbps.append((size / 1024) / elapsed)
        except Exception as e:
            print(f"\n    {label} download {i + 1}: {e}")

        if i < downloads - 1:
            await asyncio.sleep(DOWNLOAD_GAP)

    if not individual_kbps:
        return None

    return {
        "total_kb": total_bytes / 1024,
        "successful": successful,
        "median_kbps": statistics.median(individual_kbps),
        "min_kbps": min(individual_kbps),
        "max_kbps": max(individual_kbps),
        "avg_doc_kb": (total_bytes / successful / 1024) if successful else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Speed test VPN proxies against HKLII"
    )
    parser.add_argument(
        "--proxies", nargs="+", metavar="URL",
        help="Proxy URLs to test (default: auto-detect from .env)",
    )
    parser.add_argument(
        "--samples", type=int, default=DEFAULT_LATENCY_SAMPLES,
        help=f"Latency samples per endpoint (default: {DEFAULT_LATENCY_SAMPLES})",
    )
    parser.add_argument(
        "--downloads", type=int, default=DEFAULT_DOWNLOADS,
        help=f"Downloads per endpoint for throughput (default: {DEFAULT_DOWNLOADS})",
    )
    args = parser.parse_args()

    if args.proxies:
        endpoints = {"direct": {"url": None, "region": ""}} | {
            f"proxy-{i + 1}": {"url": url, "region": ""}
            for i, url in enumerate(args.proxies)
        }
    else:
        endpoints = {"direct": {"url": None, "region": ""}} | discover_proxies()

    samples = args.samples
    downloads = args.downloads
    total_requests = len(endpoints) * (samples + downloads)

    print("=" * 76)
    print(f"  HKLII Speed Test")
    print(f"  {len(endpoints)} endpoints, {samples} latency samples + {downloads} downloads each")
    print(f"  {total_requests} total requests")
    print("=" * 76)

    # --- Phase 1: Latency ---
    print(f"\n  Phase 1: Latency ({LATENCY_GAP}s gap)")
    print(f"  {'─' * 40}")

    latency_results = {}

    for name, cfg in endpoints.items():
        proxy_url = cfg.get("url")
        region = cfg.get("region", "")
        label = f"{name} ({region})" if region else name

        print(f"  {label}...", end="", flush=True)

        try:
            kwargs: dict = {"timeout": 20}
            if proxy_url:
                kwargs["proxy"] = proxy_url

            async with httpx.AsyncClient(**kwargs) as client:
                stats = await measure_latency(client, samples, label)

            if stats:
                print(f" {stats['median']:.0f}ms median ({stats['ok']}/{samples} ok)")
                latency_results[name] = stats
            else:
                print(" FAILED")
        except httpx.ProxyError:
            print(" FAILED — proxy refused")
        except Exception as e:
            print(f" FAILED — {e}")

    # --- Phase 2: Download throughput ---
    print(f"\n  Phase 2: Download speed ({DOWNLOAD_GAP}s gap, {downloads} files each)")
    print(f"  {'─' * 40}")

    throughput_results = {}

    for name, cfg in endpoints.items():
        proxy_url = cfg.get("url")
        region = cfg.get("region", "")
        label = f"{name} ({region})" if region else name

        print(f"  {label}...", end="", flush=True)

        try:
            kwargs: dict = {"timeout": 30}
            if proxy_url:
                kwargs["proxy"] = proxy_url

            async with httpx.AsyncClient(**kwargs) as client:
                stats = await measure_throughput(client, downloads, label)

            if stats:
                print(
                    f" {stats['median_kbps']:.0f} KB/s median"
                    f" ({stats['successful']}/{downloads} ok,"
                    f" {stats['total_kb']:.0f} KB total)"
                )
                throughput_results[name] = stats
            else:
                print(" FAILED")
        except httpx.ProxyError:
            print(" FAILED — proxy refused")
        except Exception as e:
            print(f" FAILED — {e}")

    # --- Combined results table ---
    all_names = list(dict.fromkeys(
        list(latency_results.keys()) + list(throughput_results.keys())
    ))

    combined = []
    for name in all_names:
        cfg = endpoints.get(name, {})
        entry = {
            "name": name,
            "region": cfg.get("region", ""),
            "lat_median": latency_results.get(name, {}).get("median"),
            "lat_min": latency_results.get(name, {}).get("min"),
            "dl_median_kbps": throughput_results.get(name, {}).get("median_kbps"),
            "dl_min_kbps": throughput_results.get(name, {}).get("min_kbps"),
            "dl_max_kbps": throughput_results.get(name, {}).get("max_kbps"),
            "avg_doc_kb": throughput_results.get(name, {}).get("avg_doc_kb"),
        }
        combined.append(entry)

    combined.sort(key=lambda r: r.get("dl_median_kbps") or 0, reverse=True)

    print(f"\n{'=' * 76}")
    print("  Results (ranked by download speed)")
    print(f"{'=' * 76}")
    print(
        f"  {'#':<4} {'Proxy':<10} {'Region':<16}"
        f" {'Latency':>9} {'DL Med':>9} {'DL Min':>9} {'DL Max':>9} {'Doc':>7}"
    )
    print(
        f"  {'─' * 4} {'─' * 10} {'─' * 16}"
        f" {'─' * 9} {'─' * 9} {'─' * 9} {'─' * 9} {'─' * 7}"
    )

    for i, r in enumerate(combined, 1):
        lat = f"{r['lat_median']:.0f}ms" if r["lat_median"] else "—"
        dl_med = f"{r['dl_median_kbps']:.0f} KB/s" if r["dl_median_kbps"] else "—"
        dl_min = f"{r['dl_min_kbps']:.0f} KB/s" if r["dl_min_kbps"] else "—"
        dl_max = f"{r['dl_max_kbps']:.0f} KB/s" if r["dl_max_kbps"] else "—"
        doc = f"{r['avg_doc_kb']:.0f}KB" if r["avg_doc_kb"] else "—"
        print(
            f"  {i:<4} {r['name']:<10} {r['region']:<16}"
            f" {lat:>9} {dl_med:>9} {dl_min:>9} {dl_max:>9} {doc:>7}"
        )

    if combined:
        fastest = combined[0]
        print(
            f"\n  Fastest download: {fastest['name']} ({fastest['region']})"
            f" — {fastest['dl_median_kbps']:.0f} KB/s median"
        )
        if fastest["avg_doc_kb"]:
            est_time_h = (122460 * fastest["avg_doc_kb"]) / (fastest["dl_median_kbps"] * 3600)
            print(
                f"  Estimated full corpus ({fastest['avg_doc_kb']:.0f}KB avg):"
                f" {est_time_h:.1f}h raw, longer with rate limiting"
            )

    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
