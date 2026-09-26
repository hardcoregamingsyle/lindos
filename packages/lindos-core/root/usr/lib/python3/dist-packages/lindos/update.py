"""lindos.update — updates for Lindos's own 12 packages, honestly (SPEC-UPDATE.md §35-§36).

Two update channels exist and this module only ever touches one of them:

* **The base system** (kernel, XFCE, Firefox, Wine, everything from Ubuntu/Mint's own
  repositories) already updates through Mint's own Update Manager (``mintupdate``). This module
  never drives that — it only *counts* how many system packages ``apt`` sees as upgradable
  (:data:`UpdateStatus.system_updates`), so the Settings page can say "N available — open Update
  Manager" without duplicating mintupdate's own logic.
* **Lindos's own ``lindos-*`` packages** have no update channel until someone points
  ``LINDOS_APT_REPO_URL`` (``build/config.env``) at a real signed apt repository (see
  ``build/publish-apt-repo.sh`` and ``docs/UPDATES.md``) — until then :func:`configured_repo_url`
  returns ``None`` and every "checked" value here says so plainly. Meanwhile the sideload path
  (:func:`scan_sideload_dir`) lets someone install/replace ``lindos-*.deb`` files from a local
  folder (e.g. a CI artifact) with no repo at all.

Every read here is unprivileged and safe (SPEC-UPDATE.md §35): :func:`check` never calls
``apt-get update`` itself and never elevates — it only reads whatever ``apt-daily.timer`` (or an
explicit, privileged ``apt-get-update`` helper action, see :mod:`lindos.helper`) already
downloaded. Applying anything is a separate, explicit, privileged step
(``lindos-helper`` actions ``system-upgrade`` / ``cleanup-old-packages`` / ``install-local-debs``)
that this module only ever *prepares a payload for* — it never runs ``apt-get``/``dpkg`` itself.

Every external command is reached through an injectable ``run``/``which`` (module-level
defaults ``subprocess.run``/``shutil.which``, exactly like :mod:`lindos.dualboot`), and every
filesystem path goes through :func:`lindos.paths.resolve` (``LINDOS_ROOT``-aware), so the whole
module is testable on Windows/macOS with no real ``apt``/``dpkg`` and no root.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from . import paths

#: build/config.env's default (a host Lindos does not own — SPEC-UPDATE.md §35); repo_status()
#: refuses to call this "configured" even if somehow written into lindos.list by hand.
PLACEHOLDER_APT_REPO_URL = "https://packages.lindos.dev"

#: where ``build/chroot/00-repos.sh`` / a real apt repo setup writes the Lindos apt source
#: (SPEC-UPDATE.md §36.5-§36.6) -- a flat-format repo, "./" as the sole "suite".
LINDOS_SOURCES_LIST = "/etc/apt/sources.list.d/lindos.list"
#: apt's package-lists cache; its own mtime (not any one file inside) is the "last refreshed" signal.
APT_LISTS_DIR = "/var/lib/apt/lists"
#: written by apt/dpkg (and read by mintupdate) whenever a just-installed package wants a reboot.
REBOOT_REQUIRED_PATH = "/var/run/reboot-required"
#: the ``lindos-kernel`` local version suffix (see ``build/kernel/build-kernel.sh``'s
#: ``LOCALVERSION=-lindos`` and ``lindos_kernel.features.is_lindos_kernel``); duplicated here
#: (rather than importing the separate ``lindos-kernel`` package) because lindos-core must not
#: depend on lindos-kernel being installed.
KERNEL_LOCALVERSION_MARKER = "-lindos"

#: apt's own three kernel package name prefixes (SPEC-UPDATE.md §36.2/§36.4); matches the
#: *actual* Debian kernel packages, Lindos's tuned build included ("linux-image-6.14.0-lindos").
KERNEL_PACKAGE_RE = re.compile(r"^(?:linux-image|linux-headers|linux-modules)-")
#: every package this addendum's own apt repo channel ships (SPEC §2's ``lindos-*`` naming).
LINDOS_PACKAGE_RE = re.compile(r"^lindos-")
#: a Debian version string, permissive enough for epoch/upstream/revision (``1:2.3.4-5~rc1build2``).
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~-]*$")

#: ``apt list --upgradable`` one data line, e.g.
#: ``lindos-core/now 1.0.1 all [upgradable from: 1.0.0]`` or
#: ``firefox/noble-security 129.0+build2-0ubuntu0.24.04.1 amd64 [upgradable from: 128.0]``.
_UPGRADABLE_LINE_RE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<origin>\S+)\s+(?P<candidate>\S+)\s+(?P<arch>\S+)\s+"
    r"\[upgradable from:\s*(?P<installed>[^\]]+)\]\s*$"
)
#: the flat-format sources.list line ``build/publish-apt-repo.sh`` documents:
#: ``deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] <URL> ./``.
_SOURCES_LINE_RE = re.compile(r"^deb\s+(?:\[[^\]]*\]\s*)?(?P<url>\S+)\s+\./?\s*$")
#: best-effort fallback for :func:`_naive_version_compare` (used only when ``dpkg`` itself is
#: not on PATH, e.g. every test on a non-Debian host): alternating digit/non-digit runs.
_VERSION_SPLIT_RE = re.compile(r"\d+|\D+")


@dataclass
class PackageUpdate:
    """One row of ``apt list --upgradable`` (SPEC-UPDATE.md §36.2)."""

    name: str
    installed: str
    candidate: str
    channel: str   # "lindos" | "system" | "kernel"

    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "installed": self.installed, "candidate": self.candidate,
                "channel": self.channel}


@dataclass
class UpdateStatus:
    """Everything ``lindos-update check`` / the Settings "Updates" page need."""

    refreshed_at: Optional[str]                    # ISO-8601 (UTC), mtime of apt's lists dir; None if never refreshed
    lindos_updates: List[PackageUpdate]
    system_updates: List[PackageUpdate]            # count only matters here -- see module docstring
    kernel_available: Optional[PackageUpdate]
    booted_kernel: str                             # uname -r
    booted_is_lindos_kernel: bool
    reboot_required: bool                          # /var/run/reboot-required exists
    repo_configured: bool
    repo_reachable: Optional[bool]                 # None when repo_configured is False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refreshed_at": self.refreshed_at,
            "lindos_updates": [u.to_dict() for u in self.lindos_updates],
            "system_updates": [u.to_dict() for u in self.system_updates],
            "kernel_available": self.kernel_available.to_dict() if self.kernel_available else None,
            "booted_kernel": self.booted_kernel,
            "booted_is_lindos_kernel": self.booted_is_lindos_kernel,
            "reboot_required": self.reboot_required,
            "repo_configured": self.repo_configured,
            "repo_reachable": self.repo_reachable,
        }


@dataclass
class SideloadCandidate:
    """One ``*.deb`` file :func:`scan_sideload_dir` looked at (SPEC-UPDATE.md §36.3)."""

    path: str
    package: Optional[str]          # None when dpkg-deb couldn't read it at all
    version: Optional[str]
    installed_version: Optional[str]
    accepted: bool                  # False: never sideloaded (wrong name / unreadable .deb)
    reason: str                     # human-readable warning/rejection reason; "" when unremarkable

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "package": self.package, "version": self.version,
                "installed_version": self.installed_version, "accepted": self.accepted,
                "reason": self.reason}


# --- classification -----------------------------------------------------------------------
def _channel_for(name: str) -> str:
    if KERNEL_PACKAGE_RE.match(name):
        return "kernel"
    if LINDOS_PACKAGE_RE.match(name):
        return "lindos"
    return "system"


def _pick_kernel_update(candidates: List[PackageUpdate]) -> Optional[PackageUpdate]:
    """The one entry that best represents "the kernel" when several linux-image/headers/modules
    packages are all upgradable at once -- prefer the ``linux-image-*`` row (what actually boots)."""
    for u in candidates:
        if u.name.startswith("linux-image-"):
            return u
    return candidates[0] if candidates else None


# --- apt list --upgradable ------------------------------------------------------------------
def apt_list_upgradable(*, run: Callable[..., Any] = subprocess.run) -> List[PackageUpdate]:
    """Parse ``apt list --upgradable`` (no root, no network -- reads apt's existing cache only).

    Never raises: a missing ``apt``, a non-zero exit, or unparsable output all yield ``[]``
    rather than an exception, exactly like the rest of this codebase's best-effort probes
    (:mod:`lindos.dualboot`).
    """
    try:
        proc = run(["apt", "list", "--upgradable"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(proc, "returncode", 1) != 0:
        return []
    out = getattr(proc, "stdout", "") or ""
    updates: List[PackageUpdate] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("Listing..."):
            continue
        m = _UPGRADABLE_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        updates.append(PackageUpdate(name=name, installed=m.group("installed").strip(),
                                     candidate=m.group("candidate"), channel=_channel_for(name)))
    return updates


def _booted_kernel(*, run: Callable[..., Any], which: Callable[[str], Optional[str]]) -> str:
    exe = which("uname")
    if not exe:
        return ""
    try:
        proc = run([exe, "-r"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                  timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return (getattr(proc, "stdout", "") or "").strip()


def _apt_lists_refreshed_at() -> Optional[str]:
    """ISO-8601 (UTC) mtime of apt's lists directory, or ``None`` when it is missing/empty (an
    empty dir -- just ``lock``/``partial`` -- means ``apt-get update`` has never actually run)."""
    d = paths.resolve(APT_LISTS_DIR)
    try:
        entries = [e for e in os.listdir(d) if e not in ("lock", "partial")]
    except OSError:
        return None
    if not entries:
        return None
    try:
        mtime = os.path.getmtime(d)
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def configured_repo_url() -> Optional[str]:
    """The URL from ``/etc/apt/sources.list.d/lindos.list``, or ``None`` when that file does not
    exist (``LINDOS_APT_REPO_ENABLE`` was ``0`` at build time -- the honest default, SPEC-UPDATE.md
    §36.6) or has no recognisable ``deb ... ./`` line."""
    path = paths.resolve(LINDOS_SOURCES_LIST)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SOURCES_LINE_RE.match(line)
        if m:
            return m.group("url")
    return None


# --- reachability --------------------------------------------------------------------------
def _default_fetch(url: str) -> Tuple[bool, str]:
    """Real network probe (a HEAD-ish GET of *url*); never raises -- returns ``(False, reason)``
    on any failure. A repo that answers with an HTTP error status is still "reachable" (the host
    exists and speaks HTTP); ``apply``/``cleanup`` will surface a real apt error later if the
    repo's *contents* are actually broken -- this is only a "does anything answer" probe."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - https enforced by caller
            code = int(getattr(resp, "status", 200) or 200)
        return True, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)


