"""MSIX / APPX packages, bundles, upload files and ``.appinstaller`` files (SPEC-WINDOWS §28.4).

What this module does
---------------------
``classify``            sniff a file's *content* (never its extension): package, bundle, upload,
                        encrypted (Store DRM), msixvc, appinstaller or unknown.  Never raises.
``inspect``             read the manifest(s) and report identity, apps and an honest status:
                        packaged desktop (Win32) apps usually work under Wine; true UWP/WinUI apps
                        cannot (Wine has no UWP app model); Store-encrypted packages are refused.
``install``             zip-slip-safe extraction of a desktop package into its own C:\\ drive
                        (``drive_c/Program Files/WindowsApps/<PackageFullName>``), ``VFS`` folders
                        materialised at their Windows locations, ``Registry.dat`` converted with the
                        optional python3-hivex, ``PsfLauncher`` resolved to the real program.
``parse_appinstaller``  read an ``.appinstaller`` pointer file.  Nothing is ever downloaded here.
``publisher_id``        the 13-character PublisherId of a Publisher string.

Wine cannot deploy MSIX packages (its PackageManager is a stub), so Lindos does what a person
would do by hand -- extract the package and run its desktop program -- and says plainly when that
cannot work.  Signatures are *reported, not verified*; nothing is ever decrypted; Store licences
are never touched.  XML is parsed by namespace URI + local name, BOM-tolerant and child-order
independent, and any DOCTYPE/ENTITY declaration is refused (no entity expansion, no external
entities).  ZIP entry names are percent-decoded *before* they are validated, then split on both
``/`` and ``\\``.

Stdlib only.  ``hivex`` (Debian: python3-hivex) is imported lazily and is optional.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import get_logger, user_home

__all__ = [
    "MsixError",
    "KINDS",
    "APP_CLASSES",
    "EXIT_UNSUPPORTED",
    "UNSIGNED_MARKER",
    "STORE_SIGNER_OID",
    "MsixApp",
    "MsixInfo",
    "MsixInstall",
    "classify",
    "inspect",
    "install",
    "parse_appinstaller",
    "publisher_id",
    # helpers for lindos-run (additive to the SPEC API)
    "appinstaller_refusal",
    "check_appinstaller_target",
    "trust_label",
    "can_try_anyway",
    "prefix_slug",
    "aumid",
    "package_root_windows",
    "expand_macros",
    "split_command_line",
    "launch_info",
    "describe",
    "main",
]

log = get_logger("lindos-compat.msix")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
KINDS = ("package", "bundle", "upload", "encrypted", "msixvc", "appinstaller", "unknown")
APP_CLASSES = ("win32", "uwp", "unknown", "needs-host")

#: lindos-run exit code for "unsupported, explained" (SPEC-WINDOWS §28.3).
EXIT_UNSUPPORTED = 3

#: Last RDN of the Publisher of a deliberately unsigned package (Microsoft Learn).
UNSIGNED_MARKER = "OID.2.25.311729368913984317654407730594956997722=1"
#: Certificate extension OID of Microsoft Store signing certificates.
STORE_SIGNER_OID = "1.3.6.1.4.1.311.76.3.1"
_STORE_OID_DER = bytes.fromhex("060a2b0601040182374c0301")  # DER OBJECT IDENTIFIER of the OID above

#: Encrypted (Store DRM) package magics -> what they contain.
ENCRYPTED_MAGICS: Dict[bytes, str] = {b"EXPH": "package", b"EXSH": "package", b"EXBH": "bundle"}

PACKAGE_MANIFEST = "AppxManifest.xml"
BUNDLE_MANIFEST = "AppxMetadata/AppxBundleManifest.xml"
_SIGNATURE = "AppxSignature.p7x"
_CODE_INTEGRITY = "appxmetadata/codeintegrity.cat"
PACKAGE_SUFFIXES = (".msix", ".appx")
BUNDLE_SUFFIXES = (".msixbundle", ".appxbundle")

# Package manifest namespaces
NS_FOUNDATION = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
NS_WIN8 = "http://schemas.microsoft.com/appx/2010/manifest"
NS_WIN81 = "http://schemas.microsoft.com/appx/2013/manifest"
NS_UAP = "http://schemas.microsoft.com/appx/manifest/uap/windows10"
NS_UAP10 = NS_UAP + "/10"
NS_UAP11 = NS_UAP + "/11"
NS_RESCAP = NS_FOUNDATION + "/restrictedcapabilities"
NS_DESKTOP4 = "http://schemas.microsoft.com/appx/manifest/desktop/windows10/4"
NS_IOT2 = "http://schemas.microsoft.com/appx/manifest/iot/windows10/2"
NS_PREVIEWSEC = "http://schemas.microsoft.com/appx/manifest/preview/windows10/security"
NS_PREVIEWSEC2 = NS_PREVIEWSEC + "/2"
_WIN8_NAMESPACES = (NS_WIN8, NS_WIN81)
_PACKAGE_NAMESPACES = (NS_FOUNDATION, NS_WIN8, NS_WIN81)
_MS_APPX_NS_PREFIX = "http://schemas.microsoft.com/appx/"

# Bundle manifest namespaces (root may be any of these; <b5:Package> lives in 2019)
BUNDLE_NAMESPACES = tuple(f"http://schemas.microsoft.com/appx/{year}/bundle" for year in (2013, 2016, 2017, 2018))
NS_BUNDLE_2019 = "http://schemas.microsoft.com/appx/2019/bundle"
_BUNDLE_PACKAGE_NAMESPACES = BUNDLE_NAMESPACES + (NS_BUNDLE_2019,)

APPINSTALLER_NAMESPACES = (
    "http://schemas.microsoft.com/appx/appinstaller/2017",
    "http://schemas.microsoft.com/appx/appinstaller/2017/2",
    "http://schemas.microsoft.com/appx/appinstaller/2018",
    "http://schemas.microsoft.com/appx/appinstaller/2021",
)

KNOWN_ARCHES = ("x86", "x64", "arm", "arm64", "x86a64", "neutral")
_ARCH_RANK = {"x64": 3, "neutral": 2, "x86": 1}      # what runs on an x86_64 PC, best first
_ARM_ARCHES = ("arm", "arm64", "x86a64")               # x86a64 = x86 package for ARM64 Windows
_DESKTOP_FAMILIES = ("windows.desktop", "windows.universal")

# Limits (all generous for real packages, small enough to stop hostile input)
_SNIFF_BYTES = 4096
_MAX_XML_BYTES = 8 << 20
_MAX_APPINSTALLER_BYTES = 1 << 20
_MAX_P7X_BYTES = 2 << 20
_MAX_JSON_BYTES = 1 << 20
_MAX_LOGO_BYTES = 16 << 20
_MAX_ENTRIES = 500_000
_MAX_APPS = 100
_MAX_UPLOAD_CANDIDATES = 16
_MAX_PATH_CHARS = 260
_MAX_SEGMENT_CHARS = 255
_RATIO_MIN_BYTES = 1 << 20          # the ratio cap only applies to entries bigger than this
_ENCRYPTED_SCAN_BYTES = 64 << 10
_FREE_SPACE_MARGIN = 64 << 20
_CHUNK = 1 << 20
_REG_MAX_NODES = 200_000
_REG_MAX_DEPTH = 256

#: Prefix directory names that are shared between apps (mirror of ``prefix.SHARED_SLUG``).
_SHARED_PREFIX_NAMES = ("default",)

_NAME_RE = re.compile(r"^[A-Za-z0-9.\-]{3,50}$")
_RESID_RE = re.compile(r"^[A-Za-z0-9.\-~]{0,30}$")
_ARCH_RE = re.compile(r"^[A-Za-z0-9]{1,7}$")
_VERSION_RE = re.compile(r"^(\d{1,5})\.(\d{1,5})\.(\d{1,5})\.(\d{1,5})$")
_DEVICE_RE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_RESERVED_CHARS = frozenset('"*<>?|:')
_GUID_PUBLISHER_RE = re.compile(
    r"^\s*CN\s*=\s*[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\s*$")
_UNSIGNED_RE = re.compile(
    r"(?:^|,)\s*OID\.2\.25\.311729368913984317654407730594956997722\s*=\s*1\s*$", re.IGNORECASE)
_CN_RE = re.compile(r'(?:^|,)\s*CN\s*=\s*("(?:[^"\\]|\\.)*"|[^,]*)', re.IGNORECASE)
_FULL_NAME_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9.\-]{3,50})_(?P<version>\d{1,5}(?:\.\d{1,5}){3})_(?P<arch>[A-Za-z0-9]{1,7})_"
    r"(?P<rid>[A-Za-z0-9.\-~]{0,30})_(?P<pid>[0-9a-hjkmnp-tv-z]{13})$",
    re.IGNORECASE,
)
_PSF_RE = re.compile(r"^psflauncher(32|64)?\.exe$", re.IGNORECASE)
_APPINSTALLER_ROOT_RE = re.compile(
    r"^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*<(?:[A-Za-z_][\w.\-]*:)?AppInstaller[\s/>]", re.DOTALL)

# Footprint files (never extracted as payload; AppxManifest.xml is kept)
_FOOTPRINT_FILES = frozenset({"appxblockmap.xml", "appxsignature.p7x", "[content_types].xml", "appxstreammap.xml"})
_FOOTPRINT_DIRS = frozenset({"appxmetadata", "microsoft.system.package.metadata"})

# VFS token -> folder below drive_c (Microsoft "behind the scenes" table), 64-bit C:\ drive
_VFS_WIN64: Dict[str, Optional[Tuple[str, ...]]] = {
    "programfilesx64": ("Program Files",),
    "programfilesx86": ("Program Files (x86)",),
    "programfilescommonx64": ("Program Files", "Common Files"),
    "programfilescommonx86": ("Program Files (x86)", "Common Files"),
    "systemx64": ("windows", "system32"),
    "systemx86": ("windows", "syswow64"),
    "windows": ("windows",),
    "common appdata": ("ProgramData",),
    "appvsystem32catroot": ("windows", "system32", "catroot"),
    "appvsystem32catroot2": ("windows", "system32", "catroot2"),
    "appvsystem32driversetc": ("windows", "system32", "drivers", "etc"),
    "appvsystem32driverstore": ("windows", "system32", "DriverStore"),
    "appvsystem32logfiles": ("windows", "system32", "LogFiles"),
    "appvsystem32spool": ("windows", "system32", "spool"),
}
# ... and in a 32-bit C:\ drive (WINEARCH=win32): 64-bit folders do not exist there
_VFS_WIN32: Dict[str, Optional[Tuple[str, ...]]] = dict(
    _VFS_WIN64,
    programfilesx64=None,
    programfilescommonx64=None,
    systemx64=None,
    programfilesx86=("Program Files",),
    programfilescommonx86=("Program Files", "Common Files"),
    systemx86=("windows", "system32"),
)

# App-V style tokens seen in Registry.dat string values (undocumented; best effort)
_REG_TOKENS = {
    "[{appvpackageroot}]": "",  # replaced by the package root
    "[{programfilesx64}]": r"C:\Program Files",
    "[{programfilesx86}]": r"C:\Program Files (x86)",
    "[{programfilescommonx64}]": r"C:\Program Files\Common Files",
    "[{programfilescommonx86}]": r"C:\Program Files (x86)\Common Files",
    "[{windows}]": r"C:\windows",
    "[{system}]": r"C:\windows\system32",
    "[{systemx64}]": r"C:\windows\system32",
    "[{systemx86}]": r"C:\windows\syswow64",
    "[{common appdata}]": r"C:\ProgramData",
}
_REG_TOKEN_RE = re.compile(r"\[\{[^\]\}]{1,64}\}\]")
_HIVE_FILES = {"registry.dat": "machine", "user.dat": "user", "userclass.dat": "userclasses",
               "userclasses.dat": "userclasses"}

# $(...) macros of uap11:Parameters / CurrentDirectoryPath
_MACRO_RE = re.compile(r"\$\$|\$\(([^()]{1,128})\)")
_PACKAGE_PATH_MACROS = frozenset({
    "package.effectivepath", "package.installedpath", "package.mutablepath", "package.currentdirectorypath",
    "package.effectiveexternalpath", "package.machineexternalpath", "package.userexternalpath",
})
_ENV_DEFAULTS = {  # Windows defaults that do not depend on the user name
    "systemroot": r"C:\windows", "windir": r"C:\windows", "systemdrive": "C:",
    "programfiles": r"C:\Program Files", "programfiles(x86)": r"C:\Program Files (x86)",
    "programw6432": r"C:\Program Files", "commonprogramfiles": r"C:\Program Files\Common Files",
    "commonprogramfiles(x86)": r"C:\Program Files (x86)\Common Files", "programdata": r"C:\ProgramData",
    "allusersprofile": r"C:\ProgramData", "public": r"C:\users\Public",
}

# Plain-language status texts (SPEC-WINDOWS §27.2: never lie about status)
_REASON_WIN32 = "Packaged desktop app — usually works."
_REASON_UWP = ("UWP/WinUI app — Wine has no UWP app model; try the web version, a Linux alternative, "
               "or `lindos-vm`.")
_REASON_NEEDS_HOST = ("This app has no program of its own: it runs inside another app's host runtime, which "
                      "only Windows provides. Try the web version, a Linux alternative, or `lindos-vm`.")
_REASON_UNKNOWN_APP = ("This package uses an app type Lindos can't identify safely, so it won't be started. "
                       "Try the developer's regular download, a Linux alternative, or `lindos-vm`.")
_REASON_FRAMEWORK = ("This is a shared runtime package (framework) — a component, not an app. Apps that need "
                     "it name it as a dependency.")
_REASON_RESOURCE = ("This is a resource package (languages or pictures for another app) — a component, "
                    "not an app.")
_REASON_NO_APPS = "This package contains no app to start — a component, not an app."
_REASON_ARM = ("This app is only built for ARM processors ({archs}); it cannot run on this PC, and the "
               "Windows VM can't run it either. Ask the developer for the x64 (Intel/AMD) version.")
_REASON_ENCRYPTED = ("Encrypted Microsoft Store package: its contents are locked to the Store's licensing and "
                     "can't be opened on Lindos. Install it from the Store inside the Windows VM "
                     "(`lindos-vm`), or get the developer's regular download.")
_REASON_MSIXVC = ("Xbox / PC Game Pass game package (.msixvc): the game files are encrypted and licence-checked "
                  "by the Xbox app, so it can't be installed on Lindos. Xbox Cloud Gaming or Windows are the "
                  "ways to play it.")


class MsixError(Exception):
    """A package cannot be read or installed.  The message is plain language, safe to show the user."""


# --------------------------------------------------------------------------- #
# public data classes (SPEC-WINDOWS §28.4)
# --------------------------------------------------------------------------- #
@dataclass
class MsixApp:
    id: str
    display_name: str
    executable: str
    entry_point: str
    app_class: str               # one of APP_CLASSES
    parameters: str              # uap10/uap11:Parameters (macros expanded at launch)
    working_dir: str             # uap11:CurrentDirectoryPath or ""
    logo: str                    # resolved package-relative PNG ("" if none)
    list_entry: bool             # AppListEntry != "none"
    console: bool                # Subsystem="console"


@dataclass
class MsixInfo:
    path: str
    kind: str
    name: str
    publisher: str
    publisher_display: str
    version: str
    arch: str
    resource_id: str
    display_name: str
    publisher_id: str            # 13-char Crockford base32 (SHA-256 of UTF-16LE Publisher, first 8 bytes)
    package_full_name: str       # <Name>_<Version>_<Arch>_<ResourceId>_<PublisherId>
    package_family_name: str     # <Name>_<PublisherId>
    apps: List[MsixApp]
    dependencies: List[Dict[str, str]]   # PackageDependency {"name", "publisher", "min_version"}
    framework: bool
    resource_package: bool
    signed: bool
    unsigned_marker: bool
    store_signals: List[str]
    status: str
    reason: str
    selected_package: Optional[str]      # bundles/uploads: inner FileName chosen
    warnings: List[str]
    #: bundles: the bundle's own Identity Version (``version`` is the chosen app package's); additive field
    bundle_version: str = ""

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class MsixInstall:
    info: MsixInfo
    install_dir: Path            # <prefix>/drive_c/Program Files/WindowsApps/<package_full_name>
    apps: List[Tuple[MsixApp, Path, Optional[Path]]]   # (app, exe path, logo png)
    notes: List[str]
    #: .reg files (UTF-16LE) converted from Registry.dat/User.dat/UserClasses.dat; the caller imports
    #: them with ``wine regedit /S`` into the *same* C:\ drive.  Empty when there was nothing to import.
    reg_files: List[Path] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "info": self.info.as_dict(),
            "install_dir": str(self.install_dir),
            "apps": [{"app": asdict(app), "exe": str(exe), "logo": str(logo) if logo else None}
                     for app, exe, logo in self.apps],
            "notes": list(self.notes),
            "reg_files": [str(p) for p in self.reg_files],
        }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
_BASE32 = "0123456789abcdefghjkmnpqrstvwxyz"   # Crockford-style alphabet used by Windows


def publisher_id(publisher: str) -> str:
    """The 13-character PublisherId: first 8 bytes of SHA-256(UTF-16LE Publisher), base32 (MSIX SDK)."""
    digest = hashlib.sha256(str(publisher).encode("utf-16-le")).digest()[:8]
    value = int.from_bytes(digest, "big") << 1      # 64 bits + 1 padding bit = 13 x 5 bits
    return "".join(_BASE32[(value >> (5 * (12 - i))) & 31] for i in range(13))


def _human(n: int) -> str:
    size = float(max(0, n))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"  # pragma: no cover


def _version_tuple(version: str, what: str = "version") -> Tuple[int, int, int, int]:
    m = _VERSION_RE.match((version or "").strip())
    if not m:
        raise MsixError(f"The {what} {version!r} is not a valid Windows package version (a.b.c.d).")
    parts = tuple(int(x) for x in m.groups())
    if any(p > 65535 for p in parts):
        raise MsixError(f"The {what} {version!r} is out of range.")
    return parts  # type: ignore[return-value]


def _is_ms_resource(text: str) -> bool:
    return (text or "").strip().casefold().startswith("ms-resource:")


def _cn(publisher: str) -> str:
    m = _CN_RE.search(publisher or "")
    if not m:
        return ""
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def _humanize(name: str) -> str:
    """'Contoso.PhotoEditor' -> 'Photo Editor' (only a fallback when no display name exists)."""
    parts = [p for p in (name or "").split(".") if p]
    if len(parts) > 1:
        parts = parts[1:]
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", " ".join(parts))
    return re.sub(r"[\s_\-]+", " ", text).strip()


def _exe_stem(executable: str) -> str:
    base = re.split(r"[\\/]", executable or "")[-1]
    return base[:-4] if base.casefold().endswith(".exe") else base


def _check_identity(name: str, publisher: str, version: str, arch: str, resource_id: str, what: str) -> None:
    """Validate attacker-controlled identity fields: they end up in folder names."""
    if not _NAME_RE.match(name or "") or name.endswith(".") or _DEVICE_RE.match(name):
        raise MsixError(f"The {what} has an invalid name ({name!r}); it is damaged or not a real Windows package.")
    if not publisher or len(publisher) > 8192 or any(ord(c) < 0x20 for c in publisher):
        raise MsixError(f"The {what} has an invalid publisher; it is damaged or not a real Windows package.")
    _version_tuple(version, f"{what} version")
    if not _ARCH_RE.match(arch or ""):
        raise MsixError(f"The {what} has an invalid processor architecture ({arch!r}).")
    if not _RESID_RE.match(resource_id or ""):
        raise MsixError(f"The {what} has an invalid resource id ({resource_id!r}).")


def _full_names(name: str, version: str, arch: str, rid: str, pid: str) -> Tuple[str, str]:
    return f"{name}_{version}_{arch}_{rid}_{pid}", f"{name}_{pid}"


def _cache_dir() -> Path:
    """Scratch space for inspect(): ~/.cache/lindos/msix (LINDOS_HOME wins over XDG_CACHE_HOME)."""
    if os.environ.get("LINDOS_HOME"):
        base = user_home() / ".cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (user_home() / ".cache"))
    return base / "lindos" / "msix"


def _free_bytes(path: Path) -> Optional[int]:
    """Free space on the file system holding ``path`` (None when unknown).  Tests monkeypatch this."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def _check_free(path: Path, need: int) -> None:
    free = _free_bytes(path)
    if free is not None and free < need + _FREE_SPACE_MARGIN:
        raise MsixError(f"Not enough free disk space: the app needs about {_human(need)} but only "
                        f"{_human(free)} is free on that drive.")


