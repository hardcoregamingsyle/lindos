"""lindos.updatestate - the root-side half of the Lindos update engine (SPEC-UPDATE.md §39-§41).

Nothing on the base system refreshes apt once Lindos has switched the stock apt-daily timers off,
so Lindos owns this: a root timer (``lindos-update-refresh``) runs ``apt-get update`` and
``apt-get -s dist-upgrade`` and writes what it found to the world-readable
``/var/lib/lindos/update-state.json``. The Settings page, the notifier and ``lindos-update
status`` read that file - no password, no apt in the user session. **It never installs
anything**: applying is the helper's ``apt-full-upgrade`` / ``system-upgrade`` (SPEC-UPDATE.md §36.4).

Everything here is pure functions over text plus a tiny IO layer with an injectable *runner* (a
callable ``(argv, timeout) -> (returncode, combined_output)``), so the parsers, the plan digest,
the state writer, the reboot-required logic, the history parser and the safe-cleanup planner are
tested on any OS with no apt. Every filesystem path goes through :func:`lindos.paths.resolve`
(``LINDOS_ROOT`` aware).

The pieces that parse ``apt`` output were written against apt's documented, stable output formats
(``Inst``/``Remv``/``Purg`` simulation lines, ``--print-uris``, ``history.log``, ``dpkg.log``) and
are defensive: an unrecognised line is ignored, never an error - but they have not yet been run
against a real apt (see docs/UPDATES.md, "What is unverified").

Command line (used by the systemd units and the apt hook)::

    python3 -m lindos.updatestate refresh [--offline] [--no-update]
    python3 -m lindos.updatestate reboot-hook
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import paths
from . import update as _update

STATE_PATH = "/var/lib/lindos/update-state.json"
IN_PROGRESS_PATH = "/var/lib/lindos/update-in-progress"
REBOOT_REQUIRED_PATH = "/run/reboot-required"
REBOOT_REQUIRED_PKGS_PATH = "/run/reboot-required.pkgs"
DPKG_LOG_PATH = "/var/log/dpkg.log"
APT_HISTORY_LOG = "/var/log/apt/history.log"
STATE_SCHEMA = 1
#: the state is called stale (and the UI says so) when it is older than this
STALE_AFTER_SECONDS = 3 * 24 * 3600
#: never remove more than this many packages in one cleanup
CLEANUP_MAX_PACKAGES = 30

CATEGORY_ORDER = ("lindos", "security", "drivers-kernel", "apps", "other")
CATEGORY_TITLES = {
    "lindos": "Lindos",
    "security": "Security",
    "drivers-kernel": "Drivers & kernel",
    "apps": "Apps",
    "other": "Other",
}

#: the neutral labels an origin may be shown as - never the name of the base distribution
ORIGIN_LABEL_LINDOS = "Lindos"
ORIGIN_LABEL_SECURITY = "Security"
ORIGIN_LABEL_APPS = "Apps"
ORIGIN_LABEL_BASE = "Lindos base system"
ORIGIN_LABEL_OTHER = "Other sources"
ORIGIN_LABELS = (ORIGIN_LABEL_LINDOS, ORIGIN_LABEL_SECURITY, ORIGIN_LABEL_APPS, ORIGIN_LABEL_BASE,
                 ORIGIN_LABEL_OTHER)

Runner = Callable[[Sequence[str], float], Tuple[int, str]]


# =================================================================================================
# apt output parsers
# =================================================================================================
#: ``Inst libc6 [2.39-0ubuntu8.3] (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates [amd64]) []``; a new
#: package has no ``[old]`` part.  Greedy up to the LAST ``)`` so an origin with parentheses survives.
_INST_RE = re.compile(r"^Inst\s+(?P<name>\S+)(?:\s+\[(?P<old>[^\]]*)\])?\s+\((?P<inner>.*)\)(?:\s*\[.*\])?\s*$")
_REMV_RE = re.compile(r"^(?P<verb>Remv|Purg)\s+(?P<name>\S+)(?:\s+\[(?P<ver>[^\]]*)\])?")
_TRAILING_ARCH_RE = re.compile(r"\s*\[(?P<arch>[^\]]*)\]\s*$")
_URI_LINE_RE = re.compile(r"^'[^']*'\s+(?P<file>\S+)\s+(?P<size>\d+)\b")
_KEPT_BACK_HEADER = "The following packages have been kept back:"


def _split_arch(name: str) -> Tuple[str, str]:
    """``libfoo:i386`` -> ``("libfoo", "i386")``; ``libfoo`` -> ``("libfoo", "")``."""
    base, _, arch = name.partition(":")
    return base, arch


def origin_parts(origin: str) -> Tuple[str, str, str]:
    """``"Ubuntu:24.04/noble-updates"`` -> ``(label, version, archive)`` (missing parts are "")."""
    label, _, rest = origin.partition(":")
    if not rest:
        label, rest = "", origin
    version, _, archive = rest.partition("/")
    if not archive:
        archive, version = version, ""
    return label.strip(), version.strip(), archive.strip()


def _is_security_origin(origin: str) -> bool:
    label, _version, archive = origin_parts(origin)
    return archive.lower().endswith("-security") or "security" in label.lower()


_APPS_ORIGIN_HINTS = ("google", "chrome", "microsoft", "wine", "steam", "valve", "flathub", "mozilla",
                      "heroic", "brave", "lutris", "prism", "vivaldi", "opera", "slack", "zoom")


def origin_label(origins: Sequence[str]) -> str:
    """The neutral label for a package's origins: ``Lindos`` > ``Security`` > ``Apps`` >
    ``Lindos base system`` > ``Other sources``. The base distribution's own name is never shown."""
    if not origins:
        return ORIGIN_LABEL_OTHER
    lowered = [o.lower() for o in origins]
    if any(o.startswith("lindos") or "lindos" in origin_parts(o)[0].lower() for o in origins):
        return ORIGIN_LABEL_LINDOS
    if any(_is_security_origin(o) for o in origins):
        return ORIGIN_LABEL_SECURITY
    if any(hint in o for o in lowered for hint in _APPS_ORIGIN_HINTS):
        return ORIGIN_LABEL_APPS
    if any(("ubuntu" in o) or ("mint" in o) or ("debian" in o) for o in lowered):
        return ORIGIN_LABEL_BASE
    return ORIGIN_LABEL_OTHER


