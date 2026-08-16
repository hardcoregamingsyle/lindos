"""Per-title profile data validity + honesty (SPEC-KERNEL §17.3 / §14)."""
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

PROFILES = SHARE / "gaming" / "profiles"
MATRIX = SHARE / "compat-matrix.json"

STATUSES = {"works", "partial", "native", "not_possible"}
RUNNERS = {"umu", "wine"}
ID_RE = re.compile(r"^[a-z0-9-]+$")

# env keys a profile is allowed to set: performance/compat only, never anything that
# claims to defeat/spoof anti-cheat, attestation or DRM (SPEC-KERNEL §14).
ALLOWED_ENV_PREFIXES = ("PROTON_", "DXVK_", "VKD3D_", "WINE", "MANGOHUD", "DXVK", "PULSE_",
                        "SDL_", "ENABLE_", "OBS_", "__GL_", "__NV_", "MESA_", "RADV_", "DXIL_")
FORBIDDEN_ENV_TOKENS = ("attest", "vanguard", "tpm", "secureboot", "secure_boot",
                        "hwid", "patchguard", "hvci", "spoof", "bypass", "faketpm")


def _profile_paths():
    return sorted(PROFILES.glob("*.json"))


def test_profiles_dir_has_starter_set():
    paths = _profile_paths()
    assert paths, "no per-title profiles shipped"
    data = [read_json(p) for p in paths]
    statuses = [d["status"] for d in data]
    assert statuses.count("works") >= 2, "need at least a couple of 'works' starter profiles"
    assert "not_possible" in statuses, "need one honest 'not_possible' example"


@pytest.mark.parametrize("path", _profile_paths(), ids=lambda p: p.name)
def test_profile_schema(path: Path):
    raw = path.read_bytes()
    assert b"\r\n" not in raw, "LF only"
    d = json.loads(raw.decode("utf-8"))
    assert isinstance(d, dict)
    assert d.get("schema") == 1
    assert ID_RE.match(d["id"]), d["id"]
    assert path.stem == d["id"], f"filename must match id: {path.name} != {d['id']}"
    assert isinstance(d["title"], str) and d["title"].strip()
    assert d["status"] in STATUSES, d["status"]

    match = d["match"]
    assert isinstance(match, dict)
    exe = match.get("exe", [])
    appids = match.get("steam_appid", [])
    assert isinstance(exe, list) and all(isinstance(x, str) and x for x in exe)
    assert isinstance(appids, list) and all(isinstance(x, int) for x in appids)
    assert exe or appids, f"{d['id']}: profile must be matchable by exe or steam_appid"

    if "runner" in d:
        assert d["runner"] in RUNNERS, d["runner"]
    if "proton" in d:
        assert isinstance(d["proton"], str) and d["proton"].strip()
    if "gamescope" in d:
        gs = d["gamescope"]
        assert isinstance(gs, dict)
        for k in ("w", "h"):
            if k in gs:
                assert isinstance(gs[k], int) and gs[k] > 0
        for k in ("fsr", "hdr"):
            if k in gs:
                assert isinstance(gs[k], bool)
    for k in ("dxvk_async", "mangohud"):
        if k in d:
            assert isinstance(d[k], bool)
    assert isinstance(d["notes"], str) and len(d["notes"].strip()) >= 20

    env = d.get("env", {})
    assert isinstance(env, dict)
    for k, v in env.items():
        assert isinstance(k, str) and isinstance(v, str), f"{d['id']}: env values must be strings"
        assert k.startswith(ALLOWED_ENV_PREFIXES), f"{d['id']}: env key {k!r} not an allowed performance/compat var"
        blob = (k + "=" + v).lower()
        for tok in FORBIDDEN_ENV_TOKENS:
            assert tok not in blob, f"{d['id']}: env {k} looks like an anti-cheat evasion ({tok})"


def test_ids_unique():
    ids = [read_json(p)["id"] for p in _profile_paths()]
    assert len(ids) == len(set(ids)), "duplicate profile ids"


def test_not_possible_profile_fakes_nothing():
    """The 'not_possible' example documents the block and enables no bypass (SPEC-KERNEL §14)."""
    nps = [read_json(p) for p in _profile_paths() if read_json(p)["status"] == "not_possible"]
    assert nps, "a not_possible example profile is required"
    for d in nps:
        assert d.get("env", {}) == {}, f"{d['id']}: not_possible profile must set no env overrides"
        assert not d.get("dxvk_async"), f"{d['id']}: not_possible must not pretend to tune anything"
        assert not d.get("gamescope"), f"{d['id']}: not_possible must ship no gamescope config"
        notes = d["notes"].lower()
        assert "not run" in notes or "will not" in notes or "does not run" in notes, \
            f"{d['id']}: must state plainly it will not run"
        assert "anti-cheat" in notes or "anticheat" in notes or "vanguard" in notes, \
            f"{d['id']}: must name the anti-cheat reason"


def test_profile_status_matches_compat_matrix():
    """A profiled title's status must agree with the compat matrix (single source of truth)."""
    entries = {e["game"].casefold(): e for e in read_json(MATRIX)["entries"]}
    for p in _profile_paths():
        d = read_json(p)
        title = d["title"].casefold()
        # find the matrix entry by exact title or word-boundary containment
        entry = entries.get(title)
        if entry is None:
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(title) + r"(?![a-z0-9])")
            hits = [e for g, e in entries.items() if pat.search(g)]
            entry = hits[0] if hits else None
        assert entry is not None, f"{d['id']}: title {d['title']!r} not found in compat-matrix.json"
        assert entry["status"] == d["status"], \
            f"{d['id']}: profile status {d['status']} != matrix status {entry['status']}"