# --------------------------------------------------------------------------- #
# XML (namespace-aware, BOM-tolerant, DOCTYPE refused)
# --------------------------------------------------------------------------- #
def _decode_xml_bytes(data: bytes, what: str, *, strict: bool = True) -> str:
    errors = "strict" if strict else "replace"
    if data.startswith(codecs.BOM_UTF8):
        raw, enc = data[3:], "utf-8"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raw, enc = data, "utf-16"
    elif data.startswith(b"<\x00"):
        raw, enc = data, "utf-16-le"
    elif data.startswith(b"\x00<"):
        raw, enc = data, "utf-16-be"
    else:
        raw, enc = data, "utf-8"
    try:
        return raw.decode(enc, errors).lstrip("\ufeff")
    except UnicodeDecodeError:
        raise MsixError(f"{what} is not valid UTF-8/UTF-16 text; the file is damaged.") from None


def _parse_xml(data: bytes, what: str) -> ET.Element:
    if len(data) > _MAX_XML_BYTES:
        raise MsixError(f"{what} is unexpectedly large ({_human(len(data))}); refusing it.")
    text = _decode_xml_bytes(data, what)
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise MsixError(f"{what} contains a DOCTYPE/ENTITY declaration. Real Windows packages never do, so "
                        "Lindos refuses it for safety.")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise MsixError(f"{what} is not valid XML ({exc}); the file is damaged.") from None


def _split_tag(tag: Any) -> Tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _children(elem: Optional[ET.Element], local: str, namespaces: Optional[Sequence[str]] = None
              ) -> List[ET.Element]:
    if elem is None:
        return []
    out = []
    for child in list(elem):
        ns, name = _split_tag(child.tag)
        if name == local and (namespaces is None or ns in namespaces):
            out.append(child)
    return out


def _first(elem: Optional[ET.Element], local: str, namespaces: Optional[Sequence[str]] = None
           ) -> Optional[ET.Element]:
    kids = _children(elem, local, namespaces)
    return kids[0] if kids else None


def _text(elem: Optional[ET.Element]) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _attr(elem: Optional[ET.Element], local: str, *namespaces: str) -> str:
    """Attribute ``local`` in one of ``namespaces`` ("" = unqualified, the default)."""
    if elem is None:
        return ""
    for ns in namespaces or ("",):
        value = elem.get(f"{{{ns}}}{local}" if ns else local)
        if value is not None:
            return value.strip()
    return ""


def _attr_ms(elem: Optional[ET.Element], local: str, preferred: Sequence[str]) -> str:
    """Attribute from the preferred namespaces, else from any newer Microsoft appx namespace."""
    value = _attr(elem, local, *preferred)
    if value or elem is None:
        return value
    for key, val in elem.attrib.items():
        ns, name = _split_tag(key)
        if name == local and ns.startswith(_MS_APPX_NS_PREFIX):
            return val.strip()
    return ""


# --------------------------------------------------------------------------- #
# ZIP access
# --------------------------------------------------------------------------- #
class _SubFile(io.RawIOBase):
    """Read-only, bounds-checked window ``[start, start + size)`` of another seekable file."""

    def __init__(self, base: Any, start: int, size: int, name: str = "") -> None:
        super().__init__()
        self._base = base
        self._start = start
        self.size = size
        self._pos = 0
        self.name = name

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self.size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        return pos

    def readinto(self, buffer: Any) -> int:
        remaining = self.size - self._pos
        if remaining <= 0:
            return 0
        view = memoryview(buffer).cast("B")
        count = min(len(view), remaining)
        self._base.seek(self._start + self._pos)
        data = self._base.read(count)
        got = len(data)
        view[:got] = data
        self._pos += got
        return got


class _Zip:
    """A ZIP container plus the raw file object it lives in (for LFH reads and nested windows)."""

    def __init__(self, zf: zipfile.ZipFile, fobj: Any, size: int, label: str, path: Optional[Path] = None) -> None:
        self.zf = zf
        self.fobj = fobj
        self.size = size
        self.label = label
        self.path = path
        self._folded: Optional[Dict[str, Tuple[str, zipfile.ZipInfo]]] = None
        self._exact: Dict[str, Tuple[str, zipfile.ZipInfo]] = {}

    def _index(self) -> Dict[str, Tuple[str, zipfile.ZipInfo]]:
        if self._folded is None:
            folded: Dict[str, Tuple[str, zipfile.ZipInfo]] = {}
            exact: Dict[str, Tuple[str, zipfile.ZipInfo]] = {}
            for zi in self.zf.infolist():
                try:
                    decoded = _decode_name(zi.orig_filename)
                except MsixError:
                    continue
                if not decoded or decoded.endswith(("/", "\\")):
                    continue
                norm = "/".join(re.split(r"[/\\]", decoded))
                exact.setdefault(norm, (norm, zi))
                folded.setdefault(norm.casefold(), (norm, zi))
            self._folded = folded
            self._exact = exact
        return self._folded

    def names(self) -> List[str]:
        """Decoded ('/'-separated) names of every file entry that can be decoded."""
        return [name for name, _ in self._index().values()]

    def lookup(self, name: str) -> Optional[Tuple[str, zipfile.ZipInfo]]:
        """Decoded-name lookup: exact first, then case-insensitive; separators '/' or '\\'."""
        norm = "/".join(re.split(r"[/\\]", name))
        folded = self._index()
        hit = self._exact.get(norm)
        return hit if hit is not None else folded.get(norm.casefold())

    def find_raw(self, raw: str) -> Optional[zipfile.ZipInfo]:
        for zi in self.zf.infolist():
            if zi.orig_filename == raw:
                return zi
        return None

    def has_raw_ci(self, raw: str) -> bool:
        low = raw.casefold()
        return any(zi.orig_filename.casefold() == low for zi in self.zf.infolist())


class _Ctx:
    """Per-call resources: the ExitStack, limits and a lazily created scratch directory."""

    def __init__(self, stack: contextlib.ExitStack, max_bytes: int, max_ratio: int,
                 work_dir: Optional[Path]) -> None:
        self.stack = stack
        self.max_bytes = max_bytes
        self.max_ratio = max_ratio
        self.work_dir = work_dir
        self._tmp: Optional[Path] = None
        self._counter = 0

    def temp_file(self) -> Path:
        if self._tmp is None:
            base = self.work_dir or _cache_dir()
            base.mkdir(parents=True, exist_ok=True)
            self._tmp = Path(tempfile.mkdtemp(prefix=".lindos-msix-tmp-", dir=str(base)))
            self.stack.callback(shutil.rmtree, str(self._tmp), True)
        self._counter += 1
        return self._tmp / f"inner-{self._counter}.bin"


def _open_zip(fobj: Any, label: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(fobj)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError, NotImplementedError,
            struct.error, UnicodeDecodeError, RuntimeError) as exc:
        raise MsixError(f"{label} is damaged or is not a valid package ({exc}).") from None
    if len(zf.infolist()) > _MAX_ENTRIES:
        zf.close()
        raise MsixError(f"{label} contains more than {_MAX_ENTRIES} files; refusing it.")
    return zf


