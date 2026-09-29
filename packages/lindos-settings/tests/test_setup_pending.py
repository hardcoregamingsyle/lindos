"""Settings > Apps "Left to finish from setup" and the Updates wording: what the installer could not
do (install-state.json) with an "Install now" button per item, all through the existing helper
actions.  Pure logic in ``lindos_settings.model``, the ``Backend`` adapter, and the pages under the
gi stub (no real display, no real helper, no network).
"""
from __future__ import annotations

import json
import types

import pytest

from lindos_settings import model
from lindos_settings.backend import Backend, CmdResult, HelperResult


def _state(**steps):
    return {"schema": 1, "online": False,
            "steps": {sid: {"status": st, "detail": "", "time": "2026-09-29T10:00:00Z"} for sid, st in steps.items()}}


# --------------------------------------------------------------------------- normalise
@pytest.mark.parametrize("bad", [None, [], "x", 3, {"steps": "nope"}, {"steps": {"a": 1, "b": {"status": "weird"}}}])
def test_normalize_install_state_never_raises_and_drops_junk(bad):
    st = model.normalize_install_state(bad)
    assert st["steps"] == {} and st["online"] is None


def test_normalize_install_state_keeps_status_and_a_short_detail():
    st = model.normalize_install_state({"online": True, "steps": {
        "browser": {"status": "failed", "detail": "apt   exit\n100"}, "compat": {"status": "done"},
        "x": {"status": "pending", "detail": 5}}})
    assert st["online"] is True
    assert st["steps"]["browser"] == {"status": "failed", "detail": "apt exit 100"}
    assert st["steps"]["compat"] == {"status": "done", "detail": ""}
    assert st["steps"]["x"]["detail"] == ""
    assert model.setup_step_status(_state(browser="pending"), "browser") == "pending"
    assert model.setup_step_status(_state(), "browser") == ""


def test_pending_steps_are_pending_or_failed_in_canonical_order():
    state = _state(flatpaks="failed", browser="pending", compat="done", gaming="skipped", updates="pending", zzz="pending")
    assert model.setup_pending_steps(state) == ["updates", "browser", "flatpaks", "zzz"]
    assert model.setup_pending_steps(None) == []


# --------------------------------------------------------------------------- the rows
def test_no_record_or_nothing_pending_means_no_rows():
    assert model.pending_setup_items(None) == []
    assert model.pending_setup_items(_state(browser="done", compat="done", drivers="skipped")) == []


def test_every_pending_step_becomes_an_install_now_row_with_a_helper_payload():
    state = _state(updates="pending", drivers="pending", browser="failed", compat="pending", gaming="pending",
                   mode_extras="pending", flatpaks="failed")
    rows = model.pending_setup_items(state, mode_packages=["thunderbird", "redshift-gtk", "thunderbird"],
                                     mode_flatpaks=["org.prismlauncher.PrismLauncher"])
    by_id = {r["id"]: r for r in rows}
    assert [r["id"] for r in rows] == ["drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks"]
    assert "updates" not in by_id, "system updates belong to the Updates page"
    assert all(r["button"] == "Install now" for r in rows)
    assert by_id["browser"]["kind"] == "browser" and by_id["browser"]["payload"] == {"browser": "chrome"}
    assert by_id["compat"]["payload"] == {"items": ["wine", "umu"]}
    assert by_id["gaming"]["payload"] == {"items": ["steam", "lutris", "heroic", "prism", "sober"]}
    assert by_id["mode_extras"]["kind"] == "packages"
    assert by_id["mode_extras"]["payload"] == {"packages": ["thunderbird", "redshift-gtk"]}      # unique, ordered
    assert by_id["flatpaks"]["payload"] == {"flatpaks": ["org.prismlauncher.PrismLauncher"]}
    assert by_id["drivers"]["payload"] == {"args": []}
    assert by_id["flatpaks"]["status"] == "failed" and by_id["compat"]["status"] == "pending"


