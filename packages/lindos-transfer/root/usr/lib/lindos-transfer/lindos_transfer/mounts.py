"""Read-only mounting of Windows partitions, hibernation check, BitLocker help (SPEC-WINDOWS §29.3).

Safety rules (SPEC-WINDOWS §27.3, binding):

* Windows volumes are mounted **read-only only**: ``udisksctl mount -b <dev> -t ntfs -o ro`` when
  ntfs-3g's ``/sbin/mount.ntfs`` exists (it shows OneDrive online-only files as *unsupported
  reparse tag* links instead of zero-filled files), else ``-t ntfs3 -o ro`` (the copy engine then
  runs its mandatory placeholder detection).  Never ``rw``, ``force``, ``remove_hiberfile`` or
  ``recover`` -- :func:`assert_read_only` enforces that on every argv this module builds.
* udisks asks polkit for permission on internal disks; ``--no-user-interaction`` is never passed.
* After mounting, the first 4 bytes of ``hiberfil.sys`` are checked (``hibr``/``HIBR`` = Windows is
  hibernated or used Fast Startup).  Only those bytes are read (:func:`secrets.read_hiberfil_header`).
* BitLocker: Lindos only explains and prints commands; the user types the recovery key into
  udisks'/cryptsetup's/dislocker's own prompt.  Lindos never sees, stores or passes it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import TransferError, secrets, system_path

__all__ = [
    "NTFS3G_HELPERS",
    "FORBIDDEN_OPTIONS",
    "HIBERNATED_WARNING",
    "HIBERNATED_ADVICE",
    "RECOVERY_KEY_URL",
    "MountEntry",
    "ntfs3g_available",
    "choose_driver",
    "mount_command",
    "unmount_command",
    "assert_read_only",
    "parse_udisks_mounted",
    "mount_table",
    "find_mount",
    "driver_of",
    "is_read_only",
    "hiberfil_state",
    "bitlocker_guidance",
    "mount",
]

log = logging.getLogger("lindos-transfer.mounts")

#: ntfs-3g's mount helper (``mount -t ntfs`` uses it when present).
NTFS3G_HELPERS = ("/sbin/mount.ntfs", "/usr/sbin/mount.ntfs")
#: Options that would let a driver write to the Windows volume -- never emitted.
FORBIDDEN_OPTIONS = ("rw", "force", "remove_hiberfile", "recover", "remount")
RECOVERY_KEY_URL = "https://aka.ms/myrecoverykey"

HIBERNATED_WARNING = "Windows was hibernated (Fast Startup) - files may be slightly out of date"
HIBERNATED_ADVICE = (
    "Windows on this PC did not shut down fully (hibernation or Fast Startup), so every Windows "
    "drive of this PC may be slightly out of date. For the freshest copy, start Windows and choose "
    "Restart (Fast Startup doesn't apply to Restart) or run 'shutdown /s /t 0', then come back to "
    "Lindos. You can also continue: Lindos only reads, so nothing on Windows is changed."
)

_MOUNTED_RE = re.compile(r"^Mounted\s+(\S+)\s+at\s+(.+?)\s*$")


@dataclass
class MountEntry:
    """One line of ``/proc/self/mounts``."""

    device: str
    mountpoint: str
    fstype: str
    options: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def ntfs3g_available(exists: Callable[[str], bool] = os.path.exists) -> bool:
    """True when ntfs-3g's ``mount.ntfs`` helper is installed (LINDOS_ROOT-aware)."""
    return any(exists(system_path(p)) for p in NTFS3G_HELPERS)


def choose_driver(exists: Callable[[str], bool] = os.path.exists) -> str:
    """``"ntfs-3g"`` when available (preferred), else ``"ntfs3"``."""
    return "ntfs-3g" if ntfs3g_available(exists) else "ntfs3"


def assert_read_only(argv: Sequence[str]) -> None:
    """Raise :class:`TransferError` unless *argv* mounts read-only and carries no write option."""
    if "-o" not in argv:
        raise TransferError("refusing to mount without the read-only option")
    opts: List[str] = []
    for i, tok in enumerate(argv):
        if tok in ("-o", "--options") and i + 1 < len(argv):
            opts.extend(o.strip().lower() for o in argv[i + 1].split(","))
    if "ro" not in opts:
        raise TransferError("refusing to mount a Windows drive without 'ro' (read-only)")
    bad = [o for o in opts if o.split("=")[0] in FORBIDDEN_OPTIONS]
    if bad:
        raise TransferError(f"refusing unsafe mount option(s): {', '.join(bad)}")
    if "--no-user-interaction" in argv:
        raise TransferError("internal error: the permission prompt must not be suppressed")


