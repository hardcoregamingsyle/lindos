"""``lindos-run`` -- open a Windows file on Lindos (SPEC §9, SPEC-WINDOWS §28).

"If Windows can open it, Lindos can open it -- or tells you why not."

Flow:

1. resolve the file (a ``.lnk`` shortcut is followed onto its C:\\ drive);
2. classify it **by content** (``formats.detect``) and get its action plan
   (``formats.plan_action``): one handler of ``formats.HANDLERS``;
3. dispatch on the handler:

   * Wine handlers -- ``run`` (``.exe .bat .cmd .lnk``), ``msiexec-install``,
     ``msiexec-patch`` (``msiexec /p ... REINSTALL=ALL REINSTALLMODE=omus`` in the
     *product's* C:\\ drive), ``win16`` (mode-aware, SPEC-WINDOWS §28.5), ``wscript``,
     ``regedit`` (Windows-style confirmation listing what the file deletes), ``screensaver``,
     ``control-panel``, ``inf-install`` (software INFs only) and ``clickonce`` (only when the
     C:\\ drive already has .NET Framework): the program's own C:\\ drive (Wine prefix) under
     ``PREFIXES_DIR``, a runner (umu / wine / bottles), output in
     ``~/.local/state/lindos/run-<slug>.log`` and, after installers, a scan that turns what was
     installed into Start Menu entries + apps-database records;
   * Linux-side handlers -- ``dos`` (DOSBox, no C:\\ drive), ``pwsh`` (PowerShell 7 after a
     confirmation), ``open-url`` (web / e-mail / FTP links only, via ``xdg-open``), ``extract``
     (``cabextract`` into a new folder, then the folder opens) and ``mount`` (read-only loop
     mount, then an AutoPlay-style "Run setup?" -- never automatic);
   * ``msix`` / ``appinstaller`` -- packaged desktop apps are unpacked into a C:\\ drive named
     after the package family and added to the Start Menu; ``.appinstaller`` files download
     their package only after an explicit yes, over HTTPS, into a quarantine folder, and the
     package identity must match;
   * ``explain`` -- nothing is run; the reason is shown (exit code 3).

Everything destructive or outward-facing asks first: a dialog when started from the file
manager, a y/N prompt in a terminal, ``--yes`` for scripts -- otherwise Lindos refuses.

Exit codes: 0 ok · 1 error (or cancelled) · 2 usage · 3 unsupported (explained).
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import http.client
import importlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import CoreMissing, __version__, core, get_logger, load_user_config, user_home
from .doctor import POWERSHELL_INSTALL_CMD
from .gui import Feedback, gui_wanted
from .icons import GENERIC_ICON, STANDARD_SIZES, icon_target_path, icons_dir, install_app_icon
from .perf import GamescopeSpec, parse_geometry, perf_env
from .profiles import Profile, gamescope_from_profile, resolve_profile
from .lnk import LnkError, LnkInfo, expand_windows_env, is_windows_path, parse_lnk, unix_to_windows, windows_to_unix
from .prefix import (
    SHARED_SLUG,
    PrefixState,
    derive_slug,
    ensure_prefix,
    find_prefix_for_path,
    find_wine,
    link_windows_apps,
    list_prefixes,
    prefix_path,
    read_marker,
    safe_slug,
)
from .recipes import Recipe, get_recipe
from .runner import (
    RUNNERS,
    RunPlan,
    bottle_prefix_path,
    build_plan,
    choose_runner,
    ensure_bottle,
    load_prefix_settings,
    log_path_for,
    run_host,
    run_plan,
    wine_path,
)
from .scan import (
    SKIP_NAME_RE,
    FoundApp,
    applications_dir,
    diff_snapshots,
    discover_apps,
    find_registered,
    register_app,
    snapshot,
    unique_app_slug,
    write_desktop_file,
)

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "EXIT_UNSUPPORTED", "SUPPORTED_SUFFIXES", "URL_SCHEMES",
           "WINE_HANDLERS", "DownloadError", "build_parser", "main", "split_windows_args", "info_to_dict",
           "analyze_file", "resolve_shortcut", "pick_slug", "gamescope_spec", "resolved_perf_env",
           "format_block", "action_block", "parse_reg_deletions", "expand_msix_macros", "download_https",
           "url_allowed", "cab_member_unsafe"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3  # the file type is not supported; the reason was shown (SPEC-WINDOWS §28.3)

#: every suffix of the SPEC-WINDOWS §28.3 format table (a hint only: files are classified by content)
SUPPORTED_SUFFIXES = (
    ".exe", ".msi", ".msp", ".mst", ".bat", ".cmd", ".lnk", ".com", ".pif", ".dll", ".ocx", ".sys", ".efi",
    ".msix", ".appx", ".msixbundle", ".appxbundle", ".msixupload", ".appxupload", ".emsix", ".eappx",
    ".emsixbundle", ".eappxbundle", ".msixvc", ".appinstaller", ".ps1", ".vbs", ".vbe", ".wsf", ".reg", ".url",
    ".scr", ".cpl", ".inf", ".cab", ".msu", ".iso", ".img", ".application", ".appref-ms",
)
INSTALL_HINT = ("Install Windows program support first: run  pkexec /usr/libexec/lindos/install-compat.sh  "
                "(or open Lindos Settings > Windows apps > Doctor for the exact fix commands)")
#: link types an Internet shortcut (.url) may open
URL_SCHEMES = ("http", "https", "mailto", "ftp")
#: handlers that run inside a C:\ drive (Wine prefix)
WINE_HANDLERS = ("run", "msiexec-install", "msiexec-patch", "win16", "wscript", "regedit", "screensaver",
                 "control-panel", "inf-install", "clickonce")
#: after these, the C:\ drive is scanned for newly installed programs
SCAN_HANDLERS = ("run", "msiexec-install", "inf-install", "win16", "clickonce")
#: these change an existing program's C:\ drive, so the user picks it
CHOOSE_PREFIX_HANDLERS = ("msiexec-patch", "regedit")
#: largest download accepted from an .appinstaller (same cap as msix.install)
APPINSTALLER_MAX_BYTES = 64 << 30
REG_PREVIEW_MAX_BYTES = 64 << 20
INF_PATH_MAX = 260  # Wine's InstallHinfSection copies the command line into a MAX_PATH buffer
MSI_RESTART_CODES = (3010, 1641)

_TOOL_LABEL = {
    "msiexec-install": "Windows Installer (msiexec)",
    "msiexec-patch": "Windows Installer (msiexec)",
    "regedit": "Registry Editor (regedit)",
    "control-panel": "Control Panel (control)",
    "wscript": "Windows Script Host (wscript)",
    "inf-install": "Windows Setup API (rundll32)",
    "clickonce": "ClickOnce (rundll32 dfshim)",
    "screensaver": "the screen saver",
}

log = get_logger("lindos-run")

_WIN_ARG_RE = re.compile(r'"[^"]*"|\S+')
_TEMP_DIR_RE = re.compile(r"(^|/)(tmp|temp|\.cache|Temp)(/|$)", re.IGNORECASE)
_MACRO_RE = re.compile(r"\$\$|\$\(([^)]*)\)")
_PSF_RE = re.compile(r"^psflauncher(32|64)?\.exe$", re.IGNORECASE)

# Seams (replaced in tests): host tool lookup, process helpers, terminal detection, input, download.
_which: Callable[[str], Optional[str]] = shutil.which
_popen: Callable[..., Any] = subprocess.Popen
_call: Callable[..., int] = subprocess.call
_run: Callable[..., Any] = subprocess.run
_input: Callable[[str], str] = input
_fetch: Optional[Callable[[str], Any]] = None


def _stdin_isatty() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _mod(name: str) -> Optional[ModuleType]:
    """An optional sibling module (formats, msix, dos, diskimage) or None when unavailable."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError as exc:
        log.debug("lindos_compat.%s is not available: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lindos-run",
        description="Open a Windows file on Lindos: programs (.exe .msi .bat .lnk), patches (.msp), "
                    "Store-style app packages (.msix .appx and bundles), registry files (.reg), scripts "
                    "(.ps1 .vbs), Internet shortcuts (.url), DOS and 16-bit Windows programs, .cab archives, "
                    "disk images (.iso .img) and more - through Wine or Proton (no virtual machine). Files "
                    "that cannot work on Linux are explained instead (exit code 3).",
        epilog="Examples:\n"
               "  lindos-run ~/Downloads/npp.8.6.Installer.x64.exe\n"
               "  lindos-run --prefix photoshop-cc-2021 ~/Downloads/Set-up.exe\n"
               "  lindos-run --runner umu --gamemode ~/Games/game.exe\n"
               "  lindos-run --prefix office update.msp\n"
               "  lindos-run --prefix notepad --yes settings.reg\n"
               "  lindos-run --info setup.exe\n"
               "Program arguments go after the file (use -- if they start with a dash):\n"
               "  lindos-run installer.exe /S\n"
               "Exit codes: 0 ok, 1 error or cancelled, 2 usage, 3 not supported (explained).\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", help="the Windows file: a program, installer, package, script, shortcut or disk image")
    p.add_argument("args", nargs="*", help="arguments passed to the Windows program")
    p.add_argument("--runner", choices=RUNNERS, metavar="umu|wine|bottles",
                   help="force a runner (default: games/unknown -> umu (Proton), installers/apps -> wine)")
    p.add_argument("--prefix", metavar="NAME",
                   help="use the C:\\ drive (Wine prefix) called NAME instead of the program's own one")
    p.add_argument("--new-prefix", action="store_true",
                   help="start from a fresh C:\\ drive (the old one is moved aside, not deleted)")
    p.add_argument("--shared", action="store_true",
                   help="use the shared 'default' C:\\ drive (fine for small utilities)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="answer 'yes' to Lindos' own questions (registry merges, PowerShell scripts, app "
                        "installs, .appinstaller downloads, 'Run setup?' on disk images) - for scripts")
    p.add_argument("--gamemode", action="store_true", help="wrap the program in gamemoderun (Feral GameMode)")
    p.add_argument("--mangohud", action="store_true", help="show the MangoHud FPS overlay")
    p.add_argument("--gamescope", nargs="?", const="", default=None, metavar="WxH",
                   help="run inside gamescope (optionally at resolution WxH, e.g. 2560x1440)")
    p.add_argument("--hdr", action="store_true", help="enable HDR (needs gamescope + an HDR display)")
    p.add_argument("--fsr", action="store_true", help="enable AMD FSR upscaling in gamescope")
    async_grp = p.add_mutually_exclusive_group()
    async_grp.add_argument("--dxvk-async", dest="dxvk_async", action="store_true", default=None,
                           help="force DXVK async shader compilation on (Proton games)")
    async_grp.add_argument("--no-dxvk-async", dest="dxvk_async", action="store_false",
                           help="force DXVK async off")
    p.add_argument("--appid", metavar="ID", help="match a per-title profile by Steam appid")
    p.add_argument("--info", action="store_true",
                   help="only print what Lindos knows about the file (JSON, incl. its format) - nothing is run")
    p.add_argument("--dry-run", action="store_true",
                   help="print the launch plan (runner, C:\\ drive, environment, command) as JSON and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug output")
    p.add_argument("--version", action="version", version=f"lindos-run {__version__}")
    return p


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


def split_windows_args(text: str) -> List[str]:
    """Split a Windows command-line tail (``"C:\\x y" /flag``) into arguments."""
    out: List[str] = []
    for m in _WIN_ARG_RE.finditer(text or ""):
        tok = m.group(0)
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            tok = tok[1:-1]
        if tok:
            out.append(tok)
    return out