def test_row_wording_is_honest_about_retries_and_needs_the_network():
    rows = {r["id"]: r for r in model.pending_setup_items(
        _state(browser="pending", drivers="failed", compat="pending", flatpaks="failed"),
        mode_flatpaks=["com.usebottles.bottles"])}
    # Chrome and drivers are retried silently in the background; the rest is not, so it must not say so
    assert "in the background" in rows["browser"]["subtitle"] and "internet connection" in rows["browser"]["subtitle"]
    assert "Couldn't be installed" in rows["drivers"]["subtitle"] and "in the background" in rows["drivers"]["subtitle"]
    assert "background" not in rows["compat"]["subtitle"] and "Install now" in rows["compat"]["subtitle"]
    assert "background" not in rows["flatpaks"]["subtitle"] and "try again now" in rows["flatpaks"]["subtitle"]
    detail = model.pending_setup_items({"steps": {"compat": {"status": "failed", "detail": "apt exit 100"}}})[0]
    assert "(apt exit 100)" in detail["subtitle"]


def test_live_checks_drop_what_is_really_installed_already():
    state = _state(browser="pending", compat="pending", gaming="pending", mode_extras="pending", flatpaks="pending",
                   drivers="pending")
    have = {"browser": True, "compat": True, "gaming": {"steam", "lutris", "heroic", "prism", "sober"},
            "packages": {"thunderbird"}, "flatpaks": {"com.usebottles.bottles"}}
    rows = model.pending_setup_items(state, mode_packages=["thunderbird"], mode_flatpaks=["com.usebottles.bottles"],
                                     installed=have)
    assert [r["id"] for r in rows] == ["drivers"]           # no live check for drivers: it stays until recorded
    # partly installed: only what is missing is offered
    have = {"gaming": {"steam", "lutris"}, "packages": {"thunderbird"}, "flatpaks": set()}
    rows = {r["id"]: r for r in model.pending_setup_items(
        state, mode_packages=["thunderbird", "redshift-gtk"], mode_flatpaks=["a.b.C"], installed=have)}
    assert rows["gaming"]["payload"]["items"] == ["heroic", "prism", "sober"]
    assert rows["mode_extras"]["payload"]["packages"] == ["redshift-gtk"]
    assert rows["flatpaks"]["payload"]["flatpaks"] == ["a.b.C"]


def test_a_row_that_could_install_nothing_is_never_shown():
    state = _state(mode_extras="pending", flatpaks="pending")
    assert model.pending_setup_items(state) == []            # the Modes list no extras: no dead button


def test_row_payloads_pass_the_helpers_own_validation():
    import os
    helper = pytest.importorskip("lindos.helper")
    modes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lindos-core", "root",
                             "usr", "share", "lindos", "modes")
    modes = pytest.importorskip("lindos.modes").load_modes(modes_dir)
    assert modes, "the shipped mode.json files must load"
    packages = [p for m in modes.values() for p in m.packages]
    flatpaks = [f for m in modes.values() for f in m.flatpaks]
    state = _state(drivers="pending", browser="pending", compat="pending", gaming="pending",
                   mode_extras="pending", flatpaks="pending")
    rows = model.pending_setup_items(state, mode_packages=packages, mode_flatpaks=flatpaks)
    actions = {"browser": "install-browser", "compat": "install-compat", "gaming": "install-gaming",
               "packages": "install-packages", "flatpaks": "install-flatpaks", "drivers": "install-drivers"}
    assert {r["kind"] for r in rows} == set(actions)
    for row in rows:
        helper.validate_payload(actions[row["kind"]], dict(row["payload"]))


# --------------------------------------------------------------------------- updates wording
def test_os_updates_subtitle_says_what_the_installer_did():
    base = model.os_updates_subtitle(None)
    assert base == "System packages, Firefox, Wine and everything else update through the system Update Manager"
    assert model.os_updates_subtitle(_state(browser="done")) == base           # no record for updates: unchanged
    done = model.os_updates_subtitle(_state(updates="done"))
    assert "installed the available system updates while installing" in done and "Update Manager" in done
    waiting = model.os_updates_subtitle(_state(updates="failed"))
    assert "still waiting" in waiting and "no internet connection" in waiting and "Update Manager" in waiting
    assert model.os_updates_subtitle(_state(updates="skipped")) == base


