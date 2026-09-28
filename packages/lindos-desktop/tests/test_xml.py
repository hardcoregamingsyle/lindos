"""lindos-desktop — structural tests for the shipped XFCE defaults (SPEC §2, §3, §5).

Pure stdlib, no GTK, runs on Windows/macOS/Linux:

* every xfconf XML parses and its <channel name> matches the file name;
* xfce4-panel.xml implements the SPEC §5 taskbar (bottom, 48 px, plugin order, colours);
* xfce4-keyboard-shortcuts.xml binds every SPEC §5 shortcut in commands/custom + xfwm4/custom;
* xsettings / xfwm4 / notifyd / session defaults carry the SPEC values;
* per-mode panel dirs are complete and pins follow SPEC §3;
* panel-profile-pack.py produces a valid xfce4-panel-profiles tarball;
* rc files, .desktop shims, autostart entries, SVG art and DEBIAN metadata are sane.
"""
from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import io
import os
import re
import tarfile
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
ROOT = PKG / "root"
XFCONF = ROOT / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
PANEL_RC = ROOT / "etc" / "xdg" / "xfce4" / "panel"
MODES = ROOT / "usr" / "share" / "lindos" / "modes"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
APPS = ROOT / "usr" / "share" / "applications"
AUTOSTART = ROOT / "etc" / "xdg" / "autostart"
DEBIAN = PKG / "DEBIAN"

MODE_IDS = ["everyday", "gaming", "work", "creator", "lite"]
CHANNEL_FILES = ["xfce4-panel.xml", "xfwm4.xml", "xsettings.xml", "xfce4-keyboard-shortcuts.xml",
                 "xfce4-desktop.xml", "thunar.xml", "xfce4-notifyd.xml", "xfce4-power-manager.xml",
                 "xfce4-session.xml", "keyboards.xml"]

# SPEC §5 plugin order (types) on panel-1
PANEL_ORDER = ["separator", "whiskermenu", "docklike", "separator", "systray", "pulseaudio",
               "power-manager-plugin", "notification-plugin", "clock", "showdesktop"]

# SPEC §5 shortcuts: (provider, key, expected command/action)
COMMAND_SHORTCUTS = {
    "Super_L": "xfce4-popup-whiskermenu",
    "<Super>e": "thunar",
    "<Super>i": "lindos-settings",
    "<Super>l": "xflock4",
    "<Super>a": "xfce4-notifyd-config",
    "<Super>x": "lindos-settings --power-menu",
    "<Super>v": "xfce4-popup-clipman",
    "<Super>r": "xfce4-appfinder --collapsed",
    "<Super><Shift>s": "xfce4-screenshooter -r",
    "Print": "xfce4-screenshooter -f",
    "<Primary><Shift>Escape": "xfce4-taskmanager",
    "<Super>period": "emote",
    "<Primary><Alt>t": None,          # a terminal (exo-open TerminalEmulator or xfce4-terminal)
    "<Super>g": "lindos-settings gaming",
}
XFWM_SHORTCUTS = {
    "<Super>Tab": "cycle_windows_key",
    "<Super>d": "show_desktop_key",
    "<Super>Left": "tile_left_key",
    "<Super>Right": "tile_right_key",
    "<Super>Up": "maximize_window_key",
    "<Super>Down": None,              # restore/minimize approximation
    "<Primary><Super>Left": "prev_workspace_key",
    "<Primary><Super>Right": "next_workspace_key",
}


# ----------------------------------------------------------------------------- helpers
def parse(path: Path) -> ET.Element:
    return ET.parse(str(path)).getroot()


def props(elem: ET.Element, prefix: str = "") -> Dict[str, ET.Element]:
    """Flatten <property> tree into {'/a/b': element}."""
    out: Dict[str, ET.Element] = {}
    for child in elem:
        if child.tag != "property":
            continue
        path = f"{prefix}/{child.get('name')}"
        out[path] = child
        out.update(props(child, path))
    return out


def value(elem: ET.Element) -> Optional[str]:
    return elem.get("value")


def array_values(elem: ET.Element) -> List[str]:
    return [v.get("value") or "" for v in elem if v.tag == "value"]


