"""Windows-executable helpers shared by lindos-compat / lindos-settings (SPEC §4.8, §9).

* :func:`analyze_exe` — minimal PE parse (``MZ`` → ``e_lfanew`` → ``PE\\0\\0`` → Machine,
  Characteristics, Subsystem) plus a marker scan of the first 4 MB (installer engines, game
  engines, version-info ``ProductName``/``CompanyName``).  Pure Python, no third-party deps,
  works on any OS.
* :func:`slugify` — stable ASCII slugs for prefixes / ``.desktop`` ids.
* :func:`apps_db_load` / :func:`apps_db_save` — ``~/.local/share/lindos/apps.json``
  (``{slug: {name, exe, prefix, runner, installed_at, kind}}``).
* :func:`choose_runner` — SPEC §9 rules: games & unknown → ``umu`` (Proton) if available else
  ``wine``; installers/apps/msi → ``wine``; creator recipes → ``bottles`` when installed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import struct
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import config as lconfig
from . import paths

log = logging.getLogger("lindos.compat")

KINDS = ("installer", "app", "game", "msi", "unknown")
ARCHES = ("x86", "x64", "unknown")
INSTALLER_TYPES = ("nsis", "inno", "installshield", "msi", "wix", "squirrel")
RUNNERS = ("umu", "wine", "bottles")

SCAN_BYTES = 4 * 1024 * 1024          # SPEC: scan the first 4 MB for markers
HASH_FULL_LIMIT = 64 * 1024 * 1024    # hash whole file up to this size (see sha256_prefix docs)
_MSI_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MACHINES = {0x014C: "x86", 0x8664: "x64"}
_SUBSYSTEMS = {2: "gui", 3: "console", 1: "native"}
IMAGE_FILE_DLL = 0x2000

#: (marker bytes, installer_type) — order matters: the first hit wins.
_INSTALLER_MARKERS: List[Tuple[bytes, str]] = [
    (b"Nullsoft.NSIS", "nsis"), (b"NullsoftInst", "nsis"), (b"Nullsoft Install System", "nsis"),
    (b"Inno Setup", "inno"), (b"InnoSetupVersion", "inno"), (b"JR.Inno.Setup", "inno"),
    (b"InstallShield", "installshield"),
    (b"WixToolset", "wix"), (b"Windows Installer XML", "wix"), (b"wixstdba", "wix"), (b"WixBundle", "wix"),
    (b"SquirrelSetup", "squirrel"), (b"Squirrel.Windows", "squirrel"), (b"squirrel.exe", "squirrel"),
]
#: (marker bytes, weight) — game engines / gaming runtimes
_GAME_MARKERS: List[Tuple[bytes, int]] = [
    (b"UnityPlayer.dll", 3), (b"UnityEngine", 3), (b"Unity Technologies", 2),
    (b"UnrealEngine", 3), (b"Unreal Engine", 3), (b"UE4Game", 3), (b"UE5", 1), (b"Epic Games", 1),
    (b"steam_api.dll", 3), (b"steam_api64.dll", 3), (b"Steamworks", 2), (b"GameOverlayRenderer", 2),
    (b"GameMaker", 3), (b"YoYo Games", 3), (b"Godot Engine", 3), (b"MonoGame", 3), (b"FNA.dll", 3),
    (b"CryEngine", 3), (b"Source Engine", 2), (b"RPG Maker", 3), (b"Ren'Py", 3), (b"renpy", 2),
    (b"xinput1_3.dll", 1), (b"XINPUT1_4.dll", 1), (b"xinput1_4.dll", 1), (b"dinput8.dll", 1),
    (b"d3d9.dll", 1), (b"d3d11.dll", 1), (b"d3d12.dll", 1), (b"dxgi.dll", 1), (b"vulkan-1.dll", 1),
    (b"XAudio2", 1), (b"fmod", 1), (b"FMOD", 1), (b"EasyAntiCheat", 3), (b"BattlEye", 3),
    (b"GalaxyPeer", 2), (b"Galaxy64.dll", 2), (b"EOSSDK-Win64-Shipping.dll", 2),
]
_INSTALLER_NAME_RE = re.compile(r"(^|[^a-z])(setup|install|installer|updater|update|redist|vc_?redist)([^a-z]|$)", re.I)
_UNINSTALLER_NAME_RE = re.compile(r"(^|[^a-z])(unins\d*|uninstall|uninstaller)([^a-z]|$)", re.I)


# --- data model ----------------------------------------------------------------------------
@dataclass
class ExeInfo:
    """What :func:`analyze_exe` learned about a Windows program file.

    ``kind`` ∈ installer|app|game|msi|unknown · ``arch`` ∈ x86|x64|unknown ·
    ``installer_type`` ∈ nsis|inno|installshield|msi|wix|squirrel|None.
    ``sha256_prefix`` is the first 16 hex chars of the SHA-256 (see :func:`file_digest`).
    Extra fields (``subsystem``, ``is_pe``, ``is_dll``, ``size``, ``markers``) carry more detail for
    ``lindos-run --info``.
    """

    path: str
    name: str
    kind: str = "unknown"
    arch: str = "unknown"
    installer_type: Optional[str] = None
    product: str = ""
    company: str = ""
    sha256_prefix: str = ""
    subsystem: str = ""
    is_pe: bool = False
    is_dll: bool = False
    size: int = 0
    markers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def display_name(self) -> str:
        """Product name when known, else the file name without extension."""
        return self.product.strip() or os.path.splitext(self.name)[0]

    @property
    def slug(self) -> str:
        return app_slug(self)

    def describe(self) -> str:
        """Multi-line human summary (``lindos-run --info``)."""
        lines = [
            f"File:      {self.path}",
            f"Kind:      {self.kind}" + (f" ({self.installer_type})" if self.installer_type else ""),
            f"Arch:      {self.arch}" + (f", {self.subsystem}" if self.subsystem else ""),
        ]
        if self.product:
            lines.append(f"Product:   {self.product}")
        if self.company:
            lines.append(f"Company:   {self.company}")
        lines.append(f"Size:      {self.size} bytes")
        if self.sha256_prefix:
            lines.append(f"SHA-256:   {self.sha256_prefix}…")
        if self.markers:
            lines.append("Markers:   " + ", ".join(self.markers))
        return "\n".join(lines)


# --- low level helpers ---------------------------------------------------------------------
def _u16(buf: bytes, off: int) -> Optional[int]:
    return struct.unpack_from("<H", buf, off)[0] if 0 <= off and off + 2 <= len(buf) else None


def _u32(buf: bytes, off: int) -> Optional[int]:
    return struct.unpack_from("<I", buf, off)[0] if 0 <= off and off + 4 <= len(buf) else None


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _read_versioninfo_string(buf: bytes, key: str) -> str:
    """Pull ``key`` (e.g. ``ProductName``) out of a VS_VERSION_INFO StringTable in *buf*."""
    needle = _utf16(key) + b"\x00\x00"
    start = 0
    while True:
        idx = buf.find(needle, start)
        if idx < 0:
            return ""
        pos = idx + len(needle)
        # value is 32-bit aligned after the key
        pos += (-pos) % 4
        end = buf.find(b"\x00\x00", pos)
        if end < 0:
            return ""
        # keep alignment of UTF-16 code units
        if (end - pos) % 2:
            end += 1
        raw = buf[pos:end]
        try:
            value = raw.decode("utf-16-le", errors="ignore").strip("\x00 \t\r\n")
        except UnicodeDecodeError:  # pragma: no cover - errors="ignore" never raises
            value = ""
        value = "".join(ch for ch in value if ch.isprintable()).strip()
        if value and len(value) <= 128:
            return value
        start = idx + 2


def file_digest(path: str, limit: int = HASH_FULL_LIMIT) -> str:
    """Hex SHA-256 identity of *path*.

    Files up to *limit* bytes are hashed completely.  Bigger files (multi-GB game installers)
    hash the first and last 4 MB plus the size — a fast, stable identity for the apps DB, not a
    download checksum.  Returns ``""`` when unreadable.
    """
    h = hashlib.sha256()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size <= limit:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            else:
                h.update(fh.read(SCAN_BYTES))
                fh.seek(max(0, size - SCAN_BYTES))
                h.update(fh.read(SCAN_BYTES))
                h.update(str(size).encode("ascii"))
    except OSError:
        return ""
    return h.hexdigest()


def _scan_markers(head: bytes) -> Tuple[Optional[str], int, List[str]]:
    """Return ``(installer_type, game_score, marker_names)`` for the first bytes of a file."""
    found: List[str] = []
    installer_type: Optional[str] = None
    for marker, itype in _INSTALLER_MARKERS:
        if marker in head or _utf16(marker.decode("ascii")) in head:
            found.append(marker.decode("ascii"))
            if installer_type is None:
                installer_type = itype
    game_score = 0
    lowered = head.lower()
    for marker, weight in _GAME_MARKERS:
        if marker in head or marker.lower() in lowered:
            found.append(marker.decode("ascii"))
            game_score += weight
    return installer_type, game_score, found


# --- analysis ------------------------------------------------------------------------------
def analyze_exe(path: str) -> ExeInfo:
    """Inspect a ``.exe`` / ``.msi`` / other Windows file (see module docs).

    Never raises for a strange file: unreadable or non-PE files come back with
    ``kind="unknown"`` (``"msi"`` for OLE compound files / ``.msi`` extension).  Raises
    ``FileNotFoundError`` only when *path* does not exist.
    """
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    info = ExeInfo(path=os.path.abspath(path), name=name)
    try:
        info.size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(SCAN_BYTES)
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        head = b""

    if head.startswith(_MSI_MAGIC) or ext == ".msi":
        info.kind, info.installer_type = "msi", "msi"
        if b"Intel64" in head or _utf16("x64") in head or b";x64" in head:
            info.arch = "x64"
        elif b"Intel;" in head or _utf16("Intel;") in head:
            info.arch = "x86"
        info.product = _read_versioninfo_string(head, "ProductName") or _ole_summary_title(head)
        info.company = _read_versioninfo_string(head, "CompanyName")
        info.sha256_prefix = file_digest(path)[:16]
        return info

    if head[:2] == b"MZ":
        e_lfanew = _u32(head, 0x3C)
        if e_lfanew is not None and head[e_lfanew:e_lfanew + 4] == b"PE\x00\x00":
            info.is_pe = True
            machine = _u16(head, e_lfanew + 4)
            characteristics = _u16(head, e_lfanew + 22) or 0
            info.arch = _MACHINES.get(machine or 0, "unknown")
            info.is_dll = bool(characteristics & IMAGE_FILE_DLL)
            opt = e_lfanew + 24
            magic = _u16(head, opt)
            if magic in (0x10B, 0x20B):
                info.subsystem = _SUBSYSTEMS.get(_u16(head, opt + 68) or 0, "")

    installer_type, game_score, markers = _scan_markers(head)
    info.markers = markers
    info.installer_type = installer_type
    info.product = _read_versioninfo_string(head, "ProductName")
    info.company = _read_versioninfo_string(head, "CompanyName")
    info.sha256_prefix = file_digest(path)[:16]

    stem = os.path.splitext(name)[0]
    if info.is_dll:
        info.kind = "unknown"
    elif installer_type is not None:
        info.kind = "installer"
    elif _UNINSTALLER_NAME_RE.search(stem):
        info.kind = "app"
    elif _INSTALLER_NAME_RE.search(stem) or _INSTALLER_NAME_RE.search(info.product):
        info.kind = "installer"
    elif game_score >= 3:
        info.kind = "game"
    elif info.is_pe and info.subsystem in ("gui", "console"):
        info.kind = "app"
    elif ext in (".bat", ".cmd", ".lnk", ".com"):
        info.kind = "app"
    else:
        info.kind = "unknown"
    return info


def _ole_summary_title(head: bytes) -> str:
    """Best-effort ``Title`` from an MSI's SummaryInformation (ASCII heuristic)."""
    m = re.search(rb"([A-Za-z0-9][A-Za-z0-9 ._+()-]{2,80}) (Setup|Installer|Installation)", head)
    return m.group(0).decode("ascii", errors="ignore").strip() if m else ""


