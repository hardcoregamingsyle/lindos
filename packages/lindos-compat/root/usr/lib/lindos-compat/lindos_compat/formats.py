"""Windows file formats: registry, magic-byte classifier and per-format action plans.

SPEC-WINDOWS §28.2 (binding API) and §28.3 (binding ids / handlers / status).  ``lindos-run``
calls :func:`detect` and :func:`plan_action`; ``lindos-compat formats --json`` prints
:func:`formats_table`.  Everything is pure computation on the file's bytes, except the few
probes that take injectable ``which`` / ``run`` callables (DOSBox lookup, the Wine WoW64-mode
probe for 16-bit programs).  Nothing is executed here: a plan is an argv list that the caller
runs after asking the user whatever :attr:`ActionPlan.confirm` says.

Honesty (§27.2): every format has ``status`` ∈ ``works | partial | unsupported`` and a
one-line ``note``; files Lindos cannot open are *explained* (handler ``explain``, exit code
:data:`EXIT_UNSUPPORTED`), never "tried anyway" in a way that looks like success.

Classifier (content first, suffix last)
---------------------------------------
``MZ`` → ``e_lfanew`` (u32le @0x3C) →

* ``PE\\0\\0`` → Machine (x86/x64 run; ARM → ``arm-exe``; other CPUs explained), DLL flag
  (``0x2000``) → ``dll`` (``.cpl`` → ``cpl``), Subsystem (opt+68: 1/8 native driver, 10–13 EFI,
  16 boot app → ``dll``-style explanation), CLR data directory #14 → ``dotnet-exe``, native
  .NET apphost (sibling ``<stem>.runtimeconfig.json``, a sibling managed ``<stem>.dll`` or the
  apphost bundle marker) → ``dotnet-exe`` with ``apphost=True``; ``.scr`` → ``scr``.
* ``NE`` → ``ne_flags`` (+0x0C) & 0x8000 → ``dll``; ``ne_exetyp`` (+0x36) 2/4 → ``win16-exe``;
  other targets (OS/2 …) → ``dos-exe`` (Wine/DOSBox run the DOS part).
* ``LE``/``LX`` → libraries / VxD drivers → ``dll``; programs → ``dos-exe`` (DOS extenders).
* anything else after ``MZ`` → ``dos-exe``.  A ``.com``/``.pif`` without ``MZ`` → ``dos-com``.

Other magics: OLE (root-storage CLSID, then suffix → ``msi``/``msp``/``mst``); ZIP (``msix.classify``
from W-B, with a zip fallback); ``EXPH``/``EXSH``/``EXBH`` encrypted MSIX; ``MSCF`` CAB/MSU;
ISO9660 ``CD001`` / UDF ``BEA01``/``NSR02``/``NSR03`` volume descriptors; ``.lnk`` header;
``.reg`` headers; ``[InternetShortcut]``; ``<AppInstaller>`` XML root.

Helpers for ``lindos-run`` (W-A2)
---------------------------------
* :func:`reg_preview` (path) → ``{"header", "adds", "deleted_keys", "deleted_values", "encoding",
  "valid", "value_count", "truncated"}`` — REGEDIT4 (ANSI) and ``Windows Registry Editor
  Version 5.00`` (UTF-16LE with BOM, or UTF-8).  :func:`reg_confirm_text` renders the
  confirmation question (Wine's ``regedit`` never asks).
* :func:`parse_url_shortcut` (path) → ``(url, allowed, reason)``; only ``http``, ``https``,
  ``mailto`` and ``ftp`` are allowed (``javascript:``, ``vbscript:``, ``file:``, ``data:`` …
  are refused with a plain-language reason).
* :func:`inf_kind` (path) → ``"software" | "driver" | "unknown"`` (:func:`inf_details` gives the
  reasons and any ``[AutoRun]`` data).
* :func:`prefix_has_dotnet_framework` (prefix) → bool (``system.reg`` ``NDP\\v4\\Full`` +
  the native files, so wine-mono's own keys are not mistaken for Microsoft .NET).
* :func:`parse_pif` (path), :func:`rundll32_path_tokens` (win_path), :data:`EXIT_UNSUPPORTED`.

``ActionPlan.exit_code`` rule for callers: ``0`` → carry the plan out; non-zero → print
``message`` and exit with that code (``3`` = unsupported and explained, ``1`` = something is
missing or broken, e.g. DOSBox not installed or an invalid ``.reg`` file).
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import get_logger, user_home
from . import dos as _dos
from .lnk import is_windows_path, unix_to_windows

__all__ = [
    "STATUSES",
    "HANDLERS",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_UNSUPPORTED",
    "FormatSpec",
    "Detection",
    "ActionPlan",
    "FORMATS",
    "UNKNOWN_FORMAT",
    "by_id",
    "by_suffix",
    "detect",
    "plan_action",
    "formats_table",
    "to_windows_path",
    "reg_preview",
    "reg_confirm_text",
    "parse_url_shortcut",
    "URL_SCHEMES",
    "inf_kind",
    "inf_details",
    "prefix_has_dotnet_framework",
    "parse_pif",
    "rundll32_path_tokens",
    "main",
]

log = get_logger("lindos-compat.formats")

STATUSES = ("works", "partial", "unsupported")
HANDLERS = ("run", "msiexec-install", "msiexec-patch", "msix", "appinstaller", "dos", "win16",
            "wscript", "pwsh", "regedit", "open-url", "screensaver", "control-panel",
            "inf-install", "extract", "mount", "clickonce", "explain")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSUPPORTED = 3

#: How much of a file :func:`detect` reads up front (covers the ISO/UDF descriptors at 32 KiB).
HEAD_BYTES = 64 * 1024
#: Largest single extra read at an offset (headers only; never whole files).
_MAX_EXTRA_READ = 1 << 20
#: How far into a native .exe the .NET apphost bundle marker is searched for.
_APPHOST_SCAN_BYTES = 8 << 20
#: Text files (.reg/.url/.inf/.appref-ms) larger than this are only partly read.
_TEXT_LIMIT = 64 << 20
_SMALL_TEXT_LIMIT = 1 << 20
#: A DOS .com program is one 64 KiB segment minus the 256-byte PSP.
MAX_COM_SIZE = 65280
#: Zip archives with more central-directory entries than this are not opened for classification.
_MAX_ZIP_ENTRIES = 200_000
#: MAX_PATH: Wine's InstallHinfSection copies "section mode path" into a 260-WCHAR buffer.
_WIN_MAX_PATH = 260


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatSpec:
    """One Windows file type Lindos knows (a row of SPEC-WINDOWS §28.3)."""

    id: str
    label: str
    suffixes: Tuple[str, ...]
    mime: str
    handler: str
    status: str
    note: str

    def as_dict(self) -> Dict[str, object]:
        return {"id": self.id, "label": self.label, "suffixes": list(self.suffixes), "mime": self.mime,
                "handler": self.handler, "status": self.status, "note": self.note}


@dataclass
class Detection:
    """What :func:`detect` found: the format, why, and the header facts it read."""

    format: FormatSpec
    reason: str                  # "PE32+ x64 GUI", "NE Windows 16-bit", "suffix .reg", ...
    details: Dict[str, object] = field(default_factory=dict)

    def info(self) -> Dict[str, object]:
        """The ``"format"`` object of ``lindos-run --info`` (SPEC-WINDOWS §28.3)."""
        f = self.format
        return {"id": f.id, "label": f.label, "status": f.status, "handler": f.handler, "note": f.note,
                "reason": self.reason}

    def as_dict(self) -> Dict[str, object]:
        out = self.info()
        out["details"] = dict(self.details)
        return out


@dataclass
class ActionPlan:
    """How to open a detected file.  Nothing here has been run yet.

    ``wine_tail`` is the argv after ``wine``/``umu-run`` (Windows paths, never a shell string);
    ``host_argv`` is a complete argv for a Linux tool (DOSBox, pwsh, xdg-open, udisksctl,
    cabextract).  ``details`` carries handler-specific data (e.g. ``dest`` and ``open`` for a
    CAB, the ``.reg`` preview, the Wine WoW64 mode).
    """

    handler: str
    wine_tail: List[str] = field(default_factory=list)
    host_argv: List[str] = field(default_factory=list)
    needs_prefix: bool = False
    force_runner: Optional[str] = None
    arch: Optional[str] = None
    prefix_hint: Optional[str] = None
    confirm: Optional[str] = None
    message: str = ""
    exit_code: int = EXIT_OK
    details: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {"handler": self.handler, "wine_tail": list(self.wine_tail), "host_argv": list(self.host_argv),
                "needs_prefix": self.needs_prefix, "force_runner": self.force_runner, "arch": self.arch,
                "prefix_hint": self.prefix_hint, "confirm": self.confirm, "message": self.message,
                "exit_code": self.exit_code, "details": dict(self.details)}


def _spec(fid: str, label: str, suffixes: Tuple[str, ...], mime: str, handler: str, status: str,
          note: str) -> FormatSpec:
    return FormatSpec(id=fid, label=label, suffixes=suffixes, mime=mime, handler=handler, status=status, note=note)


#: The format table (SPEC-WINDOWS §28.3).  Order matters for :func:`by_suffix`: the first
#: format listing a suffix owns it (``.exe`` → ``exe``, ``.img`` → ``iso``).
FORMATS: Tuple[FormatSpec, ...] = (
    _spec("exe", "Windows program", (".exe",), "application/x-ms-dos-executable", "run", "works",
          "Runs through Wine or Proton in its own C:\\ drive."),
    _spec("dotnet-exe", "Windows .NET program", (".exe",), "application/x-ms-dos-executable", "run", "partial",
          ".NET: wine-mono covers many; some need Microsoft .NET (licence tied to Windows)."),
    _spec("win16-exe", "16-bit Windows program", (".exe",), "application/x-ms-ne-executable", "win16", "partial",
          "Windows 3.x program: runs through Wine in a C:\\ drive that suits your Wine version."),
    _spec("dos-exe", "DOS program", (".exe",), "application/x-ms-dos-executable", "dos", "works",
          "Runs in DOSBox."),
    _spec("dos-com", "DOS program", (".com", ".pif"), "application/x-ms-dos-executable", "dos", "works",
          "Runs in DOSBox."),
    _spec("arm-exe", "Windows on ARM program", (".exe",), "application/x-ms-dos-executable", "explain",
          "unsupported", "Built for ARM Windows - get the x64 (Intel/AMD) version."),
    _spec("dll", "Windows library or driver", (".dll", ".ocx", ".sys", ".efi"),
          "application/vnd.microsoft.portable-executable", "explain", "unsupported",
          "A library/driver, not a program."),
    _spec("msi", "Windows Installer package", (".msi",), "application/x-msi", "msiexec-install", "works",
          "msiexec /i (a transform can be added: TRANSFORMS=x.mst)."),
    _spec("msp", "Windows Installer patch", (".msp",), "application/x-lindos-msp", "msiexec-patch", "partial",
          "Patches an installed program: goes into that program's own C:\\ drive."),
    _spec("mst", "Windows Installer transform", (".mst",), "application/x-ole-storage", "explain", "partial",
          "A transform is used together with its installer: lindos-run app.msi TRANSFORMS=x.mst"),
    _spec("msix", "App package (MSIX/APPX)", (".msix", ".appx"), "application/msix", "msix", "partial",
          "Packaged desktop apps usually work; UWP/WinUI apps do not."),
    _spec("msix-bundle", "App bundle (MSIX/APPX)", (".msixbundle", ".appxbundle"), "application/msixbundle",
          "msix", "partial", "The right package for this PC is picked from the bundle."),
    _spec("msix-upload", "Store upload package", (".msixupload", ".appxupload"),
          "application/x-lindos-msixupload", "msix", "partial",
          "Developer upload file: the app package inside it is installed."),
    _spec("msix-encrypted", "Encrypted Microsoft Store package", (".emsix", ".eappx", ".emsixbundle", ".eappxbundle"),
          "application/x-lindos-msix-encrypted", "explain", "unsupported",
          "Locked to the Microsoft Store (DRM): only its name and version can be shown."),
    _spec("msixvc", "Xbox / PC Game Pass game package", (".msixvc",), "application/x-lindos-msixvc", "explain",
          "unsupported", "Encrypted, licence-checked Xbox game package: cannot run here."),
    _spec("appinstaller", "App Installer file", (".appinstaller",), "application/appinstaller", "appinstaller",
          "partial", "Points to an app on the internet: downloaded only if you agree (HTTPS only)."),
    _spec("bat", "Batch script", (".bat", ".cmd"), "application/x-bat", "run", "works",
          "Runs with cmd /c in a C:\\ drive."),
    _spec("ps1", "PowerShell script", (".ps1",), "application/x-powershell", "pwsh", "partial",
          "Opens in the text editor; 'Run with PowerShell' uses PowerShell 7 (Windows-only commands are missing)."),
    _spec("vbs", "Windows Script Host script", (".vbs", ".vbe", ".wsf"), "application/x-lindos-wsf", "wscript",
          "partial", "Runs with Wine's wscript (.vbe/.wsf best effort)."),
    _spec("reg", "Registry file", (".reg",), "text/x-ms-regedit", "regedit", "works",
          "Merged into a C:\\ drive you choose, after you confirm (deletions are listed)."),
    _spec("lnk", "Windows shortcut", (".lnk",), "application/x-ms-shortcut", "run", "works",
          "Opens the program the shortcut points to."),
    _spec("url", "Internet shortcut", (".url",), "application/x-mswinurl", "open-url", "works",
          "Opens web, e-mail and FTP links in your browser; other link types are explained."),
    _spec("scr", "Screen saver", (".scr",), "application/x-ms-dos-executable", "screensaver", "works",
          "Shows the screen saver full-screen."),
    _spec("cpl", "Control Panel item", (".cpl",), "application/x-lindos-cpl", "control-panel", "partial",
          "Opened with Wine's Control Panel."),
    _spec("inf", "Setup information file (INF)", (".inf",), "application/x-wine-extension-inf", "inf-install",
          "partial", "Software INF files are installed; hardware driver INF files cannot be used on Linux."),
    _spec("cab", "Cabinet archive", (".cab",), "application/vnd.ms-cab-compressed", "extract", "works",
          "Extracted into a folder next to it, then the folder opens."),
    _spec("msu", "Windows Update package", (".msu",), "application/vnd.ms-cab-compressed", "explain",
          "unsupported", "Updates Windows itself: not applicable here (it can be extracted)."),
    _spec("iso", "Disk image", (".iso", ".img"), "application/x-cd-image", "mount", "works",
          "Opened like a DVD (read-only); a setup program is offered, never run automatically."),
    _spec("clickonce", "ClickOnce application", (".application", ".appref-ms"), "application/x-lindos-clickonce",
          "clickonce", "partial",
          "Needs Microsoft .NET Framework 4.x already in the C:\\ drive (licence tied to Windows; not auto-installed)."),
)

#: Returned by :func:`detect` for files that are none of the above (never listed by
#: :func:`formats_table`).  Its ``handler`` is ``explain``; the Detection's ``reason`` says why.
UNKNOWN_FORMAT = _spec("unknown", "Unknown or unsupported file", (), "application/octet-stream", "explain",
                       "unsupported", "Lindos does not know how to open this file.")

_BY_ID: Dict[str, FormatSpec] = {f.id: f for f in FORMATS}
_BY_SUFFIX: Dict[str, FormatSpec] = {}
for _f in FORMATS:
    for _s in _f.suffixes:
        _BY_SUFFIX.setdefault(_s, _f)
del _f, _s


def by_id(fid: str) -> Optional[FormatSpec]:
    """The format with id ``fid`` (``"unknown"`` gives :data:`UNKNOWN_FORMAT`), or None."""
    key = (fid or "").strip().lower()
    if key == UNKNOWN_FORMAT.id:
        return UNKNOWN_FORMAT
    return _BY_ID.get(key)


def _norm_suffix(suffix: str) -> str:
    text = (suffix or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        # accept "exe" as well as a whole file name ("setup.EXE")
        text = "." + text.rsplit(".", 1)[-1] if "." in text else "." + text
    return text


def by_suffix(suffix: str) -> Optional[FormatSpec]:
    """The format that owns a file suffix (case-insensitive; ``".exe"``, ``"EXE"`` or ``"x.exe"``)."""
    return _BY_SUFFIX.get(_norm_suffix(suffix))


def formats_table() -> List[Dict[str, object]]:
    """Every format as a plain dict (``lindos-compat formats --json``)."""
    return [f.as_dict() for f in FORMATS]


# ---------------------------------------------------------------------------
# byte access
# ---------------------------------------------------------------------------


class _Source:
    """The head of a file plus bounded reads at larger offsets (headers only)."""

    def __init__(self, head: bytes, path: Optional[Path], size: Optional[int]) -> None:
        self.head = head
        self.path = path
        self.size = size if size is not None else None

    def at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            return b""
        end = offset + length
        if end <= len(self.head):
            return self.head[offset:end]
        if self.path is None or self.size is None or offset >= self.size:
            return self.head[offset:end]
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                return fh.read(min(length, _MAX_EXTRA_READ))
        except OSError:
            return self.head[offset:end]


def _u16(data: bytes, off: int = 0) -> Optional[int]:
    if off < 0 or off + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int = 0) -> Optional[int]:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def _read_file(path: Path, limit: int) -> Tuple[bytes, bool]:
    """Read at most ``limit`` bytes; returns ``(data, truncated)``.  Raises OSError."""
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        return data[:limit], True
    return data, False


def _decode_text(data: bytes) -> Tuple[str, str]:
    """Decode a Windows text file.  Returns ``(text, encoding)``.

    ``encoding`` is ``utf-16-le`` / ``utf-16-be`` / ``utf-8`` (BOM or valid non-ASCII UTF-8) or
    ``ansi`` (Windows-1252, how Wine reads BOM-less REGEDIT4/INF files on a Western system).
    """
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace"), "utf-8"
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0 and data[2] != 0:
        # BOM-less UTF-16LE (ASCII text with every second byte zero)
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    try:
        text = data.decode("ascii")
        return text, "ansi"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "ansi"


def _ci_sibling(directory: Path, name: str, *, limit: int = 20000) -> Optional[Path]:
    """``directory/name`` matched case-insensitively (files copied from Windows keep odd case).

    Returns the entry with its real on-disk spelling; an exact match wins over a case variant.
    """
    lname = name.lower()
    variant: Optional[Path] = None
    try:
        with os.scandir(directory) as it:
            for i, entry in enumerate(it):
                if i >= limit:
                    break
                if entry.name.lower() != lname:
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if entry.name == name:
                    return Path(entry.path)
                variant = variant or Path(entry.path)
    except OSError:
        return None
    if variant is not None:
        return variant
    direct = directory / name
    try:
        return direct if direct.is_file() else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# PE / NE / LE header facts
# ---------------------------------------------------------------------------

IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
IMAGE_FILE_DLL = 0x2000
NE_FFLAGS_LIBMODULE = 0x8000
PE32_MAGIC = 0x10B
PE32PLUS_MAGIC = 0x20B

MACHINE_NAMES: Dict[int, str] = {
    0x0000: "unknown", 0x014C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0xA641: "arm64ec", 0xA64E: "arm64x",
    0x01C4: "armnt", 0x01C2: "thumb", 0x01C0: "arm", 0x0200: "ia64", 0x0166: "mips", 0x0169: "mips",
    0x0266: "mips", 0x0366: "mips", 0x0466: "mips", 0x0184: "alpha", 0x0284: "alpha64", 0x01F0: "powerpc",
    0x01F1: "powerpc", 0x01A2: "sh", 0x01A3: "sh", 0x01A6: "sh", 0x01A8: "sh", 0x5032: "riscv32",
    0x5064: "riscv64", 0x5128: "riscv128", 0x6232: "loongarch32", 0x6264: "loongarch64",
    0x0EBC: "efi-bytecode", 0x9041: "m32r", 0x01D3: "am33",
}
X86_MACHINES = frozenset({0x014C, 0x8664})
ARM_MACHINES = frozenset({0xAA64, 0xA641, 0xA64E, 0x01C4, 0x01C2, 0x01C0})
_CPU_LABELS = {
    "ia64": "Itanium (IA-64)", "mips": "MIPS", "alpha": "DEC Alpha", "alpha64": "DEC Alpha",
    "powerpc": "PowerPC", "sh": "SuperH", "riscv32": "RISC-V", "riscv64": "RISC-V", "riscv128": "RISC-V",
    "loongarch32": "LoongArch", "loongarch64": "LoongArch", "efi-bytecode": "EFI byte code",
    "m32r": "M32R", "am33": "AM33", "unknown": "an unknown processor",
}

SUBSYSTEM_NAMES: Dict[int, str] = {
    0: "unknown", 1: "native", 2: "gui", 3: "console", 5: "os2-console", 7: "posix-console",
    8: "native-win9x", 9: "wince-gui", 10: "efi-application", 11: "efi-boot-driver",
    12: "efi-runtime-driver", 13: "efi-rom", 14: "xbox", 16: "boot-application",
}
_SUBSYSTEM_LABELS = {"gui": "GUI", "console": "console"}
EFI_SUBSYSTEMS = frozenset({10, 11, 12, 13})
NATIVE_SUBSYSTEMS = frozenset({1, 8})

NE_OS_NAMES = {0: "unknown", 1: "OS/2", 2: "Windows", 3: "European MS-DOS 4", 4: "Windows 386", 5: "BOSS"}
_LE_MODULE_TYPE_MASK = 0x38000
_LE_LIBRARY_TYPES = frozenset({0x08000, 0x18000})
_LE_DRIVER_TYPES = frozenset({0x20000, 0x28000})

# .NET apphost/singlefilehost marker: 8-byte bundle-header offset (0 = not a single-file
# bundle) followed by SHA-256(".net core bundle") (dotnet/runtime bundle_marker.cpp).
_APPHOST_BUNDLE_SIGNATURE = bytes((
    0x8B, 0x12, 0x02, 0xB9, 0x6A, 0x61, 0x20, 0x38, 0x72, 0x7B, 0x93, 0x02, 0x14, 0xD7, 0xA0, 0x32,
    0x13, 0xF5, 0xB9, 0xE6, 0xEF, 0xAE, 0x33, 0x18, 0xEE, 0x3B, 0x2D, 0xCE, 0x24, 0xB3, 0x6A, 0xAE,
))


def _pe_headers(src: _Source, e_lfanew: int) -> Optional[Dict[str, int]]:
    """Parse the COFF + optional header facts Lindos needs.  None when the headers are cut off/invalid."""
    coff = src.at(e_lfanew + 4, 20)
    if len(coff) < 20:
        return None
    machine, nsect, _ts, _sym, _nsym, opt_size, chars = struct.unpack("<HHIIIHH", coff)
    opt = e_lfanew + 24
    hdr: Dict[str, int] = {"machine": machine, "nsections": nsect, "opt_size": opt_size,
                           "characteristics": chars, "opt": opt, "magic": 0, "subsystem": 0,
                           "clr_rva": 0, "clr_size": 0}
    if opt_size < 2:
        return hdr
    optional = src.at(opt, min(opt_size, 256))
    magic = _u16(optional, 0) or 0
    hdr["magic"] = magic
    if magic not in (PE32_MAGIC, PE32PLUS_MAGIC):
        return hdr
    sub = _u16(optional, 68)
    hdr["subsystem"] = sub if sub is not None else 0
    nrva_off, dd_off = (92, 96) if magic == PE32_MAGIC else (108, 112)
    nrva = _u32(optional, nrva_off) or 0
    clr_off = dd_off + 14 * 8
    if nrva > 14 and opt_size >= clr_off + 8:
        hdr["clr_rva"] = _u32(optional, clr_off) or 0
        hdr["clr_size"] = _u32(optional, clr_off + 4) or 0
    return hdr


def _rva_to_offset(src: _Source, hdr: Dict[str, int], rva: int) -> Optional[int]:
    base = hdr["opt"] + hdr["opt_size"]
    for i in range(min(hdr["nsections"], 96)):
        sec = src.at(base + i * 40, 40)
        if len(sec) < 40:
            return None
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", sec, 8)
        span = max(vsize, rawsize)
        if vaddr <= rva < vaddr + span:
            delta = rva - vaddr
            if delta >= rawsize:
                return None
            return rawptr + delta
    return None


def _clr_facts(src: _Source, hdr: Dict[str, int]) -> Dict[str, object]:
    """Best-effort .NET metadata version ("v4.0.30319") and CLR flags of a managed image."""
    out: Dict[str, object] = {}
    off = _rva_to_offset(src, hdr, hdr["clr_rva"])
    if off is None:
        return out
    cor = src.at(off, 24)
    if len(cor) < 20:
        return out
    md_rva = _u32(cor, 8) or 0
    flags = _u32(cor, 16) or 0
    out["clr_32bit_required"] = bool(flags & 0x2)
    md_off = _rva_to_offset(src, hdr, md_rva) if md_rva else None
    if md_off is None:
        return out
    root = src.at(md_off, 16 + 256)
    if _u32(root, 0) != 0x424A5342:  # "BSJB"
        return out
    vlen = _u32(root, 12) or 0
    if 0 < vlen <= 255:
        version = root[16:16 + vlen].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        if re.match(r"^v\d+\.\d+", version):
            out["clr_version"] = version
    return out


def _dotnet_family(clr_version: str) -> str:
    m = re.match(r"^v(\d+)\.(\d+)", clr_version or "")
    if not m:
        return ".NET Framework"
    major = int(m.group(1))
    if major == 1:
        return ".NET Framework 1.x"
    if major == 2:
        return ".NET Framework 2.0-3.5"
    return ".NET Framework 4.x"


def _is_managed_pe(path: Path) -> bool:
    """True when ``path`` is a PE image with a CLR header (a .NET assembly)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
        size = path.stat().st_size
    except OSError:
        return False
    if head[:2] != b"MZ":
        return False
    src = _Source(head, path, size)
    e = _u32(head, 0x3C)
    if e is None or src.at(e, 4) != b"PE\x00\x00":
        return False
    hdr = _pe_headers(src, e)
    return bool(hdr and hdr["clr_rva"] and hdr["clr_size"])