def repo_status(url: str, *, fetch: Optional[Callable[[str], Tuple[bool, str]]] = None) -> Tuple[bool, str]:
    """``(reachable, message)`` for *url*.

    HTTPS is required unless the host is ``localhost``/``127.0.0.1``/``::1`` (a self-hosted apt
    repo over plain HTTP would let anyone on the network path silently tamper with package lists
    in transit -- refused outright, never silently downgraded to "trusted anyway", per
    SPEC-UPDATE.md §35). The placeholder URL is never treated as configured even if it somehow
    ended up in ``lindos.list``. *fetch* defaults to a real network probe; tests inject a fake.
    """
    url = (url or "").strip()
    if not url:
        return False, "no repo URL configured"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, f"not a valid http(s) URL: {url!r}"
    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not is_local:
        return False, "refusing a non-HTTPS repo URL (HTTPS is required unless the host is localhost)"
    if url.rstrip("/") == PLACEHOLDER_APT_REPO_URL.rstrip("/"):
        return False, "this is still the build/config.env placeholder -- no real repo is configured (see docs/UPDATES.md)"
    probe = url.rstrip("/") + "/InRelease"
    fetch_fn = fetch or _default_fetch
    try:
        ok, detail = fetch_fn(probe)
    except Exception as exc:  # never raise: this is a best-effort reachability probe
        return False, f"{url}: {exc}"
    return bool(ok), (f"reachable ({detail})" if ok else detail)


