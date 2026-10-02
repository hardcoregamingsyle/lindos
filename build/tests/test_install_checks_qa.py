"""Hermetic tests for build/qa/install_checks.py - the read-only assertions on an INSTALLED Lindos disk.

The install test (build/qa/install_test.py) can only run on Linux with QEMU and real network; these tests
build fake installed trees in tmp_path instead and prove that every rule of the installer flow is (a) accepted
when the tree is right and (b) reported, by name, when it is wrong.  Symlinks are described through Tree's
overlay so the tests run unchanged on hosts that cannot create them (this Windows dev box included).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
QA_DIR = HERE.parent / "qa"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import install_checks as ic  # noqa: E402

GOOD_LINKS = {"etc/systemd/system/default.target": "/lib/systemd/system/oem-config.target"}

STATE_OK = {
    "schema": 1, "updated": "2026-09-29T10:25:00Z", "online": True,
    "steps": {
        "updates": {"status": "done", "detail": "212 packages upgraded", "time": "2026-09-29T10:10:00Z"},
        "drivers": {"status": "done", "detail": "free drivers and firmware installed (a proprietary GPU driver needs your consent: Settings)",
                    "time": "2026-09-29T10:12:00Z"},
        "browser": {"status": "done", "detail": "google-chrome-stable installed", "time": "2026-09-29T10:05:00Z"},
        "compat": {"status": "done", "detail": "wine winetricks umu", "time": "2026-09-29T10:14:00Z"},
        "gaming": {"status": "done", "detail": "steam lutris", "time": "2026-09-29T10:15:00Z"},
        "mode_extras": {"status": "done", "detail": "4 packages installed", "time": "2026-09-29T10:16:00Z"},
        "flatpaks": {"status": "pending", "detail": "flatpak could not install in the chroot", "time": "2026-09-29T10:18:00Z"},
    },
}

INSTALLER_LOG = """\
2026-09-29 10:00:00 lindos-installer: start (version 1.0.0, target /target, budget 2400s)
2026-09-29 10:05:00 lindos-installer: step browser: done - google-chrome-stable installed
2026-09-29 10:10:00 lindos-installer: step updates: done - upgraded 212 packages
2026-09-29 10:18:00 lindos-installer: step flatpaks: pending - flatpak could not install in the chroot
2026-09-29 10:20:00 lindos-installer: finished in 1200s
2026-09-29 10:25:00 lindos-installer: finalize: start
2026-09-29 10:25:01 lindos-installer: finalize: oem-config is armed - the first start asks for the account
2026-09-29 10:25:02 lindos-installer: finalize: done
"""


def dpkg_stanza(name: str, status: str = "install ok installed", version: str = "1.0", arch: str = "all") -> str:
    return "Package: %s\nStatus: %s\nPriority: optional\nArchitecture: %s\nVersion: %s\nDescription: x\n\n" % (
        name, status, arch, version)


# a small extras.json with one entry of every kind the disk checks know (the real one is checked in the tests below)
EXTRAS = {
    "schema": 1,
    "apt": ["gimp", "libreoffice-writer", "libvulkan1", "steam-devices"],
    "compat": ["wine", "winetricks", "umu"],
    "gaming": ["steam", "lutris"],
    "flatpaks": ["com.usebottles.bottles", "org.vinegarhq.Sober"],
    "drivers": {"firmware": ["linux-firmware", "intel-microcode", "firmware-sof-signed"]},
}

GOOD_STATUS = "".join([
    dpkg_stanza("oem-config", version="24.04.3+mint18"),
    dpkg_stanza("oem-config-gtk", version="24.04.3+mint18"),
    dpkg_stanza("google-chrome-stable", version="130.0.1", arch="amd64"),
    dpkg_stanza("coreutils", arch="amd64"),
    dpkg_stanza("libc6", arch="amd64"),
    dpkg_stanza("libc6", arch="i386"),
    # what the 'done' steps of STATE_OK install
    dpkg_stanza("gimp", arch="amd64"), dpkg_stanza("libreoffice-writer", arch="amd64"),
    dpkg_stanza("libvulkan1", arch="amd64"), dpkg_stanza("steam-devices"),
    dpkg_stanza("winehq-staging", arch="amd64"), dpkg_stanza("winetricks"),
    dpkg_stanza("steam-launcher"), dpkg_stanza("lutris"),
    dpkg_stanza("linux-firmware"), dpkg_stanza("intel-microcode", arch="amd64"), dpkg_stanza("firmware-sof-signed"),
])

DEBCONF_CONFIG = """\
Name: ubiquity/success_command
Template: ubiquity/success_command
Value: /usr/libexec/lindos/installer/finalize.sh
Owners: ubiquity
Flags: seen

Name: user-setup/allow-password-empty
Template: user-setup/allow-password-empty
Value: false
Owners: d-i
Flags: seen

Name: ubiquity/custom_title_text
Template: ubiquity/custom_title_text
Value: Lindos Setup
Owners: ubiquity
Flags: seen

