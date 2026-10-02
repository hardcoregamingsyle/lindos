"""The accent swatches of Lindos Setup carry Lindos names (no accent is called after the base distribution)."""
from __future__ import annotations

import os

from lindos_setup import plan

HERE = os.path.dirname(os.path.abspath(__file__))
ACCENTS_JSON = os.path.join(HERE, "..", "root", "usr", "share", "lindos", "setup", "accents.json")


def test_accents_json_and_the_built_in_fallback_agree_and_none_says_mint() -> None:
    from_file = plan.load_accents(ACCENTS_JSON)
    assert from_file == plan.FALLBACK_ACCENTS, "the fallback must be a copy of accents.json"
    for accent in from_file:
        assert "mint" not in (accent["name"] + accent["id"]).lower(), accent
    assert "Meadow Green" in [a["name"] for a in from_file]
    assert len({a["hex"] for a in from_file}) == 8 and len({a["id"] for a in from_file}) == 8


def test_a_missing_accents_file_falls_back_to_the_lindos_names() -> None:
    fb = plan.load_accents("/definitely/not/here.json")
    assert [a["name"] for a in fb] == [a["name"] for a in plan.FALLBACK_ACCENTS]
    assert all("mint" not in a["name"].lower() for a in fb)
