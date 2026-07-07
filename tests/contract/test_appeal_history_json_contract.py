"""On-disk contract test for the appeal_history.json sidecar files.

Contract pinned here (four axes, ranked by lens):

1. Canonical roundtrip — a JSON list of ``{act, judgments: [...]}`` at
   ``output/{court}/{year}/{court}_{year}_{n}.appeal_history.json`` is
   parsed verbatim by :func:`viewer.graph.appeal_chain`. L2/L3: pins
   both the top-level shape (list, not dict-wrapped) AND the filename
   template from the reader's docstring.

2. Absent file → ``[]`` (not ``FileNotFoundError``). Most corpus cases
   lack an appeal_history sidecar; absence is a legitimate "no chain"
   answer, distinct from a raise (L5 ambiguous-state).

3. Malformed JSON → ``json.JSONDecodeError`` propagates (Phase 3 fix,
   L1 silent-skip lens): partial writes / disk truncation are real
   data issues; the reader must not silently return ``[]``.

4. Path-traversal via a case_key crafted with ``..`` → ``ValueError``
   (recent Tier-3 hardening). Each component is regex-validated at
   the input surface before Path composition, so a crafted key like
   ``../../etc/passwd/x`` never lands on disk lookup.

This file complements the helper-tier tests in
``tests/test_viewer_graph.py`` by asserting against the on-disk file
shape end-to-end rather than only the reader's internal branches — the
downloader can evolve its emitter and this contract stays honest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hklii_downloader.viewer.graph import appeal_chain


# Canonical shape mirrors what the downloader writes for real corpus
# cases (see hklii_downloader/enrichment.py appeal_history capture):
# a top-level JSON list of {act: str, judgments: [{...}, ...]} dicts.
_CANONICAL_CHAIN = [
    {
        "act": "CACC124/2013",
        "judgments": [
            {
                "neutral": "[2013] HKCA 533",
                "date": "2013-10-07",
                "remarks": "",
                "path": "/en/cases/hkca/2013/533",
                "lang": "EN",
            }
        ],
    },
    {
        "act": "DCCC860/2012",
        "judgments": [
            {
                "neutral": "[2013] HKDC 352",
                "date": "2013-03-12",
                "remarks": "",
                "path": "/en/cases/hkdc/2013/352",
                "lang": "EN",
            }
        ],
    },
]


def _write_sidecar_raw(output_root: Path, case_key: str, payload: str) -> Path:
    """Emit *raw text* at the canonical sidecar path.

    Writes a verbatim string (not ``json.dumps``) so callers can seed
    valid and malformed JSON through the same helper — the contract
    test for "malformed JSON propagates" needs to write intentionally
    broken payloads that a json.dumps-only helper would refuse.
    """
    court, year, num = case_key.split("/", 2)
    dst = (
        output_root
        / court
        / year
        / f"{court}_{year}_{num}.appeal_history.json"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(payload, encoding="utf-8")
    return dst


# -----------------------------------------------------------------------
# Contract 1 — canonical file roundtrips through the reader unchanged.
# -----------------------------------------------------------------------


def test_canonical_sidecar_roundtrips_verbatim(tmp_path: Path) -> None:
    """A valid JSON sidecar at the canonical path is read back exactly.

    Assertions pin three separable facets of the on-disk contract:
      • filename template — ``{court}_{year}_{n}.appeal_history.json``
        (L3: the reader's docstring names this shape; drift would let
        the downloader rename the sidecar without the viewer noticing)
      • top-level JSON is a list, not a dict-wrapped envelope
        (L2: an easy semantic-drift target if the emitter ever wraps
        in ``{"chain": [...]}``)
      • the list's element shape has ``act`` + ``judgments`` keys
    """
    dst = _write_sidecar_raw(
        tmp_path, "hkdc/2013/352", json.dumps(_CANONICAL_CHAIN)
    )
    # On-disk filename: exact template from viewer.graph docstring.
    assert dst.name == "hkdc_2013_352.appeal_history.json"
    # Parent directory: /{court}/{year}/ — the FLAT layout the
    # downloader writes (see docs/viewer-design.md §5).
    assert dst.parent.name == "2013"
    assert dst.parent.parent.name == "hkdc"

    result = appeal_chain(tmp_path, "hkdc/2013/352")

    # Verbatim roundtrip — every key/value survives.
    assert result == _CANONICAL_CHAIN
    # Shape: list-of-dicts, not dict-envelope.
    assert isinstance(result, list)
    assert len(result) == 2
    # Element shape: the two canonical keys are present.
    assert set(result[0].keys()) >= {"act", "judgments"}
    assert isinstance(result[0]["judgments"], list)


# -----------------------------------------------------------------------
# Contract 2 — absent sidecar returns [], NOT FileNotFoundError.
# -----------------------------------------------------------------------


def test_absent_sidecar_returns_empty_list_not_error(tmp_path: Path) -> None:
    """No sidecar on disk → the reader returns ``[]``.

    L5 ambiguous-state: "no chain" is a legitimate answer for the vast
    majority of corpus cases. A raise would force every route rendering
    an appeal strip to wrap the call in try/except, and the wrap would
    inevitably drift into silently swallowing genuine JSON errors too.
    Pinning "absent → []" as a contract keeps that hazard closed.
    """
    # tmp_path exists but no sidecar has been written for this key.
    result = appeal_chain(tmp_path, "hkcfa/2020/1")

    assert result == []
    # L2: return type stays a list even in the empty case — callers
    # can safely ``for entry in appeal_chain(...)`` without None-check.
    assert isinstance(result, list)


# -----------------------------------------------------------------------
# Contract 3 — malformed JSON propagates, no silent-skip.
# -----------------------------------------------------------------------


def test_malformed_json_sidecar_propagates_decode_error(
    tmp_path: Path,
) -> None:
    """A sidecar with invalid JSON must SURFACE ``json.JSONDecodeError``.

    L1 silent-skip lens: real data corruption (partial write mid-flush,
    disk truncation, downloader crash between open() and dump()) is not
    the same signal as "case has no chain". The Phase 3 fix pins that
    distinction — if either was silently masked, every viewer render
    would paper over corruption and the operator would never notice.
    """
    _write_sidecar_raw(tmp_path, "hkcfa/2020/1", "not valid json {")

    with pytest.raises(json.JSONDecodeError):
        appeal_chain(tmp_path, "hkcfa/2020/1")


# -----------------------------------------------------------------------
# Contract 4 — path-traversal via case_key raises ValueError (Tier-3).
# -----------------------------------------------------------------------


class TestPathTraversalRejected:
    """Tier-3 hardening — case_key components originate from a URL at
    the future ``/case/{court}/{year}/{n}/appeal-chain`` route. Without
    the regex validators recently added to :func:`appeal_chain`, a key
    crafted as ``../../etc/passwd/x`` composes an on-disk path OUTSIDE
    ``output_root`` (``court='..', year='..', num='etc'`` resolves via
    Path join to ``output_root/../../etc/...appeal_history.json``).

    These assertions pin the input-surface defence: each component is
    validated *before* Path composition, so no traversal payload ever
    touches the filesystem lookup.
    """

    def test_dotdot_in_court_component_rejected(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError):
            appeal_chain(tmp_path, "../evil/2020/1")

    def test_dotdot_in_year_component_rejected(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError):
            appeal_chain(tmp_path, "hkcfa/../1")

    def test_dotdot_in_number_component_rejected(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError):
            appeal_chain(tmp_path, "hkcfa/2020/..")

    def test_absolute_path_component_rejected(
        self, tmp_path: Path
    ) -> None:
        """Empty-string court from a leading slash ('/etc/2020/1' →
        parts[0] == '') fails the ``^[a-z]+$`` validator. L4 wrong-side
        test: even if the caller URL-parser trims the leading slash,
        the reader still guards its own inputs.
        """
        with pytest.raises(ValueError):
            appeal_chain(tmp_path, "/etc/2020/1")
