#!/usr/bin/env python3
"""build/qa/install_checks.py - read-only assertions on a freshly INSTALLED Lindos disk.

build/qa/install_test.py installs the ISO onto a blank virtual disk (Ubiquity in OEM mode, driven by a
CI-only preseed), powers the guest off, mounts the disk read-only and hands the mounted tree to the
functions here.  Nothing in this module needs Linux, root or QEMU: every check reads plain files through
:class:`Tree`, so the unit tests (build/tests/test_install_test_qa.py) drive it with fake trees on any OS.

What "installed correctly" means here is the contract of the installer flow (docs/BUILDING.md "Installer
flow", packages/lindos-installer):

  * ``/var/lib/lindos/install-state.json`` exists, is schema 1, has a sane status for every step and does
    not contradict itself (a step cannot be ``done`` while the hook saw no network);
  * ``/var/log/lindos/installer.log`` shows the hook and ``finalize.sh`` really ran;
  * oem-config is armed (default target = oem-config.target, units and program present, packages
    installed, no ``oem-config-not-armed`` marker), the temporary ``oem`` account exists and is locked and
    LightDM does not auto-login it;
  * the installer left nothing behind (policy-rc.d, holds, pins, hook copy, half-configured packages,
    diverted start-stop-daemon) and dpkg is clean;
  * the browser marker only exists when Chrome is installed (or the step was skipped on purpose) and
    Chrome is installed exactly when the step says ``done``;
  * every OTHER step that says ``done`` really left its packages / files on the disk (the packages of
    ``extras.json`` for mode_extras, compat and gaming, the Flatpak app directories, the firmware
    packages for drivers, an ``upgrade`` line in dpkg.log for updates) - a step that swallowed its list
    and recorded "nothing to install" against a non-empty ``extras.json`` fails;
  * the first-boot account wizard has its Lindos look: the Lindos-Setup skin is on the disk, oem-config.service has a
    drop-in with GTK_THEME=Lindos-Setup, the window title answer is "Lindos Setup" and the wizard removes the drop-in
    itself; the installer session's own drop-in is gone (check_wizard_look, HOOK_LEFTOVERS);
  * the answers the image bakes (lindos.seed) had the intended effect on the installed system:
    ``user-setup/allow-password-empty`` is NOT ``true`` in the new debconf database (finalize.sh reset
    it) and the i386 architecture is still enabled in dpkg (Wine and Steam need it);
  * with ``strict_offline`` (a run without any network) the hook must have recorded ``online=false`` and
    every step ``pending`` - the offline path - while everything above about oem-config still holds;
  * a boot loader and a kernel/initrd pair are on the disk.

Every check returns :class:`Finding` objects with a level: ``fail`` fails the test, ``warn`` is reported
but does not, ``info`` records what was seen (the maintainers read those to learn what a real install
did), ``ok`` is a passed check.  Symlinks are resolved INSIDE the tree (an absolute link such as
``/lib/systemd/system/oem-config.target`` must never be looked up on the host running the test).

Standard library only; importable on any OS.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

# ---- the contract (lindos.installstate; pinned against the real module by the tests) -------------
STEPS: Tuple[str, ...] = ("updates", "drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks")
STATUSES: Tuple[str, ...] = ("done", "pending", "skipped", "failed")
STATE_SCHEMA = 1

INSTALL_STATE = "var/lib/lindos/install-state.json"
INSTALLER_LOG = "var/log/lindos/installer.log"
BROWSER_MARKER = "var/lib/lindos/browser-firstboot.done"
DRIVER_MARKER = "var/lib/lindos/driver-firstboot.done"
NOT_ARMED_MARKER = "var/lib/lindos/oem-config-not-armed"
DEBCONF_CONFIG = "var/cache/debconf/config.dat"      # the new system's debconf answers (finalize.sh edits them)
DPKG_ARCH = "var/lib/dpkg/arch"                      # the foreign architectures dpkg knows (one per line)
DPKG_LOG = "var/log/dpkg.log"
FLATPAK_APPS = "var/lib/flatpak/app"                 # system installation: one directory per installed app id
# the steps whose 'failed' is a failure (not a warning) on a run that has internet: everything the hook does with
# Ubuntu's own archives.  compat, gaming and flatpaks use third-party repositories / Flathub inside a chroot
# (unproven), so they stay warnings.
HARD_ONLINE_STEPS: Tuple[str, ...] = ("updates", "drivers", "mode_extras")

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
LEVELS = (OK, INFO, WARN, FAIL)

_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class Finding:
    """One observation about the installed system."""

    name: str
    level: str
    detail: str = ""

    def line(self) -> str:
        tag = {OK: "ok  ", INFO: "info", WARN: "WARN", FAIL: "FAIL"}[self.level]
        return "[%s] %s%s" % (tag, self.name, (": " + self.detail) if self.detail else "")


def ok(name: str, detail: str = "") -> Finding:
    return Finding(name, OK, detail)


def info(name: str, detail: str = "") -> Finding:
    return Finding(name, INFO, detail)


def warn(name: str, detail: str = "") -> Finding:
    return Finding(name, WARN, detail)


def fail(name: str, detail: str = "") -> Finding:
    return Finding(name, FAIL, detail)


def failures(findings: Iterable[Finding]) -> List[Finding]:
    return [f for f in findings if f.level == FAIL]


def warnings(findings: Iterable[Finding]) -> List[Finding]:
    return [f for f in findings if f.level == WARN]


# ---- a read-only view of a mounted system ---------------------------------------------------------
class Tree:
    """A read-only view of an installed system mounted at *root*.

    Every path is relative to the installed system's ``/`` and every symlink - including absolute ones - is
    followed inside the tree, never on the host.  *symlinks* (relative path -> link target) overrides
    ``os.readlink`` so tests can describe symlinks on hosts that cannot create them.
    """

    MAX_HOPS = 40

    def __init__(self, root: Path, symlinks: Optional[Mapping[str, str]] = None) -> None:
        self.root = Path(root)
        self._links = {self._norm(k): v for k, v in (symlinks or {}).items()}

    @staticmethod
    def _norm(rel: str) -> str:
        return "/".join(p for p in str(rel).replace("\\", "/").split("/") if p and p != ".")

    def _link_target(self, rel: str) -> Optional[str]:
        if rel in self._links:
            return self._links[rel]
        path = self.root.joinpath(*rel.split("/")) if rel else self.root
        try:
            if os.path.islink(path):
                return os.readlink(path)
        except OSError:
            return None
        return None

    def _resolve(self, rel: str) -> Optional[Path]:
        """The host path of *rel* with every symlink of its path resolved inside the tree."""
        stack = [p for p in self._norm(rel).split("/") if p][::-1]
        done: List[str] = []
        hops = 0
        while stack:
            part = stack.pop()
            if part == "..":
                done = done[:-1]
                continue
            target = self._link_target("/".join(done + [part]))
            if target is not None:
                hops += 1
                if hops > self.MAX_HOPS:
                    return None
                if target.startswith("/"):
                    done = []
                stack.extend([p for p in target.replace("\\", "/").split("/") if p and p != "."][::-1])
                continue
            done.append(part)
        return self.root.joinpath(*done) if done else self.root

    def readlink(self, rel: str) -> Optional[str]:
        """The raw target of the symlink at *rel* (its parents are resolved), or None when it is not one."""
        rel = self._norm(rel)
        if not rel:
            return None
        parent, _, leaf = rel.rpartition("/")
        base = self._resolve(parent) if parent else self.root
        if base is None:
            return None
        key = "/".join(p for p in (parent, leaf) if p)
        if key in self._links:
            return self._links[key]
        try:
            path = base / leaf
            return os.readlink(path) if os.path.islink(path) else None
        except OSError:
            return None

    def lexists(self, rel: str) -> bool:
        rel = self._norm(rel)
        if rel in self._links:
            return True
        parent, _, leaf = rel.rpartition("/")
        base = self._resolve(parent) if parent else self.root
        return bool(base is not None and os.path.lexists(base / leaf))

    def is_file(self, rel: str) -> bool:
        p = self._resolve(rel)
        return bool(p is not None and p.is_file())

    def is_dir(self, rel: str) -> bool:
        p = self._resolve(rel)
        return bool(p is not None and p.is_dir())

    def read_bytes(self, rel: str, limit: int = 8 << 20) -> Optional[bytes]:
        p = self._resolve(rel)
        if p is None:
            return None
        try:
            with open(p, "rb") as fh:
                return fh.read(limit)
        except OSError:
            return None

    def read_text(self, rel: str, limit: int = 8 << 20) -> Optional[str]:
        data = self.read_bytes(rel, limit)
        return None if data is None else data.decode("utf-8", errors="replace")

    def listdir(self, rel: str) -> List[str]:
        p = self._resolve(rel)
        try:
            return sorted(os.listdir(p)) if p is not None else []
        except OSError:
            return []

    def copy_to(self, rel: str, dest: Path) -> bool:
        """Copy one file out of the tree (following symlinks inside it); False when it is not there."""
        p = self._resolve(rel)
        if p is None or not p.is_file():
            return False
        import shutil
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        return True


# ---- dpkg status --------------------------------------------------------------------------------
class Pkg(NamedTuple):
    name: str
    want: str        # install | hold | deinstall | purge | unknown
    flag: str        # ok | reinstreq | hold | hold-reinstreq
    state: str       # installed | half-installed | unpacked | half-configured | triggers-* | config-files | not-installed
    version: str


BROKEN_STATES = ("half-installed", "unpacked", "half-configured", "triggers-awaited", "triggers-pending")


def parse_dpkg_status(text: str) -> Dict[str, Pkg]:
    """``/var/lib/dpkg/status`` -> {package: Pkg} ('name:arch' for foreign architectures)."""
    pkgs: Dict[str, Pkg] = {}
    name = status = version = arch = ""

    def flush() -> None:
        if not name:
            return
        parts = status.split()
        want, flag, state = (parts + ["", "", ""])[:3] if len(parts) >= 3 else ("unknown", "ok", "not-installed")
        key = name if arch in ("", "all", "amd64") else "%s:%s" % (name, arch)
        pkgs[key] = Pkg(key, want, flag, state, version)

    for line in text.splitlines():
        if line.startswith("Package: "):
            flush()
            name, status, version, arch = line[9:].strip(), "", "", ""
        elif line.startswith("Status: "):
            status = line[8:].strip()
        elif line.startswith("Version: "):
            version = line[9:].strip()
        elif line.startswith("Architecture: "):
            arch = line[14:].strip()
    flush()
    return pkgs


def is_installed(pkgs: Mapping[str, Pkg], name: str) -> bool:
    p = pkgs.get(name)
    return bool(p and p.state == "installed")


def audit_packages(pkgs: Mapping[str, Pkg]) -> Tuple[List[str], List[str]]:
    """(broken packages, held packages) - what ``dpkg --audit`` and ``apt-mark showhold`` would list."""
    broken = sorted(p.name for p in pkgs.values() if p.state in BROKEN_STATES or "reinstreq" in p.flag)
    held = sorted(p.name for p in pkgs.values() if p.want == "hold" or p.flag.startswith("hold"))
    return broken, held


# ---- the individual checks ---------------------------------------------------------------------------
def load_state(tree: Tree) -> Tuple[Optional[dict], List[Finding]]:
    """The parsed install-state.json (None when unusable) and the findings about the file itself."""
    raw = tree.read_text(INSTALL_STATE)
    if raw is None:
        return None, [fail("state-file", "/%s does not exist: the installer hook never recorded anything" % INSTALL_STATE)]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, [fail("state-file", "/%s is not valid JSON: %s" % (INSTALL_STATE, exc))]
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return None, [fail("state-file", "schema is %r, expected %d" % (
            data.get("schema") if isinstance(data, dict) else data, STATE_SCHEMA))]
    return data, []


def check_install_state(tree: Tree, *, expect_online: Optional[bool],
                        strict_offline: bool = False) -> Tuple[List[Finding], Optional[dict]]:
    """Schema, step ids/statuses, and consistency with the network the runner really had.

    *expect_online*: True when the CI runner has internet (Chrome must then be installed), False for an
    offline run (every network step may be pending), None when unknown (only recorded).
    *strict_offline*: the guest had NO network device at all (install_test.py ``--network off``): the hook
    must then have recorded ``online=false`` and every step ``pending`` (li_main marks all of them pending
    "offline while installing" before it does anything), otherwise the run proves nothing about the offline
    path.
    """
    data, out = load_state(tree)
    if data is None:
        return out, None
    online = data.get("online")
    updated = data.get("updated", "")
    out.append(ok("state-file", "schema %d, online=%r, updated %s" % (STATE_SCHEMA, online, updated or "?")))
    if updated and not _ISO_TIME.match(str(updated)):
        out.append(warn("state-time", "updated %r is not an ISO-8601 UTC time" % (updated,)))

    steps = data.get("steps")
    if not isinstance(steps, dict):
        out.append(fail("state-steps", "'steps' is not an object"))
        return out, data
    unknown = sorted(s for s in steps if s not in STEPS)
    missing = [s for s in STEPS if s not in steps]
    if unknown:
        out.append(fail("state-steps", "unknown step id(s): %s" % ", ".join(unknown)))
    if missing:
        out.append(fail("state-steps", "no entry for step(s): %s (finalize.sh marks unrecorded steps pending)"
                        % ", ".join(missing)))
    for sid in STEPS:
        entry = steps.get(sid)
        if entry is None:
            continue
        status = entry.get("status") if isinstance(entry, dict) else None
        detail = str(entry.get("detail", "")) if isinstance(entry, dict) else ""
        if status not in STATUSES:
            out.append(fail("step-" + sid, "status %r is not one of %s" % (status, "/".join(STATUSES))))
            continue
        text = "%s%s" % (status, (" - " + detail) if detail else "")
        if status == "failed" and expect_online is True and sid in HARD_ONLINE_STEPS:
            out.append(fail("step-" + sid, text + " (the runner has internet: a step that only needs Ubuntu's archives must not fail)"))
        elif status == "failed":
            out.append(warn("step-" + sid, text))
        elif status == "pending" and expect_online:
            out.append(warn("step-" + sid, text + " (the runner has internet: this should have been done)"))
        else:
            out.append(info("step-" + sid, text))
    if not unknown and not missing:
        out.append(ok("state-steps", "all %d steps have a valid status" % len(STEPS)))

    def status_of(step: str) -> str:
        entry = steps.get(step)
        return entry.get("status", "") if isinstance(entry, dict) else ""

    if online is False:
        for step in ("browser", "updates", "flatpaks"):
            if status_of(step) == "done":
                out.append(fail("state-consistent", "step %s is 'done' although the hook recorded online=false" % step))
    if expect_online is True:
        if online is not True:
            out.append(fail("state-online", "the runner has internet but the hook recorded online=%r" % (online,)))
        if status_of("browser") != "done":
            out.append(fail("state-browser", "Chrome must be installed when online, but the browser step is %r"
                            % (status_of("browser") or "missing")))
    elif expect_online is False and online is True and not strict_offline:
        out.append(warn("state-online", "the runner had no internet but the hook recorded online=true"))
    if strict_offline:
        out.extend(_check_offline_path(steps, online))
    return out, data


def _check_offline_path(steps: Mapping, online: object) -> List[Finding]:
    """A guest without a network device: the hook records online=false and every step pending, nothing else."""
    out: List[Finding] = []
    if online is not False:
        out.append(fail("state-offline", "the guest had no network at all but the hook recorded online=%r" % (online,)))
    bad = []
    for sid in STEPS:
        entry = steps.get(sid)
        status = entry.get("status") if isinstance(entry, dict) else None
        detail = str(entry.get("detail", "")) if isinstance(entry, dict) else ""
        if status is None:
            continue           # a missing step is reported as such by the caller
        if status != "pending":
            bad.append("%s is %r" % (sid, status))
        elif "offline" not in detail.lower():
            out.append(warn("state-offline-detail", "step %s is pending but says %r, not that it was offline" % (sid, detail)))
    if bad:
        out.append(fail("state-offline", "an offline install must leave every step 'pending' (the retries do them later): "
                                         + "; ".join(bad)))
    elif online is False:
        out.append(ok("state-offline", "offline path: online=false and all %d steps are pending" % len(STEPS)))
    return out


_HOOK_START = re.compile(r"lindos-installer: start \(version ")
_HOOK_END = re.compile(r"lindos-installer: finished in (\d+)s")
_FIN_START = re.compile(r"lindos-installer: finalize: start")
_FIN_DONE = re.compile(r"lindos-installer: finalize: done")
_FIN_ARMED = re.compile(r"lindos-installer: finalize: oem-config is armed")
_STEP_LINE = re.compile(r"lindos-installer: step (\w+): (\w+)(?: - (.*))?$")
_CRITICAL = re.compile(r"lindos-installer: .*\bCRITICAL\b")
_WARNING = re.compile(r"lindos-installer: .*\bWARNING\b")


def parse_installer_log(text: str) -> dict:
    """What /var/log/lindos/installer.log says about the hook and finalize.sh."""
    steps: Dict[str, str] = {}
    critical: List[str] = []
    warnings_: List[str] = []
    res = {"hook_started": False, "hook_finished": False, "hook_seconds": None, "finalize_started": False,
           "finalize_done": False, "armed": False, "steps": steps, "critical": critical, "warnings": warnings_}
    for raw in text.splitlines():
        line = raw.strip()
        if _HOOK_START.search(line):
            res["hook_started"] = True
        m = _HOOK_END.search(line)
        if m:
            res["hook_finished"] = True
            res["hook_seconds"] = int(m.group(1))
        if _FIN_START.search(line):
            res["finalize_started"] = True
        if _FIN_DONE.search(line):
            res["finalize_done"] = True
        if _FIN_ARMED.search(line):
            res["armed"] = True
        m = _STEP_LINE.search(line)
        if m:
            steps[m.group(1)] = m.group(2)
        if _CRITICAL.search(line):
            critical.append(line)
        elif _WARNING.search(line):
            warnings_.append(line)
    return res


def check_installer_log(tree: Tree, state: Optional[dict]) -> List[Finding]:
    text = tree.read_text(INSTALLER_LOG)
    if text is None or not text.strip():
        return [fail("installer-log", "/%s is missing or empty: neither the hook nor finalize.sh logged" % INSTALLER_LOG)]
    log = parse_installer_log(text)
    out: List[Finding] = []
    if log["hook_started"]:
        tail = "finished in %ss" % log["hook_seconds"] if log["hook_finished"] else "never logged 'finished' (killed or hung?)"
        out.append(ok("hook-ran", "the target-config hook started, " + tail) if log["hook_finished"]
                   else warn("hook-ran", "the target-config hook started but " + tail))
    else:
        out.append(fail("hook-ran", "installer.log has no 'start (version' line: the target-config hook did not run "
                                    "(wrong name/mode, or Ubiquity skipped it)"))
    if log["finalize_done"]:
        out.append(ok("finalize-ran", "finalize.sh (ubiquity/success_command) ran to the end"))
    elif log["finalize_started"]:
        out.append(fail("finalize-ran", "finalize.sh started but never logged 'done'"))
    else:
        out.append(fail("finalize-ran", "finalize.sh never ran: ubiquity/success_command did not reach Ubiquity"))
    for line in log["critical"]:
        out.append(fail("installer-critical", line))
    for line in log["warnings"][:10]:
        out.append(warn("installer-warning", line))
    if state and isinstance(state.get("steps"), dict):
        for step, status in sorted(log["steps"].items()):
            entry = state["steps"].get(step)
            recorded = entry.get("status") if isinstance(entry, dict) else None
            if recorded is not None and recorded != status:
                out.append(warn("log-vs-state", "step %s: the log says %s, install-state.json says %s"
                                % (step, status, recorded)))
    return out


def check_oem_armed(tree: Tree, pkgs: Mapping[str, Pkg]) -> List[Finding]:
    out: List[Finding] = []
    target = tree.readlink("etc/systemd/system/default.target")
    if target is not None and target.rstrip("/").rsplit("/", 1)[-1] == "oem-config.target":
        out.append(ok("oem-armed", "default.target -> %s" % target))
    else:
        out.append(fail("oem-armed", "default.target is %s, not oem-config.target: the first boot would not start "
                                     "the account wizard" % (("-> " + target) if target else "not a symlink / missing")))
    for unit in ("oem-config.service", "oem-config.target"):
        if tree.is_file("lib/systemd/system/" + unit) or tree.is_file("usr/lib/systemd/system/" + unit):
            out.append(ok("oem-unit-" + unit.split(".")[1], "%s is in /lib/systemd/system" % unit))
        else:
            out.append(fail("oem-unit-" + unit.split(".")[1], "%s is not in /lib/systemd/system (finalize.sh copies "
                                                              "it from /usr/lib/oem-config)" % unit))
    if tree.lexists("etc/systemd/system/oem-config.target.wants/oem-config.service"):
        out.append(info("oem-enable-link", "oem-config.service is enabled under oem-config.target.wants"))
    else:
        out.append(info("oem-enable-link", "no oem-config.target.wants/oem-config.service link (the target Wants= it itself)"))
    if tree.lexists("usr/sbin/oem-config-firstboot"):
        out.append(ok("oem-firstboot-program", "/usr/sbin/oem-config-firstboot is there"))
    else:
        out.append(fail("oem-firstboot-program", "/usr/sbin/oem-config-firstboot is missing (oem-config is not installed)"))
    for pkg in ("oem-config", "oem-config-gtk"):
        if is_installed(pkgs, pkg):
            out.append(ok("pkg-" + pkg, "%s %s installed" % (pkg, pkgs[pkg].version)))
        else:
            out.append(fail("pkg-" + pkg, "%s is not installed (Ubiquity skips a package it cannot find in the medium's pool)" % pkg))
    if tree.lexists(NOT_ARMED_MARKER):
        out.append(fail("oem-not-armed", "/%s exists: finalize.sh gave up arming oem-config: %s"
                        % (NOT_ARMED_MARKER, (tree.read_text(NOT_ARMED_MARKER) or "").strip()[:200])))
    return out


def check_lightdm(tree: Tree) -> List[Finding]:
    found: List[str] = []
    files = ["etc/lightdm/lightdm.conf"] + ["etc/lightdm/lightdm.conf.d/" + n for n in tree.listdir("etc/lightdm/lightdm.conf.d")]
    scanned = 0
    for rel in files:
        text = tree.read_text(rel)
        if text is None:
            continue
        scanned += 1
        for line in text.splitlines():
            if re.match(r"^\s*autologin-user\s*=\s*oem\s*$", line):
                found.append("%s: %s" % (rel, line.strip()))
    if found:
        return [fail("lightdm-autologin", "LightDM would auto-login the deleted temporary account: " + "; ".join(found))]
    return [ok("lightdm-autologin", "no autologin-user=oem in %d LightDM config file(s)" % scanned)]


def check_temp_account(tree: Tree) -> List[Finding]:
    passwd = tree.read_text("etc/passwd")
    if passwd is None or not re.search(r"^oem:", passwd, re.M):
        return [fail("oem-account", "no temporary 'oem' account: the installer did not run in OEM mode "
                                    "(oem-config/enable=true was not honoured), so there is nothing to arm")]
    out = [ok("oem-account", "the temporary 'oem' account exists")]
    shadow = tree.read_text("etc/shadow")
    if shadow is None:
        out.append(warn("oem-locked", "/etc/shadow is unreadable: cannot tell whether the account is locked"))
        return out
    for line in shadow.splitlines():
        if line.startswith("oem:"):
            field = line.split(":")[1] if line.count(":") >= 1 else ""
            if field.startswith(("!", "*")):
                out.append(ok("oem-locked", "the temporary account is locked"))
            else:
                out.append(fail("oem-locked", "the temporary 'oem' account is NOT locked (password field %s)"
                                % ("empty" if field == "" else "is a usable hash")))
            break
    else:
        out.append(warn("oem-locked", "no oem entry in /etc/shadow"))
    return out


HOOK_LEFTOVERS: Tuple[str, ...] = (
    "usr/sbin/policy-rc.d",
    "sbin/start-stop-daemon.REAL",
    "usr/sbin/start-stop-daemon.REAL",
    "sbin/initctl.REAL",
    "usr/lib/ubiquity/target-config/50lindos-install",
    "var/lib/lindos/installer-holds",
    "var/lib/lindos/installer-apt.conf",
    "etc/apt/preferences.d/00lindos-installer.pref",
    "var/cache/lindos-installer",
    "tmp/lindos-oem-debs",
    "etc/systemd/system/ubiquity.service.d/10-lindos.conf",   # the installer session's GTK_THEME drop-in (79-installer-flow.sh)
)

# What Ubiquity's own user setup (user-setup-apply: 'mount -t proc' and adduser in a bare chroot, before /run is bound)
# leaves in the new system's /run.  Harmless - /run is a tmpfs at boot - and tidied by finalize.sh; anything ELSE in it is
# not Ubiquity's and points at something that wrote into the target outside its mounts.
RUN_UPSTREAM_LEFTOVERS: Tuple[str, ...] = ("adduser", "mount")


def check_leftovers(tree: Tree, pkgs: Mapping[str, Pkg]) -> List[Finding]:
    out: List[Finding] = []
    left = [rel for rel in HOOK_LEFTOVERS if tree.lexists(rel)]
    if left:
        out.append(fail("leftovers", "the installer left behind: " + ", ".join("/" + r for r in left)))
    else:
        out.append(ok("leftovers", "no policy-rc.d, start-stop-daemon diversion, hold file, pin or hook copy is left"))
    broken, held = audit_packages(pkgs)
    if held:
        out.append(fail("dpkg-holds", "packages still on hold (the hook must release them): " + ", ".join(held[:20])))
    else:
        out.append(ok("dpkg-holds", "no package is on hold"))
    if broken:
        out.append(fail("dpkg-audit", "dpkg is not clean: %d package(s) half-installed/unpacked/half-configured: %s"
                        % (len(broken), ", ".join(broken[:20]))))
    else:
        out.append(ok("dpkg-audit", "no package is half-installed, unpacked, half-configured or awaiting triggers (%d known)" % len(pkgs)))
    # mount points must be empty directories: a leftover means something wrote into the target while it was not mounted
    for rel in ("proc", "sys", "run", "cdrom"):
        names = tree.listdir(rel)
        if rel == "run":
            known = [n for n in names if n in RUN_UPSTREAM_LEFTOVERS]
            names = [n for n in names if n not in RUN_UPSTREAM_LEFTOVERS]
            if known and not names:
                out.append(info("mountpoint-run", "/run holds %s: Ubiquity's own user setup runs mount and adduser in a bare chroot "
                                                  "(not the installer hook, whose commands see a private tmpfs there); harmless, "
                                                  "/run is a tmpfs at boot" % ", ".join(known)))
                continue
        if names:
            out.append(warn("mountpoint-" + rel, "/%s is not empty (%d entries, e.g. %s): something wrote into the target "
                                                 "outside its mounts" % (rel, len(names), ", ".join(names[:5]))))
    dev = [n for n in tree.listdir("dev") if n not in ("pts", "shm", "mqueue", "console", "null")]
    if dev:
        out.append(warn("mountpoint-dev", "/dev has %d unexpected entries, e.g. %s" % (len(dev), ", ".join(dev[:5]))))
    return out


def check_apt_sources(tree: Tree) -> List[Finding]:
    """An active 'deb cdrom:' line makes apt ask for the disc; Ubiquity comments it at the end of the install."""
    active: List[str] = []
    for rel in ["etc/apt/sources.list"] + ["etc/apt/sources.list.d/" + n for n in tree.listdir("etc/apt/sources.list.d")]:
        text = tree.read_text(rel)
        for line in (text or "").splitlines():
            if re.match(r"^\s*deb(-src)?\s+(\[[^\]]*\]\s+)?cdrom:", line):
                active.append("%s: %s" % (rel, line.strip()[:80]))
    if active:
        return [warn("apt-cdrom", "an active cdrom: apt source is left: " + "; ".join(active))]
    return [ok("apt-cdrom", "no active cdrom: apt source")]


def check_browser(tree: Tree, pkgs: Mapping[str, Pkg], state: Optional[dict]) -> List[Finding]:
    """Chrome is installed exactly when the browser step says done; the marker never lies."""
    chrome = is_installed(pkgs, "google-chrome-stable")
    marker = tree.lexists(BROWSER_MARKER)
    steps = (state or {}).get("steps")
    if not isinstance(steps, dict):
        steps = {}
    status = (steps.get("browser") or {}).get("status", "") if isinstance(steps.get("browser"), dict) else ""
    out: List[Finding] = []
    if chrome:
        out.append(ok("chrome-installed", "google-chrome-stable %s is installed" % pkgs["google-chrome-stable"].version))
        if not tree.lexists("opt/google/chrome/chrome"):
            out.append(fail("chrome-binary", "google-chrome-stable is installed but /opt/google/chrome/chrome is missing"))
        if state is not None and status != "done":
            out.append(fail("chrome-vs-state", "Chrome is installed but the browser step is %r, not 'done'" % (status or "missing")))
        if not marker:
            out.append(warn("browser-marker", "Chrome is installed but /%s is missing: the silent retry would run once more" % BROWSER_MARKER))
    else:
        out.append(info("chrome-installed", "google-chrome-stable is not installed (browser step: %s)" % (status or "unknown")))
        if status == "done":
            out.append(fail("chrome-vs-state", "the browser step says 'done' but google-chrome-stable is not installed"))
    if marker and not chrome and status != "skipped":
        out.append(fail("browser-marker", "/%s exists although Chrome is not installed and the step is %r (not 'skipped'): "
                                          "the first-boot retry would never run" % (BROWSER_MARKER, status or "missing")))
    elif not marker and not chrome and status in ("pending", "failed"):
        out.append(ok("browser-marker", "no marker and Chrome is %s: the silent first-boot retry will pick it up" % status))
    elif marker and chrome:
        out.append(ok("browser-marker", "marker present and Chrome installed"))
    driver_marker = tree.lexists(DRIVER_MARKER)
    dstatus = (steps.get("drivers") or {}).get("status", "") if isinstance(steps.get("drivers"), dict) else ""
    if driver_marker and dstatus in ("pending", "failed"):
        out.append(fail("driver-marker", "/%s exists but the drivers step is %s: the silent retry would never run" % (DRIVER_MARKER, dstatus)))
    elif not driver_marker and dstatus in ("done", "skipped"):
        out.append(warn("driver-marker", "the drivers step is %s but /%s is missing" % (dstatus, DRIVER_MARKER)))
    return out


# ---- the answers the image bakes (lindos.seed) --------------------------------------------------------
def parse_debconf_db(text: str) -> Dict[str, Dict[str, str]]:
    """``/var/cache/debconf/config.dat`` (debconf's flat 822-style database) -> {question: {field: value}}.

    Stanzas are separated by blank lines and start with ``Name:``; a line that starts with a blank continues the
    previous field (multi-line values).
    """
    db: Dict[str, Dict[str, str]] = {}
    cur: Optional[Dict[str, str]] = None
    field = ""
    for raw in text.splitlines():
        if not raw.strip():
            cur, field = None, ""
            continue
        if raw[0] in " \t":
            if cur is not None and field:
                cur[field] = (cur[field] + "\n" + raw.strip()).strip()
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Name":
            cur = db.setdefault(value, {})
            field = ""
        elif cur is not None:
            cur[key] = value
            field = key
    return db


def check_seed_effects(tree: Tree, pkgs: Mapping[str, Pkg], *, expect_i386: Optional[bool]) -> List[Finding]:
    """Did the answers baked into the medium (lindos.seed) and finalize.sh's clean-up do their job on the new system?

    * ``user-setup/allow-password-empty`` is baked ``true`` so that the OEM installer's temporary account page
      needs no password; finalize.sh (fin_reset_seed) sets it back to ``false`` in the NEW system's debconf
      database, because oem-config reads that database and must not accept an empty password for the REAL
      account.  ``true`` on the installed disk therefore fails.  (The CI preseed supplies a password, so the
      account itself proves nothing here - the database does.)
    * ``apt-setup/multiarch`` keeps i386 for Wine and Steam.  *expect_i386*: True when the image is built with
      ENABLE_I386=1 (i386 must be in /var/lib/dpkg/arch), False when it is not, None when unknown.  Installed
      i386 packages without the architecture in dpkg's list fail whatever is expected (dpkg would be inconsistent).
    """
    out: List[Finding] = []
    text = tree.read_text(DEBCONF_CONFIG, limit=64 << 20)
    if text is None:
        out.append(warn("seed-password-empty", "/%s is unreadable: cannot tell whether user-setup/allow-password-empty was "
                                               "reset (the first-boot wizard may accept an empty password)" % DEBCONF_CONFIG))
    else:
        question = parse_debconf_db(text).get("user-setup/allow-password-empty")
        value = (question or {}).get("Value", "").strip().lower()
        if question is None:
            out.append(info("seed-password-empty", "user-setup/allow-password-empty is not in the new debconf database (its "
                                                   "default is false: the real account needs a password)"))
        elif value == "true":
            out.append(fail("seed-password-empty", "user-setup/allow-password-empty is still 'true' in the installed system's "
                                                   "debconf database: finalize.sh's reset did not take effect, so the first-boot "
                                                   "wizard may create the real account with an empty password"))
        else:
            out.append(ok("seed-password-empty", "user-setup/allow-password-empty is %r in the new system" % (value or "unset")))
    archs = (tree.read_text(DPKG_ARCH) or "").split()
    i386_pkgs = sorted(n for n, p in pkgs.items() if n.endswith(":i386") and p.state == "installed")
    if "i386" in archs:
        note = "%d i386 package(s) installed" % len(i386_pkgs) if i386_pkgs else "no i386 package is installed yet"
        out.append(ok("seed-multiarch", "dpkg still has the i386 architecture (%s)" % note))
    elif i386_pkgs:
        out.append(fail("seed-multiarch", "%d i386 package(s) are installed (e.g. %s) but /%s does not list i386: the installer's "
                                          "apt setup removed the architecture (apt-setup/multiarch did not reach it)"
                        % (len(i386_pkgs), ", ".join(i386_pkgs[:3]), DPKG_ARCH)))
    elif expect_i386 is True:
        out.append(fail("seed-multiarch", "the image enables i386 (ENABLE_I386=1) but /%s %s: Wine and Steam cannot install "
                                          "their 32-bit half" % (DPKG_ARCH, ("lists " + ", ".join(archs)) if archs else "is empty or missing")))
    elif expect_i386 is False:
        out.append(ok("seed-multiarch", "the image does not enable i386 and dpkg does not list it"))
    else:
        out.append(info("seed-multiarch", "i386 is not enabled in dpkg (whether the image should have it is unknown)"))
    return out


# ---- 'done' must mean installed -----------------------------------------------------------------------
class Evidence(NamedTuple):
    """What an item of extras.json leaves on the disk: ANY of these packages, files or Flatpak apps."""

    pkgs: Tuple[str, ...] = ()
    files: Tuple[str, ...] = ()
    flatpaks: Tuple[str, ...] = ()


# install-compat.sh: WineHQ staging, else wine-staging, else Ubuntu's own 'wine'; umu is the pinned zipapp in
# /usr/local/bin (or the deb).  install-gaming.sh: Valve's steam-launcher, else Ubuntu's steam-installer; lutris from
# apt, else the Flatpak.  Keep in step with those scripts (the tests read them).
ITEM_EVIDENCE: Dict[Tuple[str, str], Evidence] = {
    ("compat", "wine"): Evidence(pkgs=("winehq-staging", "wine-staging", "wine")),
    ("compat", "winetricks"): Evidence(pkgs=("winetricks",)),
    ("compat", "umu"): Evidence(pkgs=("umu-launcher", "python3-umu-launcher"),
                                files=("usr/local/bin/umu-run", "usr/bin/umu-run")),
    ("gaming", "steam"): Evidence(pkgs=("steam-launcher", "steam-installer")),
    ("gaming", "lutris"): Evidence(pkgs=("lutris",), flatpaks=("net.lutris.Lutris",)),
}

_TRIVIAL_DETAIL = re.compile(r"nothing to install|no extra packages defined|no Flatpak apps defined", re.I)
_NOT_IN_ARCHIVES = re.compile(r"not in the archives:\s*(.*)$")
_UPGRADED = re.compile(r"(\d+) packages? upgraded")


def load_extras(path: object) -> Optional[dict]:
    """The installer's extras.json (the repository copy: the installed system drops lindos-installer), or None."""
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _names(extras: Mapping, key: str) -> List[str]:
    """extras['key'] as a list of names ('drivers.firmware' for the nested list)."""
    node: object = extras
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return [str(x).strip() for x in node if isinstance(x, str) and x.strip()] if isinstance(node, list) else []


def _evidence_words(ev: Evidence) -> str:
    return " or ".join(ev.pkgs + ev.files + tuple("flatpak " + a for a in ev.flatpaks))


def _has_evidence(tree: Tree, pkgs: Mapping[str, Pkg], ev: Evidence) -> bool:
    return (any(is_installed(pkgs, p) for p in ev.pkgs) or any(tree.lexists(f) for f in ev.files)
            or any(tree.is_dir("%s/%s" % (FLATPAK_APPS, a)) for a in ev.flatpaks))


def check_steps_vs_disk(tree: Tree, pkgs: Mapping[str, Pkg], state: Optional[dict], extras: Optional[Mapping], *,
                        expect_online: Optional[bool] = None) -> List[Finding]:
    """A step that records ``done`` must have left what it promises on the disk (the state file is the hook's own claim).

    Steps that are pending/failed/skipped promise nothing and are not judged here.  Findings are named
    ``disk-<step>``; a mismatch fails.  *extras* is the repository copy of extras.json; without it the
    package lists cannot be compared and that is said (never silently skipped).
    """
    steps = (state or {}).get("steps")
    if not isinstance(steps, dict):
        return []
    if extras is None:
        return [info("disk-steps", "extras.json is not available to the test: what the steps installed is not cross-checked")]

    def entry(step: str) -> Tuple[str, str]:
        e = steps.get(step)
        return ((e.get("status") or "", str(e.get("detail", ""))) if isinstance(e, dict) else ("", ""))

    out: List[Finding] = []

    # ---- updates: an 'upgrade' line in dpkg.log when the hook says it upgraded packages
    status, detail = entry("updates")
    m = _UPGRADED.search(detail)
    if status == "done" and m and int(m.group(1)) > 0:
        log = tree.read_text(DPKG_LOG, limit=64 << 20)
        if log is None:
            out.append(warn("disk-updates", "the updates step says %r but /%s is missing: nothing to verify it against" % (detail, DPKG_LOG)))
        else:
            n = sum(1 for ln in log.splitlines() if re.match(r"^\S+ \S+ upgrade ", ln))
            if n:
                out.append(ok("disk-updates", "/%s records %d upgrade(s) (the step says: %s)" % (DPKG_LOG, n, detail)))
            else:
                out.append(fail("disk-updates", "the updates step says %r but /%s has no 'upgrade' line: nothing was upgraded"
                                % (detail, DPKG_LOG)))

    # ---- drivers: at least one firmware package of the list is installed
    status, detail = entry("drivers")
    firmware = _names(extras, "drivers.firmware")
    if status == "done" and firmware and "firmware" in detail:
        have = [p for p in firmware if is_installed(pkgs, p)]
        gone = [p for p in firmware if p not in have]
        if not have:
            out.append(fail("disk-drivers", "the drivers step says done but none of %s is installed" % ", ".join(firmware)))
        elif gone:
            out.append(info("disk-drivers", "firmware installed: %s; not installed: %s (the hook leaves out what no archive carries)"
                            % (", ".join(have), ", ".join(gone))))
        else:
            out.append(ok("disk-drivers", "firmware installed: %s" % ", ".join(have)))

    # ---- mode_extras: every apt package of extras.json, except those the hook says the archives lack
    status, detail = entry("mode_extras")
    want = _names(extras, "apt")
    if status == "done" and want:
        if _TRIVIAL_DETAIL.search(detail):
            out.append(fail("disk-mode_extras", "the step says %r but extras.json lists %d apt package(s): the hook lost its list "
                                                "(li_extras_load swallows a load error)" % (detail, len(want))))
        elif detail.lower().startswith("none of the extra apps"):
            level = fail if expect_online is True else warn
            out.append(level("disk-mode_extras", "the step says %r although %d packages were asked for" % (detail, len(want))))
        else:
            m2 = _NOT_IN_ARCHIVES.search(detail)
            excused = set(m2.group(1).split()) if m2 else set()
            missing = [p for p in want if p not in excused and not is_installed(pkgs, p)]
            if missing:
                out.append(fail("disk-mode_extras", "the step says %r but %d package(s) are not installed: %s"
                                % (detail, len(missing), ", ".join(missing[:12]))))
            else:
                tail = " (not in the archives: %s)" % ", ".join(sorted(excused)) if excused else ""
                out.append(ok("disk-mode_extras", "%d extra apt package(s) are installed%s"
                              % (len([p for p in want if p not in excused]), tail)))

    # ---- compat / gaming: the evidence of every item
    for step in ("compat", "gaming"):
        status, detail = entry(step)
        items = _names(extras, step)
        if status != "done" or not items:
            continue
        if _TRIVIAL_DETAIL.search(detail):
            out.append(fail("disk-" + step, "the step says %r but extras.json lists %s: the hook lost its list" % (detail, ", ".join(items))))
            continue
        missing, unknown = [], []
        for item in items:
            ev = ITEM_EVIDENCE.get((step, item))
            if ev is None:
                unknown.append(item)
            elif not _has_evidence(tree, pkgs, ev):
                missing.append("%s (needs %s)" % (item, _evidence_words(ev)))
        if missing:
            out.append(fail("disk-" + step, "the step says done but not installed: " + "; ".join(missing)))
        else:
            out.append(ok("disk-" + step, "%s: %s" % (step, ", ".join(i for i in items if i not in unknown) or "-")))
        if unknown:
            out.append(info("disk-%s-items" % step, "no disk check is known for: " + ", ".join(unknown)))

    # ---- flatpaks: the app directory of every id
    status, detail = entry("flatpaks")
    ids = _names(extras, "flatpaks")
    if status == "done" and ids:
        if _TRIVIAL_DETAIL.search(detail):
            out.append(fail("disk-flatpaks", "the step says %r but extras.json lists %d Flatpak app(s): the hook lost its list"
                            % (detail, len(ids))))
        else:
            missing = [a for a in ids if not tree.is_dir("%s/%s" % (FLATPAK_APPS, a))]
            if missing:
                out.append(fail("disk-flatpaks", "the step says done but /%s has no directory for: %s" % (FLATPAK_APPS, ", ".join(missing))))
            else:
                out.append(ok("disk-flatpaks", "%d Flatpak app(s) are installed" % len(ids)))
    return out


class Kernel(NamedTuple):
    version: str
    vmlinuz: str    # path relative to the system root
    initrd: str


def list_kernels(tree: Tree) -> List[Kernel]:
    """Every /boot/vmlinuz-X that has a matching /boot/initrd.img-X, oldest first (natural version order)."""
    names = tree.listdir("boot")
    out: List[Kernel] = []
    for n in names:
        if n.startswith("vmlinuz-") and not n.endswith((".old", ".dpkg-bak")):
            ver = n[len("vmlinuz-"):]
            if "initrd.img-" + ver in names:
                out.append(Kernel(ver, "boot/" + n, "boot/initrd.img-" + ver))

    def key(k: Kernel) -> List:
        return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", k.version)]

    return sorted(out, key=key)


def pick_kernel(kernels: Sequence[Kernel], prefer_suffix: Optional[str] = None) -> Optional[Kernel]:
    """The kernel the direct-kernel boot uses: the newest one, preferably with *prefer_suffix* (e.g. -lindos)."""
    if not kernels:
        return None
    if prefer_suffix:
        wanted = [k for k in kernels if k.version.endswith(prefer_suffix)]
        if wanted:
            return wanted[-1]
    return kernels[-1]


def check_bootloader(tree: Tree, *, mbr: Optional[bytes], firmware: str = "bios",
                     esp: Optional[Tree] = None) -> List[Finding]:
    out: List[Finding] = []
    if firmware == "uefi":
        efis = []
        if esp is not None:
            for vendor in esp.listdir("EFI"):
                for n in esp.listdir("EFI/" + vendor):
                    if n.lower().endswith(".efi"):
                        efis.append("EFI/%s/%s" % (vendor, n))
        if efis:
            out.append(ok("bootloader", "the EFI system partition has %s" % ", ".join(efis[:4])))
        else:
            out.append(fail("bootloader", "no .efi boot loader on the EFI system partition"))
    else:
        if mbr is None or len(mbr) < 512:
            out.append(fail("bootloader", "the first sector of the disk could not be read"))
        elif mbr[510:512] != b"\x55\xaa":
            out.append(fail("bootloader", "the first sector has no boot signature: grub-install did not write an MBR"))
        elif b"GRUB" not in mbr[:446]:
            out.append(fail("bootloader", "the MBR boot code is not GRUB's (no 'GRUB' string in the first 446 bytes)"))
        else:
            out.append(ok("bootloader", "the MBR carries GRUB's boot code"))
    cfg = tree.read_text("boot/grub/grub.cfg")
    if cfg is None:
        out.append(fail("grub-cfg", "/boot/grub/grub.cfg does not exist"))
    elif not re.search(r"^\s*menuentry\s", cfg, re.M) or not re.search(r"^\s*linux(efi)?\s+\S+.*root=", cfg, re.M):
        out.append(fail("grub-cfg", "/boot/grub/grub.cfg has no menuentry with a linux line and root="))
    else:
        out.append(ok("grub-cfg", "/boot/grub/grub.cfg has %d menuentries" % len(re.findall(r"^\s*menuentry\s", cfg, re.M))))
    kernels = list_kernels(tree)
    if kernels:
        out.append(ok("kernel", "kernel + initrd on the disk: " + ", ".join(k.version for k in kernels)))
    else:
        out.append(fail("kernel", "no /boot/vmlinuz-X with a matching /boot/initrd.img-X"))
    return out


def check_branding(tree: Tree, pkgs: Mapping[str, Pkg]) -> List[Finding]:
    out: List[Finding] = []
    osr = tree.read_text("etc/os-release") or ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)', osr, re.M)
    pretty = m.group(1) if m else ""
    if "Lindos" in pretty:
        out.append(ok("branding", "os-release: %s" % pretty))
    else:
        out.append(warn("branding", "os-release PRETTY_NAME is %r, not Lindos" % pretty))
    # lindos-installer is medium-only: Ubiquity removes it through filesystem.manifest-remove
    if is_installed(pkgs, "lindos-installer"):
        out.append(warn("installer-package-removed", "lindos-installer is still installed on the new system "
                                                     "(filesystem.manifest-remove did not take it out)"))
    else:
        out.append(ok("installer-package-removed", "lindos-installer is not on the installed system"))
    return out


