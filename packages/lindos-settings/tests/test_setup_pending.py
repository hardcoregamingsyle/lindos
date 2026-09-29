"""Settings > Apps "Left to finish from setup" and the Updates wording: what the installer could not
do (install-state.json) with an "Install now" button per item, all through the existing helper
actions.  Pure logic in ``lindos_settings.model``, the ``Backend`` adapter, and the pages under the
gi stub (no real display, no real helper, no network).
"""
from __future__ import annotations

import json
import os
import shutil
import types

import pytest

from lindos_settings import model
from lindos_settings.backend import Backend, CmdResult, HelperResult


_PKGS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
EXTRAS_JSON = os.path.join(_PKGS, "lindos-installer", "root", "usr", "share", "lindos", "installer", "extras.json")
APPS_JSON = os.path.join(_PKGS, "lindos-setup", "root", "usr", "share", "lindos", "setup", "apps.json")
MODES_DIR = os.path.join(_PKGS, "lindos-core", "root", "usr", "share", "lindos", "modes")


def _extras():
    with open(EXTRAS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


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
    assert all(r["button"] == "Install now" for r in rows if r["id"] != "drivers")
    assert by_id["browser"]["kind"] == "browser" and by_id["browser"]["payload"] == {"browser": "chrome"}
    # what the installer's own compat / gaming steps cover (extras.json), not more: Heroic, Prism and Sober are the flatpaks step
    assert by_id["compat"]["payload"] == {"items": ["wine", "winetricks", "umu"]}
    assert by_id["gaming"]["payload"] == {"items": ["steam", "lutris"]}
    assert by_id["mode_extras"]["kind"] == "packages"
    assert by_id["mode_extras"]["payload"] == {"packages": ["thunderbird", "redshift-gtk"]}      # unique, ordered
    assert by_id["flatpaks"]["payload"] == {"flatpaks": ["org.prismlauncher.PrismLauncher"]}
    # nothing is known about this PC's graphics card or Secure Boot and no consent is recorded: nothing is
    # installed from the row, it leads to Settings > Hardware (see the driver tests below)
    assert by_id["drivers"]["kind"] == "hardware" and by_id["drivers"]["payload"] == {}
    assert by_id["drivers"]["button"] == "Open Hardware" and "confirm" not in by_id["drivers"]
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
    have = {"browser": True, "compat": {"wine", "winetricks", "umu"}, "gaming": {"steam", "lutris"},
            "packages": {"thunderbird"}, "flatpaks": {"com.usebottles.bottles"}}
    rows = model.pending_setup_items(state, mode_packages=["thunderbird"], mode_flatpaks=["com.usebottles.bottles"],
                                     installed=have)
    assert [r["id"] for r in rows] == ["drivers"]           # no live check for drivers: it stays until recorded
    # partly installed: only what is missing is offered
    have = {"compat": {"wine", "umu"}, "gaming": {"steam"}, "packages": {"thunderbird"}, "flatpaks": set()}
    rows = {r["id"]: r for r in model.pending_setup_items(
        state, mode_packages=["thunderbird", "redshift-gtk"], mode_flatpaks=["a.b.C"], installed=have)}
    assert rows["gaming"]["payload"]["items"] == ["lutris"]
    assert rows["compat"]["payload"]["items"] == ["winetricks"]         # Wine and umu alone do not finish the compat step
    assert rows["mode_extras"]["payload"]["packages"] == ["redshift-gtk"]
    assert rows["flatpaks"]["payload"]["flatpaks"] == ["a.b.C"]
    # the old boolean form of the compat live check still means "everything is there"
    assert "compat" not in {r["id"] for r in model.pending_setup_items(state, installed={"compat": True})}


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
    # the installer's GPU rule allows the install here: Intel graphics, consent recorded
    rows = model.pending_setup_items(state, mode_packages=packages, mode_flatpaks=flatpaks,
                                     installed={"drivers": {"vendors": ["intel"], "secure_boot": "enabled", "consent": True}})
    actions = {"browser": "install-browser", "compat": "install-compat", "gaming": "install-gaming",
               "packages": "install-packages", "flatpaks": "install-flatpaks", "drivers": "install-drivers"}
    assert {r["kind"] for r in rows} == set(actions)
    for row in rows:
        helper.validate_payload(actions[row["kind"]], dict(row["payload"]))


# --------------------------------------------------------------------------- the installer's own lists
def test_gaming_and_compat_rows_mirror_the_installers_steps():
    """Settings cannot read extras.json at run time (the installer package is on the medium only), so it
    mirrors the installer's compat/gaming lists: this test is what keeps the mirror honest."""
    extras = _extras()
    assert list(model.SETUP_GAMING_ITEMS) == extras["gaming"]
    assert list(model.SETUP_COMPAT_ITEMS) == extras["compat"]
    # Heroic, Prism and Sober are the installer's separate flatpaks step, not the gaming step
    assert not {"heroic", "prism", "sober"} & set(model.SETUP_GAMING_ITEMS)
    assert {"com.heroicgameslauncher.hgl", "org.prismlauncher.PrismLauncher", "org.vinegarhq.Sober"} <= set(extras["flatpaks"])
    helper = pytest.importorskip("lindos.helper")
    assert set(model.SETUP_GAMING_ITEMS) <= set(helper.GAMING_ITEMS)
    assert set(model.SETUP_COMPAT_ITEMS) <= set(helper.COMPAT_ITEMS)


def test_the_gaming_row_never_installs_what_the_flatpak_row_installs():
    extras = _extras()
    state = _state(gaming="pending", flatpaks="pending")
    rows = {r["id"]: r for r in model.pending_setup_items(state, mode_flatpaks=extras["flatpaks"])}
    assert rows["gaming"]["payload"] == {"items": ["steam", "lutris"]}
    assert "com.heroicgameslauncher.hgl" in rows["flatpaks"]["payload"]["flatpaks"]      # Heroic: once, as a Flatpak
    # Steam and Lutris there: the gaming row is done - it does not keep offering launchers its step never covered
    have = {"gaming": {"steam", "lutris"}, "flatpaks": set()}
    rows = {r["id"] for r in model.pending_setup_items(state, mode_flatpaks=extras["flatpaks"], installed=have)}
    assert rows == {"flatpaks"}
    assert "Steam, Lutris" in model.SETUP_STEP_TITLES["gaming"] and "Heroic" in model.SETUP_STEP_TITLES["flatpaks"]


# --------------------------------------------------------------------------- the drivers row honours the installer's limits
_INTEL, _AMD, _NV = ["intel"], ["amd"], ["nvidia"]


@pytest.mark.parametrize("vendors, sb, consent, kind, args", [
    (_NV, "enabled", True, "hardware", None),                   # NVIDIA + Secure Boot: a key-enrolment screen at the next start
    (_NV, "unknown", True, "hardware", None),                   # Secure Boot undetectable counts as on
    (["intel", "nvidia"], "enabled", True, "hardware", None),   # hybrid laptop
    (None, "enabled", True, "hardware", None),                  # graphics undetectable counts as "maybe NVIDIA"
    ([], "unknown", True, "hardware", None),
    (_NV, "disabled", False, "hardware", None),                 # no consent recorded: nothing proprietary
    (_AMD, "disabled", False, "hardware", None),
    (["other"], "disabled", True, "hardware", None),            # a VM's adapter: no driver to install
    (["other"], "enabled", True, "hardware", None),
    (_NV, "disabled", True, "drivers", []),                     # consent + Secure Boot definitely off: the helper's detection
    (["intel", "nvidia"], "disabled", True, "drivers", []),
    (_AMD, "enabled", True, "drivers", ["--amd"]),              # no NVIDIA possible: Secure Boot is no obstacle ...
    (_INTEL, "unknown", True, "drivers", ["--intel"]),          # ... and the vendors are named, so NVIDIA is never added
    (["amd", "intel", "other"], "enabled", True, "drivers", ["--amd", "--intel"]),
])
def test_driver_plan_follows_the_installer_and_the_silent_retry(vendors, sb, consent, kind, args):
    plan = model.driver_setup_plan(vendors, sb, consent)
    assert plan["kind"] == kind
    assert plan["note"]
    if kind == "drivers":
        assert plan["payload"] == {"args": args} and plan["button"] == "Install now"
        assert plan["confirm"]["title"] and plan["confirm"]["body"] and plan["confirm"]["accept"]
    else:
        assert plan["payload"] == {} and plan["button"] == "Open Hardware" and plan["confirm"] == {}
        assert "Settings > Hardware" in plan["note"] or "no graphics driver to install" in plan["note"]


def test_driver_plan_never_allows_a_proprietary_install_outside_the_limits():
    for vendors in (None, [], ["other"], ["nvidia"], ["amd"], ["intel"], ["intel", "nvidia"], ["amd", "intel"]):
        for sb in ("enabled", "disabled", "unknown", "", "weird"):
            for consent in (True, False):
                plan = model.driver_setup_plan(vendors, sb, consent)
                if plan["kind"] != "drivers":
                    continue
                maybe_nvidia = not vendors or "nvidia" in vendors
                assert consent, (vendors, sb)
                assert not maybe_nvidia or sb == "disabled", (vendors, sb, consent)
                if maybe_nvidia:
                    assert plan["payload"] == {"args": []}                  # NVIDIA is part of it, Secure Boot is off
                else:                                                       # only ever the named free-stack vendors
                    assert plan["payload"]["args"] and set(plan["payload"]["args"]) <= {"--amd", "--intel"}


def test_driver_plan_explains_itself_plainly():
    nv_sb = model.driver_setup_plan(_NV, "enabled", True)["note"]
    assert "Secure Boot is on" in nv_sb and "key-enrolment" in nv_sb and "Settings > Hardware" in nv_sb
    assert "could not be checked" in model.driver_setup_plan(_NV, "unknown", True)["note"]
    no_consent = model.driver_setup_plan(_NV, "disabled", False)["note"]
    assert "consent" in no_consent and "nothing is installed from here" in no_consent
    nv = model.driver_setup_plan(_NV, "disabled", True)
    assert "proprietary NVIDIA driver" in nv["confirm"]["body"] and "Secure Boot is off" in nv["confirm"]["body"]
    assert "restart" in nv["confirm"]["body"]
    free = model.driver_setup_plan(_AMD, "enabled", True)
    assert "No NVIDIA driver is involved" in free["confirm"]["body"] and "media driver" not in free["confirm"]["body"]
    assert "media driver (proprietary)" in model.driver_setup_plan(["intel"], "enabled", True)["confirm"]["body"]


def test_the_drivers_row_is_built_from_the_live_facts():
    state = _state(drivers="pending")
    nv_sb = {"drivers": {"vendors": ["nvidia"], "secure_boot": "enabled", "consent": True}}
    row = model.pending_setup_items(state, installed=nv_sb)[0]
    assert row["id"] == "drivers" and row["kind"] == "hardware" and row["button"] == "Open Hardware"
    assert row["payload"] == {} and "confirm" not in row
    # the row still says what the silent retry does (free drivers and firmware only) and why nothing is installed here
    assert "Waiting for an internet connection" in row["subtitle"] and "free drivers and firmware" in row["subtitle"]
    assert "Secure Boot is on" in row["subtitle"]
    ok = model.pending_setup_items(_state(drivers="failed"), installed={"drivers": {
        "vendors": ["nvidia"], "secure_boot": "disabled", "consent": True}})[0]
    assert ok["kind"] == "drivers" and ok["payload"] == {"args": []} and ok["button"] == "Install now"
    assert ok["confirm"]["title"] == "Install the graphics driver?" and "Couldn't be installed" in ok["subtitle"]
    # no facts at all (the probe failed): the cautious answer
    assert model.pending_setup_items(state)[0]["kind"] == "hardware"
    assert model.pending_setup_items(state, installed={"drivers": "garbage"})[0]["kind"] == "hardware"


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
    monkeypatch.setattr(b, "compat_status", lambda: (probed.append("compat"), {"wine": True, "umu": True, "winetricks": True})[1])
    monkeypatch.setattr(b, "installed_packages", lambda names: (probed.append("packages"), {"thunderbird"})[1])
    monkeypatch.setattr(b, "launcher_states", lambda: probed.append("gaming") or [])
    monkeypatch.setattr(b, "flatpak_apps", lambda: probed.append("flatpaks") or set())
    b._mods["browsers"] = types.SimpleNamespace(is_installed=lambda bid: (probed.append("browser"), False)[1])
    rows = b.setup_pending_items()
    assert sorted(probed) == ["browser", "compat", "packages"]        # gaming/flatpaks were not pending
    assert [r["id"] for r in rows] == ["browser", "mode_extras"]      # Wine, winetricks and umu are on PATH: dropped
    assert rows[1]["payload"] == {"packages": ["redshift-gtk"]}
    # nothing pending: no probes at all
    probed.clear()
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(_state(browser="done")))
    assert b.setup_pending_items() == [] and probed == []


