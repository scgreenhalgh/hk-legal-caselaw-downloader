"""Content-shape safeguards shared across the download + enrichment paths.

Lives in its own module so both scraper.py and enrichment.py can import the
challenge-page detector without forming an import cycle (scraper.py imports
enrichment.py at top level; enrichment.py previously reached back into
scraper.py via a lazy import — see B5 in scratchpad/REVIEW_VERDICT.md).
"""
from __future__ import annotations

_CHALLENGE_MARKERS = (
    # English — Cloudflare / generic WAF / rate-limit interstitials.
    # NB: the leading "just a moment..." marker requires the three-dot
    # ellipsis to match Cloudflare's actual `<title>Just a moment...</title>`
    # — without it, organic judgment text ("just a moment of anger",
    # witness "just a moment.") false-positives (36 rows lost in the
    # 2026-07-04 run before this tightening — task #63).
    # "access denied" and "too many requests" were dropped in task #66:
    # both are common legal English (access to medical care / custody /
    # premises being denied; "one too many requests for money" quoted in
    # cross-examination). HKLII is confirmed gunicorn/Apache with no WAF
    # (memory/hklii-waf-status.md), so we lean on the CF-specific brand
    # markers below and accept the risk of missing a hypothetical non-CF
    # WAF that greets us with only these bare phrases.
    "just a moment...",
    "cf-challenge",
    "cloudflare",
    "please enable javascript",
    "verify you are human",
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