# --------------------------------------------------------------------------- backend
def test_backend_install_state_reads_the_file_when_lindos_core_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "root"
    target = root / "var" / "lib" / "lindos"
    target.mkdir(parents=True)
    (target / "install-state.json").write_text(json.dumps(_state(browser="pending")), encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    b = Backend()
    b._missing.add("installstate")                       # as if lindos-core had no such module
    assert b.install_state()["steps"]["browser"]["status"] == "pending"
    (target / "install-state.json").write_text("{corrupt", encoding="utf-8")
    assert b.install_state() == {"online": None, "steps": {}}
    (target / "install-state.json").unlink()
    assert b.install_state() == {"online": None, "steps": {}}


def test_backend_setup_pending_items_probes_only_what_is_pending(monkeypatch):
    b = Backend()
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(
        _state(browser="pending", compat="pending", mode_extras="pending")))
    monkeypatch.setattr(b, "mode_extras", lambda: (["thunderbird", "redshift-gtk"], ["a.b.C"]))
    probed: list[str] = []
    monkeypatch.setattr(b, "compat_status", lambda: (probed.append("compat"), {"wine": True, "umu": True})[1])
    monkeypatch.setattr(b, "installed_packages", lambda names: (probed.append("packages"), {"thunderbird"})[1])
    monkeypatch.setattr(b, "launcher_states", lambda: probed.append("gaming") or [])
    monkeypatch.setattr(b, "flatpak_apps", lambda: probed.append("flatpaks") or set())
    b._mods["browsers"] = types.SimpleNamespace(is_installed=lambda bid: (probed.append("browser"), False)[1])
    rows = b.setup_pending_items()
    assert sorted(probed) == ["browser", "compat", "packages"]        # gaming/flatpaks were not pending
    assert [r["id"] for r in rows] == ["browser", "mode_extras"]      # Wine + umu are on PATH: dropped
    assert rows[1]["payload"] == {"packages": ["redshift-gtk"]}
    # nothing pending: no probes at all
    probed.clear()
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(_state(browser="done")))
    assert b.setup_pending_items() == [] and probed == []


def test_backend_mode_extras_is_the_union_over_every_mode(monkeypatch):
    b = Backend()
    monkeypatch.setattr(b, "load_modes", lambda: {
        "work": {"packages": ["thunderbird", "redshift-gtk"], "flatpaks": []},
        "creator": types.SimpleNamespace(packages=["winetricks", "thunderbird"], flatpaks=["com.usebottles.bottles"]),
        "lite": {"id": "lite"}})
    assert b.mode_extras() == (["thunderbird", "redshift-gtk", "winetricks"], ["com.usebottles.bottles"])


def test_backend_installed_packages_parses_dpkg_query(monkeypatch):
    b = Backend()
    assert b.installed_packages([]) == set()
    monkeypatch.setattr(b, "which", lambda cmd: None)
    assert b.installed_packages(["thunderbird"]) == set()             # no dpkg-query: unknown, nothing claimed
    monkeypatch.setattr(b, "which", lambda cmd: "/usr/bin/dpkg-query")
    seen: list[list[str]] = []
    out = "thunderbird install ok installed\nredshift-gtk deinstall ok config-files\nlibfoo:amd64 install ok installed\n"
    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: (seen.append(list(argv)), CmdResult(1, out, "no packages found"))[1])
    assert b.installed_packages(["thunderbird", "redshift-gtk", "libfoo"]) == {"thunderbird", "libfoo"}
    assert seen[0][:3] == ["dpkg-query", "-W", "-f=${Package} ${Status}\n"]


