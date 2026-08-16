"""compat-matrix.json — schema, status vocabulary and the honesty guarantees of SPEC §0.1/§10."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import SHARE, read_json  # noqa: E402

MATRIX_PATH = SHARE / "compat-matrix.json"

STATUSES = {"works", "native", "partial", "not_possible", "unknown"}
REQUIRED_KEYS = {"game", "status", "how", "anticheat", "reason", "link"}
TOP_LEVEL_KEYS = {"generated_note", "updated", "sources", "entries"}

# SPEC §10 minimum entries (name fragment, case-insensitive) → required status (None = any)
MANDATORY = {
    "valorant": "not_possible",
    "fortnite": "not_possible",
    "league of legends": "not_possible",
    "apex legends": "not_possible",
    "rainbow six siege": "not_possible",
    "destiny 2": "not_possible",
    "pubg": "not_possible",
    "grand theft auto online": "not_possible",
    "roblox": "works",
    "minecraft java": "native",
    "minecraft bedrock": "partial",
    "counter-strike 2": "native",
    "dota 2": "native",
    "elden ring": "works",
    "cyberpunk 2077": "works",
    "rocket league": "works",
    "genshin impact": "partial",
    "overwatch 2": "works",
    "sims 4": "partial",
    "forza horizon 5": "works",
    "hogwarts legacy": "works",
    "halo infinite": "works",
    "palworld": "works",
    "helldivers 2": "works",
    "marvel rivals": "works",
    "warframe": "works",
    "baldur's gate 3": "works",
    "stardew valley": "native",
    "terraria": "native",
    "among us": "works",
    "escape from tarkov": "not_possible",
    "call of duty": "not_possible",
    "delta force": "not_possible",
    "the finals": "works",
    "dead by daylight": "works",
    "battlefield 2042": "not_possible",
    "battlefield 6": "not_possible",
    "rust": "not_possible",
    "fall guys": None,
    "grand theft auto v": None,
}


def _find(entries, fragment: str):
    frag = fragment.casefold()
    exact = [e for e in entries if e["game"].casefold() == frag]
    if exact:
        return exact[0]
    # word-boundary containment ("rust" must not match "Rusty …")
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(frag) + r"(?![a-z0-9])")
    hits = [e for e in entries if pat.search(e["game"].casefold())]
    return hits[0] if hits else None


def test_file_exists_and_is_valid_json():
    assert MATRIX_PATH.is_file(), MATRIX_PATH
    raw = MATRIX_PATH.read_bytes()
    assert b"\r\n" not in raw, "LF line endings only"
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data, dict)
    assert TOP_LEVEL_KEYS <= set(data), f"missing top-level keys: {TOP_LEVEL_KEYS - set(data)}"


def test_top_level_metadata(compat_matrix):
    assert re.fullmatch(r"\d{4}-\d{2}", compat_matrix["updated"]), "updated must be YYYY-MM"
    assert compat_matrix["updated"] >= "2025-01"
    assert isinstance(compat_matrix["generated_note"], str) and len(compat_matrix["generated_note"]) > 40
    sources = compat_matrix["sources"]
    assert isinstance(sources, list) and sources
    for url in sources:
        assert url.startswith("https://"), url
    joined = " ".join(sources)
    assert "protondb.com" in joined and "areweanticheatyet.com" in joined


def test_entries_have_required_keys_and_valid_statuses(compat_matrix):
    entries = compat_matrix["entries"]
    assert isinstance(entries, list) and len(entries) >= 40
    seen = set()
    for e in entries:
        assert isinstance(e, dict), e
        missing = REQUIRED_KEYS - set(e)
        assert not missing, f"{e.get('game')}: missing {missing}"
        for k in REQUIRED_KEYS:
            assert isinstance(e[k], str), f"{e['game']}: {k} must be a string"
        assert e["game"].strip(), "empty game name"
        key = e["game"].casefold()
        assert key not in seen, f"duplicate entry: {e['game']}"
        seen.add(key)
        assert e["status"] in STATUSES, f"{e['game']}: bad status {e['status']!r}"
        assert e["how"].strip(), f"{e['game']}: 'how' empty"
        assert e["anticheat"].strip(), f"{e['game']}: 'anticheat' empty (use 'none')"
        assert len(e["reason"].strip()) >= 20, f"{e['game']}: reason too short"
        assert re.match(r"^https://[^\s]+$", e["link"]), f"{e['game']}: link must be an https URL"


def test_status_vocabulary_is_used_consistently(compat_matrix):
    entries = compat_matrix["entries"]
    by_status = {s: [e for e in entries if e["status"] == s] for s in STATUSES}
    # every not_possible entry names the anti-cheat and does not claim a launcher
    for e in by_status["not_possible"]:
        assert e["how"] in ("—", "-", "n/a", "none"), f"{e['game']}: not_possible must not advertise a launcher (how={e['how']!r})"
        assert e["anticheat"].lower() != "none" or "uwp" in e["reason"].lower(), \
            f"{e['game']}: not_possible needs the blocking anti-cheat named"
    # native entries never go through Proton/Wine
    for e in by_status["native"]:
        assert e["how"].lower().startswith("native"), f"{e['game']}: native entries use how=Native"
    assert by_status["works"] and by_status["native"] and by_status["partial"] and by_status["not_possible"]


@pytest.mark.parametrize("fragment,status", sorted(MANDATORY.items()))
def test_mandatory_games_present_with_status(compat_matrix, fragment, status):
    e = _find(compat_matrix["entries"], fragment)
    assert e is not None, f"mandatory game missing from compat matrix: {fragment}"
    if status is not None:
        assert e["status"] == status, f"{e['game']}: expected {status}, got {e['status']}"


def test_honesty_rules_spec_0_1(compat_matrix):
    entries = compat_matrix["entries"]
    valorant = _find(entries, "valorant")
    fortnite = _find(entries, "fortnite")
    roblox = _find(entries, "roblox")
    mc = _find(entries, "minecraft java")
    lol = _find(entries, "league of legends")
    assert valorant["status"] == "not_possible" and "vanguard" in valorant["anticheat"].lower()
    assert fortnite["status"] == "not_possible" and "epic" in fortnite["reason"].lower()
    assert roblox["status"] == "works" and roblox["how"] == "Sober"
    assert "windows client" in roblox["reason"].lower() or "windows player" in roblox["reason"].lower()
    assert mc["status"] == "native"
    assert lol["status"] == "not_possible" and "vanguard" in lol["anticheat"].lower()
    gta_online = _find(entries, "grand theft auto online")
    assert gta_online["status"] == "not_possible" and "battleye" in gta_online["anticheat"].lower()
    gta_story = _find(entries, "grand theft auto v (story mode)")
    assert gta_story is not None and gta_story["status"] == "works"


def test_generator_script_accepts_the_matrix(tmp_path: Path):
    """tests/gen-compat-doc.py (repo-level) must be able to render this file (SPEC §12)."""
    repo_root = SHARE.parents[5]        # share/lindos → share → usr → root → lindos-gaming → packages → repo
    gen = repo_root / "tests" / "gen-compat-doc.py"
    if not gen.is_file():
        pytest.skip("tests/gen-compat-doc.py not present in this checkout")
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_compat_doc", str(gen))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    entries = mod.load_matrix(str(MATRIX_PATH))
    assert len(entries) == len(read_json(MATRIX_PATH)["entries"])
    statuses = {e["status"] for e in entries}
    assert "unknown" not in statuses, "every entry must map to a recognised status in the generator"
    rendered = mod.render(entries)
    assert "Valorant" in rendered and "Not possible" in rendered
