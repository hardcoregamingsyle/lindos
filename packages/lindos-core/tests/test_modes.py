"""lindos.modes: shipped definitions, plans, apply_mode (dry-run and tool-less) (SPEC §3, §4.3)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from lindos import config as lconfig
from lindos import helper as lhelper
from lindos import modes, paths

SPEC_ORDER = ["everyday", "gaming", "work", "creator", "lite"]
DESKTOP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.desktop$")


# --- shipped definitions -------------------------------------------------------------------
def test_shipped_modes_load_in_spec_order(modes_dir: Path) -> None:
    loaded = modes.load_modes(str(modes_dir))
    assert list(loaded) == SPEC_ORDER == modes.MODE_IDS
    names = {m.id: m.name for m in loaded.values()}
    assert names == {"everyday": "Everyday", "gaming": "Gaming", "work": "Work", "creator": "Creator", "lite": "Lite"}


def test_shipped_modes_have_sane_values(modes_dir: Path) -> None:
    loaded = modes.load_modes(str(modes_dir))
    for mode in loaded.values():
        assert mode.description and mode.icon and mode.ram_hint, mode.id
        assert mode.governor in modes.GOVERNORS
        assert mode.compositor in modes.COMPOSITORS
        assert 0 <= mode.zram_percent <= 200
        assert mode.pins and all(DESKTOP_RE.match(p) for p in mode.pins), mode.pins
        assert "lindos-files.desktop" in mode.pins and "firefox.desktop" in mode.pins
        for pkg in mode.packages:
            assert lhelper.PACKAGE_RE.match(pkg), (mode.id, pkg)
        for fid in mode.flatpaks:
            assert lhelper.FLATPAK_RE.match(fid), (mode.id, fid)
        for unit in mode.services_disable + mode.services_enable:
            assert lhelper.unit_allowed(unit), (mode.id, unit)
        assert not (set(mode.services_disable) & set(mode.services_enable))
        for key, value in mode.sysctl.items():
            assert lhelper.SYSCTL_KEY_RE.match(key) and lhelper.SYSCTL_VALUE_RE.match(value), (mode.id, key)
        assert os.path.isfile(os.path.join(mode.path, "mode.json"))


def test_spec_key_differences(modes_dir: Path) -> None:
    m = modes.load_modes(str(modes_dir))
    assert m["everyday"].governor == "schedutil" and m["everyday"].compositor == "picom"
    assert m["gaming"].governor == "performance" and m["gaming"].zram_percent == 75
    assert m["gaming"].sysctl.get("vm.max_map_count") == "2147483642"
    assert "ananicy-cpp" in m["gaming"].services_enable
    # desktop ids as shipped: lutris (noble) = net.lutris.Lutris.desktop, Minecraft/Roblox via the
    # lindos-gaming shims; must equal lindos-desktop's modes/gaming/panel/docklike-2.rc
    for pin in ("steam.desktop", "net.lutris.Lutris.desktop", "heroic.desktop", "lindos-minecraft.desktop",
                "lindos-roblox.desktop"):
        assert pin in m["gaming"].pins
    assert "libreoffice-writer.desktop" in m["work"].pins and "thunderbird.desktop" in m["work"].pins
    assert "power-profiles-daemon" in m["work"].services_enable
    assert "com.usebottles.bottles" in m["creator"].flatpaks
    assert m["lite"].compositor == "none" and m["lite"].zram_percent == 100
    assert m["lite"].extra.get("earlyoom_min_percent", 0) > 4
    assert "lindos-settings.desktop" in m["lite"].pins


def test_mode_json_files_are_valid_json_with_exact_ids(modes_dir: Path) -> None:
    for mid in SPEC_ORDER:
        with open(modes_dir / mid / "mode.json", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["id"] == mid
        for key in ("id", "name", "description", "icon", "packages", "flatpaks", "services_disable",
                    "services_enable", "sysctl", "governor", "pins", "compositor", "zram_percent"):
            assert key in data, (mid, key)


# --- Mode.from_dict validation ------------------------------------------------------------
def test_from_dict_validation() -> None:
    good = modes.Mode.from_dict({"id": "x", "name": "X", "sysctl": {"vm.a": 1, "vm.b": True}, "zram_percent": 10})
    assert good.sysctl == {"vm.a": "1", "vm.b": "1"} and good.governor == "schedutil"
    with pytest.raises(ValueError):
        modes.Mode.from_dict({"id": "Bad Id"})
    with pytest.raises(ValueError):
        modes.Mode.from_dict({"id": "x", "governor": "turbo"})
    with pytest.raises(ValueError):
        modes.Mode.from_dict({"id": "x", "compositor": "wayfire"})
    with pytest.raises(ValueError):
        modes.Mode.from_dict({"id": "x", "zram_percent": "50"})
    with pytest.raises(ValueError):
        modes.Mode.from_dict({"id": "x", "packages": "gimp"})
    assert modes.Mode.from_dict({"id": "x", "extra_key": 5}).extra == {"extra_key": 5}
    assert modes.Mode.from_dict({"id": "x", "tagline": "t"}).to_dict()["tagline"] == "t"


def test_load_modes_skips_broken_entries(tmp_path: Path) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "mode.json").write_text('{"id": "ok", "name": "Ok"}', encoding="utf-8")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "mode.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wrongid").mkdir()
    (tmp_path / "wrongid" / "mode.json").write_text('{"id": "other"}', encoding="utf-8")
    loaded = modes.load_modes(str(tmp_path))
    assert set(loaded) == {"ok", "wrongid"}
    assert modes.load_modes(str(tmp_path / "missing")) == {}
    with pytest.raises(KeyError):
        modes.get_mode("nope", str(tmp_path))


# --- plan ---------------------------------------------------------------------------------
def test_build_system_plan_is_valid_helper_payload(modes_dir: Path) -> None:
    for mode in modes.load_modes(str(modes_dir)).values():
        plan = modes.build_system_plan(mode, set_system_default=True)
        json.dumps(plan)
        assert plan["mode"] == mode.id and plan["schema"] == modes.PLAN_SCHEMA
        assert plan["set_system_default"] is True and plan["apply_system"] is None
        validated = lhelper.validate_payload("apply-mode", plan)
        assert validated["governor"] == mode.governor
        assert validated["zram_percent"] == mode.zram_percent
        assert validated["packages"] == mode.packages
        assert validated["sysctl"] == mode.sysctl


def test_plan_offline_flag(modes_dir: Path) -> None:
    mode = modes.get_mode("lite", str(modes_dir))
    plan = modes.build_system_plan(mode, offline=True)
    assert plan["offline"] is True


def test_plan_install_flag_defaults_to_true_and_can_be_turned_off(modes_dir: Path) -> None:
    mode = modes.get_mode("gaming", str(modes_dir))
    default = modes.build_system_plan(mode)
    assert default["install"] is True
    off = modes.build_system_plan(mode, install=False)
    assert off["install"] is False
    # only the flag differs: the plan still describes the whole mode
    assert {k: v for k, v in off.items() if k != "install"} == {k: v for k, v in default.items() if k != "install"}
    assert lhelper.validate_payload("apply-mode", off)["install"] is False
    assert lhelper.validate_payload("apply-mode", default)["install"] is True


def test_system_plan_passes_install_through(core_env) -> None:
    assert modes.system_plan("creator")["install"] is True
    assert modes.system_plan("creator", install=False)["install"] is False
    assert modes.system_plan("creator", system=True, offline=True, install=False) ==         modes.build_system_plan(modes.get_mode("creator"), set_system_default=True, offline=True, install=False)
    with pytest.raises(KeyError):
        modes.system_plan("turbo", install=False)


# --- apply_mode ---------------------------------------------------------------------------
def test_apply_mode_install_flag_reaches_the_helper_plan(core_env) -> None:
    def system_step(**kwargs):
        result = modes.apply_mode("gaming", dry_run=True, log=lambda _m: None, **kwargs)
        message = next(s[2] for s in result.steps if s[0] == "system")
        return json.loads(message.split("with plan: ", 1)[1])

    assert system_step()["install"] is True                  # 'lindos-mode set' / Settings keep installing
    assert system_step(install=False)["install"] is False


def test_apply_mode_dry_run(core_env) -> None:
    lines = []
    result = modes.apply_mode("gaming", dry_run=True, log=lines.append)
    assert isinstance(result, modes.ApplyResult)
    assert result.ok, result.summary()
    names = [s[0] for s in result.steps]
    assert names[0] == "load-mode" and "write-user-config" in names and "system" in names
    assert "compositor" in names and "apply-user-script" in names
    assert any("would call helper apply-mode" in s[2] for s in result.steps if s[0] == "system")
    assert not Path(paths.user_conf()).exists()          # dry run writes nothing
    assert any("dry run" in ln for ln in lines)
    d = result.to_dict()
    assert d["ok"] is True and json.dumps(d)


def test_apply_mode_unknown_mode(core_env) -> None:
    result = modes.apply_mode("turbo", log=lambda _m: None)
    assert result.ok is False
    assert result.steps[0][0] == "load-mode" and result.steps[0][1] is False


def test_apply_mode_without_tools_records_skips(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No xfce4-panel-profiles / xfconf-query / lindos-compositor on the test host → skipped steps,
    the user config is still written and the helper (dry-run) is called."""
    monkeypatch.setattr(modes.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(lhelper.shutil, "which", lambda name, *a, **k: None)
    result = modes.apply_mode("lite", log=lambda _m: None)
    assert result.ok, result.summary()
    steps = {name: (ok, msg) for name, ok, msg in result.steps}
    assert steps["write-user-config"][0] is True
    assert lconfig.Config.load()["mode"] == "lite"
    assert steps["panel-profile"][0] is False and steps["panel-profile"][1].startswith("skipped")
    assert steps["compositor"][0] is False and steps["compositor"][1].startswith("skipped")
    assert steps["pins"][0] is False and steps["pins"][1].startswith("skipped")
    assert steps["system"][0] is True, steps["system"]      # helper ran in dry-run mode
    assert result.skipped and not result.failed
    assert modes.current_mode() == "lite"


def test_apply_mode_runs_apply_user_script_and_panel_xml_fallback(core_env, monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    mode_dir = core_env["modes"] / "work"
    (mode_dir / "panel").mkdir()
    (mode_dir / "panel" / "xfce4-panel.xml").write_text("<channel name='xfce4-panel'/>", encoding="utf-8")
    (mode_dir / "panel" / "docklike-2.rc").write_text("[user]\npinned=firefox.desktop;\n", encoding="utf-8")
    (mode_dir / "apply-user.sh").write_text("#!/bin/bash\necho hello-from-user-script\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, timeout=120, env=None):
        calls.append(cmd)
        return True, "hello-from-user-script"

    monkeypatch.setattr(modes, "_run", fake_run)
    monkeypatch.setattr(modes, "_spawn", lambda cmd: calls.append(cmd) or True)
    monkeypatch.setattr(modes.shutil, "which",
                        lambda name, *a, **k: {"bash": "/bin/bash", "xfce4-panel": "/usr/bin/xfce4-panel"}.get(name))
    result = modes.apply_mode("work", log=lambda _m: None)
    steps = {name: (ok, msg) for name, ok, msg in result.steps}
    assert steps["panel-profile"][0] is True and "copied" in steps["panel-profile"][1]
    assert Path(paths.xfconf_dir(), "xfce4-panel.xml").is_file()
    assert Path(paths.panel_dir(), "docklike-2.rc").is_file()
    assert steps["apply-user-script"][0] is True and "hello-from-user-script" in steps["apply-user-script"][1]
    assert any(cmd[-1].endswith("apply-user.sh") for cmd in calls if isinstance(cmd, list))


def test_apply_result_semantics() -> None:
    r = modes.ApplyResult()
    r.add("a", True, "fine")
    r.add("b", False, "skipped: no tool")
    assert r.ok and r.skipped and not r.failed
    r.add("c", False, "boom")
    assert not r.ok and r.failed[0][0] == "c"
    text = r.summary()
    assert "[skip] b" in text and "[FAIL] c" in text and text.endswith("result: failed")


def test_current_mode_prefers_user_over_system(core_env) -> None:
    lconfig.save_system({"mode": "work"})
    assert modes.current_mode() == "work"
    cfg = lconfig.Config.load()
    cfg["mode"] = "creator"
    cfg.save()
    assert modes.current_mode() == "creator"
