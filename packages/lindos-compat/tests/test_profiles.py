"""Per-title profile loading, resolution and user override (SPEC-KERNEL §17.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lindos_compat import profiles

# The per-title starter set is shipped by lindos-gaming (single owner: it also owns
# compat-matrix.json and its lindos-gamescope tests depend on the exact geometry).
# lindos-compat provides the loader/consumer and validates it against that one copy.
SHIPPED = (Path(__file__).resolve().parents[2] / "lindos-gaming" / "root" / "usr" / "share"
           / "lindos" / "gaming" / "profiles")


# --------------------------------------------------------------------------- #
# shipped starter set
# --------------------------------------------------------------------------- #
def test_shipped_profiles_are_valid_schema1():
    files = sorted(SHIPPED.glob("*.json"))
    assert files, "no starter profiles shipped"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("schema") == 1
        prof = profiles.profile_from_dict(data, fallback_id=path.stem)
        assert prof is not None and prof.status in profiles.STATUSES
        if not prof.possible:
            # a not_possible profile must fake nothing: no runner, no env
            assert prof.runner == "" and prof.env == {}, path.name
            assert prof.notes, path.name


def test_load_and_resolve_shipped(monkeypatch, tmp_path):
    monkeypatch.setenv("LINDOS_PROFILES_DIR", str(SHIPPED))
    monkeypatch.setenv("LINDOS_USER_PROFILES_DIR", str(tmp_path / "empty"))
    profs = profiles.load_profiles()
    assert {"elden-ring", "cyberpunk-2077", "valorant"} <= set(profs)
    # by exe name (case-insensitive)
    assert profiles.resolve_profile(exe="EldenRing.exe", profiles=profs).id == "elden-ring"
    assert profiles.resolve_profile(exe="/games/Cyberpunk2077.exe", profiles=profs).id == "cyberpunk-2077"
    # by steam appid (appid wins over exe)
    assert profiles.resolve_profile(appid="1091500", profiles=profs).id == "cyberpunk-2077"
    assert profiles.resolve_profile(appid=1245620, profiles=profs).id == "elden-ring"
    # not_possible profile resolves but refuses to fake anything
    val = profiles.resolve_profile(exe="VALORANT.exe", profiles=profs)
    assert val.status == "not_possible" and not val.possible
    # unmatched
    assert profiles.resolve_profile(exe="unknown.exe", profiles=profs) is None


# --------------------------------------------------------------------------- #
# user override
# --------------------------------------------------------------------------- #
def test_user_profile_overrides_shipped(monkeypatch, tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "elden-ring.json").write_text(json.dumps({
        "schema": 1, "id": "elden-ring", "title": "Elden Ring (mine)", "runner": "wine",
        "match": {"exe": ["eldenring.exe"]}, "status": "works",
    }), encoding="utf-8")
    monkeypatch.setenv("LINDOS_PROFILES_DIR", str(SHIPPED))
    monkeypatch.setenv("LINDOS_USER_PROFILES_DIR", str(user))
    profs = profiles.load_profiles()
    prof = profs["elden-ring"]
    assert prof.source == "user" and prof.runner == "wine" and prof.title == "Elden Ring (mine)"
    # other shipped profiles are still present
    assert profs["cyberpunk-2077"].source == "system"


# --------------------------------------------------------------------------- #
# parsing edge cases
# --------------------------------------------------------------------------- #
def test_profile_from_dict_rejects_bad_schema_and_id():
    assert profiles.profile_from_dict({"schema": 2, "id": "x"}) is None
    assert profiles.profile_from_dict({"schema": 1}) is None            # no id, no fallback
    assert profiles.profile_from_dict("nope") is None
    prof = profiles.profile_from_dict({"schema": 1, "match": {"exe": ["A.EXE"]},
                                       "dxvk_async": "true", "mangohud": "no", "status": "bogus"},
                                      fallback_id="fromfile")
    assert prof.id == "fromfile" and prof.match_exe == ["a.exe"]
    assert prof.dxvk_async is True and prof.mangohud is False and prof.status == "works"


def test_gamescope_from_profile():
    prof = profiles.Profile(id="x", gamescope={"w": 2560, "h": 1440, "hdr": True, "fsr": False})
    spec = profiles.gamescope_from_profile(prof)
    assert spec.enabled and spec.width == 2560 and spec.height == 1440 and spec.hdr and not spec.fsr
    assert not profiles.gamescope_from_profile(profiles.Profile(id="y")).enabled


def test_load_profiles_ignores_broken_files(monkeypatch, tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "good.json").write_text('{"schema":1,"id":"good","match":{"exe":["g.exe"]}}', encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LINDOS_PROFILES_DIR", str(d))
    monkeypatch.setenv("LINDOS_USER_PROFILES_DIR", str(tmp_path / "none"))
    profs = profiles.load_profiles()
    assert set(profs) == {"good"}