def _sys_root(tmp_path, monkeypatch):
    """A hermetic system root (LINDOS_ROOT) with nothing in it."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    return root


def _write_apps_json(root, apps):
    target = root / "usr" / "share" / "lindos" / "setup"
    target.mkdir(parents=True, exist_ok=True)
    (target / "apps.json").write_text(json.dumps({"schema": 1, "apps": apps}), encoding="utf-8")


def test_backend_mode_extras_is_the_union_over_every_mode(tmp_path, monkeypatch):
    root = _sys_root(tmp_path, monkeypatch)
    # this catalogue has no apt app preselected for a Mode: the union is just the modes' lists
    _write_apps_json(root, [{"id": "onlyoffice", "kind": "flatpak", "flatpaks": ["org.onlyoffice.desktopeditors"], "default_on_modes": ["work"]}])
    b = Backend()
    monkeypatch.setattr(b, "load_modes", lambda: {
        "work": {"packages": ["thunderbird", "redshift-gtk"], "flatpaks": []},
        "creator": types.SimpleNamespace(packages=["winetricks", "thunderbird"], flatpaks=["com.usebottles.bottles"]),
        "lite": {"id": "lite"}})
    assert b.mode_extras() == (["thunderbird", "redshift-gtk", "winetricks"], ["com.usebottles.bottles"])


def test_mode_extras_include_the_catalogue_apps_the_installer_installs(tmp_path, monkeypatch):
    """GIMP, Krita and Kdenlive are in lindos-setup's apps.json, in no mode.json: the retry must still offer them."""
    root = _sys_root(tmp_path, monkeypatch)
    _write_apps_json(root, [
        {"id": "creative", "kind": "apt", "packages": ["gimp", "krita", "kdenlive"], "default_on_modes": ["creator"]},
        {"id": "office", "kind": "apt", "packages": ["thunderbird"], "default_on_modes": ["work"]},          # already a Mode package
        {"id": "optional", "kind": "apt", "packages": ["inkscape"], "default_on_modes": []},                 # not preselected: not installed
        {"id": "nodefault", "kind": "apt", "packages": ["blender"]},
        {"id": "wine", "kind": "script", "action": "install-compat", "items": ["wine"], "default_on_modes": ["work"]},
        {"id": "junk", "kind": "apt", "packages": "gimp2", "default_on_modes": ["work"]}, "not-an-app"])
    b = Backend()
    monkeypatch.setattr(b, "load_modes", lambda: {"work": {"packages": ["thunderbird"], "flatpaks": []}})
    packages, flatpaks = b.mode_extras()
    assert packages == ["thunderbird", "gimp", "krita", "kdenlive"] and flatpaks == []
    # the mirror is what is used when lindos-setup (or its file) is not there, or the file is not a catalogue
    for text in (None, "{corrupt", "[]", '{"apps": "no"}'):
        target = root / "usr" / "share" / "lindos" / "setup" / "apps.json"
        if text is None:
            target.unlink()
        else:
            target.write_text(text, encoding="utf-8")
        assert b.mode_extras()[0] == ["thunderbird", "gimp", "krita", "kdenlive"], text
    # ...but a readable catalogue with no such apps means exactly that (no stale mirror on top)
    _write_apps_json(root, [{"id": "onlyoffice", "kind": "flatpak", "default_on_modes": ["work"]}])
    assert b.mode_extras()[0] == ["thunderbird"]


