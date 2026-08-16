"""lindos-gamescope-session -- argument handling, honest degrade, the wayland-session
.desktop file and the no-spoof scan (SPEC-VM Sec. 23).

The launcher is a stdlib-only Python module (like every other bin), so it is loaded
with importlib and driven through ``main([...])`` -- no bash / gamescope needed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' conftest
from gaming_testlib import BIN, ROOT, load_bin, parse_desktop  # noqa: E402

session = load_bin("lindos-gamescope-session")

SESSION_BIN = BIN / "lindos-gamescope-session"
DESKTOP = ROOT / "usr" / "share" / "wayland-sessions" / "lindos-gaming.desktop"

# Tokens that would betray an attempt to defeat/hide/spoof anti-cheat, DRM or
# attestation. The session is a display layer only and must EMIT none of them in
# the gamescope command it builds (SPEC-VM Sec. 20). These words appear in the
# source only inside honest negations ("no spoof flags here"), so the scan is
# functional -- over the generated argv -- not a raw-source grep.
FORBIDDEN = ("kvm=off", "hv-vendor-id", "smbios", "acpitable", "spoof", "bypass",
             "attest", "vanguard", "battleye", "hwid", "patchguard")


# --------------------------------------------------------------------------- static
def test_session_bin_present_and_python():
    raw = SESSION_BIN.read_bytes()
    assert b"\r\n" not in raw, "LF only"
    assert raw.startswith(b"#!/usr/bin/env python3\n")


def test_session_emits_no_spoof_flags_and_no_shell_true():
    # functional: whatever launcher/command is chosen, the gamescope argv only ever
    # carries standard display flags -- never an evasion knob.
    for inner in (["steam", "-bigpicture"], ["lutris", "--fullscreen"], ["heroic"]):
        joined = " ".join(session.build_session_argv(inner)).lower()
        for tok in FORBIDDEN:
            assert tok not in joined, f"forbidden token {tok!r} emitted for {inner}"
    low = SESSION_BIN.read_text(encoding="utf-8").lower().replace(" ", "")
    assert "shell=true" not in low


def test_wayland_session_desktop():
    assert DESKTOP.is_file()
    assert b"\r\n" not in DESKTOP.read_bytes(), "LF only"
    entry = parse_desktop(DESKTOP)["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Name"] == "Lindos Gaming"
    assert entry["Exec"] == "lindos-gamescope-session"
    assert entry["TryExec"] == "lindos-gamescope-session"


# --------------------------------------------------------------------------- pure builders
def test_build_session_argv_is_standard_only():
    assert session.build_session_argv(["steam", "-bigpicture"]) == \
        ["gamescope", "-f", "-e", "--", "steam", "-bigpicture"]


def test_resolve_inner_precedence():
    # default
    assert session.resolve_inner([], None) == ["steam", "-bigpicture"]
    # --launcher (shell-split)
    assert session.resolve_inner([], "lutris --fullscreen") == ["lutris", "--fullscreen"]
    # explicit command wins over --launcher
    assert session.resolve_inner(["heroic"], "lutris") == ["heroic"]
    # empty launcher string is a usage error
    with pytest.raises(ValueError):
        session.resolve_inner([], "   ")


# --------------------------------------------------------------------------- main() arg handling
def test_print_default_is_steam_big_picture(capsys):
    rc = session.main(["--print"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "gamescope -f -e -- steam -bigpicture"


def test_print_with_launcher(capsys):
    rc = session.main(["--print", "--launcher", "lutris --fullscreen"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "gamescope -f -e -- lutris --fullscreen"


def test_print_double_dash_command_wins(capsys):
    rc = session.main(["--print", "--launcher", "lutris", "--", "steam", "-tenfoot"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "gamescope -f -e -- steam -tenfoot"


def test_print_json(capsys):
    rc = session.main(["--print", "--json", "--", "heroic"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["launcher"] == "heroic"
    assert out["command"] == ["gamescope", "-f", "-e", "--", "heroic"]
    assert isinstance(out["gamescope"], bool)


def test_unknown_option_is_usage_error():
    # argparse rejects an unknown option before -- with SystemExit(2).
    with pytest.raises(SystemExit) as ei:
        session.main(["--frobnicate"])
    assert ei.value.code == session.EXIT_USAGE


def test_degrade_when_gamescope_absent(monkeypatch, caplog):
    monkeypatch.setattr(session, "gamescope_available", lambda: False)
    with caplog.at_level("ERROR"):
        rc = session.main([])
    assert rc == session.EXIT_ERROR
    assert "gamescope is not installed" in caplog.text.lower()


def test_degrade_when_steam_absent(monkeypatch, caplog):
    monkeypatch.setattr(session, "gamescope_available", lambda: True)
    monkeypatch.setattr(session, "launcher_available", lambda program: False)
    with caplog.at_level("ERROR"):
        rc = session.main([])
    assert rc == session.EXIT_ERROR
    assert "steam is not installed" in caplog.text.lower()