def mount_command(device: str, driver: str) -> List[str]:
    """``udisksctl mount`` argv for *device* with *driver* (``ntfs-3g`` or ``ntfs3``), read-only."""
    fstype = "ntfs" if driver == "ntfs-3g" else "ntfs3"
    argv = ["udisksctl", "mount", "-b", device, "-t", fstype, "-o", "ro"]
    assert_read_only(argv)
    return argv


def unmount_command(device: str) -> List[str]:
    return ["udisksctl", "unmount", "-b", device]


def parse_udisks_mounted(text: str) -> Optional[str]:
    """Mount point from udisksctl's (unlocalised) ``Mounted <dev> at <dir>`` line."""
    for line in (text or "").splitlines():
        m = _MOUNTED_RE.match(line.strip())
        if m:
            where = m.group(2)
            if where.endswith(".") and not os.path.isdir(where):
                where = where[:-1]
            return where
    return None


# --------------------------------------------------------------------------- #
# mount table
# --------------------------------------------------------------------------- #
def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_table(text: Optional[str] = None) -> List[MountEntry]:
    """Parse ``/proc/self/mounts`` (or *text*); LINDOS_ROOT-aware; empty when unavailable."""
    if text is None:
        path = system_path("/proc/self/mounts")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
    out: List[MountEntry] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        out.append(MountEntry(_unescape(parts[0]), _unescape(parts[1]), parts[2], parts[3].split(",")))
    return out


def find_mount(*, device: Optional[str] = None, path: Optional[str] = None,
               table: Optional[List[MountEntry]] = None) -> Optional[MountEntry]:
    """Mount entry for *device*, or the entry whose mount point contains *path* (longest match)."""
    entries = mount_table() if table is None else table
    if device:
        for e in reversed(entries):
            if e.device == device:
                return e
        return None
    if path:
        target = os.path.normpath(str(path)).replace("\\", "/")
        best: Optional[MountEntry] = None
        for e in entries:
            mp = e.mountpoint.rstrip("/") or "/"
            if target == mp or target.startswith(mp + "/") or mp == "/":
                if best is None or len(mp) >= len(best.mountpoint.rstrip("/") or "/"):
                    best = e
        return best
    return None


def driver_of(entry: Optional[MountEntry]) -> str:
    """``ntfs-3g`` (FUSE ``fuseblk``/``ntfs-3g``), ``ntfs3``, ``ntfs`` or ``unknown``."""
    if entry is None:
        return "unknown"
    fs = entry.fstype.lower()
    if fs in ("fuseblk", "ntfs-3g", "fuse.ntfs-3g", "lowntfs-3g", "fuse.lowntfs-3g"):
        return "ntfs-3g"
    if fs == "ntfs3":
        return "ntfs3"
    if fs == "ntfs":
        return "ntfs"
    return "unknown"


def is_read_only(entry: Optional[MountEntry]) -> Optional[bool]:
    if entry is None:
        return None
    return "ro" in entry.options


# --------------------------------------------------------------------------- #
# hibernation
# --------------------------------------------------------------------------- #
def _ci_child(parent: Path, name: str) -> Optional[Path]:
    want = name.lower()
    try:
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name.lower() == want:
                    return parent / entry.name
    except OSError:
        return None
    return None


def hiberfil_state(root: os.PathLike) -> str:
    """``hibernated`` | ``clean`` | ``absent`` | ``unknown`` from the first 4 bytes of ``hiberfil.sys``.

    Windows pre-allocates ``hiberfil.sys`` whenever hibernation/Fast Startup is enabled, so its
    mere presence means nothing; only the ``hibr``/``HIBR`` signature does (as ntfs-3g checks).
    """
    hib = _ci_child(Path(root), "hiberfil.sys")
    if hib is None:
        return "absent"
    try:
        head = secrets.read_hiberfil_header(hib, 4)
    except (OSError, ValueError) as exc:
        log.debug("cannot read %s header: %s", hib, exc)
        return "unknown"
    if len(head) < 4:
        return "unknown"
    return "hibernated" if head in (b"hibr", b"HIBR") else "clean"