def test_the_retry_finishes_every_package_the_installer_installs(tmp_path, monkeypatch):
    """The recorded mode_extras=pending step keeps its row until GIMP, Krita and Kdenlive are there too."""
    root = _sys_root(tmp_path, monkeypatch)
    (root / "usr" / "share" / "lindos" / "setup").mkdir(parents=True)
    shutil.copyfile(APPS_JSON, str(root / "usr" / "share" / "lindos" / "setup" / "apps.json"))
    modes = pytest.importorskip("lindos.modes").load_modes(MODES_DIR)
    b = Backend()
    monkeypatch.setattr(b, "load_modes", lambda: modes)
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(_state(mode_extras="pending")))
    packages, _flatpaks = b.mode_extras()
    mode_only = {p for m in modes.values() for p in m.packages}
    # everything a Mode package list names is installed; the catalogue apps are not
    monkeypatch.setattr(b, "installed_packages", lambda names: set(mode_only))
    rows = b.setup_pending_items()
    assert [r["id"] for r in rows] == ["mode_extras"], "GIMP, Krita and Kdenlive are still missing: the row must stay"
    assert rows[0]["payload"] == {"packages": ["gimp", "krita", "kdenlive"]}
    assert set(rows[0]["payload"]["packages"]) <= set(packages)
    # ...and they finish through the helper's own validation
    helper = pytest.importorskip("lindos.helper")
    helper.validate_payload("install-packages", dict(rows[0]["payload"]))
    # everything installed: the row goes
    monkeypatch.setattr(b, "installed_packages", lambda names: set(names))
    assert b.setup_pending_items() == []


