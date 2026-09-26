"""Disk images (``.iso`` / ``.img``): read-only loop mount via udisksctl + ``autorun.inf``.

SPEC-WINDOWS §28.6.  The flow ``lindos-run`` follows (Windows' AutoPlay, but never automatic):

1. :func:`loop_setup` — ``udisksctl loop-setup -r -f <image>`` (read-only), parse
   *"Mapped file … as /dev/loopN."* (fallback: ``losetup -j <image>``);
2. :func:`mount_loop` — pick the device to mount (the whole ``/dev/loopN`` when it carries a
   file system, as Windows ISOs do (UDF + ISO 9660); else its first partition ``loopNp1``) and
   run an explicit ``udisksctl mount -b`` (XFCE has no automounter);
3. :func:`find_autorun` / :func:`find_setup` — ``autorun.inf`` ``open=`` target, else a top-level
   ``setup.exe`` / ``install.exe``; the caller asks *"Run <setup.exe> from <label>?"* and, on yes,
   re-enters ``lindos-run`` (always through Wine — files on discs may be mode 0400, never exec);
4. :func:`detach_command` — ``udisksctl unmount`` + ``udisksctl loop-delete`` when done.

:func:`open_image` bundles 1–3.  All tool calls take injectable ``run``/``which`` callables and
raise :class:`DiskImageError` with a plain-language message on failure.  udisksctl output is
not localised (plain ``g_print``), so parsing it is safe.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import get_logger

__all__ = [
    "DiskImageError",
    "UDISKS_INSTALL_HINT",
    "SETUP_NAMES",
    "loop_setup",
    "mount_loop",
    "pick_mount_device",
    "find_autorun",
    "find_setup",
    "detach_command",
    "base_loop_device",
    "open_image",
    "parse_loop_setup_output",
    "parse_mount_output",
]

log = get_logger("lindos-compat.diskimage")

UDISKS_INSTALL_HINT = "sudo apt install udisks2"
#: Top-level programs offered when ``autorun.inf`` names none (checked in this order).
SETUP_NAMES = ("setup.exe", "install.exe")
#: What an ``open=`` line may start (anything else, e.g. a web page, is not offered).
_RUNNABLE_SUFFIXES = (".exe", ".msi", ".bat", ".cmd", ".com")
_AUTORUN_LIMIT = 64 * 1024
_PREFERRED_FS = ("udf", "iso9660", "vfat", "exfat", "ntfs")

Which = Callable[[str], Optional[str]]
Run = Callable[..., "subprocess.CompletedProcess[str]"]


class DiskImageError(RuntimeError):
    """A disk image could not be attached or mounted (message is user-facing)."""


# ---------------------------------------------------------------------------
# udisksctl
# ---------------------------------------------------------------------------

_MAPPED_RE = re.compile(r"\bas (/dev/loop\d+)\.?\s*$", re.M)
_LOSETUP_RE = re.compile(r"^(/dev/loop\d+):", re.M)
_MOUNTED_RE = re.compile(r"^Mounted (\S+) at (.+?)\s*$", re.M)
_ALREADY_RE = re.compile(r"already mounted at [`'\"](.+?)['\"]")


def _udisksctl(which: Which) -> str:
    tool = which("udisksctl")
    if not tool:
        raise DiskImageError(f"Opening disk images needs udisks2, which is not installed ({UDISKS_INSTALL_HINT}).")
    return tool


def _output(proc: object) -> Tuple[int, str, str]:
    rc = getattr(proc, "returncode", 1)
    return (1 if rc is None else int(rc)), str(getattr(proc, "stdout", "") or ""), str(getattr(proc, "stderr", "") or "")


def _first_error_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            # "Error setting up loop device for x.iso: GDBus.Error:org.freedesktop.UDisks2.Error.Failed: ..."
            return re.sub(r"GDBus\.Error:[\w.]+:\s*", "", line)
    return "unknown error"


def parse_loop_setup_output(text: str) -> Optional[str]:
    """``"Mapped file /x/a as b.iso as /dev/loop13."`` → ``"/dev/loop13"`` (last "as /dev/loopN")."""
    matches = _MAPPED_RE.findall(text or "")
    return matches[-1] if matches else None


def loop_setup(image: Path, *, run: Run = subprocess.run, which: Which = shutil.which) -> str:
    """Attach ``image`` read-only to a loop device; returns ``"/dev/loopN"``."""
    tool = _udisksctl(which)
    abspath = os.path.abspath(os.fspath(image))
    try:
        proc = run([tool, "loop-setup", "-r", "-f", abspath], capture_output=True, text=True, timeout=120,
                   check=False, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiskImageError(f"udisksctl could not be started: {exc}") from exc
    rc, out, err = _output(proc)
    device = parse_loop_setup_output(out)
    if device:
        return device
    if rc != 0:
        raise DiskImageError(f"The disk image could not be opened: {_first_error_line(err or out)}")
    # udisksctl succeeded but printed something unexpected: ask losetup which device backs the file
    losetup = which("losetup")
    if losetup:
        try:
            lp = run([losetup, "-j", abspath], capture_output=True, text=True, timeout=30, check=False,
                     errors="replace")
            found = _LOSETUP_RE.findall(_output(lp)[1])
            if found:
                return found[-1]
        except (OSError, subprocess.SubprocessError):
            pass
    raise DiskImageError("The disk image was attached, but Lindos could not tell which device it got.")


def _lsblk_tree(device: str, run: Run, which: Which) -> Optional[Dict[str, object]]:
    lsblk = which("lsblk")
    if not lsblk:
        return None
    try:
        proc = run([lsblk, "-J", "-o", "NAME,PATH,FSTYPE,TYPE,MOUNTPOINT,LABEL", device], capture_output=True,
                   text=True, timeout=30, check=False, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    rc, out, _err = _output(proc)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    devices = data.get("blockdevices") if isinstance(data, dict) else None
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        return devices[0]
    return None


def pick_mount_device(device: str, *, run: Run = subprocess.run,
                      which: Which = shutil.which) -> Tuple[str, Optional[str]]:
    """``(block device to mount, existing mount point or None)`` for a loop device.

    The whole device when it has a file system (Windows ISOs: UDF/ISO 9660, hybrid ISOs too),
    else its first partition with a file system (``loopNp1``), else the device itself.
    """
    tree = _lsblk_tree(device, run, which)
    if tree is None:
        return device, None

    def path_of(node: Dict[str, object]) -> str:
        return str(node.get("path") or ("/dev/" + str(node.get("name") or "")))

    if tree.get("fstype"):
        return path_of(tree), (str(tree["mountpoint"]) if tree.get("mountpoint") else None)
    children = [c for c in (tree.get("children") or []) if isinstance(c, dict) and c.get("fstype")]
    if not children:
        return device, None
    children.sort(key=lambda c: _PREFERRED_FS.index(str(c.get("fstype")))
                  if str(c.get("fstype")) in _PREFERRED_FS else len(_PREFERRED_FS))
    best = children[0]
    return path_of(best), (str(best["mountpoint"]) if best.get("mountpoint") else None)


def parse_mount_output(text: str) -> Optional[str]:
    """Mount point from ``"Mounted /dev/loop0 at /media/u/DISC"`` or an *already mounted* error."""
    m = _MOUNTED_RE.search(text or "")
    if m:
        where = m.group(2)
        if where.endswith(".") and not os.path.exists(where):
            where = where[:-1]  # older udisksctl ended the sentence with a period
        return where
    m = _ALREADY_RE.search(text or "")
    if m:
        return m.group(1)
    return None


def mount_loop(device: str, *, run: Run = subprocess.run, which: Which = shutil.which) -> Path:
    """Mount the loop device (or its first partition) with ``udisksctl mount -b``; returns the mount point.

    Never passes ``--no-user-interaction``: if polkit wants a password the user is asked.
    """
    tool = _udisksctl(which)
    target, mounted = pick_mount_device(device, run=run, which=which)
    if mounted:
        return Path(mounted)
    try:
        proc = run([tool, "mount", "-b", target], capture_output=True, text=True, timeout=120, check=False,
                   errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiskImageError(f"udisksctl could not be started: {exc}") from exc
    rc, out, err = _output(proc)
    where = parse_mount_output(out) or parse_mount_output(err)
    if where:
        return Path(where)
    if rc != 0:
        raise DiskImageError(f"The disk image could not be mounted: {_first_error_line(err or out)}")
    raise DiskImageError("The disk image was mounted, but Lindos could not tell where.")


def base_loop_device(device: str) -> str:
    """``/dev/loop3p1`` → ``/dev/loop3`` (other devices unchanged)."""
    m = re.match(r"^(/dev/loop\d+)p\d+$", device or "")
    return m.group(1) if m else device


def detach_command(device: str) -> List[List[str]]:
    """Commands that unmount and detach a loop device (run them in order, as the user)."""
    return [["udisksctl", "unmount", "-b", device], ["udisksctl", "loop-delete", "-b", base_loop_device(device)]]


# ---------------------------------------------------------------------------
# autorun.inf / setup
# ---------------------------------------------------------------------------


def _ci_child(directory: Path, name: str) -> Optional[Path]:
    """A direct child of ``directory`` named ``name`` in any letter case (no symlinks followed)."""
    lname = name.lower()
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.lower() == lname:
                    return Path(entry.path)
    except OSError:
        return None
    return None


def _decode(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        return data.decode("utf-16-le", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _autorun_sections(text: str) -> Dict[str, Dict[str, str]]:
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            current = sections.setdefault(line[1:line.index("]")].strip().lower(), {})
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current.setdefault(key.strip().lower(), value.strip())
    return sections


def find_autorun(mount: Path) -> Optional[Dict[str, str]]:
    """``{"open","icon","label","shellexecute"}`` from ``autorun.inf`` (case-insensitive; ANSI or UTF-16).

    ``[AutoRun.Amd64]`` wins over ``[AutoRun]`` (this is a 64-bit PC).  None when there is no
    ``autorun.inf`` or it has no AutoRun section.  Missing keys are empty strings.
    """
    inf = _ci_child(Path(mount), "autorun.inf")
    if inf is None or inf.is_symlink() or not inf.is_file():
        return None
    try:
        with open(inf, "rb") as fh:
            data = fh.read(_AUTORUN_LIMIT)
    except OSError:
        return None
    sections = _autorun_sections(_decode(data))
    section = sections.get("autorun.amd64") or sections.get("autorun")
    if section is None:
        return None
    return {key: section.get(key, "") for key in ("open", "icon", "label", "shellexecute")}


def _inside(path: Path, root: Path) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
    except OSError:
        return False
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _resolve_relative(mount: Path, rel: str) -> Optional[Path]:
    """Resolve a Windows-style relative path below ``mount`` case-insensitively; None if absent/outside."""
    text = rel.strip().strip('"').replace("/", "\\")
    if re.match(r"^[A-Za-z]:", text):
        text = text[2:]  # "D:\setup.exe" -> relative to the disc root
    parts = [p for p in text.split("\\") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    current = Path(mount)
    for part in parts:
        found = _ci_child(current, part)
        if found is None:
            return None
        current = found
    if not current.is_file() or not _inside(current, Path(mount)):
        return None
    return current


def _open_target(mount: Path, value: str) -> Optional[Path]:
    """The program an ``open=`` value starts: quoted path, or the longest existing space-split prefix."""
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        candidates = [text[1:end] if end > 0 else text[1:]]
    else:
        words = text.split(" ")
        candidates = [" ".join(words[:i]) for i in range(len(words), 0, -1)]
    for cand in candidates:
        if not cand.lower().endswith(_RUNNABLE_SUFFIXES):
            continue
        found = _resolve_relative(mount, cand)
        if found is not None:
            return found
    return None


def find_setup(mount: Path) -> Optional[Path]:
    """The setup program to offer: the ``autorun.inf`` ``open=`` target, else top-level setup.exe/install.exe."""
    root = Path(mount)
    autorun = find_autorun(root)
    if autorun and autorun.get("open"):
        target = _open_target(root, autorun["open"])
        if target is not None:
            return target
    for name in SETUP_NAMES:
        cand = _ci_child(root, name)
        if cand is not None and cand.is_file() and _inside(cand, root):
            return cand
    return None


def open_image(image: Path, *, run: Run = subprocess.run, which: Which = shutil.which) -> Dict[str, object]:
    """Attach + mount ``image`` and look for a setup program.

    Returns ``{"device", "mount", "label", "autorun", "setup", "detach"}``; ``setup`` is a path
    to *offer* (never run here).  Raises :class:`DiskImageError`.
    """
    device = loop_setup(image, run=run, which=which)
    try:
        mount = mount_loop(device, run=run, which=which)
    except DiskImageError:
        try:
            run(detach_command(device)[1], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    autorun = find_autorun(mount)
    setup = find_setup(mount)
    label = (autorun or {}).get("label") or mount.name or Path(image).stem
    mounted_dev, _ = pick_mount_device(device, run=run, which=which)
    return {"device": device, "mount": str(mount), "label": label[:32], "autorun": autorun,
            "setup": str(setup) if setup else None, "detach": detach_command(mounted_dev)}