_KERNEL_DRIVER_RE = re.compile(
    r"^(?:linux-(?:image|headers|modules|modules-extra|tools|buildinfo|hwe|generic|lowlatency|virtual|"
    r"signed|firmware|restricted|oem|cloud-tools)\b|linux-[a-z]+-\d|nvidia-|libnvidia-|"
    r"xserver-xorg-video-|intel-microcode$|amd64-microcode$|bcmwl-|broadcom-sta-|mesa-|libgl1-mesa|"
    r"libegl-mesa|libglx-mesa|libgbm1$|libdrm)")
_APP_NAMES_RE = re.compile(
    r"^(?:firefox|thunderbird|google-chrome|microsoft-edge|steam-|steam$|lutris|heroic|winehq|wine|"
    r"libwine|libreoffice|flatpak$)")


def categorize(package: str, origins: Sequence[str]) -> str:
    """The update category of one package: ``lindos`` | ``security`` | ``drivers-kernel`` | ``apps`` |
    ``other``. Kernel/driver packages win over the Lindos origin (a kernel is a kernel), then Lindos
    packages, then a security pocket, then apps."""
    if _KERNEL_DRIVER_RE.match(package):
        return "drivers-kernel"
    if package.startswith("lindos-") or origin_label(origins) == ORIGIN_LABEL_LINDOS:
        return "lindos"
    if any(_is_security_origin(o) for o in origins):
        return "security"
    if origin_label(origins) == ORIGIN_LABEL_APPS or _APP_NAMES_RE.match(package):
        return "apps"
    return "other"


@dataclass
class PlanItem:
    """One package ``apt-get -s dist-upgrade`` would install (a new install has no ``installed``)."""

    name: str                     # exactly as apt prints it (``libfoo`` or ``libfoo:i386``)
    package: str                  # without the architecture
    arch: str
    installed: Optional[str]
    candidate: str
    origins: List[str]
    origin_label: str
    category: str
    security: bool
    download_bytes: Optional[int] = None

    @property
    def is_new(self) -> bool:
        return self.installed is None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.package, "arch": self.arch, "from": self.installed, "to": self.candidate,
                "origin": ", ".join(self.origins), "origin_label": self.origin_label,
                "category": self.category, "security": self.security,
                "download_bytes": self.download_bytes}


@dataclass
class Removal:
    name: str                     # as apt prints it
    package: str
    version: Optional[str]
    purge: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.package, "version": self.version, "purge": self.purge}


@dataclass
class Plan:
    """The parsed result of an apt simulation."""

    items: List[PlanItem] = field(default_factory=list)
    removals: List[Removal] = field(default_factory=list)
    kept_back: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def digest(self) -> str:
        """A short fingerprint of what would change - the ``Inst``/``Remv`` set, nothing else - so the
        helper can check it is about to do exactly what the user was shown."""
        lines = sorted([f"inst {i.name} {i.candidate}" for i in self.items]
                       + [f"remv {r.name}" for r in self.removals])
        return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def kernel_items(self) -> List[PlanItem]:
        return [i for i in self.items if _update.KERNEL_PACKAGE_RE.match(i.package)]


def parse_simulation(text: str) -> Plan:
    """Parse ``apt-get -s dist-upgrade`` (or ``upgrade``/``install``) output into a :class:`Plan`.
    Unrecognised lines are ignored; ``E:`` lines become :attr:`Plan.errors`."""
    plan = Plan()
    in_kept = False
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if in_kept:
            if line.startswith("  "):
                plan.kept_back.extend(line.split())
                continue
            in_kept = False
        if line.startswith(_KEPT_BACK_HEADER):
            in_kept = True
            continue
        if line.startswith("E: "):
            plan.errors.append(line[3:].strip())
            continue
        m = _INST_RE.match(line)
        if m:
            inner = m.group("inner").strip()
            arch_m = _TRAILING_ARCH_RE.search(inner)
            arch_txt = arch_m.group("arch") if arch_m else ""
            if arch_m:
                inner = inner[:arch_m.start()]
            parts = inner.split(None, 1)
            candidate = parts[0] if parts else ""
            origins = [o.strip() for o in (parts[1] if len(parts) > 1 else "").split(",") if o.strip()]
            name = m.group("name")
            package, name_arch = _split_arch(name)
            plan.items.append(PlanItem(
                name=name, package=package, arch=name_arch or arch_txt, installed=m.group("old"),
                candidate=candidate, origins=origins, origin_label=origin_label(origins),
                category=categorize(package, origins), security=any(_is_security_origin(o) for o in origins)))
            continue
        m = _REMV_RE.match(line)
        if m:
            name = m.group("name")
            plan.removals.append(Removal(name=name, package=_split_arch(name)[0], version=m.group("ver"),
                                         purge=m.group("verb") == "Purg"))
    return plan


