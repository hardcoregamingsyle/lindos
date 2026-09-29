"""Unit tests for lindos_setup.plan (pure logic, no GTK, no lindos-core)."""
from __future__ import annotations

import json
import os

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
    assert sel.apps == []
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
    sel = Selections(mode="gaming", browser="edge", apps=["wine", "steam"], location=True)
    d = sel.as_dict()
    d["future_key"] = 42
    back = Selections.from_dict(d)
    assert back == sel
    assert back.apps is not sel.apps  # copied


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
@pytest.mark.parametrize("mode", MODE_IDS)
def test_build_plan_each_mode(mode, catalog: Catalog):
    sel = Selections(mode=mode, apps=catalog.default_ids(mode))
    plan = build_plan(sel, catalog)
    ids = [s.id for s in plan.steps]
    assert ids[0] == "write-config"
    assert ids[-1] == "set-default-browser"
    assert plan.get("apply-mode").payload["mode"] == mode
    assert plan.get("write-system-config").payload == {"mode": mode, "browser": "chrome"}
    assert plan.get("write-config").payload["mode"] == mode
    # kinds valid, ids unique
    assert all(s.kind in ("user", "system") for s in plan.steps)
    assert len(ids) == len(set(ids))
    # every privileged action appears at most once
    sys_actions = [a for a, _p in plan.system_payloads()]
    assert len(sys_actions) == len(set(sys_actions))
    # wine is default everywhere -> exactly one install-compat
    assert sys_actions.count("install-compat") == 1
    assert plan.get("install-compat").payload["items"] == ["wine", "umu"]
    if mode == "gaming":
        assert "install-gaming" in sys_actions
        assert "steam" in plan.get("install-gaming").payload["items"]
    if mode == "creator":
        assert plan.get("install-packages").payload["packages"] == ["gimp", "krita", "kdenlive"]
    if mode == "work":
        assert plan.get("install-flatpaks").payload["flatpaks"] == ["org.onlyoffice.desktopeditors"]
    if mode in ("everyday", "lite"):
        assert "install-gaming" not in sys_actions
        assert "install-packages" not in sys_actions
        assert "install-flatpaks" not in sys_actions
    # chrome is the default and we're online: downloaded from Google's apt repo
    assert "install-browser" in sys_actions
    assert plan.get("install-browser").payload == {"browser": "chrome"}
    # user/system split
    assert {s.action for s in plan.user_steps()} == {
        "write-config", "set-theme", "set-accent", "set-wallpaper", "set-taskbar-alignment",
        "set-default-browser"}
    assert all(s.kind == "system" for s in plan.system_steps())
    # ordering: user steps, then system, then final default-browser
    kinds = [s.kind for s in plan.steps]
    first_sys = kinds.index("system")
    assert all(k == "user" for k in kinds[:first_sys])
    assert kinds[-1] == "user" and all(k == "system" for k in kinds[first_sys:-1])


def test_apps_grouping_one_call_per_action(catalog: Catalog):
    sel = Selections(mode="gaming",
                     apps=["wine", "steam", "sober", "prism", "heroic", "lutris", "bottles",
                           "onlyoffice", "creative", "steam"])  # duplicate on purpose
    plan = build_plan(sel, catalog)
    payloads = dict(plan.system_payloads())
    assert set(payloads) == {"write-system-config", "apply-mode", "install-browser", "install-packages",
                             "install-flatpaks", "install-compat", "install-gaming"}
    assert payloads["install-browser"] == {"browser": "chrome"}
    assert payloads["install-gaming"]["items"] == ["steam", "sober", "prism", "heroic", "lutris", "bottles"]
    assert payloads["install-compat"]["items"] == ["wine", "umu"]
    assert payloads["install-packages"]["packages"] == ["gimp", "krita", "kdenlive"]
    assert payloads["install-flatpaks"]["flatpaks"] == ["org.onlyoffice.desktopeditors"]
    assert plan.selections.apps.count("steam") == 1  # de-duplicated
    # system_payloads order follows SPEC grouping order
    order = [a for a, _p in plan.system_payloads()]
    assert order == ["write-system-config", "apply-mode", "install-browser", "install-packages",
                     "install-flatpaks", "install-compat", "install-gaming"]


def test_no_apps_no_install_steps():
    # default browser (chrome) is downloaded from Google's apt repo when online (the default)
    plan = build_plan(Selections(apps=[]))
    assert [a for a, _p in plan.system_payloads()] == ["write-system-config", "apply-mode", "install-browser"]
    # firefox: nothing to download, so no install-browser step
    plan = build_plan(Selections(apps=[], browser="firefox"))
    assert [a for a, _p in plan.system_payloads()] == ["write-system-config", "apply-mode"]


