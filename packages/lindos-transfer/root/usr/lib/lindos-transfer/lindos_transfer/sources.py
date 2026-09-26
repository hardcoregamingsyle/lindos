"""Where the Windows data comes from (SPEC-WINDOWS §29.2, §29.3, §29.11).

Two kinds of source:

* **partition** -- a mounted Windows system drive (has ``Windows/`` and ``Users/``, matched
  case-insensitively).  Found with ``lsblk`` (FSTYPE ``ntfs``; ``BitLocker`` = locked; ReFS, dynamic
  disks and Storage Spaces are explained), mounted read-only by :mod:`lindos_transfer.mounts`.
* **bundle** -- a *transfer folder* written by the Windows kit: it holds ``lindos-transfer.json``
  (written by PowerShell 5.1 with a UTF-8 BOM, so it is read with ``utf-8-sig``).  Removable media
  and ``$HOME`` are scanned for bundles (depth <= 2).

:func:`open_source` turns a ``--from`` path into a :class:`Source`, the object every other module
works with.  Paths inside a Windows tree are resolved case-insensitively and **never through a
symlink/junction** (:func:`ci_path`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import TransferError, mounts, secrets, system_path, user_home, winreg
from .regf import Hive

__all__ = [
    "MANIFEST_NAME",
    "LSBLK_ARGV",
    "MAX_MANIFEST_BYTES",
    "Source",
    "ci_child",
    "ci_path",
    "is_link",
    "is_windows_root",
    "is_bundle",
    "read_manifest",
    "parse_lsblk",
    "classify_device",
    "list_partitions",
    "bundle_search_roots",
    "find_bundles",
    "detect_sources",
    "open_source",
    "windows_to_parts",
]

log = logging.getLogger("lindos-transfer.sources")

MANIFEST_NAME = "lindos-transfer.json"
LSBLK_ARGV = ["lsblk", "-J", "-b", "-o", "NAME,PATH,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINT,TYPE,PARTTYPE,RO"]
MAX_MANIFEST_BYTES = 8 << 20

# GPT / MBR partition types that are never a user's Windows drive.
_SKIP_PARTTYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",   # EFI system partition
    "e3c9e316-0b5c-4db8-817d-f92df00215ae",   # Microsoft reserved
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",   # Windows Recovery Environment
    "0x27",                                   # MBR hidden NTFS (WinRE)
    "0xef",                                   # MBR EFI
}
_LDM_PARTTYPES = {
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3",   # LDM metadata (dynamic disk)
    "af9b60a0-1431-4f62-bc68-3311714a69ad",   # LDM data (dynamic disk)
    "0x42",                                   # MBR dynamic disk
}
_STORAGE_SPACES_PARTTYPES = {"e75caf8f-f680-4cee-afa3-b001e56efc2d"}

NOTE_BITLOCKER = ("Encrypted with BitLocker. Unlock it with your recovery key first (run "
                  "'lindos-transfer mount {dev}' for step-by-step help), or use the Windows transfer kit.")
NOTE_REFS = ("ReFS drive (Windows Dev Drive) - Linux cannot read it. Use the Windows transfer kit "
             "(lindos-transfer make-usb-kit) to copy these files from Windows.")
NOTE_LDM = ("Windows dynamic disk - not supported by Lindos Transfer. Use the Windows transfer kit "
            "(lindos-transfer make-usb-kit) to copy these files from Windows.")
NOTE_SPACES = ("Windows Storage Spaces pool - Linux cannot read it. Use the Windows transfer kit "
               "(lindos-transfer make-usb-kit) to copy these files from Windows.")
NOTE_UNMOUNTED = "Not opened yet - Lindos will open it read-only (nothing on Windows is changed)."


# --------------------------------------------------------------------------- #
# case-insensitive, link-refusing path resolution
# --------------------------------------------------------------------------- #
def is_link(path: Union[str, os.PathLike]) -> bool:
    """True for symlinks (ntfs-3g/ntfs3 show junctions and reparse points as symlinks)."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def ci_child(parent: Union[str, os.PathLike], name: str) -> Optional[Path]:
    """The entry of *parent* named *name* (exact first, then case-insensitive), or ``None``."""
    parent = Path(parent)
    exact = parent / name
    try:
        os.lstat(exact)
        return exact
    except FileNotFoundError:
        pass
    except OSError:
        return None
    want = name.casefold()
    try:
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name.casefold() == want:
                    return parent / entry.name
    except OSError:
        return None
    return None