# --- read-only status ------------------------------------------------------------------------
def check(*, run: Callable[..., Any] = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which,
         fetch: Optional[Callable[[str], Tuple[bool, str]]] = None) -> UpdateStatus:
    """Everything the Settings "Updates" page / ``lindos-update check`` need.

    Never elevates and never refreshes the apt cache itself (SPEC-UPDATE.md §36.2) -- it only
    reads whatever is already on disk (apt's lists, ``uname -r``, ``/var/run/reboot-required``,
    ``lindos.list``). The only real network I/O this function ever performs is an optional
    reachability probe of an *already configured* repo URL (never the placeholder); when no repo
    is configured (the default, honest state) this call touches the network not at all.
    """
    updates = apt_list_upgradable(run=run)
    lindos_updates = [u for u in updates if u.channel == "lindos"]
    system_updates = [u for u in updates if u.channel == "system"]
    kernel_candidates = [u for u in updates if u.channel == "kernel"]

    url = configured_repo_url()
    repo_configured = url is not None
    repo_reachable: Optional[bool] = None
    if repo_configured:
        reachable, _detail = repo_status(url or "", fetch=fetch)
        repo_reachable = reachable

    booted_kernel = _booted_kernel(run=run, which=which)

    return UpdateStatus(
        refreshed_at=_apt_lists_refreshed_at(),
        lindos_updates=lindos_updates,
        system_updates=system_updates,
        kernel_available=_pick_kernel_update(kernel_candidates),
        booted_kernel=booted_kernel,
        booted_is_lindos_kernel=KERNEL_LOCALVERSION_MARKER in booted_kernel,
        reboot_required=os.path.isfile(paths.resolve(REBOOT_REQUIRED_PATH)),
        repo_configured=repo_configured,
        repo_reachable=repo_reachable,
    )