def _zip_from_file(ctx: _Ctx, path: Path) -> _Zip:
    fh = ctx.stack.enter_context(open(path, "rb"))
    size = os.fstat(fh.fileno()).st_size
    zf = ctx.stack.enter_context(_open_zip(fh, path.name))
    return _Zip(zf, fh, size, path.name, path)


def _read_member(z: _Zip, zi: zipfile.ZipInfo, limit: int, what: str) -> bytes:
    if zi.file_size > limit:
        raise MsixError(f"{what} is unexpectedly large ({_human(zi.file_size)}); refusing it.")
    if zi.flag_bits & 0x1:
        raise MsixError(f"{what} is password-protected; real Windows packages never are.")
    try:
        with z.zf.open(zi) as fh:
            data = fh.read(limit + 1)
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError, NotImplementedError, ValueError,
            RuntimeError) as exc:
        raise MsixError(f"{what} in {z.label} could not be read ({exc}); the file is damaged.") from None
    if len(data) > limit:
        raise MsixError(f"{what} is unexpectedly large; refusing it.")
    return data


def _read_exact(z: _Zip, raw_name: str, what: str) -> bytes:
    zi = z.find_raw(raw_name)
    if zi is None:
        raise MsixError(f"{z.label} has no {raw_name}; it is not a complete Windows package.")
    return _read_member(z, zi, _MAX_XML_BYTES, what)


def _member_data_offset(z: _Zip, zi: zipfile.ZipInfo) -> int:
    """Absolute offset of a member's data, read from its *local* header and bounds-checked."""
    ho = zi.header_offset
    if ho < 0 or ho + 30 > z.size:
        raise MsixError(f"{z.label}: an entry points outside the file; the file is damaged.")
    z.fobj.seek(ho)
    lfh = z.fobj.read(30)
    if len(lfh) != 30 or lfh[:4] != b"PK\x03\x04":
        raise MsixError(f"{z.label}: a file header is missing; the file is damaged.")
    name_len, extra_len = struct.unpack("<HH", lfh[26:30])
    start = ho + 30 + name_len + extra_len
    end = start + zi.compress_size
    limit = min(z.size, getattr(z.zf, "start_dir", z.size) or z.size)
    if end > limit:
        raise MsixError(f"{z.label}: an entry runs past the end of its data; the file is damaged.")
    return start


def _check_size_ratio(zi: zipfile.ZipInfo, name: str, max_bytes: int, max_ratio: int) -> None:
    if zi.file_size < 0 or zi.compress_size < 0:
        raise MsixError(f"{name!r} has an invalid size; the file is damaged.")
    if zi.file_size > max_bytes:
        raise MsixError(f"{name!r} would unpack to {_human(zi.file_size)}, more than the "
                        f"{_human(max_bytes)} limit.")
    if zi.compress_type == zipfile.ZIP_STORED:
        if zi.compress_size != zi.file_size:
            raise MsixError(f"{name!r} has inconsistent sizes; the file is damaged.")
        return
    if zi.file_size > _RATIO_MIN_BYTES and (zi.compress_size <= 0 or zi.file_size > zi.compress_size * max_ratio):
        ratio = zi.file_size // max(1, zi.compress_size)
        raise MsixError(f"{name!r} is compressed about {ratio}:1, far more than real app files ever are (limit "
                        f"{max_ratio}:1). This looks like a 'zip bomb', so Lindos refuses the package.")


def _open_member(ctx: _Ctx, z: _Zip, zi: zipfile.ZipInfo, label: str) -> _Zip:
    """Open a ZIP stored inside another ZIP: a bounded window when stored, a scratch copy when deflated."""
    if zi.flag_bits & 0x1:
        raise MsixError(f"{label} is password-protected; real Windows packages never are.")
    if zi.compress_type == zipfile.ZIP_STORED:
        if zi.compress_size != zi.file_size:
            raise MsixError(f"{label} has inconsistent sizes; the file is damaged.")
        start = _member_data_offset(z, zi)
        sub = _SubFile(z.fobj, start, zi.compress_size, label)
        zf = ctx.stack.enter_context(_open_zip(sub, label))
        return _Zip(zf, sub, zi.compress_size, label)
    if zi.compress_type != zipfile.ZIP_DEFLATED:
        raise MsixError(f"{label} uses a compression method Windows packages never use.")
    _check_size_ratio(zi, label, ctx.max_bytes, ctx.max_ratio)
    tmp = ctx.temp_file()
    _check_free(tmp.parent, zi.file_size)
    copied = 0
    try:
        with z.zf.open(zi) as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > zi.file_size or copied > ctx.max_bytes:
                    raise MsixError(f"{label} is larger than it claims; refusing it.")
                dst.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError) as exc:
        raise MsixError(f"{label} could not be unpacked ({exc}); the file is damaged.") from None
    fh = ctx.stack.enter_context(open(tmp, "rb"))
    zf = ctx.stack.enter_context(_open_zip(fh, label))
    return _Zip(zf, fh, copied, label)


def _sniff(zf: zipfile.ZipFile) -> str:
    """package | bundle | upload | corrupt | unknown, by exact footprint entry names (MSIX SDK)."""
    names = {zi.orig_filename for zi in zf.infolist()}
    has_package = PACKAGE_MANIFEST in names
    has_bundle = BUNDLE_MANIFEST in names
    if has_package and has_bundle:
        return "corrupt"
    if has_bundle:
        return "bundle"
    if has_package:
        return "package"
    if _upload_candidates(zf):
        return "upload"
    return "unknown"


def _upload_candidates(zf: zipfile.ZipFile) -> List[Tuple[str, zipfile.ZipInfo]]:
    out = []
    for zi in zf.infolist():
        try:
            decoded = _decode_name(zi.orig_filename)
        except MsixError:
            continue
        if not decoded or "/" in decoded or "\\" in decoded:
            continue
        if decoded.casefold().endswith(PACKAGE_SUFFIXES + BUNDLE_SUFFIXES):
            out.append((decoded, zi))
    return out


# --------------------------------------------------------------------------- #
# entry names (decode first, then validate)
# --------------------------------------------------------------------------- #
def _decode_name(raw: str) -> str:
    try:
        return urllib.parse.unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MsixError(f"Unsafe file path in the package: {raw!r} is not valid UTF-8. Lindos refuses it.") from None


def _check_segment(seg: str, full: str) -> None:
    def bad(why: str) -> None:
        raise MsixError(f"Unsafe file path in the package: {full!r} ({why}). Lindos refuses to install it.")

    if seg in ("", "."):
        bad("empty or '.' path segment")
    if seg == "..":
        bad("'..' would escape the install folder")
    if len(seg) > _MAX_SEGMENT_CHARS:
        bad("name too long")
    for ch in seg:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            bad("control character")
        if ch in _RESERVED_CHARS:
            bad(f"reserved character {ch!r}")
        if 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
            bad("invalid Unicode character")
    if seg[-1] in ". ":
        bad("name ends with a dot or a space")
    if _DEVICE_RE.match(seg):
        bad("reserved Windows device name")


def _split_member(raw: str) -> Tuple[str, Tuple[str, ...], bool]:
    """Decode a raw ZIP entry name, then validate it -> (decoded '/'-name, parts, is_dir)."""
    if "\x00" in raw:
        raise MsixError(f"Unsafe file path in the package: {raw!r} (NUL character). Lindos refuses to install it.")
    name = _decode_name(raw)
    if not name:
        raise MsixError("The package contains an entry without a name; the file is damaged.")
    if name[0] in "/\\":
        raise MsixError(f"Unsafe file path in the package: {name!r} (absolute path). Lindos refuses to install it.")
    if re.match(r"^[A-Za-z]:", name):
        raise MsixError(f"Unsafe file path in the package: {name!r} (drive letter). Lindos refuses to install it.")
    is_dir = name[-1] in "/\\"
    body = name[:-1] if is_dir else name
    parts = tuple(re.split(r"[/\\]", body))
    for seg in parts:
        _check_segment(seg, name)
    joined = "/".join(parts)
    if len(joined) > _MAX_PATH_CHARS:
        raise MsixError(f"Unsafe file path in the package: {joined[:80]!r}... (longer than {_MAX_PATH_CHARS} "
                        "characters). Lindos refuses to install it.")
    return joined, parts, is_dir