# --------------------------------------------------------------------------- #
# BitLocker
# --------------------------------------------------------------------------- #
def bitlocker_guidance(device: str) -> Dict[str, Any]:
    """Plain-language BitLocker help with copy-pasteable commands (Lindos never handles the key)."""
    return {
        "device": device,
        "summary": ("This Windows drive is encrypted with BitLocker. Lindos can read it after you "
                    "unlock it with your 48-digit BitLocker recovery key. You type the key into the "
                    "unlock prompt yourself; Lindos never sees or stores it."),
        "steps": [
            {"what": "Unlock it read-only in a terminal (it asks for the recovery key itself)",
             "command": ["udisksctl", "unlock", "-b", device, "--read-only"]},
            {"what": "Or click the drive in the file manager and type the key into its unlock window",
             "command": []},
            {"what": "Then mount the unlocked drive read-only (use the /dev/dm-N name udisksctl printed)",
             "command": ["lindos-transfer", "mount", "/dev/dm-N"]},
        ],
        "clear_key": {
            "why": ("If Windows set up device encryption but never saved a recovery key (PCs with a "
                    "local account only) or BitLocker is suspended, Ubuntu 24.04's cryptsetup cannot "
                    "open it. dislocker can, read-only:"),
            "command": ["sudo", "dislocker", "-r", "-c", device, "/mnt/bitlocker"],
            "then": ["sudo", "mount", "-o", "ro,loop", "/mnt/bitlocker/dislocker-file", "/mnt/windows"],
        },
        "recovery_key_url": RECOVERY_KEY_URL,
        "notes": [
            "Find your recovery key at " + RECOVERY_KEY_URL + " (sign in with the Microsoft account "
            "you used on that PC), or ask your organisation's IT if it is a work PC.",
            "Unlocking with the PC's security chip (TPM) alone is only possible inside Windows.",
            "Easiest alternative: run the Lindos transfer kit on Windows itself "
            "(lindos-transfer make-usb-kit <USB folder>).",
        ],
    }


# --------------------------------------------------------------------------- #
# mounting
# --------------------------------------------------------------------------- #
def _looks_windows(root: Path) -> bool:
    return _ci_child(root, "Windows") is not None and _ci_child(root, "Users") is not None


def mount(device: str, *, run: Callable[..., Any] = subprocess.run,
          which: Callable[[str], Optional[str]] = shutil.which,
          exists: Callable[[str], bool] = os.path.exists,
          table: Optional[Callable[[], List[MountEntry]]] = None,
          fstype: Optional[str] = None) -> Dict[str, Any]:
    """Mount *device* read-only through udisks (or reuse an existing mount) and inspect it.

    Returns ``{"device","mountpoint","driver","read_only","already_mounted","windows",
    "hibernated","hibernation","notes","command"}``.  Raises :class:`TransferError` with a
    plain-language message (BitLocker, missing udisks, udisks error).
    """
    get_table = table or mount_table
    if fstype and fstype.lower() == "bitlocker":
        raise TransferError(bitlocker_guidance(device)["summary"])
    notes: List[str] = []
    entry = find_mount(device=device, table=get_table())
    command: List[str] = []
    already = entry is not None
    if entry is None:
        if not which("udisksctl"):
            raise TransferError("udisksctl is missing - install the 'udisks2' package to mount drives "
                                "(sudo apt install udisks2)")
        driver = choose_driver(exists)
        command = mount_command(device, driver)
        log.info("mounting %s read-only (%s): %s", device, driver, " ".join(command))
        try:
            proc = run(command, capture_output=True, text=True, timeout=180, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransferError(f"could not run udisksctl: {exc}") from exc
        out = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
        if getattr(proc, "returncode", 1) != 0:
            if "AlreadyMounted" not in out:
                low = out.lower()
                if "not authorized" in low or "dismissed" in low or "cancel" in low:
                    raise TransferError("Permission to mount the drive was not given.")
                raise TransferError(f"Could not mount {device} read-only: {out.strip() or 'udisksctl failed'}")
        entry = find_mount(device=device, table=get_table())
        if entry is None:
            where = parse_udisks_mounted(out)
            if not where:
                raise TransferError(f"{device} was mounted but its folder could not be found")
            entry = MountEntry(device, where, "ntfs3" if driver == "ntfs3" else "fuseblk", ["ro"])
    mountpoint = Path(entry.mountpoint)
    ro = is_read_only(entry)
    if already and ro is False:
        notes.append("This drive was already opened with write access by another program. Lindos "
                     "only reads from it, but for safety unmount it in the file manager and let "
                     "Lindos mount it read-only.")
    hib = hiberfil_state(mountpoint)
    if hib == "hibernated":
        notes.append(HIBERNATED_ADVICE)
    windows = _looks_windows(mountpoint)
    if not windows:
        notes.append("No Windows installation on this drive (no Windows and Users folders). "
                     "You can still copy files from it with the file manager.")
    return {
        "device": device,
        "mountpoint": str(mountpoint),
        "driver": driver_of(entry),
        "read_only": ro,
        "already_mounted": already,
        "windows": windows,
        "hibernated": hib == "hibernated",
        "hibernation": hib,
        "notes": notes,
        "command": command,
    }