# --- helper payloads (SPEC-UPDATE.md §36.4) ---------------------------------------------------
def refresh_payload() -> Dict[str, object]:
    """Payload for the ``apt-get-update`` helper action -- always empty; the action itself takes
    no fields (it only ever runs a plain ``apt-get update``)."""
    return {}


def apply_payload(names_versions: Mapping[str, str], *, allow_kernel: bool = False) -> Dict[str, object]:
    """Build the ``system-upgrade`` helper payload: an explicit, exact ``name=version`` list --
    never a bare "upgrade everything".

    Kernel packages (``linux-image-*``/``linux-headers-*``/``linux-modules-*``) are silently
    excluded unless *allow_kernel* is true (defence in depth: the helper's own
    :func:`lindos.helper.validate_payload` refuses them too, but the caller should never have
    asked for them in the first place when the user did not opt into ``--include-kernel``).
    """
    packages: List[str] = []
    for name in sorted(names_versions):
        if KERNEL_PACKAGE_RE.match(name) and not allow_kernel:
            continue
        packages.append(f"{name}={names_versions[name]}")
    payload: Dict[str, object] = {"packages": packages}
    if allow_kernel:
        payload["allow_kernel"] = True
    return payload


def cleanup_payload() -> Dict[str, object]:
    """Payload for the ``cleanup-old-packages`` helper action -- always empty (a plain
    ``apt-get autoremove --purge``)."""
    return {}


