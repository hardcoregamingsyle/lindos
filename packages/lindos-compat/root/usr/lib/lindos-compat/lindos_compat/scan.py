"""Post-install discovery: what did the Windows installer put on the C:\\ drive?

Flow (SPEC §9 step 3): take a snapshot of the interesting places *before* the
installer runs, run it, snapshot again, and turn every new Start Menu shortcut
(``.lnk``), Wine-generated ``.desktop`` or new program ``.exe`` into a Lindos
Start Menu entry (``~/.local/share/applications/lindos-<slug>.desktop``) plus an
entry in the apps database (``APPS_DB``).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import CoreMissing, core, get_logger, user_home
from .icons import GENERIC_ICON
from .lnk import LnkError, is_windows_path, parse_lnk, unix_to_windows, windows_to_unix
from .prefix import safe_slug

__all__ = [
    "FoundApp",
    "SKIP_NAME_RE",
    "scan_roots",
    "snapshot",
    "diff_snapshots",
    "discover_apps",
    "applications_dir",
    "desktop_path_for",
    "desktop_quote",
    "render_desktop_file",
    "write_desktop_file",
    "register_app",
    "unregister_app",
    "unique_app_slug",
    "parse_wine_desktop_exec",
]

log = get_logger("lindos-compat.scan")

INTERESTING_SUFFIXES = (".lnk", ".exe", ".desktop")

#: shortcut / program names that are not "the app"
SKIP_NAME_RE = re.compile(
    r"(uninstall|unins\d*|remove|readme|read me|help|licen[cs]e|manual|documentation|website|homepage|"
    r"changelog|release notes|report a (bug|problem)|updater?|autoupdate|repair|setup|install|crash|dxsetup|"
    r"vcredist|vc_redist|dotnet|redist|activat|register|regsvr|elevat|helper|service|daemon|tray|"
    r"diagnos|debug|command prompt|powershell)",
    re.IGNORECASE,
)
SKIP_DIR_RE = re.compile(r"(uninstall|redist|vcredist|directx|dotnet|_CommonRedist|installer|setup|temp|cache|update)",
                         re.IGNORECASE)


@dataclass
class FoundApp:
    name: str
    exe: Path
    args: str = ""
    workdir: str = ""
    icon_source: Optional[Path] = None
    source: str = ""  # the .lnk/.desktop/.exe we learnt it from
    win_exe: str = ""

    def as_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["exe"] = str(self.exe)
        d["icon_source"] = str(self.icon_source) if self.icon_source else None
        return d


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def scan_roots(prefix: Path, *, home: Optional[Path] = None, deep: bool = True) -> List[Path]:
    """Directories to watch.  ``deep`` adds Program Files (larger walk)."""
    home = home or user_home()
    drive_c = prefix / "drive_c"
    roots: List[Path] = []
    if deep:
        for name in ("Program Files", "Program Files (x86)"):
            roots.append(drive_c / name)
        try:
            for entry in drive_c.iterdir():
                if entry.is_dir() and entry.name.lower().startswith("program files") and entry not in roots:
                    roots.append(entry)
        except OSError:
            pass
    users = drive_c / "users"
    try:
        user_dirs = [d for d in users.iterdir() if d.is_dir()] if users.is_dir() else []
    except OSError:
        user_dirs = []
    for u in user_dirs:
        roots.append(u / "AppData/Roaming/Microsoft/Windows/Start Menu")
        roots.append(u / "Start Menu")
        roots.append(u / "Desktop")
    roots.append(drive_c / "ProgramData/Microsoft/Windows/Start Menu")
    roots.append(home / ".local/share/applications/wine/Programs")
    roots.append(home / ".local/share/applications/wine")
    return roots


def snapshot(prefix: Path, *, home: Optional[Path] = None, deep: bool = True, max_depth: int = 6,
             max_files: int = 60000) -> Dict[str, float]:
    """Map ``path -> mtime`` for every .lnk/.exe/.desktop under the scan roots."""
    result: Dict[str, float] = {}
    count = 0
    for root in scan_roots(prefix, home=home, deep=deep):
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if not fn.lower().endswith(INTERESTING_SUFFIXES):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    result[full] = os.lstat(full).st_mtime
                except OSError:
                    continue
                count += 1
                if count >= max_files:
                    return result
    return result


def diff_snapshots(before: Dict[str, float], after: Dict[str, float]) -> List[Path]:
    """Files that are new (or rewritten) since ``before``."""
    out: List[Path] = []
    for path, mtime in after.items():
        prev = before.get(path)
        if prev is None or mtime > prev + 0.5:
            out.append(Path(path))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def parse_wine_desktop_exec(text: str) -> Optional[str]:
    """Pull the Windows path (or unix path) of the program out of a Wine-generated Exec= line."""
    if not text:
        return None
    # desktop-file level unescape (\\ -> \), then shell-ish unescape (\X -> X)
    level1 = text.replace("\\\\", "\\")
    out: List[str] = []
    i = 0
    while i < len(level1):
        ch = level1[i]
        if ch == "\\" and i + 1 < len(level1):
            out.append(level1[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    line = "".join(out)
    m = re.search(r"([A-Za-z]:\\[^\"']*?\.exe)", line, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(/[^\"']*?\.exe)", line, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _read_desktop_fields(path: Path) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            in_entry = False
            for raw in fh:
                line = raw.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                k, v = line.split("=", 1)
                fields.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return fields


def _looks_skippable(name: str) -> bool:
    return bool(SKIP_NAME_RE.search(name or ""))


def _exe_ok(exe: Path, prefix: Optional[Path] = None) -> bool:
    """Is this executable worth a Start Menu entry?  Only the path *inside* the C:\\ drive is judged."""
    if exe.suffix.lower() != ".exe":
        return False
    if _looks_skippable(exe.stem):
        return False
    parts = exe.parts[:-1]
    if prefix is not None:
        try:
            parts = exe.relative_to(prefix).parts[:-1]
        except ValueError:
            pass
    for part in parts:
        if SKIP_DIR_RE.fullmatch(part):
            return False
    return True


def discover_apps(new_files: Sequence[Path], prefix: Path, *, product_hint: str = "",
                  max_apps: int = 5) -> List[FoundApp]:
    """Turn new .lnk/.desktop/.exe files into :class:`FoundApp` candidates (deduplicated)."""
    found: Dict[str, FoundApp] = {}
    hint = (product_hint or "").lower()

    def add(app: FoundApp) -> None:
        key = str(app.exe).lower()
        if key in found:
            # prefer entries with a nicer name (from a shortcut) over bare exe names
            if not found[key].source.lower().endswith(".lnk") and app.source.lower().endswith(".lnk"):
                found[key] = app
            return
        found[key] = app

    # 1. Start Menu / Desktop shortcuts
    for f in new_files:
        if f.suffix.lower() != ".lnk":
            continue
        if _looks_skippable(f.stem):
            continue
        try:
            info = parse_lnk(f)
        except LnkError as exc:
            log.debug("unreadable shortcut %s: %s", f, exc)
            continue
        if not info.target:
            continue
        target: Optional[Path]
        if is_windows_path(info.target):
            target = windows_to_unix(info.target, prefix, must_exist=True)
        else:
            cand = f.parent / info.target.replace("\\", "/")
            target = cand if cand.exists() else None
        if not target or not _exe_ok(target, prefix):
            continue
        icon_src: Optional[Path] = None
        if info.icon_location:
            ico = windows_to_unix(info.icon_location.split(",")[0], prefix, must_exist=True)
            if ico and ico.suffix.lower() in (".ico", ".exe", ".dll"):
                icon_src = ico
        add(FoundApp(name=f.stem, exe=target, args=info.arguments, workdir=info.working_dir,
                     icon_source=icon_src or target, source=str(f), win_exe=info.target))

    # 2. Wine-generated .desktop files (winemenubuilder is disabled by default, but be thorough)
    for f in new_files:
        if f.suffix.lower() != ".desktop":
            continue
        fields = _read_desktop_fields(f)
        win = parse_wine_desktop_exec(fields.get("Exec", ""))
        if not win:
            continue
        target = windows_to_unix(win, prefix, must_exist=True) if is_windows_path(win) else Path(win)
        if not target or not target.exists() or not _exe_ok(target, prefix):
            continue
        name = fields.get("Name") or target.stem
        if _looks_skippable(name):
            continue
        add(FoundApp(name=name, exe=target, source=str(f), icon_source=target,
                     win_exe=win if is_windows_path(win) else (unix_to_windows(target, prefix) or "")))

    if found:
        apps = list(found.values())
    else:
        # 3. bare executables under Program Files
        candidates: List[Path] = [f for f in new_files if f.suffix.lower() == ".exe" and _exe_ok(f, prefix)]
        # keep the ones that look like the product, then the biggest few
        def score(p: Path) -> tuple:
            stem = p.stem.lower()
            parent = p.parent.name.lower()
            s = 0
            if hint and (hint in stem or stem in hint):
                s += 4
            if parent and (parent in stem or stem in parent):
                s += 2
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            return (s, size)

        candidates.sort(key=score, reverse=True)
        apps = [FoundApp(name=_pretty_name(p.stem), exe=p, source=str(p), icon_source=p,
                         win_exe=unix_to_windows(p, prefix) or "") for p in candidates[:max_apps]]
    return apps[:max_apps]


def _pretty_name(stem: str) -> str:
    text = re.sub(r"[_\-]+", " ", stem).strip()
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)  # CamelCase -> Camel Case
    return text or stem


# ---------------------------------------------------------------------------
# .desktop generation
# ---------------------------------------------------------------------------


def applications_dir(home: Optional[Path] = None) -> Path:
    return (home or user_home()) / ".local/share/applications"


def desktop_path_for(app_slug: str, home: Optional[Path] = None) -> Path:
    return applications_dir(home) / f"lindos-{app_slug}.desktop"


def desktop_quote(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec (file-level + exec-level escaping)."""
    s = str(arg)
    s = s.replace("\\", "\\\\\\\\").replace('"', '\\\\"').replace("$", "\\\\$").replace("`", "\\\\`")
    s = s.replace("%", "%%")
    return f'"{s}"'