def rc_read(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str  # type: ignore[assignment]
    text = path.read_text(encoding="utf-8")
    if not text.lstrip().startswith("["):
        text = "[__top__]\n" + text
    parser.read_string(text)
    return parser


def load_packer() -> types.ModuleType:
    path = LIBEXEC / "panel-profile-pack.py"
    loader = importlib.machinery.SourceFileLoader("lindos_panel_profile_pack", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def desktop_entries(path: Path) -> Dict[str, str]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(str(path), encoding="utf-8")
    assert parser.has_section("Desktop Entry"), f"{path.name}: no [Desktop Entry]"
    return dict(parser.items("Desktop Entry"))


# ----------------------------------------------------------------------------- xfconf channels
@pytest.mark.parametrize("name", CHANNEL_FILES)
def test_channel_files_parse_and_match_name(name: str) -> None:
    path = XFCONF / name
    assert path.is_file(), f"missing {path}"
    root = parse(path)
    assert root.tag == "channel"
    assert root.get("version") == "1.0"
    assert root.get("name") == name[:-4], f"{name}: channel name {root.get('name')!r}"
    for elem in root.iter("property"):
        assert elem.get("name"), "property without name"
        ptype = elem.get("type")
        assert ptype in {"string", "bool", "int", "uint", "int64", "uint64", "double", "array", "empty"}, \
            f"{name}: odd type {ptype!r} on {elem.get('name')}"
        if ptype not in ("array", "empty"):
            assert elem.get("value") is not None, f"{name}: {elem.get('name')} has no value"


def test_no_stray_xml_files_in_xfconf_dir() -> None:
    names = sorted(p.name for p in XFCONF.iterdir())
    assert names == sorted(CHANNEL_FILES), names


def test_all_xml_and_svg_wellformed() -> None:
    files = list(ROOT.rglob("*.xml")) + list(ROOT.rglob("*.svg")) + list(ROOT.rglob("*.conf"))
    checked = 0
    for f in files:
        if f.suffix == ".conf" and "fonts" not in str(f):
            continue
        parse(f)
        checked += 1
    assert checked >= 27


# ----------------------------------------------------------------------------- panel
@pytest.fixture(scope="module")
def panel() -> Dict[str, ET.Element]:
    return props(parse(XFCONF / "xfce4-panel.xml"))


def test_panel_geometry(panel: Dict[str, ET.Element]) -> None:
    p1 = "/panels/panel-1"
    assert value(panel[f"{p1}/position"]) == "p=10;x=0;y=0"
    assert value(panel[f"{p1}/position-locked"]) == "true"
    assert value(panel[f"{p1}/size"]) == "48"
    assert value(panel[f"{p1}/length"]) == "100"
    assert value(panel[f"{p1}/mode"]) == "0"
    assert panel[f"{p1}/size"].get("type") == "uint"
    assert array_values(panel["/panels"]) == ["1"]
    assert value(panel["/panels/dark-mode"]) == "true"


def test_panel_background_dark_translucent(panel: Dict[str, ET.Element]) -> None:
    # background-style 1 = solid colour (0 = none, 2 = image) — the only style that uses rgba
    assert value(panel["/panels/panel-1/background-style"]) == "1"
    rgba = [float(v) for v in array_values(panel["/panels/panel-1/background-rgba"])]
    assert rgba == pytest.approx([0.125, 0.125, 0.125, 0.85])


def test_panel_plugin_order(panel: Dict[str, ET.Element]) -> None:
    ids = array_values(panel["/panels/panel-1/plugin-ids"])
    assert ids == ["3", "1", "2", "4", "5", "6", "7", "8", "9", "10"]
    types_in_order = [value(panel[f"/plugins/plugin-{i}"]) for i in ids]
    assert types_in_order == PANEL_ORDER
    # both separators are transparent (style 0) and expanding → centred start + apps
    for sep in ("3", "4"):
        assert value(panel[f"/plugins/plugin-{sep}/expand"]) == "true"
        assert value(panel[f"/plugins/plugin-{sep}/style"]) == "0"


def test_panel_clock_two_lines(panel: Dict[str, ET.Element]) -> None:
    clock = "/plugins/plugin-9"
    assert value(panel[clock]) == "clock"
    assert value(panel[f"{clock}/mode"]) == "2"                # digital
    assert value(panel[f"{clock}/digital-layout"]) == "1"      # time above date
    assert value(panel[f"{clock}/digital-time-format"]) == "%H:%M"
    assert value(panel[f"{clock}/digital-date-format"]) == "%d/%m/%Y"
    assert value(panel[f"{clock}/digital-time-font"]) == "Selawik 9"
    assert value(panel[f"{clock}/digital-date-font"]) == "Selawik 9"


def test_panel_rc_files_match_plugin_ids(panel: Dict[str, ET.Element]) -> None:
    assert value(panel["/plugins/plugin-1"]) == "whiskermenu"
    assert value(panel["/plugins/plugin-2"]) == "docklike"
    assert (PANEL_RC / "whiskermenu-1.rc").is_file()
    assert (PANEL_RC / "docklike-2.rc").is_file()


# ----------------------------------------------------------------------------- whisker / docklike
def test_whiskermenu_rc_defaults() -> None:
    rc = rc_read(PANEL_RC / "whiskermenu-1.rc")
    top = rc["__top__"]
    assert top["button-icon"] == "lindos-start"
    assert top["show-button-title"] == "false"
    assert top["view-mode"] == "0", "0 = icon grid (1 = list, 2 = tree in whiskermenu's ViewMode enum)"
    assert top["menu-width"] == "680"
    assert top["menu-height"] == "720"
    assert top["position-search-alternate"] == "true"      # search at top
    assert top["position-categories-alternate"] == "true"  # categories left
    assert top["command-settings"] == "lindos-settings"
    assert top["command-lockscreen"] == "xflock4"
    assert top["command-switchuser"] == "dm-tool switch-to-greeter"
    assert top["command-logout"] == "xfce4-session-logout"
    assert top["command-profile"] == "lindos-settings accounts"
    for k in ("show-command-settings", "show-command-lockscreen", "show-command-switchuser",
              "show-command-logout", "show-command-profile"):
        assert top[k] == "true", k
    favs = top["favorites"].split(",")
    assert favs[0] == "lindos-files.desktop"
    assert "lindos-settings.desktop" in favs


def test_docklike_rc_defaults() -> None:
    rc = rc_read(PANEL_RC / "docklike-2.rc")
    user = rc["user"]
    pins = [p for p in user["pinned"].split(";") if p]
    assert pins == ["lindos-files.desktop", "firefox.desktop", "lindos-store.desktop",
                    "lindos-settings.desktop", "lindos-terminal.desktop"]
    assert user["keyComboActive"] == "true"    # Super+1..9 handled by docklike
    assert user["showPreviews"] == "true"
    assert user["iconSize"].isdigit()
    for key in ("indicatorStyle", "inactiveIndicatorStyle", "noWindowsListIfSingle",
                "onlyDisplayVisible", "showWindowCount", "keyAloneActive", "forceIconSize"):
        assert key in user, key


def test_whisker_favorites_equal_docklike_pins_everywhere() -> None:
    pairs = [(PANEL_RC / "whiskermenu-1.rc", PANEL_RC / "docklike-2.rc")]
    for mode in MODE_IDS:
        pairs.append((MODES / mode / "panel" / "whiskermenu-1.rc", MODES / mode / "panel" / "docklike-2.rc"))
    for whisker, dock in pairs:
        favs = rc_read(whisker)["__top__"]["favorites"].split(",")
        pins = [p for p in rc_read(dock)["user"]["pinned"].split(";") if p]
        assert favs == pins, f"{whisker}: favorites {favs} != pins {pins}"


# ----------------------------------------------------------------------------- shortcuts
@pytest.fixture(scope="module")
def shortcuts() -> Dict[str, ET.Element]:
    return props(parse(XFCONF / "xfce4-keyboard-shortcuts.xml"))


@pytest.mark.parametrize("key,expected", sorted(COMMAND_SHORTCUTS.items()))
def test_command_shortcut(shortcuts: Dict[str, ET.Element], key: str, expected: Optional[str]) -> None:
    for branch in ("custom", "default"):
        path = f"/commands/{branch}/{key}"
        assert path in shortcuts, f"{key} missing in commands/{branch}"
        got = value(shortcuts[path])
        if expected is None:
            assert got, f"{key}: empty command"
            if key == "<Primary><Alt>t":
                assert "Terminal" in got or "terminal" in got
        else:
            assert got == expected, f"{key}: {got!r} != {expected!r}"


@pytest.mark.parametrize("key,expected", sorted(XFWM_SHORTCUTS.items()))
def test_xfwm_shortcut(shortcuts: Dict[str, ET.Element], key: str, expected: Optional[str]) -> None:
    for branch in ("custom", "default"):
        path = f"/xfwm4/{branch}/{key}"
        assert path in shortcuts, f"{key} missing in xfwm4/{branch}"
        got = value(shortcuts[path])
        if expected is None:
            assert got and got.endswith("_key")
        else:
            assert got == expected, f"{key}: {got!r} != {expected!r}"


def test_shortcuts_custom_override_and_providers(shortcuts: Dict[str, ET.Element]) -> None:
    assert value(shortcuts["/commands/custom/override"]) == "true"
    assert value(shortcuts["/xfwm4/custom/override"]) == "true"
    assert set(array_values(shortcuts["/providers"])) == {"xfwm4", "commands"}
    # Super+1..9 must NOT be bound as commands: docklike grabs them (keyComboActive)
    for n in range(1, 10):
        assert f"/commands/custom/<Super>{n}" not in shortcuts


# ----------------------------------------------------------------------------- other channels
def test_xsettings_defaults() -> None:
    xs = props(parse(XFCONF / "xsettings.xml"))
    assert value(xs["/Net/ThemeName"]) == "Lindos-Dark"
    # default session is dark → dark-panel icon variant, same as lindos.theme.set_dark(True)
    assert value(xs["/Net/IconThemeName"]) == "Lindos-dark"
    assert value(xs["/Gtk/CursorThemeName"]) == "Fluent-dark-cursors"
    assert value(xs["/Gtk/FontName"]) == "Selawik 10"
    assert value(xs["/Gtk/DecorationLayout"]) == "menu:minimize,maximize,close"
    assert value(xs["/Gtk/DialogsUseHeader"]) == "false"
    assert "/Gtk/MonospaceFontName" in xs
    for k in ("/Xft/Antialias", "/Xft/Hinting", "/Xft/HintStyle", "/Xft/RGBA"):
        assert k in xs, k


def test_xfwm4_defaults() -> None:
    wm = props(parse(XFCONF / "xfwm4.xml"))
    g = "/general"
    assert value(wm[f"{g}/theme"]) == "Lindos-Dark"
    assert value(wm[f"{g}/button_layout"]) == "O|HMC"
    assert value(wm[f"{g}/title_font"]) == "Selawik Bold 9"
    assert value(wm[f"{g}/placement_mode"]) == "center"
    assert value(wm[f"{g}/use_compositing"]) == "true"
    assert value(wm[f"{g}/frame_opacity"]) == "100"
    assert value(wm[f"{g}/show_frame_shadow"]) == "true"
    assert value(wm[f"{g}/snap_to_windows"]) == "true"
    assert value(wm[f"{g}/tile_on_move"]) == "true"
    assert value(wm[f"{g}/wrap_workspaces"]) == "false"
    assert value(wm[f"{g}/focus_delay"]) == "100"
    assert value(wm[f"{g}/easy_click"]) == "Super"


def test_desktop_notifyd_session_keyboards_power_thunar() -> None:
    d = props(parse(XFCONF / "xfce4-desktop.xml"))
    assert value(d["/backdrop/screen0/monitor0/workspace0/last-image"]) == "/usr/share/backgrounds/lindos/aurora-dark.svg"
    assert value(d["/backdrop/screen0/monitor0/workspace0/image-style"]) == "5"
    assert value(d["/desktop-icons/single-click"]) == "false"
    for k in ("show-home", "show-trash", "show-filesystem"):
        assert value(d[f"/desktop-icons/file-icons/{k}"]) == "true", k
    n = props(parse(XFCONF / "xfce4-notifyd.xml"))
    assert value(n["/theme"]) == "Lindos"
    assert (ROOT / "usr/share/themes/Lindos/xfce-notify-4.0/gtk.css").is_file()
    s = props(parse(XFCONF / "xfce4-session.xml"))
    assert value(s["/general/SaveOnExit"]) == "false"
    k = props(parse(XFCONF / "keyboards.xml"))
    assert value(k["/Default/KeyRepeat"]) == "true"
    p = props(parse(XFCONF / "xfce4-power-manager.xml"))
    assert "/xfce4-power-manager/power-button-action" in p
    t = props(parse(XFCONF / "thunar.xml"))
    assert value(t["/last-view"]) == "ThunarDetailsView"
    assert value(t["/last-side-pane"]) == "ThunarShortcutsPane"
    assert value(t["/misc-single-click"]) == "false"


# ----------------------------------------------------------------------------- modes
@pytest.mark.parametrize("mode", MODE_IDS)
def test_mode_panel_dir_complete(mode: str) -> None:
    d = MODES / mode / "panel"
    for name in ("xfce4-panel.xml", "whiskermenu-1.rc", "docklike-2.rc"):
        assert (d / name).is_file(), f"{mode}: {name} missing"
    p = props(parse(d / "xfce4-panel.xml"))
    ids = array_values(p["/panels/panel-1/plugin-ids"])
    assert [value(p[f"/plugins/plugin-{i}"]) for i in ids] == PANEL_ORDER
    assert value(p["/panels/panel-1/size"]) == "48"
    assert value(p["/panels/panel-1/position"]) == "p=10;x=0;y=0"
    rgba = [float(v) for v in array_values(p["/panels/panel-1/background-rgba"])]
    assert rgba[:3] == pytest.approx([0.125, 0.125, 0.125])
    assert rgba[3] == pytest.approx(1.0 if mode == "lite" else 0.85)


def test_mode_pins_follow_spec() -> None:
    def pins(mode: str) -> List[str]:
        return [p for p in rc_read(MODES / mode / "panel" / "docklike-2.rc")["user"]["pinned"].split(";") if p]
    everyday = pins("everyday")
    assert everyday == [p for p in rc_read(PANEL_RC / "docklike-2.rc")["user"]["pinned"].split(";") if p]
    gaming = pins("gaming")
    for want in ("steam.desktop", "net.lutris.Lutris.desktop", "heroic.desktop",
                 "lindos-minecraft.desktop", "lindos-roblox.desktop"):
        assert want in gaming, want
    work = pins("work")
    assert "libreoffice-writer.desktop" in work and "thunderbird.desktop" in work
    creator = pins("creator")
    assert "com.usebottles.bottles.desktop" in creator and "gimp.desktop" in creator
    lite = pins("lite")
    assert len(lite) <= 4 and "lindos-settings.desktop" in lite
    assert rc_read(MODES / "lite" / "panel" / "docklike-2.rc")["user"]["showPreviews"] == "false"


def test_everyday_mode_panel_equals_system_default() -> None:
    assert (MODES / "everyday/panel/docklike-2.rc").read_text(encoding="utf-8") == \
        (PANEL_RC / "docklike-2.rc").read_text(encoding="utf-8")
    assert (MODES / "everyday/panel/whiskermenu-1.rc").read_text(encoding="utf-8") == \
        (PANEL_RC / "whiskermenu-1.rc").read_text(encoding="utf-8")
    sys_props = {k: (value(v), array_values(v)) for k, v in props(parse(XFCONF / "xfce4-panel.xml")).items()}
    mode_props = {k: (value(v), array_values(v)) for k, v in props(parse(MODES / "everyday/panel/xfce4-panel.xml")).items()}
    assert sys_props == mode_props


# ----------------------------------------------------------------------------- packer
def test_panel_profile_pack_roundtrip(tmp_path: Path) -> None:
    pk = load_packer()
    out = tmp_path / "panel.tar.bz2"
    lines, warnings = pk.pack(str(MODES / "gaming" / "panel"), str(out))
    assert out.is_file()
    assert not any("failed" in w for w in warnings)
    with tarfile.open(str(out), "r:bz2") as tar:
        names = tar.getnames()
        assert "config.txt" in names
        assert "whiskermenu-1.rc" in names and "docklike-2.rc" in names
        member = tar.extractfile("config.txt")
        assert member is not None
        text = member.read().decode("utf-8")
    got = dict(line.split(" ", 1) for line in text.splitlines() if line.strip())
    assert got["/panels"] == "[<1>]"
    assert got["/panels/panel-1/size"] == "uint32 48"
    assert got["/panels/panel-1/position"] == "'p=10;x=0;y=0'"
    assert got["/panels/panel-1/position-locked"] == "true"
    assert got["/panels/panel-1/plugin-ids"] == "[<3>, <1>, <2>, <4>, <5>, <6>, <7>, <8>, <9>, <10>]"
    assert got["/panels/panel-1/background-rgba"] == "[<0.125>, <0.125>, <0.125>, <0.85>]"
    assert got["/plugins/plugin-1"] == "'whiskermenu'"
    assert got["/plugins/plugin-9/digital-time-format"] == "'%H:%M'"
    assert "/plugins" not in got and "/panels/panel-1" not in got  # containers are not values
    assert lines == sorted(lines)
    ok, problems = pk.check(str(out))
    assert ok, problems


def test_gvariant_text_serialisation() -> None:
    pk = load_packer()
    V = pk.Value
    assert pk.gvariant_text(V("s", "it's")) == '"it\'s"'
    assert pk.gvariant_text(V("s", "a\\b\n")) == "'a\\\\b\\n'"
    assert pk.gvariant_text(V("u", 5)) == "uint32 5"
    assert pk.gvariant_text(V("i", -3)) == "-3"
    assert pk.gvariant_text(V("b", True)) == "true"
    assert pk.gvariant_text(V("d", 1.0)) == "1.0"
    assert pk.gvariant_text(V("t", 7)) == "uint64 7"
    assert pk.gvariant_text(V("av", [])) == "@av []"
    assert pk.gvariant_text(V("av", [V("s", "x"), V("u", 2)])) == "[<'x'>, <uint32 2>]"


def test_pack_all_modes(tmp_path: Path) -> None:
    import shutil
    pk = load_packer()
    work = tmp_path / "modes"
    shutil.copytree(str(MODES), str(work))
    assert pk.pack_all(str(work)) == 0
    for mode in MODE_IDS:
        assert (work / mode / "panel.tar.bz2").is_file()


# ----------------------------------------------------------------------------- desktop files
@pytest.mark.parametrize("name,exec_prefix,display_name", [
    ("lindos-files.desktop", "thunar", "File Explorer"),
    ("lindos-settings.desktop", "lindos-settings", "Settings"),
    ("lindos-store.desktop", "mintinstall", "Store"),
    ("lindos-terminal.desktop", "xfce4-terminal", "Terminal"),
])
def test_desktop_shims(name: str, exec_prefix: str, display_name: str) -> None:
    entry = desktop_entries(APPS / name)
    assert entry["Type"] == "Application"
    assert entry["Name"] == display_name
    assert entry["Exec"].split()[0] == exec_prefix
    assert entry.get("Icon")


@pytest.mark.parametrize("name,exec_cmd", [
    ("lindos-setup.desktop", "lindos-setup --first-run"),
    ("lindos-picom.desktop", "lindos-compositor start"),
    ("lindos-mode-apply-user.desktop", "/usr/libexec/lindos/first-login-panel.sh"),
])
def test_autostart_entries(name: str, exec_cmd: str) -> None:
    entry = desktop_entries(AUTOSTART / name)
    assert entry["Exec"] == exec_cmd
    assert entry["OnlyShowIn"] == "XFCE;"
    if name == "lindos-setup.desktop":
        assert entry["X-GNOME-Autostart-Delay"] == "2"
    if name == "lindos-mode-apply-user.desktop":
        assert entry["X-GNOME-Autostart-Phase"] == "Initialization"


# ----------------------------------------------------------------------------- art / branding
def test_wallpapers_and_icons() -> None:
    bg = ROOT / "usr/share/backgrounds/lindos"
    for name in ("aurora-dark", "aurora-light", "bloom-blue", "mist-purple", "nightfall"):
        root = parse(bg / f"{name}.svg")
        vb = [float(x) for x in (root.get("viewBox") or "").split()]
        assert vb[2] >= 3840 and vb[3] >= 2160, name
    for rel in ("usr/share/pixmaps/lindos-logo.svg",
                "usr/share/icons/hicolor/scalable/apps/lindos-start.svg",
                "usr/share/icons/hicolor/scalable/apps/lindos-settings.svg",
                "usr/share/icons/hicolor/symbolic/apps/lindos-start-symbolic.svg",
                "usr/share/icons/hicolor/24x24/apps/lindos-start.svg",
                "usr/share/plymouth/themes/lindos/logo.svg"):
        parse(ROOT / rel)
    # lindos-exe.svg belongs to lindos-compat, lindos-roblox.svg to lindos-gaming: not shipped twice
    assert not (ROOT / "usr/share/icons/hicolor/scalable/apps/lindos-exe.svg").exists()


def test_branding_files() -> None:
    assert (ROOT / "etc/lindos-release").read_text(encoding="utf-8").strip() == "Lindos 1.0.0 (Aurora)"
    frag = (ROOT / "usr/share/lindos/os-release.d/lindos.conf").read_text(encoding="utf-8")
    pairs = dict(line.split("=", 1) for line in frag.splitlines() if line and not line.startswith("#"))
    assert pairs["NAME"] == '"Lindos"'
    assert pairs["PRETTY_NAME"] == '"Lindos 1.0 (Aurora)"'
    assert pairs["HOME_URL"] == '"https://lindos.dev"'
    assert pairs["LINDOS_VERSION"] == "1.0.0"
    assert pairs["LINDOS_CODENAME"] == "Aurora"
    for forbidden in ("ID", "ID_LIKE", "VERSION_CODENAME", "UBUNTU_CODENAME"):
        assert forbidden not in pairs, forbidden
    ply = (ROOT / "usr/share/plymouth/themes/lindos/lindos.plymouth").read_text(encoding="utf-8")
    assert "ModuleName=script" in ply and "lindos.script" in ply
    greeter = (ROOT / "etc/lightdm/slick-greeter.conf").read_text(encoding="utf-8")
    assert "aurora-dark.svg" in greeter and "theme-name=Lindos-Dark" in greeter
    lightdm = (ROOT / "etc/lightdm/lightdm.conf.d/50-lindos.conf").read_text(encoding="utf-8")
    assert "greeter-hide-users=false" in lightdm
    css = (ROOT / "usr/share/lindos/gtk-3.0/lindos.css").read_text(encoding="utf-8")
    assert "@define-color lindos_accent #60CDFF;" in css
    fonts = (ROOT / "etc/fonts/conf.d/60-lindos-ui.conf").read_text(encoding="utf-8")
    assert "Segoe UI" in fonts and "Selawik" in fonts
    picom = (ROOT / "etc/xdg/picom-lindos.conf").read_text(encoding="utf-8")
    for needle in ('backend = "glx"', "vsync = true", "corner-radius = 8", "fade-in-step = 0.03"):
        assert needle in picom, needle


# ----------------------------------------------------------------------------- scripts / DEBIAN
def test_scripts_shebangs_and_lf() -> None:
    scripts = [ROOT / "usr/bin/lindos-compositor", LIBEXEC / "apply-branding.sh",
               LIBEXEC / "build-panel-profiles.sh", LIBEXEC / "first-login-panel.sh",
               DEBIAN / "preinst", DEBIAN / "postinst", DEBIAN / "postrm",
               PKG.parent.parent / "build" / "fetch-assets.sh"]
    for s in scripts:
        raw = s.read_bytes()
        assert b"\r\n" not in raw, f"{s.name}: CRLF"
        first = raw.split(b"\n", 1)[0]
        assert first in (b"#!/bin/sh", b"#!/bin/bash"), f"{s.name}: {first!r}"
        text = raw.decode("utf-8")
        # no privilege escalation inside scripts (callers use pkexec/sudo, SPEC §12)
        assert re.search(r"^\s*(sudo|pkexec)\s", text, flags=re.M) is None, f"{s.name}: sudo inside script"
        if first == b"#!/bin/bash":
            assert "set -Eeuo pipefail" in text, s.name
        else:
            assert "set -e" in text, s.name
    for py in ("panel-profile-pack.py", "plymouth-gen-assets.py"):
        raw = (LIBEXEC / py).read_bytes()
        assert raw.startswith(b"#!/usr/bin/env python3")
        assert b"\r\n" not in raw


def test_debian_metadata() -> None:
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    fields = dict(re.findall(r"^([A-Z][A-Za-z-]+): (.*)$", control, flags=re.M))
    assert fields["Package"] == "lindos-desktop"
    assert fields["Version"] == "1.0.0"
    assert fields["Architecture"] == "all"
    assert fields["Maintainer"] == "Lindos Team <team@lindos.dev>"
    depends = {d.strip().split()[0] for d in fields["Depends"].split(",")}
    for pkg in ("xfce4-panel", "xfce4-whiskermenu-plugin", "xfwm4", "xfconf",
                "xfce4-settings", "xfce4-notifyd", "picom", "xfce4-panel-profiles", "xfce4-clipman-plugin",
                "xfce4-screenshooter", "xfce4-taskmanager", "lightdm", "slick-greeter", "plymouth",
                "fontconfig", "lindos-core"):
        assert pkg in depends, pkg
    # xfce4-docklike-plugin is deliberately NOT a hard Depends: it is not packaged for Ubuntu
    # 24.04 "noble" (only Ubuntu 25.10+ / Debian trixie+ carry it as of this writing), so a hard
    # Depends would make lindos-desktop uninstallable on the very base Lindos targets. It is a
    # Recommends instead — apt still tries to install it, and xfce4-panel just leaves that panel
    # slot empty if it is genuinely absent.
    assert "xfce4-docklike-plugin" not in depends
    recommends = {d.strip().split()[0] for d in fields["Recommends"].split(",")}
    for pkg in ("xfce4-docklike-plugin", "xfce4-pulseaudio-plugin", "xfce4-power-manager",
                "network-manager-gnome", "librsvg2-bin", "fonts-noto-color-emoji"):
        assert pkg in recommends, pkg
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    for forbidden in ("curl ", "wget ", "git clone", "apt-get install"):
        assert forbidden not in postinst, forbidden
    conffiles = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    for c in conffiles:
        assert c.startswith("/etc/")
        assert (ROOT / c.lstrip("/")).is_file(), c
    shipped_etc = sorted("/" + p.relative_to(ROOT).as_posix() for p in (ROOT / "etc").rglob("*") if p.is_file())
    assert sorted(conffiles) == shipped_etc


def test_postinst_ensures_graphical_boot() -> None:
    # Regression (boot-test CI run 36300817476): a built ISO's live session sat at a bare text
    # VT forever -- `systemctl is-system-running` reported "running" with zero failed units,
    # yet lightdm.service stayed "inactive (dead)" with not one log line ever written for it,
    # consistent with graphical.target never being the active default target. lindos-desktop
    # owns the desktop experience, so its postinst must not depend on some other package having
    # already gotten this right -- it must set both explicitly itself.
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "systemctl set-default graphical.target" in postinst, (
        "postinst must explicitly set the default systemd target to graphical.target -- do "
        "not rely on inherited state from the base image (CI run 36300817476)"
    )
    assert "systemctl enable lightdm.service" in postinst, (
        "postinst must explicitly enable lightdm.service -- do not rely on inherited state "
        "from the base image (CI run 36300817476)"
    )
    # Both calls must be guarded (offline/chroot-safe: no bus contact, never fail the install)
    # and must never pass the flag that would also try to start/stop against a running PID1
    # (no running instance to act on at ISO build time; redundant on a real install too, where
    # a reboot follows). Checked against the actual invocation lines, not the file as a whole,
    # since this very explanation is itself allowed to name that flag in prose.
    assert "command -v systemctl" in postinst
    assert "set-default graphical.target --now" not in postinst
    assert "enable lightdm.service --now" not in postinst


def test_lightdm_has_a_start_timeout() -> None:
    # Regression (boot-test run 36319809802): lightdm.service (and plymouth-quit-wait.service
    # alongside it) was found stuck "Starting" forever -- 25+ real minutes under real KVM, the
    # display never initialized, and no operator-visible failure. Upstream's lightdm.service
    # ships no TimeoutStartSec=, so a wedged Xorg/display-manager startup (a real-hardware risk,
    # not just a QEMU one) hangs the boot indefinitely. graphical.target only Wants (not
    # Requires) display-manager.service, so lightdm timing out and failing here still lets the
    # rest of the boot (and CI's own systemd.run= smoke test / desktop-watch unit, which is
    # otherwise gated behind default.target settling) proceed instead of hanging forever too.
    conf_rel = "etc/systemd/system/lightdm.service.d/lindos-timeout.conf"
    conf = (ROOT / conf_rel).read_text(encoding="utf-8")
    assert "[Service]" in conf
    m = re.search(r"^TimeoutStartSec=(\S+)$", conf, flags=re.M)
    assert m, "lindos-timeout.conf must set TimeoutStartSec="
    # a bounded, human-scale timeout -- long enough for a slow-but-genuinely-progressing start,
    # short enough that CI (and a real user) gets a definite answer instead of waiting forever
    assert m.group(1).endswith("s")
    assert 30 <= int(m.group(1).rstrip("s")) <= 300
    assert "/" + conf_rel in (DEBIAN / "conffiles").read_text(encoding="utf-8").split()


def test_skel_readme_honesty() -> None:
    text = (ROOT / "etc/skel/.config/lindos/README").read_text(encoding="utf-8")
    assert "not" in text and "Windows" in text and "Wine" in text
    assert "config.json" in text and "setup-done" in text
