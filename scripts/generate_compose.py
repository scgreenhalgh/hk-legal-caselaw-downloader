#!/usr/bin/env python3
"""Generate docker-compose.yml from VPN_REGION_* entries in .env.

Reads .env for VPN_REGION_1, VPN_REGION_2, etc. and generates a
docker-compose.yml with one gluetun service per region, each exposing
an HTTP proxy on a sequential port starting at 8888.

Usage:
    uv run python scripts/generate_compose.py
"""

import re
import sys
from pathlib import Path

BASE_PORT = 8887

HEADER = """\
# Auto-generated — do not edit by hand.
# To update: edit VPN_REGION_* in .env, then run:
#   uv run python scripts/generate_compose.py
#
# Usage:
#   docker compose up -d         # start all VPNs
#   docker compose up -d vpn-1   # start just one for testing
#   uv run python scripts/test_vpn.py   # verify connectivity + DNS
#
# To use WireGuard instead of OpenVPN, see the README or generate configs
# with PIA's manual-connections scripts (github.com/pia-foss/manual-connections).

x-vpn-common: &vpn-common
  image: qmcgaw/gluetun:latest
  cap_add:
    - NET_ADMIN
  devices:
    - /dev/net/tun:/dev/net/tun
  healthcheck:
    test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:9999 || exit 1"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 30s
  restart: unless-stopped

x-vpn-env: &vpn-env
  VPN_SERVICE_PROVIDER: private internet access
  VPN_TYPE: openvpn
  OPENVPN_USER: ${PIA_USER}
  OPENVPN_PASSWORD: ${PIA_PASS}
  HTTPPROXY: "on"
  HTTPPROXY_LISTENING_ADDRESS: ":8888"
  HTTPPROXY_STEALTH: "on"

services:
"""

SERVICE_TEMPLATE = """\
  vpn-{i}:
    <<: *vpn-common
    container_name: hklii-vpn-{i}
    environment:
      <<: *vpn-env
      SERVER_REGIONS: ${{VPN_REGION_{i}}}
    ports:
      - "{port}:8888"
"""


def parse_regions(env_path: Path) -> dict[int, str]:
    regions = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'VPN_REGION_(\d+)\s*=\s*["\']?(.+?)["\']?\s*$', line)
        if m:
            regions[int(m.group(1))] = m.group(2)
    return regions


def main() -> int:
    env_path = Path(".env")
    if not env_path.exists():
        print("No .env found. Copy .env.example to .env and add your regions.")
        return 1

    regions = parse_regions(env_path)
    if not regions:
        print("No VPN_REGION_* entries found in .env")
        return 1

    services = []
    for i in sorted(regions.keys()):
        port = BASE_PORT + i
        services.append(SERVICE_TEMPLATE.format(i=i, port=port))

    compose = HEADER + "\n".join(services)
    Path("docker-compose.yml").write_text(compose)

    print(f"Generated docker-compose.yml with {len(regions)} VPN proxies:")
    for i in sorted(regions.keys()):
        print(f"  vpn-{i}: {regions[i]} -> http://localhost:{BASE_PORT + i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
