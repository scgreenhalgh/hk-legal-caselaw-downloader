"""Executable checks on the research/ docs.

Chapter 13's `jq` recipes drive post-run WAF triage — they run against the
`failure_samples/*.headers.json` files that `events.py:sample_failure` writes
from `resp.headers.items()`. Both httpx and curl_cffi normalise header names
to lowercase at `.items()`, so a title-case jq path silently returns null on
a real capture and misses signals the runbook depends on (e.g. Cloudflare
onset for Chapter 01's baseline revision).

These tests keep the recipes honest against the data shape production ships.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RESEARCH = _REPO_ROOT / "research"
_CH13 = _RESEARCH / "13-observability.md"


def _extract_recipe_5a_jq_expression() -> str:
    """Return the jq expression from Recipe 5a of Chapter 13 verbatim.

    Recipe 5a is 'Server-header (or content-type) distribution across all
    samples' — the first `jq -r '...'` line whose expression touches
    `.headers`. Extracting from the file (rather than hard-coding) means the
    test tracks whatever the doc actually recommends.
    """
    for line in _CH13.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        m = re.match(r"jq -r '([^']*\.headers[^']*)'", stripped)
        if m:
            return m.group(1)
    raise AssertionError(
        f"No jq recipe touching .headers found in {_CH13}"
    )


class TestRecipe5aServerHeader:
    def test_recipe_5a_matches_lowercase_server_header(
        self, tmp_path: Path
    ) -> None:
        """Recipe 5a must surface a lowercase 'server' key.

        scraper.py:_response_headers iterates `resp.headers.items()`;
        events.py:sample_failure serialises with `dict(headers)`. httpx and
        curl_cffi both normalise header names to lowercase at `.items()`, so
        a real failure_samples/*.headers.json ships lowercase keys. A
        title-case jq path returns null and Recipe 5a silently falls through
        to content-type — missing the 'Cloudflare turned on' signal.
        """
        if shutil.which("jq") is None:
            pytest.skip("jq binary not on PATH")

        fixture = tmp_path / "challenge_hkcfi_2023_3.headers.json"
        fixture.write_text(
            json.dumps(
                {
                    "signature": "challenge_hkcfi_2023_3",
                    "captured_at": "2026-07-04T06:45:28.204856+00:00",
                    "is_challenge": True,
                    "truncated": False,
                    "body_bytes": 240,
                    "headers": {
                        "server": "cloudflare",
                        "cf-ray": "8a1234b56cde-HKG",
                        "set-cookie": "cf_clearance=abc; Path=/; HttpOnly",
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        expr = _extract_recipe_5a_jq_expression()
        result = subprocess.run(
            ["jq", "-r", expr, str(fixture)],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, (
            f"jq exited {result.returncode} with stderr={result.stderr!r}"
        )
        assert "cloudflare" in result.stdout, (
            f"Recipe 5a expression {expr!r} did not surface the lowercase "
            f"'server' key that events.py writes. jq stdout: "
            f"{result.stdout!r}. Production headers are lowercase per httpx / "
            f"curl_cffi .items() normalisation."
        )
