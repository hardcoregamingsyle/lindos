"""The registry facts lindos-transfer needs, read from *offline* hives (SPEC-WINDOWS §29.4, §29.10).

All functions take an opened :class:`lindos_transfer.regf.Hive` (``SOFTWARE``, ``SYSTEM`` or a
user's ``NTUSER.DAT``) and never write anything.  Damaged parts of a hive are skipped (logged),
never guessed.  A hive with ``stale=True`` (Windows was not shut down fully) still answers, but
callers label the results "possibly out of date".

Offline there is no ``CurrentControlSet``: :func:`current_control_set` resolves
``SYSTEM\\Select\\Current`` to ``ControlSet00N``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Union

from . import secrets
from .regf import Hive, Key, RegfError

__all__ = [
    "open_hive",
    "try_open_hive",
    "uninstall_entries",
    "user_shell_folders",
    "user_environment",
    "onedrive_folders",
    "profile_list",
    "current_control_set",
    "mounted_devices",
    "hiberboot_enabled",
    "computer_name",
    "windows_version",
    "wallpaper_settings",
    "steam_paths",
    "SERVICE_SIDS",
    "UPDATE_RELEASE_TYPES",
]

log = logging.getLogger("lindos-transfer.winreg")

SOFTWARE_UNINSTALL = (
    ("Microsoft\\Windows\\CurrentVersion\\Uninstall", "machine", "x64"),
    ("WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall", "machine", "x86"),
)
NTUSER_UNINSTALL = (
    ("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall", "user", ""),
    ("Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall", "user", "x86"),
)
USER_SHELL_FOLDERS = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders"
PROFILE_LIST = "Microsoft\\Windows NT\\CurrentVersion\\ProfileList"
CURRENT_VERSION = "Microsoft\\Windows NT\\CurrentVersion"

#: Built-in service accounts (never offered for transfer).
SERVICE_SIDS = ("S-1-5-18", "S-1-5-19", "S-1-5-20")
#: Uninstall-key ``ReleaseType`` values that mark patches, not programs.
UPDATE_RELEASE_TYPES = ("update", "hotfix", "security update", "service pack", "update rollup")
_KB_RE = re.compile(r"\bKB\d{6,8}\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# opening
# --------------------------------------------------------------------------- #
def open_hive(path: Union[str, "os.PathLike[str]"]) -> Hive:
    """Open a hive read-only (deny-listed hives such as SAM raise SecretPathError)."""
    return Hive.open(path)


def try_open_hive(path: Union[str, "os.PathLike[str]", None]) -> Optional[Hive]:
    """Like :func:`open_hive` but returns ``None`` (logged) when missing or unreadable."""
    if path is None:
        return None
    try:
        return Hive.open(path)
    except FileNotFoundError:
        return None
    except secrets.SecretPathError:
        raise
    except (OSError, RegfError) as exc:
        log.warning("cannot read registry file %s: %s", path, exc)
        return None


def _safe_subkeys(key: Optional[Key]) -> List[Key]:
    if key is None:
        return []
    try:
        return key.subkeys()
    except RegfError as exc:
        log.warning("damaged registry key %s: %s", key.path, exc)
        return []


def _find(hive: Hive, path: str) -> Optional[Key]:
    try:
        return hive.find(path)
    except RegfError as exc:
        log.warning("damaged registry key %s in %s: %s", path, hive.name, exc)
        return None


# --------------------------------------------------------------------------- #
# installed programs
# --------------------------------------------------------------------------- #
def _entry(key: Key, scope: str, arch: str) -> Optional[Dict[str, Any]]:
    try:
        name = (key.get_str("DisplayName") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "version": (key.get_str("DisplayVersion") or "").strip(),
            "publisher": (key.get_str("Publisher") or "").strip(),
            "install_location": (key.get_str("InstallLocation") or "").strip(),
            "install_date": (key.get_str("InstallDate") or "").strip(),
            "size_kb": key.get_int("EstimatedSize"),
            "scope": scope,
            "arch": arch,
            "key": key.name,
            "windows_installer": key.get_int("WindowsInstaller") == 1,
            "system_component": key.get_int("SystemComponent") == 1,
            "parent": (key.get_str("ParentKeyName") or "").strip(),
            "release_type": (key.get_str("ReleaseType") or "").strip(),
        }
    except RegfError as exc:
        log.warning("damaged uninstall entry %s: %s", key.path, exc)
        return None


def _keep(entry: Dict[str, Any]) -> bool:
    if entry["system_component"] or entry["parent"]:
        return False
    if entry["release_type"].lower() in UPDATE_RELEASE_TYPES:
        return False
    if _KB_RE.search(entry["name"]):
        return False
    return True


def uninstall_entries(hive: Hive, *, filtered: bool = True) -> List[Dict[str, Any]]:
    """Programs from a ``SOFTWARE`` hive (64-bit + WOW6432Node) or an ``NTUSER.DAT``.

    Each entry: ``name, version, publisher, install_location, install_date, size_kb, scope
    ("machine"|"user"), arch ("x64"|"x86"|""), key, windows_installer, system_component, parent,
    release_type``.  With *filtered* (default) the list matches "Programs and Features": entries
    without DisplayName, with SystemComponent=1, a ParentKeyName, an update ReleaseType or a
    ``KBnnnnnnn`` name are dropped.
    """
    layouts = NTUSER_UNINSTALL if _find(hive, "Software") is not None and _find(hive, "Microsoft") is None \
        else SOFTWARE_UNINSTALL
    out: List[Dict[str, Any]] = []
    for path, scope, arch in layouts:
        for key in _safe_subkeys(_find(hive, path)):
            entry = _entry(key, scope, arch)
            if entry is None:
                continue
            if filtered and not _keep(entry):
                continue
            out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# user folders / environment
# --------------------------------------------------------------------------- #
def _string_values(key: Optional[Key]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if key is None:
        return out
    try:
        for val in key.values():
            text = val.value
            if isinstance(text, str) and val.name:
                out[val.name] = text
    except RegfError as exc:
        log.warning("damaged registry key %s: %s", key.path, exc)
    return out


def user_shell_folders(ntuser: Hive) -> Dict[str, str]:
    """Raw (unexpanded) ``User Shell Folders`` values by value name, e.g. ``{"Personal": "%USERPROFILE%\\Documents"}``."""
    return _string_values(_find(ntuser, USER_SHELL_FOLDERS))


def user_environment(ntuser: Hive) -> Dict[str, str]:
    """The user's own environment variables (``HKCU\\Environment``: OneDrive, TEMP, ...)."""
    return _string_values(_find(ntuser, "Environment"))