# ---- the look of the first-boot account wizard -----------------------------------------------------
WIZARD_SKIN = "usr/share/themes/Lindos-Setup/gtk-3.0/gtk.css"
WIZARD_DROPIN = "etc/systemd/system/oem-config.service.d/10-lindos.conf"
WIZARD_TITLE = "Lindos Setup"


def check_wizard_look(tree: Tree) -> List[Finding]:
    """The first boot is Ubiquity's oem-config: its GTK program inherits the environment of oem-config.service, so a
    drop-in with GTK_THEME=Lindos-Setup (written by finalize.sh) gives it the Lindos skin, and the debconf answer
    ubiquity/custom_title_text names its window.  The drop-in must also be removable again by the wizard itself
    (oem-config/late_command).  Whether the skin really LOADS and looks right is judged from the first-boot screenshot
    and the observer's look at the GTK program's environment (install_test.py)."""
    out: List[Finding] = []
    if tree.is_file(WIZARD_SKIN):
        out.append(ok("wizard-skin", "/%s is on the disk" % WIZARD_SKIN))
    else:
        out.append(fail("wizard-skin", "/%s is missing: the account wizard would keep Ubiquity's light default (79-installer-flow.sh "
                                       "installs it when 78-installer-brand.sh did not)" % WIZARD_SKIN))
    dropin = tree.read_text(WIZARD_DROPIN)
    if dropin is None:
        out.append(fail("wizard-theme-dropin", "/%s does not exist: finalize.sh did not give the account wizard the Lindos skin" % WIZARD_DROPIN))
    elif re.search(r"^Environment=GTK_THEME=Lindos-Setup\s*$", dropin, re.M):
        out.append(ok("wizard-theme-dropin", "oem-config.service gets GTK_THEME=Lindos-Setup"))
    else:
        out.append(fail("wizard-theme-dropin", "/%s does not set GTK_THEME=Lindos-Setup" % WIZARD_DROPIN))
    text = tree.read_text(DEBCONF_CONFIG, limit=64 << 20)
    if text is None:
        out.append(warn("wizard-title", "/%s is unreadable: the wizard's window title cannot be checked" % DEBCONF_CONFIG))
        return out
    db = parse_debconf_db(text)
    title = db.get("ubiquity/custom_title_text", {}).get("Value", "").strip()
    if title == WIZARD_TITLE:
        out.append(ok("wizard-title", "the wizard's window is called %r" % title))
    else:
        out.append(warn("wizard-title", "ubiquity/custom_title_text is %r, not %r: the window keeps Ubiquity's 'System Configuration'"
                        % (title, WIZARD_TITLE)))
    late = db.get("oem-config/late_command", {}).get("Value", "")
    if "oem-config.service.d" in late:
        out.append(ok("wizard-cleanup", "the wizard removes its theme drop-in itself (oem-config/late_command)"))
    else:
        out.append(warn("wizard-cleanup", "oem-config/late_command does not remove the theme drop-in: it stays on the installed system"))
    return out


