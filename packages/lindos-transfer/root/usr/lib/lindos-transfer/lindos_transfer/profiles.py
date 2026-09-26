"""Windows users and their folders (SPEC-WINDOWS §29.4).

Users come from ``SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\<SID>\\ProfileImagePath``
(profile *folder* names are not account names: renaming an account never renames its folder),
skipping built-in/system profiles and service SIDs; if that hive is unreadable, from ``Users/*``
minus the same skip list.  Only profiles with an ``NTUSER.DAT`` are offered.

Each known folder is resolved from the user's own ``NTUSER.DAT`` ``User Shell Folders`` **by its own
value name** (legacy names ``Desktop``, ``Personal``, ``My Music``, ``My Pictures``, ``My Video``,
``Favorites``; GUID names for Downloads and Saved Games; the "Local*" GUIDs are never used), with
``%USERPROFILE%`` & co. expanded, drive letters mapped through ``SYSTEM\\MountedDevices`` and OneDrive
Known-Folder-Move paths honoured.  Anything unresolvable falls back to the default folder.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import TransferError, winreg
from .regf import Hive
from .sources import Source, ci_child, ci_path, is_link

__all__ = [
    "SKIP_PROFILE_NAMES",
    "KNOWN_FOLDERS",
    "LOCAL_FOLDER_GUIDS",
    "FolderInfo",
    "UserProfile",
    "list_users",
    "find_user",
    "open_ntuser",
    "decode_mounted_device",
    "drive_map",
    "expand_vars",
    "windows_path_to_local",
    "resolve_folders",
    "onedrive_roots",
]

log = logging.getLogger("lindos-transfer.profiles")

#: Profile folders never offered (built-in, system, OOBE temporary and sandbox accounts).
SKIP_PROFILE_NAMES = frozenset({
    "default", "default user", "public", "all users", "defaultuser0", "defaultuser100000",
    "wdagutilityaccount", "defaultaccount", "guest", "wsiaccount", "systemprofile",
    "localservice", "networkservice", "administrator.default",
})

#: category -> (legacy value name or None, own KNOWNFOLDERID value name, default folder under the profile)
KNOWN_FOLDERS: Dict[str, Tuple[Optional[str], str, str]] = {
    "desktop": ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    "documents": ("Personal", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
    "downloads": (None, "{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "music": ("My Music", "{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
    "pictures": ("My Pictures", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    "videos": ("My Video", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
    "saved-games": (None, "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}", "Saved Games"),
    "favorites": ("Favorites", "{1777F761-68AD-4D8A-87BD-30B759FA33DD}", "Favorites"),
}

#: "Local" duplicates of known folders -- separate known folders that must never override the real ones.
LOCAL_FOLDER_GUIDS = frozenset({
    "{f42ee2d3-909f-4907-8871-4c22fc0bf756}", "{0ddd015d-b06c-45d5-8c4c-f59713854639}",
    "{7d83ee9b-2244-4e70-b1f5-5393042af1e4}", "{a0c69a99-21c8-4671-8703-7934162fcf1d}",
    "{35286a68-3c57-41a1-bbb1-0eae73d76c95}",
})

_USER_SID_RE = re.compile(r"^S-1-(5-21|12-1)-", re.IGNORECASE)
_LSBLK_IDS = ["lsblk", "-J", "-b", "-o", "PATH,PARTUUID,PTUUID,START,MOUNTPOINT,FSTYPE"]


@dataclass
class FolderInfo:
    """Where one known folder of a user lives."""

    category: str
    windows_path: str = ""
    path: Optional[Path] = None
    source: str = "default"          # "registry" | "default" | "bundle"
    onedrive: bool = False
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"category": self.category, "windows_path": self.windows_path,
                "path": str(self.path) if self.path else None, "source": self.source,
                "onedrive": self.onedrive, "note": self.note}


@dataclass
class UserProfile:
    """A Windows user profile on the source."""

    name: str                                   # profile folder name (shown to the user)
    sid: str = ""
    windows_profile: str = ""                   # e.g. C:\\Users\\alice
    profile_dir: Optional[Path] = None          # local path of the profile folder
    ntuser: Optional[Path] = None
    stale: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "sid": self.sid, "windows_profile": self.windows_profile,
                "profile": str(self.profile_dir) if self.profile_dir else None,
                "has_ntuser": self.ntuser is not None, "stale": self.stale, "notes": list(self.notes)}


# --------------------------------------------------------------------------- #
# drive letters
# --------------------------------------------------------------------------- #
def decode_mounted_device(raw: bytes) -> Optional[Tuple[str, ...]]:
    """``("gpt", partuuid)`` or ``("mbr", disk-signature-hex, byte-offset)`` from a MountedDevices value."""
    if len(raw) == 12:
        sig = int.from_bytes(raw[0:4], "little")
        offset = int.from_bytes(raw[4:12], "little")
        return ("mbr", f"{sig:08x}", str(offset))
    if raw.startswith(b"DMIO:ID:") and len(raw) >= 24:
        return ("gpt", str(uuid.UUID(bytes_le=bytes(raw[8:24]))))
    return None


def _lsblk_ids(run: Callable[..., Any]) -> List[Dict[str, Any]]:
    try:
        proc = run(_LSBLK_IDS, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(proc, "returncode", 1) != 0:
        return []
    try:
        data = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []

    def walk(devs: Any, ptuuid: str = "") -> None:
        for d in devs or []:
            if not isinstance(d, dict):
                continue
            pt = str(d.get("ptuuid") or ptuuid or "")
            out.append({"path": d.get("path"), "partuuid": str(d.get("partuuid") or "").lower(),
                        "ptuuid": pt.lower(), "start": d.get("start"), "mountpoint": d.get("mountpoint")})
            walk(d.get("children"), pt)

    walk(data.get("blockdevices"))
    return out


def drive_map(source: Source, *, run: Callable[..., Any] = subprocess.run) -> Dict[str, Path]:
    """``{"C:": <source root>, "D:": <mount point>, ...}`` for drive letters that are mounted."""
    drives: Dict[str, Path] = {"C:": source.root}
    if source.is_bundle:
        return drives
    system = source.hive("SYSTEM")
    if system is None:
        return drives
    letters = winreg.mounted_devices(system)
    if not letters:
        return drives
    devices = _lsblk_ids(run)
    root_letter = None
    for letter, raw in sorted(letters.items()):
        ident = decode_mounted_device(raw)
        if ident is None:
            continue
        for dev in devices:
            match = False
            if ident[0] == "gpt":
                match = dev["partuuid"] == ident[1]
            elif ident[0] == "mbr":
                try:
                    match = dev["ptuuid"] == ident[1] and int(dev["start"] or -1) * 512 == int(ident[2])
                except (TypeError, ValueError):
                    match = False
            if not match:
                continue
            mp = dev.get("mountpoint")
            if mp:
                if Path(mp) == source.root:
                    root_letter = letter
                drives.setdefault(letter, Path(mp))
            else:
                drives.setdefault(letter + "unmounted", Path(str(dev.get("path") or "")))
            break
    if root_letter and root_letter != "C:":
        drives[root_letter] = source.root
    return drives


# --------------------------------------------------------------------------- #
# path expansion
# --------------------------------------------------------------------------- #
def expand_vars(value: str, env: Dict[str, str]) -> str:
    """Expand ``%NAME%`` (case-insensitive) with *env*; unknown variables are kept."""
    lookup = {k.lower(): v for k, v in env.items()}

    def repl(m: "re.Match[str]") -> str:
        return lookup.get(m.group(1).lower(), m.group(0))

    for _ in range(3):  # variables may refer to other variables (e.g. %OneDrive% -> %USERPROFILE%)
        new = re.sub(r"%([^%\\/]+)%", repl, value)
        if new == value:
            break
        value = new
    return value


def _base_env(user: UserProfile, user_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    prof = user.windows_profile or f"C:\\Users\\{user.name}"
    drive = prof[:2] if re.match(r"^[A-Za-z]:", prof) else "C:"
    env = {
        "SystemDrive": "C:", "SystemRoot": "C:\\Windows", "windir": "C:\\Windows",
        "ProgramData": "C:\\ProgramData", "ALLUSERSPROFILE": "C:\\ProgramData",
        "PUBLIC": "C:\\Users\\Public", "ProgramFiles": "C:\\Program Files",
        "ProgramFiles(x86)": "C:\\Program Files (x86)",
        "USERPROFILE": prof, "HOMEDRIVE": drive, "HOMEPATH": prof[len(drive):] or "\\",
        "APPDATA": prof + "\\AppData\\Roaming", "LOCALAPPDATA": prof + "\\AppData\\Local",
        "USERNAME": user.name,
    }
    for k, v in (user_env or {}).items():
        if k.lower() not in ("path", "temp", "tmp"):
            env[k] = v
    return env


def windows_path_to_local(win_path: str, drives: Dict[str, Path]) -> Tuple[Optional[Path], str]:
    """Map ``D:\\Folder\\x`` to a local path through *drives*: ``(path or None, reason if None)``."""
    text = (win_path or "").strip().strip('"')
    if text.startswith("\\\\?\\"):
        text = text[4:]
    if text.startswith("\\\\") or text.startswith("//"):
        return None, "is a network folder, not on this computer's disk"
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", text)
    if not m:
        return None, "has an unusual location Lindos cannot follow"
    letter = m.group(1).upper() + ":"
    base = drives.get(letter)
    if base is None:
        dev = drives.get(letter + "unmounted")
        if dev is not None:
            return None, (f"is on drive {letter}, which is not open yet - run: "
                          f"lindos-transfer mount {dev}")
        return None, f"is on drive {letter}, which Lindos cannot see"
    local = ci_path(base, m.group(2))
    if local is None:
        link = ci_path(base, m.group(2), allow_final_link=True)
        if link is not None and is_link(link):
            return None, "is a link Lindos does not follow (it may be an online-only OneDrive folder)"
        return None, "was not found on the drive"
    return local, ""


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def _skip_name(name: str) -> bool:
    low = name.lower()
    return low in SKIP_PROFILE_NAMES or low.startswith("defaultuser") or low.startswith(".")


def _partition_users(source: Source) -> List[UserProfile]:
    users: Dict[str, UserProfile] = {}
    software = source.hive("SOFTWARE")
    drives = {"C:": source.root}
    if software is not None:
        entries = winreg.profile_list(software)
        entries.sort(key=lambda e: e["backup"])  # real entries before ".bak" ones
        for e in entries:
            sid = e["sid"]
            if e["service"] or not _USER_SID_RE.match(sid):
                continue
            raw = expand_vars(e["profile_image_path"], {"SystemDrive": "C:", "SystemRoot": "C:\\Windows"})
            folder = re.split(r"[\\/]", raw.rstrip("\\/"))[-1]
            if not folder or _skip_name(folder) or folder.lower() in users:
                continue
            local, why = windows_path_to_local(raw, drives)
            prof = UserProfile(name=folder, sid=sid, windows_profile=raw, profile_dir=local,
                               stale=software.stale)
            if local is None:
                log.info("profile %s %s", raw, why)
                continue
            if e["backup"]:
                prof.notes.append("Windows marked this profile as a backup copy (it may be incomplete).")
            nt = ci_path(local, "NTUSER.DAT")
            if nt is None or not nt.is_file():
                continue
            prof.ntuser = nt
            users[folder.lower()] = prof
    if not users:
        users_dir = source.path("Users")
        if users_dir is not None:
            try:
                children = sorted(users_dir.iterdir())
            except OSError:
                children = []
            for child in children:
                if is_link(child) or not child.is_dir() or _skip_name(child.name):
                    continue
                nt = ci_path(child, "NTUSER.DAT")
                if nt is None or not nt.is_file():
                    continue
                users[child.name.lower()] = UserProfile(
                    name=child.name, windows_profile=f"C:\\Users\\{child.name}", profile_dir=child, ntuser=nt,
                    notes=["Found by folder name (the Windows user list could not be read)."])
    return sorted(users.values(), key=lambda u: u.name.lower())


def list_users(source: Source) -> List[UserProfile]:
    """Users on *source* (a bundle has exactly its one user)."""
    if source.is_bundle:
        user = source.manifest.get("user") or {}
        name = str(user.get("name") or "user")
        prof = str(user.get("profile") or f"C:\\Users\\{name}")
        return [UserProfile(name=name, windows_profile=prof, profile_dir=None)]
    return _partition_users(source)


def find_user(source: Source, name: Optional[str] = None) -> UserProfile:
    """The user called *name* (folder name or SID, case-insensitive); the only user if *name* is None."""
    users = list_users(source)
    if not users:
        raise TransferError("No Windows user profiles were found on this drive.")
    if name:
        want = name.lower()
        for u in users:
            if u.name.lower() == want or (u.sid and u.sid.lower() == want):
                return u
        raise TransferError(f"No Windows user '{name}'. Users on this drive: "
                            + ", ".join(u.name for u in users))
    if len(users) == 1:
        return users[0]
    raise TransferError("This drive has several Windows users; choose one with --user: "
                        + ", ".join(u.name for u in users))


def open_ntuser(user: UserProfile) -> Optional[Hive]:
    """The user's ``NTUSER.DAT`` (read-only), or ``None``."""
    hive = winreg.try_open_hive(user.ntuser) if user.ntuser else None
    if hive is not None and hive.stale:
        user.stale = True
    return hive