def onedrive_folders(ntuser: Hive) -> List[str]:
    """OneDrive sync roots (``Software\\Microsoft\\OneDrive\\Accounts\\*\\UserFolder``), personal first."""
    found: List[str] = []
    accounts = _safe_subkeys(_find(ntuser, "Software\\Microsoft\\OneDrive\\Accounts"))
    accounts.sort(key=lambda k: (k.name.lower() != "personal", k.name.lower()))
    for acc in accounts:
        try:
            folder = acc.get_str("UserFolder")
        except RegfError:
            folder = None
        if folder and folder not in found:
            found.append(folder)
    return found


# --------------------------------------------------------------------------- #
# machine
# --------------------------------------------------------------------------- #
def profile_list(software: Hive) -> List[Dict[str, Any]]:
    """``ProfileList`` entries: ``[{"sid", "profile_image_path", "backup"}]`` (service SIDs kept, flagged)."""
    out: List[Dict[str, Any]] = []
    for key in _safe_subkeys(_find(software, PROFILE_LIST)):
        try:
            path = key.get_str("ProfileImagePath") or ""
        except RegfError:
            continue
        if not path:
            continue
        sid = key.name
        out.append({
            "sid": sid[:-4] if sid.lower().endswith(".bak") else sid,
            "profile_image_path": path,
            "backup": sid.lower().endswith(".bak"),
            "service": sid.split(".")[0] in SERVICE_SIDS,
        })
    return out


def current_control_set(system: Hive) -> str:
    """``ControlSet00N`` named by ``Select\\Current`` (fallbacks: ``Select\\Default``, ``ControlSet001``)."""
    select = _find(system, "Select")
    for name in ("Current", "Default", "LastKnownGood"):
        num = None
        if select is not None:
            try:
                num = select.get_int(name)
            except RegfError:
                num = None
        if num and 0 < num < 1000:
            candidate = f"ControlSet{num:03d}"
            if _find(system, candidate) is not None:
                return candidate
    if _find(system, "ControlSet001") is not None:
        return "ControlSet001"
    raise RegfError("SYSTEM hive has no control set")


def mounted_devices(system: Hive) -> Dict[str, bytes]:
    """Drive letters from ``MountedDevices``: ``{"C:": <raw value>, "D:": ...}``."""
    out: Dict[str, bytes] = {}
    key = _find(system, "MountedDevices")
    if key is None:
        return out
    try:
        values = key.values()
    except RegfError as exc:
        log.warning("damaged MountedDevices key: %s", exc)
        return out
    for val in values:
        m = re.match(r"^\\DosDevices\\([A-Za-z]):$", val.name)
        if m and isinstance(val.raw, bytes):
            out[m.group(1).upper() + ":"] = val.raw
    return out


