"""The secrets deny-list and the single file-open path of lindos-transfer (SPEC-WINDOWS §27.3).

Every reader in this package (copy engine, registry reader, browser/Wi-Fi/Steam/wallpaper/font
readers, the hibernation check) opens source files **only** through :func:`safe_open` /
:func:`read_bytes` / :func:`read_hiberfil_header`.  Those functions

* refuse any path matched by :data:`SECRETS_DENYLIST` (account databases, DPAPI master keys,
  Credential Manager/Vault, machine and service profiles, RAM images, browser password/cookie
  stores, ...) -- the file is never opened, not even for reading a byte;
* never follow symbolic links, junctions or reparse points (``O_NOFOLLOW`` + ``lstat``);
* open regular files only (no devices/FIFOs), read-only, binary.

Firefox's ``key4.db`` + ``logins.json`` are the only deny-listed files a user may opt in to
(``--firefox-passwords``): they are NSS-encrypted, portable, and still protected by the user's
Firefox Primary Password.  ``hiberfil.sys`` may be read only by :func:`read_hiberfil_header`, and
only its first 4 KiB (the "hibr" signature), never the RAM image behind it.

Wi-Fi ``keyMaterial`` is a secret *inside* an otherwise harmless XML file; :data:`WLAN_SECRET_ELEMENTS`
names it and :mod:`lindos_transfer.wifi` strips it on parse (offline Windows profiles hold a DPAPI
blob that Lindos never decrypts).

The matcher is case-insensitive (NTFS is), separator-agnostic and works on any OS.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from functools import lru_cache
from pathlib import PurePath
from typing import BinaryIO, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

__all__ = [
    "SECRETS_DENYLIST",
    "OPT_IN_PATTERNS",
    "FIREFOX_PASSWORD_FILES",
    "WLAN_SECRET_ELEMENTS",
    "HIBERFIL_HEADER_MAX",
    "DENY_REASON",
    "SecretPathError",
    "UnsafeFileError",
    "match_denylist",
    "is_denied",
    "check",
    "safe_open",
    "read_bytes",
    "read_hiberfil_header",
    "denied_names_help",
]

PathLike = Union[str, "os.PathLike[str]"]

_ACCOUNTS = "Windows account and security databases - never read"
_KEYS = "Windows encryption keys, saved credentials or private keys - never read"
_SYSTEM = "Windows system/service account data - never read"
_MEMORY = "a copy of the computer's memory (hibernation/page file or crash dump) - never read"
_BROWSER = "browser passwords, cookies or autofill data (encrypted by Windows) - never read"
_ENC_BOOKMARKS = "encrypted browser bookmarks (use the browser's own 'Export bookmarks') - never read"
_FF_COOKIES = "Firefox cookies or old password files - never moved"
_FF_PASSWORDS = "Firefox saved passwords - moved only if you choose to"

#: (reason, case-insensitive path globs).  ``**`` = any number of folders; matched against the
#: whole path, so the rules apply wherever the Windows tree is mounted.
_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # Windows account database and LSA secrets (+ transaction logs / RegBack copies)
    (_ACCOUNTS, (
        "**/system32/config/**/sam",
        "**/system32/config/**/sam.*",
        "**/system32/config/**/sam{*",
        "**/system32/config/**/security",
        "**/system32/config/**/security.*",
        "**/system32/config/**/security{*",
        "**/ntds.dit",
    )),
    # DPAPI master keys (machine and user), Credential Manager, Windows Vault, private keys
    (_KEYS, (
        "**/system32/microsoft/protect/**",
        "**/microsoft/protect/**",
        "**/microsoft/credentials/**",
        "**/microsoft/vault/**",
        "**/microsoft/crypto/**",
        "**/microsoft/systemcertificates/my/keys/**",
    )),
    # machine / service profiles (LSA and service secrets, Windows Hello NGC containers)
    (_SYSTEM, (
        "**/config/systemprofile/**",
        "**/serviceprofiles/**",
    )),
    # RAM images: hibernation file, page/swap files, crash dumps
    (_MEMORY, (
        "**/hiberfil.sys",
        "**/pagefile.sys",
        "**/swapfile.sys",
        "**/*.dmp",
    )),
    # Chromium-family (Chrome, Edge, Brave, Opera, Vivaldi) passwords, cookies, autofill/cards, keys
    (_BROWSER, (
        "**/login data",
        "**/login data-journal",
        "**/login data for account",
        "**/login data for account-journal",
        "**/cookies",
        "**/cookies-journal",
        "**/web data",
        "**/web data-journal",
        "**/local state",
    )),
    (_ENC_BOOKMARKS, (
        "**/encryptedbookmarks",
        "**/encryptedbookmarks2",
        "**/encryptedaccountbookmarks",
        "**/encryptedaccountbookmarks2",
    )),
    # Firefox cookies and legacy password stores (never moved)
    (_FF_COOKIES, (
        "**/cookies.sqlite",
        "**/cookies.sqlite-wal",
        "**/cookies.sqlite-shm",
        "**/key3.db",
        "**/signons.sqlite",
        "**/logins-backup.json",
    )),
    # Firefox saved passwords -- only with the explicit opt-in (OPT_IN_PATTERNS)
    (_FF_PASSWORDS, (
        "**/key4.db",
        "**/logins.json",
    )),
)

#: Every path glob that is **never opened** (case-insensitive; see the module docstring).
SECRETS_DENYLIST: Tuple[str, ...] = tuple(p for _reason, pats in _GROUPS for p in pats)

_PATTERN_REASON: Dict[str, str] = {p: reason for reason, pats in _GROUPS for p in pats}

#: Deny-list entries the user may explicitly opt in to, by option name.
OPT_IN_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "firefox_passwords": ("**/key4.db", "**/logins.json"),
}

#: The two Firefox files that move together with ``--firefox-passwords``.
FIREFOX_PASSWORD_FILES: Tuple[str, ...] = ("key4.db", "logins.json")

#: XML elements that carry secrets in Windows Wi-Fi profiles (never read out of offline profiles).
WLAN_SECRET_ELEMENTS: Tuple[str, ...] = ("keyMaterial",)

#: The most :func:`read_hiberfil_header` will ever read from ``hiberfil.sys``.
HIBERFIL_HEADER_MAX = 4096

DENY_REASON = "private Windows data (passwords, keys or memory images) - Lindos never opens it"


class SecretPathError(PermissionError):
    """Raised when code tries to open a deny-listed path.  ``reason`` is user-readable."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class UnsafeFileError(OSError):
    """Raised for a symlink/junction/reparse point, a device, a FIFO or another non-regular file."""


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def _segments(path: PathLike) -> Tuple[str, ...]:
    text = os.fspath(path) if not isinstance(path, str) else path
    text = text.replace("\\", "/").lower()
    return tuple(s for s in text.split("/") if s and s != ".")