# error signatures in the installer's own logs (Ubiquity copies its syslog to /var/log/installer)
LOG_SIGNATURES: Tuple[Tuple[str, str, str], ...] = (
    (FAIL, r"target is busy", "the target could not be unmounted: a mount was left behind"),
    (FAIL, r"InstallStepError", "an Ubiquity install step aborted"),
    (FAIL, r"E: Syntax error /usr/bin/apt-config", "a package's maintainer script ran apt-config with the binary as its configuration file: "
                                                   "APT_CONFIG leaked into the target (first real install: Chrome's postinst)"),
    (WARN, r"pkgProblemResolver::Resolve generated breaks", "apt refused an install because of held packages"),
    (WARN, r"Traceback \(most recent call last\)", "a Python traceback in the installer's log"),
    (WARN, r"dpkg: error processing", "dpkg failed while installing a package"),
    (WARN, r"E: Sub-process /usr/bin/dpkg returned an error code", "apt/dpkg reported an error code"),
    (WARN, r"cannot enter the target system", "the hook could not enter /target"),
    (WARN, r"name resolution inside the target does not work", "DNS did not work inside the target"),
)


def scan_installer_logs(texts: Mapping[str, str]) -> List[Finding]:
    """Signatures of known trouble in the logs the installer wrote (name -> text)."""
    out: List[Finding] = []
    for level, pattern, meaning in LOG_SIGNATURES:
        rx = re.compile(pattern)
        hits = [(name, ln.strip()[:160]) for name, text in texts.items() for ln in text.splitlines() if rx.search(ln)]
        if hits:
            out.append(Finding("log-" + re.sub(r"\W+", "-", pattern).strip("-").lower()[:40], level,
                               "%s (%d line(s); first: %s: %s)" % (meaning, len(hits), hits[0][0], hits[0][1])))
    if not out:
        out.append(ok("log-signatures", "no known error signature in the installer logs"))
    return out