def test_settings_derives_exactly_the_installers_apt_set(tmp_path, monkeypatch):
    """Drift guard: the mode.json packages plus the apt catalogue apps ARE the installer's mode_extras set
    (extras.json 'apt', generated by build/lib/installer_extras.py), and the mirrors match the real files."""
    root = _sys_root(tmp_path, monkeypatch)
    (root / "usr" / "share" / "lindos" / "setup").mkdir(parents=True)
    shutil.copyfile(APPS_JSON, str(root / "usr" / "share" / "lindos" / "setup" / "apps.json"))
    modes = pytest.importorskip("lindos.modes").load_modes(MODES_DIR)
    b = Backend()
    monkeypatch.setattr(b, "load_modes", lambda: modes)
    packages, flatpaks = b.mode_extras()
    extras = _extras()
    # Settings looks where lindos-setup really ships the catalogue
    assert os.path.join(_PKGS, "lindos-setup", "root", *model.SETUP_APPS_JSON.strip("/").split("/")) == APPS_JSON
    assert set(packages) == set(extras["apt"])
    assert set(flatpaks) == set(extras["flatpaks"])
    # the mirror used without lindos-setup equals what the real catalogue yields
    assert list(model.SETUP_CATALOGUE_APT) == b.catalogue_apt_packages()
    with open(APPS_JSON, encoding="utf-8") as fh:
        assert model.catalogue_apt_packages(json.load(fh)) == list(model.SETUP_CATALOGUE_APT)
    assert model.catalogue_apt_packages(None) is None and model.catalogue_apt_packages({"apps": 3}) is None