def _runtimeconfig_facts(path: Path) -> Dict[str, object]:
    try:
        data, _trunc = _read_file(path, _SMALL_TEXT_LIMIT)
        doc = json.loads(data.decode("utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return {}
    opts = doc.get("runtimeOptions") if isinstance(doc, dict) else None
    if not isinstance(opts, dict):
        return {}
    frameworks: List[str] = []
    items: List[object] = []
    if isinstance(opts.get("framework"), dict):
        items.append(opts["framework"])
    if isinstance(opts.get("frameworks"), list):
        items.extend(opts["frameworks"])
    self_contained = isinstance(opts.get("includedFrameworks"), list)
    if self_contained:
        items.extend(opts["includedFrameworks"])
    for fw in items:
        if isinstance(fw, dict) and fw.get("name"):
            frameworks.append(f"{fw.get('name')} {fw.get('version', '')}".strip())
    return {"dotnet_frameworks": frameworks, "dotnet_self_contained": self_contained}


def _apphost_marker(path: Path, size: Optional[int]) -> Optional[int]:
    """Bundle-header offset stored before the apphost marker (0 = plain apphost), None if absent."""
    sig = _APPHOST_BUNDLE_SIGNATURE
    limit = min(size if size is not None else _APPHOST_SCAN_BYTES, _APPHOST_SCAN_BYTES)
    chunk = 1 << 20
    overlap = len(sig) + 8
    try:
        with open(path, "rb") as fh:
            pos = 0
            tail = b""
            while pos < limit:
                block = fh.read(min(chunk, limit - pos))
                if not block:
                    break
                buf = tail + block
                idx = buf.find(sig)
                if idx >= 8:
                    return struct.unpack_from("<q", buf, idx - 8)[0]
                tail = buf[-overlap:]
                pos += len(block)
    except OSError:
        return None
    return None


def _apphost_facts(path: Optional[Path], size: Optional[int]) -> Dict[str, object]:
    """.NET Core 3.0+ apphost detection for a native .exe (siblings first, then the marker)."""
    if path is None:
        return {}
    parent, stem = path.parent, path.stem
    cfg = _ci_sibling(parent, stem + ".runtimeconfig.json")
    if cfg is not None:
        facts: Dict[str, object] = {"apphost": True, "dotnet_runtimeconfig": cfg.name}
        facts.update(_runtimeconfig_facts(cfg))
        return facts
    dll = _ci_sibling(parent, stem + ".dll")
    if dll is not None and _is_managed_pe(dll):
        return {"apphost": True, "dotnet_dll": dll.name}
    marker = _apphost_marker(path, size)
    if marker is not None:
        return {"apphost": True, "dotnet_single_file": marker > 0}
    return {}


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_CLSIDS = {
    bytes.fromhex("84100c0000000000c000000000000046"): "msi",   # {000C1084-0000-0000-C000-000000000046}
    bytes.fromhex("86100c0000000000c000000000000046"): "msp",   # {000C1086-...}
    bytes.fromhex("82100c0000000000c000000000000046"): "mst",   # {000C1082-...}
}
_LNK_MAGIC = b"L\x00\x00\x00" + bytes.fromhex("0114020000000000C000000000000046")
_ENCRYPTED_MAGICS = (b"EXPH", b"EXSH", b"EXBH")
_REG_HEADERS = ("Windows Registry Editor Version 5.00", "REGEDIT4", "REGEDIT")
_URL_FIRST_SECTIONS = ("[internetshortcut]", "[{000214a0-0000-0000-c000-000000000046}]")
_MSIX_KIND_TO_ID = {"package": "msix", "bundle": "msix-bundle", "upload": "msix-upload",
                    "encrypted": "msix-encrypted", "msixvc": "msixvc", "appinstaller": "appinstaller"}
#: Suffixes of Windows executable images: without an ``MZ`` header they are not programs.
_IMAGE_SUFFIXES = frozenset({".exe", ".dll", ".ocx", ".sys", ".efi", ".scr", ".cpl", ".drv", ".vxd"})
#: Suffixes whose format has a mandatory magic (a missing one means a damaged/incomplete file).
_MAGIC_REQUIRED = {
    ".msi": "Windows Installer package", ".msp": "Windows Installer patch", ".mst": "Windows Installer transform",
    ".lnk": "Windows shortcut", ".msix": "app package", ".appx": "app package", ".msixbundle": "app bundle",
    ".appxbundle": "app bundle", ".msixupload": "Store upload package", ".appxupload": "Store upload package",
    ".emsix": "encrypted Store package", ".eappx": "encrypted Store package",
    ".emsixbundle": "encrypted Store package", ".eappxbundle": "encrypted Store package",
    ".appinstaller": "App Installer file",
}


def _det(fid: str, reason: str, details: Dict[str, object]) -> Detection:
    return Detection(format=_BY_ID[fid], reason=reason, details=details)


def _unknown(reason: str, details: Dict[str, object], hint: str = "") -> Detection:
    if hint:
        details = dict(details)
        details["hint"] = hint
    return Detection(format=UNKNOWN_FORMAT, reason=reason, details=details)


def detect(path: Path, *, head: Optional[bytes] = None) -> Detection:
    """Classify ``path`` by content (suffix last).  Never raises on bad input.

    ``head`` may carry the first bytes of the file (for example when the caller already read
    them); further header reads still come from ``path`` when it exists.
    """
    p = Path(path)
    try:
        return _detect(p, head)
    except Exception as exc:  # noqa: BLE001 - detect() must never raise on a strange file
        log.debug("detect(%s) failed: %s", p, exc, exc_info=True)
        return _unknown(f"could not be read ({exc.__class__.__name__})", {"suffix": p.suffix.lower()})


def _detect(p: Path, head: Optional[bytes]) -> Detection:  # noqa: C901 - one linear decision list
    suffix = p.suffix.lower()
    details: Dict[str, object] = {"suffix": suffix}
    size: Optional[int] = None
    readable_path: Optional[Path] = None
    try:
        st = os.stat(p)
    except OSError as exc:
        st = None
        if head is None:
            missing = isinstance(exc, FileNotFoundError)
            details["io_error"] = True
            return _unknown("file not found" if missing else f"cannot open the file ({exc.strerror or exc})",
                            details)
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            details["io_error"] = True
            return _unknown("this is a folder, not a file", details)
        if not stat.S_ISREG(st.st_mode):
            details["io_error"] = True
            return _unknown("not a regular file (device, pipe or socket)", details)
        size = st.st_size
        readable_path = p
    if head is None:
        try:
            with open(p, "rb") as fh:
                head = fh.read(HEAD_BYTES)
        except OSError as exc:
            details["io_error"] = True
            return _unknown(f"cannot read the file ({exc.strerror or exc})", details)
    head = bytes(head)
    if size is None:
        size = len(head)
    details["size"] = size
    if size == 0 or not head:
        return _unknown("the file is empty", details,
                        "It has no content at all - if it was downloaded, the download did not finish.")
    src = _Source(head, readable_path, size)

    # -- executables -------------------------------------------------------
    if head[:2] in (b"MZ", b"ZM"):
        return _classify_mz(src, p, suffix, size, details)
    if head[:4] == b"\x7fELF":
        return _unknown("a Linux program, not a Windows one", details,
                        "Run it directly (chmod +x, then ./name) - it does not need Wine.")

    # -- containers with magic ---------------------------------------------
    if head[:8] == _OLE_MAGIC:
        return _classify_ole(src, suffix, details)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _classify_zip(readable_path, suffix, details)
    if head[:4] in _ENCRYPTED_MAGICS:
        details["magic"] = head[:4].decode("ascii")
        return _det("msix-encrypted", f"encrypted Store package ({details['magic']})", details)
    if head[:4] == b"MSCF":
        details["container"] = "cab"
        if suffix == ".msu":
            return _det("msu", "Microsoft Cabinet (Windows Update package)", details)
        return _det("cab", "Microsoft Cabinet", details)
    if head[:4] == b"ISc(":
        return _unknown("an InstallShield data cabinet (part of an installer)", details,
                        "Run the setup.exe that is in the same folder instead.")
    if head[:20] == _LNK_MAGIC:
        return _det("lnk", "Windows shortcut header", details)
    iso = _iso_descriptors(src)
    if iso:
        details.update(iso)
        kind = "UDF" if iso.get("udf") and not iso.get("iso9660") else ("ISO 9660 + UDF" if iso.get("udf")
                                                                        else "ISO 9660")
        return _det("iso", f"{kind} disc image", details)

    # -- text formats with a recognisable start ------------------------------
    text_kind = _text_kind(head)
    if text_kind == "reg":
        return _det("reg", "registry file header", details)
    if text_kind == "url":
        return _det("url", "[InternetShortcut] section", details)
    if text_kind == "appinstaller":
        return _det("appinstaller", "<AppInstaller> XML", details)

    return _suffix_fallback(p, suffix, size, details, src)


def _classify_mz(src: _Source, p: Path, suffix: str, size: int, details: Dict[str, object]) -> Detection:
    details["magic"] = "MZ"
    if size < 0x1C:
        return _unknown("damaged program (the MZ header is cut off)", details,
                        "The file is incomplete - download it again.")
    e_lfanew = _u32(src.at(0x3C, 4))
    if e_lfanew is not None and 4 <= e_lfanew and e_lfanew + 4 <= size:
        details["e_lfanew"] = e_lfanew
        sig = src.at(e_lfanew, 4)
        if sig == b"PE\x00\x00":
            return _classify_pe(src, p, suffix, size, e_lfanew, details)
        if sig[:2] == b"NE":
            return _classify_ne(src, e_lfanew, details)
        if sig[:2] in (b"LE", b"LX"):
            return _classify_le(src, e_lfanew, sig[:2].decode("ascii"), details)
    details.pop("e_lfanew", None)
    if p.suffix.lower() == ".pif":
        details["pif_is_program"] = True
    return _det("dos-exe", "MZ DOS program", details)


def _classify_pe(src: _Source, p: Path, suffix: str, size: int, e_lfanew: int,
                 details: Dict[str, object]) -> Detection:
    hdr = _pe_headers(src, e_lfanew)
    if hdr is None:
        return _unknown("damaged Windows program (its headers are cut off)", details,
                        "The file is incomplete - download it again.")
    machine, chars, magic, subsystem = hdr["machine"], hdr["characteristics"], hdr["magic"], hdr["subsystem"]
    machine_name = MACHINE_NAMES.get(machine, f"0x{machine:04x}")
    is_dll = bool(chars & IMAGE_FILE_DLL)
    clr = bool(hdr["clr_rva"] and hdr["clr_size"])
    details.update({"machine": machine, "machine_name": machine_name, "characteristics": chars,
                    "is_dll": is_dll, "clr": clr, "apphost": False})
    if magic not in (PE32_MAGIC, PE32PLUS_MAGIC):
        return _unknown("damaged Windows program (invalid optional header)", details,
                        "Windows itself would refuse to start this file.")
    bits = 64 if magic == PE32PLUS_MAGIC else 32
    sub_name = SUBSYSTEM_NAMES.get(subsystem, str(subsystem))
    details.update({"bits": bits, "subsystem": subsystem, "subsystem_name": sub_name})
    kind = "PE32+" if bits == 64 else "PE32"
    sub_label = _SUBSYSTEM_LABELS.get(sub_name, sub_name)
    base_reason = f"{kind} {machine_name} {sub_label}"
    if clr:
        details.update(_clr_facts(src, hdr))

    if subsystem in EFI_SUBSYSTEMS:
        details["efi"] = True
        return _det("dll", f"{kind} {machine_name} UEFI {sub_name.replace('efi-', '')}", details)
    if subsystem in NATIVE_SUBSYSTEMS:
        details["driver"] = True
        return _det("dll", f"{kind} {machine_name} Windows driver (native subsystem)", details)
    if subsystem == 16:
        details["boot_application"] = True
        return _det("dll", f"{kind} {machine_name} Windows boot component", details)
    if is_dll:
        if suffix == ".cpl" and machine in X86_MACHINES:
            return _det("cpl", f"{kind} {machine_name} Control Panel DLL", details)
        return _det("dll", f"{kind} {machine_name} DLL" + (" (.NET)" if clr else ""), details)
    if machine in ARM_MACHINES:
        return _det("arm-exe", base_reason, details)
    if machine not in X86_MACHINES:
        cpu = _CPU_LABELS.get(machine_name, machine_name)
        details["cpu"] = cpu
        reason = "built for an unknown processor type" if machine_name == "unknown" else f"built for {cpu} Windows"
        return _unknown(reason, details, "Get the x64 (Intel/AMD) version of this program.")
    if subsystem in (5, 7):
        what = "OS/2" if subsystem == 5 else "POSIX (Interix)"
        return _unknown(f"a {what} subsystem program", details,
                        "Wine cannot run programs written for this old Windows subsystem.")
    if subsystem == 9:
        return _unknown("a Windows CE program", details,
                        "Programs for Windows CE (old handhelds) do not run on PCs.")
    if subsystem == 14:
        return _unknown("an original-Xbox executable", details, "Xbox console programs do not run on PCs.")
    if suffix == ".cpl":
        return _det("cpl", base_reason + " (Control Panel item)", details)
    if suffix == ".scr":
        return _det("scr", base_reason + " (screen saver)", details)
    if clr:
        return _det("dotnet-exe", base_reason + " (.NET)", details)
    apphost = _apphost_facts(src.path, size) if suffix == ".exe" else {}
    if apphost:
        details.update(apphost)
        return _det("dotnet-exe", base_reason + " (.NET apphost)", details)
    return _det("exe", base_reason, details)


def _classify_ne(src: _Source, ne: int, details: Dict[str, object]) -> Detection:
    hdr = src.at(ne, 0x40)
    ne_flags = _u16(hdr, 0x0C)
    if ne_flags is None or len(hdr) < 0x37:
        return _det("dos-exe", "MZ DOS program (truncated NE header)", details)
    exetyp = hdr[0x36]
    os_name = NE_OS_NAMES.get(exetyp, f"type {exetyp}")
    details.update({"new_exe": "NE", "ne_exetyp": exetyp, "ne_flags": ne_flags, "ne_os": os_name})
    if ne_flags & NE_FFLAGS_LIBMODULE:
        details["is_dll"] = True
        return _det("dll", f"NE 16-bit {os_name} library", details)
    if exetyp in (2, 4):
        return _det("win16-exe", f"NE {os_name} 16-bit", details)
    details["partial_note"] = (f"This is an {os_name} program. Only its DOS part can run (in DOSBox); "
                               "the rest needs the original system.")
    return _det("dos-exe", f"NE {os_name} program (DOS part)", details)


def _classify_le(src: _Source, le: int, sig: str, details: Dict[str, object]) -> Detection:
    hdr = src.at(le, 0x14)
    flags = _u32(hdr, 0x10)
    ostype = _u16(hdr, 0x0A)
    details.update({"new_exe": sig})
    if ostype is not None:
        details["le_os"] = NE_OS_NAMES.get(ostype, f"type {ostype}")
    if flags is not None:
        module_type = flags & _LE_MODULE_TYPE_MASK
        details["le_module_type"] = module_type
        if module_type in _LE_DRIVER_TYPES:
            details["driver"] = True
            return _det("dll", f"{sig} device driver (VxD)", details)
        if module_type in _LE_LIBRARY_TYPES:
            details["is_dll"] = True
            return _det("dll", f"{sig} library", details)
    details["partial_note"] = ("DOS-extender program (for example DOS/4GW): runs in DOSBox. "
                               "OS/2 programs or Windows 9x VxD files will not run.")
    return _det("dos-exe", f"{sig} executable (DOS extender)", details)


def _ole_root_clsid(src: _Source) -> Optional[bytes]:
    shift = _u16(src.at(0x1E, 2))
    first_dir = _u32(src.at(0x30, 4))
    if shift not in (9, 12) or first_dir is None or first_dir >= 0xFFFFFFFA:
        return None
    entry = src.at((first_dir + 1) << shift, 128)
    if len(entry) < 128 or entry[0x42] != 5:  # 5 = root storage object
        return None
    return entry[0x50:0x60]


def _classify_ole(src: _Source, suffix: str, details: Dict[str, object]) -> Detection:
    details["container"] = "ole"
    clsid = _ole_root_clsid(src)
    by_clsid = _OLE_CLSIDS.get(clsid or b"")
    if by_clsid:
        details["ole_clsid"] = by_clsid
        return _det(by_clsid, f"OLE compound file ({by_clsid.upper()} class id)", details)
    if suffix in (".msi", ".msp", ".mst"):
        return _det(suffix[1:], f"OLE compound file, suffix {suffix}", details)
    return _unknown("an OLE compound file (for example an old Office document)", details,
                    "Open it with the program that made it (LibreOffice opens old Office files).")


def _zip_entry_count(path: Path) -> Optional[int]:
    """Total entries from the (Zip64) end-of-central-directory record, without opening the archive."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            tail_len = min(size, 65536 + 22)
            fh.seek(size - tail_len)
            tail = fh.read(tail_len)
    except OSError:
        return None
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0 or idx + 22 > len(tail):
        return None
    total = _u16(tail, idx + 10)
    if total != 0xFFFF:
        return total
    loc = idx - 20
    if loc >= 0 and tail[loc:loc + 4] == b"PK\x06\x07":
        z64_off = struct.unpack_from("<Q", tail, loc + 8)[0]
        try:
            with open(path, "rb") as fh:
                fh.seek(z64_off)
                rec = fh.read(56)
        except (OSError, ValueError, OverflowError):
            return None
        if rec[:4] == b"PK\x06\x06" and len(rec) >= 40:
            return struct.unpack_from("<Q", rec, 32)[0]
    return None


def _zip_kind_fallback(path: Path) -> str:
    """Stand-in for ``msix.classify`` on ZIP files when that module is unavailable."""
    count = _zip_entry_count(path)
    if count is not None and count > _MAX_ZIP_ENTRIES:
        return "unknown"
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError):
        return "unknown"
    nameset = set(names)
    has_pkg = "AppxManifest.xml" in nameset
    has_bundle = "AppxMetadata/AppxBundleManifest.xml" in nameset
    if has_pkg and has_bundle:
        return "unknown"  # both manifests: corrupt
    if has_bundle:
        return "bundle"
    if has_pkg:
        return "package"
    inner = (".msix", ".appx", ".msixbundle", ".appxbundle")
    if any(n.lower().endswith(inner) for n in names):
        return "upload"
    return "unknown"


def _msix_kind(path: Path) -> str:
    try:
        from . import msix as _msix  # W-B's module; guarded so a partial install still classifies
    except Exception as exc:  # noqa: BLE001 - ImportError or a module mid-upgrade
        log.debug("msix module unavailable (%s); using the zip fallback", exc)
        return _zip_kind_fallback(path)
    try:
        kind = str(_msix.classify(path))
    except Exception as exc:  # noqa: BLE001 - classify() on a hostile zip
        log.debug("msix.classify(%s) failed: %s", path, exc)
        return _zip_kind_fallback(path)
    return kind


def _classify_zip(path: Optional[Path], suffix: str, details: Dict[str, object]) -> Detection:
    details["container"] = "zip"
    kind = _msix_kind(path) if path is not None else "unknown"
    details["msix_kind"] = kind
    fid = _MSIX_KIND_TO_ID.get(kind)
    if fid:
        return _det(fid, f"ZIP container: MSIX {kind}", details)
    if suffix in _IMAGE_SUFFIXES:
        return _unknown(f"a ZIP archive named {suffix}", details,
                        "It is not a Windows program. Open it with the archive manager.")
    spec = by_suffix(suffix)
    if spec is not None and spec.id in ("msix", "msix-bundle", "msix-upload"):
        return _det(spec.id, f"ZIP container, suffix {suffix} (no app manifest found)", details)
    if spec is not None and suffix not in _MAGIC_REQUIRED:
        return _det(spec.id, f"suffix {suffix} (ZIP content)", details)
    return _unknown("a ZIP archive", details, "Open it with the archive manager.")


def _iso_descriptors(src: _Source) -> Dict[str, object]:
    """ISO 9660 / UDF volume recognition sequence at 32 KiB (2 KiB or 4 KiB sectors)."""
    found: Dict[str, object] = {}
    for k in range(16):
        ident = src.at(0x8000 + k * 0x800 + 1, 5)
        if len(ident) < 5:
            break
        if ident == b"CD001":
            found["iso9660"] = True
        elif ident in (b"NSR02", b"NSR03"):
            found["udf"] = True
        elif ident == b"BEA01":
            found.setdefault("udf_bea", True)
    if not (found.get("iso9660") or found.get("udf")):
        return {}
    found.pop("udf_bea", None)
    return found


_XML_ROOT_RE = re.compile(r"<\s*(?:[A-Za-z_][\w.\-]*:)?([A-Za-z_][\w.\-]*)")


def _text_kind(head: bytes) -> Optional[str]:
    """Recognise .reg / .url / .appinstaller by their first meaningful line or XML root."""
    sample = head[:8192]
    text, _enc = _decode_text(sample)
    stripped = text.lstrip("\ufeff \t\r\n")
    first = stripped.split("\n", 1)[0].strip()
    if first in _REG_HEADERS:
        return "reg"
    if first.lower() in _URL_FIRST_SECTIONS:
        return "url"
    if stripped.startswith("<"):
        body = re.sub(r"<\?.*?\?>", "", stripped, flags=re.S)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        body = re.sub(r"<!DOCTYPE[^>]*>", "", body, flags=re.S | re.I).lstrip()
        m = _XML_ROOT_RE.match(body)
        if m and m.group(1) == "AppInstaller":
            return "appinstaller"
    return None


def _suffix_fallback(p: Path, suffix: str, size: int, details: Dict[str, object], src: _Source) -> Detection:
    if suffix in (".com", ".pif"):
        if suffix == ".pif":
            pif = _parse_pif_bytes(src.at(0, 1024))
            if pif:
                details["pif"] = pif
                return _det("dos-com", "PIF settings file for a DOS program", details)
        if suffix == ".com" and size > MAX_COM_SIZE:
            return _unknown("named .com but too big to be a DOS program", details,
                            "A DOS .com program is at most 64 KB. This may be a Linux program - "
                            "try running it directly.")
        return _det("dos-com", f"suffix {suffix} (no MZ header)", details)
    if suffix in _IMAGE_SUFFIXES:
        return _unknown(f"named {suffix} but it has no Windows program header", details,
                        "The file is damaged or incomplete - if you downloaded it, download it again.")
    if suffix in _MAGIC_REQUIRED:
        what = _MAGIC_REQUIRED[suffix]
        return _unknown(f"named {suffix} but it is not a valid {what}", details,
                        "The file is damaged or incomplete - if you downloaded it, download it again.")
    spec = by_suffix(suffix)
    if spec is not None:
        return _det(spec.id, f"suffix {suffix}", details)
    return _unknown(f"unknown file type ({suffix or 'no suffix'})", details)


# ---------------------------------------------------------------------------
# Windows paths
# ---------------------------------------------------------------------------

_HOST_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def to_windows_path(path: Path, prefix: Optional[Path] = None) -> str:
    """The Windows path Wine should be given for ``path``.

    Inside ``<prefix>/drive_c`` → ``C:\\...`` (via :func:`lnk.unix_to_windows`); anywhere else
    → Wine's default ``Z:`` drive, which maps to ``/`` (``/home/a/x.msi`` → ``Z:\\home\\a\\x.msi``).
    Relative paths are made absolute against the current directory.
    """
    text = os.fspath(path)
    if prefix is not None:
        inside = unix_to_windows(text, prefix)
        if inside:
            return inside
    posix = text
    if os.sep == "\\":  # a developer's Windows machine: normalise to the Linux shape
        posix = text.replace("\\", "/")
        if _HOST_DRIVE_RE.match(posix):
            posix = posix[2:]
    if not posix.startswith("/"):
        cwd = os.getcwd()
        if os.sep == "\\":
            cwd = cwd.replace("\\", "/")
            cwd = cwd[2:] if _HOST_DRIVE_RE.match(cwd) else cwd
        posix = posixpath.join(cwd, posix)
    posix = posixpath.normpath(posix)
    if posix.startswith("//"):
        posix = "/" + posix.lstrip("/")
    return "Z:" + posix.replace("/", "\\")


def rundll32_path_tokens(win_path: str) -> List[str]:
    """Split a path for ``rundll32 <dll>,<entry> ... <path>`` so Wine passes it *unquoted*.

    ``InstallHinfSection`` and ``ShOpenVerbApplication`` take "the rest of the command line"
    verbatim; Wine rebuilds that line from argv and quotes any token that contains a space.
    Splitting on single spaces makes the rebuilt line identical to the path.  Paths with
    tabs, quotes, line breaks, doubled or edge spaces cannot be passed this way → ValueError.
    """
    if not win_path or win_path != win_path.strip(" ") or "  " in win_path:
        raise ValueError("path has leading, trailing or doubled spaces")
    if any(c in win_path for c in "\"\t\r\n\x00"):
        raise ValueError("path contains quotes, tabs or line breaks")
    return win_path.split(" ")


# ---------------------------------------------------------------------------
# .reg preview
# ---------------------------------------------------------------------------

_REG_VALUE_NAME = r'"(?:[^"\\]|\\.)*"'
_REG_DELETE_VALUE_RE = re.compile(r'^\s*(' + _REG_VALUE_NAME + r'|@)\s*=\s*-\s*$')
_REG_SET_VALUE_RE = re.compile(r'^\s*(' + _REG_VALUE_NAME + r'|@)\s*=')


def _unescape_reg_name(quoted: str) -> str:
    if quoted == "@":
        return "(Default)"
    body = quoted[1:-1]
    return re.sub(r"\\(.)", r"\1", body)


def reg_preview(path: Path) -> Dict[str, object]:
    """What importing a ``.reg`` file would change.  Raises OSError if the file cannot be read.

    Returns ``{"header", "valid", "encoding", "adds", "deleted_keys", "deleted_values",
    "value_count", "truncated"}``: ``adds`` are keys created/changed (``[KEY]``),
    ``deleted_keys`` come from ``[-KEY]`` and ``deleted_values`` (``KEY\\Name``, ``KEY\\(Default)``)
    from ``"Name"=-`` / ``@=-``.  ``header`` is the recognised header ("" when missing).
    """
    data, truncated = _read_file(Path(path), _TEXT_LIMIT)
    text, encoding = _decode_text(data)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = ""
    idx = 0
    while idx < len(lines):
        candidate = lines[idx].strip().lstrip("\ufeff")
        idx += 1
        if candidate:
            header = candidate if candidate in _REG_HEADERS else ""
            break
    adds: List[str] = []
    seen_adds = set()
    deleted_keys: List[str] = []
    deleted_values: List[str] = []
    value_count = 0
    current: Optional[str] = None
    current_deleted = False
    continuation = False
    for raw in lines[idx:]:
        line = raw.strip()
        if continuation:
            continuation = line.endswith("\\")
            continue
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            key = line[1:line.rindex("]")].strip()
            if key.startswith("-"):
                name = key[1:].strip()
                if name:
                    deleted_keys.append(name)
                current, current_deleted = name, True
            else:
                current, current_deleted = key, False
                if key and key.lower() not in seen_adds:
                    seen_adds.add(key.lower())
                    adds.append(key)
            continue
        if current is None or current_deleted:
            # values outside a key, or below a deleted key, are ignored by regedit
            continuation = line.endswith("\\")
            continue
        m = _REG_DELETE_VALUE_RE.match(line)
        if m:
            deleted_values.append(f"{current}\\{_unescape_reg_name(m.group(1))}")
            continue
        if _REG_SET_VALUE_RE.match(line):
            value_count += 1
            continuation = line.endswith("\\")
    return {"header": header, "valid": bool(header), "encoding": encoding, "adds": adds,
            "deleted_keys": deleted_keys, "deleted_values": deleted_values, "value_count": value_count,
            "truncated": truncated}


def _bullet_list(items: Sequence[str], limit: int) -> List[str]:
    out = [f"  - {item}" for item in items[:limit]]
    if len(items) > limit:
        out.append(f"  ... and {len(items) - limit} more")
    return out


def reg_confirm_text(name: str, preview: Dict[str, object], *, limit: int = 12) -> str:
    """The question to ask before ``wine regedit /S`` (Wine's regedit never asks by itself)."""
    adds = list(preview.get("adds") or [])
    dkeys = list(preview.get("deleted_keys") or [])
    dvals = list(preview.get("deleted_values") or [])
    lines = [f"Add the settings in \u201c{name}\u201d to this C:\\ drive's registry?",
             "Changing the registry can stop programs from working correctly. "
             "Only continue if you trust where this file came from."]
    if adds:
        lines.append(f"It creates or changes {len(adds)} key(s):")
        lines += _bullet_list(adds, limit)
    if dkeys:
        lines.append(f"It DELETES {len(dkeys)} key(s) and everything in them:")
        lines += _bullet_list(dkeys, limit)
    if dvals:
        lines.append(f"It DELETES {len(dvals)} value(s):")
        lines += _bullet_list(dvals, limit)
    if preview.get("truncated"):
        lines.append("(The file is very large; only its beginning was checked.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# .url
# ---------------------------------------------------------------------------

URL_SCHEMES = ("http", "https", "mailto", "ftp")
_URL_MAX = 8192
_SCRIPT_SCHEMES = ("javascript", "vbscript", "jscript", "livescript")


def _ini_sections(text: str) -> Dict[str, Dict[str, str]]:
    """Tiny INI reader: lower-case section/key names, first occurrence wins (like Windows)."""
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), {})
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current.setdefault(key.strip().lower(), value.strip())
    return sections


def parse_url_shortcut(path: Path) -> Tuple[str, bool, str]:
    """Read a Windows Internet shortcut (``.url``).  Returns ``(url, allowed, reason)``.

    Only ``http``/``https``/``mailto``/``ftp`` links are allowed (they go to ``xdg-open``);
    ``file:``, script (``javascript:``, ``vbscript:``), ``data:`` and every other scheme are
    refused with a plain-language reason.  Never raises for unreadable/garbled files.
    """
    try:
        data, _trunc = _read_file(Path(path), _SMALL_TEXT_LIMIT)
    except OSError as exc:
        return "", False, f"The shortcut could not be read ({exc.strerror or exc})."
    text, _enc = _decode_text(data)
    sections = _ini_sections(text)
    url = ""
    wide = sections.get("internetshortcut.w", {}).get("url", "")
    if wide:
        try:
            url = codecs.decode(wide.encode("ascii", errors="strict"), "utf-7").strip()
        except (UnicodeError, ValueError):
            url = ""
    if not url:
        url = sections.get("internetshortcut", {}).get("url", "").strip()
    if not url:
        return "", False, "This Internet shortcut has no web address (URL=) in it."
    if len(url) > _URL_MAX:
        return url[:200], False, "The web address in this shortcut is far too long to be a real link."
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return url, False, "The web address in this shortcut contains control characters, so it is not opened."
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", url)
    if not m:
        return url, False, "The address in this shortcut is not a valid web link."
    scheme = m.group(1).lower()
    if scheme in _SCRIPT_SCHEMES:
        return url, False, (f"This shortcut contains script code ('{scheme}:') instead of a web address; "
                            "Lindos does not run it.")
    if scheme == "file":
        return url, False, ("Links to files on a computer ('file:') are not opened from Internet shortcuts, "
                            "because they can start programs without asking.")
    if scheme == "data":
        return url, False, "This shortcut carries embedded data ('data:') instead of a web address; it is not opened."
    if scheme == "steam":
        return url, False, "This is a Steam shortcut ('steam:'). Open Steam and start the game from your Library."
    if scheme not in URL_SCHEMES:
        return url, False, (f"Lindos only opens web (http/https), e-mail (mailto) and FTP links from Internet "
                            f"shortcuts; this one uses '{scheme}:'.")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, False, "The web address in this shortcut is not valid."
    if scheme == "mailto":
        if not parts.path and not parts.query:
            return url, False, "This e-mail link has no address in it."
        return url, True, "e-mail link"
    if not parts.netloc or any(c.isspace() for c in parts.netloc):
        return url, False, "The web address in this shortcut has no valid site name."
    return url, True, ("FTP link" if scheme == "ftp" else "web link")


# ---------------------------------------------------------------------------
# .inf
# ---------------------------------------------------------------------------

_INF_DRIVER_DIRID = "12"  # %SystemRoot%\system32\drivers
_INF_NON_INSTALL_PARTS = frozenset({"services", "hw", "coinstallers", "interfaces", "wmi", "events", "filters"})
_SYS_FILE_RE = re.compile(r"(?<![\w$~.\-])[\w$~.\-]+\.sys\b", re.I)


def _inf_parse(text: str) -> Dict[str, List[Tuple[str, str]]]:
    """INF sections → list of (key, value) (value "" for bare lines).  Keys lower-cased."""
    sections: Dict[str, List[Tuple[str, str]]] = {}
    current: Optional[List[Tuple[str, str]]] = None
    pending = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = pending + raw
        pending = ""
        stripped = _strip_inf_comment(line).strip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending = stripped[:-1] + " "
            continue
        if not stripped:
            continue
        if stripped.startswith("[") and "]" in stripped:
            name = stripped[1:stripped.index("]")].strip().lower()
            current = sections.setdefault(name, [])
            continue
        if current is None:
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            current.append((key.strip().strip('"').lower(), value.strip()))
        else:
            current.append(("", stripped))
    return sections


def _strip_inf_comment(line: str) -> str:
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ";" and not in_quote:
            return line[:i]
    return line


def _inf_expand(value: str, strings: Dict[str, str]) -> str:
    """Replace ``%name%`` tokens from the [Strings] section (``%%`` is a literal percent)."""
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        if not key:
            return "%"
        return strings.get(key.lower(), m.group(0))
    return re.sub(r"%([^%]*)%", repl, value.strip())


def _inf_int(value: str) -> Optional[int]:
    text = value.strip().strip('"').lower()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


def _is_default_install(name: str) -> bool:
    if not name.startswith("defaultinstall"):
        return False
    rest = name[len("defaultinstall"):]
    if rest and not rest.startswith("."):
        return False
    parts = [p for p in rest.split(".") if p]
    return not any(p in _INF_NON_INSTALL_PARTS for p in parts)


def inf_details(path: Path) -> Dict[str, object]:
    """Classify an ``.inf`` file.  ``{"kind", "reasons", "sections", "autorun"}``; never raises.

    ``kind`` is ``driver`` when any hardware-driver signal is present ([Manufacturer], a
    [Version] Class/ClassGuid, a ``.sys`` file, the drivers directory (dirid 12) or a
    kernel/file-system service), else ``software`` when a ``[DefaultInstall*]`` section exists,
    else ``unknown``.  ``autorun`` holds ``[AutoRun]`` keys when the file is a CD ``autorun.inf``.
    """
    out: Dict[str, object] = {"kind": "unknown", "reasons": [], "sections": [], "autorun": {}}
    try:
        data, _trunc = _read_file(Path(path), 4 << 20)
    except OSError as exc:
        out["reasons"] = [f"cannot read the file ({exc.strerror or exc})"]
        return out
    text, _enc = _decode_text(data)
    sections = _inf_parse(text)
    out["sections"] = sorted(sections)
    reasons: List[str] = []
    if "manufacturer" in sections:
        reasons.append("[Manufacturer] section (hardware models)")
    for key, value in sections.get("version", []):
        if key in ("class", "classguid") and value.strip().strip('"'):
            reasons.append(f"device {key} '{value.strip()}' in [Version]")
    for key, value in sections.get("destinationdirs", []):
        if value.split(",")[0].strip() == _INF_DRIVER_DIRID:
            reasons.append("copies files into the Windows drivers folder")
            break
    strings = {k: v.strip().strip('"') for k, v in sections.get("strings", []) if k}
    for name, entries in sections.items():
        if name == "strings" or name.startswith("strings."):
            continue
        for key, value in entries:
            if key == "servicetype" and _inf_int(_inf_expand(value, strings)) in (1, 2):
                reasons.append(f"installs a kernel driver service ([{name}])")
            if _SYS_FILE_RE.search(value) or _SYS_FILE_RE.search(key):
                reasons.append(f"references a .sys driver file ([{name}])")
    reasons = list(dict.fromkeys(reasons))
    installs = [n for n in sections if _is_default_install(n)]
    autorun = {k: v for k, v in sections.get("autorun.amd64", sections.get("autorun", []))
               if k in ("open", "shellexecute", "icon", "label")}
    out["autorun"] = autorun
    if reasons:
        out["kind"] = "driver"
    elif installs:
        out["kind"] = "software"
        reasons.append("has " + ", ".join(f"[{n}]" for n in installs))
    else:
        reasons.append("no [DefaultInstall] section")
    out["reasons"] = reasons
    return out


def inf_kind(path: Path) -> str:
    """``"software"`` (installable with setupapi), ``"driver"`` (hardware driver) or ``"unknown"``."""
    return str(inf_details(path)["kind"])


# ---------------------------------------------------------------------------
# .NET Framework in a C:\ drive
# ---------------------------------------------------------------------------

_NDP_KEYS = ("software\\microsoft\\net framework setup\\ndp\\v4\\full",
             "software\\wow6432node\\microsoft\\net framework setup\\ndp\\v4\\full")


def _reg_file_key(line: str) -> Optional[str]:
    """Key name of a Wine ``system.reg`` section line (``[Software\\\\Microsoft\\\\...] 1700000000``)."""
    if not line.startswith("["):
        return None
    end = line.rfind("]")
    if end <= 0:
        return None
    return line[1:end].replace("\\\\", "\\").lower()


def prefix_has_dotnet_framework(prefix: Path) -> bool:
    """True when the C:\\ drive has Microsoft .NET Framework 4.x installed (needed for ClickOnce).

    Reads ``<prefix>/system.reg`` for ``NET Framework Setup\\NDP\\v4\\Full`` with a ``Release``
    (4.5+) or ``Install=dword:1`` value, and requires the native framework files
    (``clr.dll`` or ``dfshim.dll``) so that wine-mono's compatibility keys are not mistaken
    for the real thing.
    """
    root = Path(prefix)
    reg = root / "system.reg"
    found = False
    try:
        with open(reg, "r", encoding="utf-8", errors="replace") as fh:
            in_key = False
            for raw in fh:
                line = raw.strip()
                key = _reg_file_key(line)
                if key is not None:
                    in_key = key in _NDP_KEYS
                    continue
                if not in_key:
                    continue
                low = line.lower()
                m = re.match(r'^"release"=dword:([0-9a-f]{1,8})$', low)
                if m and int(m.group(1), 16) > 0:
                    found = True
                    break
                if re.match(r'^"install"=dword:0*1$', low):
                    found = True
                    break
    except OSError:
        return False
    if not found:
        return False
    windows = root / "drive_c" / "windows"
    candidates = [windows / "system32" / "dfshim.dll", windows / "syswow64" / "dfshim.dll",
                  windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "clr.dll",
                  windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "clr.dll"]
    return any(c.is_file() for c in candidates)


# ---------------------------------------------------------------------------
# .pif
# ---------------------------------------------------------------------------


def _cstr(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("cp437", errors="replace").strip()


def _parse_pif_bytes(data: bytes) -> Optional[Dict[str, str]]:
    # Windows PIF: basic section at 0 (title @0x02, program @0x24, directory @0x65, parameters
    # @0xA5) followed by the "MICROSOFT PIFEX" section header at 0x171.
    if len(data) < 0x171 + 15 or data[0x171:0x171 + 15] != b"MICROSOFT PIFEX":
        return None
    program = _cstr(data[0x24:0x24 + 63])
    if not program:
        return None
    return {"title": _cstr(data[0x02:0x02 + 30]), "program": program,
            "workdir": _cstr(data[0x65:0x65 + 64]), "params": _cstr(data[0xA5:0xA5 + 64])}


def parse_pif(path: Path) -> Optional[Dict[str, str]]:
    """``{"title","program","workdir","params"}`` of a Windows ``.pif`` file, or None."""
    try:
        data, _trunc = _read_file(Path(path), 4096)
    except OSError:
        return None
    return _parse_pif_bytes(data)


# ---------------------------------------------------------------------------
# plan_action
# ---------------------------------------------------------------------------

Which = Callable[[str], Optional[str]]


def _explain(message: str, *, exit_code: int = EXIT_UNSUPPORTED, **details: object) -> ActionPlan:
    return ActionPlan(handler="explain", message=message, exit_code=exit_code, details=dict(details))


def plan_action(path: Path, det: Detection, args: Sequence[str] = (), *, which: Which = shutil.which,
                run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
                prefix: Optional[Path] = None, wine_mode: Optional[str] = None,
                home: Optional[Path] = None) -> ActionPlan:
    """How to open ``path`` (already :func:`detect`-ed).  Pure except for two cached probes.

    ``args`` are the program's own arguments.  Optional keywords (additions to the §28.2
    signature): ``run`` (for the DOSBox flavour and Wine WoW64 probes), ``prefix`` (the chosen
    C:\\ drive: files inside it get ``C:\\`` paths; ClickOnce checks it for .NET Framework),
    ``wine_mode`` (skip the WoW64 probe) and ``home`` (for the DOSBox Flatpak sandbox check).
    Never raises: problems come back as an ``explain`` plan with a non-zero exit code.
    """
    p = Path(path)
    try:
        return _plan(p, det, [str(a) for a in args], which=which, run=run, prefix=prefix, wine_mode=wine_mode,
                     home=home)
    except Exception as exc:  # noqa: BLE001 - a plan must never crash lindos-run
        log.debug("plan_action(%s) failed: %s", p, exc, exc_info=True)
        return _explain(f"Lindos could not prepare \u201c{p.name}\u201d: {exc}", exit_code=EXIT_ERROR)


def _plan(p: Path, det: Detection, args: List[str], *, which: Which, run: Callable[..., object],
          prefix: Optional[Path], wine_mode: Optional[str], home: Optional[Path]) -> ActionPlan:  # noqa: C901
    fid = det.format.id
    name = p.name
    abspath = os.path.abspath(os.fspath(p))

    def win() -> str:
        return to_windows_path(p, prefix)

    if fid in ("exe", "dotnet-exe", "lnk"):
        message = _dotnet_message(det) if fid == "dotnet-exe" else ""
        return ActionPlan(handler="run", wine_tail=[win()] + args, needs_prefix=True, message=message)
    if fid == "bat":
        return ActionPlan(handler="run", wine_tail=["cmd", "/c", win()] + args, needs_prefix=True)
    if fid == "scr":
        return ActionPlan(handler="screensaver", wine_tail=[win()] + (args or ["/s"]), needs_prefix=True)
    if fid == "cpl":
        return ActionPlan(handler="control-panel", wine_tail=["control", win()] + args, needs_prefix=True,
                          force_runner="wine", message=det.format.note)
    if fid == "msi":
        return ActionPlan(handler="msiexec-install",
                          wine_tail=["msiexec", "/i", win()] + _msi_args(args, p, prefix),
                          needs_prefix=True, force_runner="wine")
    if fid == "msp":
        return _plan_msp(p, args, prefix, win())
    if fid == "mst":
        return _plan_mst(p)
    if fid in ("msix", "msix-bundle", "msix-upload"):
        return ActionPlan(handler="msix", needs_prefix=True,
                          confirm=f"Install the app package \u201c{name}\u201d into its own C:\\ drive?",
                          message=det.format.note)
    if fid == "msix-encrypted":
        return _plan_encrypted(p)
    if fid == "msixvc":
        return _explain("This is an Xbox / PC Game Pass game package (.msixvc). It is encrypted and licence-checked "
                        "by the Xbox app, so it cannot run on Lindos. Options: Xbox Cloud Gaming in Microsoft Edge "
                        "or Google Chrome (see: lindos-game route <game>), or play it on Windows.")
    if fid == "appinstaller":
        return _plan_appinstaller(p)
    if fid == "ps1":
        return _plan_ps1(p, abspath, args, which)
    if fid == "vbs":
        return _plan_vbs(p, args, win())
    if fid == "reg":
        return _plan_reg(p, win())
    if fid == "url":
        return _plan_url(p, which)
    if fid == "inf":
        return _plan_inf(p, win())
    if fid == "cab":
        return _plan_cab(p, abspath, which, home)
    if fid == "msu":
        dest = _extract_dest(p, home)
        return _explain("This is a Windows Update package (.msu). It updates Windows itself, which Lindos is not, "
                        "so there is nothing to install. If you need a file inside it, extract it with:\n"
                        f"  cabextract -d '{dest}' '{abspath}'",
                        extract=["cabextract", "-d", str(dest), abspath])
    if fid == "iso":
        return _plan_iso(abspath, which)
    if fid == "clickonce":
        return _plan_clickonce(p, prefix, win)
    if fid in ("dos-exe", "dos-com"):
        return _plan_dos(p, det, args, which=which, run=run, home=home)
    if fid == "win16-exe":
        return _plan_win16(det, args, win(), which=which, run=run, wine_mode=wine_mode)
    if fid == "arm-exe":
        machine = str(det.details.get("machine_name") or "ARM")
        return _explain(f"\u201c{name}\u201d is built for Windows on ARM ({machine}) and cannot run on this "
                        "Intel/AMD PC. Get the x64 (or x86) version of the program from its website.",
                        machine=machine)
    if fid == "dll":
        return _explain(_dll_message(name, det))
    hint = str(det.details.get("hint") or "")
    if det.reason == "file not found":
        msg = f"\u201c{p}\u201d does not exist."
    else:
        msg = f"Lindos cannot open \u201c{name}\u201d ({det.reason})."
    code = EXIT_ERROR if det.details.get("io_error") else EXIT_UNSUPPORTED
    return _explain(msg + (" " + hint if hint else ""), exit_code=code)


def _dotnet_message(det: Detection) -> str:
    d = det.details
    if d.get("apphost"):
        fws = [str(f) for f in (d.get("dotnet_frameworks") or [])]
        if d.get("dotnet_self_contained"):
            return (".NET program that brings its own .NET runtime: it usually runs like any other Windows program.")
        want = ", ".join(fws) if fws else "the .NET Desktop Runtime"
        arch = "x64" if d.get("machine_name") == "x64" else "x86"
        return (f".NET program: it needs {want} for Windows ({arch}) inside its C:\\ drive. That runtime is free "
                "(https://dotnet.microsoft.com/download): install it into the same C:\\ drive with "
                "lindos-run --prefix <this program's drive> <runtime installer>.")
    family = _dotnet_family(str(d.get("clr_version") or ""))
    return (f"{family} program: Wine's built-in replacement (wine-mono) runs many of these. Some need Microsoft's "
            ".NET Framework, whose licence is tied to a licensed copy of Windows - Lindos does not install it.")


def _dll_message(name: str, det: Detection) -> str:
    d = det.details
    if d.get("efi"):
        return (f"\u201c{name}\u201d is a UEFI firmware program: it runs before any operating system starts, "
                "not inside Windows or Linux.")
    if d.get("driver"):
        return (f"\u201c{name}\u201d is a Windows driver. Windows drivers cannot be used on Linux. Lindos can look "
                "for a Linux driver for your hardware: run  lindos-drivers detect")
    if d.get("boot_application"):
        return f"\u201c{name}\u201d is part of Windows' own start-up, not a program you can open."
    if d.get("new_exe") == "NE":
        return (f"\u201c{name}\u201d is a 16-bit Windows library that programs use; it is not a program itself. "
                "Open the program's .exe instead.")
    return (f"\u201c{name}\u201d is a library (DLL) that Windows programs use; it is not a program you can open. "
            "Open the program's .exe instead.")


_MSI_PATH_PROP_RE = re.compile(r"^(TRANSFORMS|PATCH)=(.*)$", re.I)


def _msi_args(args: Sequence[str], msi: Path, prefix: Optional[Path]) -> List[str]:
    """Convert Unix paths in ``TRANSFORMS=`` / ``PATCH=`` properties to Windows paths."""
    out: List[str] = []
    for arg in args:
        m = _MSI_PATH_PROP_RE.match(arg)
        if not m:
            out.append(arg)
            continue
        items = [_msi_path_item(item, msi, prefix) for item in m.group(2).split(";")]
        out.append(f"{m.group(1).upper()}={';'.join(items)}")
    return out


def _msi_path_item(item: str, msi: Path, prefix: Optional[Path]) -> str:
    text = item.strip().strip('"')
    lead = ""
    if text[:1] in ("@", "|"):
        lead, text = text[0], text[1:]
    if not text or text.startswith(":") or is_windows_path(text) or text.startswith("\\\\"):
        return lead + text
    if text.startswith("/"):
        return lead + to_windows_path(Path(text), prefix)
    for base in (Path.cwd(), msi.parent):
        candidate = base / text
        if candidate.is_file():
            return lead + to_windows_path(candidate.absolute(), prefix)
    return lead + text


def _plan_msp(p: Path, args: List[str], prefix: Optional[Path], win: str) -> ActionPlan:
    tail = ["msiexec", "/p", win] + _msi_args(args, p, prefix)
    keys = {a.split("=", 1)[0].strip().upper() for a in args if "=" in a}
    if "REINSTALL" not in keys:
        tail.append("REINSTALL=ALL")
    if "REINSTALLMODE" not in keys:
        tail.append("REINSTALLMODE=omus")
    return ActionPlan(handler="msiexec-patch", wine_tail=tail, needs_prefix=True, force_runner="wine",
                      confirm=f"Apply the patch \u201c{p.name}\u201d? It changes a program that is already installed.",
                      message=("A patch updates a program that is already installed, so it must go into that "
                               "program's own C:\\ drive (choose it, or use --prefix NAME)."),
                      details={"needs_product_prefix": True})


def _plan_mst(p: Path) -> ActionPlan:
    installers: List[str] = []
    try:
        with os.scandir(p.parent) as it:
            for i, entry in enumerate(it):
                if i > 5000:
                    break
                if entry.name.lower().endswith(".msi") and entry.is_file():
                    installers.append(entry.name)
    except OSError:
        pass
    if len(installers) == 1:
        cmd = f"lindos-run '{p.parent / installers[0]}' TRANSFORMS='{p}'"
    else:
        cmd = f"lindos-run <installer>.msi TRANSFORMS='{p}'"
    return _explain(f"\u201c{p.name}\u201d is a transform: it customises a Windows Installer package and is not "
                    f"opened on its own. Run it together with its installer:\n  {cmd}",
                    command=cmd, installers=installers)


_PKG_FILENAME_RE = re.compile(r"^(?P<name>[A-Za-z0-9.\-]+)_(?P<version>\d+(?:\.\d+){0,3})_(?P<arch>[A-Za-z0-9]+)_")


def _plan_encrypted(p: Path) -> ActionPlan:
    name = version = ""
    try:
        from . import msix as _msix
        info = _msix.inspect(p)
        name, version = str(getattr(info, "name", "") or ""), str(getattr(info, "version", "") or "")
    except Exception:  # noqa: BLE001 - W-B's module missing or the header unreadable
        pass
    if not name:
        m = _PKG_FILENAME_RE.match(p.name)
        if m:
            name, version = m.group("name"), m.group("version")
    what = f"{name} {version}".strip() or p.name
    return _explain(f"\u201c{what}\u201d is an encrypted Microsoft Store package. Its contents are locked to the "
                    "Store's licensing and cannot be opened on Lindos. Get the developer's regular download, a "
                    "Linux alternative, or install it from the Store on Windows.", name=name, version=version)


def _plan_appinstaller(p: Path) -> ActionPlan:
    info: Dict[str, str] = {}
    try:
        from . import msix as _msix
    except Exception as exc:  # noqa: BLE001 - W-B's module missing: lindos-run parses the file later
        log.debug("msix module unavailable: %s", exc)
        _msix = None  # type: ignore[assignment]
    if _msix is not None:
        try:
            info = {str(k): str(v) for k, v in dict(_msix.parse_appinstaller(p)).items()}
        except Exception as exc:  # noqa: BLE001 - malformed .appinstaller
            return _explain(f"\u201c{p.name}\u201d is not a valid App Installer file ({exc}).",
                            exit_code=EXIT_ERROR)
    if info.get("name") or info.get("host"):
        who = info.get("publisher") or "an unknown publisher"
        confirm = (f"\u201c{info.get('name') or p.name}\u201d from {who} would be downloaded from "
                   f"{info.get('host') or 'the internet'}. Download and install it?")
    else:
        confirm = f"\u201c{p.name}\u201d points to an app on the internet. Download and install it?"
    return ActionPlan(handler="appinstaller", needs_prefix=True, confirm=confirm,
                      message="Only HTTPS downloads are used, and the downloaded package must match this file.",
                      details={"appinstaller": info})


PWSH_INSTALL_HINT = ("PowerShell 7 is not installed. Install it from Microsoft's repository:\n"
                     "  wget https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb\n"
                     "  sudo dpkg -i packages-microsoft-prod.deb && sudo apt update && sudo apt install powershell")


def _plan_ps1(p: Path, abspath: str, args: List[str], which: Which) -> ActionPlan:
    pwsh = which("pwsh") or which("pwsh-lts") or which("pwsh-preview")
    if not pwsh:
        return ActionPlan(handler="pwsh", message=PWSH_INSTALL_HINT, exit_code=EXIT_ERROR)
    return ActionPlan(handler="pwsh", host_argv=[pwsh, "-NoProfile", "-File", abspath] + args,
                      confirm=(f"Run the PowerShell script \u201c{p.name}\u201d? Scripts can change your files and "
                               "settings - only run scripts you trust."),
                      message=("PowerShell 7 on Linux lacks many Windows-only commands (services, registry, "
                               "Get-CimInstance ...), so scripts written for Windows often stop with errors."))


def _plan_vbs(p: Path, args: List[str], win: str) -> ActionPlan:
    suffix = p.suffix.lower()
    note = ""
    if suffix == ".vbe":
        note = "Encoded VBScript (.vbe) runs only if Wine's script engine can decode it."
    elif suffix == ".wsf":
        note = "Windows Script Files (.wsf) are only partly supported by Wine; some will not start."
    return ActionPlan(handler="wscript", wine_tail=["wscript", win] + args, needs_prefix=True,
                      force_runner="wine", message=note or "Runs with Wine's Windows Script Host.")


def _plan_reg(p: Path, win: str) -> ActionPlan:
    try:
        preview = reg_preview(p)
    except OSError as exc:
        return _explain(f"\u201c{p.name}\u201d could not be read ({exc.strerror or exc}).", exit_code=EXIT_ERROR)
    if not preview["valid"]:
        return _explain(f"\u201c{p.name}\u201d is not a valid registry file: it must start with "
                        "\u201cWindows Registry Editor Version 5.00\u201d or \u201cREGEDIT4\u201d.",
                        exit_code=EXIT_ERROR, reg=preview)
    return ActionPlan(handler="regedit", wine_tail=["regedit", "/S", win], needs_prefix=True, force_runner="wine",
                      confirm=reg_confirm_text(p.name, preview), details={"reg": preview})


def _plan_url(p: Path, which: Which) -> ActionPlan:
    url, allowed, reason = parse_url_shortcut(p)
    if not allowed:
        return _explain(f"\u201c{p.name}\u201d was not opened. {reason}", url=url, reason=reason)
    opener = which("xdg-open")
    if not opener:
        return ActionPlan(handler="open-url", message="xdg-open is missing (sudo apt install xdg-utils).",
                          exit_code=EXIT_ERROR, details={"url": url})
    return ActionPlan(handler="open-url", host_argv=[opener, url], details={"url": url, "reason": reason})


def _plan_inf(p: Path, win: str) -> ActionPlan:
    info = inf_details(p)
    kind = info["kind"]
    if kind == "driver":
        why = "; ".join(str(r) for r in info["reasons"][:3])  # type: ignore[index]
        return _explain(f"\u201c{p.name}\u201d installs a Windows hardware driver ({why}). Windows drivers do not "
                        "work on Linux. Lindos can look for a Linux driver for your hardware: run  lindos-drivers detect",
                        inf=info, suggest=["lindos-drivers", "detect"])
    if kind != "software":
        autorun = info.get("autorun") or {}
        if isinstance(autorun, dict) and (autorun.get("open") or autorun.get("shellexecute")):
            target = autorun.get("open") or autorun.get("shellexecute")
            return _explain(f"\u201c{p.name}\u201d is a CD/DVD AutoPlay file. It starts \u201c{target}\u201d - "
                            "open that program instead.", inf=info)
        return _explain(f"\u201c{p.name}\u201d has no [DefaultInstall] section, so there is nothing that can be "
                        "installed from it automatically.", inf=info)
    cmdline = "DefaultInstall 132 " + win
    if len(cmdline) >= _WIN_MAX_PATH:
        return _explain(f"The folder path of \u201c{p.name}\u201d is too long for Windows' installer "
                        "(260 characters). Move the folder somewhere with a shorter path and try again.",
                        exit_code=EXIT_ERROR, inf=info)
    try:
        tokens = rundll32_path_tokens(win)
    except ValueError:
        return _explain(f"The path of \u201c{p.name}\u201d contains quotes, tabs or double spaces, which Windows' "
                        "INF installer cannot handle. Rename the folder or file and try again.",
                        exit_code=EXIT_ERROR, inf=info)
    return ActionPlan(handler="inf-install",
                      wine_tail=["rundll32", "setupapi.dll,InstallHinfSection", "DefaultInstall", "132"] + tokens,
                      needs_prefix=True, force_runner="wine",
                      confirm=f"Install \u201c{p.name}\u201d into a C:\\ drive? It can copy files and change settings.",
                      details={"inf": info})


def _extract_dest(p: Path, home: Optional[Path]) -> Path:
    parent = p.parent if p.parent != Path("") else Path(".")
    try:
        writable = os.access(parent, os.W_OK)
    except OSError:
        writable = False
    if not writable:
        base_home = home or user_home()
        downloads = base_home / "Downloads"
        parent = downloads if downloads.is_dir() else base_home
    base = parent.absolute() / (p.stem or "extracted")
    candidate = base
    n = 2
    while candidate.exists() and n < 1000:
        candidate = base.with_name(f"{base.name} ({n})")
        n += 1
    return candidate


def _plan_cab(p: Path, abspath: str, which: Which, home: Optional[Path]) -> ActionPlan:
    dest = _extract_dest(p, home)
    opener = which("xdg-open") or "xdg-open"
    details: Dict[str, object] = {"dest": str(dest), "open": [opener, str(dest)]}
    cabextract = which("cabextract")
    if cabextract:
        argv = [cabextract, "-d", str(dest), abspath]
    else:
        seven = which("7z") or which("7zz")
        if not seven:
            return ActionPlan(handler="extract", message="cabextract is not installed (sudo apt install cabextract).",
                              exit_code=EXIT_ERROR, details=details)
        argv = [seven, "x", "-o" + str(dest), abspath]
    return ActionPlan(handler="extract", host_argv=argv, message=f"Extracting into \u201c{dest}\u201d.",
                      details=details)


def _plan_iso(abspath: str, which: Which) -> ActionPlan:
    udisksctl = which("udisksctl")
    if not udisksctl:
        return ActionPlan(handler="mount", message="udisks2 is not installed (sudo apt install udisks2).",
                          exit_code=EXIT_ERROR)
    return ActionPlan(handler="mount", host_argv=[udisksctl, "loop-setup", "-r", "-f", abspath],
                      message=("Opens the disk image like a DVD drive (read-only). If it contains a setup program, "
                               "Lindos asks before running it."),
                      details={"image": abspath})


def _appref_url(p: Path) -> str:
    try:
        data, _trunc = _read_file(p, _SMALL_TEXT_LIMIT)
    except OSError:
        return ""
    text, _enc = _decode_text(data)
    first = text.strip().split("\n", 1)[0].strip()
    return first.split("#", 1)[0].strip()


def _plan_clickonce(p: Path, prefix: Optional[Path], win: Callable[[], str]) -> ActionPlan:
    if prefix is None or not prefix_has_dotnet_framework(prefix):
        return _explain(f"\u201c{p.name}\u201d is a ClickOnce app. It needs Microsoft .NET Framework 4.x inside the "
                        "C:\\ drive it runs in. That framework's licence is tied to a licensed copy of Windows, so "
                        "Lindos does not install it for you. If you own a Windows licence you may install it yourself "
                        "into a C:\\ drive (for example with winetricks dotnet48), then open this file with "
                        "lindos-run --prefix <that drive>.", prefix=str(prefix) if prefix else None)
    if p.suffix.lower() == ".appref-ms":
        target = _appref_url(p)
        scheme = target.split(":", 1)[0].lower() if ":" in target else ""
        if scheme not in ("http", "https"):
            return _explain(f"\u201c{p.name}\u201d does not contain a web address Lindos can use.",
                            exit_code=EXIT_ERROR)
    else:
        target = win()
    try:
        tokens = rundll32_path_tokens(target)
    except ValueError:
        return _explain(f"The path of \u201c{p.name}\u201d contains quotes, tabs or double spaces. Rename it and try "
                        "again.", exit_code=EXIT_ERROR)
    return ActionPlan(handler="clickonce", wine_tail=["rundll32", "dfshim.dll,ShOpenVerbApplication"] + tokens,
                      needs_prefix=True, force_runner="wine", message=_BY_ID["clickonce"].note,
                      details={"target": target})


def _plan_dos(p: Path, det: Detection, args: List[str], *, which: Which, run: Callable[..., object],
              home: Optional[Path]) -> ActionPlan:
    target = p
    prog_args = list(args)
    pif = det.details.get("pif") if isinstance(det.details.get("pif"), dict) else None
    if pif:
        program = str(pif.get("program") or "")
        base = program.replace("\\", "/").rsplit("/", 1)[-1]
        found = _ci_sibling(p.parent, base) if base else None
        if found is None:
            return _explain(f"\u201c{p.name}\u201d holds settings for the DOS program \u201c{program}\u201d. That "
                            "program was not found next to it - open the program itself instead.",
                            exit_code=EXIT_ERROR, pif=pif)
        target = found
        prog_args = str(pif.get("params") or "").split() + prog_args
    try:
        argv = _dos.dosbox_argv(target, prog_args, which=which, run=run, home=home)  # type: ignore[arg-type]
    except FileNotFoundError:
        return ActionPlan(handler="dos", exit_code=EXIT_ERROR,
                          message=("DOS programs run in DOSBox-X, which is not installed. Install it with:\n  "
                                   + _dos.DOSBOX_INSTALL_HINT))
    note = str(det.details.get("partial_note") or "")
    return ActionPlan(handler="dos", host_argv=argv, message=note, details={"program": str(target)})


def _plan_win16(det: Detection, args: List[str], win: str, *, which: Which, run: Callable[..., object],
                wine_mode: Optional[str]) -> ActionPlan:
    mode = wine_mode or _dos.wine_wow64_mode(which=which, run=run)  # type: ignore[arg-type]
    tail = [win] + args
    if mode == "new-wow64-no16bit":
        return _explain("This is a 16-bit Windows 3.x program. Your Wine runs in the new \u201cWoW64\u201d mode but is "
                        "older than Wine 10.16, which cannot run 16-bit programs. Install a newer Wine (WineHQ 11 or "
                        "later) and try again.", wine_mode=mode)
    if mode == "new-wow64-16bit":
        return ActionPlan(handler="win16", wine_tail=tail, needs_prefix=True, force_runner="wine",
                          message="16-bit Windows program: runs through Wine (your Wine supports 16-bit programs).",
                          details={"wine_mode": mode})
    message = ("16-bit Windows program: runs through Wine in a separate 32-bit C:\\ drive called \u201cwin16\u201d.")
    if mode != "old-wow64":
        message += (" Lindos could not tell how your Wine handles 16-bit programs; if it does not start, install "
                    "32-bit Wine support (sudo dpkg --add-architecture i386 && sudo apt update && "
                    "sudo apt install wine32:i386).")
    return ActionPlan(handler="win16", wine_tail=tail, needs_prefix=True, force_runner="wine", arch="win32",
                      prefix_hint="win16", message=message, details={"wine_mode": mode})


# ---------------------------------------------------------------------------
# CLI (debugging aid; `lindos-compat formats` is the user-facing command)
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m lindos_compat.formats",
                                     description="Show how Lindos classifies Windows files (no file is run).")
    parser.add_argument("files", nargs="*", help="files to classify")
    parser.add_argument("--table", action="store_true", help="print the format table")
    parser.add_argument("--plan", action="store_true", help="also show the action plan for each file")
    parser.add_argument("--json", action="store_true", help="JSON output")
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if ns.table or not ns.files:
        table = formats_table()
        if ns.json:
            print(json.dumps(table, indent=2))
        else:
            for row in table:
                print(f"{row['id']:15} {row['status']:12} {' '.join(row['suffixes']):40} {row['note']}")
        return EXIT_OK
    results: List[Dict[str, object]] = []
    rc = EXIT_OK
    for f in ns.files:
        det = detect(Path(f))
        row: Dict[str, object] = {"file": f, **det.as_dict()}
        if ns.plan:
            row["plan"] = plan_action(Path(f), det, wine_mode="unknown").as_dict()
        if det.format.handler == "explain":
            rc = EXIT_UNSUPPORTED
        results.append(row)
    if ns.json:
        print(json.dumps(results if len(results) != 1 else results[0], indent=2, default=str))
    else:
        for row in results:
            print(f"{row['file']}: {row['label']} [{row['id']}, {row['status']}] - {row['reason']}")
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
