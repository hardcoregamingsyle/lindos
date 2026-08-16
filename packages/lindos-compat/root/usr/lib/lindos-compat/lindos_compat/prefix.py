"""Wine prefix management for Lindos.

Every Windows program gets its own prefix (its own "C:\\ drive") under
``PREFIXES_DIR`` (``~/.local/share/lindos/prefixes/<slug>``, SPEC §4.1).  Small
utilities may share the ``default`` prefix (``lindos-run --shared``).

A tiny marker file ``.lindos.json`` inside each prefix remembers which runner
(wine / umu / bottles) and architecture created it, so later launches stay
consistent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import CoreMissing, core, expand_user_path, get_logger, path_const, user_home

__all__ = [
    "MARKER_NAME",
    "SHARED_SLUG",
    "PrefixState",
    "prefixes_dir",
    "prefix_path",
    "safe_slug",
    "derive_slug",
    "read_marker",
    "write_marker",
    "find_wine",
    "wine_tool",
    "wine_version",
    "base_wine_env",
    "ensure_prefix",
    "backup_prefix",
    "list_prefixes",
    "remove_prefix",
    "open_prefix",
    "run_winecfg",
    "find_prefix_for_path",
    "link_windows_apps",
    "windows_apps_dir",
    "dir_size_bytes",
]

log = get_logger("lindos-compat.prefix")

MARKER_NAME = ".lindos.json"
SHARED_SLUG = "default"

WINE_CANDIDATES = (
    "/opt/wine-staging/bin/wine",
    "/opt/wine-devel/bin/wine",
    "/opt/wine-stable/bin/wine",
    "/usr/bin/wine",
)

# tokens that carry no identity ("FooSetup-x64-v1.2.3.exe" -> "foo")
_NOISE_TOKENS = {
    "setup", "install", "installer", "installation", "x64", "x86", "win64", "win32", "amd64",
    "64bit", "32bit", "64-bit", "32-bit", "online", "offline", "web", "webinstaller",
    "portable", "final", "full", "release", "stable", "latest", "en", "eng", "en-us", "exe",
    "msi", "windows", "win", "pc", "desktop", "app", "application", "program", "update",
    "installer64", "installer32", "bootstrapper", "downloader",
}
_VERSION_RE = re.compile(r"^v?\d+([._-]\d+)*[a-z]?$", re.IGNORECASE)
# "FooSetup" / "FooInstaller" -> "Foo" (only when something meaningful is left)
_GLUED_NOISE_RE = re.compile(r"^(?P<stem>.{3,}?)(setup|installer|install)$", re.IGNORECASE)


@dataclass
class PrefixState:
    slug: str
    path: Path
    created: bool = False
    initialized: bool = False
    runner: str = "wine"
    arch: str = "win64"
    marker: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


# ---------------------------------------------------------------------------
# Locations & names
# ---------------------------------------------------------------------------


def prefixes_dir() -> Path:
    """``PREFIXES_DIR`` expanded (honours LINDOS_HOME)."""
    return expand_user_path(path_const("PREFIXES_DIR"))


def safe_slug(name: str) -> str:
    """Slugify ``name`` with the shared ``lindos.compat.slugify`` (fallback: simple rule)."""
    text = (name or "").strip()
    try:
        slug = str(core().slugify(text))
    except CoreMissing:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug.strip("-./\\")
    if not slug or slug in (".", ".."):
        return "app"
    return slug[:64]


def prefix_path(slug: str) -> Path:
    """Directory of the prefix ``slug`` (not created)."""
    return prefixes_dir() / safe_slug(slug)


def _clean_product_name(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", text or "")  # drop "(64-bit)" etc.
    text = re.sub(r"[_\-.]+", " ", text)
    words = []
    for w in text.split():
        lw = w.lower()
        if lw in _NOISE_TOKENS or _VERSION_RE.match(lw):
            continue
        glued = _GLUED_NOISE_RE.match(w)
        if glued:
            w = glued.group("stem")
        words.append(w)
    return " ".join(words).strip()


def derive_slug(info: object) -> str:
    """Pick the per-app prefix slug from an ``ExeInfo`` (product name, else file stem)."""
    product = str(getattr(info, "product", "") or "")
    name = str(getattr(info, "name", "") or "")
    path = str(getattr(info, "path", "") or "")
    stem = Path(name or path).stem if (name or path) else ""
    for candidate in (product, stem, name):
        cleaned = _clean_product_name(candidate)
        if cleaned:
            slug = safe_slug(cleaned)
            if slug and slug != "app":
                return slug
    if stem:
        return safe_slug(stem)
    return "app"


# ---------------------------------------------------------------------------
# Marker file
# ---------------------------------------------------------------------------


def read_marker(prefix: Path) -> Dict[str, object]:
    try:
        with open(prefix / MARKER_NAME, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(prefix: Path, data: Dict[str, object]) -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    tmp = prefix / (MARKER_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, prefix / MARKER_NAME)


# ---------------------------------------------------------------------------
# Wine binaries
# ---------------------------------------------------------------------------


def find_wine(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Path of the ``wine`` binary (PATH first, then WineHQ's /opt locations)."""
    found = which("wine")
    if found:
        return found
    for cand in WINE_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def wine_tool(name: str, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Path of a wine companion tool (``wineboot``, ``winecfg``, ``wineserver``...)."""
    found = which(name)
    if found:
        return found
    wine = find_wine(which)
    if wine:
        cand = os.path.join(os.path.dirname(wine), name)
        if os.path.isfile(cand):
            return cand
    return None


def wine_version(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
                 which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    wine = find_wine(which)
    if not wine:
        return None
    try:
        proc = run([wine, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0] if out else None


def base_wine_env(prefix: Path, *, arch: str = "win64", dll_overrides: Optional[Dict[str, str]] = None,
                  headless: bool = False) -> Dict[str, str]:
    """Environment for plain-Wine runs (SPEC §9 step 2)."""
    overrides = {"winemenubuilder.exe": "d"}
    if headless:
        # no display -> nobody can answer the Mono/Gecko install dialogs; skip them
        overrides.update({"mscoree": "d", "mshtml": "d"})
    if dll_overrides:
        overrides.update({k: v for k, v in dll_overrides.items() if k and v})
    env = {
        "WINEPREFIX": str(prefix),
        "WINEARCH": "win32" if arch == "win32" else "win64",
        "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": ";".join(f"{k}={v}" for k, v in overrides.items()),
    }
    return env


# ---------------------------------------------------------------------------
# Create / initialise
# ---------------------------------------------------------------------------


def backup_prefix(prefix: Path) -> Optional[Path]:
    """Move an existing prefix aside (``<slug>-old-<timestamp>``) instead of deleting it."""
    if not prefix.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = prefix.with_name(f"{prefix.name}-old-{stamp}")
    os.rename(prefix, target)
    log.info("Existing C:\\ drive moved aside to %s", target)
    return target


def ensure_prefix(
    slug: str,
    *,
    runner: str = "wine",
    arch: str = "win64",
    fresh: bool = False,
    dll_overrides: Optional[Dict[str, str]] = None,
    headless: bool = False,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    on_progress: Optional[Callable[[str], None]] = None,
    log_file: Optional[Path] = None,
    recipe_id: Optional[str] = None,
) -> PrefixState:
    """Make sure the prefix exists and (for the wine runner) is initialised.

    * ``wine``: runs ``wineboot -u`` when the prefix is new (creates C:\\ drive).
    * ``umu``: only creates the directory -- umu-run/Proton initialise it on first launch.
    * ``bottles``: nothing to do here (Bottles keeps its own bottles).
    """
    slug = safe_slug(slug)
    prefix = prefix_path(slug)
    state = PrefixState(slug=slug, path=prefix, runner=runner, arch=arch)

    if fresh and prefix.exists():
        backup_prefix(prefix)

    marker = read_marker(prefix)
    if marker:
        state.marker = marker
        state.arch = str(marker.get("arch") or arch)
        # a prefix created by one runner keeps it, unless the caller insists
        state.runner = str(marker.get("runner") or runner)

    existed = (prefix / "system.reg").exists() or (prefix / "drive_c").is_dir()
    if not existed:
        prefix.mkdir(parents=True, exist_ok=True)
        state.created = True

    if runner == "wine" and not (prefix / "system.reg").exists():
        wineboot = wine_tool("wineboot", which)
        wine = find_wine(which)
        if not wineboot and not wine:
            state.warnings.append("Wine is not installed - cannot create the C:\\ drive")
            return state
        if on_progress:
            on_progress("Preparing Windows compatibility (creating C:\\ drive)...")
        env = dict(os.environ)
        env.update(base_wine_env(prefix, arch=state.arch, dll_overrides=dll_overrides, headless=headless))
        argv = [wineboot, "-u"] if wineboot else [str(wine), "wineboot", "-u"]
        log.info("Initialising Windows compatibility prefix %s (%s)", prefix, state.arch)
        out_fh = None
        try:
            if log_file:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                out_fh = open(log_file, "a", encoding="utf-8", errors="replace")
                out_fh.write(f"\n== wineboot -u ({time.strftime('%Y-%m-%d %H:%M:%S')}) ==\n")
                out_fh.flush()
            proc = run(argv, env=env, stdout=out_fh or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                       timeout=900, check=False)
            if proc.returncode != 0:
                state.warnings.append(f"wineboot exited with code {proc.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            state.warnings.append(f"wineboot failed: {exc}")
        finally:
            if out_fh:
                out_fh.close()
        # wineboot returns before wineserver finishes writing the registry; wait a bit
        wineserver = wine_tool("wineserver", which)
        if wineserver:
            try:
                run([wineserver, "-w"], env=env, timeout=300, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        state.initialized = (prefix / "system.reg").exists()
    else:
        state.initialized = (prefix / "system.reg").exists()

    if state.created or not marker:
        marker = {
            "slug": slug,
            "runner": runner,
            "arch": state.arch,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lindos": "1.0.0",
        }
        if recipe_id:
            marker["recipe"] = recipe_id
        try:
            write_marker(prefix, marker)
        except OSError as exc:
            state.warnings.append(f"cannot write {MARKER_NAME}: {exc}")
        state.marker = marker
    elif recipe_id and marker.get("recipe") != recipe_id:
        marker["recipe"] = recipe_id
        try:
            write_marker(prefix, marker)
        except OSError:
            pass
    return state


# ---------------------------------------------------------------------------
# Listing / removal / tools
# ---------------------------------------------------------------------------


def dir_size_bytes(path: Path, limit_files: int = 200000) -> int:
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            count += 1
            if count > limit_files:
                return total
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue
    return total


def _apps_db() -> Dict[str, Dict[str, object]]:
    try:
        db = core().apps_db_load()
        return db if isinstance(db, dict) else {}
    except CoreMissing:
        return {}
    except Exception:  # noqa: BLE001
        return {}


def list_prefixes(with_size: bool = False) -> List[Dict[str, object]]:
    """All prefixes with marker info and the apps registered in them."""
    root = prefixes_dir()
    out: List[Dict[str, object]] = []
    if not root.is_dir():
        return out
    db = _apps_db()
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        if not ((entry / "drive_c").is_dir() or (entry / MARKER_NAME).exists()):
            continue
        marker = read_marker(entry)
        apps = sorted(
            slug for slug, rec in db.items()
            if isinstance(rec, dict) and (rec.get("prefix") in (entry.name, str(entry)))
        )
        item: Dict[str, object] = {
            "slug": entry.name,
            "path": str(entry),
            "runner": marker.get("runner", "wine"),
            "arch": marker.get("arch", "win64"),
            "created_at": marker.get("created_at", ""),
            "recipe": marker.get("recipe", ""),
            "apps": apps,
            "initialized": (entry / "system.reg").exists(),
        }
        if with_size:
            item["size_mb"] = round(dir_size_bytes(entry) / (1024 * 1024), 1)
        out.append(item)
    return out


def _remove_registered_apps(prefix_slug: str, prefix: Path) -> List[str]:
    """Drop APPS_DB entries (and their .desktop/icon files) that live in ``prefix``."""
    removed: List[str] = []
    try:
        c = core()
        db = c.apps_db_load()
    except CoreMissing:
        return removed
    except Exception:  # noqa: BLE001
        return removed
    if not isinstance(db, dict):
        return removed
    for slug, rec in list(db.items()):
        if not isinstance(rec, dict):
            continue
        if rec.get("prefix") not in (prefix_slug, str(prefix)):
            continue
        for key in ("desktop", "icon_file"):
            f = rec.get(key)
            if isinstance(f, str) and f:
                try:
                    Path(f).unlink()
                except OSError:
                    pass
        db.pop(slug, None)
        removed.append(slug)
    if removed:
        try:
            c.apps_db_save(db)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not update apps database: %s", exc)
    return removed


def remove_prefix(slug: str, *, keep_apps: bool = False) -> bool:
    """Delete a prefix (the whole C:\\ drive) and its registered apps."""
    prefix = prefix_path(slug)
    if not prefix.exists():
        log.error("No Windows compatibility prefix named '%s' (%s)", slug, prefix)
        return False
    if not keep_apps:
        removed = _remove_registered_apps(safe_slug(slug), prefix)
        for r in removed:
            log.info("Removed Start Menu entry for '%s'", r)
    # remove ~/Windows Apps/<Name>/C: symlinks pointing into this prefix
    try:
        wa = windows_apps_dir()
        if wa.is_dir():
            for d in wa.iterdir():
                link = d / "C:"
                if link.is_symlink():
                    try:
                        if Path(os.readlink(link)).resolve() == (prefix / "drive_c").resolve():
                            link.unlink()
                            try:
                                d.rmdir()
                            except OSError:
                                pass
                    except OSError:
                        pass
    except OSError:
        pass
    shutil.rmtree(prefix, ignore_errors=False)
    log.info("Deleted C:\\ drive of '%s' (%s)", slug, prefix)
    return True


def open_prefix(slug_or_path: str, *, run: Callable[..., object] = subprocess.Popen,
                which: Callable[[str], Optional[str]] = shutil.which) -> Optional[Path]:
    """Open the prefix's C:\\ drive in the file manager (xdg-open / thunar)."""
    target = resolve_prefix_arg(slug_or_path)
    if target is None:
        return None
    drive_c = target / "drive_c"
    if not drive_c.is_dir():
        log.error("This Windows program has no C:\\ drive yet (%s)", drive_c)
        return None
    for tool in ("xdg-open", "thunar", "exo-open"):
        exe = which(tool)
        if exe:
            try:
                run([exe, str(drive_c)])
            except OSError as exc:
                log.error("cannot open %s: %s", drive_c, exc)
                return None
            return drive_c
    log.error("No file manager opener found (xdg-open); the C:\\ drive is at %s", drive_c)
    return drive_c


def resolve_prefix_arg(slug_or_path: str) -> Optional[Path]:
    """Accept a prefix slug OR any file path (an .exe inside a prefix, or a program to run)."""
    if not slug_or_path:
        return None
    p = Path(slug_or_path)
    if p.exists() and (os.sep in slug_or_path or "/" in slug_or_path or p.suffix):
        # a file: is it inside a prefix?
        found = find_prefix_for_path(p)
        if found:
            return prefix_path(found)
        # not inside: the prefix an installer/app *would* use
        try:
            info = core().analyze_exe(str(p))
            slug = derive_slug(info)
        except Exception:  # noqa: BLE001
            slug = safe_slug(p.stem)
        return prefix_path(slug)
    candidate = prefix_path(slug_or_path)
    if candidate.exists():
        return candidate
    log.error("No Windows compatibility prefix named '%s'", slug_or_path)
    return None


def run_winecfg(slug: str, *, which: Callable[[str], Optional[str]] = shutil.which,
                run: Callable[..., object] = subprocess.call) -> int:
    """Run ``winecfg`` inside the prefix."""
    prefix = prefix_path(slug)
    if not prefix.exists():
        log.error("No Windows compatibility prefix named '%s'", slug)
        return 1
    tool = wine_tool("winecfg", which)
    if not tool:
        log.error("winecfg not found - is Wine installed? (lindos-compat doctor)")
        return 1
    marker = read_marker(prefix)
    env = dict(os.environ)
    env.update(base_wine_env(prefix, arch=str(marker.get("arch") or "win64")))
    try:
        rc = run([tool], env=env)
    except OSError as exc:
        log.error("cannot start winecfg: %s", exc)
        return 1
    return int(rc or 0)


def find_prefix_for_path(path: str | os.PathLike[str]) -> Optional[str]:
    """If ``path`` lies inside ``PREFIXES_DIR/<slug>/``, return ``slug``."""
    try:
        resolved = Path(path).resolve()
        root = prefixes_dir().resolve()
        rel = resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return rel.parts[0] if rel.parts else None


# ---------------------------------------------------------------------------
# ~/Windows Apps/<Name>/C:  (Windows-friendly touch, SPEC §9)
# ---------------------------------------------------------------------------


def windows_apps_dir() -> Path:
    return user_home() / "Windows Apps"


def _safe_display_name(name: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", name or "").strip(" .")
    return text[:80] or "Windows App"


def link_windows_apps(slug: str, display_name: str) -> Optional[Path]:
    """Create ``~/Windows Apps/<Name>/C:`` → ``<prefix>/drive_c`` (lazily, best-effort)."""
    prefix = prefix_path(slug)
    drive_c = prefix / "drive_c"
    if not drive_c.is_dir():
        return None
    folder = windows_apps_dir() / _safe_display_name(display_name)
    link = folder / "C:"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            return link
        os.symlink(str(drive_c), str(link), target_is_directory=True)
        return link
    except OSError as exc:  # e.g. Windows without symlink privilege, read-only home
        log.debug("could not create %s: %s", link, exc)
        return None


def iter_prefix_slugs() -> Iterable[str]:
    for item in list_prefixes():
        yield str(item["slug"])