LOG_FILES_FOR_SCAN: Tuple[str, ...] = ("var/log/installer/syslog", "var/log/lindos/installer.log")


def run_all_checks(tree: Tree, *, expect_online: Optional[bool], mbr: Optional[bytes] = None,
                   firmware: str = "bios", esp: Optional[Tree] = None, extras: Optional[Mapping] = None,
                   expect_i386: Optional[bool] = None, strict_offline: bool = False) -> List[Finding]:
    """Every check of the installed disk, in the order a maintainer reads them.

    *extras*: the repository copy of extras.json (see :func:`load_extras`) - without it a 'done' step's packages are
    not cross-checked.  *expect_i386*: the image is built with ENABLE_I386=1.  *strict_offline*: the guest had no
    network device (see :func:`check_install_state`).
    """
    findings: List[Finding] = []
    state_findings, state = check_install_state(tree, expect_online=expect_online, strict_offline=strict_offline)
    findings.extend(state_findings)
    status_text = tree.read_text("var/lib/dpkg/status", limit=64 << 20)
    if status_text is None:
        findings.append(fail("dpkg-status", "/var/lib/dpkg/status is missing: this is not an installed system"))
        pkgs: Dict[str, Pkg] = {}
    else:
        pkgs = parse_dpkg_status(status_text)
    findings.extend(check_installer_log(tree, state))
    findings.extend(check_oem_armed(tree, pkgs))
    findings.extend(check_lightdm(tree))
    findings.extend(check_temp_account(tree))
    findings.extend(check_leftovers(tree, pkgs))
    findings.extend(check_apt_sources(tree))
    findings.extend(check_browser(tree, pkgs, state))
    findings.extend(check_steps_vs_disk(tree, pkgs, state, extras, expect_online=expect_online))
    findings.extend(check_seed_effects(tree, pkgs, expect_i386=expect_i386))
    findings.extend(check_bootloader(tree, mbr=mbr, firmware=firmware, esp=esp))
    findings.extend(check_branding(tree, pkgs))
    findings.extend(check_wizard_look(tree))
    texts = {rel: t for rel in LOG_FILES_FOR_SCAN if (t := tree.read_text(rel, limit=16 << 20)) is not None}
    findings.extend(scan_installer_logs(texts))
    return findings


def format_findings(findings: Iterable[Finding]) -> str:
    return "\n".join(f.line() for f in findings)


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret (the throw-away CI password) before a log is copied to an artifact."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "<redacted>")
    return text
