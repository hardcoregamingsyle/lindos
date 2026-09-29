"""Windows installers that hand an app package (MSIX/APPX) to Windows -- Lindos' side of it.

Many modern Windows "installers" are small bootstrappers: they download a large ``.msix`` /
``.msixbundle`` into ``%TEMP%`` and then ask Windows to install it (``ShellExecute`` on the package,
``Add-AppxPackage``, an ``ms-appinstaller:`` link).  Wine has no app-deployment service, so without
help the shell answers "There is no Windows program configured to open this type of file" and the
bootstrapper falls back to showing the download folder.  This module is the Lindos answer
(SPEC-WINDOWS §28.4a); nothing here is ever "tried anyway" in a way that looks like success:

* **Associations inside the C:\\ drive** -- :func:`registration_reg` builds a ``.reg`` file that maps
  ``.msix .appx .msixbundle .appxbundle .msixupload .appxupload .emsix ... .appinstaller`` and the
  ``ms-appinstaller:`` scheme to a tiny handler that only *records* the path or link in
  ``C:\\ProgramData\\Lindos\\handoff.log`` and returns.  ``lindos-run`` imports it once per C:\\ drive
  (:func:`is_registered` / :func:`mark_registered`); nothing is installed at that point.
* **Safety net after the installer exits** -- :func:`find_packages` combines that queue with a
  before/after look at the C:\\ drive's Temp and Downloads folders, and :func:`assess` says for each
  package whether it is a desktop app Lindos can unpack (``role == "app"``), a component nobody needs to
  hear about, or something that cannot run (UWP/WinUI, Store-encrypted, damaged) --
  :func:`explain_text` turns the latter into ONE plain explanation.

Pure and hermetic: stdlib only, no Wine, no Linux calls; every path stays inside the C:\\ drive (or the
user's home) and nothing is ever opened in a file manager.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import get_logger, user_home
from .lnk import windows_to_unix
from .prefix import read_marker, write_marker

__all__ = [
    "HANDOFF_VERSION",
    "MARKER_KEY",
    "PROG_ID",
    "PACKAGE_SUFFIXES",
    "URI_SCHEMES",
    "QUEUE_WINDOWS_PATH",
    "Candidate",
    "Verdict",
    "handler_command",
    "uri_handler_command",
    "registration_reg",
    "prepare_registration",
    "is_registered",
    "mark_registered",
    "queue_path",
    "read_queue",
    "watch_dirs",
    "snapshot_packages",
    "find_packages",
    "assess",
    "explain_text",
    "sniff_bootstrapper",
    "human_size",
]

log = get_logger("lindos-compat.handoff")

#: bump when the registration changes: prefixes registered with an older version are re-registered
HANDOFF_VERSION = 1
#: key in the prefix marker (``.lindos.json``) that records the registered version
MARKER_KEY = "handoff"
PROG_ID = "Lindos.AppPackage"

#: every file type an installer may hand to Windows (content decides later, see msix.classify)
PACKAGE_SUFFIXES = (
    ".msix", ".appx", ".msixbundle", ".appxbundle", ".msixupload", ".appxupload",
    ".emsix", ".eappx", ".emsixbundle", ".eappxbundle", ".appinstaller",
)
URI_SCHEMES = ("ms-appinstaller",)

QUEUE_WINDOWS_PATH = "C:\\ProgramData\\Lindos\\handoff.log"
REG_DIRNAME = ".lindos-handoff"
QUEUE_MAX_BYTES = 1 << 20
QUEUE_MAX_ENTRIES = 32
SNAPSHOT_MAX_FILES = 20000
MAX_REPORTED = 3

_BOOTSTRAP_TOKENS = ("Add-AppxPackage", "AddPackageAsync", "Windows.Management.Deployment", "ms-appinstaller",
                     ".msixbundle", ".appxbundle", ".appinstaller", ".msix", ".appx")

Snapshot = Dict[str, Tuple[float, int]]


# ---------------------------------------------------------------------------
# Associations inside the C:\ drive
# ---------------------------------------------------------------------------


def handler_command() -> str:
    """The command every association runs: append the file (or link) to the queue, then return.

    ``%1`` is a file path here (paths cannot contain ``"``), so quoting keeps ``&`` and spaces harmless;
    URL protocols use :func:`uri_handler_command` instead. No program of Lindos runs inside
    Wine at this point -- the queue is read by ``lindos-run`` when the installer exits.
    """
    return f'C:\\windows\\system32\\cmd.exe /d /c echo "%1">>"{QUEUE_WINDOWS_PATH}"'


def uri_handler_command() -> str:
    """The command a URL protocol runs: record only THAT a link was handed over, never its text.

    A link may contain ``"`` or ``&``; substituted into a ``cmd.exe`` command line (as ``%1``) that
    would let a hostile link run commands, so the link text never reaches ``cmd.exe`` at all.
    """
    return f'C:\\windows\\system32\\cmd.exe /d /c echo {URI_SCHEMES[0]}:>>"{QUEUE_WINDOWS_PATH}"'


def _reg_str(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def registration_reg() -> str:
    """The ``.reg`` file (LF-separated text; :func:`prepare_registration` writes it UTF-16LE + CRLF)."""
    command = _reg_str(handler_command())
    uri_command = _reg_str(uri_handler_command())
    lines: List[str] = ["Windows Registry Editor Version 5.00", ""]
    for suffix in PACKAGE_SUFFIXES:
        lines += [f"[HKEY_CLASSES_ROOT\\{suffix}]", f'@="{PROG_ID}"', ""]
    lines += [
        f"[HKEY_CLASSES_ROOT\\{PROG_ID}]",
        '@="App package (Lindos catches it)"',
        "",
        f"[HKEY_CLASSES_ROOT\\{PROG_ID}\\shell]",
        '@="open"',
        "",
    ]
    for verb in ("open", "runas"):
        lines += [f"[HKEY_CLASSES_ROOT\\{PROG_ID}\\shell\\{verb}\\command]", f'@="{command}"', ""]
    for scheme in URI_SCHEMES:
        lines += [
            f"[HKEY_CLASSES_ROOT\\{scheme}]",
            f'@="URL:{scheme}"',
            '"URL Protocol"=""',
            "",
            f"[HKEY_CLASSES_ROOT\\{scheme}\\shell\\open\\command]",
            f'@="{uri_command}"',
            "",
        ]
    return "\n".join(lines)


def queue_path(prefix: Path) -> Path:
    return Path(prefix) / "drive_c" / "ProgramData" / "Lindos" / "handoff.log"


def prepare_registration(prefix: Path) -> Path:
    """Create the queue folder and write the ``.reg`` file (outside ``drive_c``); returns its path."""
    prefix = Path(prefix)
    queue_path(prefix).parent.mkdir(parents=True, exist_ok=True)
    out = prefix / REG_DIRNAME
    out.mkdir(parents=True, exist_ok=True)
    reg = out / "handoff.reg"
    reg.write_bytes(("\ufeff" + registration_reg().replace("\n", "\r\n") + "\r\n").encode("utf-16-le"))
    return reg


def is_registered(marker: Mapping[str, Any]) -> bool:
    try:
        return int(marker.get(MARKER_KEY) or 0) >= HANDOFF_VERSION
    except (TypeError, ValueError):
        return False


def mark_registered(prefix: Path) -> bool:
    """Remember in the prefix marker that the associations were imported.  False when it cannot be saved."""
    marker = dict(read_marker(Path(prefix)))
    marker[MARKER_KEY] = HANDOFF_VERSION
    try:
        write_marker(Path(prefix), marker)
    except OSError as exc:
        log.debug("cannot record the hand-off registration for %s: %s", prefix, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# What did the installer leave behind?
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A package (or link) an installer handed to Windows."""

    path: Optional[Path]      # None for an ms-appinstaller: link
    raw: str = ""             # the queued text (path or link)
    source: str = "temp"      # "handoff" (queue) | "temp" (before/after look)
    size: int = 0


def read_queue(prefix: Path, *, consume: bool = True) -> List[str]:
    """Lines recorded by the association handler, de-duplicated, at most :data:`QUEUE_MAX_ENTRIES`."""
    q = queue_path(prefix)
    try:
        if q.is_symlink() or not q.is_file():
            return []
        with open(q, "rb") as fh:
            raw = fh.read(QUEUE_MAX_BYTES)
    except OSError:
        return []
    if consume:
        try:
            q.unlink()
        except OSError:
            pass
    out: List[str] = []
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        text = line.strip().strip('"').strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= QUEUE_MAX_ENTRIES:
            break
    return out


def watch_dirs(prefix: Path) -> List[Tuple[Path, int]]:
    """``(folder, depth)`` pairs where installers park downloads: every user's Temp and Downloads."""
    drive_c = Path(prefix) / "drive_c"
    dirs: List[Tuple[Path, int]] = [(drive_c / "windows" / "temp", 3), (drive_c / "Temp", 3)]
    users = drive_c / "users"
    try:
        user_dirs = sorted(d for d in users.iterdir() if d.is_dir()) if users.is_dir() else []
    except OSError:
        user_dirs = []
    for u in user_dirs:
        for rel in ("Temp", "AppData/Local/Temp", "Local Settings/Temp"):
            dirs.append((u / rel, 4))
        for rel in ("Downloads", "Desktop"):
            dirs.append((u / rel, 1))
    return dirs


def snapshot_packages(prefix: Path) -> Snapshot:
    """``path -> (mtime, size)`` of every package-like file in the watched folders."""
    result: Snapshot = {}
    for root, depth in watch_dirs(prefix):
        if not root.is_dir():
            continue
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if len(Path(dirpath).parts) - base >= depth:
                dirnames[:] = []
            for name in filenames:
                if not name.lower().endswith(PACKAGE_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                result[full] = (st.st_mtime, st.st_size)
                if len(result) >= SNAPSHOT_MAX_FILES:
                    return result
    return result


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _queued_file(entry: str, prefix: Path) -> Optional[Path]:
    """The real file a queued Windows path points at -- only inside the C:\\ drive or the user's home."""
    if not entry.lower().endswith(PACKAGE_SUFFIXES):
        return None
    found = windows_to_unix(entry, prefix, must_exist=True)
    if found is None:
        return None
    try:
        real = found.resolve()
        allowed = [(Path(prefix) / "drive_c").resolve(), user_home().resolve()]
    except OSError:
        return None
    if not any(_inside(real, root) for root in allowed) or not real.is_file():
        return None
    return real


def find_packages(prefix: Path, before: Optional[Snapshot], *, started: Optional[float] = None,
                  consume: bool = True) -> List[Candidate]:
    """Packages an installer handed to Windows while it ran: the queue plus what is new in Temp/Downloads.

    ``before`` is :func:`snapshot_packages` taken before the run; when it is unknown (``None``) only files
    modified since ``started`` count.  Old leftovers that were already there are never offered again.
    """
    prefix = Path(prefix)
    found: Dict[str, Candidate] = {}
    for entry in read_queue(prefix, consume=consume):
        if entry.lower().startswith(tuple(f"{s}:" for s in URI_SCHEMES)):
            found.setdefault(entry, Candidate(path=None, raw=entry, source="handoff"))
            continue
        real = _queued_file(entry, prefix)
        if real is None:
            log.debug("queued hand-off %r is not a package inside this C:\\ drive; ignored", entry)
            continue
        try:
            size = real.stat().st_size
        except OSError:
            continue
        found.setdefault(str(real), Candidate(path=real, raw=entry, source="handoff", size=size))
    for path, (mtime, size) in snapshot_packages(prefix).items():
        if before is not None:
            if before.get(path) == (mtime, size):
                continue
        elif started is not None and mtime < started - 2.0:
            continue
        real_path = Path(path)
        try:
            key = str(real_path.resolve())
        except OSError:
            key = path
        found.setdefault(key, Candidate(path=real_path, raw=path, source="temp", size=size))
    return sorted(found.values(), key=lambda c: (-(c.size), c.raw))


# ---------------------------------------------------------------------------
# What is it, and can Lindos do anything with it?
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """``role``: ``app`` (installable) | ``appinstaller`` (asks first) | ``component`` (a framework or
    resource package, never reported) | ``unsupported`` | ``encrypted`` | ``damaged`` | ``uri``."""

    path: Optional[Path]
    role: str
    kind: str = "unknown"
    title: str = ""
    version: str = ""
    publisher: str = ""
    reason: str = ""
    size: int = 0
    host: str = ""
    installable: bool = field(default=False)

    @property
    def file_name(self) -> str:
        return self.path.name if self.path is not None else ""


def human_size(num: float) -> str:
    n = float(max(0, num))
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"  # pragma: no cover - the loop always returns


def _clean_title(info: object, fallback: str) -> str:
    for attr in ("display_name", "name"):
        value = str(getattr(info, attr, "") or "").strip()
        if value and not value.lower().startswith("ms-resource:"):
            return value
    return fallback


def _uri_host(raw: str) -> str:
    """Host of the package an ``ms-appinstaller:?source=<url>`` link names (empty when unclear)."""
    try:
        query = urllib.parse.urlsplit(raw).query or raw.split(":", 1)[-1].lstrip("?")
        source = urllib.parse.parse_qs(query).get("source", [""])[0]
        return urllib.parse.urlsplit(source).hostname or ""
    except ValueError:
        return ""


def assess(cand: Candidate, msix: Any) -> Verdict:
    """Classify a candidate through the ``msix`` module (never raises)."""
    if cand.path is None:
        host = _uri_host(cand.raw)
        return Verdict(path=None, role="uri", kind="uri", title=host or "an app", host=host,
                       reason="an ms-appinstaller: link (Windows' one-click app installer link)")
    path = cand.path
    fallback = re.sub(r"[_\-]+", " ", path.stem).strip() or path.name
    size = cand.size or _size(path)
    error_cls: Any = getattr(msix, "MsixError", Exception)

    def verdict(role: str, kind: str, **kw: Any) -> Verdict:
        return Verdict(path=path, role=role, kind=kind, size=size, **kw)

    try:
        kind = str(msix.classify(path))
    except Exception as exc:  # noqa: BLE001
        log.debug("msix.classify failed for %s: %s", path, exc)
        kind = "unknown"
    if kind == "appinstaller":
        title, version, publisher = fallback, "", ""
        try:
            ai = {str(k): str(v) for k, v in dict(msix.parse_appinstaller(path)).items()}
            title, version, publisher = ai.get("name") or fallback, ai.get("version", ""), ai.get("publisher", "")
        except Exception:  # noqa: BLE001 - _handle_appinstaller reports a bad file itself
            pass
        return verdict("appinstaller", kind, title=title, version=version, publisher=publisher, installable=True,
                       reason="an App Installer file (a pointer to a download)")
    if kind in ("encrypted", "msixvc"):
        enc: object = None
        try:
            enc = msix.inspect(path)
        except Exception:  # noqa: BLE001 - encrypted packages may not be readable at all
            pass
        what = ("an Xbox / PC Game Pass game package" if kind == "msixvc"
                else "an encrypted Microsoft Store package (its contents are locked to the Store)")
        return verdict("encrypted", kind, title=_clean_title(enc, fallback),
                       version=str(getattr(enc, "version", "") or ""), reason=what)
    if kind not in ("package", "bundle", "upload"):
        return verdict("damaged", kind, title=fallback, reason=_first_line(_unknown_reason(msix, path)))
    try:
        info = msix.inspect(path)
    except error_cls as exc:
        return verdict("damaged", kind, title=fallback, reason=_first_line(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return verdict("damaged", kind, title=fallback, reason=_first_line(str(exc)))
    title = _clean_title(info, fallback)
    version = str(getattr(info, "version", "") or "")
    publisher = str(getattr(info, "publisher_display", "") or getattr(info, "publisher", "") or "")
    if getattr(info, "framework", False) or getattr(info, "resource_package", False):
        return verdict("component", kind, title=title, version=version, publisher=publisher,
                       reason="a shared component, not an app")
    apps = list(getattr(info, "apps", []) or [])
    if str(getattr(info, "status", "")) != "unsupported" and any(
            getattr(a, "app_class", "") == "win32" for a in apps):
        return verdict("app", kind, title=title, version=version, publisher=publisher, installable=True,
                       reason=str(getattr(info, "reason", "") or ""))
    reason = _first_line(str(getattr(info, "reason", "") or "")) or "it is a UWP/WinUI app, and Wine has no UWP app model"
    return verdict("unsupported", kind, title=title, version=version, publisher=publisher, reason=reason)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unknown_reason(msix: Any, path: Path) -> str:
    try:
        msix.inspect(path)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return "it is not a complete Windows app package"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def _plain(text: str) -> str:
    """One dialog-friendly clause: no markdown backticks, no trailing full stop."""
    return (text or "").replace("`", "'").strip().rstrip(".").strip()


def explain_text(installer: str, verdicts: Sequence[Verdict]) -> str:
    """ONE plain-language explanation for packages that cannot be installed, with what to do instead."""
    lines: List[str] = []
    files = [v for v in verdicts if v.role != "uri"]
    links = [v for v in verdicts if v.role == "uri"]
    if files:
        lines.append(f"'{installer}' downloaded a Windows app package and asked Windows to install it, but "
                     "that cannot be done on Lindos:")
    else:
        lines.append(f"'{installer}' asked Windows to install an app from a link (ms-appinstaller), which "
                     "Lindos does not follow on its own:")
    for v in files[:MAX_REPORTED]:
        head = f"{v.title} {v.version}".strip()
        size = f", {human_size(v.size)}" if v.size else ""
        reason = _plain(v.reason)
        if v.role == "damaged":
            lines.append(f"  - {v.file_name}{size}: the file looks incomplete or damaged ({reason or 'unreadable'}). "
                         "The installer may have been stopped while it was downloading it.")
        else:
            lines.append(f"  - {head} ({v.file_name}{size}): {reason or 'this kind of app cannot run here'}.")
    if len(files) > MAX_REPORTED:
        lines.append(f"  - and {len(files) - MAX_REPORTED} more package(s)")
    for v in links[:MAX_REPORTED]:
        where = f" from {v.host}" if v.host else ""
        lines.append(f"  - a link{where}: Microsoft turned these one-click installs off in 2023 because "
                     "criminals abused them, so Lindos never downloads from them automatically.")
    name = next((v.title for v in verdicts if v.title and v.role != "uri"), "") or "<app name>"
    lines += [
        "",
        "What you can do instead:",
        "  - Look for a Linux version or a web version of the app on its developer's website.",
        f"  - Search winget: lindos-compat winget search \"{name}\" - many apps also offer a normal .exe or "
        ".msi installer that works here.",
        "  - If you own a Windows licence, use the Windows virtual machine (lindos-vm) or run the app from "
        "your own Windows over RDP (lindos-winapps).",
    ]
    where_file = next((v.path for v in files if v.path is not None), None)
    if where_file is not None:
        lines += ["", f"Nothing was installed or opened. The downloaded file is still at {where_file}."]
    else:
        lines += ["", "Nothing was installed, downloaded or opened."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A hint before the installer runs
# ---------------------------------------------------------------------------


def sniff_bootstrapper(path: Path, *, max_bytes: int = 8 << 20) -> List[str]:
    """Strings in a PE file that suggest it hands an app package to Windows (a hint, never a verdict).

    Looks for the ASCII and UTF-16 spellings of ``Add-AppxPackage``, ``AddPackageAsync``,
    ``ms-appinstaller`` and the package suffixes.  An empty list means "nothing suspicious", which does
    not prove the installer never does it (many bootstrappers keep these strings compressed).
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(max_bytes)
    except OSError:
        return []
    if head[:2] != b"MZ":
        return []
    lowered = head.lower()
    found: List[str] = []
    for token in _BOOTSTRAP_TOKENS:
        low = token.lower()
        if low.encode("ascii") in lowered or low.encode("utf-16-le") in lowered:
            found.append(token)
    return found