def parse_print_uris(text: str) -> Dict[str, int]:
    """``apt-get --print-uris`` output -> ``{package: bytes still to download}`` (best effort)."""
    sizes: Dict[str, int] = {}
    for raw in (text or "").splitlines():
        m = _URI_LINE_RE.match(raw.strip())
        if not m:
            continue
        package = m.group("file").split("_", 1)[0]
        sizes[package] = sizes.get(package, 0) + int(m.group("size"))
    return sizes


def apply_sizes(plan: Plan, sizes: Mapping[str, int]) -> None:
    for item in plan.items:
        if item.package in sizes:
            item.download_bytes = sizes[item.package]


def removal_names(text: str) -> List[str]:
    """The package names an apt simulation would remove/purge (architecture stripped, sorted, unique)."""
    return sorted({r.package for r in parse_simulation(text).removals})


# =================================================================================================
# Reboot / re-login
# =================================================================================================
#: a package in this list, upgraded after boot, needs a restart to be in effect
REBOOT_PACKAGE_RE = re.compile(
    r"^(?:linux-image-|linux-signed-image-|linux-image-unsigned-|libc6$|systemd$|libsystemd0$|udev$|"
    r"dbus$|dbus-daemon$|dbus-x11$|libdbus-1-3$|libglib2\.0-0|libgtk-3-0|libgtk-4-1|xserver-xorg-core$|"
    r"nvidia-driver-|nvidia-kernel-|libnvidia-|intel-microcode$|amd64-microcode$)")
#: upgraded after boot: the running desktop session should be restarted (log out and in)
RELOGIN_PACKAGE_RE = re.compile(r"^lindos-desktop$")


def _reboot_text(package: str) -> str:
    if package.startswith(("linux-image-", "linux-signed-image-")):
        return f"A new kernel was installed ({package})"
    if package == "libc6":
        return "The core system libraries were updated"
    if package in ("systemd", "libsystemd0", "udev"):
        return "The system manager was updated"
    if package.startswith(("dbus", "libdbus")):
        return "The system message bus was updated"
    if package.startswith(("libglib", "libgtk")):
        return "Core desktop libraries were updated"
    if package == "xserver-xorg-core":
        return "The display server was updated"
    if package.startswith(("nvidia-", "libnvidia-")):
        return "The graphics driver was updated"
    if package.endswith("-microcode"):
        return "Processor microcode was updated"
    return f"{package} was updated"


_DPKG_LOG_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<action>install|upgrade)\s+"
    r"(?P<pkg>\S+)\s+(?P<old>\S+)\s+(?P<new>\S+)\s*$")


def _naive_local_epoch(value: _dt.datetime) -> float:
    return value.timestamp()          # a naive datetime is taken as local time, which is what dpkg logs


def reboot_reasons_from_dpkg_log(log_text: str, *, boot_epoch: float,
                                 to_epoch: Callable[[_dt.datetime], float] = _naive_local_epoch
                                 ) -> List[Dict[str, str]]:
    """Packages of :data:`REBOOT_PACKAGE_RE` / :data:`RELOGIN_PACKAGE_RE` that dpkg installed or
    upgraded after the machine booted (``boot_epoch``), from ``/var/log/dpkg.log`` text. One reason
    per package: ``{"kind": "reboot"|"relogin", "package", "text"}``."""
    reasons: Dict[str, Dict[str, str]] = {}
    for raw in (log_text or "").splitlines():
        m = _DPKG_LOG_RE.match(raw.strip())
        if not m:
            continue
        try:
            when = _dt.datetime.strptime(f"{m.group('date')} {m.group('time')}", "%Y-%m-%d %H:%M:%S")
            stamp = to_epoch(when)
        except (ValueError, OverflowError, OSError):
            continue
        if stamp < boot_epoch:
            continue
        package = m.group("pkg").split(":", 1)[0]
        if REBOOT_PACKAGE_RE.match(package):
            reasons[package] = {"kind": "reboot", "package": package, "text": _reboot_text(package)}
        elif RELOGIN_PACKAGE_RE.match(package) and package not in reasons:
            reasons[package] = {"kind": "relogin", "package": package,
                                "text": "The Lindos desktop was updated - sign out and back in to use it"}
    return [reasons[k] for k in sorted(reasons)]


_KERNEL_RELEASE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)*)(?:-(?P<abi>\d+))?(?:-(?P<flavour>[A-Za-z][A-Za-z0-9_+.]*))?$")


def _kernel_key(release: str) -> Optional[Tuple[Tuple[int, ...], str]]:
    m = _KERNEL_RELEASE_RE.match(release.strip())
    if not m:
        return None
    nums = tuple(int(x) for x in m.group("num").split("."))
    if m.group("abi"):
        nums += (int(m.group("abi")),)
    return nums, (m.group("flavour") or "")


