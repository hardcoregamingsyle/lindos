"""lindos-installer: package metadata, the data files, and the pieces of the scripts that can be checked
without running them (the behaviour tests are in test_target_config.py / test_finalize.py)."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
from installer_testlib import BASH, LIBEXEC, PKG_ROOT, REPO, SHARE, Sandbox, needs_bash

PKG = REPO / "packages" / "lindos-installer"
SCRIPTS = ("lib.sh", "target-config.sh", "finalize.sh")


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _fields(control: str) -> dict:
    out = {}
    for line in control.splitlines():
        if line and not line.startswith((" ", "\t")) and ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- metadata
def test_control_is_a_lindos_package_that_depends_on_core_only() -> None:
    f = _fields(_text(PKG / "DEBIAN" / "control"))
    assert f["Package"] == "lindos-installer" and f["Architecture"] == "all" and f["Version"] == "1.0.0"
    assert f["Maintainer"] == "Lindos Team <team@lindos.dev>"
    assert "lindos-core" in f["Depends"] and "python3" in f["Depends"]
    assert set(re.findall(r"lindos-[a-z]+", f["Depends"])) == {"lindos-core"}, "no hard dependency on other Lindos packages"
    assert "Mint" not in _text(PKG / "DEBIAN" / "control")


def test_the_meta_package_does_not_pull_the_installer_onto_the_installed_system() -> None:
    meta = _fields(_text(REPO / "packages" / "lindos-meta" / "DEBIAN" / "control"))
    for field in ("Depends", "Recommends", "Suggests"):
        assert "lindos-installer" not in meta.get(field, ""), field


def test_the_deb_order_installs_it_and_the_medium_removes_it_from_the_new_system() -> None:
    cfg = _text(REPO / "build" / "config.env")
    m = re.search(r'LINDOS_DEB_ORDER:=([a-z0-9 _-]+)\}', cfg)
    assert m and "lindos-installer" in m.group(1).split()
    assert re.search(r'^export .*\bLINDOS_DEB_ORDER\b', cfg, re.M)
    assert "lindos-installer" in _text(REPO / "build" / "chroot" / "30-lindos-debs.sh")
    assert re.search(r'LIVE_ONLY_PACKAGES:=lindos-installer\}', cfg)
    assert re.search(r'^export .*\bLIVE_ONLY_PACKAGES\b', cfg, re.M)
    assert "filesystem.manifest-remove" in _text(REPO / "build" / "build-iso.sh")


# --------------------------------------------------------------------------- scripts
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_lf_bash_and_never_sudo(name: str) -> None:
    raw = (LIBEXEC / name).read_bytes()
    assert b"\r" not in raw and raw.startswith(b"#!/bin/bash\n")
    code = "\n".join(ln for ln in raw.decode("utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\bsudo\b", code)
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*[eu]", code, re.M), "no set -e / set -u: a failing command must not end the hook"


def test_the_hook_carries_the_contract_of_a_ubiquity_target_config_hook() -> None:
    hook = _text(LIBEXEC / "target-config.sh")
    lib = _text(LIBEXEC / "lib.sh")
    both = hook + lib
    for needle in ("timeout -k", "unshare --mount --propagation private", "apt-mark hold", "apt-mark unhold",
                   "systemd-inhibit", "DPkg::Lock::Timeout", "dpkg --configure -a", "-f install", "dpkg --audit",
                   "db_progress INFO", "db_x_loadtemplatefile", "db_subst", "trap li_on_exit EXIT",
                   "--in-installer --download-only", "--in-installer --no-download", "policy-rc.d",
                   "lindos.install=off", "lindos.install_budget", "lindos.installstate"):
        assert needle in both, needle
    # downloads first (-d), installs from the download (--no-download), an upgrade never a dist-upgrade
    assert "-d upgrade" in hook and "--no-download upgrade" in hook and "--only-upgrade" not in hook
    code = "\n".join(ln for ln in both.splitlines() if not ln.lstrip().startswith("#"))
    assert "dist-upgrade" not in code and "full-upgrade" not in code
    # the vendor's hosts are install-browser.sh's business (single source of truth)
    assert "dl.google.com" not in both and "packages.microsoft.com" not in both
    # nothing is printed to Ubiquity's debconf pipe
    assert "exec 1>&2" in hook and "exec </dev/null" in hook


def test_hold_families_cover_installer_kernel_and_bootloader() -> None:
    m = re.search(r"^LI_HOLD_RE='(.+)'$", _text(LIBEXEC / "lib.sh"), re.M)
    assert m
    rx = re.compile(m.group(1))
    for held in ("ubiquity", "ubiquity-frontend-gtk", "oem-config-gtk", "casper", "linux-image-6.14.0-lindos",
                 "linux-modules-6.8.0-45-generic", "linux-headers-generic-hwe-24.04", "lindos-kernel", "grub-efi-amd64-signed",
                 "grub-common", "shim-signed", "mokutil", "efibootmgr", "os-prober", "initramfs-tools", "plymouth"):
        assert rx.match(held), held
    for free in ("libc6", "firefox", "linux-firmware", "libreoffice-writer", "flatpak", "google-chrome-stable"):
        assert not rx.match(free), free


@needs_bash
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse(name: str) -> None:
    res = subprocess.run([BASH, "-n", str(LIBEXEC / name)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr


@needs_bash
def test_loading_the_library_has_no_side_effects(tmp_path: Path) -> None:
    log = tmp_path / "live" / "hook.log"
    target = tmp_path / "target"
    res = subprocess.run([BASH, "-c", ". '%s'" % (LIBEXEC / "lib.sh").as_posix()], capture_output=True, text=True,
                         timeout=60, env={"PATH": __import__("os").environ["PATH"], "LINDOS_INSTALLER_LOG": log.as_posix(),
                                          "LINDOS_TARGET": target.as_posix()})
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert not log.parent.exists() and not target.exists()


@needs_bash
def test_the_enter_runner_mounts_in_a_private_namespace_and_starts_a_clean_chroot(sandbox: Sandbox) -> None:
    res = sandbox.run(LIBEXEC / "lib.sh", "--enter", sandbox.target.as_posix(), "true",
                      LINDOS_CHROOT_APT_CONFIG="/var/lib/lindos/installer-apt.conf", http_proxy="http://proxy.example:3128",
                      DEBIAN_HAS_FRONTEND="1", DEBCONF_REDIR="1", DEBIAN_FRONTEND="passthrough")
    assert res.returncode == 0, res.stderr
    t = sandbox.target.as_posix()
    mounts = sandbox.calls_of("mount")
    assert mounts == [f"mount -t proc proc {t}/proc", f"mount -t sysfs sysfs {t}/sys", f"mount --rbind /dev {t}/dev",
                      f"mount -t tmpfs tmpfs {t}/run -o mode=0755,nosuid,nodev"], mounts
    chroot = sandbox.calls_of("chroot")
    assert len(chroot) == 1 and chroot[0].startswith(f"chroot {t} /usr/bin/env -i "), chroot
    for word in ("LINDOS_INSTALLER=1", "DEBIAN_FRONTEND=noninteractive", "APT_CONFIG=/var/lib/lindos/installer-apt.conf",
                 "http_proxy=http://proxy.example:3128", "LC_ALL=C.UTF-8"):
        assert word in chroot[0].split(), word
    # the debconf pipe of Ubiquity's filter never reaches what runs in the new system
    for banned in ("DEBIAN_HAS_FRONTEND", "DEBCONF_REDIR", "DEBIAN_FRONTEND=passthrough"):
        assert banned not in chroot[0], banned


@needs_bash
def test_the_enter_runner_fails_with_97_when_a_mount_fails(sandbox: Sandbox) -> None:
    (sandbox.bin / "mount").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8", newline="\n")
    res = sandbox.run(LIBEXEC / "lib.sh", "--enter", sandbox.target.as_posix(), "true")
    assert res.returncode == 97
    assert sandbox.calls_of("chroot") == [], "never a chroot with a half-mounted target"


@needs_bash
def test_running_the_library_directly_is_a_usage_error(sandbox: Sandbox) -> None:
    res = sandbox.run(LIBEXEC / "lib.sh")
    assert res.returncode == 2 and "library" in res.stderr


# --------------------------------------------------------------------------- data files
def _extras_module():
    spec = importlib.util.spec_from_file_location("installer_extras", REPO / "build" / "lib" / "installer_extras.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_extras_json_is_the_union_of_every_modes_extras() -> None:
    mod = _extras_module()
    committed = json.loads(_text(SHARE / "extras.json"))
    derived = mod.derive()
    assert committed == derived, "extras.json is stale: run python3 build/lib/installer_extras.py --write"
    assert _text(SHARE / "extras.json") == mod.render(derived)
    # ... and independently of the generator: every mode.json package/flatpak is in it
    modes = REPO / "packages" / "lindos-core" / "root" / "usr" / "share" / "lindos" / "modes"
    apt, flatpaks = set(), set()
    for f in modes.glob("*/mode.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        apt.update(data.get("packages", []))
        flatpaks.update(data.get("flatpaks", []))
    assert apt <= set(committed["apt"]) and flatpaks == set(committed["flatpaks"])
    assert {"com.usebottles.bottles", "org.prismlauncher.PrismLauncher", "org.vinegarhq.Sober",
            "com.heroicgameslauncher.hgl"} == flatpaks
    assert {"gimp", "krita", "kdenlive"} <= set(committed["apt"]), "the OOBE's creative apt defaults"


def test_extras_items_are_known_to_the_install_scripts() -> None:
    doc = json.loads(_text(SHARE / "extras.json"))
    compat_sh = _text(REPO / "packages" / "lindos-compat" / "root" / "usr" / "libexec" / "lindos" / "install-compat.sh")
    gaming_sh = _text(REPO / "packages" / "lindos-gaming" / "root" / "usr" / "libexec" / "lindos" / "install-gaming.sh")
    known = re.search(r"^KNOWN_ITEMS=\(([^)]*)\)", compat_sh, re.M).group(1).split()
    apt_items = re.search(r"^APT_ITEMS=\(([^)]*)\)", gaming_sh, re.M).group(1).split()
    assert doc["compat"] and set(doc["compat"]) <= set(known) and "bottles" not in doc["compat"]
    assert doc["gaming"] and set(doc["gaming"]) <= set(apt_items)
    assert not set(doc["gaming"]) & {"prism", "sober", "heroic"}, "Flatpak launchers come from the flatpaks step: nothing twice"
    assert "org.onlyoffice.desktopeditors" not in doc["flatpaks"] and "org.onlyoffice.desktopeditors" in doc["optional_flatpaks"]
    for key in ("apt", "flatpaks", "compat", "gaming"):
        assert all(isinstance(i, str) and i.strip() == i and i for i in doc[key]), key
    assert doc["drivers"]["firmware"], "firmware is always ensured"


def test_the_seed_bakes_the_oem_flow_and_matches_the_boot_entries() -> None:
    rows = []
    for line in _text(SHARE / "lindos.seed").splitlines():
        if line.strip() and not line.startswith("#"):
            owner, question, kind, *value = line.split(None, 3)
            rows.append((owner, question, kind, value[0] if value else ""))
    seed = {q: (o, k, v) for o, q, k, v in rows}
    assert seed["oem-config/enable"] == ("ubiquity", "boolean", "true")
    assert seed["ubiquity/success_command"] == ("ubiquity", "string", "/usr/libexec/lindos/installer/finalize.sh")
    assert seed["ubiquity/download_updates"][2] == "false"
    assert seed["apt-setup/multiarch"][2] == "i386"
    assert seed["user-setup/allow-password-empty"][2] == "true"
    assert all(k in ("string", "boolean", "select", "password") for _o, k, _v in seed.values())
    grub = _text(REPO / "build" / "overlay" / "boot" / "grub" / "grub.cfg")
    assert "ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh" in grub


def test_the_progress_template_is_one_text_line() -> None:
    text = _text(SHARE / "lindos-installer.templates")
    assert text.splitlines()[:3] == ["Template: lindos-installer/msg", "Type: text", "Description: ${MSG}"]
    assert b"\r" not in (SHARE / "lindos-installer.templates").read_bytes()


def test_the_shipped_tree_has_only_what_the_flow_needs() -> None:
    files = sorted(p.relative_to(PKG_ROOT).as_posix() for p in PKG_ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert files == ["usr/libexec/lindos/installer/finalize.sh", "usr/libexec/lindos/installer/lib.sh",
                     "usr/libexec/lindos/installer/target-config.sh", "usr/share/lindos/installer/extras.json",
                     "usr/share/lindos/installer/lindos-installer.templates", "usr/share/lindos/installer/lindos.seed"]