# --------------------------------------------------------------------------- #
# folders
# --------------------------------------------------------------------------- #
def _lookup(values: Dict[str, str], name: str) -> Optional[str]:
    want = name.lower()
    for k, v in values.items():
        if k.lower() == want:
            return v
    return None


def onedrive_roots(source: Source, user: UserProfile, ntuser: Optional[Hive],
                   drives: Dict[str, Path]) -> List[FolderInfo]:
    """The user's OneDrive folders (personal and work/school)."""
    if source.is_bundle:
        rel = (source.manifest.get("folders") or {}).get("onedrive")
        if rel:
            p = source.path(rel)
            return [FolderInfo("onedrive", rel, p, "bundle", True, "" if p else "missing from the transfer folder")]
        return []
    env = _base_env(user, winreg.user_environment(ntuser) if ntuser else None)
    raw: List[str] = []
    if ntuser is not None:
        raw.extend(winreg.onedrive_folders(ntuser))
        uenv = winreg.user_environment(ntuser)
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            v = _lookup(uenv, name)
            if v:
                raw.append(v)
    out: List[FolderInfo] = []
    seen: set = set()
    for value in raw:
        win = expand_vars(value, env)
        if win.lower() in seen:
            continue
        seen.add(win.lower())
        local, why = windows_path_to_local(win, drives)
        out.append(FolderInfo("onedrive", win, local, "registry", True, "" if local else f"OneDrive folder {why}"))
    if not out and user.profile_dir is not None:
        try:
            children = sorted(user.profile_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.name.lower().startswith("onedrive"):
                win = f"{user.windows_profile}\\{child.name}"
                if is_link(child):
                    out.append(FolderInfo("onedrive", win, None, "default", True,
                                          "This OneDrive folder is managed by the OneDrive app and cannot "
                                          "be read from Linux (its files are online-only here)."))
                elif child.is_dir():
                    out.append(FolderInfo("onedrive", win, child, "default", True, ""))
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_folders(source: Source, user: UserProfile, *, ntuser: Optional[Hive] = None,
                    drives: Optional[Dict[str, Path]] = None) -> Dict[str, FolderInfo]:
    """Known folders of *user*: ``{category: FolderInfo}`` (every KNOWN_FOLDERS category present)."""
    out: Dict[str, FolderInfo] = {}
    if source.is_bundle:
        folders = source.manifest.get("folders") or {}
        for cat in KNOWN_FOLDERS:
            rel = folders.get(cat)
            if not rel:
                out[cat] = FolderInfo(cat, "", None, "bundle", False, "not in the transfer folder")
                continue
            p = source.path(rel)
            out[cat] = FolderInfo(cat, rel, p, "bundle", False, "" if p else "missing from the transfer folder")
        return out
    drives = drives if drives is not None else {"C:": source.root}
    usf = winreg.user_shell_folders(ntuser) if ntuser is not None else {}
    uenv = winreg.user_environment(ntuser) if ntuser is not None else {}
    env = _base_env(user, uenv)
    od_roots = [f.path for f in onedrive_roots(source, user, ntuser, drives) if f.path is not None]
    for cat, (legacy, guid, default) in KNOWN_FOLDERS.items():
        raw = _lookup(usf, legacy) if legacy else None
        if raw is None:
            raw = _lookup(usf, guid)
        info = FolderInfo(cat)
        if raw:
            win = expand_vars(raw, env)
            info.windows_path = win
            local, why = windows_path_to_local(win, drives)
            if local is not None and local.is_dir():
                info.path, info.source = local, "registry"
            else:
                info.note = f"Windows keeps this folder at {win}, which {why or 'is not a folder'}."
        if info.path is None and user.profile_dir is not None:
            p = ci_path(user.profile_dir, default)
            if p is not None and p.is_dir():
                info.path = p
                info.source = "default"
                if not info.windows_path:
                    info.windows_path = f"{user.windows_profile}\\{default}"
                if info.note:
                    info.note += " Using the default folder instead."
            elif not info.note:
                info.note = "This folder does not exist."
        if info.path is not None and any(_inside(info.path, r) for r in od_roots):
            info.onedrive = True
        out[cat] = info
    return out


def public_folder(source: Source) -> Optional[Path]:
    """``Users/Public`` of a partition (offered by nothing yet; kept for callers/tests)."""
    if source.is_bundle:
        return None
    users = source.path("Users")
    return ci_child(users, "Public") if users is not None else None