# --- sideload (SPEC-UPDATE.md §36.3) ----------------------------------------------------------
def dpkg_deb_field(path: str, field: str, *, run: Callable[..., Any] = subprocess.run,
                   which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """``dpkg-deb --field <path> <field>``, or ``None`` on any problem (missing tool, bad .deb)."""
    exe = which("dpkg-deb")
    if not exe:
        return None
    try:
        proc = run([exe, "--field", path, field], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return (getattr(proc, "stdout", "") or "").strip() or None


def dpkg_installed_version(name: str, *, run: Callable[..., Any] = subprocess.run,
                          which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The installed version of *name* (``dpkg-query``), or ``None`` when it is not installed or
    ``dpkg-query`` is unavailable."""
    exe = which("dpkg-query")
    if not exe:
        return None
    try:
        proc = run([exe, "-W", "-f=${Version}", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return (getattr(proc, "stdout", "") or "").strip() or None


def _naive_version_compare(a: str, b: str) -> int:
    """Best-effort ``a`` vs ``b`` (-1/0/1) used only when ``dpkg`` itself is not on PATH (every
    test on a non-Debian host); not real Debian policy version ordering, just alternating
    digit/non-digit runs -- good enough to flag an obvious downgrade, never load-bearing for
    anything privileged (the helper never trusts this: it re-verifies package names itself)."""
    pa = _VERSION_SPLIT_RE.findall(a)
    pb = _VERSION_SPLIT_RE.findall(b)
    for x, y in zip(pa, pb):
        if x.isdigit() and y.isdigit():
            xi, yi = int(x), int(y)
            if xi != yi:
                return -1 if xi < yi else 1
        elif x != y:
            return -1 if x < y else 1
    if len(pa) != len(pb):
        return -1 if len(pa) < len(pb) else 1
    return 0


def compare_versions(a: str, b: str, *, run: Callable[..., Any] = subprocess.run,
                     which: Callable[[str], Optional[str]] = shutil.which) -> int:
    """-1 / 0 / 1 for *a* vs *b*, via ``dpkg --compare-versions`` when available, else
    :func:`_naive_version_compare`."""
    exe = which("dpkg")
    if exe:
        try:
            lt = run([exe, "--compare-versions", a, "lt", b], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, timeout=10, check=False)
            if getattr(lt, "returncode", 1) == 0:
                return -1
            gt = run([exe, "--compare-versions", a, "gt", b], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, timeout=10, check=False)
            if getattr(gt, "returncode", 1) == 0:
                return 1
            return 0
        except (OSError, subprocess.SubprocessError):
            pass
    return _naive_version_compare(a, b)


def scan_sideload_dir(directory: str, *, run: Callable[..., Any] = subprocess.run,
                      which: Callable[[str], Optional[str]] = shutil.which) -> List[SideloadCandidate]:
    """Inspect every ``*.deb`` directly inside *directory* (SPEC-UPDATE.md §36.3).

    Keeps only packages whose name matches ``^lindos-``; a downgrade from the currently
    installed version is *accepted* but flagged in :attr:`SideloadCandidate.reason` (a downgrade
    is warned about, never blocked -- the caller/CLI should ask for confirmation on those before
    proceeding without ``--yes``). Never raises: a missing directory yields ``[]``.
    """
    try:
        names = sorted(fn for fn in os.listdir(directory) if fn.lower().endswith(".deb"))
    except OSError:
        return []
    candidates: List[SideloadCandidate] = []
    for fn in names:
        path = os.path.join(directory, fn)
        package = dpkg_deb_field(path, "Package", run=run, which=which)
        version = dpkg_deb_field(path, "Version", run=run, which=which)
        if not package:
            candidates.append(SideloadCandidate(path, None, version, None, False,
                                                 "could not read the Package field (corrupt .deb, or dpkg-deb missing)"))
            continue
        if not LINDOS_PACKAGE_RE.match(package):
            candidates.append(SideloadCandidate(path, package, version, None, False,
                                                 f"package {package!r} is not a lindos-* package"))
            continue
        if not version:
            candidates.append(SideloadCandidate(path, package, None, None, False,
                                                 "could not read the Version field"))
            continue
        installed = dpkg_installed_version(package, run=run, which=which)
        reason = ""
        if installed is not None and compare_versions(version, installed, run=run, which=which) < 0:
            reason = f"downgrade: {installed} -> {version}"
        candidates.append(SideloadCandidate(path, package, version, installed, True, reason))
    return candidates


def sideload_payload(files: Iterable[str]) -> Dict[str, object]:
    """Payload for the ``install-local-debs`` helper action: absolute paths, exactly as given
    (the helper re-validates every path is absolute/exists/ends ``.deb``/is a ``lindos-*``
    package before touching anything -- this is only a shape/ordering convenience)."""
    return {"files": [os.path.abspath(f) for f in files]}


__all__ = [
    "PLACEHOLDER_APT_REPO_URL", "LINDOS_SOURCES_LIST", "APT_LISTS_DIR", "REBOOT_REQUIRED_PATH",
    "KERNEL_LOCALVERSION_MARKER", "KERNEL_PACKAGE_RE", "LINDOS_PACKAGE_RE", "VERSION_RE",
    "PackageUpdate", "UpdateStatus", "SideloadCandidate",
    "apt_list_upgradable", "configured_repo_url", "repo_status", "check",
    "refresh_payload", "apply_payload", "cleanup_payload",
    "dpkg_deb_field", "dpkg_installed_version", "compare_versions", "scan_sideload_dir", "sideload_payload",
]