def newer_kernel_installed(running: str, installed_releases: Iterable[str]) -> Optional[str]:
    """The newest installed kernel release of the SAME flavour (``-generic``, ``-lindos`` ...) that is
    newer than *running*, or ``None`` - a stock kernel never nags a user who chose the Lindos kernel."""
    run_key = _kernel_key(running)
    if run_key is None:
        return None
    best: Optional[Tuple[Tuple[int, ...], str]] = None
    best_release = None
    for release in installed_releases:
        key = _kernel_key(release)
        if key is None or key[1] != run_key[1]:
            continue
        if key[0] > run_key[0] and (best is None or key[0] > best[0]):
            best, best_release = key, release
    return best_release


def installed_kernel_releases(boot_dir: Optional[str] = None) -> List[str]:
    """Release strings from ``/boot/vmlinuz-*``."""
    directory = boot_dir if boot_dir is not None else paths.resolve("/boot")
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n[len("vmlinuz-"):] for n in names if n.startswith("vmlinuz-"))


def boot_epoch_now(*, proc_stat: Optional[str] = None) -> Optional[float]:
    """The boot time (epoch seconds) from ``/proc/stat`` ``btime``, or ``None`` when unknown."""
    if proc_stat is None:
        try:
            with open(paths.resolve("/proc/stat"), "r", encoding="utf-8", errors="replace") as fh:
                proc_stat = fh.read()
        except OSError:
            return None
    for line in proc_stat.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _running_kernel() -> str:
    uname = getattr(os, "uname", None)
    if uname is None:
        return ""
    try:
        return str(uname().release)
    except OSError:
        return ""


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def compute_reboot_reasons(*, running_kernel: Optional[str] = None, boot_epoch: Optional[float] = None,
                           dpkg_log_text: Optional[str] = None, kernels: Optional[Sequence[str]] = None,
                           to_epoch: Callable[[_dt.datetime], float] = _naive_local_epoch
                           ) -> List[Dict[str, str]]:
    """Every reason a restart (or a new login) is needed *now*: packages upgraded since boot plus a
    newer installed kernel than the running one. All inputs are injectable; defaults read the system."""
    running = _running_kernel() if running_kernel is None else running_kernel
    epoch = boot_epoch_now() if boot_epoch is None else boot_epoch
    reasons: List[Dict[str, str]] = []
    if epoch is not None:
        text = dpkg_log_text
        if text is None:
            text = _read_text(paths.resolve(DPKG_LOG_PATH))
            older = _read_text(paths.resolve(DPKG_LOG_PATH + ".1"))
            if older:
                text = older + "\n" + text
        reasons.extend(reboot_reasons_from_dpkg_log(text, boot_epoch=epoch, to_epoch=to_epoch))
    releases = installed_kernel_releases() if kernels is None else list(kernels)
    newer = newer_kernel_installed(running, releases)
    if newer and not any(r["package"].startswith("linux-image-") for r in reasons):
        reasons.append({"kind": "reboot", "package": f"linux-image-{newer}",
                        "text": f"A newer kernel ({newer}) is installed than the one running ({running})"})
    return reasons


def write_reboot_required(reasons: Sequence[Mapping[str, str]]) -> bool:
    """``/run/reboot-required`` (+ ``.pkgs``) for the *reboot* reasons; never removes them (``/run``
    is cleared by the reboot itself). Returns True when the file exists afterwards."""
    pkgs = sorted({r["package"] for r in reasons if r.get("kind") == "reboot"})
    marker = paths.resolve(REBOOT_REQUIRED_PATH)
    if not pkgs:
        return os.path.isfile(marker)
    listing = paths.resolve(REBOOT_REQUIRED_PKGS_PATH)
    existing = [ln.strip() for ln in _read_text(listing).splitlines() if ln.strip()]
    merged = sorted(set(existing) | set(pkgs))
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("*** System restart required ***\n")
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("\n".join(merged) + "\n")
    except OSError:
        return False
    return True


def read_reboot_state() -> Dict[str, Any]:
    """What is pending right now: ``{"required": bool, "packages": [...]}``."""
    marker = paths.resolve(REBOOT_REQUIRED_PATH)
    pkgs = [ln.strip() for ln in _read_text(paths.resolve(REBOOT_REQUIRED_PKGS_PATH)).splitlines() if ln.strip()]
    return {"required": os.path.isfile(marker), "packages": sorted(set(pkgs))}


def reboot_hook(**kwargs: Any) -> List[Dict[str, str]]:
    """The apt ``DPkg::Post-Invoke`` hook body: work out the reasons and write ``/run/reboot-required``."""
    reasons = compute_reboot_reasons(**kwargs)
    write_reboot_required(reasons)
    return reasons


# =================================================================================================
# The state file
# =================================================================================================
def default_runner(argv: Sequence[str], timeout: float = 900.0) -> Tuple[int, str]:
    """Run *argv* (never through a shell) with a stable ``C`` locale; ``(returncode, stdout+stderr)``.
    A missing program is ``(127, message)``; a timeout is ``(124, message)``."""
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C", "DEBIAN_FRONTEND": "noninteractive"})
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out after {int(timeout)}s"
    except OSError as exc:
        return 126, f"{argv[0]}: {exc}"
    return proc.returncode, proc.stdout or ""