# --- slugs -----------------------------------------------------------------------------------
def slugify(name: str, max_len: int = 48) -> str:
    """ASCII slug: ``"Notepad++ 8.6 Setup"`` → ``"notepad-plus-plus-8-6-setup"``.

    Accents are stripped (NFKD), ``+`` becomes ``plus``, ``&`` becomes ``and``, anything that
    is not ``[a-z0-9]`` becomes ``-`` (collapsed, trimmed).  Never returns an empty string
    (``"app"`` fallback).
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("+", " plus ").replace("&", " and ").replace("@", " at ")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "app"


_SLUG_NOISE_RE = re.compile(r"-(setup|installer|install|x64|x86|win64|win32|64bit|32bit|amd64|online|offline|web|full)$")
_SLUG_VERSION_RE = re.compile(r"-v?\d+(-\d+)*$")


def app_slug(info: ExeInfo) -> str:
    """Prefix / desktop-id slug for an app: product name if known, else the file stem,
    with trailing ``-setup``/``-x64``/version noise removed."""
    base = info.product.strip() or os.path.splitext(info.name)[0]
    slug = slugify(base)
    for _ in range(4):
        new = _SLUG_NOISE_RE.sub("", slug)
        new = _SLUG_VERSION_RE.sub("", new)
        if new == slug or not new:
            break
        slug = new
    return slug or "app"


# --- apps database ---------------------------------------------------------------------------
def apps_db_load() -> Dict[str, Dict[str, Any]]:
    """``~/.local/share/lindos/apps.json`` → ``{slug: {name, exe, prefix, runner, installed_at, kind}}``."""
    data = lconfig.read_json(paths.apps_db())
    apps = data.get("apps", data) if isinstance(data, dict) else {}
    return {k: v for k, v in apps.items() if isinstance(v, dict)} if isinstance(apps, dict) else {}


def apps_db_save(db: Dict[str, Dict[str, Any]]) -> None:
    """Atomically write the apps DB (``{slug: {...}}``)."""
    if not isinstance(db, dict):
        raise TypeError("apps db must be a dict of slug -> entry")
    lconfig.atomic_write_json(paths.apps_db(), {k: v for k, v in db.items() if isinstance(v, dict)})


def apps_db_upsert(slug: str, entry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Insert/update one entry (merging over an existing one) and save."""
    db = apps_db_load()
    merged = dict(db.get(slug, {}))
    merged.update(entry)
    db[slug] = merged
    apps_db_save(db)
    return db


