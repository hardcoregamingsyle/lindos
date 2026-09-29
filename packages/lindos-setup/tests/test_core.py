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

import json

from lindos_setup import core
from lindos_setup.plan import Runner, Selections, build_plan

HERE = os.path.dirname(os.path.abspath(__file__))


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

    def apply_mode(mode_id, *, system=False, dry_run=False, log=print, install=True):
        calls.append(("apply_mode", mode_id, install))
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
    plan = build_plan(Selections(mode="gaming", browser="edge"))
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
    # apply_mode goes through lindos.modes (user + helper part), and NEVER installs packages here
    assert ("apply_mode", "gaming", False) in calls
    assert not any(c[0] == "apply_mode" and c[2] is not False for c in calls)
    assert not any(c[0] == "helper" and c[1] == "apply-mode" for c in calls)
    # the default browser is set; nothing is ever installed from the wizard
    assert ("set_default", "edge") in calls
    assert not any(c[0] == "install_browser" for c in calls)
    helper_actions = [c[1] for c in calls if c[0] == "helper"]
    assert helper_actions == ["write-system-config"]
    payloads = {c[1]: c[2] for c in calls if c[0] == "helper"}
    assert payloads["write-system-config"] == {"mode": "gaming", "browser": "edge"}
    assert any("fake apply_mode gaming" in line for line in logs)


def test_real_executors_report_failures_without_stopping(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls, helper_ok=False)
    plan = build_plan(Selections(browser="chrome"))
    result = Runner(plan, core.make_real_executors(), log=lambda m: None).run()
    assert not result.ok
    assert result.failed_ids == ["write-system-config"]
    # chrome is not installed (fake) and nothing recorded says it is gone for good: the choice is
    # kept as a pending preference, which is NOT a failure
    default = result.get("set-default-browser")
    assert default.ok and "not installed yet" in default.message
    assert ("set_default", "chrome") not in calls, "a pending browser must not touch the personal default"
    assert result.get("apply-mode").ok and result.get("set-theme").ok
    assert [c[1] for c in calls if c[0] == "helper"] == ["write-system-config"]


