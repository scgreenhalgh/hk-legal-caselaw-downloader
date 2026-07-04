"""Content-shape safeguards shared across the download + enrichment paths.

Lives in its own module so both scraper.py and enrichment.py can import the
challenge-page detector without forming an import cycle (scraper.py imports
enrichment.py at top level; enrichment.py previously reached back into
scraper.py via a lazy import — see B5 in scratchpad/REVIEW_VERDICT.md).
"""
from __future__ import annotations

_CHALLENGE_MARKERS = (
    # English — Cloudflare / generic WAF / rate-limit interstitials.
    "just a moment",
    "cf-challenge",
    "cloudflare",
    "please enable javascript",
    "verify you are human",
    "access denied",
    "too many requests",
    # Traditional Chinese — HKLII serves bilingual content, any localized
    # challenge would slip past an English-only denylist.
    "請稍候",
    "驗證您是人類",
    "請啟用 JavaScript",
    "訪問受限",
    "系統維護",
    "拒絕存取",
)


def _looks_like_challenge_page(content_html: str) -> bool:
    """True if the HTML looks like a WAF/challenge/error interstitial.

    ASCII markers matched case-insensitively; CJK markers matched exactly
    (Python str.lower() is a no-op on CJK characters).
    """
    if not content_html:
        return False
    haystack = content_html.lower()
    return any(marker.lower() in haystack for marker in _CHALLENGE_MARKERS)