def _is_symlink(zi: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((zi.external_attr >> 16) & 0xFFFF)


def _is_footprint(parts: Sequence[str]) -> bool:
    first = parts[0].casefold()
    if len(parts) == 1 and first in _FOOTPRINT_FILES:
        return True
    return first in _FOOTPRINT_DIRS


def _key(parts: Sequence[str]) -> str:
    return "/".join(p.casefold() for p in parts)


@dataclass
class _Entry:
    zi: zipfile.ZipInfo
    name: str
    parts: Tuple[str, ...]


@dataclass
class _Plan:
    entries: List[_Entry]
    total: int

    def by_key(self) -> Dict[str, _Entry]:
        return {_key(e.parts): e for e in self.entries}


def _plan_entries(z: _Zip, max_bytes: int, max_ratio: int) -> _Plan:
    """Validate every entry (names, types, sizes) and list the payload files to extract."""
    entries: List[_Entry] = []
    seen: Dict[str, str] = {}
    total = 0
    for zi in z.zf.infolist():
        name, parts, is_dir = _split_member(zi.orig_filename)
        if _is_symlink(zi):
            raise MsixError(f"The package contains a symbolic link ({name!r}); Lindos refuses to install it.")
        if zi.flag_bits & 0x1:
            raise MsixError(f"The package contains a password-protected file ({name!r}); real Windows packages "
                            "never do.")
        if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise MsixError(f"{name!r} uses a compression method Windows packages never use.")
        key = _key(parts)
        if key in seen:
            raise MsixError(f"The package contains two files whose names differ only in upper/lower case "
                            f"({seen[key]!r} and {name!r}). Windows can't tell them apart, so Lindos refuses it.")
        seen[key] = name
        if is_dir:
            if zi.file_size:
                raise MsixError(f"{name!r} is both a folder and a file; the package is damaged.")
            continue
        if _is_footprint(parts):
            continue
        _check_size_ratio(zi, name, max_bytes, max_ratio)
        total += zi.file_size
        if total > max_bytes:
            raise MsixError(f"The package would unpack to more than the {_human(max_bytes)} limit.")
        entries.append(_Entry(zi, name, parts))
    file_keys = {_key(e.parts) for e in entries}
    for e in entries:
        for i in range(1, len(e.parts)):
            if _key(e.parts[:i]) in file_keys:
                raise MsixError(f"{'/'.join(e.parts[:i])!r} is both a file and a folder in this package; "
                                "it is damaged.")
    return _Plan(entries, total)


# --------------------------------------------------------------------------- #
# package manifest
# --------------------------------------------------------------------------- #
@dataclass
class _AppRaw:
    app: MsixApp
    logo_refs: List[str]
    note: str


@dataclass
class _Manifest:
    win8: bool
    name: str
    publisher: str
    version: str
    arch: str
    resource_id: str
    display_name: str            # "" when missing or ms-resource:
    publisher_display: str       # "" when missing or ms-resource:
    store_logo: str
    framework: bool
    resource_package: bool
    dependencies: List[Dict[str, str]]
    families: List[str]
    run_full_trust: bool
    apps: List[_AppRaw]
    warnings: List[str]


def _app_class(el: ET.Element, *, win8: bool, run_full_trust: bool) -> Tuple[str, str, bool]:
    """(app_class, why, sandboxed) for one <Application> (rule: SPEC-WINDOWS §28.4 + research)."""
    if win8:
        return "uwp", "a Windows 8 Store app", False
    host_id = _attr_ms(el, "HostId", (NS_UAP10,))
    if host_id:
        return "needs-host", f"it runs inside another app's host runtime ({host_id})", False
    rb = _attr_ms(el, "RuntimeBehavior", (NS_UAP10, NS_PREVIEWSEC2)).casefold()
    tl = _attr_ms(el, "TrustLevel", (NS_UAP10, NS_PREVIEWSEC)).casefold()
    ep = _attr(el, "EntryPoint").casefold()
    exe = _attr(el, "Executable")
    start_page = _attr(el, "StartPage")
    if rb == "appsilo" or tl == "appsilo":
        return "unknown", "it uses the preview 'appSilo' isolation model", False
    if tl and tl not in ("mediumil", "appcontainer"):
        return "unknown", f"its trust level {tl!r} is unknown", False
    if ep == "windows.fulltrustapplication":
        ep_kind = "fulltrust"
    elif ep == "windows.partialtrustapplication":
        ep_kind = "partialtrust"
    else:
        ep_kind = "class" if ep else ""
    contradiction = ("unknown", "its manifest contradicts itself (RuntimeBehavior/TrustLevel vs EntryPoint)", False)
    no_program = ("unknown", "its manifest names no program file", False)
    if rb:
        if rb in ("packagedclassicapp", "win32app"):
            if ep_kind == "class" or start_page:
                return contradiction
            if (ep_kind == "fulltrust" and tl == "appcontainer") or (ep_kind == "partialtrust" and tl == "mediumil"):
                return contradiction
            if not exe:
                return no_program
            return "win32", "", tl != "mediumil" and rb == "packagedclassicapp"
        if rb == "windowsapp":
            if ep_kind in ("fulltrust", "partialtrust"):
                return contradiction
            return "uwp", "it is a UWP app", False
        return "unknown", f"its RuntimeBehavior {rb!r} is unknown", False
    if ep_kind == "fulltrust":
        if tl == "appcontainer":
            return contradiction
        return ("win32", "", False) if exe else no_program
    if ep_kind == "partialtrust":
        if tl == "mediumil":
            return contradiction
        return ("win32", "", True) if exe else no_program
    if start_page:
        return "uwp", "it is an HTML/JavaScript UWP app", False
    if ep_kind == "class":
        return "uwp", "it is a UWP/XAML app", False
    if exe:
        if run_full_trust:
            return "win32", "", False
        return "unknown", "its manifest has neither EntryPoint nor RuntimeBehavior", False
    return no_program


def _parse_app(el: ET.Element, *, win8: bool, run_full_trust: bool, pkg_display: str, identity_name: str,
               warnings: List[str]) -> _AppRaw:
    app_id = _attr(el, "Id")
    exe = _attr(el, "Executable")
    ep = _attr(el, "EntryPoint")
    ve = None
    for child in list(el):
        if _split_tag(child.tag)[1] == "VisualElements":
            ve = child
            break
    ve_display = _attr(ve, "DisplayName")
    if _is_ms_resource(ve_display):
        ve_display = ""
    list_entry = _attr(ve, "AppListEntry").casefold() != "none"
    refs: List[str] = []
    for key in ("Square44x44Logo", "Square30x30Logo", "SmallLogo", "Square150x150Logo", "Logo"):
        value = _attr(ve, key)
        if value and value not in refs:
            refs.append(value)
    params = _attr_ms(el, "Parameters", (NS_UAP11, NS_UAP10))
    work_dir = _attr_ms(el, "CurrentDirectoryPath", (NS_UAP11,))
    subsystem = _attr_ms(el, "Subsystem", (NS_DESKTOP4, NS_UAP10, NS_IOT2))
    app_class, why, sandboxed = _app_class(el, win8=win8, run_full_trust=run_full_trust)
    display = ve_display or pkg_display or _humanize(identity_name) or _exe_stem(exe) or app_id
    if app_class == "win32":
        if sandboxed:
            warnings.append(f"'{display}' runs in a sandbox (AppContainer) on Windows; Wine runs it without one.")
        elif not run_full_trust:
            warnings.append(f"'{display}' is marked as a full-trust desktop app but the manifest doesn't declare "
                            "runFullTrust (the manifest is inconsistent).")
    app = MsixApp(id=app_id, display_name=display, executable=exe, entry_point=ep, app_class=app_class,
                  parameters=params, working_dir=work_dir, logo="", list_entry=list_entry,
                  console=subsystem.casefold() == "console")
    return _AppRaw(app, refs, why)


def _parse_manifest(data: bytes) -> _Manifest:
    root = _parse_xml(data, "AppxManifest.xml")
    ns, local = _split_tag(root.tag)
    if local != "Package" or ns not in _PACKAGE_NAMESPACES:
        raise MsixError(f"AppxManifest.xml uses a schema Lindos doesn't know ({ns or 'no namespace'}); "
                        "it isn't a Windows app manifest.")
    win8 = ns in _WIN8_NAMESPACES
    nss = _PACKAGE_NAMESPACES
    ident = _first(root, "Identity", nss)
    if ident is None:
        raise MsixError("AppxManifest.xml has no <Identity>; the package is damaged.")
    name = _attr(ident, "Name")
    publisher = _attr(ident, "Publisher")
    version = _attr(ident, "Version")
    arch = (_attr(ident, "ProcessorArchitecture") or "neutral").casefold()
    rid = _attr(ident, "ResourceId")
    _check_identity(name, publisher, version, arch, rid, "package")
    props = _first(root, "Properties", nss)
    display = _text(_first(props, "DisplayName", nss))
    pub_display = _text(_first(props, "PublisherDisplayName", nss))
    store_logo = _text(_first(props, "Logo", nss))
    framework = _text(_first(props, "Framework", nss)).casefold() == "true"
    resource_package = _text(_first(props, "ResourcePackage", nss)).casefold() == "true"
    display = "" if _is_ms_resource(display) else display
    pub_display = "" if _is_ms_resource(pub_display) else pub_display

    deps: List[Dict[str, str]] = []
    families: List[str] = []
    for child in list(_first(root, "Dependencies", nss) or []):
        cns, clocal = _split_tag(child.tag)
        if not cns.startswith(_MS_APPX_NS_PREFIX):
            continue
        if clocal == "PackageDependency":
            deps.append({"name": _attr(child, "Name"), "publisher": _attr(child, "Publisher"),
                         "min_version": _attr(child, "MinVersion")})
        elif clocal == "TargetDeviceFamily":
            families.append(_attr(child, "Name"))

    run_full_trust = any(
        _attr(c, "Name").casefold() == "runfulltrust"
        for c in _children(_first(root, "Capabilities", nss), "Capability", (NS_RESCAP,)))

    warnings: List[str] = []
    apps: List[_AppRaw] = []
    app_elems = _children(_first(root, "Applications", nss), "Application", nss)
    if len(app_elems) > _MAX_APPS:
        warnings.append(f"The package lists {len(app_elems)} apps; only the first {_MAX_APPS} are considered.")
        app_elems = app_elems[:_MAX_APPS]
    seen_ids: Set[str] = set()
    for el in app_elems:
        raw = _parse_app(el, win8=win8, run_full_trust=run_full_trust, pkg_display=display, identity_name=name,
                         warnings=warnings)
        if not raw.app.id or raw.app.id.casefold() in seen_ids:
            warnings.append("An app entry without a unique Id was ignored.")
            continue
        seen_ids.add(raw.app.id.casefold())
        apps.append(raw)
    return _Manifest(win8=win8, name=name, publisher=publisher, version=version, arch=arch, resource_id=rid,
                     display_name=display, publisher_display=pub_display, store_logo=store_logo,
                     framework=framework, resource_package=resource_package, dependencies=deps,
                     families=families, run_full_trust=run_full_trust, apps=apps, warnings=warnings)


# --------------------------------------------------------------------------- #
# logos (MRT qualifier-aware lookup)
# --------------------------------------------------------------------------- #
_QUAL_TOKEN_RE = re.compile(r"^[a-z]+-[a-z0-9]+$")
_QUAL_SEGMENT_RE = re.compile(r"^[a-z]+-[a-z0-9]+(?:_[a-z]+-[a-z0-9]+)*$")


def _qualifier_dirs(base_dirs: Sequence[str], cand_dirs: Sequence[str]) -> Optional[List[str]]:
    """Qualifier tokens of the extra folders in ``cand_dirs`` if it equals ``base_dirs`` plus
    qualifier folders (``scale-200``, ``en-us``, ``contrast-high``...), else None."""
    tokens: List[str] = []
    j = 0
    for seg in cand_dirs:
        if j < len(base_dirs) and seg == base_dirs[j]:
            j += 1
        elif _QUAL_SEGMENT_RE.match(seg):
            tokens.extend(seg.split("_"))
        else:
            return None
    return tokens if j == len(base_dirs) else None


def _logo_score(tokens: Sequence[str]) -> Tuple[int, int, int, int]:
    quals: Dict[str, str] = {}
    for token in tokens:
        key, _, value = token.partition("-")
        quals.setdefault(key, value)
    penalty = 0
    if quals.get("contrast", "standard") != "standard":
        penalty += 2
    if quals.get("theme") == "light":
        penalty += 1
    alt = quals.get("altform", "")
    if alt == "lightunplated":
        penalty += 1
    target = int(quals["targetsize"]) if quals.get("targetsize", "").isdigit() else 0
    scale = int(quals["scale"]) if quals.get("scale", "").isdigit() else 0
    if target == 256 and alt == "unplated":
        tier = 6
    elif scale == 400:
        tier = 5
    elif scale == 200:
        tier = 4
    elif not target and not scale:
        tier = 3
    else:
        tier = 2
    return (-penalty, tier, 1 if alt == "unplated" else 0, target or scale)


def _resolve_logo(ref: str, names: Sequence[str]) -> str:
    """Best package file for a manifest logo reference ('' if none).  Separator/case-insensitive;
    prefers targetsize-256_altform-unplated > scale-400 > scale-200 > plain > other sizes."""
    ref_parts = [p.casefold() for p in re.split(r"[/\\]", (ref or "").strip()) if p]
    if not ref_parts or any(p in (".", "..") for p in ref_parts):
        return ""
    stem, dot, ext = ref_parts[-1].rpartition(".")
    if not dot or not stem or not ext:
        return ""
    file_re = re.compile("^" + re.escape(stem) + r"(?:\.([a-z0-9_\-]+))?\." + re.escape(ext) + "$")
    best: Optional[Tuple[Tuple[int, int, int, int], str]] = None
    for name in names:
        parts = name.split("/")
        m = file_re.match(parts[-1].casefold())
        if not m:
            continue
        tokens = m.group(1).split("_") if m.group(1) else []
        if any(not _QUAL_TOKEN_RE.match(t) for t in tokens):
            continue
        dir_tokens = _qualifier_dirs(ref_parts[:-1], [p.casefold() for p in parts[:-1]])
        if dir_tokens is None:
            continue
        score = _logo_score(tokens + dir_tokens)
        if best is None or score > best[0] or (score == best[0] and name < best[1]):
            best = (score, name)
    return best[1] if best else ""


# --------------------------------------------------------------------------- #
# bundles
# --------------------------------------------------------------------------- #
@dataclass
class _BundlePkg:
    type: str
    version: str
    arch: str
    resource_id: str
    file_name: str
    offset: Optional[int]
    size: Optional[int]
    is_stub: bool
    families: List[str]


@dataclass
class _Bundle:
    name: str
    publisher: str
    version: str
    packages: List[_BundlePkg]
    warnings: List[str]


def _int_attr(el: ET.Element, key: str) -> Optional[int]:
    value = _attr(el, key)
    if not value:
        return None
    if not re.fullmatch(r"\d{1,20}", value):
        raise MsixError(f"The bundle manifest has an invalid {key} value ({value!r}); the file is damaged.")
    return int(value)


def _parse_bundle(data: bytes) -> _Bundle:
    root = _parse_xml(data, "AppxBundleManifest.xml")
    ns, local = _split_tag(root.tag)
    if local != "Bundle" or ns not in BUNDLE_NAMESPACES:
        raise MsixError(f"AppxBundleManifest.xml uses a schema Lindos doesn't know ({ns or 'no namespace'}).")
    ident = _first(root, "Identity", BUNDLE_NAMESPACES)
    if ident is None:
        raise MsixError("The bundle manifest has no <Identity>; the file is damaged.")
    name, publisher, version = _attr(ident, "Name"), _attr(ident, "Publisher"), _attr(ident, "Version")
    _check_identity(name, publisher, version, "neutral", "~", "bundle")
    warnings: List[str] = []
    packages: List[_BundlePkg] = []
    seen: Set[str] = set()
    for el in list(_first(root, "Packages", BUNDLE_NAMESPACES) or []):
        ens, elocal = _split_tag(el.tag)
        if elocal != "Package" or ens not in _BUNDLE_PACKAGE_NAMESPACES:
            continue
        file_name = _attr(el, "FileName")
        if not file_name or len(file_name) > 256 or not re.match(r"^.+\.(appx|msix)$", file_name, re.IGNORECASE):
            raise MsixError(f"The bundle manifest has an invalid FileName ({file_name!r}); the file is damaged.")
        if file_name.casefold() in seen:
            raise MsixError(f"The bundle manifest lists {file_name!r} twice; the file is damaged.")
        seen.add(file_name.casefold())
        pkg_version = _attr(el, "Version")
        _version_tuple(pkg_version, f"version of {file_name}")
        arch = (_attr(el, "Architecture") or "neutral").casefold()
        if not _ARCH_RE.match(arch):
            raise MsixError(f"The bundle manifest has an invalid Architecture for {file_name!r}.")
        rid = _attr(el, "ResourceId")
        if not _RESID_RE.match(rid):
            raise MsixError(f"The bundle manifest has an invalid ResourceId for {file_name!r}.")
        stub = _attr(el, "IsStub", "", NS_BUNDLE_2019).casefold() in ("true", "1")
        families = [_attr(t, "Name") for t in el.iter() if _split_tag(t.tag)[1] == "TargetDeviceFamily"]
        packages.append(_BundlePkg(type=(_attr(el, "Type") or "resource").casefold(), version=pkg_version,
                                   arch=arch, resource_id=rid, file_name=file_name,
                                   offset=_int_attr(el, "Offset"), size=_int_attr(el, "Size"),
                                   is_stub=stub, families=families))
    if _children(root, "OptionalBundle", BUNDLE_NAMESPACES):
        warnings.append("This bundle refers to optional extra packages; Lindos installs only the main app.")
    return _Bundle(name=name, publisher=publisher, version=version, packages=packages, warnings=warnings)


def _family_score(families: Sequence[str]) -> int:
    if not families:
        return 1
    folded = {f.casefold() for f in families}
    return 2 if folded & set(_DESKTOP_FAMILIES) else 0


def _select_bundle_package(bundle: _Bundle) -> Tuple[Optional[_BundlePkg], List[str]]:
    """The best application package for an x86_64 PC, or (None, architectures present)."""
    apps = [p for p in bundle.packages if p.type == "application" and not p.is_stub]
    if not apps:
        if any(p.type == "application" for p in bundle.packages):
            raise MsixError("This bundle only contains placeholder ('stub') app packages; the real app must be "
                            "downloaded by Windows. Get the developer's regular download instead.")
        raise MsixError("This bundle contains only resource packages (languages or pictures) — there is no app "
                        "in it.")
    eligible = [p for p in apps if p.arch in _ARCH_RANK]
    if not eligible:
        return None, sorted({p.arch for p in apps})
    best = max(eligible, key=lambda p: (_ARCH_RANK[p.arch], _family_score(p.families), _version_tuple(p.version)))
    return best, []


def _open_flat_sibling(ctx: _Ctx, z: _Zip, pkg: _BundlePkg) -> _Zip:
    """Flat bundle: the package is a separate file next to the bundle -- same folder only."""
    fname = pkg.file_name
    if z.path is None:
        raise MsixError("This bundle keeps its app in separate files (a 'flat bundle'), which is not supported "
                        "inside another package.")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", fname) or fname.startswith(("\\\\", "//")):
        raise MsixError(f"The bundle points to {fname!r} on the network or the web. Lindos only uses files in the "
                        "same folder as the bundle and never downloads them on its own.")
    if "/" in fname or "\\" in fname:
        raise MsixError(f"The bundle points to {fname!r}, outside its own folder. Lindos only uses files in the "
                        "same folder as the bundle.")
    _check_segment(fname, fname)
    folder = z.path.parent
    candidate = folder / fname
    if not os.path.lexists(candidate):
        try:
            for entry in os.listdir(folder):
                if entry.casefold() == fname.casefold():
                    candidate = folder / entry
                    break
        except OSError:
            pass
    if not os.path.lexists(candidate):
        raise MsixError(f"The rest of this app's files were not downloaded: {fname!r} must be in the same folder "
                        f"as the bundle ({folder}).")
    st = os.lstat(candidate)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise MsixError(f"{candidate} is not a regular file (for example a symbolic link); Lindos refuses it.")
    if candidate.resolve().parent != folder.resolve():
        raise MsixError(f"{candidate} is outside the bundle's folder; Lindos refuses it.")
    if pkg.size is not None and st.st_size != pkg.size:
        raise MsixError(f"{fname!r} has a different size than the bundle expects; it is damaged or was replaced.")
    fh = ctx.stack.enter_context(open(candidate, "rb"))
    zf = ctx.stack.enter_context(_open_zip(fh, fname))
    return _Zip(zf, fh, st.st_size, fname, candidate)


def _open_bundle_inner(ctx: _Ctx, z: _Zip, pkg: _BundlePkg) -> _Zip:
    hit = z.lookup(pkg.file_name)
    if hit is not None:
        _, zi = hit
        if zi.compress_type != zipfile.ZIP_STORED:
            raise MsixError(f"The app package {pkg.file_name!r} inside this bundle is compressed. Real bundles store "
                            "their packages uncompressed, so this file is damaged or was altered; Lindos refuses it.")
        if pkg.size is not None and pkg.size != zi.compress_size:
            raise MsixError(f"The size of {pkg.file_name!r} doesn't match the bundle manifest; the file is damaged "
                            "or was altered.")
        start = _member_data_offset(z, zi)
        if pkg.offset is not None and pkg.offset != start:
            raise MsixError(f"The position of {pkg.file_name!r} doesn't match the bundle manifest; the file is "
                            "damaged or was altered.")
        return _open_member(ctx, z, zi, pkg.file_name)
    if pkg.offset is not None:
        raise MsixError(f"The bundle says {pkg.file_name!r} is inside it, but it isn't; the file is damaged.")
    return _open_flat_sibling(ctx, z, pkg)


def _verify_inner_identity(bundle: _Bundle, pkg: _BundlePkg, m: _Manifest) -> None:
    """MSIX SDK rule: Name/Publisher equal the bundle's; Version/Architecture equal the <Package> element's."""
    if m.name.casefold() != bundle.name.casefold():
        raise MsixError(f"{pkg.file_name!r} belongs to a different app ({m.name}) than the bundle ({bundle.name}); "
                        "the file was altered.")
    if m.publisher != bundle.publisher:
        raise MsixError(f"{pkg.file_name!r} has a different publisher than the bundle; the file was altered.")
    if _version_tuple(m.version) != _version_tuple(pkg.version):
        raise MsixError(f"{pkg.file_name!r} has version {m.version} but the bundle manifest says {pkg.version}; "
                        "the file was altered.")
    if (m.arch or "neutral") != pkg.arch:
        raise MsixError(f"{pkg.file_name!r} is built for {m.arch} but the bundle manifest says {pkg.arch}; "
                        "the file was altered.")


# --------------------------------------------------------------------------- #
# encrypted / msixvc / appinstaller
# --------------------------------------------------------------------------- #
def _split_full_name(text: str) -> Optional[Dict[str, str]]:
    m = _FULL_NAME_RE.match(text or "")
    return m.groupdict() if m else None


def _encrypted_header_name(head: bytes) -> str:
    """Plaintext PackageFullName from an EXPH/EXSH/EXBH header: a WORD char count, a WORD byte count
    and UTF-16LE text that follow the key-ID array (community-documented layout).  The exact position
    is not relied on: every candidate is bounds-checked and must be a well-formed full name."""
    limit = min(len(head), _ENCRYPTED_SCAN_BYTES)
    for off in range(4, max(4, limit - 4)):
        chars, nbytes = struct.unpack_from("<HH", head, off)
        if not 20 <= chars <= 127 or nbytes not in (chars * 2, chars * 2 + 2):
            continue
        end = off + 4 + chars * 2
        if end > limit:
            continue
        try:
            text = head[off + 4:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if _FULL_NAME_RE.match(text):
            return text
    return ""


def _info_from_full_name(path: Path, kind: str, full: str, *, status: str, reason: str,
                         signals: List[str], warnings: List[str]) -> MsixInfo:
    parts = _split_full_name(full) or {}
    name = parts.get("name", "")
    pid = (parts.get("pid") or "").lower()
    return MsixInfo(path=str(path), kind=kind, name=name, publisher="", publisher_display="",
                    version=parts.get("version", ""), arch=(parts.get("arch") or "").lower(),
                    resource_id=parts.get("rid", ""), display_name=_humanize(name) or path.stem,
                    publisher_id=pid, package_full_name=full if parts else "",
                    package_family_name=f"{name}_{pid}" if parts else "", apps=[], dependencies=[],
                    framework=False, resource_package=False, signed=False, unsigned_marker=False,
                    store_signals=signals, status=status, reason=reason, selected_package=None,
                    warnings=warnings)


def _encrypted_info(path: Path) -> MsixInfo:
    with open(path, "rb") as fh:
        head = fh.read(_ENCRYPTED_SCAN_BYTES)
    magic = head[:4]
    warnings = [f"Encrypted Store {ENCRYPTED_MAGICS.get(magic, 'package')} ({magic.decode('ascii', 'replace')}). "
                "Lindos never decrypts Store packages."]
    full = _encrypted_header_name(head)
    if full:
        warnings.append("Name and version were read from the file's unencrypted header (not verified).")
    else:
        stem = path.name[: -len(path.suffix)] if path.suffix else path.name
        full = stem if _split_full_name(stem) else ""
    return _info_from_full_name(path, "encrypted", full, status="unsupported", reason=_REASON_ENCRYPTED,
                                signals=["encrypted"], warnings=warnings)


def _msixvc_info(path: Path) -> MsixInfo:
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    return _info_from_full_name(path, "msixvc", stem if _split_full_name(stem) else "", status="unsupported",
                                reason=_REASON_MSIXVC, signals=["xbox-gdk-game"], warnings=[])


def _looks_like_appinstaller(head: bytes) -> bool:
    try:
        text = _decode_xml_bytes(head, "file", strict=False)
    except MsixError:  # pragma: no cover - strict=False never raises
        return False
    return bool(_APPINSTALLER_ROOT_RE.match(text))


def parse_appinstaller(path: Path) -> Dict[str, str]:
    """Read an ``.appinstaller`` file (MainPackage or MainBundle).  Never downloads anything.

    Returns ``{uri, kind, name, version, publisher, host}`` plus ``scheme``, ``arch``,
    ``resource_id`` and ``appinstaller_uri`` (the file's own update location, informational only;
    UpdateSettings are ignored).  ``kind`` is ``package`` or ``bundle``; ``host`` is "" when the
    URI has none.  Raises :class:`MsixError` for anything malformed.
    """
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            data = fh.read(_MAX_APPINSTALLER_BYTES + 1)
    except OSError as exc:
        raise MsixError(f"{p.name} could not be read ({exc.strerror or exc}).") from None
    if len(data) > _MAX_APPINSTALLER_BYTES:
        raise MsixError(f"{p.name} is too large to be an App Installer file.")
    root = _parse_xml(data, p.name)
    ns, local = _split_tag(root.tag)
    if local != "AppInstaller" or ns not in APPINSTALLER_NAMESPACES:
        raise MsixError(f"{p.name} is not an App Installer file Lindos understands ({ns or 'no namespace'}).")
    mains = [c for c in list(root) if _split_tag(c.tag)[0] in APPINSTALLER_NAMESPACES
             and _split_tag(c.tag)[1] in ("MainPackage", "MainBundle")]
    if len(mains) != 1:
        raise MsixError(f"{p.name} must name exactly one MainPackage or MainBundle.")
    el = mains[0]
    kind = "bundle" if _split_tag(el.tag)[1] == "MainBundle" else "package"
    name, publisher, version, uri = (_attr(el, "Name"), _attr(el, "Publisher"), _attr(el, "Version"),
                                     _attr(el, "Uri"))
    arch = "neutral" if kind == "bundle" else (_attr(el, "ProcessorArchitecture") or "neutral").casefold()
    rid = "~" if kind == "bundle" else _attr(el, "ResourceId")
    _check_identity(name, publisher, version, arch, rid, "app named in the App Installer file")
    if not uri or len(uri) > 2084 or any(ord(c) < 0x21 for c in uri):
        raise MsixError(f"{p.name} has a missing or invalid download address.")
    if uri.startswith(("\\\\", "//")):
        scheme, host = "unc", re.split(r"[\\/]", uri.lstrip("\\/"))[0]
    else:
        try:
            parts = urllib.parse.urlsplit(uri)
            host = parts.hostname or ""
        except ValueError:
            raise MsixError(f"{p.name} has an invalid download address.") from None
        scheme = parts.scheme.casefold()
    return {"uri": uri, "kind": kind, "name": name, "version": version, "publisher": publisher, "host": host,
            "scheme": scheme, "arch": arch, "resource_id": rid, "appinstaller_uri": _attr(root, "Uri")}


def appinstaller_refusal(ai: Dict[str, str]) -> str:
    """Why Lindos will not download what an .appinstaller points to ("" = allowed, after consent)."""
    scheme = (ai.get("scheme") or "").casefold()
    if scheme == "https" and ai.get("host"):
        return ""
    if scheme == "unc":
        return ("This App Installer file points to a network share. Lindos only downloads apps over a secure "
                "(https) connection; copy the package to this PC and open it directly instead.")
    if scheme == "file":
        return "This App Installer file points to a local file; open that package file directly instead."
    return (f"This App Installer file points to a {scheme or 'non-web'} address. Lindos only downloads apps over "
            "a secure (https) connection; get the package from the developer's website instead.")


def check_appinstaller_target(ai: Dict[str, str], info: MsixInfo) -> None:
    """After a consented download: the package must be exactly the one the .appinstaller named."""
    problems = []
    if (info.name or "").casefold() != (ai.get("name") or "").casefold():
        problems.append(f"name {info.name!r} instead of {ai.get('name')!r}")
    if info.publisher != ai.get("publisher"):
        problems.append("a different publisher")
    if ai.get("kind") == "package":
        expected_version, expected_arch = ai.get("version", ""), (ai.get("arch") or "neutral")
        actual_version, actual_arch = info.version, info.arch
        if actual_arch != expected_arch:
            problems.append(f"architecture {actual_arch} instead of {expected_arch}")
    else:
        expected_version, actual_version = ai.get("version", ""), (info.bundle_version or info.version)
    try:
        if _version_tuple(actual_version) != _version_tuple(expected_version):
            problems.append(f"version {actual_version} instead of {expected_version}")
    except MsixError:
        problems.append("an invalid version")
    if problems:
        raise MsixError("The downloaded package is not the one the App Installer file promised ("
                        + "; ".join(problems) + "). Lindos refuses to install it.")


def _appinstaller_info(path: Path) -> MsixInfo:
    ai = parse_appinstaller(path)
    refusal = appinstaller_refusal(ai)
    pid = publisher_id(ai["publisher"])
    full, family = _full_names(ai["name"], ai["version"], ai["arch"], ai["resource_id"], pid)
    reason = refusal or (f"This file only points to an app on {ai['host']}. Lindos asks before downloading it "
                         "(secure https only) and then installs it like any MSIX package.")
    return MsixInfo(path=str(path), kind="appinstaller", name=ai["name"], publisher=ai["publisher"],
                    publisher_display=_cn(ai["publisher"]), version=ai["version"], arch=ai["arch"],
                    resource_id=ai["resource_id"], display_name=_humanize(ai["name"]) or ai["name"],
                    publisher_id=pid, package_full_name=full, package_family_name=family, apps=[],
                    dependencies=[], framework=False, resource_package=False, signed=False,
                    unsigned_marker=bool(_UNSIGNED_RE.search(ai["publisher"])), store_signals=[],
                    status="unsupported" if refusal else "partial", reason=reason, selected_package=None,
                    warnings=["Automatic updates from App Installer files are not used on Lindos."])


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #
def classify(path: Path) -> str:
    """One of :data:`KINDS`, decided by the file's content (never raises)."""
    try:
        p = Path(path)
        with open(p, "rb") as fh:
            head = fh.read(_SNIFF_BYTES)
            if head[:4] in ENCRYPTED_MAGICS:
                return "encrypted"
            if head[:4] == b"PK\x03\x04":
                fh.seek(0)
                try:
                    with zipfile.ZipFile(fh) as zf:
                        kind = _sniff(zf)
                except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError,
                        NotImplementedError, struct.error, UnicodeDecodeError, RuntimeError):
                    return "unknown"
                return kind if kind in KINDS else "unknown"
            if _looks_like_appinstaller(head):
                return "appinstaller"
        if p.suffix.casefold() == ".msixvc":
            # No public container magic exists for GDK packages; the extension is the only signal.
            return "msixvc"
        return "unknown"
    except (OSError, ValueError, TypeError):
        return "unknown"
    except Exception as exc:  # noqa: BLE001 - classify() must never raise on hostile input
        log.debug("classify(%s) failed: %s", path, exc)
        return "unknown"


def _unknown_reason(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            if fh.read(4) == b"PK\x03\x04":
                fh.seek(0)
                with zipfile.ZipFile(fh) as zf:
                    if _sniff(zf) == "corrupt":
                        return (f"{path.name} contains both an app manifest and a bundle manifest, which never "
                                "happens in a real package. The file is damaged or was altered.")
                return f"{path.name} is a ZIP archive but not an MSIX/APPX package (it has no AppxManifest.xml)."
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError, NotImplementedError,
            struct.error, UnicodeDecodeError, RuntimeError):
        return f"{path.name} is damaged: it looks like a package but can't be opened."
    return f"{path.name} is not an MSIX/APPX package, bundle or App Installer file."


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
@dataclass
class _Resolved:
    info: MsixInfo
    pkg: Optional[_Zip] = None
    manifest: Optional[_Manifest] = None
    bundle: Optional[_Bundle] = None
    bundle_zip: Optional[_Zip] = None
    plan: Optional[_Plan] = None
    ctx: Optional[_Ctx] = None


def _dependency_hints(deps: Sequence[Dict[str, str]]) -> Tuple[List[str], List[str]]:
    """(warnings, winetricks hints) for PackageDependency entries."""
    warnings, hints = [], []
    vcrun = {"microsoft.vclibs.140.00.uwpdesktop": "vcrun2022", "microsoft.vclibs.120.00.uwpdesktop": "vcrun2013",
             "microsoft.vclibs.110.00.uwpdesktop": "vcrun2012"}
    for dep in deps:
        name = dep.get("name", "")
        low = name.casefold()
        if low in vcrun:
            hints.append(f"Needs the Microsoft Visual C++ runtime ({name}). If the app doesn't start, install it "
                         f"into this app's C:\\ drive with: winetricks {vcrun[low]}")
        elif (low.startswith(("microsoft.ui.xaml.", "microsoft.windowsappruntime.", "microsoft.net.native."))
              or low in ("microsoft.vclibs.140.00", "microsoft.vclibs.140.00.debug")):
            warnings.append(f"Depends on {name}, a Windows app runtime that Wine doesn't provide — expected to "
                            "fail under Wine.")
        elif name:
            warnings.append(f"Depends on another package ({name}"
                            + (f" {dep.get('min_version')}" if dep.get("min_version") else "")
                            + "), which Lindos can't install automatically; parts of the app may not work.")
    return warnings, hints


def _signature_state(zips: Sequence[_Zip]) -> Tuple[bool, bool, List[str]]:
    """(signed, store_signer_seen, warnings).  The signature is looked at, never verified."""
    signed = store = False
    warnings: List[str] = []
    for z in zips:
        zi = z.find_raw(_SIGNATURE)
        if zi is None:
            continue
        try:
            data = _read_member(z, zi, _MAX_P7X_BYTES, _SIGNATURE)
        except MsixError as exc:
            warnings.append(str(exc))
            continue
        if data[:4] != b"PKCX":
            warnings.append(f"The signature file in {z.label} is damaged.")
            continue
        signed = True
        if _STORE_OID_DER in data:
            store = True
    return signed, store, warnings


def _store_message(signals: Sequence[str]) -> str:
    if "xbox-gdk-game" in signals:
        return ("This looks like an Xbox / PC Game Pass game. Those games check their licence through the Xbox app, "
                "which Wine doesn't have, so it will most likely not start.")
    labels = {"store-signer": "signed by the Microsoft Store", "code-integrity-catalog": "has a Store code "
              "integrity catalog", "partner-center-publisher": "uses a Microsoft Partner Center publisher id"}
    which = ", ".join(labels.get(s, s) for s in signals)
    return (f"This package appears to come from the Microsoft Store ({which}). Store apps may check for a Store "
            "licence that Wine can't provide, so it might not start. The developer's regular download, a Linux "
            "alternative or the Windows VM (`lindos-vm`) are the reliable options.")


def _status_for(m: _Manifest, apps: Sequence[MsixApp]) -> Tuple[str, str]:
    if m.framework:
        return "unsupported", _REASON_FRAMEWORK
    if m.resource_package:
        return "unsupported", _REASON_RESOURCE
    if m.arch in _ARM_ARCHES:
        return "unsupported", _REASON_ARM.format(archs=m.arch)
    if not apps:
        return "unsupported", _REASON_NO_APPS
    win32 = [a for a in apps if a.app_class == "win32"]
    if len(win32) == len(apps):
        return "partial", _REASON_WIN32
    if win32:
        return "partial", (f"Packaged desktop app — usually works. {len(win32)} of {len(apps)} apps can be "
                           "installed; the others can't run under Wine.")
    classes = {a.app_class for a in apps}
    if classes == {"needs-host"}:
        return "unsupported", _REASON_NEEDS_HOST
    if "uwp" in classes:
        return "unsupported", _REASON_UWP
    return "unsupported", _REASON_UNKNOWN_APP


def _package_info(ctx: _Ctx, path: Path, kind: str, z: _Zip, m: _Manifest, *, selected: Optional[str],
                  warnings: List[str], sig_zips: Sequence[_Zip]) -> Tuple[MsixInfo, Optional[_Plan]]:
    payload = [n for n in z.names() if not _is_footprint(n.split("/"))]
    apps: List[MsixApp] = []
    for raw in m.apps:
        logo = ""
        for ref in raw.logo_refs + ([m.store_logo] if m.store_logo else []):
            logo = _resolve_logo(ref, payload)
            if logo:
                break
        apps.append(dataclasses.replace(raw.app, logo=logo))

    signed, store_signer, sig_warnings = _signature_state(sig_zips)
    signals: List[str] = []
    if store_signer:
        signals.append("store-signer")
    if any(zz.has_raw_ci(_CODE_INTEGRITY) for zz in sig_zips):
        signals.append("code-integrity-catalog")
    if _GUID_PUBLISHER_RE.match(m.publisher):
        signals.append("partner-center-publisher")
    folded = {n.casefold() for n in payload}
    if "microsoftgame.config" in folded or any(
            _exe_stem(a.executable).casefold() == "gamelaunchhelper" for a in apps):
        signals.append("xbox-gdk-game")

    status, reason = _status_for(m, apps)
    plan: Optional[_Plan] = None
    try:
        plan = _plan_entries(z, ctx.max_bytes, ctx.max_ratio)
    except MsixError as exc:
        status, reason = "unsupported", str(exc)

    unsigned_marker = bool(_UNSIGNED_RE.search(m.publisher))
    all_warnings = list(warnings) + list(m.warnings)
    for raw, app in zip(m.apps, apps):
        if app.app_class != "win32" and len(apps) > 1:
            all_warnings.append(f"'{app.display_name}' can't be installed: {raw.note}.")
    dep_warnings, dep_hints = _dependency_hints(m.dependencies)
    all_warnings += dep_warnings + dep_hints + sig_warnings
    if signals:
        all_warnings.append(_store_message(signals))
    if signed and unsigned_marker:
        all_warnings.append("The publisher is marked 'unsigned' but the package has a signature — unusual.")
    elif not signed and not unsigned_marker:
        all_warnings.append("This package has no signature and isn't marked as unsigned; Windows itself would "
                            "refuse it. Only continue if you trust where it came from.")

    pid = publisher_id(m.publisher)
    full, family = _full_names(m.name, m.version, m.arch, m.resource_id, pid)
    display = (m.display_name or next((a.display_name for a in apps if a.display_name), "")
               or _humanize(m.name) or m.name)
    info = MsixInfo(path=str(path), kind=kind, name=m.name, publisher=m.publisher,
                    publisher_display=m.publisher_display or _cn(m.publisher), version=m.version, arch=m.arch,
                    resource_id=m.resource_id, display_name=display, publisher_id=pid, package_full_name=full,
                    package_family_name=family, apps=apps, dependencies=list(m.dependencies),
                    framework=m.framework, resource_package=m.resource_package, signed=signed,
                    unsigned_marker=unsigned_marker, store_signals=signals, status=status, reason=reason,
                    selected_package=selected, warnings=all_warnings)
    return info, plan


def _arm_only_info(path: Path, kind: str, bundle: _Bundle, z: _Zip, archs: Sequence[str]) -> MsixInfo:
    pid = publisher_id(bundle.publisher)
    full, family = _full_names(bundle.name, bundle.version, "neutral", "~", pid)
    signed, _, sig_warnings = _signature_state([z])
    return MsixInfo(path=str(path), kind=kind, name=bundle.name, publisher=bundle.publisher,
                    publisher_display=_cn(bundle.publisher), version=bundle.version, arch="neutral",
                    resource_id="~", display_name=_humanize(bundle.name) or bundle.name, publisher_id=pid,
                    package_full_name=full, package_family_name=family, apps=[], dependencies=[],
                    framework=False, resource_package=False, signed=signed,
                    unsigned_marker=bool(_UNSIGNED_RE.search(bundle.publisher)), store_signals=[],
                    status="unsupported", reason=_REASON_ARM.format(archs=", ".join(archs)),
                    selected_package=None, warnings=list(bundle.warnings) + sig_warnings,
                    bundle_version=bundle.version)


def _pick_upload_inner(ctx: _Ctx, outer: _Zip, warnings: List[str]) -> Tuple[_Zip, str, str]:
    """One level of recursion: the bundle (preferred) or the best package inside an upload file."""
    opened: List[Tuple[str, _Zip, str]] = []
    for decoded, zi in _upload_candidates(outer.zf)[:_MAX_UPLOAD_CANDIDATES]:
        try:
            inner = _open_member(ctx, outer, zi, decoded)
            kind = _sniff(inner.zf)
        except MsixError as exc:
            warnings.append(f"Skipped {decoded}: {exc}")
            continue
        if kind not in ("package", "bundle"):
            warnings.append(f"Skipped {decoded}: it is not an app package or bundle (Lindos looks only one "
                            "level deep inside upload files).")
            continue
        opened.append((decoded, inner, kind))
    if not opened:
        raise MsixError("This upload file contains no usable app package or bundle.")
    bundles = [o for o in opened if o[2] == "bundle"]
    if bundles:
        if len(bundles) > 1:
            warnings.append(f"This upload file holds {len(bundles)} bundles; Lindos used {bundles[0][0]}.")
        return bundles[0][1], "bundle", bundles[0][0]
    best: Optional[Tuple[Tuple[int, Tuple[int, int, int, int]], str, _Zip]] = None
    for decoded, inner, _ in opened:
        try:
            m = _parse_manifest(_read_exact(inner, PACKAGE_MANIFEST, "AppxManifest.xml"))
        except MsixError as exc:
            warnings.append(f"Skipped {decoded}: {exc}")
            continue
        rank = (_ARCH_RANK.get(m.arch, 0), _version_tuple(m.version))
        if best is None or rank > best[0]:
            best = (rank, decoded, inner)
    if best is None:
        raise MsixError("This upload file contains no readable app package.")
    return best[2], "package", best[1]


def _resolve_zip(ctx: _Ctx, path: Path, outer_kind: str, outer: _Zip) -> _Resolved:
    warnings: List[str] = []
    chain: List[str] = []
    z, kind = outer, outer_kind
    if outer_kind == "upload":
        z, kind, label = _pick_upload_inner(ctx, outer, warnings)
        chain.append(label)
    if kind == "bundle":
        bundle = _parse_bundle(_read_exact(z, BUNDLE_MANIFEST, "AppxBundleManifest.xml"))
        chosen, archs = _select_bundle_package(bundle)
        if chosen is None:
            return _Resolved(info=_arm_only_info(path, outer_kind, bundle, z, archs), bundle=bundle, bundle_zip=z)
        inner = _open_bundle_inner(ctx, z, chosen)
        m = _parse_manifest(_read_exact(inner, PACKAGE_MANIFEST, "AppxManifest.xml"))
        _verify_inner_identity(bundle, chosen, m)
        chain.append(chosen.file_name)
        builds = sorted({p.arch for p in bundle.packages if p.type == "application" and not p.is_stub})
        warnings.append(f"This bundle (version {bundle.version}) has builds for {', '.join(builds)}; Lindos "
                        f"picked the {chosen.arch} build ({chosen.file_name}).")
        warnings.extend(bundle.warnings)
        info, plan = _package_info(ctx, path, outer_kind, inner, m, selected="/".join(chain), warnings=warnings,
                                   sig_zips=[z, inner])
        info.bundle_version = bundle.version
        return _Resolved(info=info, pkg=inner, manifest=m, bundle=bundle, bundle_zip=z, plan=plan)
    m = _parse_manifest(_read_exact(z, PACKAGE_MANIFEST, "AppxManifest.xml"))
    info, plan = _package_info(ctx, path, outer_kind, z, m, selected="/".join(chain) or None, warnings=warnings,
                               sig_zips=[z])
    return _Resolved(info=info, pkg=z, manifest=m, plan=plan)


@contextlib.contextmanager
def _resolve(path: Path, *, max_bytes: int, max_ratio: int, work_dir: Optional[Path] = None
             ) -> Iterator[_Resolved]:
    path = Path(path)
    if not path.is_file():
        raise MsixError(f"{path}: no such file.")
    if max_bytes <= 0 or max_ratio <= 0:
        raise MsixError("max_bytes and max_ratio must be positive.")
    kind = classify(path)
    with contextlib.ExitStack() as stack:
        ctx = _Ctx(stack, max_bytes, max_ratio, work_dir)
        if kind == "encrypted":
            res = _Resolved(info=_encrypted_info(path))
        elif kind == "msixvc":
            res = _Resolved(info=_msixvc_info(path))
        elif kind == "appinstaller":
            res = _Resolved(info=_appinstaller_info(path))
        elif kind in ("package", "bundle", "upload"):
            res = _resolve_zip(ctx, path, kind, _zip_from_file(ctx, path))
        else:
            raise MsixError(_unknown_reason(path))
        res.ctx = ctx
        yield res


def inspect(path: Path) -> MsixInfo:
    """Identity, apps and an honest status of an MSIX-family file.  Raises :class:`MsixError`."""
    with _resolve(Path(path), max_bytes=64 << 30, max_ratio=200) as res:
        return res.info


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #
class _CiDirs:
    """Case-insensitive, symlink-refusing folder walker below one base folder (a C:\\ drive)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._listing: Dict[str, Dict[str, str]] = {}

    def _names(self, parent: Path) -> Dict[str, str]:
        key = str(parent)
        listing = self._listing.get(key)
        if listing is None:
            listing = {}
            try:
                for entry in sorted(os.listdir(parent)):
                    listing.setdefault(entry.casefold(), entry)
            except OSError:
                pass
            self._listing[key] = listing
        return listing

    def child(self, parent: Path, name: str) -> Path:
        actual = self._names(parent).get(name.casefold())
        return parent / (actual if actual is not None else name)

    def remember(self, parent: Path, name: str) -> None:
        self._names(parent).setdefault(name.casefold(), name)

    def walk(self, parts: Sequence[str], *, create: bool) -> Optional[Path]:
        """Folder ``base/parts`` matched case-insensitively.  Refuses symbolic links and files on the way.
        With ``create=False`` returns None when a folder is missing."""
        cur = self.base
        for part in parts:
            nxt = self.child(cur, part)
            if os.path.islink(nxt):
                raise MsixError(f"Refusing to write through the symbolic link {nxt}.")
            if not os.path.lexists(nxt):
                if not create:
                    return None
                nxt.mkdir()
                self.remember(cur, part)
            elif not nxt.is_dir():
                raise MsixError(f"{nxt} is a file, but the app needs a folder there.")
            cur = nxt
        return cur

    def find_file(self, parts: Sequence[str]) -> Optional[Path]:
        if not parts:
            return None
        folder = self.walk(parts[:-1], create=False)
        if folder is None:
            return None
        cand = self.child(folder, parts[-1])
        return cand if cand.is_file() and not os.path.islink(cand) else None


def _prefix_is_win32(prefix: Path) -> bool:
    try:
        marker = json.loads((prefix / ".lindos.json").read_text(encoding="utf-8"))
        if isinstance(marker, dict) and str(marker.get("arch", "")).casefold() == "win32":
            return True
    except (OSError, ValueError):
        pass
    try:
        with open(prefix / "system.reg", "r", encoding="utf-8", errors="replace") as fh:
            return "#arch=win32" in fh.read(4096)
    except OSError:
        return False


def _vfs_plan(plan: _Plan, win32: bool) -> Tuple[List[Tuple[_Entry, Tuple[str, ...]]], List[str], List[str]]:
    """(entry -> target below drive_c, unmapped tokens, notes)."""
    table = _VFS_WIN32 if win32 else _VFS_WIN64
    out: List[Tuple[_Entry, Tuple[str, ...]]] = []
    unmapped: Set[str] = set()
    notes: List[str] = []
    seen: Set[str] = set()
    for e in plan.entries:
        if len(e.parts) < 3 or e.parts[0].casefold() != "vfs":
            continue
        token = e.parts[1].casefold()
        if token not in table:
            unmapped.add(e.parts[1])
            continue
        base = table[token]
        if base is None:
            unmapped.add(e.parts[1])
            continue
        target = base + e.parts[2:]
        key = _key(target)
        if key in seen:
            notes.append(f"{e.name} maps to the same place as another file; only the first was used.")
            continue
        seen.add(key)
        out.append((e, target))
    return out, sorted(unmapped), notes


def _extract(z: _Zip, plan: _Plan, staging: Path, on_progress: Optional[Callable[[int, int, str], Any]],
             max_bytes: int) -> None:
    done = 0
    root = staging.resolve()
    for e in plan.entries:
        target = staging.joinpath(*e.parts)
        if root not in target.resolve().parents:
            raise MsixError(f"Unsafe file path in the package: {e.name!r}. Lindos refuses to install it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with z.zf.open(e.zi) as src, open(target, "xb") as dst:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    done += len(chunk)
                    if written > e.zi.file_size or done > max_bytes:
                        raise MsixError(f"{e.name!r} is larger than the package says; refusing it.")
                    dst.write(chunk)
                    if on_progress is not None:
                        on_progress(done, plan.total, e.name)
        except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError) as exc:
            raise MsixError(f"{e.name!r} could not be unpacked ({exc}); the package is damaged.") from None
        if written != e.zi.file_size:
            raise MsixError(f"{e.name!r} is shorter than the package says; the package is damaged.")
        if on_progress is not None and e.zi.file_size == 0:
            on_progress(done, plan.total, e.name)


def _swap_into_place(staging: Path, install_dir: Path) -> None:
    old: Optional[Path] = None
    if os.path.lexists(install_dir):
        if os.path.islink(install_dir) or not install_dir.is_dir():
            os.unlink(install_dir)
        else:
            old = install_dir.with_name(f".lindos-msix-old-{uuid.uuid4().hex[:12]}")
            os.replace(install_dir, old)
    try:
        os.replace(staging, install_dir)
    except OSError:
        if old is not None:
            os.replace(old, install_dir)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _materialize_vfs(install_dir: Path, walker: _CiDirs, vfs: Sequence[Tuple[_Entry, Tuple[str, ...]]],
                     notes: List[str]) -> Dict[str, Path]:
    """Hard-link (or copy) VFS files to their Windows locations inside this app's own C:\\ drive."""
    placed: Dict[str, Path] = {}
    replaced = 0
    for e, target in vfs:
        src = install_dir.joinpath(*e.parts)
        try:
            parent = walker.walk(target[:-1], create=True)
            assert parent is not None
            dst = walker.child(parent, target[-1])
            if os.path.islink(dst):
                os.unlink(dst)
            elif dst.is_dir():
                notes.append(f"{e.name} was not placed: a folder with that name already exists.")
                continue
            elif dst.exists():
                os.unlink(dst)
                replaced += 1
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
            walker.remember(parent, dst.name)
        except OSError as exc:
            raise MsixError(f"Could not place {e.name} in the C:\\ drive ({exc.strerror or exc}).") from None
        placed[_key(e.parts)] = dst
    if replaced:
        notes.append(f"{replaced} existing file(s) in this app's C:\\ drive were replaced by the app's own copies "
                     "(this C:\\ drive belongs to this app only).")
    return placed


def _reg_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _reg_value_line(name: str, vtype: int, data: bytes, rewrite: Callable[[str], str]) -> str:
    key = "@" if name == "" else '"' + _reg_escape(name) + '"'
    if vtype == 1:  # REG_SZ
        text = rewrite(data.decode("utf-16-le", errors="replace").split("\x00", 1)[0])
        if "\n" not in text and "\r" not in text:
            return f'{key}="{_reg_escape(text)}"'
        data = (text + "\x00").encode("utf-16-le")
    elif vtype == 4 and len(data) == 4:  # REG_DWORD
        return f"{key}=dword:{struct.unpack('<I', data)[0]:08x}"
    elif vtype in (2, 7):  # REG_EXPAND_SZ, REG_MULTI_SZ
        data = rewrite(data.decode("utf-16-le", errors="replace")).encode("utf-16-le")
    prefix = "hex" if vtype == 3 else f"hex({vtype:x})"
    return f"{key}={prefix}:" + ",".join(f"{b:02x}" for b in data)


def _hive_to_reg(hivex_mod: Any, hive: Path, role: str, rewrite: Callable[[str], str]) -> Tuple[str, int]:
    """Convert one package hive into .reg text ("Windows Registry Editor Version 5.00")."""
    h = hivex_mod.Hivex(str(hive))
    root = h.root()

    def child(node: Any, name: str) -> Any:
        for c in h.node_children(node):
            if str(h.node_name(c)).casefold() == name.casefold():
                return c
        return None

    roots: List[Tuple[Any, str]] = []
    registry = child(root, "REGISTRY")
    if role == "machine":
        if registry is not None:
            machine = child(registry, "MACHINE")
            if machine is not None:
                roots.append((machine, "HKEY_LOCAL_MACHINE"))
            users = child(registry, "USER")
            if users is not None:
                roots.extend((c, "HKEY_CURRENT_USER") for c in h.node_children(users))
        else:  # Microsoft: Registry.dat is the logical equivalent of HKLM\Software
            roots.append((root, r"HKEY_LOCAL_MACHINE\Software"))
    elif role == "user":
        users = child(registry, "USER") if registry is not None else None
        if users is not None:
            roots.extend((c, "HKEY_CURRENT_USER") for c in h.node_children(users))
        else:
            roots.append((root, "HKEY_CURRENT_USER"))
    else:
        roots.append((root, r"HKEY_CURRENT_USER\Software\Classes"))

    lines = ["Windows Registry Editor Version 5.00", ""]
    count = 0
    nodes = 0
    for top, top_path in roots:
        stack: List[Tuple[Any, str, int]] = [(top, top_path, 0)]
        while stack:
            node, key_path, depth = stack.pop()
            nodes += 1
            if nodes > _REG_MAX_NODES or depth > _REG_MAX_DEPTH:
                raise MsixError("the registry file is unreasonably large or deep")
            values = list(h.node_values(node))
            if depth > 0 or ("\\" in top_path and values):
                lines.append(f"[{key_path}]")
                for v in values:
                    vtype, data = h.value_value(v)
                    lines.append(_reg_value_line(str(h.value_key(v)), int(vtype), bytes(data), rewrite))
                    count += 1
                lines.append("")
                count += 1
            kids = list(h.node_children(node))
            for c in reversed(kids):
                stack.append((c, f"{key_path}\\{h.node_name(c)}", depth + 1))
    return "\r\n".join(lines) + "\r\n", count


def _import_registry(install_dir: Path, plan: _Plan, prefix: Path, info: MsixInfo, win32: bool,
                     notes: List[str]) -> List[Path]:
    hives = [(e, _HIVE_FILES[e.parts[0].casefold()]) for e in plan.entries
             if len(e.parts) == 1 and e.parts[0].casefold() in _HIVE_FILES]
    if not hives:
        return []
    try:
        import hivex  # type: ignore[import-not-found]  # python3-hivex, optional
    except ImportError:
        notes.append("This app includes registry settings (Registry.dat) that were not imported because "
                     "python3-hivex is not installed. Install it with 'sudo apt install python3-hivex' and install "
                     "the app again if it misbehaves.")
        return []
    root_win = package_root_windows(info)
    tokens = dict(_REG_TOKENS)
    if win32:
        tokens.update({"[{programfilesx86}]": r"C:\Program Files",
                       "[{programfilescommonx86}]": r"C:\Program Files\Common Files",
                       "[{systemx86}]": r"C:\windows\system32"})

    def rewrite(text: str) -> str:
        def sub(m: "re.Match[str]") -> str:
            token = m.group(0).casefold()
            if token not in tokens:
                return m.group(0)
            return tokens[token] or root_win
        return _REG_TOKEN_RE.sub(sub, text)

    out_dir = prefix / ".lindos-msix" / info.package_full_name
    files: List[Path] = []
    for e, role in hives:
        try:
            text, count = _hive_to_reg(hivex, install_dir.joinpath(*e.parts), role, rewrite)
        except Exception as exc:  # noqa: BLE001 - optional import must never break an install
            notes.append(f"The registry settings in {e.name} could not be read ({exc}); they were not imported.")
            continue
        if count == 0:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / (Path(e.name).stem + ".reg")
        target.write_bytes(("\ufeff" + text).encode("utf-16-le"))
        files.append(target)
    if files:
        notes.append("The app's registry settings were converted to " + ", ".join(p.name for p in files)
                     + "; they belong to this app's C:\\ drive only.")
    return files


def _norm_rel(path: str) -> str:
    """Package-relative path with '/' separators ("" when it tries to leave the package)."""
    parts = [p for p in re.split(r"[\\/]+", (path or "").strip()) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def _psf_resolve(z: _Zip, files: Dict[str, _Entry], app: MsixApp, notes: List[str]) -> Optional[MsixApp]:
    """PsfLauncher: take the real program, arguments and folder from config.json (scripts are skipped)."""
    cfg = files.get("config.json")
    if cfg is None:
        notes.append(f"'{app.display_name}' starts through the Package Support Framework launcher, but its "
                     "config.json is missing; the app was not added.")
        return None
    try:
        data = json.loads(_read_member(z, cfg.zi, _MAX_JSON_BYTES, "config.json").decode("utf-8-sig"))
    except (MsixError, ValueError, UnicodeDecodeError) as exc:
        notes.append(f"'{app.display_name}': config.json can't be read ({exc}); the app was not added.")
        return None
    entries = data.get("applications") if isinstance(data, dict) else None
    match = None
    if isinstance(entries, list):
        match = next((a for a in entries if isinstance(a, dict)
                      and str(a.get("id", "")).casefold() == app.id.casefold()), None)
    executable = str(match.get("executable") or "") if isinstance(match, dict) else ""
    if not executable:
        notes.append(f"'{app.display_name}': config.json doesn't name the real program; the app was not added.")
        return None
    if any(match.get(k) for k in ("startScript", "endScript")):  # type: ignore[union-attr]
        notes.append(f"'{app.display_name}': the package's PowerShell start/end scripts were skipped (Lindos never "
                     "runs them automatically).")
    args = match.get("arguments")  # type: ignore[union-attr]
    work_dir = match.get("workingDirectory")  # type: ignore[union-attr]
    executable = re.sub(r"^%MsixPackageRoot%[\\/]*", "", executable, flags=re.IGNORECASE)
    return dataclasses.replace(app, executable=executable.replace("/", "\\"),
                               parameters=str(args) if isinstance(args, (str, int, float)) else "",
                               working_dir=work_dir if isinstance(work_dir, str) else "")


def _scale_of(resource_id: str) -> int:
    m = re.search(r"scale-(\d+)", resource_id or "", re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _logo_from_resources(ctx: _Ctx, res: _Resolved, refs: Sequence[str], windowsapps: Path) -> Optional[Path]:
    """Bundles often keep the scaled logos in resource packages (Windows installs those next to the app)."""
    if res.bundle is None or res.bundle_zip is None:
        return None
    info = res.info
    rps = [p for p in res.bundle.packages if p.type == "resource" and not p.is_stub]
    for rp in sorted(rps, key=lambda p: -_scale_of(p.resource_id)):
        try:
            hit = res.bundle_zip.lookup(rp.file_name)
            if hit is None or hit[1].compress_type != zipfile.ZIP_STORED:
                continue
            inner = _open_member(ctx, res.bundle_zip, hit[1], rp.file_name)
            names = inner.names()
            for ref in refs:
                rel = _resolve_logo(ref, names)
                if not rel:
                    continue
                found = inner.lookup(rel)
                if found is None or found[1].file_size > _MAX_LOGO_BYTES:
                    continue
                _, parts, _ = _split_member(found[1].orig_filename)
                folder = f"{info.name}_{rp.version}_{rp.arch}_{rp.resource_id}_{info.publisher_id}"
                _check_segment(folder, folder)
                dest_dir = windowsapps / folder
                if os.path.islink(dest_dir):
                    continue
                target = dest_dir.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = _read_member(inner, found[1], _MAX_LOGO_BYTES, found[0])
                target.write_bytes(data)
                return target
        except (MsixError, OSError) as exc:
            log.debug("logo from %s failed: %s", rp.file_name, exc)
    return None


def install(path: Path, prefix: Path, *, on_progress: Optional[Callable[[int, int, str], Any]] = None,
            max_bytes: int = 64 << 30, max_ratio: int = 200) -> MsixInstall:
    """Extract the desktop app(s) of an MSIX package/bundle/upload into ``prefix`` (the app's own C:\\ drive).

    ``on_progress(done_bytes, total_bytes, current_name)`` is called while unpacking; raising from it
    aborts the install (nothing is left behind).  Raises :class:`MsixError` for anything that cannot or
    must not be installed (UWP-only, encrypted, damaged, unsafe, too big, no disk space...).  The caller
    owns the prefix (create it with the package family name as slug, see :func:`prefix_slug`), the
    Start-Menu entries (:func:`launch_info`), APPS_DB and importing :attr:`MsixInstall.reg_files`.
    """
    path, prefix = Path(path), Path(prefix)
    if prefix.name.casefold() in _SHARED_PREFIX_NAMES:
        raise MsixError("MSIX apps are installed into their own C:\\ drive, never into the shared one.")
    drive_c = prefix / "drive_c"
    drive_c.mkdir(parents=True, exist_ok=True)
    walker = _CiDirs(drive_c)
    windowsapps = walker.walk(("Program Files", "WindowsApps"), create=True)
    assert windowsapps is not None
    win32 = _prefix_is_win32(prefix)
    with _resolve(path, max_bytes=max_bytes, max_ratio=max_ratio, work_dir=windowsapps) as res:
        info = res.info
        if info.kind in ("encrypted", "msixvc"):
            raise MsixError(info.reason)
        if info.kind == "appinstaller":
            raise MsixError("An App Installer file only points to a download. Open it with lindos-run: it asks "
                            "before downloading (https only) and then installs the package.")
        if info.status == "unsupported" or res.pkg is None or res.plan is None:
            raise MsixError(info.reason)
        plan, z = res.plan, res.pkg
        files = plan.by_key()
        notes: List[str] = []
        refs_by_id = {raw.app.id: raw.logo_refs + ([res.manifest.store_logo] if res.manifest and
                                                   res.manifest.store_logo else [])
                      for raw in (res.manifest.apps if res.manifest else [])}

        # 1. decide the programs before touching the disk
        chosen: List[Tuple[MsixApp, Optional[_Entry], str]] = []
        for app in info.apps:
            if app.app_class != "win32":
                notes.append(f"'{app.display_name}' was not installed: Wine can't run this kind of app.")
                continue
            real = app
            if _PSF_RE.match(_exe_stem(app.executable) + ".exe"):
                resolved = _psf_resolve(z, files, app, notes)
                if resolved is None:
                    continue
                real = resolved
            rel = _norm_rel(real.executable)
            if re.match(r"^[A-Za-z]:", real.executable.strip()):
                chosen.append((real, None, real.executable.strip()))
                continue
            entry = files.get(rel.casefold()) if rel else None
            if entry is None:
                notes.append(f"'{app.display_name}': its program {real.executable!r} is missing from the package; "
                             "the app was not added.")
                continue
            chosen.append((real, entry, ""))
        if not chosen:
            raise MsixError("None of the apps in this package could be found, so nothing was installed. "
                            + " ".join(notes))

        vfs, unmapped, vfs_notes = _vfs_plan(plan, win32)
        for _, target in vfs:  # refuse symbolic links / files in the way before writing anything
            walker.walk(target[:-1], create=False)
        _check_free(windowsapps, plan.total)

        # 2. unpack into a fresh folder, then swap it into place
        install_dir = windowsapps / info.package_full_name
        staging = Path(tempfile.mkdtemp(prefix=".lindos-msix-", dir=str(windowsapps)))
        try:
            _extract(z, plan, staging, on_progress, max_bytes)
            _swap_into_place(staging, install_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # 3. VFS, registry, programs, logos
        notes.extend(vfs_notes)
        placed = _materialize_vfs(install_dir, walker, vfs, notes)
        if unmapped:
            notes.append("Files for these Windows folders stay inside the app's own folder: "
                         + ", ".join(unmapped) + ".")
        reg_files = _import_registry(install_dir, plan, prefix, info, win32, notes)

        apps: List[Tuple[MsixApp, Path, Optional[Path]]] = []
        for real, entry, absolute in chosen:
            if entry is None:
                win_parts = [p for p in re.split(r"[\\/]+", absolute[2:]) if p]
                exe = (walker.find_file(win_parts) if absolute[:1].casefold() == "c"
                       and not any(p in (".", "..") for p in win_parts) else None)
                if exe is None:
                    notes.append(f"'{real.display_name}': its program {absolute!r} doesn't exist; the app was "
                                 "not added.")
                    continue
            else:
                exe = placed.get(_key(entry.parts)) or install_dir.joinpath(*entry.parts)
            logo: Optional[Path] = None
            if real.logo:
                found = files.get(real.logo.casefold())
                if found is not None:
                    logo = install_dir.joinpath(*found.parts)
            if logo is None and res.ctx is not None:
                logo = _logo_from_resources(res.ctx, res, refs_by_id.get(real.id, []), windowsapps)
                if logo is not None:
                    notes.append(f"'{real.display_name}': the icon came from the bundle's resource package.")
            apps.append((real, exe, logo))
        _, hints = _dependency_hints(info.dependencies)
        notes.extend(hints)
        if info.store_signals:
            notes.append(_store_message(info.store_signals))
        return MsixInstall(info=info, install_dir=install_dir, apps=apps, notes=notes, reg_files=reg_files)


# --------------------------------------------------------------------------- #
# helpers for lindos-run / the GUI (additive to the SPEC API)
# --------------------------------------------------------------------------- #
def trust_label(info: MsixInfo) -> str:
    """One-line trust text for the UI.  Signatures are never verified by Lindos."""
    if info.kind == "encrypted":
        return "Encrypted Microsoft Store package"
    if info.signed:
        return "Signed (not verified)"
    if info.unsigned_marker:
        return f"Unsigned (publisher marked unsigned: {UNSIGNED_MARKER})"
    return "Unsigned (no signature)"


def can_try_anyway(info: MsixInfo) -> bool:
    """"Try anyway" is offered only for non-encrypted packages with at least one desktop (win32) app."""
    return (info.kind in ("package", "bundle", "upload") and info.status != "unsupported"
            and any(a.app_class == "win32" for a in info.apps))


def prefix_slug(info: MsixInfo) -> str:
    """Per-app C:\\ drive slug = package family name (SPEC-WINDOWS §28.4)."""
    return info.package_family_name


def aumid(info: MsixInfo, app: MsixApp) -> str:
    """Application User Model ID: ``<PackageFamilyName>!<AppId>``."""
    return f"{info.package_family_name}!{app.id}"


def package_root_windows(info: MsixInfo) -> str:
    """Windows path of the installed package folder inside its C:\\ drive."""
    return "C:\\Program Files\\WindowsApps\\" + info.package_full_name


def expand_macros(text: str, package_root: str) -> str:
    """Expand ``$(...)`` macros of uap11:Parameters / CurrentDirectoryPath (unknown ones stay as written)."""

    def sub(m: "re.Match[str]") -> str:
        if m.group(0) == "$$":
            return "$"
        inner = m.group(1).strip()
        low = inner.casefold()
        if low.startswith("env:"):
            var = inner[4:].strip()
            value = _ENV_DEFAULTS.get(var.casefold())
            return value if value is not None else f"%{var}%"
        if low in _PACKAGE_PATH_MACROS:
            return package_root
        if low == "system.path":
            return r"C:\windows\system32"
        if low == "windows.path":
            return r"C:\windows"
        return m.group(0)

    return _MACRO_RE.sub(sub, text or "")


def split_command_line(text: str) -> List[str]:
    """Split a Windows command line into argv (CommandLineToArgvW rules); tokens carry no quotes."""
    args: List[str] = []
    cur: List[str] = []
    have = in_quotes = False
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        if ch == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            count = j - i
            if j < n and text[j] == '"':
                cur.append("\\" * (count // 2))
                have = True
                if count % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
                continue
            cur.append("\\" * count)
            have = True
            i = j
            continue
        if ch == '"':
            if in_quotes and i + 1 < n and text[i + 1] == '"':
                cur.append('"')
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
            have = True
            continue
        if ch in " \t\r\n" and not in_quotes:
            if have:
                args.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(ch)
        have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def _windows_dir_in_prefix(win_path: str, install_dir: Path) -> Optional[Path]:
    """Map a Windows folder (absolute C:\\... or package-relative) into the prefix; None if absent."""
    text = (win_path or "").strip()
    if re.match(r"^[A-Za-z]:", text):
        if text[:1].casefold() != "c":
            return None
        parts = [p for p in re.split(r"[\\/]+", text[2:]) if p]
        base = install_dir.parents[2]  # <prefix>/drive_c
    else:
        parts = [p for p in re.split(r"[\\/]+", text) if p and p != "."]
        base = install_dir
    if any(p == ".." for p in parts):
        return None
    folder = _CiDirs(base).walk(parts, create=False)
    return folder if folder is not None and folder.is_dir() else None


def launch_info(inst: MsixInstall, index: int = 0) -> Dict[str, object]:
    """Everything a launcher/.desktop entry needs for ``inst.apps[index]`` (macros expanded, args split)."""
    app, exe, logo = inst.apps[index]
    info = inst.info
    root = package_root_windows(info)
    cwd = exe.parent
    if app.working_dir:
        folder = _windows_dir_in_prefix(expand_macros(app.working_dir, root), inst.install_dir)
        if folder is not None:
            cwd = folder
    return {
        "app_id": app.id,
        "name": app.display_name,
        "exe": str(exe),
        "args": split_command_line(expand_macros(app.parameters, root)) if app.parameters else [],
        "cwd": str(cwd),
        "icon": str(logo) if logo else "",
        "console": app.console,
        "list_entry": app.list_entry,
        "pfn": info.package_family_name,
        "aumid": aumid(info, app),
        "source": "msix",
    }


def describe(info: MsixInfo) -> str:
    """Human-readable multi-line summary (used by the CLI and dialogs)."""
    lines = [f"{info.display_name or info.name or Path(info.path).name}"]
    if info.publisher_display or info.publisher:
        lines.append(f"  Publisher: {info.publisher_display or info.publisher}")
    if info.version:
        lines.append(f"  Version:   {info.version} ({info.arch or 'unknown'})")
    lines.append(f"  Kind:      {info.kind}" + (f" -> {info.selected_package}" if info.selected_package else ""))
    lines.append(f"  Trust:     {trust_label(info)}")
    lines.append(f"  Status:    {info.status} — {info.reason}")
    for app in info.apps:
        lines.append(f"  App:       {app.display_name} [{app.app_class}] {app.executable}")
    for warning in info.warnings:
        lines.append(f"  Note:      {warning}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m lindos_compat.msix classify|inspect|appinstaller FILE [--json]`` (read-only)."""
    parser = argparse.ArgumentParser(
        prog="python3 -m lindos_compat.msix",
        description="Look inside MSIX/APPX packages, bundles and App Installer files. Nothing is installed "
                    "or downloaded.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("classify", "say what kind of file it is"), ("inspect", "show identity, apps and status"),
                       ("appinstaller", "show what an .appinstaller file points to")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("file")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    path = Path(args.file)
    try:
        if args.cmd == "classify":
            kind = classify(path)
            print(json.dumps({"path": str(path), "kind": kind}) if args.json else kind)
            return 0
        if args.cmd == "appinstaller":
            ai = parse_appinstaller(path)
            refusal = appinstaller_refusal(ai)
            if args.json:
                print(json.dumps(dict(ai, refusal=refusal), indent=2))
            else:
                print(f"{ai['name']} {ai['version']} ({ai['kind']}) from {ai['host'] or ai['uri']}")
                print(f"  Publisher: {ai['publisher']}")
                if refusal:
                    print(f"  {refusal}")
            return EXIT_UNSUPPORTED if refusal else 0
        info = inspect(path)
    except MsixError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info.as_dict(), indent=2) if args.json else describe(info))
    return EXIT_UNSUPPORTED if info.status == "unsupported" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
