"""Package-level sanity: DEBIAN/control per SPEC §10, maintainer scripts, config files, udev rules,
desktop entries, icons, line endings and shebangs."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import (  # noqa: E402
    APPS, BIN, DEBIAN, ETC, ICONS, LIBEXEC, PKG_ROOT, ROOT, SHARE,
    load_bin, parse_control, parse_desktop, read_text, split_deps,
)

SPEC_DEPENDS = {"lindos-core", "gamemode", "mangohud", "steam-devices", "python3", "curl | wget", "flatpak"}
SPEC_RECOMMENDS = {"steam-launcher | steam-installer", "lutris", "heroic", "prismlauncher", "antimicrox", "goverlay",
                   "piper", "corectrl", "openrgb", "mesa-vulkan-drivers", "libvulkan1", "vulkan-tools",
                   "xdg-desktop-portal-gtk"}


def _all_files() -> list[Path]:
    out = []
    for base in (ROOT, DEBIAN, PKG_ROOT / "tests"):
        for p in base.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p)
    return out


# --------------------------------------------------------------------------- DEBIAN
def test_control_fields():
    c = parse_control(DEBIAN / "control")
    assert c["Package"] == "lindos-gaming"
    assert c["Version"] == "1.0.0"
    assert c["Architecture"] == "all"
    assert c["Maintainer"] == "Lindos Team <team@lindos.dev>"
    assert c["Section"] and c["Description"]
    deps = set(split_deps(c["Depends"]))
    assert SPEC_DEPENDS <= deps, f"Depends missing {SPEC_DEPENDS - deps}"
    recs = set(split_deps(c["Recommends"]))
    assert SPEC_RECOMMENDS <= recs, f"Recommends missing {SPEC_RECOMMENDS - recs}"
    desc = c["Description"].lower()
    for phrase in ("valorant", "fortnite", "sober", "wine/proton"):
        assert phrase in desc, f"honesty note missing from Description: {phrase}"


def test_maintainer_scripts():
    for name in ("postinst", "postrm"):
        text = read_text(DEBIAN / name)
        assert text.startswith("#!/bin/sh\n"), name
        assert "set -e" in text, name
        for forbidden in ("curl ", "wget ", "apt-get install", "flatpak install", "pip "):
            assert forbidden not in text, f"{name}: no downloads/installs in maintainer scripts ({forbidden.strip()})"
    post = read_text(DEBIAN / "postinst")
    assert "udevadm control --reload-rules" in post and "udevadm trigger" in post
    assert "sysctl --system" in post
    assert "update-desktop-database" in post
    assert "gtk-update-icon-cache" in post
    conff = read_text(DEBIAN / "conffiles").split()
    for f in ("/etc/gamemode.ini", "/etc/xdg/MangoHud/MangoHud.conf", "/etc/udev/rules.d/60-lindos-controllers.rules",
              "/etc/sysctl.d/80-lindos-gaming.conf"):
        assert f in conff, f
        assert (ROOT / f.lstrip("/")).is_file(), f


# --------------------------------------------------------------------------- shipped files (SPEC §10)
@pytest.mark.parametrize("rel", [
    "usr/bin/lindos-proton", "usr/bin/lindos-drivers", "usr/bin/lindos-game", "usr/bin/lindos-mangohud",
    "usr/libexec/lindos/install-gaming.sh", "usr/libexec/lindos/gamemode-start.sh", "usr/libexec/lindos/gamemode-end.sh",
    "usr/libexec/lindos/install-xpadneo.sh", "usr/libexec/lindos/install-xone.sh",
    "etc/gamemode.ini", "etc/xdg/MangoHud/MangoHud.conf", "etc/udev/rules.d/60-lindos-controllers.rules",
    "etc/sysctl.d/80-lindos-gaming.conf",
    "usr/share/applications/lindos-roblox.desktop", "usr/share/applications/lindos-roblox-studio.desktop",
    "usr/share/applications/lindos-minecraft.desktop",
    "usr/share/icons/hicolor/scalable/apps/lindos-roblox.svg", "usr/share/icons/hicolor/scalable/apps/lindos-minecraft.svg",
    "usr/share/lindos/compat-matrix.json", "usr/share/lindos/gaming/launchers.json",
])
def test_spec_file_present(rel):
    assert (ROOT / rel).is_file(), rel


def test_line_endings_and_shebangs():
    for p in _all_files():
        raw = p.read_bytes()
        assert b"\r\n" not in raw, f"{p}: CRLF line endings"
        if p.parent in (BIN, LIBEXEC) or p.parent == DEBIAN and p.name in ("postinst", "postrm"):
            first = raw.split(b"\n", 1)[0]
            assert first.startswith(b"#!"), f"{p}: missing shebang"
    for p in LIBEXEC.glob("*.sh"):
        text = read_text(p)
        assert text.startswith("#!/bin/bash\n"), p
        assert "set -Eeuo pipefail" in text, p
        assert re.search(r"^log\(\)", text, re.M) and re.search(r"^die\(\)", text, re.M), f"{p}: log()/die() required"
        assert not re.search(r"^\s*sudo\s", text, re.M), f"{p}: no sudo inside scripts"
    for p in BIN.iterdir():
        assert p.is_file(), f"unexpected non-file in usr/bin: {p} (stray __pycache__?)"
        assert read_text(p).startswith("#!/usr/bin/env python3\n"), p


def test_no_pycache_in_package_tree():
    assert not list(ROOT.rglob("__pycache__")), "__pycache__ inside root/ would be shipped in the .deb"


def test_python_bins_import_without_side_effects_and_are_stdlib_only():
    for name in ("lindos-proton", "lindos-drivers", "lindos-game", "lindos-mangohud"):
        mod = load_bin(name)
        assert callable(getattr(mod, "main"))
        src = read_text(BIN / name)
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        assert not any(re.search(r"shell\s*=\s*True", ln) for ln in code_lines), f"{name}: never shell=True"
        assert "import gi" not in src and "gi.repository" not in src, f"{name}: no GTK in CLIs"
        assert "argparse" in src and "logging" in src, f"{name}: argparse + logging required"


# --------------------------------------------------------------------------- gamemode.ini
def test_gamemode_ini():
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=None, interpolation=None)
    cp.read_string(read_text(ETC / "gamemode.ini"))
    g = cp["general"]
    assert g["renice"] == "10" and g["ioprio"] == "0" and g["inhibit_screensaver"] == "1"
    assert g["softrealtime"] == "auto" and g["reaper_freq"] == "5"
    assert g["desiredgov"] == "performance" and g["defaultgov"] == "schedutil"
    assert g["igpu_desiredgov"] == "powersave" and g["igpu_power_threshold"] == "0.3"
    assert cp["gpu"]["apply_gpu_optimisations"] == "0"
    assert cp["custom"]["start"] == "/usr/libexec/lindos/gamemode-start.sh"
    assert cp["custom"]["end"] == "/usr/libexec/lindos/gamemode-end.sh"
    start = read_text(LIBEXEC / "gamemode-start.sh")
    end = read_text(LIBEXEC / "gamemode-end.sh")
    assert "lindos-compositor stop" in start and "lindos-compositor start" in end
    assert "command -v lindos-compositor" in start and "command -v lindos-compositor" in end, "guarded"
    assert "notify-send" in start and "notify-send" in end


# --------------------------------------------------------------------------- MangoHud.conf
def test_mangohud_conf_keys():
    text = read_text(ETC / "xdg" / "MangoHud" / "MangoHud.conf")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    keys = {ln.split("=", 1)[0].strip() for ln in lines}
    for k in ("fps", "frametime", "frame_timing", "cpu_stats", "cpu_temp", "gpu_stats", "gpu_temp", "ram", "vram",
              "position", "font_size", "toggle_hud", "background_alpha", "round_corners", "no_display"):
        assert k in keys, f"MangoHud.conf missing {k}"
    kv = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    assert kv["position"] == "top-left" and kv["font_size"] == "20"
    assert kv["toggle_hud"] == "Shift_R+F12" and kv["background_alpha"] == "0.4" and kv["round_corners"] == "8"
    assert "no_display" in lines, "HUD hidden by default (bare no_display line)"
    for ln in lines:
        assert re.match(r"^[a-z_0-9]+(=.*)?$", ln), f"unexpected MangoHud line: {ln!r}"


def test_lindos_mangohud_visibility_helpers(fake_home: Path, monkeypatch):
    mh = load_bin("lindos-mangohud")
    tpl = read_text(ETC / "xdg" / "MangoHud" / "MangoHud.conf")
    assert mh.is_hidden(tpl) is True
    shown = mh.set_visibility(tpl, True)
    assert mh.is_hidden(shown) is False and "fps" in shown
    hidden_again = mh.set_visibility(shown, False)
    assert mh.is_hidden(hidden_again) is True
    assert hidden_again.count("\nno_display\n") == 1
    assert mh.is_hidden("no_display=0\nfps\n") is False
    assert mh.is_hidden("fps\n") is False and mh.is_hidden(mh.set_visibility("fps\n", False)) is True
    # on/off write the per-user file under LINDOS_HOME (no lindos-core needed)
    monkeypatch.setitem(sys.modules, "lindos", None)  # force the guarded config bridge to fail gracefully
    assert mh.main(["on"]) == 0
    user = fake_home / ".config" / "MangoHud" / "MangoHud.conf"
    assert user.is_file() and mh.is_hidden(read_text(user)) is False
    assert mh.main(["off"]) == 0 and mh.is_hidden(read_text(user)) is True


# --------------------------------------------------------------------------- sysctl + udev
def test_sysctl_gaming():
    text = read_text(ETC / "sysctl.d" / "80-lindos-gaming.conf")
    kv = {}
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            k, _, v = s.partition("=")
            kv[k.strip()] = v.strip()
    assert kv == {"vm.max_map_count": "2147483642"}


def test_udev_controller_rules():
    text = read_text(ETC / "udev" / "rules.d" / "60-lindos-controllers.rules")
    low = text.lower()
    for vid in ("045e", "054c", "057e", "2dc8", "28de", "046d"):
        assert vid in low, f"vendor {vid} missing"
    assert 'SUBSYSTEM=="hidraw"' in text
    assert 'KERNEL=="uinput"' in text
    assert 'TAG+="uaccess"' in text and 'MODE="0660"' in text and 'GROUP="input"' in text
    assert 'ENV{ID_INPUT_JOYSTICK}="1"' in text
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        assert re.match(r'^(ACTION|SUBSYSTEM|KERNEL|KERNELS|ATTRS?|ENV|LABEL|GOTO)', s), f"odd udev line: {s!r}"
        assert s.count('"') % 2 == 0, f"unbalanced quotes: {s!r}"


# --------------------------------------------------------------------------- desktop entries + icons
@pytest.mark.parametrize("name,exec_prefix,icon", [
    ("lindos-roblox.desktop", "flatpak run org.vinegarhq.Sober", "lindos-roblox"),
    ("lindos-roblox-studio.desktop", "flatpak run org.vinegarhq.Vinegar", "lindos-roblox"),
    ("lindos-minecraft.desktop", "lindos-game launch prism", "lindos-minecraft"),
])
def test_desktop_entries(name, exec_prefix, icon):
    groups = parse_desktop(APPS / name)
    e = groups["Desktop Entry"]
    assert e["Type"] == "Application"
    assert e["Name"] and e["Exec"].startswith(exec_prefix), e["Exec"]
    assert e["Icon"] == icon and (ICONS / f"{icon}.svg").is_file()
    assert "Game;" in e["Categories"]
    assert e["Terminal"] == "false"
    if name == "lindos-roblox.desktop":
        assert e["Name"] == "Roblox" and "Sober" in e["Comment"]
        assert "windows" in e.get("X-Lindos-Note", "").lower()
    for action in [a for a in e.get("Actions", "").split(";") if a]:
        grp = groups.get(f"Desktop Action {action}")
        assert grp and grp.get("Name") and grp.get("Exec"), f"{name}: action {action} incomplete"


def test_icons_are_original_svgs():
    import xml.dom.minidom
    for svg in ("lindos-roblox.svg", "lindos-minecraft.svg"):
        text = read_text(ICONS / svg)
        dom = xml.dom.minidom.parseString(text)
        assert dom.documentElement.tagName == "svg"
        assert dom.documentElement.getAttribute("viewBox")
        assert "<image" not in text and "data:image" not in text, "no embedded bitmaps / trademarked art"


# --------------------------------------------------------------------------- xpadneo / xone
def test_optional_driver_scripts():
    xp = read_text(LIBEXEC / "install-xpadneo.sh")
    assert "atar-axis/xpadneo" in xp and re.search(r'XPADNEO_VERSION="\$\{XPADNEO_VERSION:-v\d+\.\d+\.\d+\}"', xp)
    assert "dkms" in xp
    xo = read_text(LIBEXEC / "install-xone.sh")
    assert "--accept-firmware-license" in xo and "Microsoft" in xo and "firmware" in xo.lower()
    assert re.search(r'die "the Xbox Wireless Adapter firmware is proprietary', xo), "licence gate must be explicit"
    for text in (xp, xo):
        assert re.search(r'die "offline[^"]*" 3', text)