def _ccs_key(system: Hive, sub: str) -> Optional[Key]:
    try:
        ccs = current_control_set(system)
    except RegfError:
        return None
    return _find(system, f"{ccs}\\{sub}")


def hiberboot_enabled(system: Hive) -> Optional[bool]:
    """Fast Startup setting (``Control\\Session Manager\\Power\\HiberbootEnabled``), ``None`` if absent."""
    key = _ccs_key(system, "Control\\Session Manager\\Power")
    if key is None:
        return None
    try:
        val = key.get_int("HiberbootEnabled")
    except RegfError:
        return None
    return None if val is None else bool(val)


def computer_name(system: Hive) -> Optional[str]:
    key = _ccs_key(system, "Control\\ComputerName\\ComputerName")
    if key is None:
        return None
    try:
        name = key.get_str("ComputerName")
    except RegfError:
        return None
    return name.strip() if name else None


def windows_version(software: Hive) -> Dict[str, str]:
    """``{"caption": "Windows 11 Pro", "version": "10.0.26100", "display_version": "24H2", "text": ...}``.

    ``ProductName`` still says "Windows 10" on Windows 11, so builds >= 22000 are relabelled.
    """
    key = _find(software, CURRENT_VERSION)
    if key is None:
        return {"caption": "Windows", "version": "", "display_version": "", "text": "Windows"}
    try:
        product = (key.get_str("ProductName") or "Windows").strip()
        build = (key.get_str("CurrentBuildNumber") or key.get_str("CurrentBuild") or "").strip()
        major = key.get_int("CurrentMajorVersionNumber")
        minor = key.get_int("CurrentMinorVersionNumber")
        display = (key.get_str("DisplayVersion") or key.get_str("ReleaseId") or "").strip()
        legacy = (key.get_str("CurrentVersion") or "").strip()
    except RegfError:
        return {"caption": "Windows", "version": "", "display_version": "", "text": "Windows"}
    if build.isdigit() and int(build) >= 22000 and product.startswith("Windows 10"):
        product = "Windows 11" + product[len("Windows 10"):]
    if major is not None and minor is not None:
        version = f"{major}.{minor}.{build}" if build else f"{major}.{minor}"
    else:
        version = f"{legacy}.{build}" if legacy and build else (legacy or build)
    text = f"{product} {version}".strip()
    return {"caption": product, "version": version, "display_version": display, "text": text}


def wallpaper_settings(ntuser: Hive) -> Dict[str, Optional[str]]:
    """Desktop picture settings: ``wallpaper, style, tile`` (Control Panel) and ``policy_wallpaper,
    policy_style`` (``Policies\\System``, managed PCs; its style uses its own 0-5 enum)."""
    out: Dict[str, Optional[str]] = {"wallpaper": None, "style": None, "tile": None,
                                     "policy_wallpaper": None, "policy_style": None}
    desk = _find(ntuser, "Control Panel\\Desktop")
    pol = _find(ntuser, "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System")
    try:
        if desk is not None:
            out["wallpaper"] = desk.get_str("WallPaper")
            out["style"] = desk.get_str("WallpaperStyle")
            out["tile"] = desk.get_str("TileWallpaper")
        if pol is not None:
            out["policy_wallpaper"] = pol.get_str("Wallpaper")
            out["policy_style"] = pol.get_str("WallpaperStyle")
    except RegfError as exc:
        log.warning("damaged wallpaper settings: %s", exc)
    return out


def steam_paths(software: Optional[Hive] = None, ntuser: Optional[Hive] = None) -> List[str]:
    """Windows paths of the Steam client folder (``SteamPath`` uses forward slashes)."""
    found: List[str] = []
    candidates = []
    if ntuser is not None:
        candidates.append((ntuser, "Software\\Valve\\Steam", "SteamPath"))
    if software is not None:
        candidates.append((software, "WOW6432Node\\Valve\\Steam", "InstallPath"))
        candidates.append((software, "Valve\\Steam", "InstallPath"))
    for hive, path, name in candidates:
        key = _find(hive, path)
        if key is None:
            continue
        try:
            value = key.get_str(name)
        except RegfError:
            continue
        if value:
            value = value.replace("/", "\\")
            if value.lower() not in (f.lower() for f in found):
                found.append(value)
    return found