def test_set_default_browser_outcomes(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    execs = core.make_real_executors()
    step = build_plan(Selections(browser="edge")).get("set-default-browser")
    assert execs["set-default-browser"](step, lambda m: None) is True      # installed: set as default
    assert ("set_default", "edge") in calls
    # not installed and the installer said 'skipped': an honest failure, nothing changed
    sys.modules["lindos.browsers"].is_installed = lambda bid: False
    monkeypatch.setattr(core, "install_steps", lambda: {"browser": "skipped"})
    step = build_plan(Selections(browser="chrome")).get("set-default-browser")
    ok, msg = execs["set-default-browser"](step, lambda m: None)
    assert ok is False and "not installed" in msg
    # the plan said it is pending: success with an explanation, never a failure
    step = build_plan(Selections(browser="chrome"), browser_states={"chrome": "pending"}).get("set-default-browser")
    ok, msg = execs["set-default-browser"](step, lambda m: None)
    assert ok is True and "when it is added" in msg
    # pending at run time although the plan did not know: same answer
    monkeypatch.setattr(core, "install_steps", lambda: {"browser": "pending"})
    step = build_plan(Selections(browser="chrome")).get("set-default-browser")
    assert core._exec_set_default_browser(step, lambda m: None)[0] is True
    # it landed between the summary and the apply page: it is simply made the default
    sys.modules["lindos.browsers"].is_installed = lambda bid: True
    step = build_plan(Selections(browser="chrome"), browser_states={"chrome": "pending"}).get("set-default-browser")
    assert execs["set-default-browser"](step, lambda m: None) is True
    assert ("set_default", "chrome") in calls


def test_missing_wallpaper_fails_step_only(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    # browser pinned to firefox (on the ISO, not downloaded): isolates this test to the
    # wallpaper failure only — the fake lindos.browsers.is_installed() always reports chrome
    # as absent, which would otherwise also fail the unrelated set-default-browser step.
    plan = build_plan(Selections(wallpaper="/usr/share/backgrounds/lindos/does-not-exist.svg",
                                 browser="firefox"))
    result = Runner(plan, core.make_real_executors(), log=lambda m: None).run()
    assert result.failed_ids == ["set-wallpaper"]
    assert not any(c[0] == "set_wallpaper" for c in calls)


def test_core_helpers_with_fake_lindos(monkeypatch, tmp_path):
    calls = []
    _install_fake_lindos(monkeypatch, tmp_path, calls)
    modes = core.load_modes()
    assert list(modes) == ["everyday", "gaming", "work", "creator", "lite"]   # SPEC order restored
    assert list(core.browsers_table()) == ["edge", "chrome", "firefox"]
    assert not hasattr(core, "is_online"), "the wizard never probes the network"
    assert core.install_steps() == {}          # the fake lindos has no installstate: no record, no crash
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


def test_transfer_sources_missing_binary(monkeypatch):
    monkeypatch.setattr(core, "which", lambda cmd: None)
    data = core.transfer_sources()
    assert data == {"partitions": [], "bundles": [], "available": False,
                    "note": "The Transfer tool (lindos-transfer) is not installed."}


def test_transfer_sources_parses_json(monkeypatch):
    monkeypatch.setattr(core, "which", lambda cmd: "/usr/bin/lindos-transfer" if cmd == "lindos-transfer" else None)
    payload = {"partitions": [{"device": "/dev/sda2", "windows": True, "mountpoint": "/media/alice/OS"}],
              "bundles": [{"path": "/media/USB/kit", "computer": "DESKTOP-1"}]}
    import json as _json

    def fake_run(argv, **kwargs):
        assert argv == ["/usr/bin/lindos-transfer", "sources", "--json"]
        return types.SimpleNamespace(returncode=0, stdout=_json.dumps(payload), stderr="")

    data = core.transfer_sources(run=fake_run)
    assert data["available"] is True and data["note"] == ""
    assert data["partitions"][0]["device"] == "/dev/sda2"
    assert data["bundles"][0]["computer"] == "DESKTOP-1"


def test_transfer_sources_handles_bad_exit_and_json(monkeypatch):
    monkeypatch.setattr(core, "which", lambda cmd: "/usr/bin/lindos-transfer")
    bad_exit = lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom")  # noqa: E731
    data = core.transfer_sources(run=bad_exit)
    assert data["available"] is False and "boom" in data["note"]
    bad_json = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="{not json", stderr="")  # noqa: E731
    data2 = core.transfer_sources(run=bad_json)
    assert data2["available"] is False and data2["partitions"] == []
    raising = lambda argv, **kw: (_ for _ in ()).throw(OSError("no such file"))  # noqa: E731
    data3 = core.transfer_sources(run=raising)
    assert data3["available"] is False and "no such file" in data3["note"]


def test_launch_transfer_gui(monkeypatch):
    monkeypatch.setattr(core, "which", lambda cmd: None)
    assert core.launch_transfer_gui() is False
    calls = []
    monkeypatch.setattr(core, "which", lambda cmd: "/usr/bin/lindos-transfer-gui")
    monkeypatch.setattr(core.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert core.launch_transfer_gui("/media/alice/OS") is True
    assert calls == [["/usr/bin/lindos-transfer-gui", "--from", "/media/alice/OS"]]
    calls.clear()
    assert core.launch_transfer_gui() is True
    assert calls == [["/usr/bin/lindos-transfer-gui"]]


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
    # no lindos-core also means no install-state: nothing recorded, browsers by their own checks only
    assert core.install_steps() == {}
    assert core.browser_state("firefox") == "installed"
    assert core.browser_state("chrome") == "pending" and core.browser_state("edge") == "unavailable"


# --------------------------------------------------------------------------- install-state / browser states
def _write_install_state(monkeypatch, tmp_path, steps):
    root = tmp_path / "state-root"
    target = root / "var" / "lib" / "lindos"
    target.mkdir(parents=True, exist_ok=True)
    body = {"schema": 1, "updated": "2026-09-29T10:00:00Z", "online": False,
            "steps": {sid: {"status": st, "detail": "", "time": "2026-09-29T10:00:00Z"} for sid, st in steps.items()}}
    (target / "install-state.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    core._module_cache.clear()


def _need_real_installstate():
    try:
        import lindos.installstate  # noqa: F401
    except ImportError:
        pytest.skip("lindos-core is not importable")


def test_install_steps_reads_the_installers_record(monkeypatch, tmp_path):
    _need_real_installstate()
    assert core.install_steps() == {}                      # no file: empty, never an error
    _write_install_state(monkeypatch, tmp_path, {"browser": "done", "compat": "pending", "drivers": "failed"})
    assert core.install_steps() == {"browser": "done", "compat": "pending", "drivers": "failed"}
    (tmp_path / "state-root" / "var" / "lib" / "lindos" / "install-state.json").write_text("{corrupt", encoding="utf-8")
    assert core.install_steps() == {}


@pytest.mark.parametrize("recorded,expected", [
    ("done", "installed"),        # the installer downloaded Chrome from Google's repository
    ("pending", "pending"),       # it was offline: the silent retry adds it later
    ("failed", "pending"),        # the retry still runs
    ("", "pending"),              # unrecorded is retried too (legacy install, dead installer hook)
    ("skipped", "unavailable"),   # left out on purpose: not offered
])
def test_chrome_state_follows_the_install_state(monkeypatch, recorded, expected):
    monkeypatch.setattr(core, "browser_installed", lambda bid: False)
    steps = {"browser": recorded} if recorded else {}
    assert core.browser_state("chrome", steps) == expected


def test_browser_state_rules(monkeypatch):
    installed = set()
    monkeypatch.setattr(core, "browser_installed", lambda bid: bid in installed)
    assert core.browser_state("firefox", {}) == "installed"        # always: it is on the ISO
    assert core.browser_state("edge", {"browser": "done"}) == "unavailable"   # never installed by Lindos
    installed.add("edge")
    assert core.browser_state("edge", {}) == "installed"           # ... unless it is really there
    installed.add("chrome")
    assert core.browser_state("chrome", {"browser": "pending"}) == "installed"   # really there wins
    assert core.browser_states(["edge", "chrome", "firefox"], {}) == {
        "edge": "installed", "chrome": "installed", "firefox": "installed"}


def test_browser_states_reads_install_state_and_the_browsers_table(monkeypatch, tmp_path):
    _need_real_installstate()
    monkeypatch.setattr(core, "browser_installed", lambda bid: False)
    _write_install_state(monkeypatch, tmp_path, {"browser": "pending"})
    assert core.browser_states() == {"edge": "unavailable", "chrome": "pending", "firefox": "installed"}
    _write_install_state(monkeypatch, tmp_path, {"browser": "done"})
    assert core.browser_states()["chrome"] == "installed"
