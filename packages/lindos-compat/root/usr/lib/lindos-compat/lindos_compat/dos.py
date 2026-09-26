"""DOS programs in DOSBox, and the Wine WoW64-mode probe for 16-bit Windows programs.

SPEC-WINDOWS §28.5.

DOSBox
------
Lookup order (:func:`find_dosbox`): ``dosbox-x`` (apt, Ubuntu 24.04 universe) → the DOSBox-X
Flatpak ``com.dosbox_x.DOSBox-X`` → ``dosbox`` (apt 0.74; if ``dosbox --version`` says
"staging" it is DOSBox Staging) → the DOSBox Staging Flatpak ``io.github.dosbox-staging``.
Each flavour gets its own argv (:func:`dosbox_argv`):

* DOSBox-X  ``-fastlaunch -nopromptfolder -exit <abs>`` (quits when the program ends)
* Staging   ``<abs>`` (mounts the folder as C:, quits on exit by itself)
* 0.74      ``<abs> -exit``

Program arguments need the ``-c`` form (mount the folder as C:, run ``NAME args``, exit).
Flatpak builds only see ``$HOME``, so a program elsewhere (``/media``, a mounted ISO) is shared
read-only with ``--filesystem=<dir>:ro``.  Lindos never relies on Wine's own
winevdm → ``dosbox`` fallback.

16-bit Windows (Win16) and Wine's WoW64 mode
--------------------------------------------
:func:`wine_wow64_mode` answers once per Wine build (cached in ``~/.cache/lindos``):

* ``old-wow64``          — Ubuntu's wine 9.0 + wine32:i386, WineHQ i386+amd64: Win16 runs in a
  dedicated ``win16`` C:\\ drive with ``WINEARCH=win32``;
* ``new-wow64-16bit``    — new WoW64, Wine ≥ 10.16: a normal 64-bit C:\\ drive runs Win16;
* ``new-wow64-no16bit``  — new WoW64 before 10.16: 16-bit programs cannot run (explained);
* ``unknown``            — no Wine, or the probe was inconclusive.

The probe runs ``WINEARCH=win32 wineboot -i`` on a throw-away prefix; new-WoW64 Wine refuses
with *"WINEARCH is set to 'win32' but this is not supported in wow64 mode."*
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import expand_user_path, get_logger, user_home
from .prefix import find_wine, wine_tool

__all__ = [
    "DOSBOX_INSTALL_HINT",
    "DOSBOX_X_FLATPAK",
    "STAGING_FLATPAK",
    "FLAVORS",
    "WOW64_MODES",
    "WOW64_WIN32_ERROR",
    "NEW_WOW64_16BIT_SINCE",
    "find_dosbox",
    "dosbox_argv",
    "flatpak_app_installed",
    "is_83_name",
    "parse_wine_version",
    "wine_wow64_mode",
    "clear_wow64_memo",
    "wow64_cache_dir",
]

log = get_logger("lindos-compat.dos")

#: What to tell the user when no DOSBox is installed (Ubuntu 24.04 universe).
DOSBOX_INSTALL_HINT = "sudo apt install dosbox-x"
DOSBOX_X_FLATPAK = "com.dosbox_x.DOSBox-X"
STAGING_FLATPAK = "io.github.dosbox-staging"
FLAVORS = ("dosbox-x", "staging", "dosbox")

WOW64_MODES = ("old-wow64", "new-wow64-16bit", "new-wow64-no16bit", "unknown")
#: Fatal error printed by new-WoW64 Wine for WINEARCH=win32 (dlls/ntdll/unix/server.c).
WOW64_WIN32_ERROR = "WINEARCH is set to 'win32' but this is not supported in wow64 mode"
#: First Wine (development) release with 16-bit support in new WoW64 mode.
NEW_WOW64_16BIT_SINCE = (10, 16)
_CACHE_NAME = "wine-wow64.json"
_PROBE_TIMEOUT = 180

Which = Callable[[str], Optional[str]]
Run = Callable[..., "subprocess.CompletedProcess[str]"]

_MEMO: Dict[Tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _system_path(path: str) -> Path:
    """A system path with ``LINDOS_ROOT`` applied (so tests never see the host's /var)."""
    root = os.environ.get("LINDOS_ROOT")
    if root:
        return Path(root) / path.lstrip("/")
    return Path(path)


def flatpak_app_installed(app_id: str, *, which: Which = shutil.which, run: Run = subprocess.run,
                          home: Optional[Path] = None) -> bool:
    """True when the Flatpak ``app_id`` is installed (user or system), like runner.is_bottles_installed."""
    flatpak = which("flatpak")
    if not flatpak:
        return False
    home = home or user_home()
    for exports in (home / ".local/share/flatpak/exports/bin" / app_id,
                    _system_path("/var/lib/flatpak/exports/bin") / app_id):
        try:
            if exports.exists():
                return True
        except OSError:
            continue
    try:
        proc = run([flatpak, "info", app_id], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(proc, "returncode", 1) == 0


def _dosbox_is_staging(dosbox: str, run: Run) -> bool:
    """``dosbox`` is both 0.74's and DOSBox Staging's binary name: ask it."""
    try:
        proc = run([dosbox, "--version"], capture_output=True, text=True, timeout=15, check=False,
                   errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    out = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}".lower()
    return "staging" in out


# ---------------------------------------------------------------------------
# DOSBox
# ---------------------------------------------------------------------------


def find_dosbox(which: Which = shutil.which, run: Run = subprocess.run, *,
                home: Optional[Path] = None) -> Optional[Tuple[List[str], str]]:
    """``(argv prefix, flavor)`` of the best installed DOSBox, or None.

    ``flavor`` is one of :data:`FLAVORS`.  Flatpak prefixes are ``[flatpak, "run", <app id>]``.
    """
    native = which("dosbox-x")
    if native:
        return [native], "dosbox-x"
    if flatpak_app_installed(DOSBOX_X_FLATPAK, which=which, run=run, home=home):
        return [str(which("flatpak")), "run", DOSBOX_X_FLATPAK], "dosbox-x"
    dosbox = which("dosbox")
    if dosbox:
        return [dosbox], ("staging" if _dosbox_is_staging(dosbox, run) else "dosbox")
    if flatpak_app_installed(STAGING_FLATPAK, which=which, run=run, home=home):
        return [str(which("flatpak")), "run", STAGING_FLATPAK], "staging"
    return None


_83_RE = re.compile(r"^[A-Z0-9_$~!#%&'(){}^@`\-]{1,8}(\.[A-Z0-9_$~!#%&'(){}^@`\-]{1,3})?$")


def is_83_name(name: str) -> bool:
    """True when ``name`` is a valid DOS 8.3 file name (case-insensitive)."""
    return bool(_83_RE.match((name or "").upper()))


def _dos_quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or "\t" in arg) else arg


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def dosbox_argv(path: Path, args: Sequence[str] = (), *, which: Which = shutil.which, run: Run = subprocess.run,
                home: Optional[Path] = None) -> List[str]:
    """Full argv that runs the DOS program ``path`` (with ``args``) in the best DOSBox.

    Raises FileNotFoundError (message: :data:`DOSBOX_INSTALL_HINT`) when no DOSBox is installed.
    """
    found = find_dosbox(which, run, home=home)
    if found is None:
        raise FileNotFoundError(DOSBOX_INSTALL_HINT)
    prefix, flavor = found
    abspath = os.path.abspath(os.fspath(path))
    folder = os.path.dirname(abspath)
    name = os.path.basename(abspath)
    argv = list(prefix)
    if len(argv) >= 3 and os.path.basename(argv[0]) == "flatpak":
        home_dir = Path(os.path.abspath(os.fspath(home or user_home())))
        if not _under(Path(folder), home_dir):
            argv = argv[:-1] + [f"--filesystem={folder}:ro"] + argv[-1:]
    prog_args = [str(a) for a in args]

    use_c_form = bool(prog_args)
    if use_c_form and ('"' in folder or '"' in name):
        log.warning("DOS program arguments dropped: the folder name contains a quote character")
        use_c_form = False
    if use_c_form and flavor == "dosbox" and not is_83_name(name):
        # DOSBox 0.74 has no long file names: the program is only reachable by a guessed 8.3 alias
        log.warning("DOSBox 0.74 cannot pass arguments to '%s' (not an 8.3 name); starting it without them", name)
        use_c_form = False

    if use_c_form:
        command = " ".join([_dos_quote(name)] + [_dos_quote(a) for a in prog_args])
        if flavor == "dosbox-x":
            argv += ["-fastlaunch", "-nopromptfolder"]
        argv += ["-c", f'mount c "{folder}"', "-c", "c:", "-c", command, "-c", "exit"]
        return argv
    if flavor == "dosbox-x":
        return argv + ["-fastlaunch", "-nopromptfolder", "-exit", abspath]
    if flavor == "staging":
        return argv + [abspath]
    return argv + [abspath, "-exit"]


# ---------------------------------------------------------------------------
# Wine WoW64 mode
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"wine-(\d+)\.(\d+)")


