"""The login-time autostart entry that makes the browser chosen in Lindos Setup XFCE's preferred one once it is
installed (review finding: a Chrome that landed after Setup - offline install, silent retry - never reached the
user's xfce4 helpers.rc, so exo-open, the browser key and the menu's web search kept opening Firefox).

The logic lives in lindos-core (lindos.browsers.sync_default, 'lindos-browser sync-default'); this package only
starts it.  Covers the entry, its package wiring and that the exact command line it runs is a real subcommand.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
DESKTOP = ROOT / "etc" / "xdg" / "autostart" / "lindos-browser-default.desktop"
CORE = PKG.parent / "lindos-core" / "root"
CORE_CLI = CORE / "usr" / "bin" / "lindos-browser"
CORE_LIB = CORE / "usr" / "lib" / "python3" / "dist-packages"

LIVE_CMDLINE = "BOOT_IMAGE=/casper/vmlinuz boot=casper username=liveuser quiet splash --"


def _entries(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln and not ln.startswith("#"))


def test_the_entry_runs_the_login_time_sync_in_xfce_only_and_shows_nothing() -> None:
    entry = _entries(DESKTOP)
    assert entry["Type"] == "Application"
    assert entry["Exec"] == "lindos-browser sync-default --wait"
    assert entry["TryExec"] == "lindos-browser"                       # no lindos-core, no entry - never an error dialog
    assert entry["OnlyShowIn"] == "XFCE;"
    assert entry["NoDisplay"] == "true" and entry["Terminal"] == "false" and entry["StartupNotify"] == "false"
    assert entry["Name"] and entry["Comment"] and entry["Icon"]
    assert entry["X-Lindos-Component"] == "desktop"


def test_the_entry_starts_after_the_session_settled_and_never_before_setup_can_have_begun() -> None:
    entry = _entries(DESKTOP)
    assert entry["X-GNOME-Autostart-Phase"] == "Applications"
    assert int(entry["X-GNOME-Autostart-Delay"]) >= 10                # Lindos Setup (delay 2) is up before it looks
    assert "X-XFCE-Autostart-Override" not in entry or entry["X-XFCE-Autostart-Override"] != "false"


def test_the_entry_is_a_conffile_and_the_package_says_what_it_is_for() -> None:
    conffiles = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    assert "/" + DESKTOP.relative_to(ROOT).as_posix() in conffiles
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    assert "lindos-browser-default" in control and "sync-default" in control
    assert "lindos-core" in dict(line.split(": ", 1) for line in control.splitlines() if ": " in line)["Depends"]


def test_the_command_the_entry_runs_is_a_real_subcommand_and_does_nothing_in_the_live_session(tmp_path: Path) -> None:
    """Runs the exact argument list of the Exec line through the shipped CLI; the live session must be a no-op."""
    argv = _entries(DESKTOP)["Exec"].split()
    assert argv[0] == "lindos-browser"
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(LIVE_CMDLINE, encoding="utf-8")
    home = tmp_path / "home"
    root = tmp_path / "root"
    home.mkdir()
    root.mkdir()
    env = dict(os.environ)
    env.update({"LINDOS_TEST_CMDLINE": str(cmdline), "LINDOS_HOME": str(home), "LINDOS_ROOT": str(root),
                "PYTHONPATH": str(CORE_LIB) + os.pathsep + env.get("PYTHONPATH", ""), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.run([sys.executable, str(CORE_CLI), *argv[1:]], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "sync-default: live"
    assert not any(home.rglob("*")), "nothing is written in the live session"
    as_json = subprocess.run([sys.executable, str(CORE_CLI), *argv[1:], "--json"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert json.loads(as_json.stdout)["outcome"] == "live"