Name: oem-config/late_command
Template: oem-config/late_command
Value: rm -rf /etc/systemd/system/oem-config.service.d
Owners: oem-config
Flags: seen
"""

DPKG_LOG = """\
2026-09-29 10:02:00 install oem-config:all <none> 24.04.3+mint18
2026-09-29 10:10:01 upgrade libc6:amd64 2.39-0ubuntu8.3 2.39-0ubuntu8.4
2026-09-29 10:10:02 status installed libc6:amd64 2.39-0ubuntu8.4
"""

GRUB_MBR = b"\xeb\x63\x90" + b"\0" * 0x1e + b"GRUB \0Geom\0Read\0 Error\r\n" + b"\0" * 400
GRUB_MBR = GRUB_MBR[:446].ljust(446, b"\0") + b"\0" * 64 + b"\x55\xaa"

GRUB_CFG = """\
set default=0
menuentry 'Lindos' --class lindos {
    linux /boot/vmlinuz-6.14.0-lindos root=UUID=1234-5678 ro quiet splash
    initrd /boot/initrd.img-6.14.0-lindos
}
"""


def _w(root: Path, rel: str, text: str = "", *, binary: Optional[bytes] = None) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary is not None:
        p.write_bytes(binary)
    else:
        p.write_text(text, encoding="utf-8", newline="\n")


def make_good_tree(root: Path) -> None:
    """A correctly installed system, as the installer flow should leave it (online run, Chrome installed)."""
    _w(root, ic.INSTALL_STATE, json.dumps(STATE_OK))
    _w(root, ic.INSTALLER_LOG, INSTALLER_LOG)
    _w(root, "var/lib/dpkg/status", GOOD_STATUS)
    _w(root, "opt/google/chrome/chrome", "binary")
    _w(root, ic.BROWSER_MARKER, "")
    _w(root, ic.DRIVER_MARKER, "")
    _w(root, "lib/systemd/system/oem-config.service", "[Unit]\n")
    _w(root, "lib/systemd/system/oem-config.target", "[Unit]\n")
    _w(root, "usr/sbin/oem-config-firstboot", "#!/bin/sh\n")
    _w(root, "etc/passwd", "root:x:0:0::/root:/bin/bash\noem:x:29999:29999:OEM:/home/oem:/bin/bash\n")
    _w(root, "etc/shadow", "root:*:19000::::::\noem:!:19000::::::\n")
    _w(root, "etc/lightdm/lightdm.conf", "[Seat:*]\nsession-wrapper=/etc/X11/Xsession\n")
    _w(root, "boot/vmlinuz-6.14.0-lindos", "k")
    _w(root, "boot/initrd.img-6.14.0-lindos", "i")
    _w(root, "boot/grub/grub.cfg", GRUB_CFG)
    _w(root, "etc/os-release", 'PRETTY_NAME="Lindos 1.0 (Aurora)"\nID=linuxmint\n')
    _w(root, "etc/apt/sources.list", "# deb cdrom:[Lindos 1.0]/ noble main\n")
    _w(root, ic.DEBCONF_CONFIG, DEBCONF_CONFIG)
    _w(root, ic.DPKG_ARCH, "i386\n")
    _w(root, ic.DPKG_LOG, DPKG_LOG)
    _w(root, "usr/local/bin/umu-run", "#!/usr/bin/env python3\n")
    _w(root, ic.WIZARD_SKIN, "/* skin */\n")
    _w(root, ic.WIZARD_DROPIN, "[Service]\nEnvironment=GTK_THEME=Lindos-Setup\n")
    for d in ("proc", "sys", "run", "dev", "cdrom"):
        (root / d).mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def tree(tmp_path: Path) -> ic.Tree:
    make_good_tree(tmp_path)
    return ic.Tree(tmp_path, GOOD_LINKS)


def run(tree: ic.Tree, **kw) -> List[ic.Finding]:
    kw.setdefault("expect_online", True)
    kw.setdefault("mbr", GRUB_MBR)
    kw.setdefault("extras", EXTRAS)
    kw.setdefault("expect_i386", True)
    return ic.run_all_checks(tree, **kw)


def fails(findings: List[ic.Finding]) -> Dict[str, str]:
    return {f.name: f.detail for f in findings if f.level == ic.FAIL}


def levels(findings: List[ic.Finding], name: str) -> List[str]:
    return [f.level for f in findings if f.name == name]


# --------------------------------------------------------------------------------------------- baseline
def test_a_correctly_installed_online_system_passes_every_check(tree):
    findings = run(tree)
    assert fails(findings) == {}
    # what a maintainer reads: every step's status is recorded as info/warn
    names = {f.name for f in findings}
    assert {"step-" + s for s in ic.STEPS} <= names
    assert ic.warnings(findings)[0].name == "step-flatpaks"     # a 'pending' step on an online run is a warning


def test_step_ids_and_statuses_match_the_real_lindos_installstate_module():
    """The contract is binding: the assertions must not drift from lindos.installstate."""
    installstate = pytest.importorskip("lindos.installstate")
    assert ic.STEPS == tuple(installstate.STEPS)
    assert ic.STATUSES == tuple(installstate.STATUSES)
    assert ic.STATE_SCHEMA == installstate.SCHEMA
    assert "/" + ic.INSTALL_STATE == installstate.paths.INSTALL_STATE


def test_paths_match_what_the_installer_scripts_write():
    lib = (QA_DIR.parent.parent / "packages" / "lindos-installer" / "root" / "usr" / "libexec" / "lindos" / "installer" / "lib.sh")
    text = lib.read_text(encoding="utf-8")
    fin = lib.with_name("finalize.sh").read_text(encoding="utf-8")
    assert "/var/log/lindos/installer.log" in text and ic.INSTALLER_LOG == "var/log/lindos/installer.log"
    assert "oem-config-not-armed" in fin and ic.NOT_ARMED_MARKER.endswith("oem-config-not-armed")
    for rel in ("var/lib/lindos/installer-holds", "var/lib/lindos/installer-apt.conf", "etc/apt/preferences.d/00lindos-installer.pref"):
        assert rel in ic.HOOK_LEFTOVERS and "/" + rel in text
    assert "usr/lib/ubiquity/target-config/50lindos-install" in ic.HOOK_LEFTOVERS
    assert "50lindos-install" in fin


# --------------------------------------------------------------------------------------------- install-state
def _set_state(tmp_path: Path, mutate) -> None:
    data = json.loads(json.dumps(STATE_OK))
    mutate(data)
    _w(tmp_path, ic.INSTALL_STATE, json.dumps(data))


def test_missing_state_file_fails(tmp_path, tree):
    (tmp_path / ic.INSTALL_STATE).unlink()
    assert "state-file" in fails(run(tree))


def test_corrupt_state_file_fails(tmp_path, tree):
    _w(tmp_path, ic.INSTALL_STATE, "{not json")
    assert "not valid JSON" in fails(run(tree))["state-file"]


def test_wrong_schema_fails(tmp_path, tree):
    _set_state(tmp_path, lambda d: d.update(schema=2))
    assert "schema" in fails(run(tree))["state-file"]


def test_a_missing_step_is_a_failure_because_finalize_marks_unrecorded_steps(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"].pop("gaming"))
    assert "gaming" in fails(run(tree))["state-steps"]


def test_an_unknown_step_or_status_fails(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"].update(bogus={"status": "done"}))
    assert "bogus" in fails(run(tree))["state-steps"]
    _set_state(tmp_path, lambda d: d["steps"]["updates"].update(status="maybe"))
    assert "step-updates" in fails(run(tree))


def test_failed_steps_are_warnings_not_failures(tmp_path, tree):
    """Best-effort steps (Flatpak in a chroot is unproven) must not fail the install test."""
    _set_state(tmp_path, lambda d: d["steps"]["flatpaks"].update(status="failed", detail="bwrap"))
    findings = run(tree)
    assert fails(findings) == {}
    assert levels(findings, "step-flatpaks") == [ic.WARN]


def test_online_run_requires_chrome_to_be_done(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"]["browser"].update(status="pending"))
    got = fails(run(tree))
    assert "state-browser" in got
    assert "chrome-vs-state" in got            # ... and Chrome IS installed although the step says pending


def test_online_run_requires_the_hook_to_have_seen_the_network(tmp_path, tree):
    _set_state(tmp_path, lambda d: d.update(online=False))
    got = fails(run(tree))
    assert "state-online" in got and "state-consistent" in got     # online=false but browser/updates 'done'


def test_offline_run_accepts_pending_network_steps(tmp_path):
    """No internet on the runner: every network step is pending, no Chrome, no browser marker - and that is fine."""
    make_good_tree(tmp_path)
    offline = json.loads(json.dumps(STATE_OK))
    offline["online"] = False
    for step in ("updates", "browser", "compat", "gaming", "mode_extras", "flatpaks"):
        offline["steps"][step] = {"status": "pending", "detail": "offline", "time": "2026-09-29T10:00:00Z"}
    _w(tmp_path, ic.INSTALL_STATE, json.dumps(offline))
    (tmp_path / ic.BROWSER_MARKER).unlink()
    (tmp_path / "opt/google/chrome/chrome").unlink()
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("google-chrome-stable", version="130.0.1", arch="amd64"), ""))
    findings = run(ic.Tree(tmp_path, GOOD_LINKS), expect_online=False)
    assert fails(findings) == {}
    assert any(f.name == "browser-marker" and f.level == ic.OK for f in findings)     # the silent retry will pick it up


def test_offline_run_that_claims_chrome_done_is_inconsistent(tmp_path, tree):
    _set_state(tmp_path, lambda d: d.update(online=False))
    assert "state-consistent" in fails(run(tree, expect_online=False))


def test_unknown_network_only_records(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"]["browser"].update(status="pending", detail="offline"))
    _w(tmp_path, ic.INSTALL_STATE, json.dumps({**STATE_OK, "steps": {**STATE_OK["steps"], "browser": {"status": "pending"}}, "online": None}))
    (tmp_path / ic.BROWSER_MARKER).unlink()
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("google-chrome-stable", version="130.0.1", arch="amd64"), ""))
    findings = run(ic.Tree(tmp_path, GOOD_LINKS), expect_online=None)
    assert fails(findings) == {}


# --------------------------------------------------------------------------------------------- installer log
def test_missing_or_empty_installer_log_fails(tmp_path, tree):
    (tmp_path / ic.INSTALLER_LOG).unlink()
    assert "installer-log" in fails(run(tree))
    _w(tmp_path, ic.INSTALLER_LOG, "  \n")
    assert "installer-log" in fails(run(tree))


def test_hook_that_never_started_fails(tmp_path, tree):
    _w(tmp_path, ic.INSTALLER_LOG, INSTALLER_LOG.replace("start (version", "begin (version"))
    assert "hook-ran" in fails(run(tree))


def test_hook_that_never_finished_is_only_a_warning(tmp_path, tree):
    _w(tmp_path, ic.INSTALLER_LOG, "\n".join(l for l in INSTALLER_LOG.splitlines() if "finished in" not in l) + "\n")
    findings = run(tree)
    assert "hook-ran" not in fails(findings) and levels(findings, "hook-ran") == [ic.WARN]


def test_finalize_that_never_ran_fails(tmp_path, tree):
    _w(tmp_path, ic.INSTALLER_LOG, "\n".join(l for l in INSTALLER_LOG.splitlines() if "finalize" not in l) + "\n")
    assert "finalize-ran" in fails(run(tree))
    _w(tmp_path, ic.INSTALLER_LOG, "\n".join(l for l in INSTALLER_LOG.splitlines() if "finalize: done" not in l) + "\n")
    assert "never logged 'done'" in fails(run(tree))["finalize-ran"]


def test_critical_installer_log_line_fails(tmp_path, tree):
    _w(tmp_path, ic.INSTALLER_LOG, INSTALLER_LOG + "2026-09-29 10:25:02 lindos-installer: finalize: CRITICAL - oem-config is not in the new system\n")
    assert "installer-critical" in fails(run(tree))


def test_log_and_state_that_disagree_warn(tmp_path, tree):
    _w(tmp_path, ic.INSTALLER_LOG, INSTALLER_LOG.replace("step updates: done", "step updates: failed"))
    assert levels(run(tree), "log-vs-state") == [ic.WARN]


def test_parse_installer_log_reads_steps_and_markers():
    got = ic.parse_installer_log(INSTALLER_LOG)
    assert got["hook_started"] and got["hook_finished"] and got["hook_seconds"] == 1200
    assert got["finalize_started"] and got["finalize_done"] and got["armed"]
    assert got["steps"] == {"browser": "done", "updates": "done", "flatpaks": "pending"}
    assert got["critical"] == []


# --------------------------------------------------------------------------------------------- oem-config
def test_default_target_must_be_oem_config(tmp_path):
    make_good_tree(tmp_path)
    for links in ({}, {"etc/systemd/system/default.target": "/lib/systemd/system/graphical.target"}):
        got = fails(run(ic.Tree(tmp_path, links)))
        assert "oem-armed" in got and "first boot" in got["oem-armed"]


def test_default_target_link_is_followed_inside_the_tree_only(tmp_path):
    """An absolute link must resolve against the mounted system, never against the machine running the test."""
    make_good_tree(tmp_path)
    (tmp_path / "usr" / "lib" / "systemd" / "system").mkdir(parents=True)
    (tmp_path / "lib" / "systemd" / "system" / "oem-config.target").replace(tmp_path / "usr/lib/systemd/system/oem-config.target")
    (tmp_path / "lib" / "systemd" / "system" / "oem-config.service").replace(tmp_path / "usr/lib/systemd/system/oem-config.service")
    (tmp_path / "lib" / "systemd" / "system").rmdir()
    (tmp_path / "lib" / "systemd").rmdir()
    (tmp_path / "lib").rmdir()
    links = dict(GOOD_LINKS, lib="usr/lib")                              # the usr-merge symlink of a real Mint
    assert fails(run(ic.Tree(tmp_path, links))) == {}


def test_oem_units_program_and_packages_are_required(tmp_path, tree):
    (tmp_path / "lib/systemd/system/oem-config.service").unlink()
    assert "oem-unit-service" in fails(run(tree))
    make_good_tree(tmp_path)
    (tmp_path / "usr/sbin/oem-config-firstboot").unlink()
    assert "oem-firstboot-program" in fails(run(tree))
    make_good_tree(tmp_path)
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("oem-config-gtk", version="24.04.3+mint18"), ""))
    assert "pkg-oem-config-gtk" in fails(run(tree))


def test_the_not_armed_marker_fails_with_its_reason(tmp_path, tree):
    _w(tmp_path, ic.NOT_ARMED_MARKER, "oem-config is not in the new system and no bundled copy could be installed\n")
    assert "bundled copy" in fails(run(tree))["oem-not-armed"]


def test_lightdm_autologin_of_the_temporary_account_fails(tmp_path, tree):
    _w(tmp_path, "etc/lightdm/lightdm.conf", "[Seat:*]\nautologin-guest=false\nautologin-user=oem\nautologin-user-timeout=0\n")
    assert "autologin-user=oem" in fails(run(tree))["lightdm-autologin"]
    make_good_tree(tmp_path)
    _w(tmp_path, "etc/lightdm/lightdm.conf.d/50-x.conf", "[Seat:*]\n  autologin-user = oem \n")
    assert "lightdm.conf.d/50-x.conf" in fails(run(tree))["lightdm-autologin"]
    make_good_tree(tmp_path)
    _w(tmp_path, "etc/lightdm/lightdm.conf.d/50-x.conf", "[Seat:*]\nautologin-user=alice\n")
    assert "lightdm-autologin" not in fails(run(tree))


def test_temporary_account_must_exist_and_be_locked(tmp_path, tree):
    _w(tmp_path, "etc/passwd", "root:x:0:0::/root:/bin/bash\n")
    assert "OEM mode" in fails(run(tree))["oem-account"]
    make_good_tree(tmp_path)
    _w(tmp_path, "etc/shadow", "oem:$6$abc$def:19000::::::\n")
    assert "NOT locked" in fails(run(tree))["oem-locked"]
    _w(tmp_path, "etc/shadow", "oem::19000::::::\n")
    assert "empty" in fails(run(tree))["oem-locked"]
    _w(tmp_path, "etc/shadow", "oem:!$6$abc$def:19000::::::\n")               # passwd -l on a real hash
    assert "oem-locked" not in fails(run(tree))


def test_unreadable_shadow_only_warns(tmp_path, tree):
    (tmp_path / "etc/shadow").unlink()
    assert levels(run(tree), "oem-locked") == [ic.WARN]


# --------------------------------------------------------------------------------------------- leftovers / dpkg
@pytest.mark.parametrize("rel", list(ic.HOOK_LEFTOVERS))
def test_every_installer_leftover_fails(tmp_path, tree, rel):
    _w(tmp_path, rel, "x")
    assert rel in fails(run(tree))["leftovers"]


def test_held_packages_fail(tmp_path, tree):
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS + dpkg_stanza("ubiquity", "hold ok installed", arch="amd64"))
    assert "ubiquity" in fails(run(tree))["dpkg-holds"]


@pytest.mark.parametrize("status", ["install ok half-configured", "install ok unpacked", "install ok half-installed",
                                    "install ok triggers-awaited", "install reinstreq installed"])
def test_a_dpkg_that_is_not_clean_fails(tmp_path, tree, status):
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS + dpkg_stanza("broken-pkg", status, arch="amd64"))
    assert "broken-pkg" in fails(run(tree))["dpkg-audit"]


def test_removed_and_config_files_packages_are_clean(tmp_path, tree):
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS + dpkg_stanza("gone", "deinstall ok config-files") + dpkg_stanza("purged", "purge ok not-installed"))
    assert "dpkg-audit" not in fails(run(tree))


def test_parse_dpkg_status_keys_foreign_architectures_separately():
    pkgs = ic.parse_dpkg_status(GOOD_STATUS)
    assert set(pkgs) >= {"oem-config", "libc6", "libc6:i386"}
    assert pkgs["oem-config"].version == "24.04.3+mint18" and pkgs["oem-config"].state == "installed"
    assert ic.audit_packages(pkgs) == ([], [])


def test_what_ubiquitys_own_user_setup_leaves_in_run_is_not_a_warning(tmp_path, tree):
    """First real install: 'WARN /run is not empty (adduser, mount)'.  user-setup-apply runs mount and adduser in a bare chroot
    (its own bind of /run comes later): Ubiquity's doing, not the hook's; harmless on a tmpfs."""
    (tmp_path / "run" / "adduser").mkdir()
    (tmp_path / "run" / "mount").mkdir()
    findings = run(tree)
    assert fails(findings) == {} and levels(findings, "mountpoint-run") == [ic.INFO]
    detail = next(f.detail for f in findings if f.name == "mountpoint-run")
    assert "adduser" in detail and "mount" in detail and "hook" in detail


def test_anything_else_in_run_still_warns_and_is_named(tmp_path, tree):
    for name in ("adduser", "mount", "stray-lock"):
        (tmp_path / "run" / name).mkdir()
    findings = run(tree)
    assert levels(findings, "mountpoint-run") == [ic.WARN]
    detail = next(f.detail for f in findings if f.name == "mountpoint-run")
    assert "stray-lock" in detail and "1 entries" in detail and "adduser" not in detail


def test_non_empty_mount_points_warn(tmp_path, tree):
    _w(tmp_path, "run/reboot-required", "")
    _w(tmp_path, "proc/stray", "")
    findings = run(tree)
    assert fails(findings) == {} and levels(findings, "mountpoint-run") == [ic.WARN] and levels(findings, "mountpoint-proc") == [ic.WARN]


def test_active_cdrom_apt_source_warns(tmp_path, tree):
    _w(tmp_path, "etc/apt/sources.list", "deb [trusted=yes] cdrom:[Lindos 1.0]/ noble main\n")
    findings = run(tree)
    assert "apt-cdrom" not in fails(findings) and levels(findings, "apt-cdrom") == [ic.WARN]


# --------------------------------------------------------------------------------------------- browser / drivers
def test_browser_marker_without_chrome_fails_unless_skipped(tmp_path, tree):
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("google-chrome-stable", version="130.0.1", arch="amd64"), ""))
    got = fails(run(tree, expect_online=None))
    assert "browser-marker" in got and "chrome-vs-state" in got        # marker present, step 'done', no package
    _set_state(tmp_path, lambda d: d["steps"]["browser"].update(status="skipped", detail="lindos.install=off"))
    got = fails(run(tree, expect_online=None))
    assert "browser-marker" not in got and "chrome-vs-state" not in got


def test_chrome_without_its_binary_fails(tmp_path, tree):
    (tmp_path / "opt/google/chrome/chrome").unlink()
    assert "chrome-binary" in fails(run(tree))


def test_driver_marker_must_agree_with_the_drivers_step(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"]["drivers"].update(status="pending"))
    assert "driver-marker" in fails(run(tree))
    _set_state(tmp_path, lambda d: d["steps"]["drivers"].update(status="done"))
    (tmp_path / ic.DRIVER_MARKER).unlink()
    findings = run(tree)
    assert "driver-marker" not in fails(findings) and levels(findings, "driver-marker") == [ic.WARN]


# --------------------------------------------------------------------------------------------- boot loader
def test_bios_bootloader_needs_grub_in_the_mbr(tmp_path, tree):
    assert "bootloader" not in fails(run(tree))
    assert "first sector" in fails(run(tree, mbr=None))["bootloader"]
    assert "boot signature" in fails(run(tree, mbr=b"\0" * 512))["bootloader"]
    assert "not GRUB" in fails(run(tree, mbr=b"\0" * 510 + b"\x55\xaa"))["bootloader"]


def test_uefi_bootloader_needs_an_efi_binary_on_the_esp(tmp_path, tree):
    esp = tmp_path.parent / (tmp_path.name + "-esp")
    (esp / "EFI" / "ubuntu").mkdir(parents=True)
    assert "bootloader" in fails(run(tree, firmware="uefi", esp=ic.Tree(esp)))
    (esp / "EFI" / "ubuntu" / "grubx64.efi").write_bytes(b"MZ")
    assert "bootloader" not in fails(run(tree, firmware="uefi", esp=ic.Tree(esp)))
    assert "bootloader" in fails(run(tree, firmware="uefi", esp=None))


def test_grub_cfg_and_kernel_are_required(tmp_path, tree):
    (tmp_path / "boot/grub/grub.cfg").unlink()
    assert "grub-cfg" in fails(run(tree))
    _w(tmp_path, "boot/grub/grub.cfg", "set default=0\n")
    assert "grub-cfg" in fails(run(tree))
    make_good_tree(tmp_path)
    (tmp_path / "boot/initrd.img-6.14.0-lindos").unlink()
    assert "kernel" in fails(run(tree))


def test_list_and_pick_kernel(tmp_path, tree):
    for v in ("6.8.0-31-generic", "6.14.0-lindos", "6.10.0-1-generic"):
        _w(tmp_path, "boot/vmlinuz-" + v, "k")
        _w(tmp_path, "boot/initrd.img-" + v, "i")
    _w(tmp_path, "boot/vmlinuz-6.99.0-nointrd", "k")                    # no initrd: not bootable
    _w(tmp_path, "boot/vmlinuz-6.8.0-31-generic.old", "k")
    kernels = ic.list_kernels(tree)
    assert [k.version for k in kernels] == ["6.8.0-31-generic", "6.10.0-1-generic", "6.14.0-lindos"]
    assert ic.pick_kernel(kernels).version == "6.14.0-lindos"
    assert ic.pick_kernel(kernels, "-lindos").version == "6.14.0-lindos"
    assert ic.pick_kernel(kernels, "-generic").version == "6.10.0-1-generic"          # newest with the suffix
    assert ic.pick_kernel(kernels, "-nothing").version == "6.14.0-lindos"             # suffix absent: newest
    assert ic.pick_kernel([]) is None


# --------------------------------------------------------------------------------------------- branding / logs
def test_branding_and_the_medium_only_installer_package(tmp_path, tree):
    _w(tmp_path, "etc/os-release", 'PRETTY_NAME="Linux Mint 22.2"\n')
    assert levels(run(tree), "branding") == [ic.WARN]
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS + dpkg_stanza("lindos-installer"))
    assert levels(run(tree), "installer-package-removed") == [ic.WARN]


def test_installer_log_signatures():
    ok_ = ic.scan_installer_logs({"syslog": "all quiet\nubiquity: done\n"})
    assert [f.name for f in ok_] == ["log-signatures"]
    bad = ic.scan_installer_logs({"var/log/installer/syslog": "umount: /target: target is busy.\nTraceback (most recent call last):\n"})
    by_level = {f.level for f in bad}
    assert by_level == {ic.FAIL, ic.WARN}
    assert any("target could not be unmounted" in f.detail for f in bad if f.level == ic.FAIL)


def test_a_target_busy_line_in_ubiquitys_syslog_fails_the_whole_run(tmp_path, tree):
    _w(tmp_path, "var/log/installer/syslog", "ubiquity: umount: /target: target is busy.\n")
    assert any("unmounted" in v for v in fails(run(tree)).values())


def test_missing_dpkg_status_means_this_is_not_an_installed_system(tmp_path, tree):
    (tmp_path / "var/lib/dpkg/status").unlink()
    assert "dpkg-status" in fails(run(tree))


def test_redact_secrets():
    assert ic.redact_secrets("pw=abcdef123456 x", ["abcdef123456"]) == "pw=<redacted> x"
    assert ic.redact_secrets("short ab", ["ab"]) == "short ab"           # too short to be a secret: never mangle text
    assert ic.redact_secrets("x", [""]) == "x"


# --------------------------------------------------------------------------------------------- Tree
def test_tree_follows_symlinks_inside_the_root_only(tmp_path):
    (tmp_path / "usr" / "lib").mkdir(parents=True)
    (tmp_path / "usr" / "lib" / "f").write_text("inside", encoding="utf-8")
    t = ic.Tree(tmp_path, {"lib": "usr/lib", "abs": "/usr/lib", "up": "usr/lib/../lib"})
    assert t.read_text("lib/f") == "inside"
    assert t.read_text("abs/f") == "inside"
    assert t.read_text("up/f") == "inside"
    assert t.readlink("lib") == "usr/lib" and t.readlink("usr/lib") is None
    assert t.lexists("lib") and t.is_dir("lib") and t.is_file("lib/f")


def test_tree_absolute_link_does_not_escape_to_the_host(tmp_path):
    t = ic.Tree(tmp_path, {"etc/hostname-link": "/etc/hostname"})
    assert t.read_text("etc/hostname-link") is None          # there is no /etc/hostname INSIDE tmp_path


def test_tree_symlink_loops_end(tmp_path):
    t = ic.Tree(tmp_path, {"a": "b", "b": "a"})
    assert t.read_text("a/x") is None and not t.is_file("a")


def test_tree_with_real_symlinks(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("x", encoding="utf-8")
    try:
        os.symlink("real", str(tmp_path / "link"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    t = ic.Tree(tmp_path)
    assert t.readlink("link") == "real" and t.read_text("link/f") == "x"


def test_tree_copy_to(tmp_path):
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "vmlinuz").write_bytes(b"kernel")
    t = ic.Tree(tmp_path, {"boot/vmlinuz-x": "vmlinuz"})
    assert t.copy_to("boot/vmlinuz-x", tmp_path / "out" / "k") and (tmp_path / "out" / "k").read_bytes() == b"kernel"
    assert not t.copy_to("boot/absent", tmp_path / "out" / "z")


def test_findings_format_and_helpers():
    fs = [ic.ok("a"), ic.info("b", "x"), ic.warn("c", "y"), ic.fail("d", "z")]
    assert [f.line() for f in fs] == ["[ok  ] a", "[info] b: x", "[WARN] c: y", "[FAIL] d: z"]
    assert [f.name for f in ic.failures(fs)] == ["d"] and [f.name for f in ic.warnings(fs)] == ["c"]
    assert ic.format_findings(fs).count("\n") == 3


def _edit_state(tmp_path: Path, mutate) -> None:
    """Like _set_state, but starting from the state file that is on disk now (several edits add up)."""
    data = json.loads((tmp_path / ic.INSTALL_STATE).read_text(encoding="utf-8"))
    mutate(data)
    _w(tmp_path, ic.INSTALL_STATE, json.dumps(data))


# --------------------------------------------------------------------------------------------- 'failed' on an online run
@pytest.mark.parametrize("step", ["updates", "drivers", "mode_extras"])
def test_a_failed_archive_step_fails_an_online_run(tmp_path, tree, step):
    """updates, drivers and the extra apps only need Ubuntu's archives: with internet a 'failed' is a real failure."""
    _set_state(tmp_path, lambda d: d["steps"][step].update(status="failed", detail="apt-get exited 100"))
    findings = run(tree)
    assert "step-" + step in fails(findings) and "must not fail" in fails(findings)["step-" + step]
    # ... but only when the runner really had internet (an unknown or offline network only records it)
    assert "step-" + step not in fails(run(tree, expect_online=None)) and levels(run(tree, expect_online=None), "step-" + step) == [ic.WARN]


