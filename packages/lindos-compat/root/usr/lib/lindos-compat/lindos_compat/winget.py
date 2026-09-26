"""winget on Lindos: find, inspect and install programs from Microsoft's winget catalogue.

SPEC-WINDOWS §28.10.  Lindos does not run Microsoft's winget client; it reads the same public,
hash-chained catalogue and runs the publisher's installer through ``lindos-run``:

1. ``source2.msix`` from ``cdn.winget.microsoft.com`` (HTTPS, ETag-cached, refreshed at most every
   15 minutes) holds ``Public/index.db`` -- a SQLite index with one row per package, whose ``hash``
   column is the SHA-256 of that package's version list;
2. ``packages/<Id>/<hash[:8]>/versionData.mszyml`` (Windows Compression API, MSZIP) -- must hash
   to ``packages.hash``; it lists every version with the SHA-256 (``s256H``) of its manifest;
3. the merged manifest ``cache/<rP>`` -- must hash to ``s256H``; it names each installer's URL and
   ``InstallerSha256``;
4. the installer itself -- downloaded over HTTPS only and **never run unless its SHA-256 matches**.
   There is no override.

When the CDN layout is not what this code understands (index schema != 2.0, a missing version
list, an undecodable container), the documented GitHub layout of ``microsoft/winget-pkgs`` is used
instead: one Git Trees API call lists the versions (and each file's git blob SHA-1, which is
checked), the files come from ``raw.githubusercontent.com``.  GitHub's anonymous limit (60
requests per hour) is reported honestly.

Installer choice deliberately differs from winget (documented in docs/WINGET.md): Wine cannot
deploy MSIX, and inside a C:\\ drive "machine" scope still means "this Linux user", so Lindos ranks
architecture (x64 > x86 > neutral) -> scope (user > machine) -> locale (system, then en-US) -> type
(msi/wix > inno > nullsoft > burn > exe > zip+portable), and uses msix/appx only when nothing
else exists (``lindos-run`` hands those to the MSIX unpacker).

Refused with an explanation (never "tried anyway"): Microsoft Store ids (9N..., XP...), ``msstore``,
``pwa`` and ``font`` installers, installers for ARM only, and plain ``http://`` downloads.

Everything that touches the network or runs a program is injectable (``fetch``, ``fetch_stream``,
``run_lindos``), so the tests never use the network, Wine or root.
"""

from __future__ import annotations

import copy
import functools
import getpass
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import CoreMissing, __version__, core, expand_user_path, get_logger, path_const, user_home
from . import wingetyaml

__all__ = [
    "WingetError",
    "WingetUnsupported",
    "WingetNotFound",
    "WingetHashError",
    "WingetLayoutError",
    "Response",
    "INDEX_URL",
    "CDN_BASE",
    "GITHUB_TREES_URL",
    "GITHUB_RAW_BASE",
    "USER_AGENT",
    "DEFAULT_SWITCHES",
    "TYPE_ORDER",
    "default_cache_dir",
    "default_fetch",
    "default_fetch_stream",
    "decode_mszip",
    "load_index",
    "search",
    "canonical_id",
    "list_versions",
    "load_manifest",
    "manifest_dir",
    "effective_installers",
    "select_installer",
    "installer_switches",
    "installer_argv",
    "split_command_line",
    "download",
    "install",
    "format_plan",
    "installer_notes",
    "list_installed",
    "index_info",
    "compare_versions",
    "exit_code_outcome",
    "system_locale",
    "is_store_id",
]

log = get_logger("lindos-compat.winget")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

INDEX_URL = "https://cdn.winget.microsoft.com/cache/source2.msix"
CDN_BASE = "https://cdn.winget.microsoft.com/cache/"
GITHUB_TREES_URL = "https://api.github.com/repos/microsoft/winget-pkgs/git/trees/"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/microsoft/winget-pkgs/master/"
INDEX_MEMBER = "Public/index.db"
INDEX_SCHEMA = ("2", "0")
#: An honest client identification (no browser/winget impersonation).
USER_AGENT = f"lindos-compat/{__version__} winget-catalogue-reader (+https://lindos.dev)"

MSZIP_MAGIC = b"\x0a\x51\xe5\xc0"
MSZIP_ALGORITHM = 2            # COMPRESS_ALGORITHM_MSZIP
MSZIP_BLOCK = 32768            # MSZIP blocks hold at most 32 KiB of output

MAX_FETCH = 64 << 20           # any metadata response
MAX_INDEX_MSIX = 64 << 20
MAX_INDEX_DB = 512 << 20
MAX_VERSION_DATA = 16 << 20
MAX_MANIFEST = 8 << 20
MAX_INSTALLER = 16 << 30
MAX_EXTRACT_BYTES = 16 << 30
MAX_EXTRACT_FILES = 200000
MAX_EXTRACT_RATIO = 200
MAX_DEPENDENCY_DEPTH = 4

#: Windows version a new Wine prefix reports (Windows 10 22H2).
WINE_WINDOWS_VERSION = "10.0.19045"

_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_STORE_ID_RE = re.compile(r"^(9[0-9A-Z]{11}|XP[0-9A-Z]{12})$", re.IGNORECASE)
_ID_SEGMENT_RE = re.compile(r'^[^.\s\\/:*?"<>|\x00-\x1f]{1,32}$')

#: Default InstallerSwitches by *effective* installer type (winget GetDefaultKnownSwitches).
DEFAULT_SWITCHES: Dict[str, Dict[str, str]] = {
    "msi": {"Silent": "/quiet /norestart", "SilentWithProgress": "/passive /norestart",
            "Log": '/log "<LOGPATH>"', "InstallLocation": 'TARGETDIR="<INSTALLPATH>"'},
    "nullsoft": {"Silent": "/S", "SilentWithProgress": "/S", "InstallLocation": "/D=<INSTALLPATH>"},
    "inno": {"Silent": "/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART",
             "SilentWithProgress": "/SP- /SILENT /SUPPRESSMSGBOXES /NORESTART",
             "Log": '/LOG="<LOGPATH>"', "InstallLocation": '/DIR="<INSTALLPATH>"'},
}
DEFAULT_SWITCHES["wix"] = dict(DEFAULT_SWITCHES["msi"])
DEFAULT_SWITCHES["burn"] = dict(DEFAULT_SWITCHES["msi"])

#: Default expected return codes by effective type (winget GetDefaultKnownReturnCodes).
DEFAULT_RETURN_CODES: Dict[str, Dict[int, str]] = {
    "msi": {
        1618: "installInProgress", 112: "diskFull", 1601: "contactSupport", 3010: "rebootRequiredToFinish",
        1641: "rebootInitiated", 1602: "cancelledByUser", 1638: "alreadyInstalled", 1654: "systemNotSupported",
        1625: "blockedByPolicy", 1644: "blockedByPolicy", 1643: "blockedByPolicy", 1648: "blockedByPolicy",
        1640: "blockedByPolicy", 87: "invalidParameter", 1628: "invalidParameter", 1639: "invalidParameter",
        1650: "invalidParameter", 1623: "systemNotSupported", 1633: "systemNotSupported",
    },
    "inno": {2: "cancelledByUser", 5: "cancelledByUser", 8: "rebootRequiredForInstall"},
}
DEFAULT_RETURN_CODES["wix"] = DEFAULT_RETURN_CODES["msi"]
DEFAULT_RETURN_CODES["burn"] = DEFAULT_RETURN_CODES["msi"]

#: ReturnResponse -> (outcome, plain-language message)
RETURN_RESPONSES: Dict[str, Tuple[str, str]] = {
    "packageInUse": ("failed", "a program from this package is still running - close it and try again"),
    "packageInUseByApplication": ("failed", "a program from this package is still running - close it and try again"),
    "installInProgress": ("busy", "another installation is still running in this C:\\ drive - wait for it to finish "
                                  "and try again"),
    "fileInUse": ("failed", "a file the installer must replace is in use - close the program and try again"),
    "missingDependency": ("failed", "something this program needs is missing (see the dependencies listed above)"),
    "diskFull": ("failed", "the disk is full"),
    "insufficientMemory": ("failed", "there is not enough memory"),
    "invalidParameter": ("failed", "the installer did not accept its options (the winget manifest may not match "
                                   "this installer build)"),
    "noNetwork": ("failed", "the installer needs an internet connection"),
    "contactSupport": ("failed", "the installer failed; the publisher asks you to contact their support"),
    "rebootRequiredToFinish": ("installed-restart", "installed - it asks for a restart to finish. On Lindos you do not "
                                                    "need to restart the PC: close every program of this C:\\ drive and "
                                                    "start it again"),
    "rebootInitiated": ("installed-restart", "installed - the installer asked Windows to restart; nothing restarts "
                                             "on Lindos, just close and reopen the program"),
    "rebootRequiredForInstall": ("failed", "the installer wants a restart before it can install - close the programs "
                                           "of this C:\\ drive and try again"),
    "cancelledByUser": ("cancelled", "the installation was cancelled"),
    "alreadyInstalled": ("already-installed", "this version (or a newer one) is already installed in this C:\\ drive"),
    "downgrade": ("failed", "a newer version is already installed in this C:\\ drive"),
    "blockedByPolicy": ("failed", "the installer refused to run because of a policy setting"),
    "systemNotSupported": ("failed", "the installer says this system is not supported (it may need a newer Windows "
                                     "version than Wine reports - try 'Windows 11' in winecfg)"),
    "custom": ("failed", "the installer reported its own error code; see the publisher's help pages"),
}

#: Lindos installer-type preference (lower is better); msix/appx are a separate, last tier.
TYPE_ORDER: Dict[str, int] = {"msi": 0, "wix": 0, "inno": 1, "nullsoft": 2, "burn": 3, "exe": 4, "portable": 5,
                              "msix": 6, "appx": 6}
_ARP_TYPES = frozenset({"exe", "inno", "msi", "nullsoft", "wix", "burn", "portable"})
_MSIX_TYPES = frozenset({"msix", "appx"})
_RUNNABLE_TYPES = frozenset({"msi", "wix", "inno", "nullsoft", "burn", "exe", "portable", "msix", "appx"})
_EXT_BY_TYPE = {"exe": ".exe", "burn": ".exe", "inno": ".exe", "nullsoft": ".exe", "portable": ".exe",
                "msi": ".msi", "wix": ".msi", "msix": ".msix", "appx": ".msix", "zip": ".zip"}
