"""lindos.dualboot — honest "restart into Windows" for the games/apps that need a real Windows
kernel (kernel-mode anti-cheat, TPM/Secure-Boot attestation) that no Linux kernel or VM can ever
satisfy (SPEC-WINDOWS §27, §30.3).

This module only *reads* the firmware/bootloader and builds a payload; it never edits Windows,
BCD, Secure Boot keys or firmware settings, and it never runs anything privileged itself — the
one-shot boot choice and the actual reboot go through ``lindos.helper``'s ``reboot-to-windows`` /
``firmware-setup`` actions, which re-validate everything as root before doing anything (see
``/usr/libexec/lindos/lindos-helper``).

Two independent restart methods, in preference order:

* **UEFI one-shot** (``efibootmgr --bootnext``): only when the firmware already has an *active*
  Windows Boot Manager entry (identified by its loader path, never by its label alone — a label
  can be forged). The firmware clears ``BootNext`` itself after one use, so the PC returns to
  Lindos afterwards without any further action.
* **GRUB one-shot** (``grub-reboot``): for BIOS/MBR systems, or when ``efibootmgr`` is not
  installed. Only offered when the target ``/boot`` can actually save GRUB's one-shot choice
  (:func:`grubenv_writable`) — on btrfs/zfs/LVM/mdraid ``/boot`` the choice sticks forever and
  every subsequent boot would go to Windows, so that combination is refused with an explanation.

Every function takes an optional *root* directory so tests can point it at a synthetic
``/sys``/``/proc``/``/boot`` tree instead of the real machine; nothing here needs root to run
(reading is enough), except the actual reboot, which the helper performs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

#: the loader path bcdboot writes for the Windows Boot Manager, compared case-insensitively
#: (and tolerant of a leading ``.`` as some shims print ``File(.\EFI\...)``).
WINDOWS_LOADER_PATH = "/efi/microsoft/boot/bootmgfw.efi"
#: efivarfs file for the "SecureBoot" global variable (EFI_GLOBAL_VARIABLE GUID); the 5th byte
#: (after 4 bytes of little-endian attributes) is 1 (enabled) or 0 (disabled).
SECURE_BOOT_EFIVAR = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
TPM_VERSION_FILE = "sys/class/tpm/tpm0/tpm_version_major"
EFI_DIR = "sys/firmware/efi"
GRUB_DIR = "boot/grub"
GRUB_CFG = "boot/grub/grub.cfg"
#: written under ~/.local/share/applications by ``lindos-dualboot shortcut``.
DESKTOP_SHORTCUT_NAME = "lindos-restart-windows.desktop"

_BOOT_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})([* ]) (.*)$")
_BOOTORDER_RE = re.compile(r"^BootOrder:\s*(.*)$")
_BOOTNEXT_RE = re.compile(r"^BootNext:\s*([0-9A-Fa-f]{4})\s*$")
_BOOTCURRENT_RE = re.compile(r"^BootCurrent:\s*([0-9A-Fa-f]{4})\s*$")
_TIMEOUT_RE = re.compile(r"^Timeout:\s*(\d+)\s*seconds?\s*$")
_FILE_LOADER_RE = re.compile(r"[Ff]ile\(([^)]*)\)")
_HD_PARTUUID_RE = re.compile(r"HD\(\s*\d+\s*,\s*GPT\s*,\s*([0-9A-Fa-f-]+)\s*,", re.IGNORECASE)
#: 30_os-prober.in: menuentry '<title>' ... --class windows ... $menuentry_id_option 'osprober-(efi|chain)-<id>'
_OSPROBER_RE = re.compile(
    r"^\s*menuentry\s+'(?P<title>(?:[^'\\]|\\.)*)'[^\n{]*--class\s+windows[^\n{]*?"
    r"(?:\$menuentry_id_option|--id)\s+'(?P<id>osprober-(?:efi|chain)-[^']+)'",
    re.MULTILINE,
)
#: filesystems / abstractions GRUB documents as unable to ``save_env`` (grub.d/00_header.in)
_GRUBENV_UNSAFE_FS = {"btrfs", "zfs"}


@dataclass(frozen=True)
class BootEntry:
    """One ``efibootmgr`` boot entry."""

    num: str            # 4 hex digits, e.g. "0001"
    label: str           # firmware-supplied description ("Windows Boot Manager"); never trusted alone
    active: bool          # the '*' marker (LOAD_OPTION_ACTIVE)
    loader: str           # e.g. "\\EFI\\Microsoft\\Boot\\bootmgfw.efi" (raw, as printed)
    partuuid: str         # from HD(n,GPT,<PARTUUID>,...) when present, else ""
    is_windows: bool      # loader path == \\EFI\\Microsoft\\Boot\\bootmgfw.efi, case-insensitive

    def to_dict(self) -> Dict[str, Any]:
        return {"num": self.num, "label": self.label, "active": self.active, "loader": self.loader,
                "partuuid": self.partuuid, "is_windows": self.is_windows}


# --- small path helper ------------------------------------------------------------------------
def _under(root: Optional[str], relpath: str) -> str:
    """Join *relpath* (posix-style, no leading ``/``) under *root*, or the real ``/`` when None."""
    relpath = relpath.lstrip("/")
    if root:
        return os.path.join(root, *relpath.split("/"))
    return "/" + relpath


# --- efibootmgr --------------------------------------------------------------------------------
def _is_windows_loader(loader: str) -> bool:
    if not loader:
        return False
    norm = loader.strip().replace("\\", "/")
    norm = norm.lstrip(".")
    if not norm.startswith("/"):
        norm = "/" + norm
    return norm.lower() == WINDOWS_LOADER_PATH


def parse_efibootmgr(text: str) -> Dict[str, Any]:
    """Parse ``efibootmgr`` (v18, Ubuntu noble) plain-text output.

    Tolerant of missing header lines, entries with no device-path column, and trailing
    whitespace; never raises on malformed input.  A boot entry is only ever recognised as
    Windows by its *loader path* (``\\EFI\\Microsoft\\Boot\\bootmgfw.efi``, case-insensitive) —
    the label is cosmetic and easily forged, so a "Windows Boot Manager"-labelled entry whose
    loader points somewhere else is **not** treated as Windows.

    Returns ``{"bootnext", "bootcurrent", "timeout", "bootorder": [...], "entries": [BootEntry, ...]}``.
    """
    bootnext: Optional[str] = None
    bootcurrent: Optional[str] = None
    timeout: Optional[int] = None
    bootorder: List[str] = []
    entries: List[BootEntry] = []

    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        m = _BOOT_ENTRY_RE.match(line)
        if m:
            num, active_ch, rest = m.groups()
            if "\t" in rest:
                label, devpath = rest.split("\t", 1)
            else:
                label, devpath = rest, ""
            loader = ""
            fm = _FILE_LOADER_RE.search(devpath)
            if fm:
                loader = fm.group(1).strip()
            partuuid = ""
            hm = _HD_PARTUUID_RE.search(devpath)
            if hm:
                partuuid = hm.group(1)
            entries.append(BootEntry(num=num.upper(), label=label.strip(), active=(active_ch == "*"),
                                     loader=loader, partuuid=partuuid, is_windows=_is_windows_loader(loader)))
            continue
        m = _BOOTORDER_RE.match(line)
        if m:
            bootorder = [x.strip() for x in m.group(1).split(",") if x.strip()]
            continue
        m = _BOOTNEXT_RE.match(line)
        if m:
            bootnext = m.group(1).upper()
            continue
        m = _BOOTCURRENT_RE.match(line)
        if m:
            bootcurrent = m.group(1).upper()
            continue
        m = _TIMEOUT_RE.match(line)
        if m:
            timeout = int(m.group(1))
            continue

    return {"bootnext": bootnext, "bootcurrent": bootcurrent, "timeout": timeout,
            "bootorder": bootorder, "entries": entries}


def windows_entries(parsed: Dict[str, Any]) -> List[BootEntry]:
    """The subset of ``parse_efibootmgr()``'s entries whose loader is the Windows Boot Manager."""
    return [e for e in parsed.get("entries", []) if e.is_windows]