@pytest.mark.parametrize("step", ["compat", "gaming", "flatpaks"])
def test_third_party_steps_stay_warnings_even_online(tmp_path, tree, step):
    _set_state(tmp_path, lambda d: d["steps"][step].update(status="failed", detail="download exit 1"))
    findings = run(tree)
    assert "step-" + step not in fails(findings) and levels(findings, "step-" + step) == [ic.WARN]


# --------------------------------------------------------------------------------------------- the lindos.seed answers
def test_a_correct_install_passes_both_seed_effects(tree):
    findings = run(tree)
    assert fails(findings) == {}
    assert levels(findings, "seed-password-empty") == [ic.OK] and levels(findings, "seed-multiarch") == [ic.OK]


def test_allow_password_empty_left_true_fails_the_run(tmp_path, tree):
    """finalize.sh's fin_reset_seed only logs a WARNING when it cannot reset the answer; the disk must prove it worked."""
    _w(tmp_path, ic.DEBCONF_CONFIG, DEBCONF_CONFIG.replace("Value: false", "Value: true"))
    got = fails(run(tree))
    assert "seed-password-empty" in got and "empty password" in got["seed-password-empty"]


def test_password_empty_that_is_absent_or_unreadable_is_not_a_failure(tmp_path, tree):
    _w(tmp_path, ic.DEBCONF_CONFIG, "Name: ubiquity/success_command\nValue: x\n")
    findings = run(tree)
    assert "seed-password-empty" not in fails(findings) and levels(findings, "seed-password-empty") == [ic.INFO]
    (tmp_path / ic.DEBCONF_CONFIG).unlink()
    findings = run(tree)
    assert "seed-password-empty" not in fails(findings) and levels(findings, "seed-password-empty") == [ic.WARN]