# --------------------------------------------------------------------------- what this PC allows for the drivers step
def test_secure_boot_state_is_read_like_the_installer_does(tmp_path, monkeypatch):
    root = _sys_root(tmp_path, monkeypatch)
    b = Backend()
    monkeypatch.setattr(b, "which", lambda cmd: None)                       # no mokutil
    assert b.secure_boot_state() == "disabled"                              # no /sys/firmware/efi: a legacy-BIOS boot
    efivars = root / "sys" / "firmware" / "efi" / "efivars"
    efivars.mkdir(parents=True)
    assert b.secure_boot_state() == "unknown"                               # UEFI, but the variable cannot be read
    var = efivars / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    var.write_bytes(b"\x06\x00\x00\x00\x01")
    assert b.secure_boot_state() == "enabled"
    var.write_bytes(b"\x06\x00\x00\x00\x00")
    assert b.secure_boot_state() == "disabled"
    var.write_bytes(b"")
    assert b.secure_boot_state() == "unknown"
    # mokutil first, exactly the two words the installer looks for
    monkeypatch.setattr(b, "which", lambda cmd: "/usr/bin/mokutil" if cmd == "mokutil" else None)
    monkeypatch.setattr(b, "run", lambda argv, **kw: CmdResult(0, "SecureBoot enabled\nSecureBoot validation is disabled in shim\n", "", list(argv)))
    assert b.secure_boot_state() == "enabled"
    monkeypatch.setattr(b, "run", lambda argv, **kw: CmdResult(0, "SecureBoot disabled\n", "", list(argv)))
    assert b.secure_boot_state() == "disabled"
    var.write_bytes(b"\x06\x00\x00\x00\x01")
    monkeypatch.setattr(b, "run", lambda argv, **kw: CmdResult(1, "", "EFI variables are not supported on this system", list(argv)))
    assert b.secure_boot_state() == "enabled"                               # mokutil could not say: the EFI variable does


