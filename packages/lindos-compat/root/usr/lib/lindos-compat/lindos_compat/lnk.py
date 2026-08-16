"""Minimal Windows Shell Link (``.lnk``) parser -- [MS-SHLLINK].

Only what Lindos needs: the link target (LinkInfo → LocalBasePath + CommonPathSuffix,
or the EnvironmentVariableDataBlock, or the relative-path StringData), plus the
working directory, command-line arguments and icon location.  The LinkTargetIDList
is skipped (we do not walk shell item IDs).

Also provides helpers to map ``C:\\...`` paths onto a Wine prefix (``drive_c``),
resolving case-insensitively because Linux file systems are case-sensitive while the
Windows program that wrote the shortcut is not.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "LnkError",
    "LnkInfo",
    "parse_lnk",
    "parse_lnk_bytes",
    "expand_windows_env",
    "windows_to_unix",
    "unix_to_windows",
    "is_windows_path",
    "resolve_lnk_target",
    "HAS_LINK_TARGET_ID_LIST",
    "HAS_LINK_INFO",
    "HAS_NAME",
    "HAS_RELATIVE_PATH",
    "HAS_WORKING_DIR",
    "HAS_ARGUMENTS",
    "HAS_ICON_LOCATION",
    "IS_UNICODE",
]

# LinkFlags (MS-SHLLINK 2.1.1)
HAS_LINK_TARGET_ID_LIST = 0x00000001
HAS_LINK_INFO = 0x00000002
HAS_NAME = 0x00000004
HAS_RELATIVE_PATH = 0x00000008
HAS_WORKING_DIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
HAS_ICON_LOCATION = 0x00000040
IS_UNICODE = 0x00000080
HAS_EXP_STRING = 0x00000200

# LinkInfoFlags (2.3)
VOLUME_ID_AND_LOCAL_BASE_PATH = 0x00000001
COMMON_NETWORK_RELATIVE_LINK_AND_PATH_SUFFIX = 0x00000002

# ExtraData block signatures (2.5)
ENVIRONMENT_PROPS_SIG = 0xA0000001

HEADER_SIZE = 0x4C
LINK_CLSID = bytes.fromhex("0114020000000000C000000000000046")


class LnkError(ValueError):
    """The file is not a Windows shortcut we can read."""


@dataclass
class LnkInfo:
    """Parsed shortcut fields.  Paths are Windows-style (``C:\\Program Files\\...``)."""

    path: str = ""
    target: str = ""  # best-effort absolute Windows path of the target
    local_base_path: str = ""
    common_path_suffix: str = ""
    relative_path: str = ""
    working_dir: str = ""
    arguments: str = ""
    icon_location: str = ""
    icon_index: int = 0
    description: str = ""
    env_target: str = ""  # from EnvironmentVariableDataBlock (may contain %VARS%)
    link_flags: int = 0
    file_attributes: int = 0
    show_command: int = 1
    is_unicode: bool = False
    is_directory: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)

    @property
    def target_is_exe(self) -> bool:
        return self.target.lower().endswith(".exe")


# ---------------------------------------------------------------------------
# Low level readers
# ---------------------------------------------------------------------------


def _read_cstr(data: bytes, offset: int, *, unicode: bool = False) -> str:
    """Read a NUL-terminated string starting at ``offset``."""
    if offset < 0 or offset >= len(data):
        return ""
    if unicode:
        end = offset
        while end + 1 < len(data) and data[end : end + 2] != b"\x00\x00":
            end += 2
        return data[offset:end].decode("utf-16-le", errors="replace")
    end = data.find(b"\x00", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("cp1252", errors="replace")


def _read_string_data(data: bytes, offset: int, unicode: bool) -> tuple[str, int]:
    """Read a StringData item (CountCharacters + chars).  Returns (text, new_offset)."""
    if offset + 2 > len(data):
        raise LnkError("truncated StringData")
    (count,) = struct.unpack_from("<H", data, offset)
    offset += 2
    if unicode:
        raw = data[offset : offset + count * 2]
        offset += count * 2
        return raw.decode("utf-16-le", errors="replace"), offset
    raw = data[offset : offset + count]
    offset += count
    return raw.decode("cp1252", errors="replace"), offset


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_lnk_bytes(data: bytes, *, path: str = "") -> LnkInfo:
    """Parse the raw bytes of a ``.lnk`` file."""
    if len(data) < HEADER_SIZE:
        raise LnkError("file too small to be a Windows shortcut")
    (header_size,) = struct.unpack_from("<I", data, 0)
    if header_size != HEADER_SIZE:
        raise LnkError("bad ShellLinkHeader size (not a .lnk file)")
    if data[4:20] != LINK_CLSID:
        raise LnkError("bad LinkCLSID (not a .lnk file)")
    (link_flags, file_attributes) = struct.unpack_from("<II", data, 20)
    (icon_index, show_command) = struct.unpack_from("<iI", data, 56)

    info = LnkInfo(
        path=path,
        link_flags=link_flags,
        file_attributes=file_attributes,
        icon_index=icon_index,
        show_command=show_command,
        is_unicode=bool(link_flags & IS_UNICODE),
        is_directory=bool(file_attributes & 0x10),
    )
    offset = HEADER_SIZE

    # LinkTargetIDList: skip
    if link_flags & HAS_LINK_TARGET_ID_LIST:
        if offset + 2 > len(data):
            raise LnkError("truncated LinkTargetIDList")
        (id_list_size,) = struct.unpack_from("<H", data, offset)
        offset += 2 + id_list_size

    # LinkInfo
    if link_flags & HAS_LINK_INFO:
        if offset + 4 > len(data):
            raise LnkError("truncated LinkInfo")
        (link_info_size,) = struct.unpack_from("<I", data, offset)
        li = data[offset : offset + link_info_size]
        if len(li) >= 0x1C:
            (li_header_size, li_flags, _vol_off, local_base_off, _cnrl_off, suffix_off) = struct.unpack_from(
                "<IIIIII", li, 4
            )
            local_base_off_u = suffix_off_u = 0
            if li_header_size >= 0x24:
                (local_base_off_u, suffix_off_u) = struct.unpack_from("<II", li, 0x1C)
            if li_flags & VOLUME_ID_AND_LOCAL_BASE_PATH:
                if local_base_off_u:
                    info.local_base_path = _read_cstr(li, local_base_off_u, unicode=True)
                elif local_base_off:
                    info.local_base_path = _read_cstr(li, local_base_off)
            if suffix_off_u:
                info.common_path_suffix = _read_cstr(li, suffix_off_u, unicode=True)
            elif suffix_off:
                info.common_path_suffix = _read_cstr(li, suffix_off)
        offset += link_info_size

    # StringData (in this fixed order)
    unicode = info.is_unicode
    try:
        if link_flags & HAS_NAME:
            info.description, offset = _read_string_data(data, offset, unicode)
        if link_flags & HAS_RELATIVE_PATH:
            info.relative_path, offset = _read_string_data(data, offset, unicode)
        if link_flags & HAS_WORKING_DIR:
            info.working_dir, offset = _read_string_data(data, offset, unicode)
        if link_flags & HAS_ARGUMENTS:
            info.arguments, offset = _read_string_data(data, offset, unicode)
        if link_flags & HAS_ICON_LOCATION:
            info.icon_location, offset = _read_string_data(data, offset, unicode)
    except LnkError:
        # A truncated StringData section is not fatal: we may still have LinkInfo.
        offset = len(data)

    # ExtraData blocks
    while offset + 8 <= len(data):
        (block_size, signature) = struct.unpack_from("<II", data, offset)
        if block_size < 4:
            break
        block = data[offset : offset + block_size]
        if signature == ENVIRONMENT_PROPS_SIG and len(block) >= 8 + 260:
            ansi = _read_cstr(block, 8)
            uni = _read_cstr(block, 8 + 260, unicode=True) if len(block) >= 8 + 260 + 520 else ""
            info.env_target = uni or ansi
        offset += block_size

    # Best-effort target
    if info.local_base_path or info.common_path_suffix:
        base = info.local_base_path
        suffix = info.common_path_suffix
        if base and suffix and not base.endswith("\\") and not suffix.startswith("\\"):
            info.target = base + "\\" + suffix
        else:
            info.target = base + suffix
    elif info.env_target:
        info.target = expand_windows_env(info.env_target)
    elif info.relative_path:
        info.target = info.relative_path
    return info


def parse_lnk(path: str | os.PathLike[str]) -> LnkInfo:
    """Parse a ``.lnk`` file from disk."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise LnkError(f"cannot read shortcut {p}: {exc}") from exc
    info = parse_lnk_bytes(data, path=str(p))
    # Relative-path targets are relative to the shortcut's own directory.
    if info.target and not is_windows_path(info.target) and info.relative_path:
        info.extra["relative_to"] = str(p.parent)
    return info