def test_parse_debconf_db_reads_stanzas_and_continuation_lines():
    db = ic.parse_debconf_db("Name: a/b\nTemplate: a/b\nValue: one\n two\nFlags: seen\n\nName: c/d\nValue: true\n")
    assert db["a/b"]["Value"] == "one\ntwo" and db["a/b"]["Flags"] == "seen" and db["c/d"]["Value"] == "true"
    assert ic.parse_debconf_db("") == {}


def test_i386_installed_but_removed_from_dpkg_fails(tmp_path, tree):
    """apt-setup removing the foreign architecture (the baked apt-setup/multiarch did not reach it)."""
    _w(tmp_path, ic.DPKG_ARCH, "")
    got = fails(run(tree))
    assert "seed-multiarch" in got and "libc6:i386" in got["seed-multiarch"]
    (tmp_path / ic.DPKG_ARCH).unlink()
    assert "seed-multiarch" in fails(run(tree))


def test_i386_expected_but_missing_fails_even_without_i386_packages(tmp_path, tree):
    _w(tmp_path, ic.DPKG_ARCH, "")
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("libc6", arch="i386"), ""))
    assert "ENABLE_I386" in fails(run(tree, expect_i386=True))["seed-multiarch"]
    findings = run(tree, expect_i386=False)
    assert "seed-multiarch" not in fails(findings) and levels(findings, "seed-multiarch") == [ic.OK]
    findings = run(tree, expect_i386=None)
    assert "seed-multiarch" not in fails(findings) and levels(findings, "seed-multiarch") == [ic.INFO]