def info_to_dict(info: object) -> Dict[str, Any]:
    """``ExeInfo`` (dataclass or namespace) → plain dict."""
    if info is None:
        return {}
    if dataclasses.is_dataclass(info) and not isinstance(info, type):
        return {k: v for k, v in dataclasses.asdict(info).items()}
    d = getattr(info, "__dict__", None)
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if not k.startswith("_")}
    return {}


def format_block(det: object) -> Optional[Dict[str, object]]:
    """``lindos-run --info`` ``"format"``: ``{id,label,status,handler,note,reason}`` of a ``formats.Detection``."""
    if det is None:
        return None
    spec = getattr(det, "format", None)
    return {
        "id": str(getattr(spec, "id", "") or ""),
        "label": str(getattr(spec, "label", "") or ""),
        "status": str(getattr(spec, "status", "") or ""),
        "handler": str(getattr(spec, "handler", "") or ""),
        "note": str(getattr(spec, "note", "") or ""),
        "reason": str(getattr(det, "reason", "") or ""),
    }


def action_block(fplan: object) -> Optional[Dict[str, object]]:
    """A ``formats.ActionPlan`` as plain JSON-able data."""
    if fplan is None:
        return None
    as_dict = getattr(fplan, "as_dict", None)
    if callable(as_dict):
        try:
            data = as_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - fall back to the spec fields below
            pass
    out: Dict[str, object] = {}
    for key in ("handler", "wine_tail", "host_argv", "needs_prefix", "force_runner", "arch", "prefix_hint",
                "confirm", "message", "exit_code"):
        value = getattr(fplan, key, None)
        out[key] = [str(v) for v in value] if isinstance(value, (list, tuple)) else value
    return out


def _fallback_info(path: Path) -> SimpleNamespace:
    suffix = path.suffix.lower()
    if suffix == ".msi":
        kind, itype = "msi", "msi"
    elif suffix in (".bat", ".cmd"):
        kind, itype = "app", None
    else:
        kind, itype = "unknown", None
    return SimpleNamespace(path=str(path), name=path.name, kind=kind, arch="unknown", installer_type=itype,
                           product="", company="", sha256_prefix="")


def analyze_file(path: Path) -> object:
    """Analyse ``path`` with the shared analyzer (``lindos.compat.analyze_exe``).

    Non-PE files (``.bat``, ``.msi`` when the analyzer cannot read them) get a
    minimal fallback record so the rest of the flow still works.
    Raises :class:`CoreMissing` when lindos-core is absent.
    """
    c = core()  # may raise CoreMissing
    suffix = path.suffix.lower()
    info: object
    try:
        info = c.analyze_exe(str(path))
    except Exception as exc:  # noqa: BLE001 - the analyzer is best-effort
        log.debug("analyze_exe failed on %s: %s", path, exc)
        info = _fallback_info(path)
    kind = str(getattr(info, "kind", "") or "")
    if suffix == ".msi" and kind in ("", "unknown"):
        _set(info, "kind", "msi")
        _set(info, "installer_type", "msi")
    elif suffix in (".bat", ".cmd") and kind in ("", "unknown"):
        _set(info, "kind", "app")
    if not getattr(info, "path", None):
        _set(info, "path", str(path))
    if not getattr(info, "name", None):
        _set(info, "name", path.name)
    return info


def _set(obj: object, key: str, value: object) -> None:
    try:
        setattr(obj, key, value)
    except Exception:  # noqa: BLE001 - frozen dataclass etc.
        pass


def resolve_shortcut(lnk_path: Path, prefix_opt: Optional[str] = None) -> Tuple[Optional[Path], Optional[LnkInfo], Optional[str], str]:
    """Resolve a ``.lnk`` to ``(target_path, lnk_info, prefix_slug, error_message)``.

    The shortcut's own location decides the C:\\ drive when it lives inside one;
    otherwise every Lindos C:\\ drive is searched for the target.
    """
    try:
        info = parse_lnk(lnk_path)
    except LnkError as exc:
        return None, None, None, f"'{lnk_path.name}' is not a readable Windows shortcut: {exc}"
    if not info.target:
        return None, info, None, f"The shortcut '{lnk_path.name}' has no target (it may point to a folder or a network location)."

    candidates: List[Path] = []
    if prefix_opt:
        candidates.append(prefix_path(prefix_opt))
    inside = find_prefix_for_path(lnk_path)
    if inside:
        candidates.append(prefix_path(inside))
    if not candidates:
        candidates.extend(Path(str(p["path"])) for p in list_prefixes())

    if is_windows_path(info.target):
        for pfx in candidates:
            target = windows_to_unix(info.target, pfx, must_exist=True)
            if target is not None:
                return target, info, pfx.name, ""
        where = ("the C:\\ drive '" + prefix_opt + "'") if prefix_opt else "any Lindos C:\\ drive"
        return None, info, None, (
            f"The shortcut points to {info.target} but no Windows program with that path exists on {where}."
        )
    # relative target: relative to the shortcut's folder
    rel = lnk_path.parent / info.target.replace("\\", "/")
    if rel.exists():
        return rel, info, inside or (prefix_opt and safe_slug(prefix_opt)) or None, ""
    if info.env_target:
        for pfx in candidates:
            target = windows_to_unix(info.env_target, pfx, must_exist=True)
            if target is not None:
                return target, info, pfx.name, ""
    return None, info, None, f"The shortcut's target '{info.target}' was not found next to it."


def pick_slug(path: Path, info: object, *, prefix_opt: Optional[str] = None, shared: bool = False,
              registered: Optional[Dict[str, object]] = None) -> str:
    """Which C:\\ drive does this program get?  (``--prefix`` > ``--shared`` > apps DB > location > product)."""
    if prefix_opt:
        return safe_slug(prefix_opt)
    if shared:
        return SHARED_SLUG
    if registered and registered.get("prefix"):
        return safe_slug(str(registered["prefix"]))
    inside = find_prefix_for_path(path)
    if inside:
        return safe_slug(inside)
    return derive_slug(info)


def _pretty(text: str) -> str:
    text = re.sub(r"[_\-]+", " ", text or "").strip()
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return text or "Windows program"


def _display_name(info: object, path: Path) -> str:
    product = str(getattr(info, "product", "") or "").strip()
    return product or _pretty(path.stem)


def _print_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def gamescope_spec(ns: argparse.Namespace, profile: Optional[Profile]) -> GamescopeSpec:
    """Combine ``--gamescope [WxH] --hdr --fsr`` with a profile's gamescope block.

    A profile supplies the defaults; explicit command-line flags win.  gamescope is
    enabled when ``--gamescope`` is given or the profile carries a gamescope block.
    """
    spec = gamescope_from_profile(profile) if profile is not None else GamescopeSpec()
    cli_requested = getattr(ns, "gamescope", None) is not None
    if cli_requested:
        spec.enabled = True
        w, h = parse_geometry(ns.gamescope)
        if w and h:
            spec.width, spec.height = w, h
    if getattr(ns, "hdr", False):
        spec.hdr = True
    if getattr(ns, "fsr", False):
        spec.fsr = True
    return spec


def resolved_perf_env(runner: str, kind: str, *, dxvk_async: Optional[bool], hdr: bool) -> Dict[str, str]:
    """The documented perf env that would be applied (for ``--info``)."""
    return perf_env(kind=kind, runner=runner, dxvk_async=dxvk_async, hdr=hdr)


def _profile_env(profile: Profile) -> Dict[str, str]:
    """Environment contributed by a per-title profile (its ``env`` plus a Proton pin)."""
    env: Dict[str, str] = dict(profile.env)
    if profile.proton and "PROTONPATH" not in env and "PROTONPATH" not in os.environ:
        env["PROTONPATH"] = "GE-Proton" if profile.proton in ("GE-Proton-latest", "latest") else profile.proton
    return env


# --- .reg preview --------------------------------------------------------------------------

_REG_HEADERS = ("windows registry editor version 5.00", "regedit4", "regedit")
_REG_VALUE_DELETE_RE = re.compile(r'^(@|"((?:[^"\\]|\\.)*)")\s*=\s*-\s*$')


def _decode_reg(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if len(raw) >= 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le", errors="replace")  # UTF-16LE without a BOM
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_reg_deletions(path: Path, *, max_bytes: int = REG_PREVIEW_MAX_BYTES) -> List[str]:
    """Everything a ``.reg`` file would *delete*: ``[-KEY]`` keys and ``"name"=-`` values.

    Raises ``ValueError`` when the file is not a registration file (no ``REGEDIT4`` /
    ``Windows Registry Editor Version 5.00`` header) or is too large to preview.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"the file is too large to check ({size // (1 << 20)} MiB)")
    with open(path, "rb") as fh:
        text = _decode_reg(fh.read())
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = next((ln.strip().lstrip("\ufeff") for ln in lines if ln.strip()), "")
    if header.lower() not in _REG_HEADERS:
        raise ValueError("it does not start with 'Windows Registry Editor Version 5.00' or 'REGEDIT4'")
    out: List[str] = []
    current = ""
    logical = ""
    for raw in lines[1:] + [""]:
        piece = raw.strip()
        if logical:
            logical += piece
        else:
            logical = piece
        if logical.endswith("\\") and not logical.startswith("["):
            logical = logical[:-1]
            continue  # value continues on the next line
        line, logical = logical, ""
        if not line or line.startswith(";"):
            continue
        if line.startswith("[-") and line.endswith("]"):
            out.append(f"[{line[2:-1].strip()}] (this key and everything in it)")
            current = ""
        elif line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
        else:
            m = _REG_VALUE_DELETE_RE.match(line)
            if m:
                name = "(Default)" if m.group(1) == "@" else m.group(2).replace('\\"', '"').replace("\\\\", "\\")
                out.append(f'value "{name}" in [{current or "?"}]')
    return out


def _format_deletion(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        key = str(item.get("key") or item.get("path") or "")
        name = item.get("value", item.get("name"))
        if name is None:
            return f"[{key}] (this key and everything in it)"
        return f'value "{name or "(Default)"}" in [{key}]'
    if isinstance(item, (list, tuple)) and len(item) == 2:
        key, name = item
        return f'value "{name or "(Default)"}" in [{key}]'
    return str(item)


def _deletions_from_preview(preview: object) -> Optional[List[str]]:
    """Normalise ``formats.reg_preview()`` output; None when its shape is not recognised."""
    if preview is None:
        return None
    if dataclasses.is_dataclass(preview) and not isinstance(preview, type):
        preview = dataclasses.asdict(preview)
    if not isinstance(preview, dict):
        return None
    found = False
    out: List[str] = []
    for key in ("deletions", "deleted", "delete_keys", "deleted_keys", "delete_values", "deleted_values",
                "removed_keys", "removed_values"):
        if key in preview:
            found = True
            for item in preview.get(key) or []:
                out.append(_format_deletion(item))
    return out if found else None


def reg_deletions(path: Path) -> List[str]:
    """What merging ``path`` deletes: ``formats.reg_preview`` when available, else the local parser."""
    formats = _mod("formats")
    preview_fn = getattr(formats, "reg_preview", None) if formats is not None else None
    if callable(preview_fn):
        try:
            got = _deletions_from_preview(preview_fn(path))
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the built-in parser
            log.debug("formats.reg_preview failed (%s); using the built-in parser", exc)
            got = None
        if got is not None:
            return got
    return parse_reg_deletions(path)


# --- MSIX helpers --------------------------------------------------------------------------


def expand_msix_macros(text: str, package_dir: str) -> str:
    """Expand ``$(...)`` macros of ``uap11:Parameters`` / ``CurrentDirectoryPath``.

    ``package_dir`` is the Windows path of the unpacked package; ``$(env:X)`` uses Wine's
    default Windows environment; ``$$`` is a literal ``$``; unknown macros stay as written.
    """
    def repl(m: "re.Match[str]") -> str:
        if m.group(0) == "$$":
            return "$"
        key = (m.group(1) or "").strip()
        low = key.lower()
        if low.startswith("env:"):
            return expand_windows_env(f"%{key[4:]}%")
        if low.startswith("package.") and low.endswith("path"):
            return package_dir
        if low == "system.path":
            return "C:\\windows\\system32"
        if low == "windows.path":
            return "C:\\windows"
        return m.group(0)

    return _MACRO_RE.sub(repl, text or "")


def _ci_child(directory: Path, name: str) -> Optional[Path]:
    exact = directory / name
    if exact.exists():
        return exact
    try:
        for entry in os.listdir(directory):
            if entry.lower() == name.lower():
                return directory / entry
    except OSError:
        return None
    return None


def _package_file(root: Path, rel: str) -> Optional[Path]:
    """``rel`` (``/`` or ``\\`` separated, any case) inside ``root``; None if it escapes or is missing."""
    parts = [p for p in re.split(r"[\\/]+", rel.strip()) if p and p != "."]
    if not parts or any(p == ".." or ":" in p for p in parts):
        return None
    current = root
    for part in parts:
        nxt = _ci_child(current, part)
        if nxt is None:
            return None
        current = nxt
    try:
        current.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return current


def _resolve_psf(app: object, exe: Path, install_dir: Path) -> Tuple[Path, Optional[str], Optional[str]]:
    """PsfLauncher*.exe → the real target from the package's ``config.json`` (never its scripts)."""
    if not _PSF_RE.match(exe.name):
        return exe, None, None
    cfg = _ci_child(install_dir, "config.json")
    if cfg is None:
        return exe, None, None
    try:
        if cfg.stat().st_size > (1 << 20):
            return exe, None, None
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return exe, None, None
    app_id = str(getattr(app, "id", "") or "")
    for entry in data.get("applications", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict) or str(entry.get("id", "")) != app_id:
            continue
        if entry.get("startScript") or entry.get("endScript"):
            log.info("The package's PowerShell start/end scripts are not run (Lindos never runs them)")
        target = str(entry.get("executable") or "").replace("%MsixPackageRoot%", "").replace("%msixpackageroot%", "")
        real = _package_file(install_dir, target) if target else None
        if real is None:
            return exe, None, None
        args = entry.get("arguments")
        wd = entry.get("workingDirectory")
        return real, (str(args) if args else None), (str(wd) if wd else None)
    return exe, None, None


def _msix_app_name(app: object, info: object) -> str:
    for cand in (getattr(app, "display_name", ""), getattr(info, "display_name", ""), getattr(info, "name", ""),
                 Path(str(getattr(app, "executable", "") or "app")).stem):
        text = str(cand or "").strip()
        if text and not text.lower().startswith("ms-resource:"):
            return text
    return "Windows app"


# --- URL / cab / download helpers ----------------------------------------------------------


def url_allowed(url: str) -> Tuple[bool, str]:
    """Only http/https/mailto/ftp links from Internet shortcuts; ``(ok, why_not)``."""
    if not url or len(url) > 4096:
        return False, "The link is empty or too long."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in url):
        return False, "The link contains spaces or control characters, so it is not opened."
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in URL_SCHEMES:
        return False, (f"Lindos only opens web (http/https), e-mail (mailto) and FTP links from shortcuts; "
                       f"'{scheme or 'unknown'}:' links can start programs or open local files, so this one "
                       "is not opened.")
    return True, ""


