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


def check_install_state(tree: Tree, *, expect_online: Optional[bool]) -> Tuple[List[Finding], Optional[dict]]:
    """Schema, step ids/statuses, and consistency with the network the runner really had.

    *expect_online*: True when the CI runner has internet (Chrome must then be installed), False for an
    offline run (every network step may be pending), None when unknown (only recorded).
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
        if status == "failed":
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
    elif expect_online is False and online is True:
        out.append(warn("state-online", "the runner had no internet but the hook recorded online=true"))
    return out, data


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
)


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


# error signatures in the installer's own logs (Ubiquity copies its syslog to /var/log/installer)
LOG_SIGNATURES: Tuple[Tuple[str, str, str], ...] = (
    (FAIL, r"target is busy", "the target could not be unmounted: a mount was left behind"),
    (FAIL, r"InstallStepError", "an Ubiquity install step aborted"),
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
                   firmware: str = "bios", esp: Optional[Tree] = None) -> List[Finding]:
    """Every check of the installed disk, in the order a maintainer reads them."""
    findings: List[Finding] = []
    state_findings, state = check_install_state(tree, expect_online=expect_online)
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
    findings.extend(check_bootloader(tree, mbr=mbr, firmware=firmware, esp=esp))
    findings.extend(check_branding(tree, pkgs))
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
