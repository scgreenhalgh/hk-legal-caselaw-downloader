"""Tests for scripts/expand_vpn_pool.py — dynamic PIA server pool generator.

Parses gluetun's `format-servers -private-internet-access` Markdown table
and generates a docker-compose.yml with one service per (region, server)
pinned via SERVER_NAMES for reproducibility.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import the module.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import expand_vpn_pool as evp


# Fixture mimicking gluetun's actual format-servers output shape.
GLUETUN_FIXTURE = """\
| Region | Hostname | Name | TCP | UDP | Port forwarding |
| --- | --- | --- | --- | --- | --- |
| Singapore | `singapore.pvt.site` | Server-1001-0a | ✅ | ✅ | ✅ |
| Singapore | `singapore.pvt.site` | Server-1002-0a | ✅ | ✅ | ✅ |
| Singapore | `singapore.pvt.site` | Server-1003-0a | ✅ | ✅ | ✅ |
| Singapore | `singapore.pvt.site` | Server-1004-0a | ✅ | ✅ | ✅ |
| Hong Kong | `hongkong.pvt.site` | Server-2001-0a | ✅ | ✅ | ✅ |
| Hong Kong | `hongkong.pvt.site` | Server-2002-0a | ✅ | ✅ | ✅ |
| Macao | `macao.pvt.site` | Server-9001-0a | ✅ | ✅ | ✅ |
| Macao | `macao.pvt.site` | Server-9002-0a | ✅ | ✅ | ✅ |
| Macao | `macao.pvt.site` | Server-9003-0a | ✅ | ✅ | ✅ |
| Macao | `macao.pvt.site` | Server-9004-0a | ✅ | ✅ | ✅ |
| US East | `us-east.pvt.site` | Server-3001-0a | ✅ | ❌ | ✅ |
| US East | `us-east.pvt.site` | Server-3002-0a | ✅ | ✅ | ❌ |
"""


class TestParseServers:
    def test_parses_all_data_rows(self):
        rows = evp.parse_servers(GLUETUN_FIXTURE)
        # 4 Singapore + 2 HK + 4 Macao + 2 US East = 12
        assert len(rows) == 12

    def test_parses_region_hostname_name(self):
        rows = evp.parse_servers(GLUETUN_FIXTURE)
        first = rows[0]
        assert first["region"] == "Singapore"
        assert first["hostname"] == "singapore.pvt.site"
        assert first["server_name"] == "Server-1001-0a"

    def test_parses_tcp_udp_port_forward_bools(self):
        rows = evp.parse_servers(GLUETUN_FIXTURE)
        # US East Server-3001 has TCP=✅, UDP=❌, PF=✅
        us = [r for r in rows if r["server_name"] == "Server-3001-0a"][0]
        assert us["tcp"] is True
        assert us["udp"] is False
        assert us["port_forward"] is True
        # US East Server-3002 has PF=❌
        us2 = [r for r in rows if r["server_name"] == "Server-3002-0a"][0]
        assert us2["port_forward"] is False

    def test_skips_header_row(self):
        rows = evp.parse_servers(GLUETUN_FIXTURE)
        assert not any(r["region"] == "Region" for r in rows)

    def test_skips_separator_row(self):
        rows = evp.parse_servers(GLUETUN_FIXTURE)
        assert not any(r["region"] == "---" for r in rows)

    def test_ignores_non_table_lines(self):
        with_noise = "some header line\n" + GLUETUN_FIXTURE + "trailing text\n"
        rows = evp.parse_servers(with_noise)
        assert len(rows) == 12


class TestPickServersPerRegion:
    def test_picks_default_count_per_region(self):
        servers = evp.parse_servers(GLUETUN_FIXTURE)
        picked = evp.pick_servers_per_region(
            servers, ["Singapore", "Hong Kong", "Macao"], per_region=3,
        )
        # 3 SG + 2 HK (only 2 available) + 3 Macao = 8
        assert len(picked) == 8
        by_region: dict[str, int] = {}
        for s in picked:
            by_region[s["region"]] = by_region.get(s["region"], 0) + 1
        assert by_region == {"Singapore": 3, "Hong Kong": 2, "Macao": 3}

    def test_all_mode_picks_every_server(self):
        servers = evp.parse_servers(GLUETUN_FIXTURE)
        picked = evp.pick_servers_per_region(
            servers, ["Singapore", "Hong Kong", "Macao"], per_region=None,
        )
        assert len(picked) == 4 + 2 + 4  # all Singapore + HK + Macao

    def test_unlisted_regions_return_no_servers(self):
        servers = evp.parse_servers(GLUETUN_FIXTURE)
        picked = evp.pick_servers_per_region(
            servers, ["Atlantis"], per_region=3,
        )
        assert picked == []

    def test_result_is_grouped_in_region_order(self):
        servers = evp.parse_servers(GLUETUN_FIXTURE)
        picked = evp.pick_servers_per_region(
            servers, ["Hong Kong", "Singapore"], per_region=2,
        )
        # Hong Kong first (2 rows), then Singapore (2 rows).
        assert [p["region"] for p in picked] == [
            "Hong Kong", "Hong Kong", "Singapore", "Singapore",
        ]


class TestRenderCompose:
    def test_service_count_matches_picked(self):
        picked = [
            {"region": "Singapore", "server_name": "Server-1001-0a"},
            {"region": "Singapore", "server_name": "Server-1002-0a"},
            {"region": "Hong Kong", "server_name": "Server-2001-0a"},
        ]
        out = evp.render_compose(picked)
        # 3 services: vpn-1, vpn-2, vpn-3
        assert out.count("container_name: hklii-vpn-") == 3

    def test_sequential_port_mapping_from_8888(self):
        picked = [
            {"region": "Singapore", "server_name": "Server-1001-0a"},
            {"region": "Singapore", "server_name": "Server-1002-0a"},
            {"region": "Hong Kong", "server_name": "Server-2001-0a"},
        ]
        out = evp.render_compose(picked)
        assert '"8888:8888"' in out
        assert '"8889:8888"' in out
        assert '"8890:8888"' in out

    def test_pins_server_names_per_service(self):
        picked = [
            {"region": "Singapore", "server_name": "Server-1001-0a"},
            {"region": "Hong Kong", "server_name": "Server-2001-0a"},
        ]
        out = evp.render_compose(picked)
        assert 'SERVER_NAMES: "Server-1001-0a"' in out
        assert 'SERVER_NAMES: "Server-2001-0a"' in out

    def test_includes_region_per_service(self):
        picked = [
            {"region": "JP Tokyo", "server_name": "Server-500-0a"},
        ]
        out = evp.render_compose(picked)
        assert 'SERVER_REGIONS: "JP Tokyo"' in out
        assert 'SERVER_NAMES: "Server-500-0a"' in out

    def test_output_starts_with_auto_generated_marker(self):
        out = evp.render_compose([{"region": "SG", "server_name": "s1"}])
        assert out.startswith("# Auto-generated")

    def test_includes_gluetun_image_and_healthcheck(self):
        out = evp.render_compose([{"region": "SG", "server_name": "s1"}])
        assert "qmcgaw/gluetun" in out
        assert "healthcheck:" in out