def test_install_setup_item_uses_the_existing_helper_actions(monkeypatch):
    b = Backend()
    calls: list[tuple] = []
    monkeypatch.setattr(b, "run_privileged", lambda action, payload=None: calls.append((action, payload)) or HelperResult(True))
    monkeypatch.setattr(b, "install_browser", lambda bid, set_default=True: calls.append(("browser", bid, set_default)) or HelperResult(True))
    monkeypatch.setattr(b, "effective_browser", lambda: "chrome")
    for item in ({"kind": "compat", "payload": {"items": ["wine", "umu"]}},
                 {"kind": "gaming", "payload": {"items": ["steam"]}},
                 {"kind": "packages", "payload": {"packages": ["thunderbird"]}},
                 {"kind": "flatpaks", "payload": {"flatpaks": ["a.b.C"]}},
                 {"kind": "drivers", "payload": {"args": []}},
                 {"kind": "browser", "payload": {"browser": "chrome"}}):
        assert b.install_setup_item(item).ok
    assert calls == [("install-compat", {"items": ["wine", "umu"]}), ("install-gaming", {"items": ["steam"]}),
                     ("install-packages", {"packages": ["thunderbird"]}), ("install-flatpaks", {"flatpaks": ["a.b.C"]}),
                     ("install-drivers", {"args": []}), ("browser", "chrome", True)]
    res = b.install_setup_item({"kind": "mystery", "payload": {}})
    assert not res.ok and res.code == 2


def test_installing_chrome_from_setup_keeps_a_different_default_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = Backend()
    b.config_set("browser", "firefox")
    calls: list[tuple] = []
    b._mods["browsers"] = types.SimpleNamespace(
        install=lambda bid, log=print: calls.append(("install", bid)) is None,
        set_default=lambda bid: calls.append(("default", bid)) is None)
    res = b.install_setup_item({"kind": "browser", "payload": {"browser": "chrome"}})
    assert res.ok and calls == [("install", "chrome")]        # installed, but Firefox stays the default
    assert b.config_get("browser") == "firefox"
    b.config_set("browser", "chrome")
    calls.clear()
    assert b.install_setup_item({"kind": "browser", "payload": {"browser": "chrome"}}).ok
    assert calls == [("install", "chrome"), ("default", "chrome")]


def test_install_flatpaks_calls_the_helper_action(monkeypatch):
    b = Backend()
    calls: list[tuple] = []
    monkeypatch.setattr(b, "run_privileged", lambda action, payload=None: calls.append((action, payload)) or HelperResult(True))
    assert b.install_flatpaks(["com.usebottles.bottles"]).ok
    assert calls == [("install-flatpaks", {"flatpaks": ["com.usebottles.bottles"]})]


# --------------------------------------------------------------------------- pages (gi stub)
class _App:
    def __init__(self, backend):
        self.backend = backend
        self.window = None
        self.toasts: list[str] = []

    def toast(self, text):
        self.toasts.append(text)

    def show_page(self, page_id):  # noqa: ARG002
        pass


def _sync(fn, on_done=None, name="worker"):  # noqa: ARG001
    try:
        result, exc = fn(), None
    except BaseException as err:  # noqa: BLE001
        result, exc = None, err
    if on_done is not None:
        on_done(result, exc)


def _apps_page(monkeypatch, rows):
    from lindos_settings.pages import apps as apps_mod

    b = Backend()
    monkeypatch.setattr(b, "setup_pending_items", lambda: rows)
    monkeypatch.setattr(b, "browsers", lambda: [])
    monkeypatch.setattr(apps_mod, "run_async", _sync)
    monkeypatch.setattr(apps_mod, "confirm", lambda *a, **k: True)
    app = _App(b)
    page = apps_mod.AppsPage(app, model.pages_by_id(model.load_pages())["apps"])
    shown: list[bool] = []
    page._set_pending_visible = lambda visible: shown.append(bool(visible))
    return apps_mod, b, app, page, shown


ROW = {"id": "compat", "title": "Windows app support (Wine + Proton)", "subtitle": "Not installed yet",
       "status": "pending", "kind": "compat", "payload": {"items": ["wine", "umu"]}, "button": "Install now"}