def _url_from(value: object) -> str:
    """The URL out of ``formats.parse_url_shortcut()`` (``(url, allowed, reason)``, a dict or a str)."""
    if isinstance(value, (tuple, list)):
        return str(value[0]).strip() if value else ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or value.get("URL") or "").strip()
    return str(getattr(value, "url", "") or "").strip()


def cab_member_unsafe(name: str) -> bool:
    """True for archive member names that would escape the extraction folder."""
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return True
    if any(ord(ch) < 0x20 for ch in n):
        return True
    return any(part == ".." for part in n.split("/"))


def _cab_members(cabextract: str, path: Path) -> Optional[List[str]]:
    """Member names from ``cabextract -l``; None when the listing failed."""
    try:
        proc = _run([cabextract, "-l", str(path)], capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    names: List[str] = []
    for line in (proc.stdout or "").splitlines():
        cols = line.split("|", 2)
        if len(cols) == 3 and cols[0].strip().isdigit():
            names.append(cols[2].strip())
    return names


def _unique_dir(parent: Path, stem: str) -> Path:
    base = re.sub(r"[\x00-\x1f/\\]+", "_", stem).strip(" .") or "extracted"
    candidate = parent / base
    n = 2
    while candidate.exists() and n < 1000:
        candidate = parent / f"{base} ({n})"
        n += 1
    return candidate


class DownloadError(Exception):
    """An ``.appinstaller`` download was refused or failed (the message is user-facing)."""


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(newurl, code, "refusing a redirect to a non-HTTPS address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https_fetch(url: str) -> Any:
    opener = urllib.request.build_opener(_HttpsOnlyRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": f"lindos-run/{__version__}"})
    return opener.open(req, timeout=60)


def _safe_filename(name: str) -> str:
    name = urllib.parse.unquote(name or "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip("._")[:120]
    return name or "package.msix"


def download_https(url: str, dest_dir: Path, *, fetch: Optional[Callable[[str], Any]] = None,
                   max_bytes: int = APPINSTALLER_MAX_BYTES,
                   on_progress: Optional[Callable[[int, Optional[int]], None]] = None) -> Path:
    """Download ``url`` (HTTPS only, also after redirects) into ``dest_dir``; returns the file.

    ``fetch(url)`` returns a response object with ``read(n)``, ``geturl()`` and ``headers``
    (injectable; default: urllib with an HTTPS-only redirect policy and an honest
    ``lindos-run/<version>`` user agent).  Raises :class:`DownloadError`.
    """
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise DownloadError("only secure https:// downloads are allowed")
    fetch = fetch or _fetch or _https_fetch
    try:
        resp = fetch(url)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"the server answered {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"could not connect ({getattr(exc, 'reason', exc)})") from exc
    tmp: Optional[Path] = None
    try:
        final = str(resp.geturl()) if hasattr(resp, "geturl") else url
        if urllib.parse.urlsplit(final).scheme.lower() != "https":
            raise DownloadError("the server redirected to an insecure (non-HTTPS) address")
        headers = getattr(resp, "headers", {}) or {}
        length: Optional[int] = None
        try:
            raw_len = headers.get("Content-Length") if hasattr(headers, "get") else None
            length = int(raw_len) if raw_len not in (None, "") else None
        except (TypeError, ValueError):
            length = None
        if length is not None and length > max_bytes:
            raise DownloadError(f"the package is too large ({length // (1 << 20)} MiB)")
        dest_dir.mkdir(parents=True, exist_ok=True)
        if length is not None:
            free = shutil.disk_usage(str(dest_dir)).free
            if length + (256 << 20) > free:
                raise DownloadError("there is not enough free disk space for the download")
        name = _safe_filename(os.path.basename(urllib.parse.urlsplit(final).path))
        tmp = dest_dir / (name + ".part")
        total = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError("the download is larger than allowed")
                fh.write(chunk)
                if on_progress:
                    on_progress(total, length)
        if length is not None and total != length:
            raise DownloadError(f"the download is incomplete ({total} of {length} bytes)")
        target = dest_dir / name
        os.replace(tmp, target)
        tmp = None
        return target
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadError(f"the download failed ({exc})") from exc
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _quarantine_dir() -> Path:
    base = user_home() / ".cache" / "lindos" / "quarantine"
    d = base / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


# ---------------------------------------------------------------------------
# registration helpers
# ---------------------------------------------------------------------------


def _png_size(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return 0
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return 0
    return struct.unpack(">I", head[16:20])[0]


def _install_png_icon(png: Path, app_slug: str) -> Tuple[str, Optional[Path]]:
    """Install a package logo (PNG) as ``lindos-<slug>`` in the user's hicolor theme."""
    width = _png_size(png)
    try:
        too_big = png.stat().st_size > (8 << 20)
    except OSError:
        too_big = True
    if width <= 0 or too_big:
        return GENERIC_ICON, None
    size = width if width in STANDARD_SIZES else next((s for s in STANDARD_SIZES if s >= width), 512)
    target = icon_target_path(app_slug, size)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, target)
        os.utime(icons_dir(), None)
    except OSError as exc:
        log.debug("cannot install icon %s: %s", target, exc)
        return GENERIC_ICON, None
    return f"lindos-{app_slug}", target


def _install_icon(app: FoundApp, app_slug: str) -> Tuple[str, Optional[Path]]:
    src = app.icon_source or app.exe
    if Path(src).suffix.lower() == ".png":
        icon, icon_file = _install_png_icon(Path(src), app_slug)
        if icon_file is not None:
            return icon, icon_file
        src = app.exe
    return install_app_icon(src, app_slug)


def _set_desktop_terminal(desktop: Path) -> None:
    """Console programs (MSIX ``Subsystem="console"``) open in a terminal window."""
    try:
        text = desktop.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^Terminal=false$", "Terminal=true", text)
        if new != text:
            tmp = desktop.with_suffix(".desktop.tmp")
            tmp.write_text(new, encoding="utf-8", newline="\n")
            os.replace(tmp, desktop)
    except OSError as exc:
        log.debug("cannot update %s: %s", desktop, exc)


def _register_found(app: FoundApp, *, prefix_slug: str, runner: str, kind: str = "app",
                    extra: Optional[Dict[str, object]] = None, terminal: bool = False) -> Optional[str]:
    """Icon + .desktop + apps-database record for one discovered program.  Returns the app slug."""
    try:
        c = core()
        db = c.apps_db_load()
        if not isinstance(db, dict):
            db = {}
    except CoreMissing:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("apps database unreadable: %s", exc)
        db = {}
    app_slug = unique_app_slug(app.name, app.exe, db)
    icon, icon_file = _install_icon(app, app_slug)
    # The .desktop "Path=" must be a real directory on this machine: shortcuts carry a
    # Windows working directory (C:\...), so map it onto the C:\ drive first.
    workdir = ""
    if app.workdir:
        if is_windows_path(app.workdir):
            mapped = windows_to_unix(app.workdir, prefix_path(prefix_slug), must_exist=True)
            workdir = str(mapped) if mapped is not None and mapped.is_dir() else ""
        elif Path(app.workdir).is_dir():
            workdir = app.workdir
    try:
        desktop: Optional[Path] = write_desktop_file(app_slug, name=app.name, exe=app.exe, prefix_slug=prefix_slug,
                                                     runner=runner, icon=icon, workdir=workdir)
    except OSError as exc:
        log.warning("could not write the Start Menu entry for %s: %s", app.name, exc)
        desktop = None
    if terminal and desktop is not None:
        _set_desktop_terminal(desktop)
    try:
        slug = register_app(app, prefix_slug=prefix_slug, runner=runner, kind=kind, icon=icon, icon_file=icon_file,
                            desktop_file=desktop, db=db, save=False)
        rec = db.get(slug)
        if extra and isinstance(rec, dict):
            rec.update(extra)
        c.apps_db_save(db)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save the apps database: %s", exc)
    log.info("Start Menu entry created: %s (%s)", app.name, desktop or "no .desktop file")
    return app_slug


def _refresh_desktop_database() -> None:
    tool = _which("update-desktop-database")
    if not tool:
        return
    try:
        _run([tool, "-q", str(applications_dir())], check=False, timeout=60,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# context, questions, small UI helpers
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    ns: argparse.Namespace
    fb: Feedback
    path: Path
    args: List[str]
    det: Any = None
    fplan: Any = None
    lnk_info: Optional[LnkInfo] = None
    depth: int = 0

    @property
    def handler(self) -> str:
        if self.fplan is None:
            return "run"
        return str(getattr(self.fplan, "handler", "") or "run")

    def plan(self, name: str, default: Any = None) -> Any:
        if self.fplan is None:
            return default
        value = getattr(self.fplan, name, default)
        return default if value is None else value


def _fail(ctx: _Ctx, msg: str, *, code: int = EXIT_ERROR) -> int:
    log.error("%s", msg)
    ctx.fb.error(msg)
    return code


def _explain(ctx: _Ctx, text: str, *, code: int = EXIT_UNSUPPORTED) -> int:
    """Show why a file cannot be opened; exit code 3 (SPEC-WINDOWS §27.2: never 'try anyway')."""
    text = (text or f"Lindos cannot open '{ctx.path.name}'.").strip()
    if ctx.path.name not in text:
        text = f"{ctx.path.name}: {text}"
    log.warning("%s", text)
    ctx.fb.explain(text)
    return code


def _cancelled(ctx: _Ctx) -> int:
    log.info("Cancelled - nothing was changed.")
    ctx.fb.close_progress()
    return EXIT_ERROR


def _needs_yes(ctx: _Ctx, what: str) -> int:
    msg = (f"{what} needs your confirmation, and Lindos could not ask (no terminal and no dialog). "
           "Run it again from a terminal, open it from the file manager, or add --yes.")
    log.error("%s", msg)
    return EXIT_USAGE


def _confirm(ctx: _Ctx, question: str, *, ok_label: str = "Yes", cancel_label: str = "No") -> Optional[bool]:
    """Ask first: ``--yes`` → True; a dialog in GUI mode; a y/N prompt on a terminal; else None."""
    if ctx.ns.yes:
        return True
    answer = ctx.fb.question(question, ok_label=ok_label, cancel_label=cancel_label)
    if answer is not None:
        return answer
    if _stdin_isatty():
        print(question)
        try:
            reply = _input(f"{ok_label}? [y/N] ")
        except EOFError:
            return False
        return reply.strip().lower() in ("y", "yes")
    return None


def _spawn(argv: Sequence[str], *, cwd: Optional[str] = None) -> bool:
    """Start a desktop program (file manager, browser, terminal) without waiting for it."""
    try:
        _popen([str(a) for a in argv], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        log.warning("could not start %s: %s", argv[0] if argv else "?", exc)
        return False


def _open_folder(folder: Path) -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    opener = _which("xdg-open") or _which("exo-open")
    return bool(opener) and _spawn([str(opener), str(folder)])


def _terminal_argv(argv: Sequence[str]) -> Optional[List[str]]:
    """``argv`` wrapped in a terminal window that stays open afterwards (for console programs)."""
    for name, pre in (("xfce4-terminal", ["--hold", "-x"]), ("gnome-terminal", ["--"]),
                      ("konsole", ["--hold", "-e"]), ("x-terminal-emulator", ["-e"])):
        exe = _which(name)
        if exe:
            return [exe, *pre, *[str(a) for a in argv]]
    return None


def _host_dry_run(ctx: _Ctx, argv: Sequence[str], **extra: object) -> int:
    out: Dict[str, object] = {"handler": ctx.handler, "file": str(ctx.path), "argv": [str(a) for a in argv],
                              "format": format_block(ctx.det)}
    out.update(extra)
    _print_json(out)
    return EXIT_OK


def _plan_confirm(ctx: _Ctx) -> Optional[int]:
    """Ask the question an ActionPlan carries (if any).  Returns an exit code to stop with, or None."""
    question = ctx.plan("confirm")
    if not question:
        return None
    ans = _confirm(ctx, str(question))
    if ans is None:
        return _needs_yes(ctx, f"Opening '{ctx.path.name}'")
    if not ans:
        return _cancelled(ctx)
    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    ns, unknown = parser.parse_known_args(list(argv) if argv is not None else None)
    prog_args: List[str] = list(ns.args) + list(unknown)
    global log
    log = get_logger("lindos-run", verbose=bool(ns.verbose) or None)
    try:
        return _main(ns, prog_args)
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR


def _prefix_hint(ctx: _Ctx) -> Optional[Path]:
    """The C:\\ drive already known before any question: ``--prefix``, ``--shared`` or the file's own."""
    if ctx.ns.prefix:
        return prefix_path(ctx.ns.prefix)
    if ctx.ns.shared:
        return prefix_path(SHARED_SLUG)
    inside = find_prefix_for_path(ctx.path)
    return prefix_path(inside) if inside else None


def _classify(ctx: _Ctx) -> None:
    """``formats.detect`` + ``formats.plan_action`` (content first, suffix as fallback)."""
    formats = _mod("formats")
    if formats is None:
        if ctx.path.suffix.lower() not in (".exe", ".msi", ".msp", ".bat", ".cmd", ".lnk"):
            log.warning("'%s' is not a typical Windows program file (.exe/.msi/.bat) - trying anyway", ctx.path.name)
        return
    try:
        ctx.det = formats.detect(ctx.path)
        try:
            # optional keywords of W-A1's plan_action: the known C:\ drive (C:\ paths, ClickOnce's
            # .NET check) and the home folder (DOSBox Flatpak sandbox)
            ctx.fplan = formats.plan_action(ctx.path, ctx.det, list(ctx.args), which=_which,
                                            prefix=_prefix_hint(ctx), home=user_home())
        except TypeError:
            ctx.fplan = formats.plan_action(ctx.path, ctx.det, list(ctx.args), which=_which)
    except Exception as exc:  # noqa: BLE001 - a classifier problem must not block a launch
        log.warning("could not classify '%s' (%s); treating it as a Windows program", ctx.path.name, exc)
        ctx.det = ctx.fplan = None


def _main(ns: argparse.Namespace, prog_args: List[str], *, depth: int = 0) -> int:
    fb = Feedback(gui_wanted() and not (ns.info or ns.dry_run))
    raw = Path(os.path.expanduser(str(ns.file)))
    ctx = _Ctx(ns=ns, fb=fb, path=raw, args=list(prog_args), depth=depth)

    # -- 1. the file ---------------------------------------------------------
    if not raw.exists():
        return _fail(ctx, f"File not found: {raw}", code=EXIT_USAGE)
    try:
        ctx.path = raw.resolve()
    except OSError:
        ctx.path = raw.absolute()

    if ctx.path.suffix.lower() == ".lnk":
        target, lnk_info, slug_hint, err = resolve_shortcut(ctx.path, ns.prefix)
        if target is None:
            return _fail(ctx, err or f"Cannot resolve the shortcut {ctx.path.name}")
        log.info("Shortcut '%s' -> %s", ctx.path.name, target)
        ctx.path, ctx.lnk_info = target, lnk_info
        if lnk_info is not None and lnk_info.arguments and not ctx.args:
            ctx.args = split_windows_args(lnk_info.arguments)
        if slug_hint and not ns.prefix:
            ns.prefix = slug_hint
    if ctx.path.is_dir():
        return _fail(ctx, f"{ctx.path} is a folder, not a Windows program.", code=EXIT_USAGE)

    # -- 2. what is it? ---------------------------------------------------------
    _classify(ctx)
    if ns.info:
        return _cmd_info(ctx)
    handler = ctx.handler
    code = ctx.plan("exit_code", EXIT_OK)
    if handler == "explain":
        note = str(getattr(getattr(ctx.det, "format", None), "note", "") or "")
        text = str(ctx.plan("message", "") or note)
        if isinstance(code, int) and code not in (EXIT_OK, EXIT_UNSUPPORTED):
            return _fail(ctx, text or f"Lindos cannot open '{ctx.path.name}'.", code=code)  # e.g. unreadable file
        return _explain(ctx, text)
    if isinstance(code, int) and code != EXIT_OK:
        # the handler is right but a tool is missing (pwsh, DOSBox, cabextract, udisks2, xdg-open)
        return _fail(ctx, str(ctx.plan("message", "") or f"'{ctx.path.name}' cannot be opened."), code=code)

    # -- 3. dispatch ------------------------------------------------------------------
    fn = _HANDLERS.get(handler)
    if fn is None:
        log.warning("unknown file handler '%s' - trying to run '%s' as a Windows program", handler, ctx.path.name)
        fn = _handle_wine
    return fn(ctx)


# ---------------------------------------------------------------------------
# --info
# ---------------------------------------------------------------------------


def _cmd_info(ctx: _Ctx) -> int:
    ns, path = ctx.ns, ctx.path
    warning = ""
    try:
        info = analyze_file(path)
    except CoreMissing as exc:
        info, warning = _fallback_info(path), str(exc)
    kind = str(getattr(info, "kind", "unknown") or "unknown")
    config = load_user_config()
    registered = find_registered(path)
    slug = pick_slug(path, info, prefix_opt=ns.prefix, shared=ns.shared, registered=registered)
    pfx_path = prefix_path(slug)
    marker = read_marker(pfx_path) if not ns.new_prefix else {}
    recipe = _recipe_for(slug, marker)
    profile: Optional[Profile] = resolve_profile(exe=path.name, appid=ns.appid)
    requested_runner = ns.runner
    if profile is not None and profile.runner and not requested_runner and profile.possible:
        requested_runner = profile.runner
    runner, reason = choose_runner(info, config, requested=requested_runner, recipe=recipe,
                                   prefix_runner=str(marker.get("runner")) if marker.get("runner") else None)
    dxvk_async = ns.dxvk_async if ns.dxvk_async is not None else (profile.dxvk_async if profile else None)
    gs_spec = gamescope_spec(ns, profile)
    data = info_to_dict(info)
    data.update({
        "suggested_prefix": slug,
        "prefix_path": str(pfx_path),
        "prefix_exists": pfx_path.is_dir(),
        "suggested_runner": runner,
        "runner_reason": reason,
        "recipe": recipe.id if recipe else None,
        "profile": profile.as_dict() if profile else None,
        "perf_env": resolved_perf_env(runner, kind, dxvk_async=dxvk_async, hdr=gs_spec.hdr),
        "gamescope": gs_spec.as_dict() if gs_spec.enabled else None,
        "registered": registered,
        "shortcut": ctx.lnk_info.as_dict() if ctx.lnk_info else None,
        "log": str(log_path_for(slug)),
        "format": format_block(ctx.det),
        "action": action_block(ctx.fplan),
    })
    if warning:
        data["warning"] = warning
    if profile is not None and not profile.possible:
        data["route_hint"] = f"lindos-game route {profile.id}"
    handler = ctx.handler
    if handler == "msix":
        msix = _mod("msix")
        try:
            pkg = msix.inspect(path) if msix is not None else None
            if pkg is not None:
                data["package"] = pkg.as_dict()
                if getattr(pkg, "package_family_name", ""):
                    data["suggested_prefix"] = safe_slug(ns.prefix or pkg.package_family_name)
                    data["prefix_path"] = str(prefix_path(data["suggested_prefix"]))
        except Exception as exc:  # noqa: BLE001 - --info never fails on a bad package
            data["package"] = {"error": str(exc)}
    elif handler == "appinstaller":
        msix = _mod("msix")
        try:
            data["appinstaller"] = msix.parse_appinstaller(path) if msix is not None else None
        except Exception as exc:  # noqa: BLE001
            data["appinstaller"] = {"error": str(exc)}
    elif handler == "regedit":
        try:
            data["reg"] = {"deletions": reg_deletions(path)}
        except (OSError, ValueError) as exc:
            data["reg"] = {"error": str(exc)}
    _print_json(data)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Wine handlers (the program's own C:\ drive)
# ---------------------------------------------------------------------------


def _recipe_for(slug: str, marker: Dict[str, object]) -> Optional[Recipe]:
    recipe: Optional[Recipe] = None
    recipe_id = str(marker.get("recipe") or "") if marker else ""
    if recipe_id:
        recipe = get_recipe(recipe_id)
    if recipe is None and slug != SHARED_SLUG:
        recipe = get_recipe(slug)  # `--prefix photoshop-cc-2021` picks the recipe of the same name
    return recipe


def _refuse_profile(ctx: _Ctx, profile: Profile) -> int:
    note = profile.notes or "this title uses kernel-level anti-cheat that does not run on Linux"
    msg = (f"'{profile.title}' cannot run on Lindos: {note}\n"
           "Lindos does not fake anti-cheat or attestation - doing so only gets you hardware-banned. "
           "See docs/ANTI-CHEAT.md.\n"
           f"Other ways to play it (official cloud streaming where offered, or restarting into Windows): "
           f"lindos-game route {profile.id}")
    return _fail(ctx, msg, code=EXIT_UNSUPPORTED)


_CHOOSE_TEXT = {
    "regedit": ("Merge '{file}' into which C:\\ drive?\nEvery Windows program has its own C:\\ drive - pick the one "
                "of the program these settings are for."),
    "msiexec-patch": ("Apply the update '{file}' to which C:\\ drive?\nPick the C:\\ drive where the program being "
                      "updated is installed."),
}


def _choose_prefix(ctx: _Ctx) -> Tuple[Optional[str], str]:
    """The C:\\ drive a .reg/.msp is for: ``(slug, "")``, ``("", "")`` if cancelled, ``(None, why)``."""
    ns = ctx.ns
    if ns.prefix:
        return safe_slug(ns.prefix), ""
    if ns.shared:
        return SHARED_SLUG, ""
    inside = find_prefix_for_path(ctx.path)
    if inside:
        return safe_slug(inside), ""
    rows: List[Tuple[str, str]] = []
    for item in list_prefixes():
        apps = [str(a) for a in item.get("apps", []) or []]
        rows.append((str(item["slug"]), ", ".join(apps[:4]) or "(no programs registered)"))
    if not any(slug == SHARED_SLUG for slug, _ in rows):
        rows.append((SHARED_SLUG, "shared C:\\ drive for small tools"))
    names = ", ".join(slug for slug, _ in rows)
    if ns.dry_run:
        return None, f"--dry-run needs --prefix NAME or --shared for '{ctx.path.name}' (available: {names})."
    question = _CHOOSE_TEXT.get(ctx.handler, "Use which C:\\ drive for '{file}'?").format(file=ctx.path.name)
    choice = ctx.fb.choose(question, rows)
    if choice is not None:
        return choice, ""
    if _stdin_isatty():
        print(question)
        for i, (slug, detail) in enumerate(rows, 1):
            print(f"  {i}) {slug}  - {detail}")
        try:
            reply = _input("Number of the C:\\ drive (Enter = cancel): ").strip()
        except EOFError:
            return "", ""
        if reply.isdigit() and 1 <= int(reply) <= len(rows):
            return rows[int(reply) - 1][0], ""
        if reply and safe_slug(reply) in {slug for slug, _ in rows}:
            return safe_slug(reply), ""
        return "", ""
    return None, (f"Choose the C:\\ drive for '{ctx.path.name}' with --prefix NAME (available: {names}) "
                  "or use --shared.")


def _win16_target(ctx: _Ctx, info: object, registered: Optional[Dict[str, object]]) -> Union[int, Tuple[str, str]]:
    """``(slug, arch)`` for a 16-bit Windows program by the installed Wine's WoW64 mode (§28.5)."""
    ns = ctx.ns
    details = ctx.plan("details", {}) or {}
    mode = str(details.get("wine_mode") or "") if isinstance(details, dict) else ""
    if not mode:
        mode = "unknown"
        dos = _mod("dos")
        if dos is not None:
            try:
                mode = str(dos.wine_wow64_mode(which=_which))
            except Exception as exc:  # noqa: BLE001 - probe problems fall back to the classic layout
                log.debug("Wine WoW64 probe failed: %s", exc)
    if mode == "new-wow64-no16bit":
        return _explain(ctx, (
            f"'{ctx.path.name}' is a 16-bit Windows program (from the Windows 3.x era). The Wine installed on "
            "this PC runs in the new 'WoW64' mode, which can only run 16-bit programs from Wine 10.16 / 11.0 "
            "on - this Wine is older. Install Wine 11 or newer from WineHQ and open the file again "
            "('lindos-compat doctor' shows how)."))
    if mode == "new-wow64-16bit":
        slug = pick_slug(ctx.path, info, prefix_opt=ns.prefix, shared=ns.shared, registered=registered)
        return slug, str(read_marker(prefix_path(slug)).get("arch") or "win64")
    # classic ("old") WoW64 -- Ubuntu's wine + wine32:i386, WineHQ's i386+amd64 -- or unknown:
    # 16-bit programs get a dedicated 32-bit C:\ drive
    hint = str(ctx.plan("prefix_hint", "") or "win16")
    slug = safe_slug(ns.prefix) if ns.prefix else (find_prefix_for_path(ctx.path) or safe_slug(hint))
    existing = str(read_marker(prefix_path(slug)).get("arch") or "")
    if existing and existing != "win32":
        log.warning("The C:\\ drive '%s' is 64-bit; with this Wine, 16-bit programs need a 32-bit one "
                    "(leave out --prefix to use the 'win16' drive).", slug)
    return slug, "win32"


def _tool_runner(ctx: _Ctx, info: object, config: object, recipe: Optional[Recipe],
                 marker: Dict[str, object]) -> Tuple[str, str]:
    """Runner for Wine tools (msiexec, regedit, control, wscript, rundll32) and 16-bit programs."""
    handler = ctx.handler
    prefix_runner = str(marker.get("runner") or "")
    if handler == "win16":
        return "wine", "16-bit Windows programs run with Wine (Proton is not used for them)"
    if ctx.ns.runner:
        return choose_runner(info, config, requested=ctx.ns.runner, recipe=recipe,
                             prefix_runner=prefix_runner or None)
    if str(ctx.plan("force_runner", "") or "") != "wine":
        return choose_runner(info, config, recipe=recipe, prefix_runner=prefix_runner or None)
    have_umu = _which("umu-run") is not None
    if prefix_runner == "umu" and have_umu:
        return "umu", "this C:\\ drive was created by Proton (umu), so Proton's own Wine opens the file"
    if find_wine(_which):
        return "wine", f"{_TOOL_LABEL.get(handler, 'this tool')} comes with Wine"
    if have_umu:
        return "umu", "Wine is not installed; using Proton (umu) instead"
    return "wine", "Wine is not installed"


def _inf_kind(path: Path) -> str:
    formats = _mod("formats")
    fn = getattr(formats, "inf_kind", None) if formats is not None else None
    if not callable(fn):
        return "unknown"
    try:
        value = fn(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("formats.inf_kind failed: %s", exc)
        return "unknown"
    if isinstance(value, dict):
        value = value.get("kind")
    return str(value or "unknown").lower()


def _has_dotnet_framework(prefix: Path) -> bool:
    formats = _mod("formats")
    fn = getattr(formats, "prefix_has_dotnet_framework", None) if formats is not None else None
    if not callable(fn):
        return False
    try:
        return bool(fn(prefix))
    except Exception as exc:  # noqa: BLE001
        log.debug("formats.prefix_has_dotnet_framework failed: %s", exc)
        return False


def _deletion_lines(deletions: List[str], slug: str) -> List[str]:
    lines = [f"This file DELETES {len(deletions)} item(s) from the C:\\ drive '{slug}':"]
    lines += [f"  - {item}" for item in deletions[:20]]
    if len(deletions) > 20:
        lines.append(f"  ... and {len(deletions) - 20} more")
    return lines


def _reg_question(ctx: _Ctx, slug: str) -> str:
    """Windows' "Merge" question for a ``.reg`` file, always listing what it deletes.

    Uses the preview ``formats.plan_action`` attached (``details["reg"]``) and
    ``formats.reg_confirm_text``; falls back to :func:`reg_deletions`.  Raises ``ValueError``
    / ``OSError`` when the file is not a registration file.
    """
    path = ctx.path
    closing = ["", f"This only changes the Windows settings of the C:\\ drive '{slug}', not Linux.",
               f"Merge '{path.name}' into the C:\\ drive '{slug}'?"]
    details = ctx.plan("details", {}) or {}
    preview = details.get("reg") if isinstance(details, dict) else None
    formats = _mod("formats")
    confirm_fn = getattr(formats, "reg_confirm_text", None) if formats is not None else None
    if isinstance(preview, dict) and callable(confirm_fn):
        try:
            text = str(confirm_fn(path.name, preview))
        except Exception as exc:  # noqa: BLE001 - use the built-in wording below
            log.debug("reg_confirm_text failed: %s", exc)
        else:
            deletions = _deletions_from_preview(preview) or []
            if deletions and not all(d in text for d in deletions[:3]):
                text += "\n" + "\n".join(_deletion_lines(deletions, slug))  # never hide a deletion
            return text + "\n" + "\n".join(closing)
    deletions = reg_deletions(path)
    lines = [
        "Adding information can unintentionally change or delete values and cause programs to stop "
        "working correctly.",
        f"If you do not trust the source of this information in '{path.name}', do not add it.",
    ]
    if deletions:
        lines += [""] + _deletion_lines(deletions, slug)
    return "\n".join(lines + closing)


def _gate_wine_handler(ctx: _Ctx, pfx: Path, tail: Optional[List[str]]) -> Optional[int]:
    """Format-specific refusals before anything runs (ClickOnce, INF).  Returns an exit code or None."""
    name = ctx.path.name
    if ctx.handler == "clickonce" and not _has_dotnet_framework(pfx):
        return _explain(ctx, (
            f"'{name}' is a ClickOnce application. It needs Microsoft .NET Framework 4.x inside its C:\\ drive, "
            "and the .NET Framework licence belongs to a licensed copy of Windows, so Lindos does not install it "
            "for you. If you own a Windows licence you may add it yourself "
            f"(WINEPREFIX='{pfx}' winetricks dotnet48) and open this file again. Many ClickOnce apps also offer "
            "a normal installer on their website."))
    if ctx.handler == "inf-install":
        if _inf_kind(ctx.path) == "driver":
            return _explain(ctx, (
                f"'{name}' installs a hardware driver. Windows drivers cannot be used on Linux - Linux has its "
                "own drivers, and most hardware works out of the box. Run 'lindos-drivers detect' to see what "
                "Lindos can install for your hardware."))
        if tail and len(str(tail[-1])) > INF_PATH_MAX:
            return _explain(ctx, (
                f"The path of '{name}' is longer than Windows' 260-character limit for setup files. Move the "
                "folder somewhere with a shorter path (for example your Downloads folder) and try again."))
    return None


def _handle_wine(ctx: _Ctx) -> int:  # noqa: C901 - the launch flow is linear
    ns, fb, path, handler = ctx.ns, ctx.fb, ctx.path, ctx.handler

    try:
        info = analyze_file(path)
    except CoreMissing as exc:
        return _fail(ctx, str(exc))
    kind = str(getattr(info, "kind", "unknown") or "unknown")
    display_name = _display_name(info, path)
    config = load_user_config()
    prog_args = list(ctx.args)
    registered = find_registered(path) if handler == "run" else None
    if registered and not prog_args and registered.get("args"):
        prog_args = split_windows_args(str(registered["args"]))

    # -- per-title profile (SPEC-KERNEL §17.3): honest refusal before anything is prepared --
    profile: Optional[Profile] = resolve_profile(exe=path.name, appid=ns.appid) if handler == "run" else None
    if profile is not None and not profile.possible:
        return _refuse_profile(ctx, profile)

    # -- which C:\ drive ------------------------------------------------------------
    arch_override: Optional[str] = None
    if handler in CHOOSE_PREFIX_HANDLERS:
        chosen, why = _choose_prefix(ctx)
        if chosen is None:
            return _fail(ctx, why, code=EXIT_USAGE)
        if not chosen:
            return _cancelled(ctx)
        slug = chosen
    elif handler == "win16":
        target = _win16_target(ctx, info, registered)
        if isinstance(target, int):
            return target
        slug, arch_override = target
    else:
        slug = pick_slug(path, info, prefix_opt=ns.prefix, shared=ns.shared, registered=registered)
    pfx_path = prefix_path(slug)
    marker = read_marker(pfx_path) if not ns.new_prefix else {}
    recipe = _recipe_for(slug, marker)

    # -- runner ------------------------------------------------------------------------
    if handler == "run":
        requested_runner = ns.runner
        if profile is not None and profile.runner and not requested_runner and profile.possible:
            requested_runner = profile.runner
        runner, reason = choose_runner(info, config, requested=requested_runner, recipe=recipe,
                                       prefix_runner=str(marker.get("runner")) if marker.get("runner") else None)
    else:
        runner, reason = _tool_runner(ctx, info, config, recipe, marker)
    if ns.runner and runner != ns.runner:
        log.warning("Runner '%s' is not available: %s", ns.runner, reason)
    if handler == "win16" and runner != "wine":
        return _fail(ctx, f"16-bit Windows programs need Wine, which is not installed.\n{INSTALL_HINT}")

    tail: Optional[List[str]] = None
    if handler != "run":
        tail = [str(t) for t in (ctx.plan("wine_tail", []) or [])] or None
    gate = _gate_wine_handler(ctx, pfx_path, tail)
    if gate is not None:
        return gate

    # effective toggles (command-line flag wins over the profile)
    dxvk_async = ns.dxvk_async if ns.dxvk_async is not None else (profile.dxvk_async if profile else None)
    gs_spec = gamescope_spec(ns, profile)
    mangohud_flag = bool(ns.mangohud or (profile is not None and profile.mangohud))

    # -- plan -------------------------------------------------------------------------
    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    extra_env, overrides = load_prefix_settings(slug)
    if profile is not None:
        extra_env = {**_profile_env(profile), **extra_env}
    if recipe is not None:
        extra_env = {**recipe.env, **extra_env}
        overrides = {**recipe.dll_overrides, **overrides}
    arch = arch_override or str(marker.get("arch") or (recipe.arch if recipe else "win64"))
    workdir = ""
    if handler == "run":
        workdir = str(registered.get("workdir") or "") if registered else (ctx.lnk_info.working_dir if ctx.lnk_info else "")
    try:
        plan: RunPlan = build_plan(runner=runner, slug=slug, exe=path, kind=kind, config=config,
                                   args=prog_args if tail is None else [], tail=tail,
                                   gamemode_flag=ns.gamemode, mangohud_flag=mangohud_flag, headless=headless,
                                   extra_env=extra_env, dll_overrides=overrides, arch=arch, reason=reason,
                                   dxvk_async=dxvk_async, hdr=gs_spec.hdr, gamescope=gs_spec)
    except FileNotFoundError as exc:
        what = {"umu": "umu-launcher (umu-run)", "bottles": "Bottles (flatpak)", "wine": "Wine"}.get(runner, runner)
        return _fail(ctx, f"{what} is not installed, so '{path.name}' cannot be opened ({exc}).\n{INSTALL_HINT}")
    if workdir:
        wd = windows_to_unix(workdir, pfx_path, must_exist=True) if is_windows_path(workdir) else Path(workdir)
        if wd is not None and wd.is_dir():
            plan.cwd = str(wd)
    for w in plan.warnings:
        log.warning("%s", w)

    question: Optional[str] = None
    if handler == "regedit":
        try:
            question = _reg_question(ctx, slug)
        except (OSError, ValueError) as exc:
            return _fail(ctx, f"'{path.name}' is not a Windows registration (.reg) file that Lindos can merge: {exc}")
    elif ctx.plan("confirm"):
        question = str(ctx.plan("confirm"))
    note = str(ctx.plan("message", "") or "")
    if note and not ns.dry_run:
        log.info("%s", note)

    if ns.dry_run:
        out = plan.as_dict()
        out["info"] = info_to_dict(info)
        out["recipe"] = recipe.id if recipe else None
        out["handler"] = handler
        out["format"] = format_block(ctx.det)
        if question:
            out["confirm"] = question
        _print_json(out)
        return EXIT_OK

    if question:
        ok_label = "Merge" if handler == "regedit" else "Yes"
        ans = _confirm(ctx, question, ok_label=ok_label, cancel_label="Cancel" if handler == "regedit" else "No")
        if ans is None:
            return _needs_yes(ctx, f"Merging '{path.name}'" if handler == "regedit" else f"Opening '{path.name}'")
        if not ans:
            return _cancelled(ctx)

    # -- 4. C:\ drive -------------------------------------------------------------
    fb.progress("Preparing Windows compatibility\u2026")
    log.info("Runner: %s (%s); C:\\ drive: %s", runner, reason, pfx_path)
    if runner == "bottles":
        if not ensure_bottle(slug, environment="gaming" if kind == "game" else "application", log_file=plan.log_path):
            return _fail(ctx, f"Bottles could not create the bottle '{slug}'. Log: {plan.log_path}")
    state: PrefixState = ensure_prefix(slug, runner=runner, arch=arch, fresh=ns.new_prefix, dll_overrides=overrides,
                                       headless=headless, on_progress=fb.progress, log_file=plan.log_path,
                                       recipe_id=recipe.id if recipe else None)
    for w in state.warnings:
        log.warning("%s", w)
    if runner == "wine" and not state.initialized:
        log.warning("Wine did not finish creating the C:\\ drive; continuing anyway (see %s)", plan.log_path)
    if state.arch != arch and runner != "umu":
        # the prefix already existed with another architecture: honour it
        plan.env["WINEARCH"] = "win32" if state.arch == "win32" else "win64"

    scan_prefix = bottle_prefix_path(slug) if runner == "bottles" else pfx_path
    scanning = handler in SCAN_HANDLERS
    deep = kind in ("installer", "msi", "unknown") or handler != "run"
    before = snapshot(scan_prefix, deep=deep) if scanning else {}

    # -- 5. run -----------------------------------------------------------------------
    fb.close_progress()
    log.info("Starting %s (%s) - log: %s", path.name, handler if handler != "run" else kind, plan.log_path)
    started = time.time()
    rc = run_plan(plan)
    log.info("'%s' exited with code %s after %.0f s", path.name, rc, time.time() - started)

    # -- 6. what got installed? -----------------------------------------------------
    created: List[str] = []
    if scanning:
        after = snapshot(scan_prefix, deep=deep)
        new_files = diff_snapshots(before, after)
        apps = discover_apps(new_files, scan_prefix, product_hint=str(getattr(info, "product", "") or path.stem))
        for app in apps:
            if _register_found(app, prefix_slug=slug, runner=runner, kind="app"):
                created.append(app.name)
        if not apps and handler in ("run", "win16") and kind in ("app", "game") and path.suffix.lower() == ".exe" \
                and not registered and not SKIP_NAME_RE.search(path.stem) and not _TEMP_DIR_RE.search(str(path.parent)):
            direct = FoundApp(name=display_name, exe=path, source=str(path), icon_source=path,
                              win_exe=unix_to_windows(path, scan_prefix) or "")
            if _register_found(direct, prefix_slug=slug, runner=runner, kind=kind):
                log.info("'%s' was added to the Start Menu and to Lindos Settings > Windows apps", display_name)
        if created or apps or kind in ("app", "game"):
            _refresh_desktop_database()
        link_windows_apps(slug, created[0] if len(created) == 1 else display_name)

    # -- 7. feedback ------------------------------------------------------------------
    return _wine_feedback(ctx, rc=rc, slug=slug, created=created, log_path=plan.log_path)


def _wine_feedback(ctx: _Ctx, *, rc: int, slug: str, created: List[str], log_path: Path) -> int:
    fb, name, handler = ctx.fb, ctx.path.name, ctx.handler
    if created:
        names = ", ".join(created[:4]) + (" \u2026" if len(created) > 4 else "")
        fb.notify("Windows program installed", f"{names} - now in the Start Menu (Wine / Windows apps)")
        print(f"Installed: {names}\nStart Menu entries were created; also listed in Lindos Settings > Windows apps.")
    msiexec = handler in ("msiexec-install", "msiexec-patch") or (handler == "run" and ctx.path.suffix.lower() in (".msi", ".msp"))
    if msiexec and rc in MSI_RESTART_CODES:
        print(f"'{name}' finished; the program asks to be restarted before the change takes effect.")
        rc = 0
    if rc == 0:
        done = {
            "regedit": ("Registry updated",
                        f"The keys and values contained in {name} were added to the C:\\ drive '{slug}'."),
            "msiexec-patch": ("Update applied", f"The update {name} was applied to the C:\\ drive '{slug}'."),
            "inf-install": ("Installed", f"{name} was installed into the C:\\ drive '{slug}'."),
        }.get(handler)
        if done:
            fb.notify(*done)
            print(done[1])
        return EXIT_OK
    if created:
        return EXIT_OK
    if msiexec and rc == 1602:
        log.info("The installation was cancelled.")
        return EXIT_ERROR
    msi_msgs = {
        1618: "Another installation is already running in this C:\\ drive. Wait for it to finish, then try again.",
        1638: "Another version of this program is already installed in this C:\\ drive.",
        1642: (f"This update does not match any program installed in the C:\\ drive '{slug}'. Open it again and "
               "choose the C:\\ drive where the program is installed."),
    }
    if msiexec and rc in msi_msgs:
        msg = f"'{name}': {msi_msgs[rc]}\nLog: {log_path}"
    elif rc == 127:
        msg = f"'{name}' could not be started.\n{INSTALL_HINT}\nLog: {log_path}"
    else:
        msg = (f"'{name}' ended with exit code {rc}.\n"
               f"If it did not work, the log is here (copy it into a bug report):\n{log_path}")
    log.error("%s", msg.replace("\n", " "))
    fb.error(msg)
    return EXIT_ERROR


# ---------------------------------------------------------------------------
# Linux-side handlers
# ---------------------------------------------------------------------------


def _handle_dos(ctx: _Ctx) -> int:
    """DOS programs → DOSBox-X / DOSBox / DOSBox Staging (no C:\\ drive, SPEC-WINDOWS §28.5)."""
    dos = _mod("dos")
    hint = str(getattr(dos, "DOSBOX_INSTALL_HINT", "") or "sudo apt install dosbox-x")
    argv = [str(a) for a in (ctx.plan("host_argv", []) or [])]
    if not argv and dos is not None:
        try:
            if dos.find_dosbox(which=_which) is not None:
                argv = [str(a) for a in dos.dosbox_argv(ctx.path, list(ctx.args), which=_which, home=user_home())]
        except Exception as exc:  # noqa: BLE001 - no DOSBox is reported below
            log.debug("DOSBox lookup failed: %s", exc)
            argv = []
    if not argv:
        return _fail(ctx, f"'{ctx.path.name}' is a DOS program. DOS programs run in DOSBox, which is not "
                          f"installed.\nInstall it with:  {hint}")
    if ctx.ns.dry_run:
        return _host_dry_run(ctx, argv)
    stop = _plan_confirm(ctx)
    if stop is not None:
        return stop
    log_path = log_path_for("dos-" + safe_slug(ctx.path.stem))
    log.info("Starting %s in DOSBox - log: %s", ctx.path.name, log_path)
    rc = run_host(argv, log_path=log_path, cwd=str(ctx.path.parent), what=str(ctx.path))
    if rc == 127:
        return _fail(ctx, f"DOSBox could not be started.\nInstall it with:  {hint}\nLog: {log_path}")
    if rc != 0:
        return _fail(ctx, f"DOSBox ended with exit code {rc} while running '{ctx.path.name}'.\nLog: {log_path}")
    return EXIT_OK


_PWSH_QUESTION = (
    "Run the PowerShell script '{file}'?\n\n"
    "Scripts can change or delete your files - only run scripts you trust.\n"
    "PowerShell 7 on Linux cannot use Windows-only commands (services, registry, WMI/CIM, Windows features), "
    "so scripts written for Windows often stop with errors.\n"
    "Lindos asks because PowerShell on Linux has no execution policy that could stop a script."
)


def _handle_pwsh(ctx: _Ctx) -> int:
    """``.ps1`` → PowerShell 7 (``pwsh -NoProfile -File``) after a confirmation."""
    name = ctx.path.name
    pwsh = _which("pwsh")
    if not pwsh:
        return _fail(ctx, f"'{name}' is a PowerShell script. Running it needs PowerShell 7, which is not "
                          f"installed. Install it from Microsoft's repository:\n  {POWERSHELL_INSTALL_CMD}")
    argv = [str(a) for a in (ctx.plan("host_argv", []) or [])] or \
        [pwsh, "-NoProfile", "-File", str(ctx.path), *ctx.args]
    if ctx.plan("confirm"):
        question = "\n\n".join(t for t in (str(ctx.plan("confirm")), str(ctx.plan("message", "") or ""),
                                            "Lindos asks because PowerShell on Linux has no execution policy "
                                            "that could stop a script.") if t)
    else:
        question = _PWSH_QUESTION.format(file=name)
    if ctx.ns.dry_run:
        return _host_dry_run(ctx, argv, confirm=question)
    ans = _confirm(ctx, question, ok_label="Run", cancel_label="Cancel")
    if ans is None:
        return _needs_yes(ctx, f"Running the PowerShell script '{name}'")
    if not ans:
        return _cancelled(ctx)
    cwd = str(ctx.path.parent)
    if not ctx.fb.enabled and _stdin_isatty():
        rc = int(_call(argv, cwd=cwd))  # a terminal is attached: the script talks to it directly
        if rc != 0:
            log.error("The script '%s' ended with exit code %s", name, rc)
            return EXIT_ERROR
        return EXIT_OK
    term = _terminal_argv(argv)
    if term and _spawn(term, cwd=cwd):
        return EXIT_OK
    log_path = log_path_for("pwsh-" + safe_slug(ctx.path.stem))
    rc = run_host(argv, log_path=log_path, cwd=cwd, what=str(ctx.path))
    if rc != 0:
        return _fail(ctx, f"The script '{name}' ended with exit code {rc}.\nIts output is in {log_path}")
    ctx.fb.info(f"The script '{name}' finished. Its output is in {log_path}")
    return EXIT_OK


def _handle_url(ctx: _Ctx) -> int:
    """``.url`` Internet shortcuts → ``xdg-open`` (web, e-mail and FTP links only)."""
    name = ctx.path.name
    host_argv = ctx.plan("host_argv", []) or []
    url = str(host_argv[-1]) if host_argv else ""
    if not url:
        formats = _mod("formats")
        parser = getattr(formats, "parse_url_shortcut", None) if formats is not None else None
        try:
            url = _url_from(parser(ctx.path)) if callable(parser) else ""
        except Exception as exc:  # noqa: BLE001
            log.debug("parse_url_shortcut failed: %s", exc)
            url = ""
    if not url:
        return _fail(ctx, f"'{name}' is an Internet shortcut without a web address (no URL= line).")
    ok, why = url_allowed(url)
    if not ok:
        shown = url if len(url) <= 120 else url[:117] + "..."
        return _explain(ctx, f"'{name}' points to '{shown}'. {why}")
    opener = _which("xdg-open")
    if not opener:
        return _fail(ctx, "xdg-open is missing, so links cannot be opened (sudo apt install xdg-utils).")
    argv = [opener, url]
    if ctx.ns.dry_run:
        return _host_dry_run(ctx, argv)
    if not _spawn(argv):
        return _fail(ctx, f"Could not open {url}")
    return EXIT_OK


def _handle_extract(ctx: _Ctx) -> int:
    """``.cab`` → ``cabextract`` into a new folder next to it (or in Downloads), then open it."""
    name = ctx.path.name
    cab = _which("cabextract")
    if not cab:
        return _fail(ctx, f"'{name}' is a Windows cabinet (.cab) archive. Unpacking it needs cabextract:  "
                          "sudo apt install cabextract")
    details = ctx.plan("details", {}) or {}
    planned = details.get("dest") if isinstance(details, dict) else None
    if planned:
        dest = Path(str(planned))  # chosen by formats.plan_action (next to the file, or in Downloads)
        parent = dest.parent
    else:
        parent = ctx.path.parent
        if not os.access(str(parent), os.W_OK):
            downloads = user_home() / "Downloads"
            parent = downloads if downloads.is_dir() else user_home()
        dest = _unique_dir(parent, ctx.path.stem)
    if dest.exists():
        dest = _unique_dir(dest.parent, dest.name)
    argv = [cab, "-d", str(dest), str(ctx.path)]
    if ctx.ns.dry_run:
        return _host_dry_run(ctx, argv, destination=str(dest))
    members = _cab_members(cab, ctx.path)
    if members is None:
        return _fail(ctx, f"'{name}' could not be read - the archive is damaged or not a cabinet file.")
    unsafe = [m for m in members if cab_member_unsafe(m)]
    if unsafe:
        return _fail(ctx, f"'{name}' was not unpacked: it contains file names that would be written outside "
                          f"the destination folder (for example '{unsafe[0]}'). This is typical of a damaged or "
                          "malicious archive.")
    stop = _plan_confirm(ctx)
    if stop is not None:
        return stop
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return _fail(ctx, f"Cannot create the folder {dest}: {exc}")
    log_path = log_path_for("cab-" + safe_slug(ctx.path.stem))
    rc = run_host(argv, log_path=log_path, cwd=str(parent), what=str(ctx.path))
    if rc != 0:
        return _fail(ctx, f"Unpacking '{name}' failed (cabextract exit code {rc}).\nLog: {log_path}")
    print(f"Unpacked '{name}' into {dest}")
    if not _open_folder(dest):
        ctx.fb.info(f"Unpacked '{name}' into {dest}")
    return EXIT_OK


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _handle_mount(ctx: _Ctx) -> int:
    """``.iso`` / ``.img`` → read-only loop mount, then "Run setup?" like Windows AutoPlay (§28.6)."""
    name = ctx.path.name
    di = _mod("diskimage")
    if di is None:
        return _fail(ctx, "Disk image support is missing from this installation (reinstall lindos-compat).")
    if not _which("udisksctl"):
        return _fail(ctx, f"Opening the disk image '{name}' needs udisks2:  sudo apt install udisks2")
    if ctx.ns.dry_run:
        argv = [str(a) for a in (ctx.plan("host_argv", []) or [])] or \
            ["udisksctl", "loop-setup", "-r", "-f", str(ctx.path)]
        return _host_dry_run(ctx, argv, read_only=True)
    if ctx.depth > 0:
        return _fail(ctx, f"'{name}' is a disk image inside a disk image; open it from the file manager.")
    ctx.fb.progress(f"Opening {name}\u2026")
    try:
        device = str(di.loop_setup(ctx.path, which=_which))
        mount = Path(di.mount_loop(device))
    except Exception as exc:  # noqa: BLE001 - udisks errors are shown to the user
        return _fail(ctx, f"Could not open the disk image '{name}': {exc}")
    ctx.fb.close_progress()
    try:
        autorun = di.find_autorun(mount) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("autorun.inf unreadable: %s", exc)
        autorun = {}
    label = str(autorun.get("label") or ctx.path.stem)
    try:
        setup = di.find_setup(mount)
    except Exception as exc:  # noqa: BLE001
        log.debug("setup lookup failed: %s", exc)
        setup = None
    if setup is not None and not _inside(Path(setup), mount):
        log.warning("Ignoring autorun target outside the disk image: %s", setup)
        setup = None
    print(f"'{name}' is open (read-only) at {mount}")
    try:
        eject = " && ".join(" ".join(cmd) for cmd in di.detach_command(device))
        print(f"To eject it later:  {eject}")
    except Exception:  # noqa: BLE001
        pass
    if setup is not None:
        setup = Path(setup)
        question = (f"'{label}' is now open (read-only).\n\nRun {setup.name} from {label}?\n\n"
                    "Only run programs from discs and downloads you trust.")
        ans = _confirm(ctx, question, ok_label=f"Run {setup.name}", cancel_label="Open folder")
        if ans:
            sub = copy.copy(ctx.ns)
            sub.file = str(setup)
            sub.args = []
            sub.info = sub.dry_run = False
            return _main(sub, [], depth=ctx.depth + 1)
    if not _open_folder(mount):
        ctx.fb.info(f"'{label}' is open (read-only) at {mount}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# MSIX / APPX / .appinstaller
# ---------------------------------------------------------------------------


def _msix_unsupported_text(info: object, name: str) -> str:
    reason = str(getattr(info, "reason", "") or "").strip()
    kind = str(getattr(info, "kind", "") or "")
    if getattr(info, "framework", False) or getattr(info, "resource_package", False):
        base = (f"'{name}' is a component (a framework or resource package) that other apps use - it is not an "
                "app you can start.")
    elif kind == "encrypted":
        base = (f"'{name}' is an encrypted Microsoft Store package. Its contents are locked to the Store, so no "
                "program outside Windows can open it. Get the app from its developer's website, use a Linux "
                "alternative, or use the Windows virtual machine (lindos-vm).")
    elif kind == "msixvc":
        base = (f"'{name}' is an Xbox / PC Game Pass game package (MSIXVC). Its programs stay encrypted, so it "
                "cannot run outside Windows.")
    else:
        base = (f"'{name}' is a UWP/WinUI app. Wine has no UWP app model, so it cannot run on Lindos. Try the web "
                "version, a Linux alternative, or the Windows virtual machine (lindos-vm).")
    return f"{base}\n{reason}" if reason and reason not in base else base


def _signature_line(info: object) -> str:
    if getattr(info, "signed", False):
        return "Signature: signed (not verified by Lindos)"
    if getattr(info, "unsigned_marker", False):
        return "Signature: unsigned (the publisher marked this package as unsigned)"
    return "Signature: none"


def _progress_cb(fb: Feedback, title: str) -> Callable[..., None]:
    def cb(*args: object, **_kw: object) -> None:
        text = f"Installing {title}\u2026"
        if args and isinstance(args[0], str):
            text = str(args[0])
        elif len(args) >= 2 and isinstance(args[0], (int, float)) and isinstance(args[1], (int, float)) and args[1]:
            text = f"Installing {title}\u2026 {int(100 * float(args[0]) / float(args[1]))}%"
        fb.progress(text)

    return cb


def _import_package_registry(inst: object, *, slug: str, runner: str, config: object, headless: bool) -> None:
    """Merge the package's own registry settings (``MsixInstall.reg_files``) into its C:\\ drive."""
    pfx = prefix_path(slug)
    for reg in list(getattr(inst, "reg_files", []) or []):
        reg = Path(reg)
        try:
            plan = build_plan(runner=runner, slug=slug, exe=reg, kind="app", config=config, headless=headless,
                              tail=["regedit", "/S", wine_path(reg, pfx)])
        except FileNotFoundError as exc:
            log.warning("The app's registry settings were not imported (%s); it may still work.", exc)
            return
        rc = run_plan(plan)
        if rc != 0:
            log.warning("Importing the app's registry settings from %s failed (exit code %s); see %s",
                        reg.name, rc, plan.log_path)


def _handle_msix(ctx: _Ctx, *, package: Optional[Path] = None, confirmed: bool = False,
                 expect: Optional[Dict[str, str]] = None) -> int:  # noqa: C901 - one linear install flow
    """MSIX/APPX (+bundles, uploads): inspect → honest refusal or install into a per-package C:\\ drive."""
    ns, fb = ctx.ns, ctx.fb
    path = package or ctx.path
    name = ctx.path.name if package is None else path.name
    msix = _mod("msix")
    if msix is None:
        return _fail(ctx, "Windows app package support is missing from this installation (reinstall lindos-compat).")
    error_cls: Any = getattr(msix, "MsixError", Exception)
    try:
        kind = str(msix.classify(path))
    except Exception as exc:  # noqa: BLE001
        log.debug("msix.classify failed: %s", exc)
        kind = "unknown"
    if kind == "appinstaller":
        if package is not None:
            return _fail(ctx, "The download is another App Installer file, not an app package; nothing was installed.")
        return _handle_appinstaller(ctx)
    if kind in ("encrypted", "msixvc"):
        try:
            info_enc: object = msix.inspect(path)
        except Exception:  # noqa: BLE001 - encrypted packages may not be readable at all
            info_enc = SimpleNamespace(kind=kind, reason="")
        if not getattr(info_enc, "kind", ""):
            _set(info_enc, "kind", kind)
        return _explain(ctx, _msix_unsupported_text(info_enc, name))
    if kind not in ("package", "bundle", "upload"):
        return _fail(ctx, f"'{name}' is not a valid Windows app package (MSIX/APPX); it may be damaged or incomplete.")
    try:
        info = msix.inspect(path)
    except error_cls as exc:
        return _fail(ctx, f"'{name}' could not be read as a Windows app package: {exc}")
    if expect is not None:
        try:
            msix.check_appinstaller_target(expect, info)
        except error_cls as exc:
            return _fail(ctx, f"{exc} It was deleted and nothing was installed.")
    apps = list(getattr(info, "apps", []) or [])
    win32_apps = [a for a in apps if getattr(a, "app_class", "") == "win32"]
    if str(getattr(info, "status", "")) == "unsupported" or getattr(info, "framework", False) \
            or getattr(info, "resource_package", False) or not win32_apps:
        return _explain(ctx, _msix_unsupported_text(info, name))

    title = _msix_app_name(win32_apps[0], info) if len(win32_apps) == 1 else \
        str(getattr(info, "display_name", "") or getattr(info, "name", "") or name)
    if title.lower().startswith("ms-resource:"):
        title = str(getattr(info, "name", "") or name)
    slug = safe_slug(ns.prefix) if ns.prefix else safe_slug(str(getattr(info, "package_family_name", "") or title))
    pfx_path = prefix_path(slug)
    if ns.dry_run:
        return _host_dry_run(ctx, [], package=info.as_dict() if hasattr(info, "as_dict") else info_to_dict(info),
                             prefix=slug, prefix_path=str(pfx_path),
                             install_dir=str(pfx_path / "drive_c" / "Program Files" / "WindowsApps"
                                             / str(getattr(info, "package_full_name", "") or slug)))

    lines = [f"Install {title}?", "",
             f"Publisher: {getattr(info, 'publisher_display', '') or getattr(info, 'publisher', '')}",
             f"Version: {getattr(info, 'version', '')} ({getattr(info, 'arch', '') or 'neutral'})",
             _signature_line(info)]
    reason = str(getattr(info, "reason", "") or "")
    if reason:
        lines += ["", reason]
    skipped = [a for a in apps if getattr(a, "app_class", "") != "win32"]
    if skipped:
        lines.append(f"{len(skipped)} part(s) of this package are UWP apps and will not be installed.")
    warnings = [str(w) for w in getattr(info, "warnings", []) or []]
    for w in warnings[:6]:
        lines.append(f"Note: {w}")
    store = [str(s) for s in getattr(info, "store_signals", []) or []]
    ok_label = "Install"
    if store:
        lines += ["", "This package comes from the Microsoft Store (" + ", ".join(store[:3]) + "). Store apps "
                  "usually check their licence with the Store and may refuse to start under Wine."]
        ok_label = "Try anyway"
    if not confirmed:
        ans = _confirm(ctx, "\n".join(lines), ok_label=ok_label, cancel_label="Cancel")
        if ans is None:
            return _needs_yes(ctx, f"Installing '{name}'")
        if not ans:
            return _cancelled(ctx)

    config = load_user_config()
    marker = read_marker(pfx_path) if not ns.new_prefix else {}
    runner, reason_r = choose_runner(SimpleNamespace(kind="app"), config, requested=ns.runner,
                                     prefix_runner=str(marker.get("runner") or "") or None)
    if runner == "bottles":
        runner, reason_r = "wine", "packaged apps are unpacked into a Lindos C:\\ drive (not a Bottles bottle)"
    log.info("Installing %s into the C:\\ drive '%s' (%s: %s)", title, slug, runner, reason_r)
    fb.progress(f"Installing {title}\u2026")
    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    state = ensure_prefix(slug, runner=runner, arch="win64", fresh=ns.new_prefix, headless=headless,
                          on_progress=fb.progress, log_file=log_path_for(slug))
    for w in state.warnings:
        log.warning("%s", w)
    try:
        inst = msix.install(path, pfx_path, on_progress=_progress_cb(fb, title))
    except error_cls as exc:
        return _fail(ctx, f"Installing {title} failed: {exc}")
    except OSError as exc:
        return _fail(ctx, f"Installing {title} failed: {exc}")
    _import_package_registry(inst, slug=slug, runner=runner, config=config, headless=headless)
    fb.close_progress()

    install_dir = Path(getattr(inst, "install_dir", pfx_path))
    package_win = unix_to_windows(install_dir, pfx_path) or wine_path(install_dir, pfx_path)
    pfn = str(getattr(info, "package_family_name", "") or "")
    created: List[str] = []
    for entry in getattr(inst, "apps", []) or []:
        app, exe, logo = entry[0], Path(entry[1]), (Path(entry[2]) if len(entry) > 2 and entry[2] else None)
        if getattr(app, "app_class", "win32") != "win32" or not getattr(app, "list_entry", True):
            continue
        exe, psf_args, psf_wd = _resolve_psf(app, exe, install_dir)
        params = expand_msix_macros(psf_args if psf_args is not None else str(getattr(app, "parameters", "") or ""),
                                    package_win)
        workdir = expand_msix_macros(psf_wd if psf_wd is not None else str(getattr(app, "working_dir", "") or ""),
                                     package_win)
        if workdir and not is_windows_path(workdir):
            workdir = package_win.rstrip("\\") + "\\" + workdir.replace("/", "\\").lstrip("\\")
        found = FoundApp(name=_msix_app_name(app, info), exe=exe, args=params, workdir=workdir,
                         icon_source=logo or exe, source=str(ctx.path), win_exe=unix_to_windows(exe, pfx_path) or "")
        extra: Dict[str, object] = {
            "source": "msix", "pfn": pfn, "aumid": f"{pfn}!{getattr(app, 'id', '')}",
            "package": str(ctx.path), "package_full_name": str(getattr(info, "package_full_name", "") or ""),
            "version": str(getattr(info, "version", "") or ""),
        }
        if _register_found(found, prefix_slug=slug, runner=runner, kind="app", extra=extra,
                           terminal=bool(getattr(app, "console", False))):
            created.append(found.name)
    _refresh_desktop_database()
    link_windows_apps(slug, title)
    for note in list(getattr(inst, "notes", []) or []):
        log.warning("%s", note)
    if created:
        names = ", ".join(created[:4]) + (" \u2026" if len(created) > 4 else "")
        fb.notify("App installed", f"{names} - now in the Start Menu (Wine / Windows apps)")
        print(f"Installed: {names}\nStart Menu entries were created; also listed in Lindos Settings > Windows apps.")
    else:
        print(f"{title} was unpacked into {install_dir}, but it has no app to add to the Start Menu.")
    return EXIT_OK


def _handle_appinstaller(ctx: _Ctx) -> int:
    """``.appinstaller``: show publisher + host, explicit consent, HTTPS download to quarantine, then MSIX."""
    name = ctx.path.name
    msix = _mod("msix")
    if msix is None:
        return _fail(ctx, "Windows app package support is missing from this installation (reinstall lindos-compat).")
    try:
        d = {str(k): str(v) for k, v in dict(msix.parse_appinstaller(ctx.path)).items()}
    except Exception as exc:  # noqa: BLE001 - MsixError or malformed XML
        return _fail(ctx, f"'{name}' is not a readable App Installer file: {exc}")
    uri = d.get("uri", "")
    split = urllib.parse.urlsplit(uri)
    host = split.hostname or d.get("host", "")
    if split.scheme.lower() != "https" or not split.hostname or split.username or split.password:
        return _explain(ctx, (
            f"'{name}' points to '{uri[:200]}'. Lindos only downloads app packages over a secure HTTPS connection "
            "from a named web server (not http://, network shares or local paths), so it will not use this file. "
            "Download the .msix / .msixbundle from the publisher's website instead."))
    question = (
        f"'{name}' is an App Installer file. It does not contain the app itself - it asks to download\n\n"
        f"    {d.get('name', '?')} {d.get('version', '')}\n"
        f"    Publisher: {d.get('publisher', '?')}\n"
        f"    From: {host}\n\n"
        "from the internet and install it. Microsoft turned off one-click installs from these files in 2023 "
        f"because criminals used them to spread malware. Only continue if you trust {host} and expected this "
        "file.\n\nDownload and install?")
    if ctx.ns.dry_run:
        return _host_dry_run(ctx, [], appinstaller=d, download=uri, confirm=question)
    ans = _confirm(ctx, question, ok_label="Download and install", cancel_label="Cancel")
    if ans is None:
        return _needs_yes(ctx, f"Downloading the app named in '{name}'")
    if not ans:
        return _cancelled(ctx)
    qdir = _quarantine_dir()
    try:
        ctx.fb.progress(f"Downloading {d.get('name', 'the app')} from {host}\u2026")

        def progress(done: int, total: Optional[int]) -> None:
            if total:
                ctx.fb.progress(f"Downloading from {host}\u2026 {int(100 * done / total)}%")

        try:
            pkg = download_https(uri, qdir, on_progress=progress)
        except DownloadError as exc:
            return _fail(ctx, f"The download from {host} failed: {exc}. Nothing was installed.")
        ctx.fb.close_progress()
        return _handle_msix(ctx, package=pkg, confirmed=True, expect=d)
    finally:
        shutil.rmtree(qdir, ignore_errors=True)


_HANDLERS: Dict[str, Callable[[_Ctx], int]] = {h: _handle_wine for h in WINE_HANDLERS}
_HANDLERS.update({
    "dos": _handle_dos,
    "pwsh": _handle_pwsh,
    "open-url": _handle_url,
    "extract": _handle_extract,
    "mount": _handle_mount,
    "msix": _handle_msix,
    "appinstaller": _handle_appinstaller,
})


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