def windows_to_parts(rel: Union[str, Sequence[str]]) -> List[str]:
    """Split a relative path on both ``/`` and ``\\``; ``..`` is refused (``ValueError``)."""
    if isinstance(rel, str):
        parts = re.split(r"[\\/]+", rel)
    else:
        parts = [p for r in rel for p in re.split(r"[\\/]+", str(r))]
    out: List[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == ".." or "\x00" in part:
            raise ValueError(f"unsafe path component {part!r}")
        out.append(part)
    return out


def ci_path(base: Union[str, os.PathLike], rel: Union[str, Sequence[str]] = "", *,
            allow_final_link: bool = False) -> Optional[Path]:
    """Resolve *rel* below *base* case-insensitively, never passing through a link.

    Returns ``None`` when a component is missing, when any intermediate component is a
    symlink/junction, or (unless *allow_final_link*) when the result itself is a link.
    """
    try:
        parts = windows_to_parts(rel)
    except ValueError:
        return None
    cur = Path(base)
    for i, part in enumerate(parts):
        nxt = ci_child(cur, part)
        if nxt is None:
            return None
        last = i == len(parts) - 1
        if is_link(nxt) and not (last and allow_final_link):
            return None
        cur = nxt
    return cur


# --------------------------------------------------------------------------- #
# recognising sources
# --------------------------------------------------------------------------- #
def is_windows_root(path: Union[str, os.PathLike]) -> bool:
    """A mounted Windows system drive: real ``Windows/`` and ``Users/`` folders (any case)."""
    p = Path(path)
    win = ci_path(p, "Windows")
    users = ci_path(p, "Users")
    return bool(win and users and win.is_dir() and users.is_dir())


def is_bundle(path: Union[str, os.PathLike]) -> bool:
    """A transfer folder (contains ``lindos-transfer.json``)."""
    man = ci_path(Path(path), MANIFEST_NAME)
    return bool(man and man.is_file())


def _as_list(value: Any) -> List[Any]:
    """PowerShell 5.1's ConvertTo-Json unwraps 1-element arrays: accept an object as a list of one."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def read_json_file(path: Union[str, os.PathLike], *, limit: int = MAX_MANIFEST_BYTES) -> Any:
    """Read a JSON file written on Windows (UTF-8 with or without BOM, or UTF-16 with BOM)."""
    raw = secrets.read_bytes(path, limit)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    return json.loads(text)


def read_manifest(bundle: Union[str, os.PathLike]) -> Dict[str, Any]:
    """Load and validate ``lindos-transfer.json`` (schema 1); raises :class:`TransferError`."""
    man = ci_path(Path(bundle), MANIFEST_NAME)
    if man is None:
        raise TransferError(f"{bundle} is not a transfer folder (no {MANIFEST_NAME})")
    try:
        data = read_json_file(man)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TransferError(f"The transfer folder's {MANIFEST_NAME} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise TransferError(f"{MANIFEST_NAME} is not a Lindos transfer manifest")
    schema = data.get("schema")
    if schema != 1:
        raise TransferError(f"{MANIFEST_NAME} has schema {schema!r}; this Lindos understands schema 1. "
                            "Make the transfer folder again with the kit from this Lindos version.")
    out: Dict[str, Any] = dict(data)
    for key in ("computer", "created", "tool", "tool_version", "wallpaper", "wallpaper_style",
                "fonts", "apps", "winget_export"):
        if key in out and out[key] is not None and not isinstance(out[key], str):
            out[key] = str(out[key])
    user = out.get("user")
    out["user"] = user if isinstance(user, dict) else ({"name": str(user)} if user else {})
    win = out.get("windows")
    out["windows"] = win if isinstance(win, dict) else ({"caption": str(win)} if win else {})
    folders = out.get("folders")
    out["folders"] = {str(k): str(v) for k, v in folders.items()} if isinstance(folders, dict) else {}
    out["browsers"] = [b for b in _as_list(out.get("browsers")) if isinstance(b, dict)]
    out["skipped"] = [s for s in _as_list(out.get("skipped")) if isinstance(s, dict)]
    wifi = out.get("wifi")
    out["wifi"] = wifi if isinstance(wifi, dict) else {}
    steam = out.get("steam")
    out["steam"] = steam if isinstance(steam, dict) else {}
    return out


# --------------------------------------------------------------------------- #
# partitions (lsblk)
# --------------------------------------------------------------------------- #
def _flatten(devs: Iterable[Dict[str, Any]], parent: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for dev in devs or []:
        if not isinstance(dev, dict):
            continue
        entry = {k: v for k, v in dev.items() if k != "children"}
        entry["parent"] = parent
        out.append(entry)
        out.extend(_flatten(dev.get("children") or [], str(dev.get("name") or parent)))
    return out


def parse_lsblk(text: str) -> List[Dict[str, Any]]:
    """Flatten ``lsblk -J`` output (children included, each with its ``parent`` name)."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    return _flatten(data.get("blockdevices") or [])


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def classify_device(dev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one lsblk row into a ``sources --json`` partition entry, or ``None`` when irrelevant."""
    fstype = str(dev.get("fstype") or "")
    parttype = str(dev.get("parttype") or "").lower()
    path = str(dev.get("path") or (f"/dev/{dev.get('name')}" if dev.get("name") else ""))
    if not path or parttype in _SKIP_PARTTYPES:
        return None
    kind = fstype.lower()
    base = {
        "device": path,
        "label": str(dev.get("label") or ""),
        "size": _int(dev.get("size")),
        "fstype": fstype,
        "mountpoint": str(dev.get("mountpoint") or ""),
        "windows": False,
        "bitlocker": False,
        "hibernated": False,
        "note": "",
        "disk": str(dev.get("parent") or ""),
    }
    if kind == "ntfs":
        return base
    if kind == "bitlocker":
        base["bitlocker"] = True
        base["note"] = NOTE_BITLOCKER.format(dev=path)
        return base
    if kind == "refs":
        base["note"] = NOTE_REFS
        return base
    if parttype in _LDM_PARTTYPES:
        base["note"] = NOTE_LDM
        return base
    if parttype in _STORAGE_SPACES_PARTTYPES:
        base["note"] = NOTE_SPACES
        return base
    return None


def list_partitions(*, run: Callable[..., Any] = subprocess.run,
                    table: Optional[List[mounts.MountEntry]] = None) -> List[Dict[str, Any]]:
    """Windows-related partitions with mount state, Windows detection and hibernation."""
    try:
        proc = run(LSBLK_ARGV, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lsblk failed: %s", exc)
        return []
    if getattr(proc, "returncode", 1) != 0:
        log.warning("lsblk failed: %s", (getattr(proc, "stderr", "") or "").strip())
        return []
    entries = mounts.mount_table() if table is None else table
    parts: List[Dict[str, Any]] = []
    for dev in parse_lsblk(getattr(proc, "stdout", "") or ""):
        entry = classify_device(dev)
        if entry is None:
            continue
        if not entry["mountpoint"]:
            m = mounts.find_mount(device=entry["device"], table=entries)
            if m is not None:
                entry["mountpoint"] = m.mountpoint
        if entry["fstype"].lower() == "ntfs":
            if entry["mountpoint"]:
                root = Path(entry["mountpoint"])
                entry["windows"] = is_windows_root(root)
                entry["hibernated"] = mounts.hiberfil_state(root) == "hibernated"
                if is_bundle(root):
                    entry["note"] = "Holds a Lindos transfer folder."
                m = mounts.find_mount(device=entry["device"], table=entries)
                if m is not None and mounts.is_read_only(m) is False:
                    entry["note"] = ("Opened with write access by another program; Lindos only "
                                     "reads, but unmounting it and letting Lindos reopen it "
                                     "read-only is safer.")
            else:
                entry["note"] = NOTE_UNMOUNTED
        parts.append(entry)
    if any(p["hibernated"] for p in parts):
        for p in parts:
            if p["fstype"].lower() == "ntfs" and not p["hibernated"]:
                extra = "Windows on this PC is hibernated (Fast Startup), so this drive may be out of date too."
                p["note"] = f"{p['note']} {extra}".strip()
            elif p["hibernated"]:
                p["note"] = f"{p['note']} {mounts.HIBERNATED_WARNING}.".strip()
    return parts


# --------------------------------------------------------------------------- #
# bundles
# --------------------------------------------------------------------------- #
def bundle_search_roots() -> List[Path]:
    """Removable-media mount folders and the home folder."""
    roots: List[Path] = []
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    for base in ("/media", "/run/media"):
        top = Path(system_path(base))
        candidates = [top / user] if user else []
        candidates.append(top)
        for cand in candidates:
            try:
                if cand.is_dir() and not is_link(cand):
                    for child in sorted(cand.iterdir()):
                        if child.is_dir() and not is_link(child) and child not in roots and child.name != user:
                            roots.append(child)
            except OSError:
                continue
    home = user_home()
    if home.is_dir():
        roots.append(home)
    return roots


def _bundle_entry(path: Path) -> Optional[Dict[str, Any]]:
    try:
        man = read_manifest(path)
    except TransferError as exc:
        log.debug("ignoring %s: %s", path, exc)
        return None
    return {"path": str(path), "computer": man.get("computer") or "",
            "user": (man.get("user") or {}).get("name") or "", "created": man.get("created") or ""}


def find_bundles(roots: Optional[Iterable[Path]] = None, *, max_depth: int = 2,
                 max_entries: int = 2000) -> List[Dict[str, Any]]:
    """Transfer folders at depth <= *max_depth* below each root (hidden folders and links skipped)."""
    found: List[Dict[str, Any]] = []
    seen: set = set()
    for root in (bundle_search_roots() if roots is None else roots):
        stack: List[Tuple[Path, int]] = [(Path(root), 0)]
        while stack:
            cur, depth = stack.pop()
            key = os.path.normcase(str(cur))
            if key in seen:
                continue
            seen.add(key)
            if is_bundle(cur):
                entry = _bundle_entry(cur)
                if entry:
                    found.append(entry)
                continue
            if depth >= max_depth:
                continue
            try:
                with os.scandir(cur) as it:
                    children = []
                    for i, e in enumerate(it):
                        if i >= max_entries:
                            break
                        if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
                            continue
                        children.append(Path(e.path))
            except OSError:
                continue
            for child in sorted(children, reverse=True):
                stack.append((child, depth + 1))
    found.sort(key=lambda b: b.get("created") or "", reverse=True)
    return found


def detect_sources(*, run: Callable[..., Any] = subprocess.run,
                   bundle_roots: Optional[Iterable[Path]] = None,
                   table: Optional[List[mounts.MountEntry]] = None) -> Dict[str, Any]:
    """``lindos-transfer sources --json``: ``{"partitions": [...], "bundles": [...]}``."""
    parts = list_partitions(run=run, table=table)
    roots = list(bundle_search_roots() if bundle_roots is None else bundle_roots)
    for p in parts:
        mp = p.get("mountpoint")
        if mp and Path(mp) not in roots:
            roots.append(Path(mp))
    return {"partitions": parts, "bundles": find_bundles(roots)}


# --------------------------------------------------------------------------- #
# the Source model
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    """An opened transfer source (see module docstring)."""

    type: str                                   # "partition" | "bundle"
    root: Path
    computer: str = ""
    windows: str = ""
    hibernated: bool = False
    driver: str = "unknown"                     # "ntfs-3g" | "ntfs3" | "ntfs" | "bundle" | "unknown"
    read_only: Optional[bool] = None
    manifest: Dict[str, Any] = field(default_factory=dict)
    hiberboot: Optional[bool] = None
    registry_stale: bool = False
    warnings: List[str] = field(default_factory=list)
    _hives: Dict[str, Optional[Hive]] = field(default_factory=dict, repr=False)

    @property
    def is_bundle(self) -> bool:
        return self.type == "bundle"

    def path(self, rel: Union[str, Sequence[str]], *, allow_final_link: bool = False) -> Optional[Path]:
        """Case-insensitive, link-refusing lookup below the source root."""
        return ci_path(self.root, rel, allow_final_link=allow_final_link)

    def hive(self, which: str) -> Optional[Hive]:
        """``SOFTWARE`` or ``SYSTEM`` of a partition source (cached; ``None`` if unreadable)."""
        which = which.upper()
        if which not in ("SOFTWARE", "SYSTEM"):
            raise ValueError("only the SOFTWARE and SYSTEM hives are read")
        if self.is_bundle:
            return None
        if which not in self._hives:
            path = self.path(["Windows", "System32", "config", which])
            hive = winreg.try_open_hive(path) if path is not None else None
            if hive is not None and hive.stale:
                self.registry_stale = True
            self._hives[which] = hive
        return self._hives[which]

    def close(self) -> None:
        for hive in self._hives.values():
            if hive is not None:
                hive.close()
        self._hives.clear()

    def as_plan_dict(self) -> Dict[str, Any]:
        """The ``source`` object of plan schema 1."""
        return {"type": self.type, "root": str(self.root), "computer": self.computer,
                "windows": self.windows, "hibernated": self.hibernated, "driver": self.driver}


def _open_partition(root: Path, *, table: Optional[List[mounts.MountEntry]] = None) -> Source:
    src = Source(type="partition", root=root)
    entry = mounts.find_mount(path=str(root), table=table)
    src.driver = mounts.driver_of(entry)
    src.read_only = mounts.is_read_only(entry)
    if src.read_only is False and entry is not None and entry.mountpoint not in ("/", ""):
        src.warnings.append("The Windows drive is open with write access by another program. Lindos "
                            "only reads from it, but mounting it read-only is safer "
                            "(unmount it, then: lindos-transfer mount <device>).")
    state = mounts.hiberfil_state(root)
    src.hibernated = state == "hibernated"
    if src.hibernated:
        src.warnings.append(mounts.HIBERNATED_WARNING)
    software = src.hive("SOFTWARE")
    system = src.hive("SYSTEM")
    if software is not None:
        src.windows = winreg.windows_version(software)["text"]
    else:
        src.windows = "Windows"
    if system is not None:
        src.computer = winreg.computer_name(system) or ""
        src.hiberboot = winreg.hiberboot_enabled(system)
    if not src.computer:
        src.computer = root.name
    if src.registry_stale:
        src.warnings.append("Some Windows settings may be out of date (Windows did not shut down "
                            "fully, so its latest registry changes are not on the disk yet).")
    return src


def _open_bundle(root: Path) -> Source:
    man = read_manifest(root)
    win = man.get("windows") or {}
    windows = " ".join(x for x in (str(win.get("caption") or "").replace("Microsoft ", "", 1),
                                   str(win.get("version") or "")) if x).strip()
    src = Source(type="bundle", root=root, computer=str(man.get("computer") or ""),
                 windows=windows or "Windows", driver="bundle", read_only=None, manifest=man)
    if not src.computer:
        src.computer = root.name
    return src


def open_source(path: Union[str, os.PathLike], *,
                table: Optional[List[mounts.MountEntry]] = None) -> Source:
    """Open ``--from``: a Windows drive folder or a transfer folder (or a folder holding exactly one)."""
    text = os.fspath(path)
    if text.startswith("/dev/"):
        raise TransferError(f"{text} is a device, not a folder. Open it first (read-only) with: "
                            f"lindos-transfer mount {text}")
    root = Path(text).expanduser()
    if not root.is_dir():
        raise TransferError(f"{root} is not a folder")
    if is_link(root):
        raise TransferError(f"{root} is a link; give the real folder")
    if is_bundle(root):
        return _open_bundle(root)
    if is_windows_root(root):
        return _open_partition(root, table=table)
    inner = find_bundles([root], max_depth=1)
    if len(inner) == 1:
        return _open_bundle(Path(inner[0]["path"]))
    if len(inner) > 1:
        listing = "\n  ".join(b["path"] for b in inner)
        raise TransferError(f"{root} holds several transfer folders; pick one with --from:\n  {listing}")
    raise TransferError(f"{root} is neither a Windows drive (with Windows and Users folders) nor a "
                        f"Lindos transfer folder (with {MANIFEST_NAME}).")
