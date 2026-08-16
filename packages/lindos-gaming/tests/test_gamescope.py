"""lindos-gamescope wrapper: arg-handling, mode/profile defaults, honest degrade (SPEC-KERNEL §17.2)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import BIN, SHARE, load_bin  # noqa: E402

PROFILES = SHARE / "gaming" / "profiles"

# Tokens that would betray an attempt to defeat/spoof anti-cheat, attestation or DRM.
# lindos-gamescope is a display layer only and must never emit anything like these
# (SPEC-KERNEL §14). Kept lower-case for a case-insensitive scan.
FORBIDDEN_TOKENS = ("attest", "vanguard", "battleye", "eac", "tpm", "secure-boot",
                    "secureboot", "hwid", "patchguard", "hvci", "spoof", "bypass")


@pytest.fixture(scope="module")
def gs():
    return load_bin("lindos-gamescope")


def test_bin_present_and_python():
    p = BIN / "lindos-gamescope"
    assert p.is_file()
    raw = p.read_bytes()
    assert b"\r\n" not in raw, "LF only"
    assert raw.startswith(b"#!/usr/bin/env python3\n")


def test_parse_resolution(gs):
    assert gs.parse_resolution("1920x1080") == (1920, 1080)
    assert gs.parse_resolution("2560X1440") == (2560, 1440)
    assert gs.parse_resolution(" 1280 , 720 ") == (1280, 720)
    for bad in ("", "1920", "0x1080", "-1x2", "axb"):
        with pytest.raises(ValueError):
            gs.parse_resolution(bad)


def test_build_gamescope_argv_full(gs):
    argv = gs.build_gamescope_argv(["umu-run", "game.exe"], width=2560, height=1440,
                                   refresh=144, fsr=True, hdr=True, mangoapp=True)
    assert argv[0] == "gamescope"
    assert argv[:5] == ["gamescope", "-W", "2560", "-H", "1440"]
    assert "-r" in argv and "144" in argv
    assert "-f" in argv                       # fullscreen default
    assert argv[argv.index("-F") + 1] == "fsr"
    assert "--hdr-enabled" in argv and "--mangoapp" in argv
    # command comes after a single --
    assert argv.count("--") == 1
    assert argv[argv.index("--") + 1:] == ["umu-run", "game.exe"]


def test_build_gamescope_argv_borderless_and_minimal(gs):
    assert gs.build_gamescope_argv(["x"], borderless=True, fullscreen=False) == ["gamescope", "-b", "--", "x"]
    # minimal: fullscreen only, nothing spurious
    assert gs.build_gamescope_argv(["x"]) == ["gamescope", "-f", "--", "x"]
    # borderless wins over fullscreen when both requested
    argv = gs.build_gamescope_argv(["x"], borderless=True, fullscreen=True)
    assert "-b" in argv and "-f" not in argv


def test_build_never_emits_anticheat_tokens(gs):
    argv = gs.build_gamescope_argv(["game.exe"], width=1920, height=1080, fsr=True, hdr=True,
                                   mangoapp=True, steam=True, refresh=60)
    blob = " ".join(argv[:argv.index("--")]).lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in blob, f"gamescope option list must not contain {tok!r}: {argv}"


def test_resolve_options_mode_defaults(gs, monkeypatch):
    ns = gs.build_parser().parse_args(["--mode", "gaming"])
    opts = gs.resolve_options(ns)
    assert opts["mangoapp"] is True and opts["fullscreen"] is True
    ns = gs.build_parser().parse_args(["--mode", "everyday"])
    assert gs.resolve_options(ns)["mangoapp"] is False


def test_resolve_options_profile_then_explicit(gs):
    prof = PROFILES / "elden-ring.json"
    ns = gs.build_parser().parse_args(["--mode", "everyday", "--profile", str(prof)])
    opts = gs.resolve_options(ns)
    assert (opts["width"], opts["height"]) == (2560, 1440)
    assert opts["fsr"] is True and opts["hdr"] is False
    assert opts["mangoapp"] is True                      # profile mangohud=true
    # explicit flags override the profile
    ns = gs.build_parser().parse_args(["--profile", str(prof), "--res", "1280x720", "--no-fsr", "--no-mangoapp"])
    opts = gs.resolve_options(ns)
    assert (opts["width"], opts["height"]) == (1280, 720)
    assert opts["fsr"] is False and opts["mangoapp"] is False


def test_load_profile_gamescope(gs):
    block = gs.load_profile_gamescope(PROFILES / "cyberpunk-2077.json")
    assert block == {"width": 2560, "height": 1440, "fsr": True, "hdr": True, "mangoapp": True}
    # not_possible profile has no gamescope block -> nothing enabled
    block = gs.load_profile_gamescope(PROFILES / "valorant.json")
    assert "width" not in block and "height" not in block


def test_main_no_command_is_usage_error(gs, capsys):
    assert gs.main(["--mode", "gaming"]) == gs.EXIT_USAGE


def test_main_print_wraps_when_gamescope_present(gs, monkeypatch, capsys):
    monkeypatch.setattr(gs, "gamescope_available", lambda: True)
    rc = gs.main(["--mode", "gaming", "-W", "1920", "-H", "1080", "--print", "--", "steam"])
    assert rc == gs.EXIT_OK
    out = capsys.readouterr().out.strip()
    assert out.startswith("gamescope -W 1920 -H 1080")
    assert out.endswith("-- steam")


def test_main_print_json_when_present(gs, monkeypatch, capsys):
    monkeypatch.setattr(gs, "gamescope_available", lambda: True)
    rc = gs.main(["--print", "--json", "--", "umu-run", "g.exe"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["gamescope"] is True
    assert data["command"][0] == "gamescope" and data["command"][-2:] == ["umu-run", "g.exe"]


def test_main_degrades_without_gamescope(gs, monkeypatch, capsys):
    monkeypatch.setattr(gs, "gamescope_available", lambda: False)
    rc = gs.main(["--print", "--", "umu-run", "game.exe"])
    assert rc == gs.EXIT_OK
    out = capsys.readouterr().out.strip()
    assert out == "umu-run game.exe", "degrade: run the command directly, no gamescope"
    err = capsys.readouterr().err  # warning went to stderr (logging)


def test_split_dashdash(gs):
    assert gs._split_dashdash(["-W", "1920", "--", "steam", "-foo"]) == (["-W", "1920"], ["steam", "-foo"])
    assert gs._split_dashdash(["steam"]) == (["steam"], [])


def test_tokens_before_dashdash_become_command(gs, monkeypatch, capsys):
    """`lindos-gamescope steam` (no --) still runs `steam`."""
    monkeypatch.setattr(gs, "gamescope_available", lambda: True)
    rc = gs.main(["--print", "steam"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("-- steam")


# --------------------------------------------------------------------------- lindos-game --gamescope
def test_lindos_game_folds_gamescope():
    game = load_bin("lindos-game")
    # no --gamescope -> command unchanged
    assert game.wrap_gamescope(["steam"], None) == ["steam"]
    # --gamescope (bare) -> wrap through lindos-gamescope, no resolution
    wrapped = game.wrap_gamescope(["steam"], "")
    assert wrapped[0].endswith("lindos-gamescope") and wrapped[1] == "--" and wrapped[-1] == "steam"
    # --gamescope WxH -> pass the resolution to the wrapper
    wrapped = game.wrap_gamescope(["flatpak", "run", "org.vinegarhq.Sober"], "1920x1080")
    assert wrapped[1:4] == ["--res", "1920x1080", "--"]
    assert wrapped[-3:] == ["flatpak", "run", "org.vinegarhq.Sober"]


def test_lindos_game_launch_parser_accepts_gamescope():
    game = load_bin("lindos-game")
    ns = game.build_parser().parse_args(["launch", "steam", "--gamescope", "2560x1440"])
    assert ns.gamescope == "2560x1440"
    ns = game.build_parser().parse_args(["launch", "steam", "--gamescope"])
    assert ns.gamescope == ""
    ns = game.build_parser().parse_args(["launch", "steam"])
    assert ns.gamescope is None


def test_lindos_gamescope_path_resolves_to_package():
    game = load_bin("lindos-game")
    assert Path(game.gamescope_path()).resolve() == (BIN / "lindos-gamescope").resolve()