def test_apps_page_hides_the_setup_section_until_something_is_pending(monkeypatch):
    _apps_mod, _b, _app, page, shown = _apps_page(monkeypatch, [])
    page._refresh_pending()
    assert shown == [False] and page._pending_items == []
    _apps_mod, _b, _app, page, shown = _apps_page(monkeypatch, [ROW])
    page._refresh_pending()
    assert shown == [True] and page._pending_items == [ROW]


def test_apps_page_reveals_the_setup_list_before_showing_its_rows(monkeypatch):
    # GTK: show_all() does nothing while the list still has no_show_all set, so the rows would stay hidden
    _apps_mod, _b, _app, page, _shown = _apps_page(monkeypatch, [ROW])
    order: list[str] = []
    page._set_pending_visible = lambda visible: order.append("visible" if visible else "hidden")
    page._pending_cards.show_all = lambda: order.append("show_all")
    page._refresh_pending()
    assert order == ["visible", "show_all"]
    order.clear()
    page._render_pending([])
    assert order == ["hidden", "show_all"]


def test_apps_page_install_now_runs_the_helper_and_removes_the_row(monkeypatch):
    _apps_mod, b, app, page, shown = _apps_page(monkeypatch, [ROW])
    ran: list[dict] = []
    monkeypatch.setattr(b, "install_setup_item", lambda item: ran.append(item) or HelperResult(True))
    page._refresh_pending()
    page._install_pending(ROW)
    assert ran == [ROW]
    assert "Windows app support (Wine + Proton) installed" in app.toasts[-1]
    # the record still says pending (nothing writes it from here), but the item does not come back
    # this session even though the live probe cannot tell
    assert page._installed_now == {"compat"} and page._pending_items == []
    assert shown[-1] is False


def test_apps_page_reports_a_failed_install_and_keeps_the_row(monkeypatch):
    _apps_mod, b, app, page, _shown = _apps_page(monkeypatch, [ROW])
    monkeypatch.setattr(b, "install_setup_item", lambda item: HelperResult(False, "", "offline: cannot download", 3))
    page._refresh_pending()
    page._install_pending(ROW)
    assert "Install failed: offline: cannot download" in app.toasts[-1]
    assert page._installed_now == set() and page._pending_items == [ROW]
    assert page._pending_busy is False


def test_apps_page_install_now_can_be_declined(monkeypatch):
    apps_mod, b, _app, page, _shown = _apps_page(monkeypatch, [ROW])
    monkeypatch.setattr(apps_mod, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(b, "install_setup_item", lambda item: (_ for _ in ()).throw(AssertionError("declined")))
    page._install_pending(ROW)
    assert page._pending_busy is False and page._installed_now == set()


def test_updates_page_describes_what_the_installer_did(monkeypatch):
    from lindos_settings import pages

    b = Backend()
    app = _App(b)
    page = pages.build_page(app, model.pages_by_id(model.builtin_pages())["updates"])
    subtitles: list[str] = []
    page.os_card = types.SimpleNamespace(set_subtitle=subtitles.append)
    page._install_state_done(model.normalize_install_state(_state(updates="done")), None)
    page._install_state_done(model.normalize_install_state(_state(updates="pending")), None)
    page._install_state_done(None, RuntimeError("boom"))
    assert "installed the available system updates" in subtitles[0]
    assert "still waiting" in subtitles[1]
    assert subtitles[2] == model.os_updates_subtitle(None)


def test_touched_settings_files_are_lf_only():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(here, "..", "root", "usr", "lib", "lindos-settings", "lindos_settings")
    for rel in ("model.py", "backend.py", os.path.join("pages", "apps.py"), os.path.join("pages", "updates.py"),
                os.path.join(here, "test_setup_pending.py")):
        path = rel if os.path.isabs(rel) else os.path.join(lib, rel)
        raw = open(path, "rb").read()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"), path