SIMULATE_ARGV = ("apt-get", "-q", "-s", "dist-upgrade")
PRINT_URIS_ARGV = ("apt-get", "-q", "--print-uris", "-y", "dist-upgrade")


def simulate_upgrade(runner: Runner, *, timeout: float = 300.0) -> Plan:
    """Run the simulation and parse it; a non-zero exit without an ``E:`` line still counts as an error."""
    rc, out = runner(SIMULATE_ARGV, timeout)
    plan = parse_simulation(out)
    if rc != 0 and not plan.errors:
        last = [ln for ln in out.splitlines() if ln.strip()][-1:] or [f"apt-get exited with {rc}"]
        plan.errors.append(last[0].strip())
    return plan


def _iso(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def _repo_reachability(update_output: str, url: Optional[str]) -> Optional[bool]:
    """From ``apt-get update`` output: did the Lindos repository answer? ``None`` = not known."""
    if not url or not update_output:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    if not host:
        return None
    verdict: Optional[bool] = None
    for line in update_output.splitlines():
        low = line.lower()
        if host not in low:
            continue
        if line.startswith(("Err:", "E:", "W:")) or "failed" in low:
            return False
        if line.startswith(("Hit:", "Get:")):
            verdict = True
    return verdict


def group_items(items: Sequence[PlanItem]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for cat in CATEGORY_ORDER:
        members = sorted((i for i in items if i.category == cat), key=lambda i: i.package)
        if not members:
            continue
        sized = [i.download_bytes for i in members if i.download_bytes is not None]
        groups.append({"id": cat, "title": CATEGORY_TITLES[cat], "count": len(members),
                       "download_bytes": sum(sized) if sized else None,
                       "items": [i.to_dict() for i in members]})
    return groups


def build_state(plan: Plan, *, now: _dt.datetime, refreshed_at: Optional[str], refresh: Mapping[str, Any],
                repo: Mapping[str, Any], reboot_pending: Mapping[str, Any],
                reboot_reasons: Sequence[Mapping[str, str]], booted_kernel: str,
                held: Sequence[str]) -> Dict[str, Any]:
    """The ``update-state.json`` document (schema 1, documented in SPEC-UPDATE.md §40)."""
    counts = {cat: sum(1 for i in plan.items if i.category == cat) for cat in CATEGORY_ORDER}
    counts["total"] = len(plan.items)
    sized = [i.download_bytes for i in plan.items if i.download_bytes is not None]
    would_reboot = sorted({i.package for i in plan.items if REBOOT_PACKAGE_RE.match(i.package)})
    relogin = sorted({r["package"] for r in reboot_reasons if r.get("kind") == "relogin"})
    return {
        "schema": STATE_SCHEMA,
        "written_at": _iso(now),
        "refreshed_at": refreshed_at,
        "refresh": dict(refresh),
        "repo": dict(repo),
        "plan_ok": plan.ok,
        "plan_errors": list(plan.errors),
        "counts": counts,
        "download_bytes": sum(sized) if sized else None,
        "groups": group_items(plan.items),
        "removals": [r.to_dict() for r in plan.removals],
        "kept_back": sorted(set(plan.kept_back)),
        "held": sorted(set(held)),
        "reboot": {
            "required": bool(reboot_pending.get("required")),
            "packages": list(reboot_pending.get("packages") or []),
            "reasons": [dict(r) for r in reboot_reasons],
            "relogin": relogin,
            "would_require_reboot": bool(would_reboot),
            "would_require_reboot_packages": would_reboot,
        },
        "booted_kernel": booted_kernel,
        "digest": plan.digest,
    }


def write_json_atomic(path: str, data: Any, mode: int = 0o644) -> None:
    """Write *data* as JSON to *path* atomically (temp file in the same directory, ``os.replace``) with
    the given mode, so a reader never sees a half-written file."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The parsed state file, or ``None`` when it is missing or not a JSON object."""
    target = path or paths.resolve(STATE_PATH)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def state_age_seconds(state: Mapping[str, Any], *, now: Optional[_dt.datetime] = None) -> Optional[float]:
    """Seconds since the state was last refreshed by ``apt-get update`` (``refreshed_at``), or ``None``."""
    stamp = state.get("refreshed_at")
    if not isinstance(stamp, str):
        return None
    try:
        when = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    current = now or _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (current - when).total_seconds())


def refresh_state(*, runner: Optional[Runner] = None, now: Optional[_dt.datetime] = None,
                  do_update: bool = True, offline: bool = False, write: bool = True,
                  running_kernel: Optional[str] = None, boot_epoch: Optional[float] = None,
                  dpkg_log_text: Optional[str] = None, kernels: Optional[Sequence[str]] = None,
                  lists_just_refreshed: bool = False) -> Dict[str, Any]:
    """Refresh apt's lists (unless *offline*/``do_update`` is false), simulate ``dist-upgrade`` and write
    ``update-state.json``. Never installs anything. Returns the state document.

    *lists_just_refreshed*: the caller has itself just run a successful ``apt-get update`` (the helper's
    ``apt-get-update`` action) and only wants the state rewritten - ``refreshed_at`` becomes now, exactly as when
    this function runs the update. Without it, a run with ``do_update=False`` keeps the previous ``refreshed_at``."""
    run = runner or default_runner
    current = now or _dt.datetime.now(_dt.timezone.utc)
    previous = read_state() or {}
    info = _update.repo_info()
    refresh: Dict[str, Any] = {"attempted": False, "ok": None, "message": "", "at": None}
    update_output = ""
    if offline:
        refresh["message"] = "offline: package lists were not refreshed"
    elif do_update:
        rc, update_output = run(("apt-get", "update", "-q", "-o", "Acquire::Retries=2"), 900.0)
        errs = [ln.strip() for ln in update_output.splitlines() if ln.startswith(("Err:", "E:"))]
        refresh = {"attempted": True, "ok": rc == 0, "at": _iso(current),
                   "message": "" if rc == 0 else (errs[-1] if errs else f"apt-get update exited with {rc}")}
    elif lists_just_refreshed:
        refresh = {"attempted": True, "ok": True, "at": _iso(current), "message": ""}
    refreshed_at = _iso(current) if refresh["ok"] else (previous.get("refreshed_at") or _update.apt_lists_refreshed_at())

    plan = simulate_upgrade(run)
    if plan.ok and plan.items:
        rc, uris = run(PRINT_URIS_ARGV, 300.0)
        if rc == 0:
            apply_sizes(plan, parse_print_uris(uris))
    held_rc, held_out = run(("apt-mark", "showhold"), 30.0)
    held = [ln.strip() for ln in held_out.splitlines() if ln.strip()] if held_rc == 0 else []

    booted = _running_kernel() if running_kernel is None else running_kernel
    reasons = compute_reboot_reasons(running_kernel=booted, boot_epoch=boot_epoch, dpkg_log_text=dpkg_log_text,
                                     kernels=kernels)
    repo = {"configured": info.get("configured", False), "enabled": info.get("enabled", False),
            "url": info.get("url"), "placeholder": info.get("placeholder", False),
            "reachable": _repo_reachability(update_output, info.get("url") if info.get("configured") else None)}
    state = build_state(plan, now=current, refreshed_at=refreshed_at, refresh=refresh, repo=repo,
                        reboot_pending=read_reboot_state(), reboot_reasons=reasons, booted_kernel=booted,
                        held=held)
    if write:
        write_json_atomic(paths.resolve(STATE_PATH), state)
    return state


# =================================================================================================
# In-progress marker (an interrupted upgrade is repaired at the next boot)
# =================================================================================================
def marker_path() -> str:
    return paths.resolve(IN_PROGRESS_PATH)


def write_marker(action: str, *, digest: Optional[str] = None, now: Optional[_dt.datetime] = None) -> None:
    write_json_atomic(marker_path(), {"action": action, "digest": digest, "pid": os.getpid(),
                                      "started_at": _iso(now or _dt.datetime.now(_dt.timezone.utc))})


def clear_marker() -> None:
    try:
        os.unlink(marker_path())
    except OSError:
        pass


def marker_present() -> bool:
    return os.path.isfile(marker_path())


# =================================================================================================
# Checking a plan before the helper runs it
# =================================================================================================
#: packages an upgrade may never remove: losing one breaks the desktop, the network or the boot
UPGRADE_PROTECT_RE = re.compile(
    r"^(?:lindos-|xfce4|xfwm4|xfdesktop4|xfconf|thunar|lightdm|slick-greeter|light-locker|network-manager|"
    r"nm-|plymouth|grub|shim|casper|ubiquity|systemd$|dbus$|libc6$|policykit|polkit|pipewire|wireplumber|"
    r"xserver-xorg-core$|xorg$|mint-meta|sudo$|apt$|dpkg$|python3$)")
#: packages a cleanup (autoremove) may never remove - the build-time list, minus kernels (handled by version)
CLEANUP_PROTECT_RE = re.compile(
    r"^(?:xfce4|xfwm4|xfdesktop4|xfconf|thunar|tumbler|lightdm|slick-greeter|light-locker|mint|network-manager|"
    r"nm-|cups|system-config-printer|avahi|casper|ubiquity|grub|shim|plymouth|pulseaudio|pipewire|wireplumber|"
    r"mesa|libgl|libegl|libdrm|xserver|xorg|xinit|x11|python3|gir1\.2|libgtk|gtk|glib|gvfs|udisks|upower|"
    r"policykit|polkit|systemd|dbus|firefox|thunderbird|blueman|bluez|gnome-|libreoffice|fonts-|hicolor|"
    r"adwaita|mate-|xdg-|initramfs|busybox|lupin|memtest|efibootmgr|os-prober|lindos-|"
    r"linux-(?:generic|image-generic|headers-generic|hwe|firmware|base|libc-dev))")
_KERNEL_PKG_RE = re.compile(r"^linux-(?:image(?:-unsigned)?|signed-image|headers|modules(?:-extra)?|tools|buildinfo)-(?P<rel>\d\S*)$")
_ABI_BASE_RE = re.compile(r"^(\d+\.\d+\.\d+-\d+)")


def kernel_release_of(package: str) -> Optional[str]:
    """``linux-image-6.8.0-45-generic`` -> ``6.8.0-45-generic``; not a versioned kernel package -> None."""
    m = _KERNEL_PKG_RE.match(package)
    return m.group("rel") if m else None


def kernel_base(release: str) -> str:
    """``6.8.0-45-generic`` -> ``6.8.0-45`` (the part every flavour of one build shares)."""
    m = _ABI_BASE_RE.match(release)
    return m.group(1) if m else release


def running_kernel_packages(running: str) -> List[str]:
    return [f"{prefix}-{running}" for prefix in ("linux-image", "linux-modules", "linux-modules-extra")]


def check_upgrade_plan(plan: Plan, *, expected_digest: str, allow_kernel: bool, allow_removals: bool,
                       running_kernel: str) -> List[str]:
    """Reasons the helper must refuse to run *plan* (an empty list = go ahead)."""
    problems: List[str] = []
    if not plan.ok:
        problems.append("apt cannot compute the upgrade: " + "; ".join(plan.errors))
        return problems
    if plan.digest != expected_digest:
        problems.append("the list of changes is not the one you were shown (the package lists changed "
                        "in between) - check for updates again and review the list")
    kernels = plan.kernel_items()
    if kernels and not allow_kernel:
        names = ", ".join(sorted({i.package for i in kernels})[:4])
        problems.append(f"the upgrade includes a kernel update ({names}); it needs the kernel to be "
                        "explicitly included")
    protected_running = set(running_kernel_packages(running_kernel)) if running_kernel else set()
    doomed = sorted({r.package for r in plan.removals
                     if UPGRADE_PROTECT_RE.match(r.package) or r.package in protected_running})
    if doomed:
        problems.append("the upgrade would remove protected packages: " + ", ".join(doomed[:8]))
    elif plan.removals and not allow_removals:
        names = ", ".join(sorted({r.package for r in plan.removals})[:6])
        problems.append(f"the upgrade would remove {len(plan.removals)} package(s) ({names}) - "
                        "removals have to be approved explicitly")
    return problems


@dataclass
class CleanupDecision:
    """What a safe cleanup will do: purge ``packages`` (empty = nothing to do) or refuse (``refused``)."""

    packages: List[str] = field(default_factory=list)
    kept: List[Tuple[str, str]] = field(default_factory=list)      # (package, why it was left alone)
    refused: str = ""

    @property
    def nothing_to_do(self) -> bool:
        return not self.packages and not self.refused


def _protected_kernel_bases(running: str, installed_releases: Sequence[str]) -> set:
    """The running kernel, the newest installed one, and the newest one older than that."""
    keyed = sorted({kernel_base(r) for r in installed_releases if _kernel_key(r) is not None},
                   key=lambda b: (_kernel_key(b) or ((), ""))[0])
    protected = {kernel_base(running)} if running else set()
    protected.update(keyed[-2:])
    return protected


def decide_cleanup(runner: Runner, *, running_kernel: str, installed_kernels: Sequence[str],
                   max_packages: int = CLEANUP_MAX_PACKAGES) -> CleanupDecision:
    """Ported from ``build/chroot/lib.sh`` ``safe_autoremove``: simulate ``autoremove --purge``, leave out
    everything protected (desktop, network, boot, the running / newest / previous kernel), then prove with a
    second simulation that purging just the rest removes nothing else."""
    rc, out = runner(("apt-get", "-q", "-s", "autoremove", "--purge"), 300.0)
    if rc != 0:
        return CleanupDecision(refused="apt cannot compute the cleanup (is another package tool running?)")
    victims = removal_names(out)
    if not victims:
        return CleanupDecision()
    protected_bases = _protected_kernel_bases(running_kernel, installed_kernels)
    safe: List[str] = []
    kept: List[Tuple[str, str]] = []
    for name in victims:
        release = kernel_release_of(name)
        if release is not None:
            if kernel_base(release) in protected_bases:
                kept.append((name, "the running, newest or previous kernel"))
                continue
            safe.append(name)
            continue
        if CLEANUP_PROTECT_RE.match(name):
            kept.append((name, "part of the desktop or the base system"))
            continue
        safe.append(name)
    if not safe:
        return CleanupDecision(kept=kept)
    if len(safe) > max_packages:
        return CleanupDecision(kept=kept, refused=f"{len(safe)} packages would be removed (more than {max_packages}); "
                                                  "nothing was removed")
    rc2, out2 = runner(("apt-get", "-q", "-s", "purge", "--") + tuple(safe), 300.0)
    if rc2 != 0:
        return CleanupDecision(kept=kept, refused="apt cannot purge the leftover packages safely; nothing was removed")
    would_go = removal_names(out2)
    extras = sorted(set(would_go) - set(safe))
    if extras:
        return CleanupDecision(kept=kept, refused="removing them would also remove other packages ("
                                                  + ", ".join(extras[:6]) + "); nothing was removed")
    return CleanupDecision(packages=safe, kept=kept)


# =================================================================================================
# apt history (/var/log/apt/history.log)
# =================================================================================================
_HISTORY_ENTRY_RE = re.compile(r"(?P<name>[^\s,()]+)\s+\((?P<inner>[^)]*)\)")
_HISTORY_ACTIONS = ("Install", "Upgrade", "Downgrade", "Reinstall", "Remove", "Purge")


def _parse_history_packages(action: str, value: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in _HISTORY_ENTRY_RE.finditer(value):
        name = m.group("name").split(":", 1)[0]
        parts = [p.strip() for p in m.group("inner").split(",")]
        entry: Dict[str, Any] = {"name": name}
        if action in ("Upgrade", "Downgrade"):
            entry["from"] = parts[0] if parts else None
            entry["to"] = parts[1] if len(parts) > 1 else None
        else:
            entry["version"] = parts[0] if parts else None
            entry["automatic"] = "automatic" in parts[1:]
        out.append(entry)
    return out


def parse_history(text: str) -> List[Dict[str, Any]]:
    """Parse apt's ``history.log`` into transactions, oldest first:
    ``{"start", "end", "commandline", "requested_by", "error", "actions": {"Upgrade": [{name, from, to}], ...}}``."""
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            current = None
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if key == "Start-Date":
            current = {"start": re.sub(r"\s+", " ", value), "end": None, "commandline": "", "requested_by": "",
                       "error": None, "actions": {}}
            entries.append(current)
            continue
        if current is None:
            continue
        if key == "End-Date":
            current["end"] = re.sub(r"\s+", " ", value)
        elif key == "Commandline":
            current["commandline"] = value
        elif key == "Requested-By":
            current["requested_by"] = value
        elif key == "Error":
            current["error"] = value
        elif key in _HISTORY_ACTIONS:
            current["actions"][key] = _parse_history_packages(key, value)
    return entries


def read_history(limit: int = 50) -> List[Dict[str, Any]]:
    """The newest *limit* transactions (newest first) from ``history.log`` and its rotated copies."""
    base = paths.resolve(APT_HISTORY_LOG)
    directory = os.path.dirname(base)
    chunks: List[Tuple[int, str]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    stem = os.path.basename(base)
    for name in names:
        if name == stem:
            rank = 0
        else:
            m = re.match(re.escape(stem) + r"\.(\d+)(\.gz)?$", name)
            if not m:
                continue
            rank = int(m.group(1))
        full = os.path.join(directory, name)
        try:
            if name.endswith(".gz"):
                with gzip.open(full, "rt", encoding="utf-8", errors="replace") as fh:
                    chunks.append((rank, fh.read()))
            else:
                chunks.append((rank, _read_text(full)))
        except (OSError, EOFError):
            continue
    entries: List[Dict[str, Any]] = []
    for _rank, text in sorted(chunks, key=lambda c: -c[0]):          # oldest file first
        entries.extend(parse_history(text))
    entries.reverse()
    return entries[:max(0, limit)]


# =================================================================================================
# CLI: python3 -m lindos.updatestate ...
# =================================================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stderr.write("usage: python3 -m lindos.updatestate refresh [--offline] [--no-update] | reboot-hook\n")
        return 2
    command = args[0]
    if command == "refresh":
        unknown = [a for a in args[1:] if a not in ("--offline", "--no-update")]
        if unknown:
            sys.stderr.write(f"lindos.updatestate: unknown option {unknown[0]}\n")
            return 2
        # checked again HERE, not only by the unit and update-refresh: this runs after the queue behind other
        # apt jobs (apt-serialise), long after those two looked, and an upgrade may have started meanwhile
        if marker_present():
            sys.stdout.write("an update is in progress or waiting for repair - not refreshing now\n")
            return 0
        try:
            state = refresh_state(offline="--offline" in args, do_update="--no-update" not in args)
        except OSError as exc:
            sys.stderr.write(f"lindos.updatestate: cannot write the update state: {exc}\n")
            return 1
        counts = state.get("counts", {})
        sys.stdout.write(f"update state written: {counts.get('total', 0)} update(s) available\n")
        return 0
    if command == "reboot-hook":
        try:
            reboot_hook()
        except Exception:                                           # a hook must never break apt
            return 0
        return 0
    sys.stderr.write(f"lindos.updatestate: unknown command {command!r}\n")
    return 2


__all__ = [
    "STATE_PATH", "IN_PROGRESS_PATH", "REBOOT_REQUIRED_PATH", "REBOOT_REQUIRED_PKGS_PATH", "DPKG_LOG_PATH",
    "APT_HISTORY_LOG", "STATE_SCHEMA", "STALE_AFTER_SECONDS", "CLEANUP_MAX_PACKAGES", "CATEGORY_ORDER",
    "CATEGORY_TITLES", "ORIGIN_LABELS", "PlanItem", "Removal", "Plan", "CleanupDecision",
    "parse_simulation", "parse_print_uris", "apply_sizes", "removal_names", "origin_parts", "origin_label",
    "categorize", "reboot_reasons_from_dpkg_log", "newer_kernel_installed", "installed_kernel_releases",
    "boot_epoch_now", "compute_reboot_reasons", "write_reboot_required", "read_reboot_state", "reboot_hook",
    "default_runner", "simulate_upgrade", "group_items", "build_state", "write_json_atomic", "read_state",
    "state_age_seconds", "refresh_state", "marker_path", "write_marker", "clear_marker", "marker_present",
    "check_upgrade_plan", "decide_cleanup", "kernel_release_of", "kernel_base", "running_kernel_packages",
    "parse_history", "read_history", "main",
]

if __name__ == "__main__":
    sys.exit(main())