# --------------------------------------------------------------------------------------------- 'done' must be installed
def _without(status_text: str, *names: str) -> str:
    for n in names:
        status_text = status_text.replace(dpkg_stanza(n, arch="amd64"), "").replace(dpkg_stanza(n), "")
    return status_text


def test_every_done_step_of_a_correct_install_is_confirmed_on_the_disk(tmp_path, tree):
    for app in EXTRAS["flatpaks"]:
        _w(tmp_path, "%s/%s/x86_64/stable/active/files/x" % (ic.FLATPAK_APPS, app), "")
    _set_state(tmp_path, lambda d: d["steps"]["flatpaks"].update(status="done", detail="2 Flatpak apps installed"))
    findings = run(tree)
    assert fails(findings) == {}
    for name in ("disk-updates", "disk-drivers", "disk-mode_extras", "disk-compat", "disk-gaming", "disk-flatpaks"):
        assert ic.OK in levels(findings, name), name


def test_mode_extras_done_with_a_missing_package_fails(tmp_path, tree):
    """'done' is derived from apt's exit codes: LibreOffice, Steam or the Flatpaks can be missing all the same."""
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "libreoffice-writer", "steam-devices"))
    got = fails(run(tree))
    assert "libreoffice-writer" in got["disk-mode_extras"] and "steam-devices" in got["disk-mode_extras"]