def test_unknown_app_id_is_ignored_with_note(catalog: Catalog):
    plan = build_plan(Selections(apps=["wine", "does-not-exist"]), catalog)
    assert "install-compat" in plan.actions()
    assert any("does-not-exist" in n for n in plan.notes)


def test_browser_edge_online_adds_install_step():
    plan = build_plan(Selections(browser="edge"), online=True)
    step = plan.get("install-browser")
    assert step is not None and step.kind == "system" and step.payload == {"browser": "edge"}
    assert plan.get("set-default-browser").payload == {"browser": "edge"}
    assert plan.get("write-config").payload["browser"] == "edge"
    assert plan.notes == []


def test_offline_browser_fallback_to_firefox():
    for wanted in ("edge", "chrome"):
        plan = build_plan(Selections(browser=wanted), online=False)
        assert plan.selections.browser == "firefox"
        assert plan.get("install-browser") is None
        assert plan.get("set-default-browser").payload == {"browser": "firefox"}
        assert plan.get("write-config").payload["browser"] == "firefox"
        assert plan.get("write-system-config").payload["browser"] == "firefox"
        assert any(wanted in n and "lindos-settings apps" in n for n in plan.notes)
    # firefox offline: no note, no fallback needed
    plan = build_plan(Selections(browser="firefox"), online=False)
    assert plan.notes == []
    # offline with downloads pending -> note about finishing later
    cat = load_catalog(APPS_JSON)
    plan = build_plan(Selections(apps=["wine"]), cat, online=False)
    assert any("Offline" in n for n in plan.notes)
    assert plan.get("apply-mode").payload["online"] is False


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


def test_build_plan_does_not_mutate_input(catalog: Catalog):
    sel = Selections(browser="chrome", apps=["wine", "wine"])
    build_plan(sel, catalog, online=False)
    assert sel.browser == "chrome" and sel.apps == ["wine", "wine"]


# --------------------------------------------------------------------------- json
def test_json_roundtrip(catalog: Catalog):
    sel = Selections(mode="creator", browser="chrome", theme="light", accent="#B4A0FF",
                     wallpaper=LIGHT_WALLPAPER, taskbar_alignment="left",
                     apps=catalog.default_ids("creator"), location=True, crash_reports=False)
    plan = build_plan(sel, catalog, online=True)
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
    assert Plan.from_selections(sel, catalog, online=True) == plan


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
def test_runner_with_fake_executors(catalog: Catalog):
    sel = Selections(mode="gaming", browser="edge", apps=catalog.default_ids("gaming"))
    plan = build_plan(sel, catalog, online=True)
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
    assert ("install-browser", {"browser": "edge"}) in record


def test_runner_failure_isolation(catalog: Catalog):
    sel = Selections(mode="gaming", browser="edge", apps=catalog.default_ids("gaming"))
    plan = build_plan(sel, catalog, online=True)
    record = []
    executors = make_recording_executors(plan.actions(), record,
                                         fail={"install-browser"}, raise_on={"install-gaming"})
    logs = []
    result = Runner(plan, executors, log=logs.append).run()
    assert not result.ok
    assert set(result.failed_ids) == {"install-browser", "install-gaming"}
    # every step still ran (isolation), in order
    assert [a for a, _p in record] == plan.actions()
    assert result.get("install-browser").message == "simulated failure"
    assert "RuntimeError" in result.get("install-gaming").message
    assert result.get("set-default-browser").ok  # ran after the failures
    assert any("[failed]" in line for line in logs)
    assert result.summary().endswith("2 failed")


def test_runner_missing_executor_is_skipped_not_failed():
    plan = build_plan(Selections(browser="edge"), online=True)
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


def test_printing_executors_dry_run(catalog: Catalog):
    sel = Selections(mode="work", apps=catalog.default_ids("work"))
    plan = build_plan(sel, catalog)
    out = []
    executors = make_printing_executors(plan, write=out.append)
    result = Runner(plan, executors, log=lambda m: None).run()
    assert result.ok
    assert len(out) == len(plan)
    assert all(line.startswith("[dry-run] ") for line in out)
    assert any("install-flatpaks" in line and "org.onlyoffice.desktopeditors" in line for line in out)
    # without write=, lines go through the runner log
    logs = []
    Runner(plan, make_printing_executors(plan), log=logs.append).run()
    assert sum(1 for line in logs if line.startswith("[dry-run] ")) == len(plan)


