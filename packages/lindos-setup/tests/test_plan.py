"""Unit tests for lindos_setup.plan (pure logic, no GTK, no lindos-core)."""
from __future__ import annotations

import json
import os
import sys

import pytest

from lindos_setup import plan as planmod
from lindos_setup.plan import (
    BROWSER_IDS, DEFAULT_ACCENT, DEFAULT_WALLPAPER, LIGHT_WALLPAPER, MODE_IDS, AppEntry, Catalog,
    Plan, Runner, Selections, Step, build_plan, load_accents, load_catalog,
    make_printing_executors, make_recording_executors, summarize,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SHARE = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "share", "lindos", "setup"))
APPS_JSON = os.path.join(SHARE, "apps.json")
ACCENTS_JSON = os.path.join(SHARE, "accents.json")


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    cat = load_catalog(APPS_JSON)
    assert len(cat) > 0, "apps.json must load"
    return cat


# --------------------------------------------------------------------------- selections
def test_defaults_match_spec():
    sel = Selections()
    assert sel.mode == "everyday"
    assert sel.browser == "chrome"
    assert sel.theme == "dark" and sel.dark
    assert sel.accent == DEFAULT_ACCENT == "#60CDFF"
    assert sel.wallpaper == DEFAULT_WALLPAPER == "/usr/share/backgrounds/lindos/aurora-dark.svg"
    assert sel.taskbar_alignment == "center"
    assert not hasattr(sel, "apps"), "the wizard no longer offers or installs apps"
    assert sel.location is False and sel.crash_reports is False
    sel.validate()


def test_constants_match_spec():
    assert MODE_IDS == ["everyday", "gaming", "work", "creator", "lite"]
    assert BROWSER_IDS == ["edge", "chrome", "firefox"]


@pytest.mark.parametrize("field,value", [
    ("mode", "turbo"), ("browser", "opera"), ("theme", "blue"), ("accent", "60CDFF"),
    ("accent", "#GGGGGG"), ("taskbar_alignment", "right"), ("wallpaper", ""),
])
def test_validate_rejects_bad_values(field, value):
    sel = Selections()
    setattr(sel, field, value)
    with pytest.raises(ValueError):
        sel.validate()
    with pytest.raises(ValueError):
        build_plan(sel)


def test_selections_roundtrip_and_unknown_keys():
    sel = Selections(mode="gaming", browser="edge", location=True)
    d = sel.as_dict()
    d["future_key"] = 42
    back = Selections.from_dict(d)
    assert back == sel
    assert "apps" not in d


def test_old_selection_files_with_apps_still_load():
    old = {"mode": "creator", "browser": "chrome", "apps": ["wine", "steam"], "theme": "light"}
    sel = Selections.from_dict(old)
    assert sel.mode == "creator" and sel.theme == "light"
    assert not hasattr(sel, "apps") and "apps" not in sel.as_dict()