def parse_wine_version(text: str) -> Optional[Tuple[int, int]]:
    """``"wine-10.16 (Staging)"`` → ``(10, 16)``; ``"wine-9.0 (Ubuntu 9.0~repack-4build3)"`` → ``(9, 0)``."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def wow64_cache_dir() -> Path:
    """Default cache location (``~/.cache/lindos``, honouring LINDOS_HOME)."""
    return expand_user_path("~/.cache/lindos")


def clear_wow64_memo() -> None:
    """Forget the in-process answers (tests; after installing another Wine)."""
    _MEMO.clear()


def _wine_version_text(wine: str, run: Run) -> str:
    try:
        proc = run([wine, "--version"], capture_output=True, text=True, timeout=30, check=False, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or "").strip()
    return out.splitlines()[0].strip() if out else ""


def _read_cache(path: Path, wine: str, version: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("wine") != wine or data.get("version") != version:
        return None
    mode = data.get("mode")
    return mode if mode in WOW64_MODES and mode != "unknown" else None


def _write_cache(path: Path, wine: str, version: str, mode: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "wine": wine, "version": version, "mode": mode,
                       "checked": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("could not write %s: %s", path, exc)


def _mode_from_probe(returncode: int, output: str, version: str) -> str:
    if WOW64_WIN32_ERROR.lower() in output.lower():
        parsed = parse_wine_version(version)
        if parsed is not None and parsed < NEW_WOW64_16BIT_SINCE:
            return "new-wow64-no16bit"
        return "new-wow64-16bit"
    if returncode == 0:
        return "old-wow64"
    return "unknown"


def _probe(wine: str, version: str, which: Which, run: Run) -> str:
    workdir = tempfile.mkdtemp(prefix="lindos-wow64-probe-")
    pfx = os.path.join(workdir, "pfx")
    env = dict(os.environ)
    env.update({"WINEPREFIX": pfx, "WINEARCH": "win32", "WINEDEBUG": "-all",
                "WINEDLLOVERRIDES": "mscoree,mshtml=;winemenubuilder.exe=d"})
    wineboot = wine_tool("wineboot", which)
    argv = [wineboot, "-i"] if wineboot else [wine, "wineboot", "-i"]
    log.info("Checking how this Wine runs 16-bit programs (one-time check)...")
    try:
        try:
            proc = run(argv, env=env, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False,
                       errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("WoW64 probe failed to run: %s", exc)
            return "unknown"
        output = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
        rc = getattr(proc, "returncode", 1)
        return _mode_from_probe(1 if rc is None else int(rc), output, version)
    finally:
        wineserver = wine_tool("wineserver", which)
        if wineserver:
            try:
                run([wineserver, "-k"], env=env, capture_output=True, text=True, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def wine_wow64_mode(*, which: Which = shutil.which, run: Run = subprocess.run,
                    cache_dir: Optional[Path] = None) -> str:
    """How the installed Wine runs 16-bit programs: one of :data:`WOW64_MODES` (cached)."""
    wine = find_wine(which)
    if not wine:
        return "unknown"
    version = _wine_version_text(wine, run)
    key = (wine, version)
    if key in _MEMO:
        return _MEMO[key]
    cache_file = (cache_dir if cache_dir is not None else wow64_cache_dir()) / _CACHE_NAME
    cached = _read_cache(cache_file, wine, version)
    if cached:
        _MEMO[key] = cached
        return cached
    mode = _probe(wine, version, which, run)
    _MEMO[key] = mode
    if mode != "unknown":
        _write_cache(cache_file, wine, version, mode)
    return mode