@lru_cache(maxsize=None)
def _compile(pattern: str) -> Tuple[str, ...]:
    return tuple(s for s in pattern.lower().split("/") if s)


def _seg_match(name: str, pat: str) -> bool:
    if not any(ch in pat for ch in "*?[{"):
        return name == pat
    if "{" in pat:  # literal brace (e.g. "sam{*" for SAM{GUID}.TM.blf): fnmatch treats it literally
        head, _, tail = pat.partition("{")
        return name.startswith(head + "{") and fnmatch.fnmatchcase(name[len(head) + 1:], tail or "*")
    return fnmatch.fnmatchcase(name, pat)


def _glob_match(segs: Sequence[str], pats: Sequence[str]) -> bool:
    """``**``-aware segment glob (``**`` matches zero or more segments)."""
    # iterative DP over (segment index, pattern index)
    n, m = len(segs), len(pats)
    reach = [False] * (m + 1)
    reach[0] = True
    # pattern prefix of only '**' also matches empty segments
    for j in range(1, m + 1):
        reach[j] = reach[j - 1] and pats[j - 1] == "**"
    for i in range(1, n + 1):
        new = [False] * (m + 1)
        for j in range(1, m + 1):
            p = pats[j - 1]
            if p == "**":
                new[j] = new[j - 1] or reach[j]
            else:
                new[j] = reach[j - 1] and _seg_match(segs[i - 1], p)
        reach = new
        if not any(reach):
            return False
    return reach[m]


