"""Tests for lindos_setup.core: the guarded bridge to lindos-core.

A fake ``lindos`` package is injected into ``sys.modules`` so the real
executors can be exercised on any OS; the fake records every call so we can
check the SPEC §13 call map (config -> theme -> helper actions).
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from lindos_setup import core
from lindos_setup.plan import Runner, Selections, build_plan, load_catalog

HERE = os.path.dirname(os.path.abspath(__file__))
APPS_JSON = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "share", "lindos", "setup", "apps.json"))


class _FakeConfig:
    saved = []
    store = {}

    @classmethod
    def load(cls):
        inst = cls()
        inst.data = dict(cls.store)
        return inst

    def set(self, key, value):
        if key == "location_services":
            raise KeyError("unknown key")   # simulate a strict config for one key
        self.data[key] = value

    def __setitem__(self, key, value):
        raise KeyError("unknown key")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self):
        _FakeConfig.store = dict(self.data)
        _FakeConfig.saved.append(dict(self.data))


def _install_fake_lindos(monkeypatch, tmp_path, calls, *, helper_ok=True, install_ok=True):
    pkg = types.ModuleType("lindos")
    pkg.__path__ = []  # mark as package

    paths = types.ModuleType("lindos.paths")
    paths.SETUP_DONE = "~/.config/lindos/setup-done"
    paths.LOG_DIR = "~/.local/state/lindos"
    paths.USER_CONF_DIR = "~/.config/lindos"
    paths.SHARE_DIR = "/usr/share/lindos"

    config = types.ModuleType("lindos.config")
    _FakeConfig.saved = []
    _FakeConfig.store = {}
    config.Config = _FakeConfig

    theme = types.ModuleType("lindos.theme")
    for name in ("set_dark", "set_accent", "set_wallpaper", "set_taskbar_alignment"):
        theme.__dict__[name] = (lambda n: (lambda *a: calls.append((n,) + a)))(name)
    theme.list_wallpapers = lambda: ["/usr/share/backgrounds/lindos/aurora-dark.svg"]

    modes = types.ModuleType("lindos.modes")

    def apply_mode(mode_id, *, system=False, dry_run=False, log=print):
        calls.append(("apply_mode", mode_id))
        log("fake apply_mode %s" % mode_id)
        return types.SimpleNamespace(ok=True, steps=[("panel-profile", True, ""), ("helper", True, "ok")])

    modes.apply_mode = apply_mode
    modes.load_modes = lambda modes_dir=None: {
        m: types.SimpleNamespace(id=m, name=m.capitalize(), description="d", icon="i", governor="schedutil",
                                 compositor="picom", zram_percent=50, pins=[], packages=[], flatpaks=[])
        for m in ("lite", "everyday", "gaming", "work", "creator")}

    browsers = types.ModuleType("lindos.browsers")
    browsers.BROWSERS = {"edge": {"name": "Microsoft Edge"}, "chrome": {"name": "Google Chrome"},
                         "firefox": {"name": "Mozilla Firefox"}}
    browsers.install = lambda bid, log=print: (calls.append(("install_browser", bid)), install_ok)[1]
    browsers.set_default = lambda bid: (calls.append(("set_default", bid)), True)[1]
    browsers.is_installed = lambda bid: bid != "chrome"
    browsers.online = lambda: True

    helper = types.ModuleType("lindos.helper")

    def run_privileged(action, payload, log=None):
        calls.append(("helper", action, payload))
        if log:
            log("helper %s" % action)
        return types.SimpleNamespace(ok=helper_ok, out="out line", err="" if helper_ok else "boom", code=0 if helper_ok else 3)

    helper.run_privileged = run_privileged

    hardware = types.ModuleType("lindos.hardware")
    hardware.ram_info = lambda: {"total": 3900, "used": 700, "available": 3000}

    for name, mod in (("lindos", pkg), ("lindos.paths", paths), ("lindos.config", config),
                      ("lindos.theme", theme), ("lindos.modes", modes), ("lindos.browsers", browsers),
                      ("lindos.helper", helper), ("lindos.hardware", hardware)):
        monkeypatch.setitem(sys.modules, name, mod)
    core._module_cache.clear()

    # wallpaper must exist for set-wallpaper (LINDOS_ROOT prefix)
    root = tmp_path / "root"
    wall = root / "usr" / "share" / "backgrounds" / "lindos"
    wall.mkdir(parents=True)
    (wall / "aurora-dark.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _clear_cache():
    core._module_cache.clear()
    yield
    core._module_cache.clear()


def test_real_executors_call_core_apis(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    catalog = load_catalog(APPS_JSON)
    sel = Selections(mode="gaming", browser="edge", apps=["wine", "steam", "creative", "onlyoffice"])
    plan = build_plan(sel, catalog, online=True)
    logs = []
    result = Runner(plan, core.make_real_executors(), log=logs.append).run()
    assert result.ok, [(r.step_id, r.message) for r in result.results if not r.ok]
    # config written first, with rejected key reported but not fatal
    assert _FakeConfig.saved and _FakeConfig.store["mode"] == "gaming"
    assert _FakeConfig.store["browser"] == "edge" and _FakeConfig.store["telemetry"] is False
    assert "location_services" not in _FakeConfig.store
    assert "keys not stored: location_services" in result.get("write-config").message
    # theme calls
    assert ("set_dark", True) in calls
    assert ("set_accent", "#60CDFF") in calls
    assert ("set_wallpaper", "/usr/share/backgrounds/lindos/aurora-dark.svg") in calls
    assert ("set_taskbar_alignment", "center") in calls
    # apply_mode goes through lindos.modes (user + helper part), not the raw helper
    assert ("apply_mode", "gaming") in calls
    assert not any(c[0] == "helper" and c[1] == "apply-mode" for c in calls)
    # browser install + default via lindos.browsers
    assert ("install_browser", "edge") in calls and ("set_default", "edge") in calls
    # exactly one helper call per privileged group
    helper_actions = [c[1] for c in calls if c[0] == "helper"]
    assert helper_actions == ["write-system-config", "install-packages", "install-flatpaks",
                              "install-compat", "install-gaming"]
    payloads = {c[1]: c[2] for c in calls if c[0] == "helper"}
    assert payloads["write-system-config"] == {"mode": "gaming", "browser": "edge"}
    assert payloads["install-packages"] == {"packages": ["gimp", "krita", "kdenlive"]}
    assert payloads["install-flatpaks"] == {"flatpaks": ["org.onlyoffice.desktopeditors"]}
    assert payloads["install-compat"] == {"items": ["wine", "umu"]}
    assert payloads["install-gaming"] == {"items": ["steam"]}
    assert any("fake apply_mode gaming" in line for line in logs)


def test_real_executors_report_failures_without_stopping(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls, helper_ok=False, install_ok=False)
    catalog = load_catalog(APPS_JSON)
    plan = build_plan(Selections(browser="chrome", apps=["wine"]), catalog, online=True)
    result = Runner(plan, core.make_real_executors(), log=lambda m: None).run()
    assert not result.ok
    assert "install-browser" in result.failed_ids
    assert "lindos-settings apps" in result.get("install-browser").message
    assert "write-system-config" in result.failed_ids and "install-compat" in result.failed_ids
    # chrome is not installed (fake) -> default browser step fails honestly, others still ran
    assert "set-default-browser" in result.failed_ids
    assert result.get("apply-mode").ok and result.get("set-theme").ok
    assert [c[1] for c in calls if c[0] == "helper"] == ["write-system-config", "install-compat"]


def test_missing_wallpaper_fails_step_only(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    plan = build_plan(Selections(wallpaper="/usr/share/backgrounds/lindos/does-not-exist.svg"))
    result = Runner(plan, core.make_real_executors(), log=lambda m: None).run()
    assert result.failed_ids == ["set-wallpaper"]
    assert not any(c[0] == "set_wallpaper" for c in calls)


def test_core_helpers_with_fake_lindos(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    modes = core.load_modes()
    assert list(modes) == ["everyday", "gaming", "work", "creator", "lite"]   # SPEC order restored
    assert list(core.browsers_table()) == ["edge", "chrome", "firefox"]
    assert core.is_online() is True
    assert core.ram_total_mb() == 3900
    assert core.list_wallpapers() == ["/usr/share/backgrounds/lindos/aurora-dark.svg"]
    assert core.setup_done_exists() is False
    assert core.mark_setup_done() is True
    assert os.path.exists(core.setup_done_path())
    assert _FakeConfig.store.get("setup_done") is True
    assert core.setup_done_exists() is True
    live = core.LiveApplier(dry_run=False)
    assert live.set_dark(False) and ("set_dark", False) in calls
    assert live.set_accent("#B4A0FF") and ("set_accent", "#B4A0FF") in calls
    dry = core.LiveApplier(dry_run=True)
    n = len(calls)
    assert dry.set_wallpaper("/x.svg") is True and len(calls) == n


def test_threaded_live_applier_serialises_and_coalesces(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    live = core.LiveApplier(dry_run=False, threaded=True)
    try:
        # a burst of accent clicks: only the last colour must reach lindos.theme
        for hex_colour in ("#0067C0", "#6CCB5F", "#B4A0FF"):
            assert live.set_accent(hex_colour) is True
        assert live.set_dark(False) is True
        assert live.set_taskbar_alignment("left") is True
        assert live.drain(10.0) is True
        accents = [c for c in calls if c[0] == "set_accent"]
        assert accents and accents[-1] == ("set_accent", "#B4A0FF")
        assert ("set_dark", False) in calls
        assert ("set_taskbar_alignment", "left") in calls
        # a second round after the worker went idle still works
        assert live.set_wallpaper("/usr/share/backgrounds/lindos/aurora-dark.svg") is True
        assert live.drain(10.0) is True
        assert ("set_wallpaper", "/usr/share/backgrounds/lindos/aurora-dark.svg") in calls
    finally:
        live.close()
    # sync mode is unchanged and drain() is a no-op
    sync = core.LiveApplier(dry_run=False)
    assert sync.drain() is True
    assert sync.set_accent("#F2C94C") is True and ("set_accent", "#F2C94C") in calls


def test_core_without_lindos_falls_back(monkeypatch, tmp_path):
    for name in list(sys.modules):
        if name == "lindos" or name.startswith("lindos."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if "lindos-core" not in p])
    core._module_cache.clear()
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "empty-root"))
    assert core.core_module("modes") is None
    modes = core.load_modes()
    assert list(modes) == ["everyday", "gaming", "work", "creator", "lite"]
    assert modes["lite"].name == "Lite" and modes["gaming"].icon
    assert list(core.browsers_table()) == ["edge", "chrome", "firefox"]
    assert core.browsers_table()["edge"]["name"] == "Microsoft Edge"
    walls = core.list_wallpapers()
    assert len(walls) == 5 and walls[0] == "/usr/share/backgrounds/lindos/aurora-dark.svg"
    assert core.wallpaper_display_name(walls[3]) == "Mist Purple"
    assert core.LiveApplier().set_dark(True) is False   # honest: nothing to call
    # executors fail cleanly (never raise out of the runner)
    plan = build_plan(Selections())
    result = Runner(plan, core.make_real_executors(), log=lambda m: None).run()
    assert not result.ok and len(result.failed_ids) == len(plan)
    assert "lindos-core" in result.get("write-config").message