def apps_db_remove(slug: str) -> bool:
    """Remove *slug*; True when it existed."""
    db = apps_db_load()
    if slug not in db:
        return False
    del db[slug]
    apps_db_save(db)
    return True


# --- runner choice ---------------------------------------------------------------------------
def umu_available() -> bool:
    return shutil.which("umu-run") is not None


def wine_available() -> bool:
    return any(shutil.which(b) for b in ("wine", "wine64", "wine-stable", "wine-staging"))


def bottles_installed() -> bool:
    """Bottles present as a Flatpak (``com.usebottles.bottles``) or on PATH.  Guarded."""
    if shutil.which("bottles") or shutil.which("bottles-cli"):
        return True
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    try:
        proc = subprocess.run([flatpak, "info", "com.usebottles.bottles"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15, check=False)
        return proc.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return default
    return getattr(config, key, default)


def choose_runner(info: ExeInfo, config: Any = None) -> str:
    """``"umu"`` | ``"wine"`` | ``"bottles"`` for *info* under the SPEC §9 rules.

    * an explicit ``config["runner"]`` (umu/wine/bottles) wins when that runner is available;
    * games & unknown → ``umu`` (Proton-GE via umu-launcher) if ``umu-run`` exists, else ``wine``;
    * installers, apps, ``.msi`` → ``wine``; when Wine is missing but umu exists → ``umu``;
    * creator recipes (Adobe/Corel/Affinity vendors) → ``bottles`` when Bottles is installed.
    """
    kind = str(getattr(info, "kind", "unknown") or "unknown")
    forced = str(_cfg_get(config, "runner", "") or "").lower()
    if forced in RUNNERS:
        if forced == "umu" and umu_available():
            return "umu"
        if forced == "bottles" and bottles_installed():
            return "bottles"
        if forced == "wine":
            return "wine"
    company = str(getattr(info, "company", "") or "").lower()
    product = str(getattr(info, "product", "") or "").lower()
    creator_vendor = any(v in company or v in product for v in ("adobe", "corel", "affinity", "serif"))
    if creator_vendor and kind in ("installer", "app", "msi") and bottles_installed():
        return "bottles"
    if kind in ("game", "unknown"):
        return "umu" if umu_available() else "wine"
    if not wine_available() and umu_available():
        return "umu"
    return "wine"


__all__ = [
    "KINDS", "ARCHES", "INSTALLER_TYPES", "RUNNERS", "SCAN_BYTES", "ExeInfo", "analyze_exe",
    "file_digest", "slugify", "app_slug", "apps_db_load", "apps_db_save", "apps_db_upsert",
    "apps_db_remove", "umu_available", "wine_available", "bottles_installed", "choose_runner",
]