_KNOWN_EXTS = (".exe", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle", ".zip")

#: Installer-level fields a manifest may also set at its root (winget root -> installer defaults).
ROOT_INSTALLER_FIELDS = (
    "InstallerLocale", "Platform", "MinimumOSVersion", "InstallerType", "NestedInstallerType",
    "NestedInstallerFiles", "Scope", "InstallModes", "InstallerSwitches", "InstallerSuccessCodes",
    "ExpectedReturnCodes", "UpgradeBehavior", "Commands", "Protocols", "FileExtensions", "Dependencies",
    "PackageFamilyName", "ProductCode", "Capabilities", "RestrictedCapabilities", "Markets",
    "InstallerAbortsTerminal", "ReleaseDate", "InstallLocationRequired", "RequireExplicitUpgrade",
    "DisplayInstallWarnings", "UnsupportedOSArchitectures", "UnsupportedArguments", "AppsAndFeaturesEntries",
    "ElevationRequirement", "InstallationMetadata", "DownloadCommandProhibited", "RepairBehavior",
    "ArchiveBinariesDependOnPath", "Authentication", "DesiredStateConfiguration",
)
_CONDITIONAL_ROOT_FIELDS = ("NestedInstallerType", "NestedInstallerFiles", "ProductCode", "PackageFamilyName",
                            "AppsAndFeaturesEntries")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class WingetError(Exception):
    """A user-facing failure; the message is written for the person at the keyboard."""

    exit_code = 1


class WingetUnsupported(WingetError):
    """Refused on purpose and explained (Store ids, PWAs, fonts, ARM-only, plain http)."""

    exit_code = 3


class WingetNotFound(WingetError):
    """No such package id or version."""


class WingetHashError(WingetError):
    """A SHA-256 (or git blob SHA-1) in the chain did not match - never used, never run."""


class WingetLayoutError(WingetError):
    """Microsoft's CDN layout is not what this code understands (-> GitHub fallback)."""


# ---------------------------------------------------------------------------
# network (injectable)
# ---------------------------------------------------------------------------


@dataclass
class Response:
    """What a ``fetch`` callable returns (a plain ``bytes`` body is accepted too: status 200)."""

    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""


Fetch = Callable[[str, Dict[str, str]], Union[Response, bytes]]


def _scheme(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or url
    except ValueError:
        return url


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never to anything that is not HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if _scheme(newurl) != "https":
            raise WingetError(f"The server tried to redirect the download to a non-HTTPS address ({newurl}). "
                              "Lindos only downloads over HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HttpsOnlyRedirect())


def _offline_error(url: str, exc: BaseException) -> WingetError:
    reason = getattr(exc, "reason", None) or exc
    return WingetError(f"Cannot reach {_host(url)} ({reason}). Check the internet connection and try again.")


def default_fetch(url: str, headers: Optional[Dict[str, str]] = None, *, max_bytes: int = MAX_FETCH,
                  timeout: float = 60) -> Response:
    """GET ``url`` (HTTPS only, redirects only to HTTPS); HTTP errors come back as a status."""
    if _scheme(url) != "https":
        raise WingetError(f"Refusing a non-HTTPS address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with _opener().open(req, timeout=timeout) as resp:  # noqa: S310 - https only (checked above)
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise WingetError(f"The answer from {_host(url)} is unexpectedly large; refusing it.")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return Response(int(getattr(resp, "status", 200) or 200), body, hdrs, resp.geturl())
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        try:
            body = exc.read(65536) or b""
        except (OSError, http.client.HTTPException):
            body = b""
        return Response(int(exc.code), body, hdrs, url)
    except WingetError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise _offline_error(url, exc) from None


def default_fetch_stream(url: str) -> Any:
    """Open ``url`` for streaming (HTTPS only; redirects only to HTTPS)."""
    if _scheme(url) != "https":
        raise WingetUnsupported(_http_refusal(url))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return _opener().open(req, timeout=60)  # noqa: S310 - https only (checked above)
    except urllib.error.HTTPError as exc:
        raise WingetError(f"The download server answered HTTP {exc.code} ({exc.reason}) for {url}. The link in the "
                          "winget manifest may be out of date; check the publisher's website.") from None
    except WingetError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise _offline_error(url, exc) from None


def _call_fetch(fetch: Fetch, url: str, headers: Optional[Dict[str, str]] = None) -> Response:
    result = fetch(url, dict(headers or {}))
    if isinstance(result, (bytes, bytearray)):
        return Response(200, bytes(result), {}, url)
    if isinstance(result, Response):
        return result
    raise TypeError("fetch() must return bytes or winget.Response")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _cf(text: Any) -> str:
    return unicodedata.normalize("NFC", str(text or "")).casefold()


def _s(value: Any) -> str:
    """Manifest scalar -> str ('' for missing/containers)."""
    return value if isinstance(value, str) else ""


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def is_store_id(package_id: str) -> bool:
    """Microsoft Store product ids (``9NBLGGH4NNS1``, ``XP89DCGQ3K6VLD``) - not in winget-pkgs."""
    return bool(_STORE_ID_RE.match((package_id or "").strip()))


def _refuse_store_id(package_id: str) -> None:
    pid = (package_id or "").strip()
    if is_store_id(pid):
        raise WingetUnsupported(
            f"'{pid}' is a Microsoft Store product id. The Microsoft Store is not available on Linux and Lindos "
            "does not download Store packages (they are licensed to Windows). See whether the app has a web "
            f"version at https://apps.microsoft.com/detail/{pid.upper()}, or search the winget catalogue for a "
            "desktop installer: lindos-compat winget search <name>")


def _check_id(package_id: str) -> str:
    pid = unicodedata.normalize("NFC", (package_id or "").strip())
    segs = pid.split(".")
    if len(pid) > 128 or len(segs) < 2 or not all(_ID_SEGMENT_RE.match(s) for s in segs):
        raise WingetNotFound(f"'{package_id}' is not a winget package id (they look like Publisher.Program, e.g. "
                             "Mozilla.Firefox). Search first: lindos-compat winget search <name>")
    return pid


def manifest_dir(package_id: str, version: Optional[str] = None) -> str:
    """``manifests/<id[0].lower()>/<Id with '.' -> '/'>/[<version>/]`` (winget-pkgs layout)."""
    pid = _check_id(package_id)
    path = f"manifests/{pid[0].lower()}/{pid.replace('.', '/')}/"
    if version is not None:
        if not version or "/" in version or "\\" in version or version in (".", ".."):
            raise WingetNotFound(f"'{version}' is not a valid package version")
        path += version + "/"
    return path


def _quote_path(path: str) -> str:
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


def default_cache_dir() -> Path:
    """``~/.cache/lindos/winget`` (``LINDOS_HOME`` honoured; ``$XDG_CACHE_HOME`` otherwise)."""
    if os.environ.get("LINDOS_HOME"):
        return user_home() / ".cache" / "lindos" / "winget"
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else user_home() / ".cache"
    return base / "lindos" / "winget"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# versions (winget Version semantics, AppInstallerSharedLib/Versions.cpp)
# ---------------------------------------------------------------------------

_U64 = (1 << 64) - 1


@dataclass(frozen=True)
class _Part:
    integer: int
    other: str
    folded: str


def _part(text: str) -> _Part:
    t = text.strip()
    m = re.match(r"^([+-]?)(\d+)", t)
    if not m:
        return _Part(0, t, _cf(t))
    value = int(m.group(2))
    if value > _U64:  # strtoull ERANGE: Integer 0, the whole part becomes "Other"
        return _Part(0, t, _cf(t))
    if m.group(1) == "-" and value:
        value = (-value) & _U64  # strtoull negates in unsigned arithmetic
    other = t[m.end():]
    return _Part(value, other, _cf(other))


def _part_lt(a: _Part, b: _Part) -> bool:
    if a.integer != b.integer:
        return a.integer < b.integer
    if not a.other:
        return False
    if not b.other:
        return True
    return a.folded < b.folded


@dataclass(frozen=True)
class _Version:
    parts: Tuple[_Part, ...]
    approx: int  # -1 "< x", 0 exact, +1 "> x"
    latest: bool
    unknown: bool


@functools.lru_cache(maxsize=4096)
def _parse_version(text: str) -> _Version:
    version = (text or "").strip()
    approx = 0
    base = version
    if version[:2].lower() == "< ":
        approx, base = -1, version[2:]
    elif version[:2].lower() == "> ":
        approx, base = 1, version[2:]
    digit = re.search(r"\d", base)
    split = base.find(".")
    if digit and (split < 0 or digit.start() < split):
        base = base[digit.start():]
    parts: List[_Part] = []
    pos = 0
    while pos < len(base):
        nxt = base.find(".", pos)
        end = len(base) if nxt < 0 else nxt
        parts.append(_part(base[pos:end]))
        pos = end + 1
    while parts and parts[-1].integer == 0 and not parts[-1].other:
        parts.pop()
    single = parts[0] if len(parts) == 1 and parts[0].integer == 0 else None
    latest = bool(single and single.folded == "latest")
    unknown = bool(single and single.folded == "unknown")
    return _Version(tuple(parts), approx, latest, unknown)


def _version_lt(a: _Version, b: _Version) -> bool:
    if a.latest or b.latest:
        if a.latest and b.latest:
            return a.approx < b.approx
        return b.latest
    if a.unknown or b.unknown:
        if a.unknown and b.unknown:
            return a.approx < b.approx
        return a.unknown
    empty = _Part(0, "", "")
    for i in range(max(len(a.parts), len(b.parts))):
        pa = a.parts[i] if i < len(a.parts) else empty
        pb = b.parts[i] if i < len(b.parts) else empty
        if _part_lt(pa, pb):
            return True
        if _part_lt(pb, pa):
            return False
    return a.approx < b.approx


def compare_versions(a: str, b: str) -> int:
    """winget version order: -1 if a < b, 0 if equal, 1 if a > b.

    Numeric parts compare as numbers, ``1.0 == 1.0.0``, a bare number beats the same number with a
    suffix (``1.0 > 1.0-beta``), suffixes compare case-insensitively, leading ``v`` is ignored,
    ``Latest`` sorts above and ``Unknown`` below everything.
    """
    va, vb = _parse_version(str(a)), _parse_version(str(b))
    if _version_lt(va, vb):
        return -1
    if _version_lt(vb, va):
        return 1
    return 0


def _sort_versions_desc(items: List[Any], key: Callable[[Any], str] = str) -> List[Any]:
    return sorted(items, key=functools.cmp_to_key(lambda x, y: compare_versions(key(y), key(x))))


# ---------------------------------------------------------------------------
# MSZIP (Windows Compression API buffer) decoder
# ---------------------------------------------------------------------------


def decode_mszip(raw: bytes, *, max_size: int = MAX_VERSION_DATA) -> bytes:
    """Decode a Windows Compression API MSZIP buffer (``versionData.mszyml``).

    Layout: 24-byte header (magic ``0A 51 E5 C0``, header size, algorithm 2 = MSZIP, u64
    uncompressed size, u64 block size), then blocks of ``u32 size`` + ``"CK"`` + raw DEFLATE.
    Each block inflates to at most 32 KiB and uses the previous 32 KiB of output as its preset
    dictionary (MSZIP keeps the history across blocks).  Raises :class:`WingetLayoutError`.
    """
    if len(raw) < 24 or raw[:4] != MSZIP_MAGIC:
        raise WingetLayoutError("the package version list is not in the expected (MSZIP) format")
    header_size = raw[4]
    if header_size < 24 or header_size > len(raw):
        raise WingetLayoutError("the package version list has an unexpected header")
    if raw[7] != MSZIP_ALGORITHM:
        raise WingetLayoutError(f"the package version list uses compression algorithm {raw[7]}, not MSZIP")
    total = struct.unpack_from("<Q", raw, 8)[0]
    if total > max_size:
        raise WingetLayoutError("the package version list is unexpectedly large")
    out = bytearray()
    pos = header_size
    while pos < len(raw):
        if pos + 4 > len(raw):
            raise WingetLayoutError("the package version list is truncated")
        size = struct.unpack_from("<I", raw, pos)[0]
        pos += 4
        if size < 2 or pos + size > len(raw):
            raise WingetLayoutError("the package version list has a damaged block")
        block = raw[pos:pos + size]
        pos += size
        if block[:2] != b"CK":
            raise WingetLayoutError("the package version list has a block without the MSZIP 'CK' signature")
        history = bytes(out[-MSZIP_BLOCK:])
        inflater = zlib.decompressobj(-15, zdict=history) if history else zlib.decompressobj(-15)
        try:
            piece = inflater.decompress(block[2:], MSZIP_BLOCK + 1)
        except zlib.error as exc:
            raise WingetLayoutError(f"the package version list cannot be decompressed ({exc})") from None
        if len(piece) > MSZIP_BLOCK or inflater.unconsumed_tail:
            raise WingetLayoutError("the package version list has an oversized block")
        if not inflater.eof:
            raise WingetLayoutError("the package version list has an incomplete block")
        out += piece
        if len(out) > total:
            raise WingetLayoutError("the package version list is longer than its header says")
    if len(out) != total:
        raise WingetLayoutError("the package version list is shorter than its header says")
    return bytes(out)


# ---------------------------------------------------------------------------
# the index (source2.msix -> Public/index.db)
# ---------------------------------------------------------------------------


def _check_index(con: sqlite3.Connection) -> None:
    try:
        meta = {str(k): str(v) for k, v in con.execute("SELECT name, value FROM metadata")}
        con.execute("SELECT rowid, id, name, moniker, latest_version, hash FROM packages LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        raise WingetLayoutError(f"the winget catalogue index is damaged or in an unknown format ({exc})") from None
    version = (meta.get("majorVersion", "?"), meta.get("minorVersion", "?"))
    if version != INDEX_SCHEMA:
        raise WingetLayoutError(
            f"the winget catalogue index uses format {version[0]}.{version[1]}; this Lindos understands "
            f"{INDEX_SCHEMA[0]}.{INDEX_SCHEMA[1]} only (update lindos-compat)")


def _open_index(path: Path) -> sqlite3.Connection:
    """Open a saved ``index.db``: an in-memory snapshot (Python >= 3.11), else read-only on disk."""
    con = sqlite3.connect(":memory:")
    try:
        if hasattr(con, "deserialize"):
            con.deserialize(path.read_bytes())
        else:  # pragma: no cover - Python 3.10 has no Connection.deserialize
            con.close()
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        _check_index(con)
    except sqlite3.DatabaseError as exc:
        con.close()
        raise WingetLayoutError(f"the winget catalogue index is damaged ({exc})") from None
    except WingetError:
        con.close()
        raise
    try:
        con.create_function("lindos_cf", 1, _cf, deterministic=True)
    except (TypeError, sqlite3.NotSupportedError):  # pragma: no cover - very old SQLite
        con.create_function("lindos_cf", 1, _cf)
    return con


def _extract_index(body: bytes) -> bytes:
    if len(body) > MAX_INDEX_MSIX:
        raise WingetLayoutError("the winget catalogue file is unexpectedly large")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            try:
                info = zf.getinfo(INDEX_MEMBER)
            except KeyError:
                raise WingetLayoutError("the winget catalogue file has no Public/index.db") from None
            if info.file_size > MAX_INDEX_DB:
                raise WingetLayoutError("the winget catalogue index is unexpectedly large")
            with zf.open(info) as fh:
                data = fh.read(MAX_INDEX_DB + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
        raise WingetLayoutError(f"the winget catalogue file is not a valid package ({exc})") from None
    if len(data) > MAX_INDEX_DB:
        raise WingetLayoutError("the winget catalogue index is unexpectedly large")
    return data


def load_index(*, fetch: Optional[Fetch] = None, cache_dir: Optional[Path] = None,
               max_age_s: int = 900) -> sqlite3.Connection:
    """Open the winget V2 index, downloading ``source2.msix`` when the cached copy is older than
    ``max_age_s`` (conditional GET with the cached ETag; ``max_age_s=0`` always re-checks).

    Checks ``metadata`` ``majorVersion == 2`` and ``minorVersion == 0`` (else
    :class:`WingetLayoutError`).  When the network is down, a cached index is used (with a
    warning).  The returned connection is an in-memory snapshot with a ``lindos_cf(text)``
    (Unicode case-fold) SQL function.
    """
    fetch = fetch or default_fetch
    idx_dir = Path(cache_dir) / "index" if cache_dir else default_cache_dir() / "index"
    db_path = idx_dir / "index.db"
    meta_path = idx_dir / "index.json"
    meta = _read_json(meta_path)
    have_cache = db_path.is_file()
    now = time.time()
    try:
        checked = float(meta.get("checked", 0))
    except (TypeError, ValueError):
        checked = 0.0
    fresh = have_cache and max_age_s > 0 and 0 <= now - checked < max_age_s
    if not fresh:
        headers: Dict[str, str] = {}
        if have_cache and meta.get("etag"):
            headers["If-None-Match"] = str(meta["etag"])
        resp: Optional[Response]
        try:
            resp = _call_fetch(fetch, INDEX_URL, headers)
        except WingetError as exc:
            if not have_cache:
                raise WingetError(f"Cannot download the winget catalogue: {exc}") from None
            log.warning("Could not refresh the winget catalogue (%s); using the saved copy from %s.", exc,
                        meta.get("last_modified") or "earlier")
            resp = None
        if resp is not None:
            if resp.status == 304 and have_cache:
                meta["checked"] = now
                _write_json(meta_path, meta)
            elif resp.status == 200:
                data = _extract_index(resp.body)
                idx_dir.mkdir(parents=True, exist_ok=True)
                tmp = db_path.with_name("index.db.tmp")
                with open(tmp, "wb") as fh:
                    fh.write(data)
                try:
                    _open_index(tmp).close()  # validate before replacing the saved copy
                except WingetError:
                    _unlink(tmp)
                    raise
                os.replace(tmp, db_path)
                meta = {
                    "etag": resp.headers.get("etag", ""),
                    "last_modified": resp.headers.get("last-modified", ""),
                    "checked": now,
                    "downloaded": now,
                    "sha256": _sha256(resp.body),
                    "size": len(resp.body),
                    "url": INDEX_URL,
                }
                _write_json(meta_path, meta)
            elif have_cache:
                log.warning("The winget catalogue server answered HTTP %s; using the saved copy.", resp.status)
            else:
                raise WingetError(f"Cannot download the winget catalogue: the server answered HTTP {resp.status}. "
                                  "Try again later.")
    return _open_index(db_path)


def _cached_index(cache_dir: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """The saved index without any network access (None when absent/unusable)."""
    db_path = (Path(cache_dir) if cache_dir else default_cache_dir()) / "index" / "index.db"
    if not db_path.is_file():
        return None
    try:
        return _open_index(db_path)
    except (WingetError, OSError):
        return None


def _rows(con: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
    try:
        return list(con.execute(sql, tuple(params)))
    except sqlite3.OperationalError as exc:  # optional table missing in a smaller index
        log.debug("index query skipped (%s): %s", exc, sql)
        return []


def _match_rank(query: str, value: Any) -> Optional[int]:
    text = _cf(value)
    if not text:
        return None
    if text == query:
        return 0
    if text.startswith(query):
        return 1
    if query in text:
        return 2
    return None


def search(query: str, *, index: Optional[sqlite3.Connection] = None, limit: int = 20) -> List[Dict[str, str]]:
    """Case-insensitive search over Id, Name, Moniker, Command and Tag (+ exact PackageFamilyName,
    ProductCode, UpgradeCode), like ``winget search``.  Best matches first:
    ``[{"id","name","version","moniker","match"}]``.
    """
    q = _cf((query or "").strip())
    if not q:
        raise WingetError("Type what you are looking for, e.g.: lindos-compat winget search firefox")
    limit = max(1, min(int(limit), 1000))
    con = index if index is not None else load_index()
    hits: Dict[int, Tuple[Tuple[int, int], str]] = {}

    def note(rowid: Any, rank: Optional[int], field_rank: int, label: str) -> None:
        # tiers: exact id/name/moniker (0) > starts-with (1) > exact command/tag (2) > contains (3) >
        # command/tag starts-with (4) > contains (5); exact PackageFamilyName/ProductCode first (-1)
        if rank is None or not isinstance(rowid, int):
            return
        if rank < 0:
            tier = -1
        elif field_rank <= 2:
            tier = (0, 1, 3)[rank]
        else:
            tier = (2, 4, 5)[rank]
        key = (tier, field_rank)
        if rowid not in hits or key < hits[rowid][0]:
            hits[rowid] = (key, label)

    for table, column, label in (("pfns2", "pfn", "PackageFamilyName"), ("productcodes2", "productcode", "ProductCode"),
                                 ("upgradecodes2", "upgradecode", "UpgradeCode")):
        for pkg, value in _rows(con, f"SELECT package, {column} FROM {table} WHERE lindos_cf({column}) = ?", (q,)):
            note(pkg, -1, 0, f"{label}: {value}")
    for rowid, pid, name, moniker in _rows(
            con, "SELECT rowid, id, name, moniker FROM packages WHERE instr(lindos_cf(id), ?) > 0 "
                 "OR instr(lindos_cf(name), ?) > 0 OR instr(lindos_cf(moniker), ?) > 0", (q, q, q)):
        note(rowid, _match_rank(q, pid), 0, "")
        note(rowid, _match_rank(q, name), 1, "")
        note(rowid, _match_rank(q, moniker), 2, f"Moniker: {moniker}")
    for rowid, value in _rows(con, "SELECT m.package, c.command FROM commands2 c JOIN commands2_map m "
                                   "ON m.command = c.rowid WHERE instr(lindos_cf(c.command), ?) > 0", (q,)):
        note(rowid, _match_rank(q, value), 3, f"Command: {value}")
    for rowid, value in _rows(con, "SELECT m.package, t.tag FROM tags2 t JOIN tags2_map m "
                                   "ON m.tag = t.rowid WHERE instr(lindos_cf(t.tag), ?) > 0", (q,)):
        note(rowid, _match_rank(q, value), 4, f"Tag: {value}")
    if not hits:
        return []
    packages: Dict[int, Tuple[Any, ...]] = {}
    ids = list(hits)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in _rows(con, f"SELECT rowid, id, name, moniker, latest_version FROM packages WHERE rowid IN ({marks})",
                         chunk):
            packages[row[0]] = row
    def order(row: Tuple[Any, ...]) -> Tuple[Any, ...]:
        # ties: a package whose id has the query as a whole segment ("Mozilla.Firefox" for
        # "firefox") before its sub-packages ("Mozilla.Firefox.ach"), then shorter ids, then names
        segment = 0 if q in [_cf(seg) for seg in str(row[1]).split(".")] else 1
        return (hits[row[0]][0], segment, len(str(row[1])), _cf(row[2]), _cf(row[1]))

    ordered = sorted(packages.values(), key=order)
    return [{"id": str(r[1]), "name": str(r[2] or ""), "version": str(r[4] or ""), "moniker": str(r[3] or ""),
             "match": hits[r[0]][1]} for r in ordered[:limit]]


def canonical_id(package_id: str, *, index: Optional[sqlite3.Connection] = None) -> str:
    """The exact-case PackageIdentifier for ``package_id`` (matched case-insensitively)."""
    _refuse_store_id(package_id)
    pid = _check_id(package_id)
    con = index if index is not None else load_index()
    row = con.execute("SELECT id FROM packages WHERE id = ?", (pid,)).fetchone()
    if row:
        return str(row[0])
    rows = con.execute("SELECT id FROM packages WHERE lindos_cf(id) = ?", (_cf(pid),)).fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    if len(rows) > 1:
        raise WingetError(f"'{pid}' matches several packages that differ only in upper/lower case: "
                          + ", ".join(str(r[0]) for r in rows) + ". Type the id exactly.")
    raise WingetNotFound(f"There is no package '{pid}' in the winget catalogue. Search for it: "
                         f"lindos-compat winget search {pid.split('.')[-1]}")


# ---------------------------------------------------------------------------
# versions and manifests: CDN chain
# ---------------------------------------------------------------------------


def _hash_hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value or "").strip().lower()


def _cdn_version_entries(package_id: str, fetch: Fetch, con: sqlite3.Connection) -> Tuple[str, List[Dict[str, str]]]:
    cid = canonical_id(package_id, index=con)
    row = con.execute("SELECT hash FROM packages WHERE id = ?", (cid,)).fetchone()
    expected = _hash_hex(row[0] if row else None)
    if not _SHA256_RE.match(expected):
        raise WingetLayoutError(f"the index has no version-list fingerprint for {cid}")
    url = f"{CDN_BASE}packages/{urllib.parse.quote(cid, safe='')}/{expected[:8]}/versionData.mszyml"
    resp = _call_fetch(fetch, url)
    if resp.status == 404:
        raise WingetLayoutError(f"the CDN has no version list for {cid}")
    if resp.status != 200:
        raise WingetError(f"The winget catalogue server answered HTTP {resp.status} for {cid}. Try again later.")
    actual = _sha256(resp.body)
    if actual != expected:
        raise WingetHashError(
            f"The version list of {cid} does not match the fingerprint in the catalogue index "
            f"(expected SHA-256 {expected}, got {actual}). Lindos will not use it. Refresh the catalogue with "
            "'lindos-compat winget update-index' and try again.")
    try:
        doc = wingetyaml.load(decode_mszip(resp.body))
    except wingetyaml.YamlError as exc:
        raise WingetLayoutError(f"the version list of {cid} cannot be read ({exc})") from None
    doc = _dict(doc)
    sv = _s(doc.get("sV"))
    if sv and not sv.startswith("1."):
        raise WingetLayoutError(f"the version list of {cid} uses schema {sv}")
    entries: List[Dict[str, str]] = []
    for item in _list(doc.get("vD")):
        item = _dict(item)
        v, rp, sha = _s(item.get("v")), _s(item.get("rP")), _s(item.get("s256H")).lower()
        if v and rp and _SHA256_RE.match(sha):
            entries.append({"v": v, "rP": rp, "s256H": sha})
    if not entries:
        raise WingetLayoutError(f"the version list of {cid} is empty or unreadable")
    return cid, _sort_versions_desc(entries, key=lambda e: e["v"])


def _pick_version(versions: Sequence[str], requested: Optional[str], pid: str) -> str:
    if not versions:
        raise WingetNotFound(f"{pid} has no versions in the winget catalogue.")
    if requested is None or not str(requested).strip():
        return versions[0]
    want = str(requested).strip()
    for match in (lambda v: v == want, lambda v: _cf(v) == _cf(want), lambda v: compare_versions(v, want) == 0):
        for v in versions:
            if match(v):
                return v
    newest = ", ".join(versions[:5])
    raise WingetNotFound(f"{pid} has no version '{want}'. Newest versions: {newest}. "
                         f"See: lindos-compat winget show {pid}")


def _safe_rel_path(rp: str) -> str:
    parts = rp.split("/")
    if (not rp or rp.startswith("/") or "\\" in rp or "\x00" in rp or ":" in parts[0]
            or any(p in ("", ".", "..") for p in parts)):
        raise WingetError(f"The catalogue names an unsafe manifest path ({rp!r}); refusing it.")
    return rp


def _cdn_manifest(entry: Dict[str, str], fetch: Fetch, cid: str) -> Dict[str, Any]:
    url = CDN_BASE + _quote_path(_safe_rel_path(entry["rP"]))
    resp = _call_fetch(fetch, url)
    if resp.status == 404:
        raise WingetLayoutError(f"the CDN has no manifest for {cid} {entry['v']}")
    if resp.status != 200:
        raise WingetError(f"The winget catalogue server answered HTTP {resp.status} for {cid}. Try again later.")
    if len(resp.body) > MAX_MANIFEST:
        raise WingetError(f"The manifest of {cid} is unexpectedly large; refusing it.")
    actual = _sha256(resp.body)
    if actual != entry["s256H"]:
        raise WingetHashError(
            f"The manifest of {cid} {entry['v']} does not match the fingerprint in its version list "
            f"(expected SHA-256 {entry['s256H']}, got {actual}). Lindos will not use it. Refresh the catalogue "
            "with 'lindos-compat winget update-index' and try again.")
    try:
        manifest = wingetyaml.load(resp.body)
    except wingetyaml.YamlError as exc:
        raise WingetLayoutError(f"the manifest of {cid} cannot be read ({exc})") from None
    if not isinstance(manifest, dict):
        raise WingetLayoutError(f"the manifest of {cid} is not a YAML mapping")
    manifest["LindosSource"] = {"channel": "cdn", "url": url, "sha256": actual,
                                "chain": "index.db -> versionData (SHA-256) -> manifest (SHA-256)"}
    return manifest


# ---------------------------------------------------------------------------
# GitHub fallback
# ---------------------------------------------------------------------------


def _git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git object id, not security


def _rate_limit_message(resp: Response) -> str:
    reset = resp.headers.get("x-ratelimit-reset", "")
    when = ""
    if reset.isdigit():
        when = " (it resets at " + time.strftime("%H:%M", time.localtime(int(reset))) + ")"
    return ("GitHub limits anonymous access to 60 requests per hour and that limit is used up" + when + ". "
            "Lindos needed GitHub because Microsoft's winget CDN could not be used. Try again later.")


def _github_listing(package_id: str, fetch: Fetch) -> Tuple[str, str, Dict[str, Dict[str, str]]]:
    pid = _check_id(package_id)
    base = manifest_dir(pid).rstrip("/")
    url = GITHUB_TREES_URL + urllib.parse.quote("master:" + base, safe="/:") + "?recursive=1"
    resp = _call_fetch(fetch, url, {"Accept": "application/vnd.github+json"})
    if resp.status in (403, 429):
        raise WingetError(_rate_limit_message(resp))
    if resp.status == 404:
        raise WingetNotFound(f"There is no package '{pid}' in the winget catalogue on GitHub (ids are case-sensitive "
                             "there, e.g. Microsoft.PowerToys). Search: lindos-compat winget search <name>")
    if resp.status != 200:
        raise WingetError(f"GitHub answered HTTP {resp.status} while listing {pid}. Try again later.")
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WingetError("GitHub sent an unreadable answer; try again later.") from None
    if data.get("truncated"):
        log.warning("GitHub's file list for %s is truncated; some versions may be missing.", pid)
    versions: Dict[str, Dict[str, str]] = {}
    for item in _list(_dict(data).get("tree")):
        item = _dict(item)
        if item.get("type") != "blob":
            continue
        parts = _s(item.get("path")).split("/")
        sha = _s(item.get("sha"))
        if len(parts) == 2 and parts[1].startswith(pid + ".") and parts[1].endswith(".yaml") and sha:
            versions.setdefault(parts[0], {})[parts[1]] = sha.lower()
    versions = {v: files for v, files in versions.items() if f"{pid}.yaml" in files}
    if not versions:
        raise WingetNotFound(f"GitHub lists no versions for '{pid}'.")
    return pid, base, versions


def _github_manifest(package_id: str, version: Optional[str], fetch: Fetch) -> Dict[str, Any]:
    pid, base, listing = _github_listing(package_id, fetch)
    ver = _pick_version(_sort_versions_desc(list(listing)), version, pid)
    files = listing[ver]

    def get(name: str) -> Dict[str, Any]:
        url = GITHUB_RAW_BASE + _quote_path(f"{base}/{ver}/{name}")
        resp = _call_fetch(fetch, url)
        if resp.status in (403, 429):
            raise WingetError(_rate_limit_message(resp))
        if resp.status != 200:
            raise WingetError(f"GitHub answered HTTP {resp.status} for {name}. Try again later.")
        if len(resp.body) > MAX_MANIFEST:
            raise WingetError(f"{name} is unexpectedly large; refusing it.")
        actual = _git_blob_sha1(resp.body)
        if actual != files[name]:
            raise WingetHashError(f"{name} does not match GitHub's file list (expected git object {files[name]}, got "
                                  f"{actual}); refusing it.")
        try:
            doc = wingetyaml.load(resp.body)
        except wingetyaml.YamlError as exc:
            raise WingetError(f"{name} is not valid YAML ({exc}).") from None
        if not isinstance(doc, dict):
            raise WingetError(f"{name} is not a YAML mapping.")
        return doc

    vdoc = get(f"{pid}.yaml")
    mtype = _s(vdoc.get("ManifestType")).lower()
    if mtype in ("singleton", "merged"):
        manifest = dict(vdoc)
    elif mtype == "version":
        installer_name = f"{pid}.installer.yaml"
        if installer_name not in files:
            raise WingetError(f"{pid} {ver} has no installer manifest on GitHub.")
        default_locale = _s(vdoc.get("DefaultLocale")) or "en-US"
        locale_doc: Dict[str, Any] = {}
        locale_name = f"{pid}.locale.{default_locale}.yaml"
        if locale_name in files:
            locale_doc = get(locale_name)
        manifest = {}
        manifest.update({k: v for k, v in locale_doc.items() if k not in ("ManifestType", "ManifestVersion")})
        manifest.update(get(installer_name))
        manifest.setdefault("PackageLocale", default_locale)
        manifest["ManifestType"] = "merged"
    else:
        raise WingetError(f"{pid} {ver}: unknown manifest type '{mtype}'.")
    manifest["LindosSource"] = {"channel": "github", "url": GITHUB_RAW_BASE + _quote_path(f"{base}/{ver}/"),
                                "chain": "GitHub tree listing -> file git SHA-1"}
    return manifest


# ---------------------------------------------------------------------------
# public: versions / manifest
# ---------------------------------------------------------------------------


def list_versions(package_id: str, *, fetch: Optional[Fetch] = None, index: Optional[sqlite3.Connection] = None,
                  cache_dir: Optional[Path] = None) -> List[str]:
    """Every published version of ``package_id``, newest first."""
    _refuse_store_id(package_id)
    fetch = fetch or default_fetch
    try:
        con = index if index is not None else load_index(fetch=fetch, cache_dir=cache_dir)
        _cid, entries = _cdn_version_entries(package_id, fetch, con)
        return [e["v"] for e in entries]
    except WingetLayoutError as exc:
        log.warning("Microsoft's winget CDN could not be used (%s); asking GitHub instead.", exc)
    _pid, _base, listing = _github_listing(package_id, fetch)
    return _sort_versions_desc(list(listing))


def load_manifest(package_id: str, version: Optional[str] = None, *, fetch: Optional[Fetch] = None,
                  index: Optional[sqlite3.Connection] = None, cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The merged (installer + default-locale) manifest of ``package_id`` at ``version`` (newest
    when None), verified through the hash chain.  Adds ``LindosSource`` (where it came from)."""
    _refuse_store_id(package_id)
    fetch = fetch or default_fetch
    pid = package_id.strip()
    manifest: Optional[Dict[str, Any]] = None
    try:
        con = index if index is not None else load_index(fetch=fetch, cache_dir=cache_dir)
        pid, entries = _cdn_version_entries(package_id, fetch, con)
        chosen = _pick_version([e["v"] for e in entries], version, pid)
        entry = next(e for e in entries if e["v"] == chosen)
        manifest = _cdn_manifest(entry, fetch, pid)
    except WingetLayoutError as exc:
        log.warning("Microsoft's winget CDN could not be used (%s); asking GitHub instead.", exc)
    if manifest is None:
        manifest = _github_manifest(pid, version, fetch)
    got = _s(manifest.get("PackageIdentifier"))
    if not got or _cf(got) != _cf(pid):
        raise WingetError(f"The manifest found for '{pid}' describes '{got or '?'}'; refusing it.")
    if not _list(manifest.get("Installers")):
        raise WingetError(f"The manifest of {got} lists no installers.")
    return manifest


# ---------------------------------------------------------------------------
# installers: inheritance, selection, switches
# ---------------------------------------------------------------------------


def _type_of(value: Any) -> str:
    return _s(value).strip().lower()


def effective_installers(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve each ``Installers[]`` entry against the manifest root, like winget's populator:

    copy the root's installer fields, override scalars, merge ``InstallerSwitches`` per key,
    replace lists and ``Dependencies`` wholesale, inherit ``Nested*`` only for zip (and
    ProductCode / AppsAndFeaturesEntries / PackageFamilyName only where the type uses them), then
    fill missing switches from winget's defaults by the *effective* type.  Adds the computed keys
    ``BaseInstallerType`` and ``EffectiveInstallerType`` (lower case).
    """
    root = _dict(manifest)
    out: List[Dict[str, Any]] = []
    for entry in _list(root.get("Installers")):
        if not isinstance(entry, dict):
            continue
        inst: Dict[str, Any] = {k: copy.deepcopy(root[k]) for k in ROOT_INSTALLER_FIELDS
                                if k in root and k not in _CONDITIONAL_ROOT_FIELDS}
        for key, value in entry.items():
            if key == "InstallerSwitches" and isinstance(value, dict):
                merged = dict(_dict(inst.get("InstallerSwitches")))
                merged.update(copy.deepcopy(value))
                inst["InstallerSwitches"] = merged
            else:
                inst[key] = copy.deepcopy(value)
        base = _type_of(inst.get("InstallerType"))
        if base == "zip":
            for key in ("NestedInstallerType", "NestedInstallerFiles"):
                if key not in entry and key in root:
                    inst[key] = copy.deepcopy(root[key])
        else:
            for key in ("NestedInstallerType", "NestedInstallerFiles"):
                if key not in entry:
                    inst.pop(key, None)
        effective = _type_of(inst.get("NestedInstallerType")) if base == "zip" else base
        if "ProductCode" not in entry and "ProductCode" in root and effective in _ARP_TYPES:
            inst["ProductCode"] = copy.deepcopy(root["ProductCode"])
        if "AppsAndFeaturesEntries" not in entry and "AppsAndFeaturesEntries" in root and effective in _ARP_TYPES:
            inst["AppsAndFeaturesEntries"] = copy.deepcopy(root["AppsAndFeaturesEntries"])
        if "PackageFamilyName" not in entry and "PackageFamilyName" in root and effective in ("msix", "appx", "msstore"):
            inst["PackageFamilyName"] = copy.deepcopy(root["PackageFamilyName"])
        defaults = DEFAULT_SWITCHES.get(effective, {})
        if defaults:
            switches = dict(_dict(inst.get("InstallerSwitches")))
            for key, value in defaults.items():
                if not _s(switches.get(key)):
                    switches[key] = value
            inst["InstallerSwitches"] = switches
        inst["BaseInstallerType"] = base
        inst["EffectiveInstallerType"] = effective
        out.append(inst)
    return out


def system_locale() -> str:
    """The user's locale as a BCP-47 tag (``de_DE.UTF-8`` -> ``de-DE``; C/POSIX -> ``en-US``)."""
    value = ""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            break
    value = value.split(".")[0].split("@")[0]
    if not value or value in ("C", "POSIX"):
        return "en-US"
    return value.replace("_", "-")


def _locale_rank(installer_locale: str, wanted: str) -> int:
    if not installer_locale:
        return 2
    have, want = installer_locale.casefold(), wanted.casefold()
    if have == want:
        return 0
    if have.split("-")[0] == want.split("-")[0]:
        return 1
    if have == "en-us":
        return 3
    if have.split("-")[0] == "en":
        return 4
    return 5


def _http_refusal(url: str) -> str:
    return (f"The download link ({url}) uses plain http://, which Lindos refuses: a file fetched that way could be "
            "swapped on the way to your PC. Get the program from the publisher's website instead.")


def _reject_reason(inst: Dict[str, Any], arch: Optional[str], homepage: str) -> Optional[str]:
    a = _type_of(inst.get("Architecture"))
    base = inst.get("BaseInstallerType", "")
    effective = inst.get("EffectiveInstallerType", "")
    url = _s(inst.get("InstallerUrl"))
    if a in ("arm", "arm64"):
        return "built for ARM Windows (Lindos runs x64/x86 programs)"
    if a not in ("x64", "x86", "neutral"):
        return f"unknown architecture '{a or '?'}'"
    if "x64" in [_type_of(x) for x in _list(inst.get("UnsupportedOSArchitectures"))]:
        return "the publisher says it does not support 64-bit Windows"
    if arch and a not in (arch, "neutral"):
        return f"{a}, but --arch {arch} was requested"
    if base == "msstore" or effective == "msstore":
        return "a Microsoft Store package (the Store is not available on Linux)"
    if base == "pwa" or effective == "pwa":
        where = f" - open {homepage} in your browser and use its 'Install app' option" if homepage else ""
        return "a web app (PWA), not a Windows program" + where
    if base == "font" or effective == "font":
        return "a font package (Lindos does not install fonts through winget)"
    if base == "zip" and effective not in _RUNNABLE_TYPES:
        return f"a zip file with an unsupported inner installer type '{effective or '?'}'"
    if base != "zip" and base not in _RUNNABLE_TYPES:
        return f"unknown installer type '{base or '?'}'"
    if _scheme(url) == "http":
        return f"plain http:// download ({url})"
    if _scheme(url) != "https":
        return "no usable download link"
    if not _SHA256_RE.match(_s(inst.get("InstallerSha256")).strip()):
        return "no SHA-256 fingerprint in the manifest"
    return None


def _rank_installers(manifest: Dict[str, Any], *, arch: Optional[str], locale: Optional[str]
                     ) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], str]]]:
    arch = (arch or "").strip().lower() or None
    if arch not in (None, "x64", "x86"):
        raise WingetError("--arch must be x64 or x86")
    wanted = locale or system_locale()
    homepage = _s(manifest.get("PackageUrl")) or _s(manifest.get("PublisherUrl"))
    ok: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
    rejected: List[Tuple[Dict[str, Any], str]] = []
    for idx, inst in enumerate(effective_installers(manifest)):
        reason = _reject_reason(inst, arch, homepage)
        if reason:
            rejected.append((inst, reason))
            continue
        a = _type_of(inst.get("Architecture"))
        effective = inst["EffectiveInstallerType"]
        scope = _type_of(inst.get("Scope"))
        key = (
            1 if effective in _MSIX_TYPES else 0,
            {"x64": 0, "x86": 1, "neutral": 2}[a] if not arch else (0 if a == arch else 1),
            0 if scope == "user" else 1,  # a scope-less (portable) entry does not beat "machine"
            _locale_rank(_s(inst.get("InstallerLocale")), wanted),
            TYPE_ORDER.get(effective, 9),
            1 if inst["BaseInstallerType"] == "zip" else 0,
            idx,
        )
        ok.append((key, inst))
    ok.sort(key=lambda kv: kv[0])
    return [inst for _key, inst in ok], rejected


def select_installer(manifest: Dict[str, Any], *, arch: Optional[str] = None,
                     locale: Optional[str] = None) -> Dict[str, Any]:
    """Pick the installer Lindos should use (see the module docstring for the order), or raise
    :class:`WingetUnsupported` explaining why none of them can be used."""
    ranked, rejected = _rank_installers(manifest, arch=arch, locale=locale)
    if ranked:
        return ranked[0]
    pid = _s(manifest.get("PackageIdentifier")) or "this package"
    if not rejected:
        raise WingetUnsupported(f"The manifest of {pid} lists no installers.")
    reasons = sorted({reason for _inst, reason in rejected})
    lines = [f"Lindos cannot install {pid}: none of its installers can be used here."]
    for inst, reason in rejected:
        lines.append(f"  - {_type_of(inst.get('Architecture')) or '?'} {inst.get('BaseInstallerType') or '?'}: {reason}")
    if all(r.startswith("plain http") for r in reasons):
        lines.append(_http_refusal(_s(rejected[0][0].get("InstallerUrl"))))
    elif all(r.startswith("built for ARM") for r in reasons):
        lines.append("Get the x64 (Intel/AMD) version from the publisher's website if there is one.")
    elif arch and any("--arch" in r for r in reasons):
        lines.append("Try again without --arch.")
    raise WingetUnsupported("\n".join(lines))


def split_command_line(text: str) -> List[str]:
    """Split a Windows command-line fragment into arguments (CommandLineToArgvW rules): white space
    separates, ``"..."`` groups (the quotes are removed), ``\\"`` is a literal quote, ``""`` inside
    quotes is a literal quote, and backslashes are literal unless they precede a quote."""
    args: List[str] = []
    cur: List[str] = []
    in_quotes = False
    have = False
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
                have = True
                continue
            in_quotes = not in_quotes
            have = True
            i += 1
            continue
        if ch in " \t" and not in_quotes:
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


def _safe_path_token(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._+-]+", "_", text or "").strip("._")
    return cleaned[:80] or "Program"


def installer_switches(installer: Dict[str, Any], *, interactive: bool = False, install_path: Optional[str] = None,
                       log_path: Optional[str] = None) -> List[str]:
    """The arguments Lindos passes to the installer, as separate tokens.

    Silent (or SilentWithProgress) -- or Interactive with ``interactive=True`` -- then ``Custom``
    (always); ``Log``, ``Upgrade`` and ``InstallLocation`` are left out (InstallLocation only when the
    manifest says ``InstallLocationRequired``).  ``<INSTALLPATH>``/``<LOGPATH>`` are replaced
    before splitting, no token keeps a literal quote (Wine re-quotes arguments that contain spaces
    itself), and an NSIS ``/D=`` goes last, unquoted.
    """
    switches = _dict(installer.get("InstallerSwitches"))
    effective = installer.get("EffectiveInstallerType") or _type_of(installer.get("InstallerType"))
    parts: List[str] = []
    if interactive:
        if _s(switches.get("Interactive")):
            parts.append(_s(switches.get("Interactive")))
    else:
        experience = _s(switches.get("Silent")) or _s(switches.get("SilentWithProgress"))
        if experience:
            parts.append(experience)
    if _s(switches.get("Custom")):
        parts.append(_s(switches.get("Custom")))
    if _s(installer.get("InstallLocationRequired")).lower() == "true" and _s(switches.get("InstallLocation")):
        parts.append(_s(switches.get("InstallLocation")))
    name = _safe_path_token(_s(installer.get("LindosPackageIdentifier")) or "Program")
    ipath = install_path or f"C:\\Programs\\{name}"
    lpath = log_path or "C:\\windows\\temp\\lindos-winget-install.log"
    tokens: List[str] = []
    for part in parts:
        text = part.replace("<INSTALLPATH>", ipath).replace("<LOGPATH>", lpath)
        tokens.extend(split_command_line(text))
    tokens = [t.replace('"', "") for t in tokens]
    tokens = [t for t in tokens if t]
    if effective == "nullsoft":
        dest = [t for t in tokens if t.startswith("/D=")]
        tokens = [t for t in tokens if not t.startswith("/D=")] + dest[-1:]
    return tokens


def installer_argv(installer: Dict[str, Any], *, windows_file: str, interactive: bool = False) -> List[str]:
    """The Windows command line (as argv) that installs ``windows_file``:
    ``["msiexec", "/i", file, ...]`` for msi/wix, ``[file, ...]`` otherwise (no switches for
    portable and MSIX, which are not "run")."""
    effective = installer.get("EffectiveInstallerType") or _type_of(installer.get("InstallerType"))
    if effective in _MSIX_TYPES or effective == "portable":
        return [windows_file]
    tokens = installer_switches(installer, interactive=interactive)
    if effective in ("msi", "wix"):
        return ["msiexec", "/i", windows_file] + tokens
    return [windows_file] + tokens


def _int(value: Any) -> Optional[int]:
    """A manifest integer (YAML gives strings) as an unsigned 32-bit Windows exit code, or None."""
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip()) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None


def _return_code_table(installer: Dict[str, Any]) -> Dict[int, str]:
    table = dict(DEFAULT_RETURN_CODES.get(installer.get("EffectiveInstallerType", ""), {}))
    for item in _list(installer.get("ExpectedReturnCodes")):
        item = _dict(item)
        code = _int(item.get("InstallerReturnCode"))
        if code is not None:
            table[code] = _s(item.get("ReturnResponse")) or "custom"
    return table


def exit_code_outcome(installer: Dict[str, Any], code: int) -> Tuple[str, str]:
    """Map an installer exit code to ``(outcome, message)``; outcome is one of ``installed``,
    ``installed-restart``, ``already-installed``, ``cancelled``, ``busy`` or ``failed``."""
    value = int(code) & 0xFFFFFFFF
    success = {0}
    for item in _list(installer.get("InstallerSuccessCodes")):
        parsed = _int(item)
        if parsed is not None:
            success.add(parsed)
    if value in success:
        return "installed", "installed"
    response = _return_code_table(installer).get(value)
    if response:
        outcome, message = RETURN_RESPONSES.get(response, RETURN_RESPONSES["custom"])
        return outcome, message
    if value == 1603 and installer.get("EffectiveInstallerType") in ("msi", "wix", "burn"):
        return "failed", "the installer hit a fatal error (Windows Installer error 1603)"
    return "failed", f"the installer ended with error code {code}"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def _stream_url(stream: Any, fallback: str) -> str:
    url = getattr(stream, "url", None)
    if not url and callable(getattr(stream, "geturl", None)):
        url = stream.geturl()
    return str(url or fallback)


def _installer_file_name(installer: Dict[str, Any], url: str, sha: str) -> str:
    base = installer.get("BaseInstallerType") or _type_of(installer.get("InstallerType"))
    ext = _EXT_BY_TYPE.get(base, "")
    try:
        last = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    except ValueError:
        last = ""
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", last).strip("._-")[:100]
    root, dot_ext = os.path.splitext(stem)
    if dot_ext.lower() in _KNOWN_EXTS and root:
        stem = root
    if not stem:
        stem = f"installer-{sha[:12]}"
    return stem + ext


def download(installer: Dict[str, Any], dest_dir: Path, *, fetch_stream: Optional[Callable[[str], Any]] = None,
             max_bytes: int = MAX_INSTALLER, on_progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Download the installer to ``dest_dir`` and return its path -- only if its SHA-256 matches.

    The file is written as ``<sha256>.part`` (not runnable, not associated with anything) and only
    renamed to its real name after the hash matched; on a mismatch it is deleted and
    :class:`WingetHashError` quotes both hashes.  HTTPS only (also after redirects).  No override.
    """
    url = _s(installer.get("InstallerUrl")).strip()
    if _scheme(url) == "http":
        raise WingetUnsupported(_http_refusal(url))
    if _scheme(url) != "https":
        raise WingetError(f"The manifest has no usable download link ({url or 'none'}).")
    expected = _s(installer.get("InstallerSha256")).strip()
    if not _SHA256_RE.match(expected):
        raise WingetError("The manifest has no valid SHA-256 fingerprint for this installer; Lindos will not "
                          "download it.")
    expected = expected.lower()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = dest_dir / f"{expected}.part"
    stream = (fetch_stream or default_fetch_stream)(url)
    digest = hashlib.sha256()
    total = 0
    try:
        final_url = _stream_url(stream, url)
        if _scheme(final_url) != "https":
            raise WingetUnsupported(f"The download was redirected to a non-HTTPS address ({final_url}); Lindos "
                                    "refuses it.")
        status = getattr(stream, "status", None)
        if isinstance(status, int) and status != 200:
            raise WingetError(f"The download server answered HTTP {status} for {url}.")
        headers = getattr(stream, "headers", None)
        length = ""
        if headers is not None and hasattr(headers, "get"):
            length = str(headers.get("Content-Length") or headers.get("content-length") or "")
        expected_len = int(length) if length.isdigit() else 0
        if expected_len > max_bytes:
            raise WingetError(f"The installer is {expected_len} bytes - larger than Lindos accepts ({max_bytes}).")
        with open(part, "wb") as fh:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise WingetError(f"The download is larger than Lindos accepts ({max_bytes} bytes); stopped.")
                digest.update(chunk)
                fh.write(chunk)
                if on_progress is not None:
                    on_progress(total, expected_len)
    except BaseException:
        _unlink(part)
        raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass
    if total == 0:
        _unlink(part)
        raise WingetError(f"The download from {_host(url)} was empty; nothing to install.")
    actual = digest.hexdigest()
    if actual != expected:
        _unlink(part)
        released = _s(installer.get("ReleaseDate"))
        raise WingetHashError(
            "The downloaded file does not match the fingerprint (SHA-256) in the official winget manifest:\n"
            f"  expected {expected}\n  got      {actual}\n"
            "Lindos deleted it and will not run it - there is no way to override this. The publisher has probably "
            "replaced the file at the same address" + (f" since the manifest was written ({released})" if released
                                                       else "")
            + "; the catalogue is usually updated within a few days. You can also get the program from the "
              "publisher's website.")
    final = dest_dir / _installer_file_name(installer, url, expected)
    os.replace(part, final)
    return final


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# zip extraction (zip-slip safe)
# ---------------------------------------------------------------------------


def _safe_member_parts(name: str) -> Optional[Tuple[str, ...]]:
    """Validated path parts of a zip member (None for a directory entry); raises WingetError."""
    if any(ord(c) < 32 for c in name):
        raise WingetError(f"The archive contains a file name with control characters ({name!r}); refusing it.")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise WingetError(f"The archive contains an absolute path ({name!r}); refusing it.")
    is_dir = norm.endswith("/")
    parts = norm.rstrip("/").split("/") if norm.rstrip("/") else []
    for part in parts:
        if part in ("", ".", "..") or ":" in part:
            raise WingetError(f"The archive contains an unsafe path ({name!r}); refusing it.")
    if is_dir:
        return None
    if not parts:
        raise WingetError("The archive contains an entry without a name; refusing it.")
    return tuple(parts)


def _extract_zip(archive: Path, dest: Path, *, max_bytes: int = MAX_EXTRACT_BYTES, max_files: int = MAX_EXTRACT_FILES,
                 max_ratio: int = MAX_EXTRACT_RATIO) -> List[Tuple[str, ...]]:
    """Extract ``archive`` into the (new, empty) directory ``dest``; returns the file parts."""
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WingetError(f"The downloaded file is not a valid zip archive ({exc}).") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise WingetError(f"The archive has {len(infos)} entries - more than Lindos accepts ({max_files}).")
        plan: List[Tuple[zipfile.ZipInfo, Tuple[str, ...]]] = []
        seen: set = set()
        total = 0
        for info in infos:
            parts = _safe_member_parts(info.filename)
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise WingetError(f"The archive contains a symbolic link ({info.filename}); refusing it.")
            if info.flag_bits & 0x1:
                raise WingetError("The archive is password-protected; Lindos cannot unpack it.")
            if parts is None:
                continue
            key = "/".join(p.casefold() for p in parts)
            if key in seen:
                raise WingetError(f"The archive contains two files that differ only in upper/lower case "
                                  f"({info.filename}); refusing it.")
            seen.add(key)
            total += info.file_size
            if total > max_bytes:
                raise WingetError(f"The archive unpacks to more than {max_bytes} bytes; refusing it.")
            if info.file_size > (1 << 20) and info.file_size > max_ratio * max(info.compress_size, 1):
                raise WingetError(f"The archive entry {info.filename} is compressed suspiciously well (a 'zip bomb'?); "
                                  "refusing it.")
            plan.append((info, parts))
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        files: List[Tuple[str, ...]] = []
        for info, parts in plan:
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if root not in target.parent.resolve().parents and target.parent.resolve() != root:
                raise WingetError(f"The archive entry {info.filename} would land outside its folder; refusing it.")
            written = 0
            try:
                with zf.open(info) as src, open(target, "xb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise WingetError(f"The archive entry {info.filename} is larger than it claims; refusing it.")
                        dst.write(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise WingetError(f"The archive is damaged ({exc}).") from None
            except FileExistsError:
                raise WingetError(f"The archive contains {info.filename} twice; refusing it.") from None
            files.append(parts)
        return files


def _find_ci(root: Path, rel: str) -> Optional[Path]:
    """Case-insensitive lookup of a (backslash or slash separated) relative path below ``root``."""
    parts = _safe_member_parts(rel)
    if not parts:
        return None
    current = root
    for part in parts:
        exact = current / part
        if exact.exists():
            current = exact
            continue
        try:
            match = next((c for c in current.iterdir() if c.name.casefold() == part.casefold()), None)
        except OSError:
            return None
        if match is None:
            return None
        current = match
    return current


def _pe_subsystem(path: Path) -> Optional[int]:
    """PE optional-header Subsystem (2 = GUI, 3 = console) or None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 24 + 70 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return None
    return int(struct.unpack_from("<H", head, pe + 24 + 68)[0])


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    return expand_user_path(path_const("STATE_DIR")) / "winget-installed.json"


def _ledger_load() -> Dict[str, Dict[str, Any]]:
    data = _read_json(_ledger_path())
    packages = data.get("packages")
    return {k: v for k, v in packages.items() if isinstance(v, dict)} if isinstance(packages, dict) else {}


def _ledger_save(packages: Dict[str, Dict[str, Any]]) -> None:
    _write_json(_ledger_path(), {"schema": 1, "packages": packages})


def _apps_db() -> Tuple[Any, Dict[str, Dict[str, Any]]]:
    c = core()
    db = c.apps_db_load()
    return c, (db if isinstance(db, dict) else {})


def _tag_apps(prefix_slug: str, before: Dict[str, str], manifest: Dict[str, Any]) -> List[str]:
    """Mark APPS_DB records created by this install with source/winget_id/winget_version."""
    try:
        c, db = _apps_db()
    except CoreMissing:
        return []
    except Exception as exc:  # noqa: BLE001 - a broken apps db must not fail the install
        log.warning("apps database unreadable: %s", exc)
        return []
    tagged: List[str] = []
    for slug, rec in db.items():
        if not isinstance(rec, dict) or rec.get("prefix") != prefix_slug:
            continue
        if slug in before and before[slug] == str(rec.get("installed_at", "")):
            continue
        if rec.get("source") and rec.get("source") != "winget":
            rec.setdefault("source_file", rec.get("source"))
        rec.update({"source": "winget", "winget_id": _s(manifest.get("PackageIdentifier")),
                    "winget_version": _s(manifest.get("PackageVersion"))})
        tagged.append(slug)
    if tagged:
        c.apps_db_save(db)
    return sorted(tagged)


def _apps_snapshot() -> Dict[str, str]:
    try:
        _c, db = _apps_db()
    except Exception:  # noqa: BLE001 - CoreMissing or an unreadable db
        return {}
    return {slug: str(rec.get("installed_at", "")) for slug, rec in db.items() if isinstance(rec, dict)}


def _prefix_user(prefix_dir: Path) -> str:
    users = prefix_dir / "drive_c" / "users"
    try:
        me = getpass.getuser()
    except (KeyError, OSError, ImportError):  # pragma: no cover - no passwd entry
        me = ""
    me = re.sub(r"[^A-Za-z0-9._-]", "_", me).strip("._") or "user"
    try:
        names = [d.name for d in users.iterdir() if d.is_dir() and d.name.lower() not in ("public", "root")]
    except OSError:
        names = []
    if me in names or not names:
        return me
    return sorted(names)[0]


def _default_run_lindos(argv: List[str], env: Dict[str, str]) -> int:
    try:
        return subprocess.run(argv, env=env, check=False).returncode
    except OSError as exc:
        log.error("cannot start %s: %s", argv[0], exc)
        return 127


def _lindos_run_path(which: Callable[[str], Optional[str]]) -> str:
    return which("lindos-run") or "/usr/bin/lindos-run"


def _manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    agreements = []
    for item in _list(manifest.get("Agreements")):
        item = _dict(item)
        agreements.append({"label": _s(item.get("AgreementLabel")), "text": _s(item.get("Agreement")),
                           "url": _s(item.get("AgreementUrl"))})
    return {
        "id": _s(manifest.get("PackageIdentifier")),
        "version": _s(manifest.get("PackageVersion")),
        "name": _s(manifest.get("PackageName")) or _s(manifest.get("PackageIdentifier")),
        "publisher": _s(manifest.get("Publisher")),
        "description": _s(manifest.get("ShortDescription")) or _s(manifest.get("Description")),
        "homepage": _s(manifest.get("PackageUrl")) or _s(manifest.get("PublisherUrl")),
        "license": _s(manifest.get("License")),
        "license_url": _s(manifest.get("LicenseUrl")),
        "privacy_url": _s(manifest.get("PrivacyUrl")),
        "agreements": agreements,
        "source": _dict(manifest.get("LindosSource")),
    }


def _installer_summary(inst: Dict[str, Any]) -> Dict[str, Any]:
    url = _s(inst.get("InstallerUrl"))
    nested = []
    for item in _list(inst.get("NestedInstallerFiles")):
        item = _dict(item)
        nested.append({"path": _s(item.get("RelativeFilePath")), "alias": _s(item.get("PortableCommandAlias"))})
    return {
        "type": inst.get("BaseInstallerType", ""),
        "effective_type": inst.get("EffectiveInstallerType", ""),
        "architecture": _type_of(inst.get("Architecture")),
        "scope": _type_of(inst.get("Scope")),
        "locale": _s(inst.get("InstallerLocale")),
        "url": url,
        "host": _host(url),
        "sha256": _s(inst.get("InstallerSha256")).lower(),
        "elevation": _s(inst.get("ElevationRequirement")),
        "minimum_os": _s(inst.get("MinimumOSVersion")),
        "nested_files": nested,
    }


def _dependencies(inst: Dict[str, Any]) -> Dict[str, Any]:
    deps = _dict(inst.get("Dependencies"))
    packages = []
    for item in _list(deps.get("PackageDependencies")):
        item = _dict(item)
        pid = _s(item.get("PackageIdentifier"))
        if pid:
            packages.append({"id": pid, "min_version": _s(item.get("MinimumVersion"))})
    return {
        "packages": packages,
        "windows_features": [_s(x) for x in _list(deps.get("WindowsFeatures")) if _s(x)],
        "windows_libraries": [_s(x) for x in _list(deps.get("WindowsLibraries")) if _s(x)],
        "external": [_s(x) for x in _list(deps.get("ExternalDependencies")) if _s(x)],
    }


def installer_notes(manifest: Dict[str, Any], inst: Dict[str, Any], *, interactive: bool = False) -> List[str]:
    """Plain-language notes (trust, limits, dependencies) shown before installing ``inst``."""
    notes: List[str] = []
    effective = inst.get("EffectiveInstallerType", "")
    switches = _dict(inst.get("InstallerSwitches"))
    elevation = _s(inst.get("ElevationRequirement"))
    if elevation in ("elevationRequired", "elevatesSelf"):
        notes.append("This installer asks for administrator rights. Wine grants them silently inside this C:\\ drive "
                     "(nothing outside it) - only install software you trust.")
    elif elevation == "elevationProhibited":
        notes.append("This installer expects to run without administrator rights; under Wine it runs as an "
                     "administrator of its C:\\ drive and may refuse or misbehave.")
    if effective == "exe" and not interactive and not (_s(switches.get("Silent")) or _s(switches.get("SilentWithProgress"))):
        notes.append("This installer has no silent mode: its own window will open - click through it.")
    if interactive:
        notes.append("Interactive mode: the installer's own window will open.")
    if _type_of(inst.get("Scope")) == "machine":
        notes.append("'For all users' installers still only install into this C:\\ drive, for your Linux user.")
    minimum = _s(inst.get("MinimumOSVersion"))
    if minimum and compare_versions(minimum, WINE_WINDOWS_VERSION) > 0:
        notes.append(f"It needs Windows {minimum} or newer; Wine reports Windows 10 ({WINE_WINDOWS_VERSION}) by default. "
                     "If the installer refuses, pick 'Windows 11' in winecfg for this C:\\ drive.")
    if effective in _MSIX_TYPES:
        notes.append("This is an MSIX/APPX package. Wine cannot install those itself; Lindos unpacks packaged desktop "
                     "apps into the C:\\ drive and explains when a package is a UWP/WinUI app that cannot run.")
    if effective == "portable":
        notes.append("A portable program: nothing is 'installed'; Lindos unpacks it into the C:\\ drive "
                     "(AppData\\Local\\Microsoft\\WinGet\\Packages) and adds it to the apps list.")
    if _s(inst.get("ArchiveBinariesDependOnPath")).lower() == "true":
        notes.append("The programs in this archive expect their folder on PATH; start them from that folder.")
    deps = _dependencies(inst)
    if deps["windows_features"]:
        notes.append("It needs the Windows features " + ", ".join(deps["windows_features"])
                     + " - Windows features are not available under Wine; the program may not work.")
    if deps["windows_libraries"]:
        notes.append("It needs the Windows libraries " + ", ".join(deps["windows_libraries"]) + ".")
    if deps["external"]:
        notes.append("It needs, from elsewhere: " + ", ".join(deps["external"]) + ".")
    notes.append("Integrity: the file is checked against the SHA-256 in the hash-checked winget catalogue before it "
                 "runs. The publisher's code signature is not verified.")
    return notes


def format_plan(plan: Dict[str, Any]) -> str:
    """Human-readable summary of an install plan (what, from where, licence, notes)."""
    inst = plan["installer"]
    lines = [
        f"Package:    {plan['name']} {plan['version']}  ({plan['id']})" + (f" by {plan['publisher']}" if plan["publisher"]
                                                                          else ""),
        f"Installer:  {inst['effective_type']}" + (" in a zip" if inst["type"] == "zip" else "")
        + f", {inst['architecture']}" + (f", {inst['scope']} scope" if inst["scope"] else "")
        + f", from {inst['host']}",
        f"C:\\ drive:  {plan['prefix']}",
    ]
    if plan["license"] or plan["license_url"]:
        lines.append("Licence:    " + " - ".join(x for x in (plan["license"], plan["license_url"]) if x))
    for agreement in plan["agreements"]:
        label = agreement["label"] or "Agreement"
        text = agreement["text"].strip()
        lines.append(f"{label}: " + (text if len(text) <= 600 else text[:600] + " ...")
                     + (f" ({agreement['url']})" if agreement["url"] else ""))
    deps = plan["dependencies"]["packages"]
    if deps:
        lines.append("Needs:      " + ", ".join(d["id"] + (f" (>= {d['min_version']})" if d["min_version"] else "")
                                                for d in deps))
    for note in plan["notes"]:
        lines.append(f"Note:       {note}")
    return "\n".join(lines)


def _ask_nobody(_question: str, _default: bool = False) -> bool:
    return False


def _installed_version(package_id: str, prefix_slug: str) -> Optional[str]:
    rec = _ledger_load().get(f"{prefix_slug}/{package_id}")
    if rec and _s(rec.get("version")):
        return _s(rec.get("version"))
    return None


def install(package_id: str, **kw: Any) -> Dict[str, Any]:
    """Install a winget package into a C:\\ drive.

    Keyword arguments: ``version``, ``arch`` (x64|x86), ``prefix`` (C:\\ drive name; default: the
    package name), ``interactive``, ``accept_package_agreements``, ``dry_run``, ``locale``,
    ``fetch``, ``fetch_stream``, ``index``, ``cache_dir``, ``run_lindos(argv, env) -> int``,
    ``which``, ``confirm(question, default) -> bool``, ``out(text)``, ``manifest`` (skip loading).

    Raises :class:`WingetError` for problems found before anything ran (unknown id, refusals,
    hash mismatch, network).  Returns ``{"ok", "status", "message", ...plan}`` with ``status`` in
    ``dry-run | cancelled | needs-agreement | installed | installed-restart | already-installed |
    unsupported | failed``.
    """
    version: Optional[str] = kw.get("version")
    arch: Optional[str] = kw.get("arch")
    interactive = bool(kw.get("interactive", False))
    accept = bool(kw.get("accept_package_agreements", False))
    dry_run = bool(kw.get("dry_run", False))
    fetch: Fetch = kw.get("fetch") or default_fetch
    fetch_stream = kw.get("fetch_stream")
    index = kw.get("index")
    cache = Path(kw["cache_dir"]) if kw.get("cache_dir") else default_cache_dir()
    run_lindos: Callable[[List[str], Dict[str, str]], int] = kw.get("run_lindos") or _default_run_lindos
    which: Callable[[str], Optional[str]] = kw.get("which") or shutil.which
    confirm: Callable[..., bool] = kw.get("confirm") or _ask_nobody
    out: Callable[[str], None] = kw.get("out") or (lambda text: None)
    depth = int(kw.get("_depth", 0))
    chain: Tuple[str, ...] = tuple(kw.get("_chain", ()))

    _refuse_store_id(package_id)
    manifest = kw.get("manifest")
    if manifest is None:
        manifest = load_manifest(package_id, version, fetch=fetch, index=index, cache_dir=cache)
    summary = _manifest_summary(manifest)
    pid = summary["id"] or package_id
    inst = select_installer(manifest, arch=arch, locale=kw.get("locale"))
    inst["LindosPackageIdentifier"] = pid
    effective = inst["EffectiveInstallerType"]

    from .prefix import prefix_path, safe_slug  # local: prefix imports lindos-core lazily

    slug = safe_slug(kw.get("prefix") or summary["name"] or pid)
    prefix_dir = prefix_path(slug)
    tokens = [] if effective in _MSIX_TYPES or effective == "portable" else installer_switches(inst, interactive=interactive)
    dl_dir = cache / "downloads" / _safe_path_token(pid) / _safe_path_token(summary["version"] or "latest")
    planned_file = dl_dir / _installer_file_name(inst, _s(inst.get("InstallerUrl")), _s(inst.get("InstallerSha256")))
    lindos_run = _lindos_run_path(which)
    plan: Dict[str, Any] = dict(summary)
    plan.update({
        "installer": _installer_summary(inst),
        "prefix": slug,
        "prefix_path": str(prefix_dir),
        "switches": tokens,
        "file": str(planned_file),
        "argv": [lindos_run, "--prefix", slug, "--", str(planned_file)] + tokens
        if effective != "portable" and inst["BaseInstallerType"] != "zip" else [],
        "dependencies": _dependencies(inst),
        "notes": installer_notes(manifest, inst, interactive=interactive),
        "already_installed": _installed_version(pid, slug),
    })
    if plan["already_installed"]:
        plan["notes"].insert(0, f"Version {plan['already_installed']} is already installed in this C:\\ drive; it will "
                                "be installed again over it.")

    def result(status: str, ok: bool, message: str, **extra: Any) -> Dict[str, Any]:
        res = dict(plan)
        res.update({"ok": ok, "status": status, "message": message})
        res.update(extra)
        return res

    if dry_run:
        return result("dry-run", True, "Nothing was downloaded or run (--dry-run).")
    out(format_plan(plan))
    if not accept:
        question = (f"Install {plan['name']} {plan['version']} into the C:\\ drive '{slug}'? You agree to the licence "
                    "terms shown above.")
        if not confirm(question, False):
            if depth == 0 and confirm is _ask_nobody:
                return result("needs-agreement", False, "Not installed: confirm by answering 'yes', or add "
                                                        "--accept-package-agreements.")
            return result("cancelled", False, "Not installed (cancelled).")

    dep_results: List[Dict[str, Any]] = []
    for dep in plan["dependencies"]["packages"]:
        dep_id = dep["id"]
        if _cf(dep_id) in {_cf(x) for x in chain + (pid,)}:
            log.warning("Dependency loop at %s - skipped.", dep_id)
            continue
        have = _installed_version(dep_id, slug)
        if have and (not dep["min_version"] or compare_versions(have, dep["min_version"]) >= 0):
            dep_results.append({"id": dep_id, "status": "already-installed", "ok": True, "version": have})
            continue
        if depth + 1 > MAX_DEPENDENCY_DEPTH:
            return result("failed", False, f"Dependencies nest too deeply at {dep_id}; stopped.")
        if not accept and not confirm(f"{plan['name']} needs {dep_id}. Install it into the same C:\\ drive first?",
                                      True):
            out(f"Skipping {dep_id} - {plan['name']} may not work without it.")
            continue
        sub_kw = dict(kw)
        sub_kw.update({"version": None, "arch": None, "prefix": slug, "manifest": None, "_depth": depth + 1,
                       "_chain": chain + (pid,), "index": index, "interactive": False})
        try:
            dep_res = install(dep_id, **sub_kw)
        except WingetError as exc:
            return result("failed", False, f"The dependency {dep_id} could not be installed: {exc}",
                          dependency_results=dep_results)
        dep_results.append(dep_res)
        if not dep_res.get("ok"):
            return result("failed", False, f"The dependency {dep_id} was not installed ({dep_res.get('message')}); "
                                           f"{plan['name']} was not installed either.", dependency_results=dep_results)

    out(f"Downloading from {plan['installer']['host']} ...")
    path = download(inst, dl_dir, fetch_stream=fetch_stream)
    out("Download verified (SHA-256 matches the winget manifest).")

    if effective == "portable":
        res = _install_portable(manifest, inst, path, slug, prefix_dir, which=which, out=out, cache=cache)
        status = "installed"
        message = res["message"]
        apps = res["apps"]
        exit_code: Optional[int] = 0
    else:
        target = path
        if inst["BaseInstallerType"] == "zip":
            target = _unpack_nested(inst, path, cache)
        argv = [lindos_run, "--prefix", slug, "--", str(target)] + tokens
        before = _apps_snapshot()
        result_file = cache / "run-results" / f"{_safe_path_token(pid)}-{os.getpid()}.json"
        _unlink(result_file)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["LINDOS_RUN_RESULT"] = str(result_file)
        out("Running the installer through lindos-run ...")
        rc = run_lindos(argv, env)
        run_info = _read_json(result_file)
        _unlink(result_file)
        exit_code = run_info.get("exit_code") if isinstance(run_info.get("exit_code"), int) else None
        if exit_code is not None:
            status, message = exit_code_outcome(inst, exit_code)
        elif rc == 0:
            status, message = "installed", "installed"
        elif rc == 3:
            status, message = "unsupported", "lindos-run explained why this package cannot be installed (see above)"
        else:
            status, message = "failed", (f"the installer did not finish successfully (lindos-run exit code {rc}); the "
                                         f"log is in ~/.local/state/lindos/")
        apps = _tag_apps(slug, before, manifest) if status.startswith("installed") or status == "already-installed" \
            else []
        plan["argv"] = argv
    ok = status in ("installed", "installed-restart", "already-installed")
    if ok:
        ledger = _ledger_load()
        ledger[f"{slug}/{pid}"] = {
            "id": pid, "name": plan["name"], "version": plan["version"], "prefix": slug,
            "installer_type": effective, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "apps": apps,
        }
        try:
            _ledger_save(ledger)
        except OSError as exc:
            log.warning("cannot record the installation: %s", exc)
    text = f"{plan['name']} {plan['version']}: {message}."
    return result(status, ok, text[0].upper() + text[1:], file=str(path), exit_code=exit_code, apps=apps,
                  dependency_results=dep_results)


def _unpack_nested(inst: Dict[str, Any], archive: Path, cache: Path) -> Path:
    files = [_dict(x) for x in _list(inst.get("NestedInstallerFiles"))]
    if len(files) != 1 or not _s(files[0].get("RelativeFilePath")):
        raise WingetError("The manifest must name exactly one installer inside the zip file; it does not.")
    dest = cache / "extract" / archive.stem
    if dest.exists():
        shutil.rmtree(dest)
    _extract_zip(archive, dest)
    target = _find_ci(dest, _s(files[0].get("RelativeFilePath")))
    if target is None or not target.is_file():
        raise WingetError(f"The zip file does not contain {_s(files[0].get('RelativeFilePath'))} as the manifest says.")
    return target


def _install_portable(manifest: Dict[str, Any], inst: Dict[str, Any], path: Path, slug: str, prefix_dir: Path, *,
                      which: Callable[[str], Optional[str]], out: Callable[[str], None], cache: Path) -> Dict[str, Any]:
    from .icons import GENERIC_ICON, install_app_icon
    from .lnk import unix_to_windows
    from .scan import FoundApp, register_app, write_desktop_file

    pid = _s(manifest.get("PackageIdentifier"))
    user = _prefix_user(prefix_dir)
    packages_root = prefix_dir / "drive_c" / "users" / user / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    pkg_dir = packages_root / f"{_check_id(pid)}_winget"
    staging = packages_root / f".{pkg_dir.name}.new"
    if staging.exists():
        shutil.rmtree(staging)
    targets: List[Tuple[Path, str]] = []
    skipped: List[str] = []
    if inst["BaseInstallerType"] == "zip":
        _extract_zip(path, staging)
        for item in _list(inst.get("NestedInstallerFiles")):
            item = _dict(item)
            rel = _s(item.get("RelativeFilePath"))
            if not rel.lower().endswith(".exe"):
                skipped.append(rel)
                continue
            found = _find_ci(staging, rel)
            if found is None or not found.is_file():
                raise WingetError(f"The archive does not contain {rel} as the manifest says.")
            targets.append((found, _s(item.get("PortableCommandAlias"))))
        if not targets:
            raise WingetError("The manifest names no .exe program inside the archive; Lindos only sets up .exe "
                              "programs from portable packages.")
    else:
        staging.mkdir(parents=True, exist_ok=True)
        dest = staging / path.name
        shutil.copy2(path, dest)
        alias = ""
        for cmd in _list(inst.get("Commands")):
            if _s(cmd):
                alias = _s(cmd)
                break
        targets.append((dest, alias))
    rel_targets = [(t.relative_to(staging), alias) for t, alias in targets]
    if pkg_dir.exists():
        old = packages_root / f".{pkg_dir.name}.old"
        if old.exists():
            shutil.rmtree(old)
        os.replace(pkg_dir, old)
        os.replace(staging, pkg_dir)
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(staging, pkg_dir)
    apps: List[str] = []
    commands: List[str] = []
    try:
        c = core()
        db = c.apps_db_load()
        db = db if isinstance(db, dict) else {}
    except CoreMissing:
        c, db = None, {}
    name = _s(manifest.get("PackageName")) or pid
    for rel, alias in rel_targets:
        exe = pkg_dir / rel
        label = name if len(rel_targets) == 1 else (alias or exe.stem)
        win = unix_to_windows(exe, prefix_dir) or ""
        gui = _pe_subsystem(exe) == 2
        if c is None:
            continue
        app = FoundApp(name=label, exe=exe, source=str(exe), icon_source=exe, win_exe=win)
        app_slug = register_app(app, prefix_slug=slug, runner="wine", kind="app", db=db, save=False)
        record = db[app_slug]
        record.update({"source_file": record.get("source", ""), "source": "winget", "winget_id": pid,
                       "winget_version": _s(manifest.get("PackageVersion")), "portable": True,
                       "portable_alias": alias or exe.stem, "console": not gui})
        if gui:
            icon, icon_file = install_app_icon(exe, app_slug, which=which)
            desktop = write_desktop_file(app_slug, name=label, exe=exe, prefix_slug=slug, runner="wine", icon=icon)
            record.update({"icon": icon or GENERIC_ICON, "icon_file": str(icon_file) if icon_file else "",
                           "desktop": str(desktop)})
        else:
            commands.append(f'lindos-run --prefix {slug} "{exe}"')
        apps.append(app_slug)
    if c is not None:
        c.apps_db_save(db)
    message = f"unpacked into {pkg_dir}"
    if commands:
        message += ". Command-line programs run in a terminal with: " + "; ".join(commands)
    if skipped:
        message += ". Skipped (not .exe programs): " + ", ".join(skipped)
    return {"apps": apps, "message": message, "dir": str(pkg_dir)}


def list_installed(*, cache_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Packages installed with ``lindos-compat winget install`` (ledger + APPS_DB), with the newest
    catalogue version when a saved index is available (no network access)."""
    items: Dict[str, Dict[str, Any]] = {}
    for key, rec in _ledger_load().items():
        items[key] = {"id": _s(rec.get("id")), "name": _s(rec.get("name")), "version": _s(rec.get("version")),
                      "prefix": _s(rec.get("prefix")), "installed_at": _s(rec.get("installed_at")),
                      "apps": [a for a in _list(rec.get("apps")) if isinstance(a, str)]}
    try:
        _c, db = _apps_db()
    except Exception:  # noqa: BLE001 - CoreMissing or an unreadable db
        db = {}
    for slug, rec in db.items():
        if not isinstance(rec, dict) or rec.get("source") != "winget" or not _s(rec.get("winget_id")):
            continue
        key = f"{_s(rec.get('prefix'))}/{_s(rec.get('winget_id'))}"
        item = items.setdefault(key, {"id": _s(rec.get("winget_id")), "name": _s(rec.get("name")),
                                      "version": _s(rec.get("winget_version")), "prefix": _s(rec.get("prefix")),
                                      "installed_at": _s(rec.get("installed_at")), "apps": []})
        if slug not in item["apps"]:
            item["apps"].append(slug)
    con = _cached_index(cache_dir)
    for item in items.values():
        item["latest"] = ""
        item["update_available"] = None
        if con is None:
            continue
        row = con.execute("SELECT latest_version FROM packages WHERE id = ?", (item["id"],)).fetchone()
        if row and row[0]:
            item["latest"] = str(row[0])
            item["update_available"] = compare_versions(item["latest"], item["version"]) > 0
    if con is not None:
        con.close()
    return sorted(items.values(), key=lambda x: (_cf(x["name"]), x["prefix"]))


def index_info(con: sqlite3.Connection, cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Package count and freshness of an opened index (for ``winget update-index``)."""
    count = con.execute("SELECT count(*) FROM packages").fetchone()[0]
    meta = _read_json((Path(cache_dir) if cache_dir else default_cache_dir()) / "index" / "index.json")
    return {"packages": int(count), "last_modified": _s(meta.get("last_modified")), "etag": _s(meta.get("etag")),
            "checked": meta.get("checked"), "url": INDEX_URL}