def render_desktop_file(*, name: str, exe: Path, prefix_slug: str, runner: str = "wine",
                        icon: str = GENERIC_ICON, comment: str = "", workdir: str = "",
                        app_slug: str = "") -> str:
    exec_line = f"lindos-run --prefix {prefix_slug} {desktop_quote(str(exe))}"
    lines = [
        "[Desktop Entry]",
        "Version=1.0",
        "Type=Application",
        f"Name={name}",
        f"Comment={comment or 'Windows program (runs through the Lindos compatibility layer)'}",
        f"Exec={exec_line}",
        f"Icon={icon}",
        "Terminal=false",
        "Categories=Wine;X-Lindos;",
        f"StartupWMClass={exe.name}",
        "StartupNotify=true",
        "Keywords=Windows;Wine;Lindos;",
    ]
    wd = workdir or str(exe.parent)
    if wd and "\n" not in wd:
        lines.append(f"Path={wd}")
    lines.append(f"X-Lindos-Prefix={prefix_slug}")
    lines.append(f"X-Lindos-Runner={runner}")
    if app_slug:
        lines.append(f"X-Lindos-App={app_slug}")
    return "\n".join(lines) + "\n"


def write_desktop_file(app_slug: str, *, name: str, exe: Path, prefix_slug: str, runner: str = "wine",
                       icon: str = GENERIC_ICON, home: Optional[Path] = None, workdir: str = "") -> Path:
    """Write ``~/.local/share/applications/lindos-<app_slug>.desktop`` and return its path."""
    path = desktop_path_for(app_slug, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_desktop_file(name=name, exe=exe, prefix_slug=prefix_slug, runner=runner, icon=icon,
                                  workdir=workdir, app_slug=app_slug)
    tmp = path.with_suffix(".desktop.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# APPS_DB
# ---------------------------------------------------------------------------


def unique_app_slug(name: str, exe: Path, db: Dict[str, object]) -> str:
    base = safe_slug(name) or "windows-app"
    slug = base
    n = 2
    while slug in db:
        rec = db.get(slug)
        if isinstance(rec, dict) and str(rec.get("exe", "")).lower() == str(exe).lower():
            return slug  # same program: reuse
        slug = f"{base}-{n}"
        n += 1
    return slug


def register_app(app: FoundApp, *, prefix_slug: str, runner: str, kind: str = "app", icon: str = GENERIC_ICON,
                 icon_file: Optional[Path] = None, desktop_file: Optional[Path] = None,
                 db: Optional[Dict[str, object]] = None, save: bool = True) -> str:
    """Add ``app`` to APPS_DB (``{slug:{name,exe,prefix,runner,installed_at,kind,...}}``)."""
    c = core()
    if db is None:
        loaded = c.apps_db_load()
        db = loaded if isinstance(loaded, dict) else {}
    slug = unique_app_slug(app.name, app.exe, db)
    db[slug] = {
        "name": app.name,
        "exe": str(app.exe),
        "prefix": prefix_slug,
        "runner": runner,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "args": app.args,
        "workdir": app.workdir,
        "win_exe": app.win_exe,
        "source": app.source,
        "icon": icon,
        "icon_file": str(icon_file) if icon_file else "",
        "desktop": str(desktop_file) if desktop_file else "",
    }
    if save:
        c.apps_db_save(db)
    return slug


def unregister_app(app_slug: str, *, home: Optional[Path] = None) -> bool:
    """Remove an app from APPS_DB and delete its .desktop/icon files."""
    try:
        c = core()
        db = c.apps_db_load()
    except CoreMissing:
        return False
    if not isinstance(db, dict) or app_slug not in db:
        return False
    rec = db.pop(app_slug)
    if isinstance(rec, dict):
        for key in ("desktop", "icon_file"):
            f = rec.get(key)
            if isinstance(f, str) and f:
                try:
                    Path(f).unlink()
                except OSError:
                    pass
    dp = desktop_path_for(app_slug, home)
    try:
        dp.unlink()
    except OSError:
        pass
    c.apps_db_save(db)
    return True


def find_registered(exe: Path) -> Optional[Dict[str, object]]:
    """APPS_DB record whose ``exe`` equals ``exe`` (used to recover args/prefix at launch)."""
    try:
        db = core().apps_db_load()
    except CoreMissing:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(db, dict):
        return None
    target = str(exe).lower()
    for slug, rec in db.items():
        if isinstance(rec, dict) and str(rec.get("exe", "")).lower() == target:
            out = dict(rec)
            out["slug"] = slug
            return out
    return None


def iter_registered() -> Iterable[Dict[str, object]]:
    try:
        db = core().apps_db_load()
    except CoreMissing:
        return []
    if not isinstance(db, dict):
        return []
    items = []
    for slug, rec in db.items():
        if isinstance(rec, dict):
            out = dict(rec)
            out["slug"] = slug
            items.append(out)
    return items