# --------------------------------------------------------------------------- summary
def test_summarize_rows(catalog: Catalog):
    sel = Selections(mode="gaming", browser="edge", apps=["wine", "steam"], accent="#60CDFF")
    rows = dict(summarize(sel, catalog, {"gaming": "Gaming"}, {"edge": "Microsoft Edge"},
                          {"#60CDFF": "Aurora Blue"}))
    assert rows["Mode"] == "Gaming"
    assert rows["Browser"] == "Microsoft Edge"
    assert rows["Theme"] == "Dark"
    assert rows["Accent"] == "Aurora Blue (#60CDFF)"
    assert rows["Wallpaper"] == "Aurora Dark"
    assert rows["Taskbar"] == "Center"
    assert "Steam" in rows["Apps"] and "Windows app support" in rows["Apps"]
    assert rows["Location services"] == "Off" and rows["Crash reports"] == "Off"
    assert rows["Bring your files from Windows"] == "Not now"
    assert dict(summarize(Selections()))["Apps"] == "None"
    sel.transfer = {"enabled": True, "source_type": "partition", "source": "/media/alice/OS"}
    rows2 = dict(summarize(sel, catalog))
    assert "Transfer tool" in rows2["Bring your files from Windows"]


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
        "set-default-browser", "write-system-config", "apply-mode", "install-browser",
        "install-packages", "install-flatpaks", "install-compat", "install-gaming"}


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


def test_headless_dry_run_prints_default_plan(monkeypatch):
    from lindos_setup import core
    monkeypatch.setattr(core, "is_online", lambda *a, **k: False)
    out = []
    assert core.headless_dry_run(write=out.append) == 0
    data = json.loads(out[0])
    assert data["schema"] == 1 and data["selections"]["mode"] == "everyday"
    assert "install-compat" in [s["action"] for s in data["steps"]]


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
    assert main.PAGE_IDS == ["welcome", "mode", "browser", "personalize", "apps", "privacy",
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
    assert pages.PAGE_ORDER == ["welcome", "mode", "browser", "personalize", "apps", "privacy",
                                "transfer", "summary", "apply", "done"]
    assert [p.id for p in pages.make_pages()] == pages.PAGE_ORDER
    assert widgets.COLUMN_MAX_W == 760            # centred content column, not a fixed 900x620 card
    assert not hasattr(app, "CARD_W")
    assert (widgets.THUMB_W, widgets.THUMB_H) == (192, 108)
    assert os.path.isfile(app.CSS_PATH)
    assert isinstance(widgets.style_priority(1), int)


def test_page_context_connectivity_state(monkeypatch):
    """PageContext.set_online / ensure_online_known (pure logic, runs under the gi stub)."""
    _ensure_gi()
    from lindos_setup import core, pages
    ctx = pages.PageContext(
        selections=Selections(browser="edge"), catalog=Catalog([]), accents=[], modes={},
        browsers={}, online=False, dry_run=True, first_run=True, wallpapers=[],
        ram_total_mb=None, live=core.LiveApplier(dry_run=True),
        executors_factory=lambda plan: {}, online_known=False)
    assert ctx.online_known is False and ctx.online is False
    seen = []
    ctx.online_listeners.append(lambda: seen.append(ctx.online))
    # a blocking probe result is recorded once and listeners fire
    monkeypatch.setattr(core, "is_online", lambda *a, **k: True)
    assert ctx.ensure_online_known() is True
    assert ctx.online_known and ctx.online and seen == [True]
    monkeypatch.setattr(core, "is_online", lambda *a, **k: False)
    assert ctx.ensure_online_known() is True          # already known: no re-probe
    assert ctx.set_online(False) is False              # idle_add-compatible return value
    assert ctx.online is False and seen == [True, False]
    # default: known immediately (tests / explicit callers)
    ctx2 = pages.PageContext(
        selections=Selections(), catalog=Catalog([]), accents=[], modes={}, browsers={},
        online=True, dry_run=True, first_run=True, wallpapers=[], ram_total_mb=None,
        live=core.LiveApplier(dry_run=True), executors_factory=lambda plan: {})
    assert ctx2.online_known is True and ctx2.ensure_online_known() is True


def _make_ctx(**kw):
    from lindos_setup import core, pages
    defaults = dict(selections=Selections(), catalog=Catalog([]), accents=[], modes={}, browsers={},
                    online=True, dry_run=True, first_run=True, wallpapers=[], ram_total_mb=None,
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