def test_mode_extras_excuses_only_what_the_hook_says_the_archives_lack(tmp_path, tree):
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "steam-devices"))
    _set_state(tmp_path, lambda d: d["steps"]["mode_extras"].update(detail="3 packages installed; not in the archives: steam-devices"))
    findings = run(tree)
    assert "disk-mode_extras" not in fails(findings) and levels(findings, "disk-mode_extras") == [ic.OK]
    _set_state(tmp_path, lambda d: d["steps"]["mode_extras"].update(detail="3 packages installed; not in the archives: gimp"))
    assert "steam-devices" in fails(run(tree))["disk-mode_extras"]


@pytest.mark.parametrize("step, detail, name", [
    ("mode_extras", "no extra packages defined", "disk-mode_extras"),
    ("compat", "nothing to install", "disk-compat"),
    ("gaming", "nothing to install", "disk-gaming"),
    ("flatpaks", "no Flatpak apps defined", "disk-flatpaks"),
])
def test_a_step_that_lost_its_list_fails_against_a_non_empty_extras_json(tmp_path, tree, step, detail, name):
    """li_extras_load swallows every error, so a missing/renamed extras.json makes the hook record 'done - nothing to
    install': only a comparison with the repository's extras.json can tell that from the real thing."""
    _set_state(tmp_path, lambda d: d["steps"][step].update(status="done", detail=detail))
    got = fails(run(tree))
    assert name in got and "lost its list" in got[name]
    # ... and with an empty list in extras.json it IS the truth
    assert name not in fails(run(tree, extras={"schema": 1, "apt": [], "compat": [], "gaming": [], "flatpaks": [], "drivers": {}}))