# ---------------------------------------------------------------------------
# Windows path helpers
# ---------------------------------------------------------------------------

_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ENV_RE = re.compile(r"%([^%]+)%")


def is_windows_path(text: str) -> bool:
    """True for ``C:\\...`` style paths (also accepts forward slashes)."""
    return bool(_WIN_ABS_RE.match(text or ""))


def default_windows_env(user: str = "user") -> Dict[str, str]:
    """Windows environment variables as laid out inside a Wine prefix."""
    profile = f"C:\\users\\{user}"
    return {
        "SYSTEMDRIVE": "C:",
        "SYSTEMROOT": "C:\\windows",
        "WINDIR": "C:\\windows",
        "PROGRAMFILES": "C:\\Program Files",
        "PROGRAMFILES(X86)": "C:\\Program Files (x86)",
        "PROGRAMW6432": "C:\\Program Files",
        "COMMONPROGRAMFILES": "C:\\Program Files\\Common Files",
        "COMMONPROGRAMFILES(X86)": "C:\\Program Files (x86)\\Common Files",
        "PROGRAMDATA": "C:\\ProgramData",
        "ALLUSERSPROFILE": "C:\\ProgramData",
        "PUBLIC": "C:\\users\\Public",
        "USERPROFILE": profile,
        "HOMEDRIVE": "C:",
        "HOMEPATH": f"\\users\\{user}",
        "APPDATA": profile + "\\AppData\\Roaming",
        "LOCALAPPDATA": profile + "\\AppData\\Local",
        "TEMP": profile + "\\Temp",
        "TMP": profile + "\\Temp",
        "USERNAME": user,
    }