def test_transfer_selection_defaults_validate_and_roundtrip():
    sel = Selections()
    assert sel.transfer == {"enabled": False, "source_type": "", "source": ""}
    sel.validate()
    sel.transfer = {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    sel.validate()
    d = sel.as_dict()
    assert d["transfer"] == {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    back = Selections.from_dict(d)
    assert back == sel and back.transfer is not sel.transfer  # copied, not shared


@pytest.mark.parametrize("bad", [
    {"enabled": "yes", "source_type": "", "source": ""},
    {"enabled": True, "source_type": "usb", "source": ""},
    {"enabled": True, "source_type": "partition", "source": 5},
])
def test_transfer_selection_rejects_bad_values(bad):
    sel = Selections(transfer=bad)
    with pytest.raises(ValueError):
        sel.validate()


def test_transfer_from_dict_ignores_junk():
    sel = Selections.from_dict({"transfer": "not-a-dict"})
    assert sel.transfer == {"enabled": False, "source_type": "", "source": ""}
    sel2 = Selections.from_dict({"transfer": {"enabled": True, "source_type": "bundle",
                                              "source": "/media/USB/kit", "extra": 1}})
    assert sel2.transfer == {"enabled": True, "source_type": "bundle", "source": "/media/USB/kit"}


def test_set_theme_follows_default_wallpaper():
    sel = Selections()
    sel.set_theme("light")
    assert sel.wallpaper == LIGHT_WALLPAPER
    sel.set_theme("dark")
    assert sel.wallpaper == DEFAULT_WALLPAPER
    custom = "/usr/share/backgrounds/lindos/nightfall.svg"
    sel.wallpaper = custom
    sel.set_theme("light")
    assert sel.wallpaper == custom  # custom choice untouched
    with pytest.raises(ValueError):
        sel.set_theme("sepia")


# --------------------------------------------------------------------------- catalog
def test_catalog_loads_and_has_spec_apps(catalog: Catalog):
    ids = catalog.ids()
    for required in ("wine", "steam", "sober", "prism", "heroic", "lutris", "bottles", "onlyoffice", "creative"):
        assert required in ids
    wine = catalog.get("wine")
    assert wine is not None and wine.kind == "script" and wine.action == "install-compat"
    steam = catalog.get("steam")
    assert steam is not None and steam.action == "install-gaming" and steam.items == ["steam"]
    assert catalog.get("creative").kind == "apt" and "gimp" in catalog.get("creative").packages
    assert catalog.get("onlyoffice").kind == "flatpak"
    assert "nope" not in catalog and catalog.get("nope") is None


def test_catalog_default_on_modes(catalog: Catalog):
    # SPEC: Windows app support on everywhere; Steam on in gaming
    for mode in MODE_IDS:
        assert "wine" in catalog.default_ids(mode), mode
    assert "steam" in catalog.default_ids("gaming")
    for mode in ("everyday", "work", "creator", "lite"):
        assert "steam" not in catalog.default_ids(mode), mode
    assert catalog.default_ids("everyday") == ["wine"]
    assert "onlyoffice" in catalog.default_ids("work")
    assert set(catalog.default_ids("creator")) >= {"wine", "bottles", "creative"}
    # order follows the catalog
    order = catalog.ids()
    for mode in MODE_IDS:
        ids = catalog.default_ids(mode)
        assert ids == sorted(ids, key=order.index)


def test_catalog_validation_errors():
    with pytest.raises(ValueError):
        AppEntry.from_dict({"id": "x", "kind": "apt"})            # apt without packages
    with pytest.raises(ValueError):
        AppEntry.from_dict({"id": "x", "kind": "flatpak"})        # flatpak without ids
    with pytest.raises(ValueError):
        AppEntry.from_dict({"id": "x", "kind": "script", "action": "rm-rf", "items": ["a"]})
    with pytest.raises(ValueError):
        AppEntry.from_dict({"id": "x", "kind": "snap", "packages": ["a"]})
    with pytest.raises(ValueError):
        Catalog([AppEntry.from_dict({"id": "dup", "kind": "apt", "packages": ["a"]}),
                 AppEntry.from_dict({"id": "dup", "kind": "apt", "packages": ["b"]})])


def test_load_catalog_missing_file_is_empty(tmp_path):
    cat = load_catalog(str(tmp_path / "nope.json"))
    assert len(cat) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(load_catalog(str(bad))) == 0


def test_find_data_file_uses_package_root():
    assert planmod.find_data_file(planmod.APPS_JSON_REL) == os.path.normpath(APPS_JSON)


def test_accents_json():
    accents = load_accents(ACCENTS_JSON)
    assert len(accents) == 8
    assert accents[0]["hex"] == "#60CDFF" and accents[0]["name"] == "Aurora Blue"
    assert len({a["hex"] for a in accents}) == 8
    for a in accents:
        assert a["hex"].startswith("#") and len(a["hex"]) == 7
    # built-in fallback also has 8 with the same first entry
    fb = load_accents("/definitely/not/here.json")
    assert len(fb) == 8 and fb[0]["hex"] == "#60CDFF"


# --------------------------------------------------------------------------- build_plan
INSTALL_ACTIONS = {"install-browser", "install-packages", "install-flatpaks", "install-compat",
                   "install-gaming", "install-drivers"}


@pytest.mark.parametrize("mode", MODE_IDS)
def test_build_plan_each_mode_is_install_free(mode):
    plan = build_plan(Selections(mode=mode))
    ids = [s.id for s in plan.steps]
    assert ids[0] == "write-config"
    assert ids[-1] == "set-default-browser"
    assert plan.get("apply-mode").payload == {"mode": mode, "install": False}
    assert plan.get("write-system-config").payload == {"mode": mode, "browser": "chrome"}
    assert plan.get("write-config").payload["mode"] == mode
    assert all(s.kind in ("user", "system") for s in plan.steps)
    assert len(ids) == len(set(ids))
    # nothing to install or download: the installer did all of that
    assert not INSTALL_ACTIONS & set(plan.actions())
    assert [a for a, _p in plan.system_payloads()] == ["write-system-config", "apply-mode"]
    assert {s.action for s in plan.user_steps()} == {
        "write-config", "set-theme", "set-accent", "set-wallpaper", "set-taskbar-alignment",
        "set-default-browser"}
    # ordering: user steps, then the one privileged batch, then the final default-browser step
    kinds = [s.kind for s in plan.steps]
    first_sys = kinds.index("system")
    assert all(k == "user" for k in kinds[:first_sys])
    assert kinds[-1] == "user" and all(k == "system" for k in kinds[first_sys:-1])
    assert plan.notes == []


@pytest.mark.parametrize("browser", BROWSER_IDS)
def test_no_browser_choice_ever_adds_a_download_step(browser):
    plan = build_plan(Selections(browser=browser))
    assert "install-browser" not in plan.actions()
    assert plan.get("set-default-browser").payload == {"browser": browser}
    assert plan.get("write-config").payload["browser"] == browser
    assert plan.notes == []


def test_legacy_arguments_are_ignored():
    """tests/test_integration.py still passes a catalog and online=; they change nothing."""
    cat = load_catalog(APPS_JSON)
    assert build_plan(Selections(), cat, online=False) == build_plan(Selections())
    assert Plan.from_selections(Selections(), cat, online=False) == build_plan(Selections())


def test_pending_browser_is_stored_as_the_preference_without_failing():
    states = {"edge": "unavailable", "chrome": "pending", "firefox": "installed"}
    plan = build_plan(Selections(browser="chrome"), browser_states=states)
    step = plan.get("set-default-browser")
    assert step.payload == {"browser": "chrome", "pending": True}
    assert "once it is added" in step.title
    # the preference reaches the user config and system.json (the silent retry reads system.json)
    assert plan.get("write-config").payload["browser"] == "chrome"
    assert plan.get("write-system-config").payload["browser"] == "chrome"
    assert len(plan.notes) == 1
    assert "isn't on this PC yet" in plan.notes[0] and "when you're online" in plan.notes[0]
    assert "Firefox" in plan.notes[0]
    assert "install-browser" not in plan.actions()
    # a browser that is installed, or a different choice, gets the ordinary step and no note
    plan = build_plan(Selections(browser="firefox"), browser_states=states)
    assert plan.get("set-default-browser").payload == {"browser": "firefox"} and plan.notes == []
    plan = build_plan(Selections(browser="chrome"), browser_states=dict(states, chrome="installed"))
    assert plan.get("set-default-browser").payload == {"browser": "chrome"} and plan.notes == []


def test_write_config_payload_contains_privacy_and_look():
    sel = Selections(theme="light", accent="#0067C0", taskbar_alignment="left",
                     wallpaper=LIGHT_WALLPAPER, location=True, crash_reports=True)
    plan = build_plan(sel)
    p = plan.get("write-config").payload
    assert p["theme"] == "light" and p["accent"] == "#0067C0"
    assert p["taskbar_alignment"] == "left" and p["wallpaper"] == LIGHT_WALLPAPER
    assert p["telemetry"] is True and p["location_services"] is True
    assert plan.get("set-theme").payload == {"dark": False, "theme": "light"}
    assert plan.get("set-accent").payload == {"accent": "#0067C0"}
    assert plan.get("set-wallpaper").payload == {"path": LIGHT_WALLPAPER}
    assert plan.get("set-taskbar-alignment").payload == {"alignment": "left"}


def test_build_plan_does_not_mutate_input():
    sel = Selections(browser="chrome")
    build_plan(sel, browser_states={"chrome": "pending"})
    assert sel.browser == "chrome" and sel == Selections(browser="chrome")


# --------------------------------------------------------------------------- json
def test_json_roundtrip():
    sel = Selections(mode="creator", browser="chrome", theme="light", accent="#B4A0FF",
                     wallpaper=LIGHT_WALLPAPER, taskbar_alignment="left",
                     location=True, crash_reports=False)
    plan = build_plan(sel, browser_states={"chrome": "pending"})
    text = plan.to_json()
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["selections"]["mode"] == "creator"
    assert isinstance(data["steps"], list) and data["steps"][0]["id"] == "write-config"
    back = Plan.from_json(text)
    assert back == plan
    assert back.to_json() == text
    assert [s.action for s in back.steps] == plan.actions()
    assert back.system_payloads() == plan.system_payloads()
    # Plan.from_selections is build_plan
    assert Plan.from_selections(sel, browser_states={"chrome": "pending"}) == plan


def test_plan_json_from_before_the_installer_flow_still_loads():
    """A saved plan that still has install-* steps and selections.apps loads; nothing crashes."""
    old = {"schema": 1, "notes": [], "selections": {"mode": "gaming", "apps": ["steam"]},
           "steps": [
               {"id": "write-config", "title": "Save", "kind": "user", "action": "write-config",
                "payload": {}},
               {"id": "install-gaming", "title": "Install", "kind": "system",
                "action": "install-gaming", "payload": {"items": ["steam"]}}]}
    plan = Plan.from_json(json.dumps(old))
    assert plan.selections.mode == "gaming" and not hasattr(plan.selections, "apps")
    assert plan.actions() == ["write-config", "install-gaming"]
    # no executor is registered for the retired action: it is skipped, never run
    result = Runner(plan, make_recording_executors(["write-config"], []), log=lambda m: None).run()
    assert result.ok and result.skipped_ids == ["install-gaming"]


def test_plan_from_json_rejects_bad_schema():
    with pytest.raises(ValueError):
        Plan.from_json(json.dumps({"schema": 99, "steps": []}))
    with pytest.raises(ValueError):
        Step(id="", title="", kind="user", action="x")
    with pytest.raises(ValueError):
        Step(id="a", title="", kind="root", action="x")
    with pytest.raises(ValueError):
        Plan([Step("a", "A", "user", "x"), Step("a", "A", "user", "y")])


# --------------------------------------------------------------------------- runner
def test_runner_with_fake_executors():
    sel = Selections(mode="gaming", browser="edge")
    plan = build_plan(sel)
    record = []
    executors = make_recording_executors(plan.actions(), record)
    logs = []
    starts, dones = [], []
    runner = Runner(plan, executors, log=logs.append,
                    on_step_start=lambda i, n, s: starts.append(s.id),
                    on_step_done=lambda i, n, s, r: dones.append((s.id, r.ok)))
    result = runner.run()
    assert result.ok
    assert [a for a, _p in record] == plan.actions()
    assert starts == [s.id for s in plan.steps]
    assert all(ok for _sid, ok in dones)
    assert result.succeeded_ids == [s.id for s in plan.steps]
    assert result.failed_ids == [] and result.skipped_ids == []
    assert any("[ok]" in line for line in logs)
    assert "finished" in logs[-1].lower()
    # payloads passed through untouched
    assert ("apply-mode", {"mode": "gaming", "install": False}) in record
    assert ("set-default-browser", {"browser": "edge"}) in record


def test_runner_failure_isolation():
    plan = build_plan(Selections(mode="gaming", browser="edge"))
    record = []
    executors = make_recording_executors(plan.actions(), record,
                                         fail={"apply-mode"}, raise_on={"write-system-config"})
    logs = []
    result = Runner(plan, executors, log=logs.append).run()
    assert not result.ok
    assert set(result.failed_ids) == {"apply-mode", "write-system-config"}
    # every step still ran (isolation), in order
    assert [a for a, _p in record] == plan.actions()
    assert result.get("apply-mode").message == "simulated failure"
    assert "RuntimeError" in result.get("write-system-config").message
    assert result.get("set-default-browser").ok  # ran after the failures
    assert any("[failed]" in line for line in logs)
    assert result.summary().endswith("2 failed")


def test_runner_missing_executor_is_skipped_not_failed():
    plan = build_plan(Selections(browser="edge"))
    record = []
    executors = make_recording_executors(["write-config", "set-theme"], record)
    result = Runner(plan, executors, log=lambda m: None).run()
    assert result.ok  # skipped steps do not fail the run
    assert set(result.skipped_ids) == set(plan.actions()) - {"write-config", "set-theme"}
    assert [a for a, _p in record] == ["write-config", "set-theme"]


def test_runner_stop_on_failure_and_cancel():
    plan = build_plan(Selections())
    record = []
    executors = make_recording_executors(plan.actions(), record, fail={"set-theme"})
    result = Runner(plan, executors, log=lambda m: None, stop_on_failure=True).run()
    assert [a for a, _p in record] == ["write-config", "set-theme"]
    assert result.failed_ids == ["set-theme"]
    record.clear()
    runner = Runner(plan, make_recording_executors(plan.actions(), record), log=lambda m: None)
    runner.cancel()
    result = runner.run()
    assert record == [] and len(result.skipped_ids) == len(plan)


def test_executor_result_shapes():
    plan = Plan([Step("a", "A", "user", "x"), Step("b", "B", "user", "y"),
                 Step("c", "C", "system", "z")])
    executors = {"x": lambda s, l: None, "y": lambda s, l: (True, "fine"), "z": lambda s, l: (False, "nope")}
    result = Runner(plan, executors, log=lambda m: None).run()
    assert result.get("a").ok and result.get("b").ok and result.get("b").message == "fine"
    assert result.get("c").failed and result.get("c").message == "nope"


def test_printing_executors_dry_run():
    plan = build_plan(Selections(mode="work"))
    out = []
    executors = make_printing_executors(plan, write=out.append)
    result = Runner(plan, executors, log=lambda m: None).run()
    assert result.ok
    assert len(out) == len(plan)
    assert all(line.startswith("[dry-run] ") for line in out)
    assert any("apply-mode" in line and '"install": false' in line for line in out)
    assert not any("install-" in line for line in out)
    # without write=, lines go through the runner log
    logs = []
    Runner(plan, make_printing_executors(plan), log=logs.append).run()
    assert sum(1 for line in logs if line.startswith("[dry-run] ")) == len(plan)


# --------------------------------------------------------------------------- summary
def test_summarize_rows():
    sel = Selections(mode="gaming", browser="edge", accent="#60CDFF")
    rows = dict(summarize(sel, {"gaming": "Gaming"}, {"edge": "Microsoft Edge"},
                          {"#60CDFF": "Aurora Blue"}))
    assert rows["Mode"] == "Gaming"
    assert rows["Browser"] == "Microsoft Edge"
    assert rows["Theme"] == "Dark"
    assert rows["Accent"] == "Aurora Blue (#60CDFF)"
    assert rows["Wallpaper"] == "Aurora Dark"
    assert rows["Taskbar"] == "Center"
    assert "Apps" not in rows, "the wizard installs no apps, so it does not summarise any"
    assert rows["Location services"] == "Off" and rows["Crash reports"] == "Off"
    assert rows["Bring your files from Windows"] == "Not now"
    sel.transfer = {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    rows2 = dict(summarize(sel))
    assert "Transfer tool" in rows2["Bring your files from Windows"]


def test_summarize_says_plainly_when_the_browser_is_still_pending():
    sel = Selections(browser="chrome")
    rows = dict(summarize(sel, None, {"chrome": "Google Chrome"}, None, {"chrome": "pending"}))
    assert rows["Browser"] == "Google Chrome — will be added when you're online (Firefox until then)"
    rows = dict(summarize(sel, None, {"chrome": "Google Chrome"}, None, {"chrome": "installed"}))
    assert rows["Browser"] == "Google Chrome"


# --------------------------------------------------------------------------- install-state recap
def test_install_recap_only_reports_what_the_installer_recorded():
    assert planmod.install_recap(None) == [] and planmod.install_recap({}) == []
    rows = planmod.install_recap({"browser": "done", "drivers": "done", "compat": "pending",
                                  "flatpaks": "failed", "gaming": "skipped", "updates": "pending",
                                  "mode_extras": "done", "unknown-step": "done"})
    by_step = {step: (name, text) for step, name, text in rows}
    assert [step for step, _n, _t in rows] == [
        "updates", "drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks"]   # canonical order
    assert by_step["browser"] == ("Google Chrome", "installed while Lindos was installing")
    assert by_step["drivers"][1] == "set up while Lindos was installing"
    assert "Update Manager" in by_step["updates"][1]
    assert "Lindos Settings › Apps" in by_step["compat"][1] and "Lindos Settings › Apps" in by_step["flatpaks"][1]
    assert by_step["gaming"][1] == "left out on purpose"
    # the two steps with a silent background retry say so
    rows = dict((s, t) for s, _n, t in planmod.install_recap({"browser": "pending", "drivers": "failed"}))
    assert "in the background" in rows["browser"] and "in the background" in rows["drivers"]


def test_pending_steps_are_pending_or_failed_in_canonical_order():
    steps = {"flatpaks": "failed", "browser": "pending", "compat": "done", "gaming": "skipped", "x": "pending"}
    assert planmod.pending_steps(steps) == ["browser", "flatpaks"]
    assert planmod.pending_steps(None) == [] and planmod.pending_steps({}) == []


def test_mode_extras_pending_follows_what_the_mode_brings():
    import types
    plain = types.SimpleNamespace(packages=[], flatpaks=[])
    apt_mode = types.SimpleNamespace(packages=["thunderbird"], flatpaks=[])
    flat_mode = types.SimpleNamespace(packages=[], flatpaks=["com.usebottles.bottles"])
    waiting = {"mode_extras": "pending", "flatpaks": "failed", "gaming": "pending"}
    assert planmod.mode_extras_pending(plain, "everyday", waiting) is False
    assert planmod.mode_extras_pending(apt_mode, "work", waiting) is True
    assert planmod.mode_extras_pending(flat_mode, "creator", waiting) is True
    assert planmod.mode_extras_pending(plain, "gaming", waiting) is True        # launchers
    assert planmod.mode_extras_pending(plain, "gaming", {"gaming": "done"}) is False
    assert planmod.mode_extras_pending(apt_mode, "work", {"flatpaks": "pending"}) is False
    assert planmod.mode_extras_pending(apt_mode, "work", {}) is False


def test_plan_module_has_no_gtk_or_lindos_imports():
    for name in ("gi", "Gtk", "lindos"):
        assert name not in vars(planmod)
    with open(planmod.__file__, encoding="utf-8") as fh:
        src = fh.read()
    assert "import gi" not in src and "from gi" not in src
    assert "import lindos" not in src and "from lindos" not in src
    # core.py (the bridge) must also import without gi and without lindos-core present
    from lindos_setup import core
    assert core.in_xfce({"XDG_CURRENT_DESKTOP": "XFCE"})
    assert core.in_xfce({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}) is False
    assert core.in_xfce({"DESKTOP_SESSION": "xfce"})
    assert set(core.make_real_executors()) == {
        "write-config", "set-theme", "set-accent", "set-wallpaper", "set-taskbar-alignment",
        "set-default-browser", "write-system-config", "apply-mode"}


def test_core_paths_honour_lindos_home(monkeypatch, tmp_path):
    from lindos_setup import core
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    assert core.home_dir() == str(tmp_path)
    assert core.setup_done_path().startswith(str(tmp_path))
    assert core.log_file().endswith("setup.log")
    assert not core.setup_done_exists() or os.path.exists(core.setup_done_path())
    # dry-run never writes
    assert core.mark_setup_done(dry_run=True) is True
    assert not os.path.exists(core.setup_done_path())


def test_headless_dry_run_prints_default_plan():
    from lindos_setup import core
    out = []
    assert core.headless_dry_run(write=out.append) == 0
    data = json.loads(out[0])
    assert data["schema"] == 1 and data["selections"]["mode"] == "everyday"
    actions = [s["action"] for s in data["steps"]]
    assert "apply-mode" in actions and not INSTALL_ACTIONS & set(actions)


# --------------------------------------------------------------------------- shipped files
def _read_desktop(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert "\r" not in text, "%s must use LF line endings" % path
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln and not ln.startswith("#"))


def test_desktop_entries_match_spec():
    root = os.path.normpath(os.path.join(HERE, "..", "root"))
    menu = _read_desktop(os.path.join(root, "usr", "share", "applications", "lindos-setup.desktop"))
    assert menu["Name"] == "Lindos Setup"
    assert menu["Exec"] == "lindos-setup --reconfigure"
    assert menu["NoDisplay"] == "false"
    assert menu["Categories"] == "Settings;System;"
    auto = _read_desktop(os.path.join(root, "usr", "share", "lindos", "setup", "autostart", "lindos-setup.desktop"))
    assert auto["Exec"] == "lindos-setup --first-run"
    assert auto["OnlyShowIn"] == "XFCE;"
    assert auto["X-GNOME-Autostart-Delay"] == "2"
    launcher = os.path.join(root, "usr", "bin", "lindos-setup")
    with open(launcher, encoding="utf-8") as fh:
        src = fh.read()
    assert src.startswith("#!/bin/sh\n") and "\r" not in src
    assert 'exec python3 /usr/lib/lindos-setup/main.py "$@"' in src


def test_control_file_dependencies():
    control = os.path.normpath(os.path.join(HERE, "..", "DEBIAN", "control"))
    fields = {}
    with open(control, encoding="utf-8") as fh:
        for line in fh:
            if line[:1].strip() and ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
    assert fields["Package"] == "lindos-setup" and fields["Version"] == "1.0.0"
    assert fields["Architecture"] == "all"
    assert fields["Maintainer"] == "Lindos Team <team@lindos.dev>"
    depends = {d.strip() for d in fields["Depends"].split(",")}
    assert {"python3", "python3-gi", "gir1.2-gtk-3.0", "gir1.2-gdkpixbuf-2.0", "lindos-core",
            "xdg-utils"} <= depends
    recommends = {d.strip() for d in fields["Recommends"].split(",")}
    assert {"lindos-compat", "lindos-gaming"} <= recommends


# --------------------------------------------------------------------------- entry point / UI imports
def _load_main_module():
    import importlib.util
    path = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "lib", "lindos-setup", "main.py"))
    spec = importlib.util.spec_from_file_location("lindos_setup_main_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_parser_flags_match_spec():
    main = _load_main_module()
    parser = main.build_parser()
    args = parser.parse_args(["--first-run", "--dry-run", "--page", "summary"])
    assert args.first_run and args.dry_run and args.page == "summary" and not args.reconfigure
    args = parser.parse_args(["--reconfigure"])
    assert args.reconfigure and not args.first_run
    assert main.PAGE_IDS == ["welcome", "mode", "browser", "personalize", "privacy",
                             "transfer", "summary", "apply", "done"]
    with pytest.raises(SystemExit):
        parser.parse_args(["--first-run", "--reconfigure"])   # mutually exclusive
    with pytest.raises(SystemExit):
        parser.parse_args(["--page", "nope"])


def test_first_run_gate(monkeypatch, tmp_path):
    import logging
    from lindos_setup import core
    main = _load_main_module()
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    logger = logging.getLogger("lindos-setup-test")
    # not XFCE -> exit 0 silently
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    assert main.first_run_gate(logger) == 0
    # XFCE, not done -> run (None)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    assert main.first_run_gate(logger) is None
    # SETUP_DONE marker present -> exit 0
    marker = core.setup_done_path()
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("done\n")
    assert main.first_run_gate(logger) == 0
    # main() honours the gate without needing GTK
    assert main.main(["--first-run"]) == 0


def _cmdline(tmp_path, monkeypatch, text):
    path = tmp_path / "fake-cmdline"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LINDOS_TEST_CMDLINE", str(path))


def test_first_run_gate_never_runs_in_the_live_session(monkeypatch, tmp_path, caplog):
    import logging
    main = _load_main_module()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    logger = logging.getLogger("lindos-setup-test-live")
    assert main.first_run_gate(logger) is None                   # an ordinary installed session
    for line in ("BOOT_IMAGE=/casper/vmlinuz boot=casper username=liveuser quiet splash ---\n",
                 "boot=live noprompt\n"):
        _cmdline(tmp_path, monkeypatch, line)
        with caplog.at_level(logging.INFO, logger="lindos-setup-test-live"):
            caplog.clear()
            assert main.first_run_gate(logger) == 0
        assert any("live" in rec.getMessage() for rec in caplog.records), "the gate logs why it exited"
        assert main.main(["--first-run"]) == 0
    # words are matched exactly, like lindos.session does
    _cmdline(tmp_path, monkeypatch, "xboot=casper boot=casper2 quiet\n")
    assert main.first_run_gate(logger) is None


def test_first_run_gate_never_runs_as_the_temporary_oem_user(monkeypatch, caplog):
    import logging
    from lindos_setup import core
    main = _load_main_module()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    logger = logging.getLogger("lindos-setup-test-oem")
    monkeypatch.setattr(core, "is_oem_temp_user", lambda: True)
    with caplog.at_level(logging.INFO, logger="lindos-setup-test-oem"):
        assert main.first_run_gate(logger) == 0
    assert any("oem" in rec.getMessage() for rec in caplog.records)
    assert main.main(["--first-run"]) == 0
    monkeypatch.setattr(core, "is_oem_temp_user", lambda: False)
    assert main.first_run_gate(logger) is None


def test_reconfigure_is_refused_in_live_or_oem_but_dry_run_is_not(monkeypatch, tmp_path, capsys):
    from lindos_setup import core
    main = _load_main_module()
    _cmdline(tmp_path, monkeypatch, "boot=casper\n")
    monkeypatch.setattr(core, "headless_dry_run", lambda logger=None, write=None: 7)
    assert main.main(["--reconfigure"]) == 0
    assert "not available in the live" in capsys.readouterr().err
    # a dry run only prints a plan and changes nothing, so it stays available for developers
    monkeypatch.setitem(sys.modules, "lindos_setup.app", None)   # no GTK: the headless fallback answers
    assert main.main(["--dry-run"]) == 7


def test_session_helpers_fall_back_when_lindos_core_is_missing(monkeypatch, tmp_path):
    from lindos_setup import core
    monkeypatch.setattr(core, "core_module", lambda name: None)
    assert core.is_live_session() is False
    _cmdline(tmp_path, monkeypatch, "quiet boot=casper\n")
    assert core.is_live_session() is True
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "oem")
    assert core.is_oem_temp_user() is True
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    assert core.is_oem_temp_user() is False


def test_session_helpers_use_lindos_session_when_available(monkeypatch, tmp_path):
    from lindos_setup import core
    core._module_cache.clear()
    _cmdline(tmp_path, monkeypatch, "boot=casper\n")
    try:
        import lindos.session  # noqa: F401
    except ImportError:
        pytest.skip("lindos-core is not importable")
    assert core.is_live_session() is True
    assert core.is_oem_temp_user() is False
    core._module_cache.clear()


def _ensure_gi() -> None:
    """Real PyGObject or the repo ``gi`` stub (tests/lindos_testsupport.py); else skip."""
    import sys
    try:
        import gi  # noqa: F401
        return
    except ImportError:
        pass
    tests_dir = os.path.normpath(os.path.join(HERE, "..", "..", "..", "tests"))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        import lindos_testsupport  # noqa: F401  (installs the stub)
    except ImportError:
        pytest.skip("neither PyGObject nor the repo gi stub is available")


def test_ui_modules_import_with_gi_stub():
    """widgets/pages/app import under the repo ``gi`` stub (or real PyGObject)."""
    _ensure_gi()
    from lindos_setup import app, pages, widgets
    assert pages.PAGE_ORDER == ["welcome", "mode", "browser", "personalize", "privacy",
                                "transfer", "summary", "apply", "done"]
    assert [p.id for p in pages.make_pages()] == pages.PAGE_ORDER
    assert not hasattr(pages, "AppsPage"), "the wizard no longer has an apps page"
    assert widgets.COLUMN_MAX_W == 760            # centred content column, not a fixed 900x620 card
    assert not hasattr(app, "CARD_W")
    assert (widgets.THUMB_W, widgets.THUMB_H) == (192, 108)
    assert os.path.isfile(app.CSS_PATH)
    assert isinstance(widgets.style_priority(1), int)


def test_page_context_knows_the_install_state_and_never_probes_the_network():
    _ensure_gi()
    from lindos_setup import core, pages
    ctx = pages.PageContext(
        selections=Selections(), accents=[], modes={}, browsers={"chrome": {}, "edge": {}, "firefox": {}},
        dry_run=True, first_run=True, wallpapers=[], ram_total_mb=None,
        live=core.LiveApplier(dry_run=True), executors_factory=lambda plan: {})
    assert ctx.install_steps == {}
    # without install-state Firefox is the only browser that is certainly there
    assert ctx.browser_states == {"chrome": "unavailable", "edge": "unavailable", "firefox": "installed"}
    for gone in ("online", "online_known", "ensure_online_known", "set_online", "online_listeners"):
        assert not hasattr(ctx, gone), gone
    ctx2 = pages.PageContext(
        selections=Selections(), accents=[], modes={}, browsers={}, dry_run=True, first_run=True,
        wallpapers=[], ram_total_mb=None, live=core.LiveApplier(dry_run=True),
        executors_factory=lambda plan: {}, install_steps={"browser": "pending"},
        browser_states={"chrome": "pending"})
    assert ctx2.install_steps == {"browser": "pending"} and ctx2.browser_states == {"chrome": "pending"}


def _make_ctx(**kw):
    from lindos_setup import core, pages
    defaults = dict(selections=Selections(), accents=[], modes={}, browsers={},
                    dry_run=True, first_run=True, wallpapers=[], ram_total_mb=None,
                    live=core.LiveApplier(dry_run=True), executors_factory=lambda plan: {})
    defaults.update(kw)
    return pages.PageContext(**defaults)


def test_transfer_page_lists_sources_and_records_selection():
    """SPEC-WINDOWS §32: the 'transfer' page never blocks on the CLI and records
    Selections.transfer; the special 'skip' card is always offered and selected by default."""
    _ensure_gi()
    from lindos_setup import pages

    ctx = _make_ctx()
    page = pages.TransferPage()
    page.build(ctx)
    data = {"available": True, "note": "", "partitions": [
        {"device": "/dev/sda2", "label": "OS", "windows": True, "mountpoint": "/media/alice/OS", "note": ""},
        {"device": "/dev/sda3", "label": "Data", "windows": False, "mountpoint": "/media/alice/Data"},
    ], "bundles": [{"path": "/media/USB/kit", "computer": "DESKTOP-1", "user": "alice", "created": "2026-09-26"}]}
    page._loaded_cb(data)
    # non-Windows partitions are not offered; the Windows one and the bundle are, plus 'skip'
    assert set(page._sources) == {"skip", "part:/dev/sda2", "bundle:/media/USB/kit"}
    assert page.group.selected == "skip"
    assert ctx.selections.transfer == {"enabled": False, "source_type": "", "source": ""}

    page._changed("part:/dev/sda2")
    assert ctx.selections.transfer == {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    page._changed("bundle:/media/USB/kit")
    assert ctx.selections.transfer == {"enabled": True, "source_type": "bundle", "source": "/media/USB/kit"}
    page._changed("skip")
    assert ctx.selections.transfer == {"enabled": False, "source_type": "", "source": ""}

    # re-entering the page with a prior choice re-selects the matching card
    ctx.selections.transfer = {"enabled": True, "source_type": "bundle", "source": "/media/USB/kit"}
    page._loaded_cb(data)
    assert page.group.selected == "bundle:/media/USB/kit"


def test_transfer_page_handles_missing_cli_and_no_sources():
    _ensure_gi()
    from lindos_setup import pages

    ctx = _make_ctx()
    page = pages.TransferPage()
    page.build(ctx)
    page._loaded_cb({"available": False, "note": "The Transfer tool (lindos-transfer) is not installed.",
                     "partitions": [], "bundles": []})
    assert set(page._sources) == {"skip"}
    assert page.group.selected == "skip"
    # available but nothing found: no crash, 'skip' still the only (selectable) option
    page._loaded_cb({"available": True, "note": "", "partitions": [], "bundles": []})
    assert set(page._sources) == {"skip"}
    assert page.group.selected == "skip"


def test_done_page_launches_transfer_gui_once_when_chosen(monkeypatch):
    _ensure_gi()
    from lindos_setup import core, pages

    ctx = _make_ctx(dry_run=False)
    ctx.selections.transfer = {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    page = pages.DonePage()
    page.build(ctx)
    calls = []
    monkeypatch.setattr(core, "launch_transfer_gui", lambda source="": (calls.append(source), True)[1])
    page.on_enter(ctx)
    page.on_enter(ctx)  # a second visit must not launch it again
    assert calls == ["/media/alice/OS"]
    assert ctx.transfer_launched is True


def test_done_page_skips_transfer_gui_when_not_chosen(monkeypatch):
    _ensure_gi()
    from lindos_setup import core, pages

    ctx = _make_ctx(dry_run=False)
    page = pages.DonePage()
    page.build(ctx)
    calls = []
    monkeypatch.setattr(core, "launch_transfer_gui", lambda source="": calls.append(source))
    page.on_enter(ctx)
    assert calls == []
    assert ctx.transfer_launched is False