def test_mode_extras_that_found_nothing_in_the_archives_is_suspicious_online(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"]["mode_extras"].update(detail="none of the extra apps is available in the archives"))
    assert "disk-mode_extras" in fails(run(tree, expect_online=True))
    findings = run(tree, expect_online=None)
    assert "disk-mode_extras" not in fails(findings) and levels(findings, "disk-mode_extras") == [ic.WARN]


def test_compat_and_gaming_items_need_their_own_evidence(tmp_path, tree):
    (tmp_path / "usr/local/bin/umu-run").unlink()
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "steam-launcher", "winetricks"))
    got = fails(run(tree))
    assert "umu" in got["disk-compat"] and "winetricks" in got["disk-compat"] and "wine (" not in got["disk-compat"]
    assert "steam" in got["disk-gaming"] and "lutris" not in got["disk-gaming"]


def test_the_fallbacks_of_the_install_scripts_count_as_installed(tmp_path, tree):
    """Ubuntu's own wine, the deb of umu, Ubuntu's steam-installer and the Lutris Flatpak are all legitimate outcomes."""
    (tmp_path / "usr/local/bin/umu-run").unlink()
    _w(tmp_path, "usr/bin/umu-run", "#!/bin/sh\n")
    status = _without(GOOD_STATUS, "winehq-staging", "steam-launcher", "lutris") + dpkg_stanza("wine", arch="amd64") + dpkg_stanza("steam-installer", arch="amd64")
    _w(tmp_path, "var/lib/dpkg/status", status)
    _w(tmp_path, ic.FLATPAK_APPS + "/net.lutris.Lutris/x86_64/stable/active/files/x", "")
    findings = run(tree)
    assert "disk-compat" not in fails(findings) and "disk-gaming" not in fails(findings)


def test_flatpaks_done_needs_the_app_directories(tmp_path, tree):
    _set_state(tmp_path, lambda d: d["steps"]["flatpaks"].update(status="done", detail="2 Flatpak apps installed"))
    got = fails(run(tree))
    assert "com.usebottles.bottles" in got["disk-flatpaks"] and "org.vinegarhq.Sober" in got["disk-flatpaks"]
    _w(tmp_path, ic.FLATPAK_APPS + "/com.usebottles.bottles/x86_64/stable/active/files/x", "")
    assert "com.usebottles.bottles" not in fails(run(tree))["disk-flatpaks"]


def test_drivers_done_needs_a_firmware_package(tmp_path, tree):
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "linux-firmware", "intel-microcode", "firmware-sof-signed"))
    assert "linux-firmware" in fails(run(tree))["disk-drivers"]
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "intel-microcode"))
    findings = run(tree)
    assert "disk-drivers" not in fails(findings) and levels(findings, "disk-drivers") == [ic.INFO]      # the hook leaves out what no archive has


def test_updates_done_needs_an_upgrade_line_in_dpkg_log(tmp_path, tree):
    _w(tmp_path, ic.DPKG_LOG, "2026-09-29 10:02:00 install oem-config:all <none> 24.04.3+mint18\n")
    assert "no 'upgrade' line" in fails(run(tree))["disk-updates"]
    (tmp_path / ic.DPKG_LOG).unlink()
    findings = run(tree)
    assert "disk-updates" not in fails(findings) and levels(findings, "disk-updates") == [ic.WARN]
    _set_state(tmp_path, lambda d: d["steps"]["updates"].update(detail="already up to date"))
    assert "disk-updates" not in {f.name for f in run(tree)}          # nothing was claimed, nothing to prove


def test_steps_that_are_not_done_promise_nothing(tmp_path, tree):
    for step in ("compat", "gaming", "mode_extras", "updates", "drivers"):
        _edit_state(tmp_path, lambda d, step=step: d["steps"][step].update(status="pending", detail="offline while installing"))
    _w(tmp_path, "var/lib/dpkg/status", _without(GOOD_STATUS, "winehq-staging", "lutris", "gimp"))
    assert not [n for n in fails(run(tree)) if n.startswith("disk-")]


def test_without_extras_json_the_cross_check_is_said_to_be_skipped(tree):
    findings = run(tree, extras=None)
    assert levels(findings, "disk-steps") == [ic.INFO] and fails(findings) == {}


def test_load_extras(tmp_path):
    good = tmp_path / "extras.json"
    good.write_text(json.dumps(EXTRAS), encoding="utf-8")
    assert ic.load_extras(good) == EXTRAS
    good.write_text("[1]", encoding="utf-8")
    assert ic.load_extras(good) is None
    good.write_text("{nope", encoding="utf-8")
    assert ic.load_extras(good) is None and ic.load_extras(tmp_path / "missing.json") is None


REPO = HERE.parent.parent
REAL_EXTRAS = REPO / "packages" / "lindos-installer" / "root" / "usr" / "share" / "lindos" / "installer" / "extras.json"


def test_every_item_of_the_real_extras_json_has_disk_evidence():
    """A new compat/gaming item must come with the packages that prove it is installed - otherwise it is never checked."""
    extras = ic.load_extras(REAL_EXTRAS)
    assert extras is not None
    for step in ("compat", "gaming"):
        for item in extras[step]:
            assert (step, item) in ic.ITEM_EVIDENCE, "%s item %r has no entry in install_checks.ITEM_EVIDENCE" % (step, item)


def test_the_evidence_names_are_what_the_install_scripts_install():
    compat = (REPO / "packages" / "lindos-compat" / "root" / "usr" / "libexec" / "lindos" / "install-compat.sh").read_text(encoding="utf-8")
    gaming = (REPO / "packages" / "lindos-gaming" / "root" / "usr" / "libexec" / "lindos" / "install-gaming.sh").read_text(encoding="utf-8")
    for pkg in ic.ITEM_EVIDENCE[("compat", "wine")].pkgs + ("winetricks",):
        assert pkg in compat, pkg
    assert "/usr/local/bin/umu-run" in (REPO / "packages" / "lindos-compat" / "root" / "usr" / "lib" / "lindos-compat" / "lindos_compat" /
                                       "installers.py").read_text(encoding="utf-8")
    for pkg in ic.ITEM_EVIDENCE[("gaming", "steam")].pkgs + ("lutris",):
        assert pkg in gaming, pkg
    assert "net.lutris.Lutris" in gaming