def match_denylist(path: PathLike) -> Optional[str]:
    """Return the first :data:`SECRETS_DENYLIST` pattern matching *path* (or ``None``)."""
    segs = _segments(path)
    if not segs:
        return None
    for pattern in SECRETS_DENYLIST:
        if _glob_match(segs, _compile(pattern)):
            return pattern
    return None


def _reason_for(pattern: str) -> str:
    return _PATTERN_REASON.get(pattern, DENY_REASON)


def is_denied(path: PathLike, *, allow_firefox_passwords: bool = False) -> Optional[str]:
    """Return a user-readable reason when *path* must never be opened, else ``None``."""
    segs = _segments(path)
    if not segs:
        return None
    optin: FrozenSet[str] = frozenset(OPT_IN_PATTERNS["firefox_passwords"]) if allow_firefox_passwords else frozenset()
    for pattern in SECRETS_DENYLIST:
        if pattern in optin:
            continue
        if _glob_match(segs, _compile(pattern)):
            return _reason_for(pattern)
    return None


def check(path: PathLike, *, allow_firefox_passwords: bool = False) -> None:
    """Raise :class:`SecretPathError` when *path* is deny-listed."""
    reason = is_denied(path, allow_firefox_passwords=allow_firefox_passwords)
    if reason:
        raise SecretPathError(os.fspath(path), reason)


# --------------------------------------------------------------------------- #
# opening
# --------------------------------------------------------------------------- #
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0)
            | _O_NOFOLLOW)


def _open_regular(path: PathLike) -> BinaryIO:
    """Open *path* read-only as a regular file without following links."""
    p = os.fspath(path)
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeFileError(f"{p}: is a link/junction/reparse point (not followed)")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFileError(f"{p}: not a regular file")
    fd = os.open(p, _O_FLAGS)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise UnsafeFileError(f"{p}: not a regular file")
        if _O_FLAGS & getattr(os, "O_NONBLOCK", 0) and hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def safe_open(path: PathLike, *, allow_firefox_passwords: bool = False) -> BinaryIO:
    """Open a *source* file for reading after the deny-list and no-link checks.

    Raises :class:`SecretPathError` (deny-listed), :class:`UnsafeFileError` (link or special
    file) or ``OSError``.
    """
    check(path, allow_firefox_passwords=allow_firefox_passwords)
    return _open_regular(path)


def read_bytes(path: PathLike, limit: int, *, allow_firefox_passwords: bool = False) -> bytes:
    """Read a whole (small) source file; ``OSError(EFBIG)`` when it is larger than *limit* bytes."""
    with safe_open(path, allow_firefox_passwords=allow_firefox_passwords) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        import errno

        raise OSError(errno.EFBIG, f"file larger than {limit} bytes", os.fspath(path))
    return data


def read_hiberfil_header(path: PathLike, size: int = 4) -> bytes:
    """Read the first *size* (<= 4096) bytes of ``hiberfil.sys`` -- the only allowed read of it."""
    name = PurePath(os.fspath(path).replace("\\", "/")).name.lower()
    if name != "hiberfil.sys":
        raise ValueError("read_hiberfil_header() only reads hiberfil.sys")
    size = max(0, min(int(size), HIBERFIL_HEADER_MAX))
    with _open_regular(path) as fh:
        return fh.read(size)


def denied_names_help() -> List[str]:
    """Human-readable list of what is never transferred (used by the report)."""
    return [
        "Windows account and security databases (SAM, SECURITY)",
        "Windows encryption keys, Credential Manager and Vault",
        "system and service account data",
        "hibernation, page and swap files and crash dumps (copies of memory)",
        "browser passwords, cookies, autofill and payment data (they are encrypted by Windows)",
        "Wi-Fi passwords stored inside Windows (encrypted by Windows)",
        "Firefox saved passwords (unless you chose to move them)",
    ]