# --- firmware / Secure Boot / TPM ---------------------------------------------------------------
def firmware(root: Optional[str] = None) -> str:
    """``"uefi"`` when ``/sys/firmware/efi`` exists, else ``"bios"``."""
    return "uefi" if os.path.isdir(_under(root, EFI_DIR)) else "bios"


def secure_boot_state(root: Optional[str] = None, *, which: Callable[[str], Optional[str]] = shutil.which,
                      run: Callable[..., Any] = subprocess.run) -> str:
    """``"enabled"`` / ``"disabled"`` / ``"unknown"``.

    Reads the ``SecureBoot`` efivar directly (no root needed); falls back to ``mokutil
    --sb-state`` when the efivar is not readable (older kernels, or a mokutil-only environment).
    """
    efivar = _under(root, SECURE_BOOT_EFIVAR)
    try:
        with open(efivar, "rb") as fh:
            data = fh.read()
        if len(data) >= 5:
            return "enabled" if data[4] else "disabled"
    except OSError:
        pass
    mokutil = which("mokutil")
    if mokutil:
        try:
            proc = run([mokutil, "--sb-state"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        out = (getattr(proc, "stdout", "") or "").lower()
        # mokutil prints e.g. "SecureBoot enabled" / "SecureBoot disabled" (occasionally "SB enabled");
        # matching on the plain word is robust to both spellings and any surrounding text.
        if "disabled" in out:
            return "disabled"
        if "enabled" in out:
            return "enabled"
    return "unknown"


def tpm_version(root: Optional[str] = None) -> Optional[int]:
    """The TCG spec major version of ``/dev/tpm0`` (``2`` on any modern PC), or ``None``."""
    try:
        with open(_under(root, TPM_VERSION_FILE), "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


# --- GRUB / os-prober --------------------------------------------------------------------------
def grub_windows_entries(grub_cfg: str) -> List[Dict[str, str]]:
    """``[{"id": "osprober-efi-...", "title": "Windows Boot Manager (on /dev/sda1)"}, ...]``.

    Extracted from ``os-prober``'s ``--class windows`` menu entries in an already-read
    ``grub.cfg``; the *id* (never the title, which embeds a possibly-changing device path and is
    localised) is what ``grub-reboot`` must be given.
    """
    seen = set()
    out: List[Dict[str, str]] = []
    for m in _OSPROBER_RE.finditer(grub_cfg or ""):
        mid = m.group("id")
        if mid in seen:
            continue
        seen.add(mid)
        title = m.group("title").replace("'\\''", "'")
        out.append({"id": mid, "title": title})
    return out


def _grub_probe(grub_dir: str, target: str, *, run: Callable[..., Any]) -> Optional[str]:
    try:
        proc = run(["grub-probe", f"--target={target}", grub_dir], stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return (getattr(proc, "stdout", "") or "").strip()


def _proc_mounts_fs_and_device(root: Optional[str], grub_dir: str) -> "tuple[Optional[str], Optional[str]]":
    """Best-effort fallback when ``grub-probe`` is unavailable: the longest-prefix mount of
    *grub_dir* from ``/proc/mounts``, as ``(fstype, device)``.

    Each mount entry's path is re-resolved under *root* before comparing, so this also works
    against a synthetic ``root`` in tests (a real ``/proc/mounts`` always lists real absolute
    paths, which mean nothing under a fake root otherwise).
    """
    try:
        with open(_under(root, "proc/mounts"), "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None, None
    grub_dir_norm = os.path.normpath(grub_dir)
    root_norm = os.path.normpath(_under(root, "/"))
    best_mp = ""
    best: "tuple[Optional[str], Optional[str]]" = (None, None)
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        mountpoint = mountpoint.replace("\\040", " ")
        resolved_mp = os.path.normpath(_under(root, mountpoint))
        if grub_dir_norm == resolved_mp or grub_dir_norm.startswith(resolved_mp.rstrip(os.sep) + os.sep) \
           or resolved_mp == root_norm:
            if len(resolved_mp) >= len(best_mp):
                best_mp = resolved_mp
                best = (fstype, device)
    return best


def grubenv_writable(root: Optional[str] = None, *, run: Callable[..., Any] = subprocess.run) -> bool:
    """Whether GRUB can actually persist ``grub-reboot``'s one-shot ``next_entry``.

    ``False`` when ``/boot/grub`` does not exist, or sits on btrfs/zfs (GRUB cannot
    ``save_env`` there), or on an LVM/mdraid/other "diskfilter" abstraction (``grub-probe
    --target=abstraction`` reports anything other than ``none``). Prefers asking
    ``grub-probe`` (authoritative); falls back to ``/proc/mounts`` + a device-path heuristic
    when ``grub-probe`` is not installed (e.g. every test on a non-Linux host), defaulting to
    ``True`` only when nothing disqualifying could be found — a stock Mint ext4 install.
    """
    grub_dir = _under(root, GRUB_DIR)
    if not os.path.isdir(grub_dir):
        return False
    fs = _grub_probe(grub_dir, "fs", run=run)
    abstraction = _grub_probe(grub_dir, "abstraction", run=run)
    if fs is not None or abstraction is not None:
        if fs and fs.lower() in _GRUBENV_UNSAFE_FS:
            return False
        if abstraction and abstraction.lower() not in ("none", ""):
            return False
        return True
    # grub-probe unavailable: fall back to /proc/mounts (device-path heuristic for LVM/mdraid)
    fstype, device = _proc_mounts_fs_and_device(root, grub_dir)
    if fstype and fstype.lower() in _GRUBENV_UNSAFE_FS:
        return False
    if device and (re.match(r"^/dev/(mapper/|md\d)", device) or "/dev/dm-" in device):
        return False
    return True


# --- lsblk (disk for a PARTUUID; BitLocker hint) -----------------------------------------------
def _lsblk_devices(*, run: Callable[..., Any], which: Callable[[str], Optional[str]]) -> Optional[List[Dict[str, Any]]]:
    exe = which("lsblk")
    if not exe:
        return None
    try:
        proc = run([exe, "-J", "-b", "-o", "PATH,PARTUUID,FSTYPE"], stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError:
        return None
    devices = data.get("blockdevices")
    if not isinstance(devices, list):
        return None
    flat: List[Dict[str, Any]] = []

    def _walk(items: List[Any]) -> None:
        for it in items:
            if isinstance(it, dict):
                flat.append(it)
                children = it.get("children")
                if isinstance(children, list):
                    _walk(children)

    _walk(devices)
    return flat


def _disk_for_partuuid(partuuid: str, devices: Optional[List[Dict[str, Any]]]) -> str:
    if not partuuid or not devices:
        return ""
    target = partuuid.lower()
    for dev in devices:
        if str(dev.get("partuuid") or "").lower() == target:
            return str(dev.get("path") or "")
    return ""


def _bitlocker_hint(rows: List[Dict[str, Any]], devices: Optional[List[Dict[str, Any]]]) -> bool:
    if not rows or not devices:
        return False
    partuuids = {str(r["partuuid"]).lower() for r in rows if r.get("partuuid")}
    if not partuuids:
        return False
    for dev in devices:
        pu = str(dev.get("partuuid") or "").lower()
        fstype = str(dev.get("fstype") or "").lower()
        if pu in partuuids and fstype == "bitlocker":
            return True
    return False


def _lindos_kernel_signed(*, run: Callable[..., Any], which: Callable[[str], Optional[str]]) -> Optional[bool]:
    """Best-effort: ask ``lindos-kernel secureboot status --json`` (SPEC §31) whether the
    running kernel is MOK-signed. ``None`` when lindos-kernel is not installed or unavailable
    (e.g. this host, or a plain-Ubuntu system) — never a hard failure."""
    exe = which("lindos-kernel")
    if not exe:
        return None
    try:
        proc = run([exe, "secureboot", "status", "--json"], stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError:
        return None
    # ``lindos-kernel secureboot status --json``'s real key is "any_lindos_kernel_signed"
    # (lindos_kernel.secureboot.status()); there is no "signed" key in that payload.
    signed = data.get("any_lindos_kernel_signed") if isinstance(data, dict) else None
    return bool(signed) if isinstance(signed, bool) else None


# --- status --------------------------------------------------------------------------------
def status(*, run: Callable[..., Any] = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which,
          root: Optional[str] = None) -> Dict[str, Any]:
    """Everything ``lindos-dualboot status`` / the Settings "Games that need Windows" page need.

    ``{"firmware", "secure_boot", "tpm", "windows_entries": [{num,label,partuuid,disk}, ...],
    "grub_windows_entries": [{id,title}, ...], "can_reboot_to_windows", "method": "bootnext" |
    "grub-reboot" | None, "why", "lindos_kernel_signed", "bitlocker_hint"}``. Never raises: every
    external command is best-effort and missing tools just narrow the available method.
    """
    fw = firmware(root)
    sb = secure_boot_state(root, which=which, run=run)
    tpm = tpm_version(root)
    devices = _lsblk_devices(run=run, which=which)

    win_rows: List[Dict[str, Any]] = []
    efibootmgr = which("efibootmgr") if fw == "uefi" else None
    if efibootmgr:
        try:
            proc = run([efibootmgr], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      timeout=15, check=False)
            out = (getattr(proc, "stdout", "") or "") if getattr(proc, "returncode", 1) == 0 else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        parsed = parse_efibootmgr(out)
        for entry in windows_entries(parsed):
            if not entry.active:
                continue
            win_rows.append({"num": entry.num, "label": entry.label, "partuuid": entry.partuuid,
                             "disk": _disk_for_partuuid(entry.partuuid, devices)})

    grub_rows: List[Dict[str, str]] = []
    grubenv_ok = False
    if not win_rows:
        try:
            with open(_under(root, GRUB_CFG), "r", encoding="utf-8", errors="replace") as fh:
                cfg_text = fh.read()
        except OSError:
            cfg_text = ""
        if cfg_text:
            grub_rows = grub_windows_entries(cfg_text)
        if grub_rows:
            grubenv_ok = grubenv_writable(root, run=run)

    if win_rows:
        method: Optional[str] = "bootnext"
        why = ""
    elif grub_rows and grubenv_ok:
        method, why = "grub-reboot", ""
    elif grub_rows and not grubenv_ok:
        method, why = None, ("GRUB cannot save its one-shot boot choice on this /boot (btrfs, zfs, "
                             "LVM or mdraid) — pick Windows from the GRUB menu by hand instead")
    elif fw == "uefi" and not efibootmgr:
        method, why = None, "efibootmgr is not installed (apt install efibootmgr)"
    else:
        method, why = None, "no Windows Boot Manager entry was found in the firmware or the GRUB menu"

    return {
        "firmware": fw,
        "secure_boot": sb,
        "tpm": tpm,
        "windows_entries": win_rows,
        "grub_windows_entries": grub_rows,
        "can_reboot_to_windows": method is not None,
        "method": method,
        "why": why,
        "lindos_kernel_signed": _lindos_kernel_signed(run=run, which=which),
        "bitlocker_hint": _bitlocker_hint(win_rows, devices),
    }


def reboot_payload(st: Dict[str, Any], entry: Optional[str] = None) -> Dict[str, Any]:
    """Build the ``lindos.helper`` ``reboot-to-windows`` payload from a :func:`status` result.

    *entry* picks a specific boot number (``"0001"``) or GRUB menu id when *st* offers more than
    one; otherwise the first (firmware-preferred) one is used. Raises :class:`ValueError` (never
    a helper call) when nothing usable is available, so the caller can show *st["why"]*.
    """
    method = st.get("method")
    if method == "bootnext":
        rows = st.get("windows_entries") or []
        if entry:
            chosen = next((r for r in rows if str(r.get("num", "")).upper() == entry.upper()), None)
            if chosen is None:
                raise ValueError(f"no active Windows Boot Manager entry {entry!r} in this status")
        else:
            chosen = rows[0] if rows else None
        if chosen is None:
            raise ValueError("no active Windows Boot Manager entry available")
        return {"method": "bootnext", "entry": chosen["num"], "reboot": True}
    if method == "grub-reboot":
        rows = st.get("grub_windows_entries") or []
        if entry:
            chosen = next((r for r in rows if r.get("id") == entry), None)
            if chosen is None:
                raise ValueError(f"no GRUB Windows menu entry {entry!r} in this status")
        else:
            chosen = rows[0] if rows else None
        if chosen is None:
            raise ValueError("no GRUB Windows menu entry available")
        return {"method": "grub-reboot", "menuentry": chosen["id"], "reboot": True}
    raise ValueError(st.get("why") or "no supported way to restart into Windows was found")


__all__ = [
    "WINDOWS_LOADER_PATH", "SECURE_BOOT_EFIVAR", "TPM_VERSION_FILE", "DESKTOP_SHORTCUT_NAME",
    "BootEntry", "parse_efibootmgr", "windows_entries", "firmware", "secure_boot_state",
    "tpm_version", "grub_windows_entries", "grubenv_writable", "status", "reboot_payload",
]
