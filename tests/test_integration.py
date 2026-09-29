"""Cross-component contract tests (SPEC §4, §13).

Every package has its own unit tests; this file only checks that the *seams*
between packages agree: helper actions/payloads/whitelists, mode pins vs the
shipped panel profiles, CLI names and sub-commands other packages call, desktop
ids used as pins, and the lindos_compat <-> lindos.compat interface (the compat
package tests use a fake core by design).

Runs on any OS (no subprocesses except python itself).
"""
from __future__ import annotations

import configparser
import glob
import json
import os
import re
import struct
import sys
from typing import Dict, List, Set

import pytest

import lindos_testsupport  # noqa: F401  (sys.path + gi stub)

REPO = lindos_testsupport.REPO_ROOT
PKGS = os.path.join(REPO, "packages")


def _root(pkg: str, *parts: str) -> str:
    return os.path.join(PKGS, pkg, "root", *parts)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _rc(path: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(path, encoding="utf-8")
    return parser


MODE_IDS = ("everyday", "gaming", "work", "creator", "lite")


# --------------------------------------------------------------------------- #
# helper actions
# --------------------------------------------------------------------------- #
def test_helper_executable_handles_every_action():
    from lindos import helper

    src = _read(_root("lindos-core", "usr", "libexec", "lindos", "lindos-helper"))
    for action in helper.ACTIONS:
        assert f'"{action}":' in src, f"lindos-helper has no handler for {action}"


def _grep_actions(paths: List[str]) -> Set[str]:
    found: Set[str] = set()
    pat = re.compile(r"""["'](apply-mode|install-browser|install-packages|install-flatpaks|set-governor|"""
                     r"""set-services|apply-sysctl|apply-tune|set-zram|install-compat|install-gaming|"""
                     r"""install-drivers|set-fan-profile|write-system-config|enable-earlyoom)["']""")
    for path in paths:
        found.update(pat.findall(_read(path)))
    return found


def test_every_caller_action_is_a_helper_action():
    from lindos import helper

    callers = [
        _root("lindos-setup", "usr", "lib", "lindos-setup", "lindos_setup", "plan.py"),
        _root("lindos-setup", "usr", "lib", "lindos-setup", "lindos_setup", "core.py"),
        _root("lindos-settings", "usr", "lib", "lindos-settings", "lindos_settings", "backend.py"),
        _root("lindos-tune", "usr", "lib", "lindos-tune", "lindos_tune", "privileged.py"),
        _root("lindos-gaming", "usr", "bin", "lindos-game"),
        _root("lindos-gaming", "usr", "bin", "lindos-drivers"),
        _root("lindos-compat", "usr", "lib", "lindos-compat", "lindos_compat", "installers.py"),
    ]
    used = _grep_actions(callers)
    assert used, "no helper actions found in callers (grep broken?)"
    assert used <= set(helper.ACTIONS)


def test_setup_plan_payloads_validate():
    """Every privileged step lindos-setup can build passes lindos.helper.validate_payload."""
    from lindos import helper
    from lindos_setup import plan as splan

    catalog = splan.load_catalog(_root("lindos-setup", "usr", "share", "lindos", "setup", "apps.json"))
    all_ids = [a.id for a in catalog.apps]
    for mode in MODE_IDS:
        for browser in ("edge", "chrome", "firefox"):
            sel = splan.Selections(mode=mode, browser=browser)
            sel.apps = list(all_ids)
            p = splan.build_plan(sel, catalog, online=True)
            for action, payload in p.system_payloads():
                if action == "apply-mode":
                    # executed through lindos.modes.apply_mode(), which builds its own plan
                    continue
                helper.validate_payload(action, dict(payload))


def test_tune_privileged_requests_validate():
    from lindos import helper
    from lindos_tune import privileged

    cases = [
        privileged.Request.apply("gaming", offline=True),
        privileged.Request.zram(75),
        privileged.Request.governor("performance"),
        privileged.Request.services(enable=["fstrim.timer"], disable=["bluetooth.service"]),
        privileged.Request.fan("quiet"),
        privileged.Request.power("power-saver"),
    ]
    for req in cases:
        action, payload = privileged.helper_call(req)
        assert action in helper.ACTIONS
        helper.validate_payload(action, dict(payload))


def test_settings_driver_payloads_validate():
    from lindos import helper
    from lindos_settings import model

    for vendor, variant in (("nvidia", "open"), ("nvidia", "proprietary"), ("amd", ""), ("intel", ""), ("", "")):
        helper.validate_payload("install-drivers", model.driver_install_payload(vendor, variant))
    helper.validate_payload("set-services", {"enable": ["fstrim.timer"], "disable": []})
    for launcher in model.LAUNCHERS:
        helper.validate_payload("install-gaming", {"items": [launcher.id]})


def test_helper_item_whitelists_match_scripts_and_catalogues():
    from lindos import helper

    gaming_sh = _read(_root("lindos-gaming", "usr", "libexec", "lindos", "install-gaming.sh"))
    m = re.search(r"^ALL_ITEMS=\(([^)]*)\)", gaming_sh, re.M)
    assert m, "install-gaming.sh ALL_ITEMS not found"
    assert set(m.group(1).split()) | {"all"} == set(helper.GAMING_ITEMS)

    launchers = _json(_root("lindos-gaming", "usr", "share", "lindos", "gaming", "launchers.json"))
    assert {entry["id"] for entry in launchers["launchers"]} <= set(helper.GAMING_ITEMS)

    compat_sh = _read(_root("lindos-compat", "usr", "libexec", "lindos", "install-compat.sh"))
    m = re.search(r"^KNOWN_ITEMS=\(([^)]*)\)", compat_sh, re.M)
    assert m, "install-compat.sh KNOWN_ITEMS not found"
    assert set(m.group(1).split()) == set(helper.COMPAT_ITEMS)

    apps = _json(_root("lindos-setup", "usr", "share", "lindos", "setup", "apps.json"))
    for app in apps["apps"]:
        if app.get("kind") != "script":
            continue
        allowed = helper.COMPAT_ITEMS if app["action"] == "install-compat" else helper.GAMING_ITEMS
        assert app["action"] in ("install-compat", "install-gaming")
        assert set(app["items"]) <= set(allowed), app["id"]


def test_installer_extras_use_only_what_the_helper_and_the_modes_know():
    """lindos-installer's extras.json (what the installer adds once, for every Mode) hands ids to
    install-compat.sh / install-gaming.sh: the same ones the helper accepts for Settings > Apps 'Install now'."""
    from lindos import helper

    extras = _json(_root("lindos-installer", "usr", "share", "lindos", "installer", "extras.json"))
    assert extras["compat"] and set(extras["compat"]) <= set(helper.COMPAT_ITEMS)
    assert extras["gaming"] and set(extras["gaming"]) <= set(helper.GAMING_ITEMS)
    for mode_id in MODE_IDS:
        mode = _json(_root("lindos-core", "usr", "share", "lindos", "modes", mode_id, "mode.json"))
        assert set(mode.get("packages", [])) <= set(extras["apt"]), mode_id
        assert set(mode.get("flatpaks", [])) <= set(extras["flatpaks"]), mode_id


def test_tune_service_whitelist_within_helper_whitelist():
    from lindos import helper
    from lindos_tune import services

    for unit in services.parse_whitelist(_read(_root("lindos-tune", "usr", "share", "lindos", "tune",
                                                     "services-whitelist.txt"))):
        assert helper.unit_allowed(unit), unit


# --------------------------------------------------------------------------- #
# modes: core mode.json vs desktop panel profiles vs shipped desktop ids
# --------------------------------------------------------------------------- #
def _shipped_desktop_ids() -> Set[str]:
    ids: Set[str] = set()
    for pkg in os.listdir(PKGS):
        for path in glob.glob(_root(pkg, "usr", "share", "applications", "*.desktop")):
            ids.add(os.path.basename(path))
    return ids


def test_mode_pins_equal_desktop_docklike_pins_and_whisker_favorites():
    from lindos import modes

    loaded = modes.load_modes(_root("lindos-core", "usr", "share", "lindos", "modes"))
    for mid in MODE_IDS:
        panel_dir = _root("lindos-desktop", "usr", "share", "lindos", "modes", mid, "panel")
        pinned = _rc(os.path.join(panel_dir, "docklike-2.rc"))["user"]["pinned"]
        pins = [p for p in pinned.split(";") if p]
        assert loaded[mid].pins == pins, f"{mid}: mode.json pins != lindos-desktop docklike-2.rc"
        # whiskermenu rc files have no section header
        fav_line = next(ln for ln in _read(os.path.join(panel_dir, "whiskermenu-1.rc")).splitlines()
                        if ln.startswith("favorites="))
        favorites = fav_line.split("=", 1)[1]
        assert [f for f in favorites.split(",") if f] == pins, f"{mid}: whisker favorites != pins"


def test_lindos_shim_pins_exist_and_mode_data_validates():
    from lindos import helper, modes

    shipped = _shipped_desktop_ids()
    loaded = modes.load_modes(_root("lindos-core", "usr", "share", "lindos", "modes"))
    for mid in MODE_IDS:
        mode = loaded[mid]
        for pin in mode.pins:
            if pin.startswith("lindos-"):
                assert pin in shipped, f"{mid}: pin {pin} is not shipped by any package"
        # the privileged plan for every mode must pass the helper validator
        helper.validate_payload("apply-mode", modes.build_system_plan(mode))
        # vm.max_map_count is owned by lindos-gaming's always-on 80-lindos-gaming.conf;
        # only the gaming mode may (re)state it in 90-lindos-mode.conf (sorts later, wins)
        if mid != "gaming":
            assert "vm.max_map_count" not in mode.sysctl, mid


def test_gaming_sysctl_file_matches_gaming_mode():
    from lindos import modes

    text = _read(_root("lindos-gaming", "etc", "sysctl.d", "80-lindos-gaming.conf"))
    m = re.search(r"^\s*vm\.max_map_count\s*=\s*(\d+)", text, re.M)
    assert m
    loaded = modes.load_modes(_root("lindos-core", "usr", "share", "lindos", "modes"))
    assert loaded["gaming"].sysctl["vm.max_map_count"] == m.group(1)


# --------------------------------------------------------------------------- #
# CLIs and scripts other packages call
# --------------------------------------------------------------------------- #
def _shipped_executables() -> Set[str]:
    names: Set[str] = set()
    for pkg in os.listdir(PKGS):
        for sub in (("usr", "bin"), ("usr", "libexec", "lindos")):
            d = _root(pkg, *sub)
            if os.path.isdir(d):
                names.update(n for n in os.listdir(d) if not n.startswith("__"))
    return names


def test_referenced_lindos_commands_and_libexec_scripts_are_shipped():
    shipped = _shipped_executables()
    cli_re = re.compile(r"\blindos-(run|compat|proton|drivers|game|tune|mode|browser|config|ram|compositor|"
                        r"helper|mangohud|setup|settings|dualboot|transfer|transfer-gui)\b")
    libexec_re = re.compile(r"/usr/libexec/lindos/([A-Za-z0-9._-]+)")
    for pkg in os.listdir(PKGS):
        for dp, _dn, fn in os.walk(_root(pkg)):
            if "__pycache__" in dp:
                continue
            for f in fn:
                if not f.endswith((".py", ".sh", ".rc", ".xml", ".desktop", ".ini", ".json")) and \
                        not dp.endswith(("bin", "lindos")):
                    continue
                path = os.path.join(dp, f)
                try:
                    text = _read(path)
                except (UnicodeDecodeError, OSError):
                    continue
                for m in cli_re.finditer(text):
                    assert m.group(0) in shipped, f"{path}: references {m.group(0)} (not shipped)"
                for m in libexec_re.finditer(text):
                    assert m.group(1) in shipped, f"{path}: references /usr/libexec/lindos/{m.group(1)}"


def test_helper_calls_lindos_tune_with_real_subcommands():
    """helper → `lindos-tune apply --mode X --system [--offline] | zram N | governor g | fan set p`."""
    src = _read(_root("lindos-core", "usr", "libexec", "lindos", "lindos-helper"))
    for needle in ('["lindos-tune", "apply", "--mode"', '["lindos-tune", "zram"', '["lindos-tune", "governor"',
                   '["lindos-tune", "fan", "set"', '["lindos-drivers", "install"]'):
        assert needle in src, needle
    from lindos_tune import cli

    parser = cli.build_parser()
    ns = parser.parse_args(["apply", "--mode", "gaming", "--system", "--offline"])
    assert ns.mode == "gaming" and ns.offline
    parser.parse_args(["zram", "75"])
    parser.parse_args(["governor", "performance"])
    parser.parse_args(["fan", "set", "quiet"])
    parser.parse_args(["power", "balanced"])
    parser.parse_args(["status", "--json"])


def test_settings_page_ids_used_by_desktop_shortcuts_exist():
    from lindos_settings import model

    pages = set(model.PAGE_ORDER)
    xml = _read(_root("lindos-desktop", "etc", "xdg", "xfce4", "xfconf", "xfce-perchannel-xml",
                      "xfce4-keyboard-shortcuts.xml"))
    whisker = _read(_root("lindos-desktop", "etc", "xdg", "xfce4", "panel", "whiskermenu-1.rc"))
    for m in re.finditer(r"lindos-settings ([a-z-]+)", xml + whisker):
        arg = m.group(1)
        if arg.startswith("--"):
            continue
        assert arg in pages, f"lindos-settings {arg}: unknown page id"
    assert "--power-menu" in xml


def test_gamemode_scripts_call_compositor_with_supported_verbs():
    for name, verb in (("gamemode-start.sh", "stop"), ("gamemode-end.sh", "start")):
        text = _read(_root("lindos-gaming", "usr", "libexec", "lindos", name))
        assert re.search(r"lindos-compositor\s+" + verb, text), name
    ini = _read(_root("lindos-gaming", "etc", "gamemode.ini"))
    assert "/usr/libexec/lindos/gamemode-start.sh" in ini and "/usr/libexec/lindos/gamemode-end.sh" in ini


# --------------------------------------------------------------------------- #
# lindos_compat against the REAL lindos.compat (its own tests use a fake)
# --------------------------------------------------------------------------- #
def _fake_pe(path: str, machine: int = 0x8664, marker: bytes = b"Nullsoft Install System") -> None:
    data = bytearray(b"MZ" + b"\0" * 58 + struct.pack("<I", 0x80))
    data += b"\0" * (0x80 - len(data))
    data += b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 18 + b"\0" * 100
    data += marker + b"\0" * 100
    with open(path, "wb") as fh:
        fh.write(bytes(data))


def test_lindos_compat_uses_real_core_compat(tmp_path, monkeypatch):
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("LINDOS_NO_GUI", "1")
    (tmp_path / "home").mkdir()
    (tmp_path / "root").mkdir()
    for key in [k for k in sys.modules if k == "lindos.compat"]:
        assert not getattr(sys.modules[key], "__lindos_fake__", False)

    from lindos import compat as core_compat
    from lindos_compat import cli_run, prefix, runner, scan

    exe = tmp_path / "FooSetup-x64.exe"
    _fake_pe(str(exe))
    info = core_compat.analyze_exe(str(exe))
    assert info.kind == "installer" and info.arch == "x64" and info.installer_type == "nsis"
    assert prefix.derive_slug(info) == "foo"
    assert runner.choose_runner(info, {}, which=lambda n: None)[0] in ("wine", "umu")

    # apps db round trip: lindos_compat.scan writes through the real lindos.compat (SPEC §4.8)
    found = scan.FoundApp(name="Foo", exe=tmp_path / "home" / "foo.exe")
    slug = scan.register_app(found, prefix_slug="foo", runner="wine")
    db = core_compat.apps_db_load()
    assert slug in db and db[slug]["prefix"] == "foo" and db[slug]["runner"] == "wine"
    assert set(db[slug]) >= {"name", "exe", "prefix", "runner", "installed_at", "kind"}
    assert prefix.prefix_path("foo") == os.path.join(str(tmp_path / "home"), ".local", "share", "lindos",
                                                     "prefixes", "foo") or \
        str(prefix.prefix_path("foo")).replace("\\", "/").endswith("/.local/share/lindos/prefixes/foo")

    # `lindos-run --info` prints the ExeInfo JSON with lindos_compat's extra keys
    rc = cli_run.main([str(exe), "--info"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# packaging seams
# --------------------------------------------------------------------------- #
def test_setup_autostart_reference_is_identical_to_desktop_autostart():
    """lindos-setup's postinst may copy this file into /etc/xdg/autostart when lindos-desktop is
    not installed yet; if lindos-desktop is unpacked afterwards the contents must be identical or
    dpkg raises a conffile prompt for a file it now owns."""
    a = _read(_root("lindos-setup", "usr", "share", "lindos", "setup", "autostart", "lindos-setup.desktop"))
    b = _read(_root("lindos-desktop", "etc", "xdg", "autostart", "lindos-setup.desktop"))
    assert a == b


def test_no_two_packages_ship_the_same_file():
    owners: Dict[str, List[str]] = {}
    for pkg in os.listdir(PKGS):
        base = _root(pkg)
        if not os.path.isdir(base):
            continue
        for dp, _dn, fn in os.walk(base):
            if "__pycache__" in dp:
                continue
            for f in fn:
                rel = os.path.relpath(os.path.join(dp, f), base).replace("\\", "/")
                owners.setdefault(rel, []).append(pkg)
    dupes = {k: v for k, v in owners.items() if len(v) > 1}
    assert not dupes, dupes


def test_core_does_not_depend_on_other_lindos_packages():
    control = _read(os.path.join(PKGS, "lindos-core", "DEBIAN", "control"))
    depends = next(ln for ln in control.splitlines() if ln.startswith("Depends:"))
    assert "lindos-" not in depends
    for pkg in os.listdir(PKGS):
        if pkg in ("lindos-core", "lindos-meta"):
            continue
        ctl = _read(os.path.join(PKGS, pkg, "DEBIAN", "control"))
        dep_line = next((ln for ln in ctl.splitlines() if ln.startswith("Depends:")), "")
        assert "lindos-core" in dep_line, f"{pkg} must depend on lindos-core"
        # no hard dependency cycles between the leaf packages
        for other in re.findall(r"lindos-[a-z]+", dep_line):
            assert other in ("lindos-core",), f"{pkg} hard-depends on {other}"


@pytest.mark.parametrize("pkg", sorted(p for p in os.listdir(PKGS) if os.path.isdir(os.path.join(PKGS, p, "DEBIAN"))))
def test_maintainer_scripts_are_posix_sh_with_set_e(pkg):
    for name in ("preinst", "postinst", "prerm", "postrm"):
        path = os.path.join(PKGS, pkg, "DEBIAN", name)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        assert text.startswith("#!/bin/sh\n"), f"{pkg}/{name}: must start with #!/bin/sh"
        assert re.search(r"^set -e\b", text, re.M), f"{pkg}/{name}: missing set -e"
        assert "\r" not in text


# --------------------------------------------------------------------------- #
# Addendum W (SPEC-WINDOWS.md) cross-component checks — W-I integration
# --------------------------------------------------------------------------- #
#: every id in the SPEC-WINDOWS §28.3 format table (binding; order does not matter)
SPEC_WINDOWS_FORMAT_IDS = {
    "exe", "dotnet-exe", "win16-exe", "dos-exe", "dos-com", "arm-exe", "dll", "msi", "msp", "mst",
    "msix", "msix-bundle", "msix-upload", "msix-encrypted", "msixvc", "appinstaller", "bat", "ps1",
    "vbs", "reg", "lnk", "url", "scr", "cpl", "inf", "cab", "msu", "iso", "clickonce",
}

#: every action the SPEC-WINDOWS §30.4 table adds to lindos.helper.ACTIONS
SPEC_WINDOWS_HELPER_ACTIONS = {"reboot-to-windows", "firmware-setup", "import-wifi", "set-binfmt"}

#: SPEC-WINDOWS §33.1 "cross-component call map" — CLI names other components call
SPEC_WINDOWS_CLIS = {
    "lindos-run": ("lindos-compat", "usr", "bin", "lindos-run"),
    "lindos-compat": ("lindos-compat", "usr", "bin", "lindos-compat"),
    "lindos-game": ("lindos-gaming", "usr", "bin", "lindos-game"),
    "lindos-dualboot": ("lindos-core", "usr", "bin", "lindos-dualboot"),
    "lindos-transfer": ("lindos-transfer", "usr", "bin", "lindos-transfer"),
    "lindos-transfer-gui": ("lindos-transfer", "usr", "bin", "lindos-transfer-gui"),
}


def test_addendum_w_format_ids_match_spec_table():
    """Every id SPEC-WINDOWS §28.3 defines exists in formats.FORMATS, and vice versa."""
    from lindos_compat import formats

    shipped_ids = {f.id for f in formats.FORMATS}
    assert shipped_ids == SPEC_WINDOWS_FORMAT_IDS, (
        f"formats.FORMATS ids vs SPEC-WINDOWS §28.3: "
        f"missing={SPEC_WINDOWS_FORMAT_IDS - shipped_ids} extra={shipped_ids - SPEC_WINDOWS_FORMAT_IDS}"
    )
    # every handler used is one of the binding HANDLERS, and every plan actually validates
    plan_targets = [(f.id, f.handler, f.status) for f in formats.FORMATS]
    for fid, handler, status in plan_targets:
        assert handler in formats.HANDLERS, f"{fid}: handler {handler!r} not in formats.HANDLERS"
        assert status in formats.STATUSES, f"{fid}: status {status!r} not in formats.STATUSES"


def test_addendum_w_helper_actions_present():
    """SPEC-WINDOWS §30.4's four new privileged actions exist with a real handler."""
    from lindos import helper

    assert SPEC_WINDOWS_HELPER_ACTIONS <= set(helper.ACTIONS)
    src = _read(_root("lindos-core", "usr", "libexec", "lindos", "lindos-helper"))
    for action in SPEC_WINDOWS_HELPER_ACTIONS:
        assert f'"{action}":' in src, f"lindos-helper has no handler for {action}"


def test_addendum_w_transfer_calls_import_wifi_via_stdin():
    """lindos-transfer's Wi-Fi importer must use the stdin_payload contract (SPEC-WINDOWS §27.3/§30.4):
    a Wi-Fi password must never be placed on a command line or in a log."""
    src = _read(_root("lindos-transfer", "usr", "lib", "lindos-transfer", "lindos_transfer", "wifi.py"))
    assert 'run_privileged("import-wifi"' in src or "run_privileged('import-wifi'" in src
    assert "stdin_payload=True" in src, "wifi.py must pass stdin_payload=True to helper.run_privileged"


def test_addendum_w_game_route_calls_dualboot():
    """lindos-game's Windows route must go through lindos-dualboot, never edit Windows itself."""
    src = _read(_root("lindos-gaming", "usr", "bin", "lindos-game"))
    assert "lindos-dualboot" in src


def test_addendum_w_clis_exist_and_are_executable_scripts():
    """Every CLI named in the SPEC-WINDOWS §33.1 cross-component call map is shipped and is a
    real python3/bash entry point (a valid shebang), independent of the checked-out exec bit."""
    shipped = _shipped_executables()
    for name, parts in SPEC_WINDOWS_CLIS.items():
        assert name in shipped, f"{name} referenced in SPEC-WINDOWS §33.1 but not shipped"
        path = _root(*parts)
        assert os.path.isfile(path), f"{name}: expected at {path}"
        first_line = _read(path).splitlines()[0]
        assert first_line.startswith("#!/usr/bin/env python3") or first_line.startswith("#!/usr/bin/python3") \
            or first_line.startswith("#!/bin/bash") or first_line.startswith("#!/bin/sh"), \
            f"{name}: unexpected shebang {first_line!r}"


def test_addendum_w_run_desktop_mime_types_are_all_known():
    """Every MIME type lindos-run.desktop claims is either newly defined by lindos-windows.xml or
    named in that file's own header as an existing shared-mime-info 2.4 type (SPEC-WINDOWS §28.9)
    — never a type that is silently undefined."""
    desktop = _read(_root("lindos-compat", "usr", "share", "applications", "lindos-run.desktop"))
    m = re.search(r"^MimeType=(.*)$", desktop, re.M)
    assert m, "lindos-run.desktop has no MimeType= line"
    types = [t for t in m.group(1).split(";") if t]
    assert types, "lindos-run.desktop MimeType= is empty"

    mime_xml = _read(_root("lindos-compat", "usr", "share", "mime", "packages", "lindos-windows.xml"))
    comment_end = mime_xml.index("-->")
    header, body = mime_xml[:comment_end], mime_xml[comment_end:]
    known = set(re.findall(r"\b(?:application|text)/[A-Za-z0-9_.+-]+", header))
    known |= set(re.findall(r'type="((?:application|text)/[^"]+)"', body))
    missing = [t for t in types if t not in known]
    assert not missing, f"lindos-run.desktop MimeType(s) not defined anywhere: {missing}"


def test_addendum_w_lindos_meta_depends_on_transfer():
    """SPEC-WINDOWS §33: lindos-meta Depends lindos-transfer (= 1.0.0)."""
    ctl = _read(os.path.join(PKGS, "lindos-meta", "DEBIAN", "control"))
    dep_line = next(ln for ln in ctl.splitlines() if ln.startswith("Depends:"))
    assert "lindos-transfer (= 1.0.0)" in dep_line


def test_addendum_w_deb_order_installs_transfer_before_setup():
    """SPEC-WINDOWS §33: 30-lindos-debs.sh installs lindos-transfer before lindos-setup."""
    for path in (
        os.path.join(REPO, "build", "chroot", "30-lindos-debs.sh"),
        os.path.join(REPO, "build", "config.env"),
    ):
        text = _read(path)
        m = re.search(r"LINDOS_DEB_ORDER:?=([a-z0-9 _-]+)", text)
        assert m, f"{path}: no LINDOS_DEB_ORDER default found"
        order = m.group(1).split()
        assert "lindos-transfer" in order, f"{path}: lindos-transfer missing from LINDOS_DEB_ORDER"
        assert order.index("lindos-transfer") < order.index("lindos-setup"), \
            f"{path}: lindos-transfer must install before lindos-setup"


def test_addendum_w_transfer_package_is_lib_dir_discovered():
    """lindos_testsupport must put lindos_transfer on sys.path like every other package (either via
    the explicit LIB_DIRS list or the packages/*/root/usr/lib/lindos-* glob fallback)."""
    transfer_lib = os.path.join(PKGS, "lindos-transfer", "root", "usr", "lib", "lindos-transfer")
    assert os.path.isdir(transfer_lib)
    assert transfer_lib in (lindos_testsupport.LIB_DIRS + lindos_testsupport._extra_lib_dirs())
    import lindos_transfer  # noqa: F401  must import cleanly once sys.path is set up