def test_driver_consent_is_the_installers_marker_file(tmp_path, monkeypatch):
    root = _sys_root(tmp_path, monkeypatch)
    b = Backend()
    assert b.driver_consent() is False
    (root / "var" / "lib" / "lindos").mkdir(parents=True)
    (root / "var" / "lib" / "lindos" / "driver-proprietary-consent").write_text("", encoding="utf-8")
    assert b.driver_consent() is True


def test_driver_facts_fail_cautiously(tmp_path, monkeypatch):
    _sys_root(tmp_path, monkeypatch)
    b = Backend()
    monkeypatch.setattr(b, "gpu_info", lambda: [{"vendor": "nvidia", "model": "GeForce"}, {"vendor": "intel"}])
    monkeypatch.setattr(b, "secure_boot_state", lambda: "enabled")
    assert b.driver_setup_facts() == {"vendors": ["nvidia", "intel"], "secure_boot": "enabled", "consent": False}
    monkeypatch.setattr(b, "gpu_info", lambda: (_ for _ in ()).throw(OSError("lspci")))
    monkeypatch.setattr(b, "secure_boot_state", lambda: (_ for _ in ()).throw(OSError("mokutil")))
    assert b.driver_setup_facts() == {"vendors": [], "secure_boot": "unknown", "consent": False}
    assert b.driver_setup_plan()["kind"] == "hardware"


def test_backend_builds_the_drivers_row_from_this_pc(monkeypatch):
    b = Backend()
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(_state(drivers="pending")))
    monkeypatch.setattr(b, "mode_extras", lambda: ([], []))
    facts = {"vendors": ["nvidia"], "secure_boot": "enabled", "consent": True}
    monkeypatch.setattr(b, "driver_setup_facts", lambda: dict(facts))
    rows = b.setup_pending_items()
    assert [(r["id"], r["kind"]) for r in rows] == [("drivers", "hardware")]
    facts["secure_boot"] = "disabled"                                       # Secure Boot off: the installer's rule allows it
    assert [(r["id"], r["kind"]) for r in b.setup_pending_items()] == [("drivers", "drivers")]


def test_backend_probes_the_compat_pieces_one_by_one(monkeypatch):
    """Wine and umu on PATH do not finish the compat step: winetricks is part of it."""
    b = Backend()
    monkeypatch.setattr(b, "install_state", lambda: model.normalize_install_state(_state(compat="pending")))
    monkeypatch.setattr(b, "mode_extras", lambda: ([], []))
    monkeypatch.setattr(b, "compat_status", lambda: {"wine": True, "umu": True, "winetricks": False, "lindos_run": True})
    assert [r["payload"] for r in b.setup_pending_items()] == [{"items": ["winetricks"]}]
    monkeypatch.setattr(b, "compat_status", lambda: {"wine": True, "umu": True, "winetricks": True})
    assert b.setup_pending_items() == []
    monkeypatch.setattr(b, "compat_status", lambda: {})
    assert [r["payload"] for r in b.setup_pending_items()] == [{"items": ["wine", "winetricks", "umu"]}]


