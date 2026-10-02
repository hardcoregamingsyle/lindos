"""lindos-desktop: a taskbar with a window list even though xfce4-docklike-plugin is not packaged for Ubuntu 24.04.

Every shipped layout has Docklike in slot plugin-2.  Without the plugin a fresh session has no window buttons and no
pinned apps, so taskbar-fallback.py rewrites a layout to xfce4-panel's own task list plus one launcher per installed pin,
first-login-panel.sh seeds that layout (everyday mode too) and build-panel-profiles.sh packs it for `lindos-mode set`.
Hermetic: fake roots, stdlib, bash only where a shell script is the thing under test.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
MODES = ROOT / "usr" / "share" / "lindos" / "modes"
DEFAULT_PANEL = ROOT / "etc" / "xdg" / "xfce4"          # xfconf/.../xfce4-panel.xml + panel/*.rc
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
MODE_IDS = ["everyday", "gaming", "work", "creator", "lite"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tf = _load("lindos_taskbar_fallback", LIBEXEC / "taskbar-fallback.py")
packer = _load("lindos_panel_profile_pack_tf", LIBEXEC / "panel-profile-pack.py")


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _rc_pins(panel_dir: Path) -> List[str]:
    return tf.parse_pins((panel_dir / "docklike-2.rc").read_text(encoding="utf-8"))


def _fake_root(tmp: Path, installed: List[str], docklike: bool = False) -> Path:
    root = tmp / "root"
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    for name in installed:
        (apps / name).write_text("[Desktop Entry]\nType=Application\nName=x\nExec=true\n", encoding="utf-8")
    if docklike:
        plug = root / "usr" / "share" / "xfce4" / "panel" / "plugins"
        plug.mkdir(parents=True, exist_ok=True)
        (plug / "docklike.desktop").write_text("[Xfce Panel]\nName=Docklike Taskbar\n", encoding="utf-8")
    return root


def _every_pin() -> List[str]:
    seen: List[str] = []
    for mode in MODE_IDS:
        for pin in _rc_pins(MODES / mode / "panel"):
            if pin not in seen:
                seen.append(pin)
    return seen


def _layout(xml_path: Path) -> Tuple[Dict[int, ET.Element], List[int], ET.Element]:
    root = ET.parse(str(xml_path)).getroot()
    plugins = next(p for p in root.findall("property") if p.get("name") == "plugins")
    table = {int(re.match(r"plugin-(\d+)$", p.get("name")).group(1)): p for p in plugins.findall("property")}
    panels = next(p for p in root.findall("property") if p.get("name") == "panels")
    panel = next(p for p in panels.findall("property") if p.get("name") == "panel-1")
    ids_prop = next(p for p in panel.findall("property") if p.get("name") == "plugin-ids")
    return table, [int(v.get("value")) for v in ids_prop.findall("value")], root


def _types(table: Dict[int, ET.Element], ids: List[int]) -> List[str]:
    return [table[i].get("value") for i in ids]


def _panel_dir(name: str, tmp: Path) -> Path:
    """The panel/ directory of a mode, or the system default (xml and rc files live apart under /etc/xdg) gathered in one."""
    if name != "default":
        return MODES / name / "panel"
    gathered = tmp / "default-panel"
    gathered.mkdir()
    etc = ROOT / "etc" / "xdg" / "xfce4"
    shutil.copyfile(etc / "xfconf" / "xfce-perchannel-xml" / "xfce4-panel.xml", gathered / "xfce4-panel.xml")
    for rc in (etc / "panel").glob("*.rc"):
        shutil.copyfile(rc, gathered / rc.name)
    return gathered


# ----------------------------------------------------------------------------- which kind of taskbar
def test_status_says_docklike_only_when_the_plugin_is_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINDOS_TASKBAR", raising=False)
    assert tf.taskbar_kind(str(_fake_root(tmp_path / "a", [], docklike=False))) == "tasklist"
    assert tf.taskbar_kind(str(_fake_root(tmp_path / "b", [], docklike=True))) == "docklike"
    monkeypatch.setenv("LINDOS_TASKBAR", "tasklist")
    assert tf.taskbar_kind(str(_fake_root(tmp_path / "c", [], docklike=True))) == "tasklist"
    monkeypatch.setenv("LINDOS_TASKBAR", "nonsense")
    assert tf.taskbar_kind(str(_fake_root(tmp_path / "d", [], docklike=True))) == "docklike"


def test_the_cli_prints_the_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("LINDOS_TASKBAR", raising=False)
    assert tf.main(["status", "--root", str(_fake_root(tmp_path, []))]) == 0
    assert capsys.readouterr().out.strip() == "tasklist"


# ----------------------------------------------------------------------------- the rewritten layout
@pytest.mark.parametrize("mode", ["default"] + MODE_IDS)
def test_the_task_list_takes_the_docklike_slot_in_every_shipped_layout(mode: str, tmp_path: Path) -> None:
    panel_dir = _panel_dir(mode, tmp_path)
    root = _fake_root(tmp_path, _every_pin())
    out = tmp_path / "out"
    pins = tf.convert_dir(str(panel_dir), str(out), root=str(root), home=str(tmp_path / "home"))
    assert pins == _rc_pins(panel_dir)

    src_table, src_ids, src_root = _layout(panel_dir / "xfce4-panel.xml")
    table, ids, root_el = _layout(out / "xfce4-panel.xml")
    assert "docklike" not in _types(table, ids) and "docklike" not in (out / "xfce4-panel.xml").read_text(encoding="utf-8").split("-->", 1)[1]
    tasklist = table[2]
    assert tasklist.get("value") == "tasklist"
    props = {p.get("name"): p.get("value") for p in tasklist.findall("property")}
    assert props["show-labels"] == "false" and props["flat-buttons"] == "true" and props["show-handle"] == "false"

    # order: Start, then the pinned launchers, then the task list, then the tray/clock tail; nothing else expands
    types = _types(table, ids)
    first_launcher = types.index("launcher") if "launcher" in types else types.index("tasklist")
    assert types.index("whiskermenu") < first_launcher <= types.index("tasklist")
    assert types[types.index("tasklist") - len(pins):types.index("tasklist")] == ["launcher"] * len(pins)
    assert types[types.index("tasklist") + 1:] == ["systray", "pulseaudio", "power-manager-plugin", "notification-plugin", "clock", "showdesktop"]
    assert len(ids) == len(set(ids)) and all(i in table for i in ids)
    for pid in ids:
        if table[pid].get("value") == "separator":
            assert all(not (p.get("name") == "expand" and p.get("value") == "true") for p in table[pid].findall("property"))
    launchers = [table[i] for i in ids if table[i].get("value") == "launcher"]
    assert [[v.get("value") for v in l.find("property").findall("value")] for l in launchers] == [[p] for p in pins]
    assert all(i > 10 for i in ids if table[i].get("value") == "launcher")

    # everything the swap must not touch is byte-for-byte the shipped configuration
    def config(el: ET.Element) -> List[Tuple[Optional[str], Optional[str], List[Optional[str]]]]:
        return [(p.get("name"), p.get("value"), [v.get("value") for v in p.findall("value")]) for p in el.iter("property")]

    for pid in (1, 5, 6, 7, 8, 9, 10):
        assert config(table[pid]) == config(src_table[pid]), pid

    def panel_props(r: ET.Element) -> List[Tuple[str, Optional[str]]]:
        panels = next(p for p in r.findall("property") if p.get("name") == "panels")
        panel = next(p for p in panels.findall("property") if p.get("name") == "panel-1")
        return [(p.get("name"), p.get("value")) for p in panel.iter("property") if p.get("name") != "plugin-ids"]
    assert panel_props(root_el) == panel_props(src_root)

    # the companion rc files: Whisker's stays, Docklike's is not carried over
    assert (out / "whiskermenu-1.rc").read_bytes() == (panel_dir / "whiskermenu-1.rc").read_bytes()
    assert not list(out.glob("docklike-*.rc"))


def test_only_installed_apps_are_pinned_unless_all_pins_are_asked_for(tmp_path: Path) -> None:
    panel_dir = MODES / "gaming" / "panel"
    every = _rc_pins(panel_dir)
    present = [p for p in every if p.startswith("lindos-")]
    assert 0 < len(present) < len(every)
    root = _fake_root(tmp_path, present)
    got = tf.convert_dir(str(panel_dir), str(tmp_path / "a"), root=str(root), home=str(tmp_path / "home"))
    assert got == present                                                     # order kept, missing apps left out
    assert tf.convert_dir(str(panel_dir), str(tmp_path / "b"), root=str(root), home=str(tmp_path / "home"), all_pins=True) == every
    # flatpak exports and the user's own applications directory count as installed
    home = tmp_path / "home"
    (home / ".local" / "share" / "applications").mkdir(parents=True)
    (home / ".local" / "share" / "applications" / "heroic.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    flat = root / "var" / "lib" / "flatpak" / "exports" / "share" / "applications"
    flat.mkdir(parents=True)
    (flat / "net.lutris.Lutris.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    got = tf.convert_dir(str(panel_dir), str(tmp_path / "c"), root=str(root), home=str(home))
    assert "heroic.desktop" in got and "net.lutris.Lutris.desktop" in got and "steam.desktop" not in got


def test_a_layout_without_docklike_is_copied_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    xml = ('<?xml version="1.0"?>\n<channel name="xfce4-panel" version="1.0">\n <property name="plugins" type="empty">\n'
           '  <property name="plugin-1" type="string" value="whiskermenu"/>\n </property>\n'
           ' <property name="panels" type="array"><value type="int" value="1"/>\n  <property name="panel-1" type="empty">\n'
           '   <property name="plugin-ids" type="array"><value type="int" value="1"/></property>\n  </property>\n </property>\n</channel>\n')
    (src / "xfce4-panel.xml").write_text(xml, encoding="utf-8")
    (src / "docklike-2.rc").write_text("[user]\npinned=a.desktop;\n", encoding="utf-8")
    assert tf.convert_dir(str(src), str(tmp_path / "out"), root=str(tmp_path)) == []
    assert (tmp_path / "out" / "xfce4-panel.xml").read_text(encoding="utf-8") == xml
    assert (tmp_path / "out" / "docklike-2.rc").is_file()


def test_the_rewritten_layout_packs_into_a_valid_panel_profile(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, _every_pin())
    out = tmp_path / "out"
    tf.convert_dir(str(MODES / "gaming" / "panel"), str(out), root=str(root), home=str(tmp_path / "home"))
    tarball = tmp_path / "panel.tar.bz2"
    _, warnings = packer.pack(str(out), str(tarball))
    assert not [w for w in warnings if "docklike" in w], warnings
    ok, problems = packer.check(str(tarball))
    assert ok, problems
    with tarfile.open(str(tarball), "r:bz2") as tar:
        config = tar.extractfile("config.txt").read().decode("utf-8")
        assert "whiskermenu-1.rc" in tar.getnames() and not [n for n in tar.getnames() if "docklike" in n]
    assert "/plugins/plugin-2 'tasklist'" in config and "docklike" not in config
    assert "/plugins/plugin-11 'launcher'" in config and "lindos-files.desktop" in config


def test_the_cli_reports_a_broken_layout(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "xfce4-panel.xml").write_text("<channel", encoding="utf-8")
    assert tf.main(["convert", str(tmp_path / "bad"), str(tmp_path / "out")]) == 1
    assert tf.main(["convert", str(tmp_path / "missing"), str(tmp_path / "out")]) == 1


# ----------------------------------------------------------------------------- first-login-panel.sh
def _shim(tmp: Path) -> Path:
    """A python3 on PATH for the scripts (Git Bash on Windows has none): runs this interpreter."""
    d = tmp / "shim"
    d.mkdir(exist_ok=True)
    (d / "python3").write_text('#!/bin/sh\nexec "%s" "$@"\n' % _posix(Path(sys.executable)), encoding="utf-8", newline="\n")
    return d


def _env(tmp: Path, kind: str, installed: List[str]) -> Dict[str, str]:
    env = dict(os.environ)
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "HOME": _posix(home), "XDG_CONFIG_HOME": _posix(home / ".config"), "XDG_STATE_HOME": _posix(home / ".state"),
        "LINDOS_HOME": str(home), "LINDOS_ROOT": str(_fake_root(tmp, installed)), "LINDOS_TASKBAR": kind,
        "LINDOS_MODES_DIR": _posix(MODES), "PATH": str(_shim(tmp)) + os.pathsep + env.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("LINDOS_TASKBAR_FALLBACK", None)
    return env


def _login(env: Dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, _posix(LIBEXEC / "first-login-panel.sh"), *args], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, check=False)


def _user_files(tmp: Path) -> Tuple[Path, Path, Path]:
    conf = tmp / "home" / ".config"
    return conf / "xfce4" / "xfconf" / "xfce-perchannel-xml" / "xfce4-panel.xml", conf / "xfce4" / "panel", conf / "lindos" / "panel-init.done"


@needs_bash
def test_without_docklike_a_fresh_user_gets_the_task_list_even_in_everyday_mode(tmp_path: Path) -> None:
    env = _env(tmp_path, "tasklist", _every_pin())
    res = _login(env)
    assert res.returncode == 0, res.stderr
    xml, panel, stamp = _user_files(tmp_path)
    table, ids, _ = _layout(xml)
    assert table[2].get("value") == "tasklist" and "launcher" in _types(table, ids)
    assert (panel / "whiskermenu-1.rc").is_file() and not (panel / "docklike-2.rc").exists()
    assert "taskbar=tasklist" in stamp.read_text(encoding="utf-8")
    before = xml.read_bytes()
    xml.write_bytes(before + b"<!-- edited by the user -->\n")
    assert _login(env).returncode == 0                       # the stamp says it is done: nothing is touched again
    assert xml.read_bytes() == before + b"<!-- edited by the user -->\n"


@needs_bash
def test_with_docklike_everyday_keeps_the_system_default_and_the_other_modes_copy_the_shipped_layout(tmp_path: Path) -> None:
    env = _env(tmp_path, "docklike", [])
    assert _login(env).returncode == 0
    xml, panel, stamp = _user_files(tmp_path)
    assert not xml.exists()                                      # everyday: the /etc/xdg default is the layout
    assert (panel / "docklike-2.rc").is_file() and (panel / "whiskermenu-1.rc").is_file()
    assert "taskbar=docklike" in stamp.read_text(encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    env = _env(other, "docklike", [])
    assert _login(env, "--mode", "gaming").returncode == 0
    xml, _, _ = _user_files(other)
    assert xml.read_bytes() == (MODES / "gaming" / "panel" / "xfce4-panel.xml").read_bytes()


@needs_bash
def test_a_stamp_from_before_the_fallback_is_redone_but_never_over_an_existing_user_layout(tmp_path: Path) -> None:
    env = _env(tmp_path, "tasklist", _every_pin())
    xml, panel, stamp = _user_files(tmp_path)
    stamp.parent.mkdir(parents=True)
    stamp.write_text("mode=everyday\ndate=2026-09-28T10:00:00Z\nfiles=0\n", encoding="utf-8")       # written by the old script
    assert _login(env).returncode == 0
    assert _layout(xml)[0][2].get("value") == "tasklist" and "taskbar=tasklist" in stamp.read_text(encoding="utf-8")
    # a user who already has a panel layout keeps it; only the stamp moves on
    other = tmp_path / "other"
    other.mkdir()
    env = _env(other, "tasklist", _every_pin())
    xml, _, stamp = _user_files(other)
    xml.parent.mkdir(parents=True)
    xml.write_text("<channel name='xfce4-panel'/>\n", encoding="utf-8")
    stamp.parent.mkdir(parents=True)
    stamp.write_text("mode=everyday\ndate=x\nfiles=0\n", encoding="utf-8")
    assert _login(env).returncode == 0
    assert xml.read_text(encoding="utf-8") == "<channel name='xfce4-panel'/>\n" and "taskbar=tasklist" in stamp.read_text(encoding="utf-8")


@needs_bash
def test_when_the_conversion_fails_the_shipped_layout_is_kept_and_the_next_login_tries_again(tmp_path: Path) -> None:
    stub = tmp_path / "stub.py"
    stub.write_text("import sys\nprint('tasklist') if sys.argv[1] == 'status' else sys.exit(1)\n", encoding="utf-8")
    env = _env(tmp_path, "tasklist", [])
    env["LINDOS_TASKBAR_FALLBACK"] = _posix(stub)
    assert _login(env, "--mode", "work").returncode == 0
    xml, _, stamp = _user_files(tmp_path)
    assert xml.read_bytes() == (MODES / "work" / "panel" / "xfce4-panel.xml").read_bytes()
    assert "taskbar=docklike" in stamp.read_text(encoding="utf-8")          # differs from 'tasklist': retried next time
    # a missing tool cannot tell the kind either: the shipped layout, as before the fallback existed
    other = tmp_path / "other"
    other.mkdir()
    env = _env(other, "tasklist", [])
    env["LINDOS_TASKBAR_FALLBACK"] = _posix(tmp_path / "does-not-exist.py")
    assert _login(env).returncode == 0
    assert not _user_files(other)[0].exists() and "taskbar=docklike" in _user_files(other)[2].read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- build-panel-profiles.sh
def _build(tmp: Path, kind: str, *args: str) -> subprocess.CompletedProcess:
    env = _env(tmp, kind, _every_pin())
    return subprocess.run([BASH, _posix(LIBEXEC / "build-panel-profiles.sh"), "--no-verify", "--modes-dir", _posix(tmp / "modes"), *args],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)


def _config(tarball: Path) -> str:
    with tarfile.open(str(tarball), "r:bz2") as tar:
        return tar.extractfile("config.txt").read().decode("utf-8")


@needs_bash
def test_the_mode_profiles_are_packed_from_the_kind_of_taskbar_the_system_has(tmp_path: Path) -> None:
    shutil.copytree(MODES, tmp_path / "modes")
    res = _build(tmp_path, "tasklist")
    assert res.returncode == 0, res.stderr
    for mode in MODE_IDS:
        tarball = tmp_path / "modes" / mode / "panel.tar.bz2"
        ok, problems = packer.check(str(tarball))
        assert ok, (mode, problems)
        assert "/plugins/plugin-2 'tasklist'" in _config(tarball) and "docklike" not in _config(tarball), mode
        assert (tmp_path / "modes" / mode / ".panel-taskbar").read_text(encoding="utf-8").strip() == "tasklist"
    assert "0 built, 5 up to date" in _build(tmp_path, "tasklist").stderr
    # the plugin turned up (or was forced): the profiles are rebuilt from the shipped Docklike layouts
    res = _build(tmp_path, "docklike")
    assert res.returncode == 0, res.stderr
    for mode in MODE_IDS:
        assert "/plugins/plugin-2 'docklike'" in _config(tmp_path / "modes" / mode / "panel.tar.bz2"), mode
        assert (tmp_path / "modes" / mode / ".panel-taskbar").read_text(encoding="utf-8").strip() == "docklike"


# ----------------------------------------------------------------------------- packaging
def test_the_tool_is_shipped_executable_documented_and_removed_with_the_package() -> None:
    raw = (LIBEXEC / "taskbar-fallback.py").read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3") and b"\r" not in raw
    postinst = (PKG / "DEBIAN" / "postinst").read_text(encoding="utf-8")
    assert "/usr/libexec/lindos/taskbar-fallback.py" in postinst
    assert ".panel-taskbar" in (PKG / "DEBIAN" / "postrm").read_text(encoding="utf-8")
    control = (PKG / "DEBIAN" / "control").read_text(encoding="utf-8")
    assert "task list" in control and "nothing else about the desktop depends on it" not in control


def test_the_theme_code_does_not_toggle_a_separator_when_the_task_list_fills_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    core = PKG.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"
    monkeypatch.syspath_prepend(str(core))
    from lindos import theme

    set_calls: List[Tuple[str, str]] = []
    plugins = {1: "whiskermenu", 3: "separator", 2: "tasklist", 5: "systray"}
    monkeypatch.setattr(theme, "xfconf_available", lambda: True)
    monkeypatch.setattr(theme, "_panel_plugin_ids", lambda panel="panel-1": list(plugins))
    monkeypatch.setattr(theme, "_plugin_type", lambda pid: plugins[pid])
    monkeypatch.setattr(theme, "xfconf_set", lambda channel, prop, value, vtype="string": set_calls.append((prop, str(value))) or True)
    monkeypatch.setattr(theme, "_save_config", lambda **kw: None)
    assert theme.set_taskbar_alignment("center") is True and set_calls == []
    plugins[2] = "docklike"                                    # with Docklike the leading separator still does the centring
    assert theme.set_taskbar_alignment("center") is True
    assert ("/plugins/plugin-3/expand", "True") in set_calls