def test_the_real_extras_json_is_fully_verified_on_a_complete_installation(tmp_path):
    """Every package, Flatpak and firmware package of the shipped extras.json, installed: nothing is reported missing."""
    extras = ic.load_extras(REAL_EXTRAS)
    make_good_tree(tmp_path)
    names = list(extras["apt"]) + list(extras["drivers"]["firmware"]) + ["winehq-staging", "winetricks", "steam-launcher", "lutris"]
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS + "".join(dpkg_stanza(n, arch="amd64") for n in names))
    for app in extras["flatpaks"]:
        _w(tmp_path, "%s/%s/x86_64/stable/active/files/x" % (ic.FLATPAK_APPS, app), "")
    _set_state(tmp_path, lambda d: d["steps"]["flatpaks"].update(status="done", detail="%d Flatpak apps installed" % len(extras["flatpaks"])))
    findings = run(ic.Tree(tmp_path, GOOD_LINKS), extras=extras)
    assert fails(findings) == {}
    assert ic.OK in levels(findings, "disk-mode_extras") and ic.OK in levels(findings, "disk-flatpaks")


# --------------------------------------------------------------------------------------------- the offline path
def _offline_tree(tmp_path: Path) -> ic.Tree:
    """What a guest without a network device leaves: online=false, every step pending, no Chrome, no retry markers."""
    make_good_tree(tmp_path)
    state = json.loads(json.dumps(STATE_OK))
    state["online"] = False
    for step in ic.STEPS:
        state["steps"][step] = {"status": "pending", "detail": "offline while installing", "time": "2026-09-29T10:00:00Z"}
    _w(tmp_path, ic.INSTALL_STATE, json.dumps(state))
    for rel in (ic.BROWSER_MARKER, ic.DRIVER_MARKER, "opt/google/chrome/chrome"):
        (tmp_path / rel).unlink()
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS.replace(dpkg_stanza("google-chrome-stable", version="130.0.1", arch="amd64"), ""))
    return ic.Tree(tmp_path, GOOD_LINKS)


def test_a_correct_offline_install_passes_the_strict_offline_checks(tmp_path):
    findings = run(_offline_tree(tmp_path), expect_online=False, strict_offline=True)
    assert fails(findings) == {}
    assert levels(findings, "state-offline") == [ic.OK]
    assert ic.OK in levels(findings, "oem-armed")                        # the machine still arms oem-config


def test_offline_run_where_a_step_claims_done_fails(tmp_path):
    tree = _offline_tree(tmp_path)
    _edit_state(tmp_path, lambda d: d["steps"]["mode_extras"].update(status="done", detail="4 packages installed"))
    got = fails(run(tree, expect_online=False, strict_offline=True))
    assert "mode_extras is 'done'" in got["state-offline"]


def test_offline_run_where_the_hook_saw_a_network_fails(tmp_path):
    tree = _offline_tree(tmp_path)
    _edit_state(tmp_path, lambda d: d.update(online=True))
    got = fails(run(tree, expect_online=False, strict_offline=True))
    assert "no network at all" in got["state-offline"]
    # without the strict flag the old, softer behaviour stays (the runner's probe may be wrong): a warning
    assert "state-offline" not in fails(run(tree, expect_online=False))


def test_offline_pending_steps_should_say_they_were_offline(tmp_path):
    tree = _offline_tree(tmp_path)
    _edit_state(tmp_path, lambda d: d["steps"]["updates"].update(status="pending", detail="time budget used up"))
    findings = run(tree, expect_online=False, strict_offline=True)
    assert "state-offline" not in fails(findings) and levels(findings, "state-offline-detail") == [ic.WARN]


def test_offline_install_with_chrome_on_the_disk_fails(tmp_path):
    tree = _offline_tree(tmp_path)
    _w(tmp_path, "var/lib/dpkg/status", GOOD_STATUS)
    _w(tmp_path, "opt/google/chrome/chrome", "binary")
    assert "chrome-vs-state" in fails(run(tree, expect_online=False, strict_offline=True))


# --------------------------------------------------------------------------------------------- the account wizard's look
def test_a_correct_install_has_the_wizard_skin_the_drop_in_the_title_and_the_cleanup_command(tree):
    findings = run(tree)
    for name in ("wizard-skin", "wizard-theme-dropin", "wizard-title", "wizard-cleanup"):
        assert levels(findings, name) == [ic.OK], name


def test_a_missing_skin_or_drop_in_fails_and_names_what_to_fix(tmp_path, tree):
    (tmp_path / ic.WIZARD_SKIN).unlink()
    (tmp_path / ic.WIZARD_DROPIN).unlink()
    got = fails(run(tree))
    assert "79-installer-flow.sh" in got["wizard-skin"] and "finalize.sh" in got["wizard-theme-dropin"]


@pytest.mark.parametrize("text", ["[Service]\nEnvironment=GTK_THEME=Lindos-Dark\n", "[Service]\n# Environment=GTK_THEME=Lindos-Setup\n", ""])
def test_a_drop_in_for_another_theme_fails(tmp_path, tree, text):
    _w(tmp_path, ic.WIZARD_DROPIN, text)
    assert "does not set GTK_THEME=Lindos-Setup" in fails(run(tree))["wizard-theme-dropin"]


def test_a_wrong_or_missing_title_and_cleanup_only_warn(tmp_path, tree):
    _w(tmp_path, ic.DEBCONF_CONFIG, "Name: user-setup/allow-password-empty\nValue: false\n")
    findings = run(tree)
    assert fails(findings) == {} and levels(findings, "wizard-title") == [ic.WARN] and levels(findings, "wizard-cleanup") == [ic.WARN]
    _w(tmp_path, ic.DEBCONF_CONFIG, DEBCONF_CONFIG.replace("Value: Lindos Setup", "Value: System Configuration"))
    assert "System Configuration" in next(f.detail for f in run(tree) if f.name == "wizard-title")
    (tmp_path / ic.DEBCONF_CONFIG).unlink()
    assert levels(run(tree), "wizard-title") == [ic.WARN]


def test_the_installer_sessions_theme_drop_in_is_a_leftover_on_the_installed_system(tmp_path, tree):
    assert "etc/systemd/system/ubiquity.service.d/10-lindos.conf" in ic.HOOK_LEFTOVERS
    _w(tmp_path, "etc/systemd/system/ubiquity.service.d/10-lindos.conf", "[Service]\n")
    assert "ubiquity.service.d" in fails(run(tree))["leftovers"]


def test_a_leaked_apt_config_in_the_installer_log_fails_the_run(tmp_path, tree):
    """First real install: four 'E: Syntax error /usr/bin/apt-config:13: Extra junk after value' around the Chrome install."""
    _w(tmp_path, ic.INSTALLER_LOG, INSTALLER_LOG + "E: Syntax error /usr/bin/apt-config:13: Extra junk after value\n")
    assert any("APT_CONFIG leaked" in v for v in fails(run(tree)).values())
    held = ic.scan_installer_logs({"var/log/lindos/installer.log": "E: Error, pkgProblemResolver::Resolve generated breaks, this may be caused by held packages.\n"})
    assert [f.level for f in held] == [ic.WARN] and "held packages" in held[0].detail