@pytest.mark.parametrize("facts, args", [
    ({"vendors": ["nvidia"], "secure_boot": "enabled", "consent": True}, None),          # the MOK-screen case
    ({"vendors": ["intel", "nvidia"], "secure_boot": "unknown", "consent": True}, None),
    ({"vendors": [], "secure_boot": "disabled", "consent": True}, None),                 # graphics unknown: maybe NVIDIA, no driver to pick
    ({"vendors": ["nvidia"], "secure_boot": "disabled", "consent": False}, None),        # no recorded consent
    ({"vendors": ["amd"], "secure_boot": "disabled", "consent": False}, None),
    ({"vendors": ["nvidia"], "secure_boot": "disabled", "consent": True}, []),
    ({"vendors": ["amd"], "secure_boot": "enabled", "consent": True}, ["--amd"]),
])
def test_drivers_install_now_honours_the_installers_limits(monkeypatch, facts, args):
    allowed = args is not None
    b = Backend()
    calls: list[tuple] = []
    monkeypatch.setattr(b, "run_privileged", lambda action, payload=None: calls.append((action, payload)) or HelperResult(True))
    monkeypatch.setattr(b, "driver_setup_facts", lambda: dict(facts))
    # a stale or forged 'drivers' row (whatever its payload says) cannot get around the check on this PC
    res = b.install_setup_item({"kind": "drivers", "payload": {"args": ["--nvidia-proprietary"]}})
    assert res.ok is allowed
    assert calls == ([("install-drivers", {"args": args})] if allowed else [])
    if not allowed:
        assert res.code == 2 and res.err and ("Settings > Hardware" in res.err or "no graphics driver" in res.err)
    # the Hardware row never installs anything, whatever this PC looks like
    calls.clear()
    assert not b.install_setup_item({"kind": "hardware", "payload": {}}).ok and calls == []


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
    # this PC: Intel graphics, consent recorded - the installer's own rule lets the GPU install through
    monkeypatch.setattr(b, "driver_setup_facts", lambda: {"vendors": ["intel"], "secure_boot": "enabled", "consent": True})
    for item in ({"kind": "compat", "payload": {"items": ["wine", "umu"]}},
                 {"kind": "gaming", "payload": {"items": ["steam"]}},
                 {"kind": "packages", "payload": {"packages": ["thunderbird"]}},
                 {"kind": "flatpaks", "payload": {"flatpaks": ["a.b.C"]}},
                 {"kind": "drivers", "payload": {"args": []}},
                 {"kind": "browser", "payload": {"browser": "chrome"}}):
        assert b.install_setup_item(item).ok
    assert calls == [("install-compat", {"items": ["wine", "umu"]}), ("install-gaming", {"items": ["steam"]}),
                     ("install-packages", {"packages": ["thunderbird"]}), ("install-flatpaks", {"flatpaks": ["a.b.C"]}),
                     ("install-drivers", {"args": ["--intel"]}), ("browser", "chrome", True)]
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


HARDWARE_ROW = {"id": "drivers", "title": "Drivers and firmware", "subtitle": "Waiting for an internet connection.",
                "status": "pending", "kind": "hardware", "payload": {}, "button": "Open Hardware"}
DRIVERS_ROW = {"id": "drivers", "title": "Drivers and firmware", "subtitle": "Waiting for an internet connection.",
               "status": "pending", "kind": "drivers", "payload": {"args": []}, "button": "Install now",
               "confirm": {"title": "Install the graphics driver?", "body": "Installs the graphics driver for this PC (Intel).",
                           "accept": "Install now"}}


def test_apps_page_hardware_row_opens_settings_hardware_and_installs_nothing(monkeypatch):
    apps_mod, b, app, page, _shown = _apps_page(monkeypatch, [HARDWARE_ROW])
    opened: list[str] = []
    app.show_page = opened.append
    monkeypatch.setattr(apps_mod, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing to confirm")))
    monkeypatch.setattr(b, "install_setup_item", lambda item: (_ for _ in ()).throw(AssertionError("nothing is installed from here")))
    page._refresh_pending()
    assert page._pending_items == [HARDWARE_ROW]
    page._install_pending(HARDWARE_ROW)
    assert opened == ["hardware"]
    assert page._pending_busy is False and page._installed_now == set() and app.toasts == []


def test_apps_page_drivers_row_confirms_with_what_it_will_install(monkeypatch):
    apps_mod, b, app, page, _shown = _apps_page(monkeypatch, [DRIVERS_ROW])
    asked: list[tuple] = []
    monkeypatch.setattr(apps_mod, "confirm", lambda window, title, body, accept="OK", **k: asked.append((title, body, accept)) or True)
    ran: list[dict] = []
    monkeypatch.setattr(b, "install_setup_item", lambda item: ran.append(item) or HelperResult(True))
    page._refresh_pending()
    page._install_pending(DRIVERS_ROW)
    assert asked == [("Install the graphics driver?", "Installs the graphics driver for this PC (Intel).", "Install now")]
    assert ran == [DRIVERS_ROW] and "Drivers and firmware installed" in app.toasts[-1]
    # the other rows keep the generic wording
    asked.clear()
    page._install_pending(ROW)
    assert asked[0][0] == "Install Windows app support (Wine + Proton)?" and "official source" in asked[0][1]


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