def expand_windows_env(text: str, env: Optional[Dict[str, str]] = None, user: str = "user") -> str:
    """Expand ``%VAR%`` references using Wine's default Windows layout."""
    table = {k.upper(): v for k, v in default_windows_env(user).items()}
    if env:
        table.update({k.upper(): v for k, v in env.items()})

    def repl(m: "re.Match[str]") -> str:
        return table.get(m.group(1).upper(), m.group(0))

    return _ENV_RE.sub(repl, text or "")


def _ci_lookup(directory: Path, name: str) -> Optional[Path]:
    """Return ``directory/name`` resolving ``name`` case-insensitively."""
    candidate = directory / name
    if candidate.exists() or candidate.is_symlink():
        return candidate
    lname = name.lower()
    try:
        for entry in os.listdir(directory):
            if entry.lower() == lname:
                return directory / entry
    except OSError:
        return None
    return None


def windows_to_unix(win_path: str, prefix: str | os.PathLike[str], *, must_exist: bool = False) -> Optional[Path]:
    """Map a Windows path inside a Wine prefix to the real path on disk.

    ``C:\\Program Files\\Foo\\foo.exe`` → ``<prefix>/drive_c/Program Files/Foo/foo.exe``.
    Other drive letters go through ``<prefix>/dosdevices/<letter>:``.  Components
    are matched case-insensitively.  Returns ``None`` for UNC or unmappable paths,
    or (with ``must_exist``) when the file is not there.
    """
    if not win_path:
        return None
    text = expand_windows_env(win_path.strip().strip('"'))
    text = text.replace("/", "\\")
    if text.startswith("\\\\"):
        return None  # UNC path
    prefix_path = Path(prefix)
    if not is_windows_path(text) and text[1:2] != ":":
        return None
    drive = text[0].lower()
    rest = text[2:].lstrip("\\")
    if drive == "c":
        base = prefix_path / "drive_c"
    else:
        base = prefix_path / "dosdevices" / f"{drive}:"
    current = base
    parts = [p for p in rest.split("\\") if p and p != "."]
    for i, part in enumerate(parts):
        found = _ci_lookup(current, part)
        if found is None:
            if must_exist:
                return None
            # keep the remaining components verbatim
            return current.joinpath(*parts[i:])
        current = found
    return current


def unix_to_windows(path: str | os.PathLike[str], prefix: str | os.PathLike[str]) -> Optional[str]:
    """Inverse of :func:`windows_to_unix` for files below ``<prefix>/drive_c``."""
    try:
        rel = Path(path).resolve().relative_to(Path(prefix).resolve() / "drive_c")
    except (ValueError, OSError):
        return None
    return "C:\\" + "\\".join(rel.parts)


def resolve_lnk_target(lnk_path: str | os.PathLike[str], prefix: str | os.PathLike[str]) -> Optional[Path]:
    """Parse a shortcut and return the target's real path inside ``prefix`` (or None)."""
    info = parse_lnk(lnk_path)
    if not info.target:
        return None
    if is_windows_path(info.target):
        return windows_to_unix(info.target, prefix, must_exist=True)
    # relative path: relative to the shortcut's directory
    candidate = Path(lnk_path).parent / info.target.replace("\\", "/")
    if candidate.exists():
        return candidate
    return None
