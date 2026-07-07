"""Tests for ``viewer.courts.bcp47`` — BCP-47 language mapping filter.

Design §9 line 262: `bcp47(lang)` Jinja filter maps `'en' → 'en'`,
`'tc' → 'zh-Hant'`. Templates use `lang="{{ body_lang | bcp47 }}"` on
`<article>` (and, in later routes, on `<html>`). `body_lang` is derived
from **which file is being served** (route-level `served_body_lang`),
NOT from `case.lang` — a bilingual case with DB `lang='en'` served as
`.tc.html` must still render with `lang="zh-Hant"`.

Design §5 lines 120-121 treats legacy `'zh'` as an alias for `'tc'` in
the discriminator, so ``bcp47('zh')`` must also produce `'zh-Hant'` —
otherwise the CJK font stack (`:lang(zh-Hant)` in `app.css`) would miss
every case whose checkpoint row was written before the corpus rename to
`'tc'`.
"""

from __future__ import annotations

import pytest
from jinja2 import Environment


# ---------------------------------------------------------------------------
# Value assertions (four L2 semantic-drift pins)
# ---------------------------------------------------------------------------


def test_bcp47_en_maps_to_en() -> None:
    """English → 'en'. Identity — the baseline the fallback also lands
    on, so this pin distinguishes 'we handled en explicitly' from 'we
    fell through to the fallback'.
    """
    from hklii_downloader.viewer.courts import bcp47

    assert bcp47("en") == "en"


def test_bcp47_tc_maps_to_zh_Hant() -> None:
    """Traditional Chinese ('tc') → 'zh-Hant'. The CSS selector
    ``:lang(zh-Hant)`` in ``app.css`` (design §9) targets exactly this
    tag; if the mapping drifts, TC bodies render in the English serif
    stack, which is the bug §5 line 267 called out.
    """
    from hklii_downloader.viewer.courts import bcp47

    assert bcp47("tc") == "zh-Hant"


def test_bcp47_zh_legacy_alias_maps_to_zh_Hant() -> None:
    """Legacy 'zh' → 'zh-Hant'. Design §5 line 120-121 explicitly treats
    ``case.lang == 'zh'`` as equivalent to ``case.lang == 'tc'`` (the
    checkpoint's pre-rename Traditional-Chinese label). Without this
    alias every zh-tagged row would font-fall back to the English
    stack — an L2 semantic drift between the discriminator's alias
    handling and the template's lang emission.
    """
    from hklii_downloader.viewer.courts import bcp47

    assert bcp47("zh") == "zh-Hant"


def test_bcp47_unknown_code_falls_back_to_en() -> None:
    """Any input outside the {'en', 'tc', 'zh'} set → 'en' fallback.

    L5 ambiguous-state: templates must always emit a *valid* BCP-47 tag
    on the article — an empty ``lang=""`` or a raw ``lang="wut"`` would
    fail HTML validators and leave assistive tech guessing. The
    fallback pins the safe default. In practice the discriminator never
    passes non-canonical values, but the filter is defence in depth.
    """
    from hklii_downloader.viewer.courts import bcp47

    assert bcp47("unknown-code") == "en"


# ---------------------------------------------------------------------------
# Jinja integration (L4 wrong-side test — the filter has to work in a
# real template environment, not just as a plain call). Mirrors how
# ``app.create_app`` registers ``curial_roman`` / ``court_name`` /
# ``thousands`` on ``templates.env.filters``.
# ---------------------------------------------------------------------------


def test_bcp47_registers_as_jinja_filter_and_renders_zh_Hant() -> None:
    """Registered on a Jinja ``Environment`` under name ``bcp47``,
    ``{{ 'tc' | bcp47 }}`` must render as ``'zh-Hant'``. Locks the two
    surfaces the synthesis stage will wire together: the helper's
    callable signature and the template author's expected filter name.
    """
    from hklii_downloader.viewer.courts import bcp47

    env = Environment(autoescape=True)
    env.filters["bcp47"] = bcp47
    template = env.from_string("{{ 'tc' | bcp47 }}")
    assert template.render() == "zh-Hant"
