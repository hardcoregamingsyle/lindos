"""Secure Boot / MOK status for the Lindos kernel (SPEC-WINDOWS §31.3).

Everything here is **read-only** and safe to call without root: it inspects the SecureBoot
UEFI variable (or falls back to ``mokutil --sb-state``), whether a Lindos Machine Owner Key
(MOK) exists on disk and is enrolled (``mokutil --list-enrolled``), and whether any installed
``-lindos`` kernel image is already signed (``sbverify --list``).  It never signs anything and
never calls ``sudo``/``pkexec`` — that happens in the kernel's own
``/etc/kernel/postinst.d/zz-lindos-sbsign`` hook (a plain POSIX ``sh`` script, not Python) and
is only *reported* here.

Every probe is defensive: a missing tool, an unreadable file or a non-UEFI machine reads back
as ``None`` ("unknown") or ``False``, never an exception -- this module only *describes* the
system, it never changes it.

Honesty (SPEC-WINDOWS §27, binding): this module never fakes attestation, never spoofs a TPM
or Secure-Boot state, and never bypasses signature checks. It reports the real state so the
user can make an informed choice (enrol a MOK, or turn Secure Boot off) -- see
:mod:`lindos_kernel.grub` for how that state gates which kernel GRUB boots by default.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import resolve

__all__ = [
    "SECUREBOOT_EFIVAR", "MOK_DIR", "MOK_PRIV", "MOK_DER", "KernelImage",
    "firmware_is_uefi", "secure_boot_enabled", "mok_present", "mok_enrolled",
    "installed_lindos_kernels", "image_signed", "status",
]

#: The well-known SecureBoot UEFI variable (SPEC-WINDOWS §31.3, efivarfs.rst).
SECUREBOOT_EFIVAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
#: Where Ubuntu's shim-signed keeps the Machine Owner Key it can sign kernels with.
MOK_DIR = "/var/lib/shim-signed/mok"
MOK_PRIV = MOK_DIR + "/MOK.priv"
MOK_DER = MOK_DIR + "/MOK.der"

RunFn = Callable[..., Any]
WhichFn = Callable[[str], Optional[str]]


@dataclass
class KernelImage:
    """One ``/boot/vmlinuz-*`` entry whose version carries the Lindos localversion."""

    version: str
    path: str
    signed: Optional[bool]  # True/False, or None when it could not be checked

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "path": self.path, "signed": self.signed}


# --- firmware / Secure Boot state -----------------------------------------------------------
def firmware_is_uefi(exists: Callable[[str], bool] = os.path.exists) -> bool:
    """True when the firmware is UEFI (``/sys/firmware/efi`` present)."""
    return exists(resolve("/sys/firmware/efi"))


def _read_secureboot_efivar(read_bytes: Optional[Callable[[str], bytes]] = None) -> Optional[bool]:
    """Read the SecureBoot efivar directly (attrs are the first 4 bytes, state is byte 5)."""
    path = resolve(SECUREBOOT_EFIVAR)
    try:
        if read_bytes is not None:
            data = read_bytes(path)
        else:
            with open(path, "rb") as handle:
                data = handle.read()
    except OSError:
        return None
    if len(data) < 5:
        return None
    return data[4] == 1


def _mokutil_sb_state(run: RunFn, which: WhichFn) -> Optional[bool]:
    if not which("mokutil"):
        return None
    try:
        proc = run(["mokutil", "--sb-state"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    low = out.lower()
    if "sbat" in low and "enabled" not in low and "disabled" not in low:
        return None
    if "secureboot enabled" in low or "secure boot: enabled" in low:
        return True
    if "secureboot disabled" in low or "secure boot: disabled" in low:
        return False
    return None


def secure_boot_enabled(run: RunFn = subprocess.run, which: WhichFn = shutil.which,
                         read_bytes: Optional[Callable[[str], bytes]] = None) -> Optional[bool]:
    """``True``/``False``/``None`` (unknown - not UEFI, or nothing readable)."""
    direct = _read_secureboot_efivar(read_bytes)
    if direct is not None:
        return direct
    return _mokutil_sb_state(run, which)


# --- Machine Owner Key -----------------------------------------------------------------------
def mok_present(exists: Callable[[str], bool] = os.path.exists) -> bool:
    """True when both the Lindos MOK private key and certificate exist on disk."""
    return exists(resolve(MOK_PRIV)) and exists(resolve(MOK_DER))


def mok_enrolled(run: RunFn = subprocess.run, which: WhichFn = shutil.which) -> Optional[bool]:
    """``True`` when ``mokutil --list-enrolled`` reports at least one enrolled certificate.

    ``None`` when ``mokutil`` is not installed or cannot be run (unknown, not "no").
    """
    if not which("mokutil"):
        return None
    try:
        proc = run(["mokutil", "--list-enrolled"], capture_output=True, text=True, timeout=15,
                   check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    if not out:
        return False
    low = out.lower()
    # real mokutil --list-enrolled prints one block per certificate with markers like these;
    # a "no keys enrolled"-style message (wording varies) has none of them.
    if any(marker in low for marker in ("sha1 fingerprint", "certificate:", "[key")):
        return True
    return False


# --- installed kernel images + signature check ------------------------------------------------
def installed_lindos_kernels(glob_fn: Optional[Callable[[str], List[str]]] = None) -> List[str]:
    """Absolute paths of ``/boot/vmlinuz-*-lindos`` images (newest last, lexically sorted)."""
    import glob as _glob
    finder = glob_fn or (lambda pattern: sorted(_glob.glob(pattern)))
    pattern = resolve("/boot/vmlinuz-*")
    return sorted(p for p in finder(pattern) if "-lindos" in os.path.basename(p))


def image_signed(path: str, run: RunFn = subprocess.run,
                  which: WhichFn = shutil.which) -> Optional[bool]:
    """``True``/``False`` from ``sbverify --list <path>``; ``None`` when ``sbverify`` is absent
    or the check could not be completed (unknown, never asserted as a fact)."""
    if not which("sbverify"):
        return None
    try:
        proc = run(["sbverify", "--list", path], capture_output=True, text=True, timeout=15,
                   check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).lower()
    if "no signature" in out:
        return False
    if getattr(proc, "returncode", 1) == 0 and "signature" in out:
        return True
    if getattr(proc, "returncode", 1) != 0:
        return False
    return None


def _version_from_vmlinuz(path: str) -> str:
    base = os.path.basename(path)
    return base[len("vmlinuz-"):] if base.startswith("vmlinuz-") else base


def status(run: RunFn = subprocess.run, which: WhichFn = shutil.which,
           exists: Callable[[str], bool] = os.path.exists,
           glob_fn: Optional[Callable[[str], List[str]]] = None,
           read_bytes: Optional[Callable[[str], bytes]] = None) -> Dict[str, Any]:
    """Full ``lindos-kernel secureboot status`` blob (SPEC-WINDOWS §31.3)."""
    uefi = firmware_is_uefi(exists)
    sb = secure_boot_enabled(run, which, read_bytes) if uefi else False
    present = mok_present(exists)
    enrolled = mok_enrolled(run, which)
    kernels: List[KernelImage] = []
    for image_path in installed_lindos_kernels(glob_fn):
        signed = image_signed(image_path, run, which)
        kernels.append(KernelImage(version=_version_from_vmlinuz(image_path), path=image_path,
                                    signed=signed))
    any_signed = any(k.signed for k in kernels) if kernels else None
    return {
        "firmware": "uefi" if uefi else "bios",
        "secure_boot": sb,
        "mok": {
            "present": present,
            "enrolled": enrolled,
            "priv": resolve(MOK_PRIV),
            "der": resolve(MOK_DER),
        },
        "tools": {
            "sbsign": which("sbsign") is not None,
            "sbverify": which("sbverify") is not None,
            "mokutil": which("mokutil") is not None,
        },
        "kernels": [k.to_dict() for k in kernels],
        "any_lindos_kernel_signed": any_signed,
    }
