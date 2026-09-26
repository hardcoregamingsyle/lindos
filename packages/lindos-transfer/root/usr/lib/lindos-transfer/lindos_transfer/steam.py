"""Steam games (SPEC-WINDOWS §29.8).

Libraries come from ``steamapps/libraryfolders.vdf`` (or the old ``config/libraryfolders.vdf``) of the
Windows Steam folder (registry ``SteamPath``/``InstallPath``, else ``Program Files (x86)\\Steam``); each
installed game from ``appmanifest_<appid>.acf``.  A game item copies ``steamapps/common/<installdir>``
into the Linux Steam library and writes a cleaned manifest (Windows ``LauncherPath`` removed,
``StateFlags`` 1026 = "update required" so Steam checks the files before the first start).

Honesty: games whose anti-cheat blocks Linux are **listed with their other ways to play**
(``lindos-game route``), never copied as if they would work.  The report tells the user to force a
Proton version for games that also have a Linux version *before* Steam checks/installs them.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import vdf
from .copyengine import write_generated
from .profiles import windows_path_to_local
from .report import ItemResult
from .sources import ci_path, is_link

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = ["Game", "parse_libraryfolders", "read_manifest", "windows_steam_dirs", "library_dirs",
           "installed_games", "linux_steam_root", "rewrite_manifest", "plan_items", "run_item",
           "FORCE_PROTON_NOTE"]

log = logging.getLogger("lindos-transfer.steam")

STATE_FULLY_INSTALLED = 4
STATE_VALIDATE = "1026"   # UpdateRequired | UpdateStarted: Steam re-checks the files on next start
_APPID_RE = re.compile(r"^\d{1,10}$")
_UNSAFE_DIR = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
FORCE_PROTON_NOTE = ("If this game also has a Linux version: in Steam right-click it > Properties > Compatibility "
                     "and force a Steam Play (Proton) version BEFORE pressing Install/Update, so Steam keeps "
                     "these Windows files instead of downloading the Linux ones.")


@dataclass
class Game:
    appid: str
    name: str
    installdir: str
    size: int
    state_flags: int
    manifest: Path
    library: Path
    game_dir: Optional[Path]


def parse_libraryfolders(data: Dict[str, Any]) -> List[str]:
    """Library paths (Windows form) from new-style and old-style ``libraryfolders.vdf``."""
    top = vdf.get(data, "libraryfolders") or vdf.get(data, "LibraryFolders") or {}
    out: List[str] = []
    if not isinstance(top, dict):
        return out
    for key, value in top.items():
        if not key.isdigit():
            continue
        path = vdf.get(value, "path") if isinstance(value, dict) else value
        if isinstance(path, str) and path and path not in out:
            out.append(path)
    return out


def safe_installdir(name: Any) -> Optional[str]:
    """``installdir`` must be one plain folder name (it comes from a file on the source)."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name in (".", "..") or _UNSAFE_DIR.search(name) or len(name) > 255:
        return None
    return name


def read_manifest(path: Path, library: Path) -> Optional[Game]:
    try:
        data = vdf.load(path)
    except (OSError, vdf.VdfError) as exc:
        log.info("skipping %s: %s", path, exc)
        return None
    state = vdf.get(data, "AppState")
    if not isinstance(state, dict):
        return None
    appid = str(vdf.get(state, "appid") or "")
    installdir = safe_installdir(vdf.get(state, "installdir"))
    if not _APPID_RE.match(appid) or installdir is None:
        return None
    try:
        flags = int(str(vdf.get(state, "StateFlags") or "0"))
    except ValueError:
        flags = 0
    try:
        size = int(str(vdf.get(state, "SizeOnDisk") or "0"))
    except ValueError:
        size = 0
    game_dir = None
    for rel in (("steamapps", "common", installdir), ("common", installdir)):
        cand = ci_path(library, list(rel))
        if cand is not None and cand.is_dir():
            game_dir = cand
            break
    name = str(vdf.get(state, "name") or installdir)
    return Game(appid, name, installdir, max(size, 0), flags, path, library, game_dir)


def windows_steam_dirs(ctx: "Context") -> List[Path]:
    """Local paths of the Windows Steam client folder(s)."""
    from . import winreg

    out: List[Path] = []
    raw = winreg.steam_paths(ctx.source.hive("SOFTWARE"), ctx.ntuser)
    for win in raw:
        local, _why = windows_path_to_local(win, ctx.drives)
        if local is not None and local not in out:
            out.append(local)
    default = ctx.source.path(["Program Files (x86)", "Steam"])
    if default is not None and default not in out:
        out.append(default)
    return out


def library_dirs(ctx: "Context") -> List[Path]:
    """Every Steam library folder on the source (a folder holding ``steamapps``)."""
    src = ctx.source
    if src.is_bundle:
        steam = src.manifest.get("steam") or {}
        rel = steam.get("dir")
        folder = src.path(str(rel)) if rel else None
        return [folder] if folder is not None and folder.is_dir() else []
    libs: List[Path] = []
    for root in windows_steam_dirs(ctx):
        if root not in libs:
            libs.append(root)
        for rel in (("steamapps", "libraryfolders.vdf"), ("config", "libraryfolders.vdf")):
            f = ci_path(root, list(rel))
            if f is None or not f.is_file():
                continue
            try:
                paths = parse_libraryfolders(vdf.load(f))
            except (OSError, vdf.VdfError) as exc:
                log.info("cannot read %s: %s", f, exc)
                continue
            for win in paths:
                local, _why = windows_path_to_local(win, ctx.drives)
                if local is not None and local not in libs:
                    libs.append(local)
    return libs


def installed_games(ctx: "Context") -> List[Game]:
    games: Dict[str, Game] = {}
    for lib in library_dirs(ctx):
        folders = [lib] if ctx.source.is_bundle else []
        apps = ci_path(lib, "steamapps")
        if apps is not None:
            folders.append(apps)
        for folder in folders:
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for e in entries:
                if is_link(e) or not re.match(r"^appmanifest_\d+\.acf$", e.name, re.IGNORECASE):
                    continue
                game = read_manifest(e, lib)
                if game is not None and game.appid not in games:
                    games[game.appid] = game
    return sorted(games.values(), key=lambda g: g.name.lower())


def linux_steam_root(home: Path) -> Path:
    """The Linux Steam folder (native first, then Flatpak; native default when Steam isn't installed yet)."""
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    if native.is_dir():
        return native
    if flatpak.is_dir():
        return flatpak
    return native


def rewrite_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop Windows-only fields and ask Steam to verify the copied files on first start."""
    state = vdf.get(data, "AppState")
    if not isinstance(state, dict):
        return data
    for key in [k for k in state if k.lower() == "launcherpath"]:
        del state[key]
    for key in [k for k in state if k.lower() == "stateflags"]:
        del state[key]
    state["StateFlags"] = STATE_VALIDATE
    return data


def _blocked(ctx: "Context", game: Game) -> Optional[Dict[str, Any]]:
    from .apps import blocked_game

    return blocked_game(ctx, appid=game.appid, name=game.name)


def plan_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import make_item

    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    root = linux_steam_root(ctx.home)
    warnings: List[str] = []
    for game in installed_games(ctx):
        if not game.state_flags & STATE_FULLY_INSTALLED:
            continue
        block = _blocked(ctx, game)
        dest = root / "steamapps" / "common" / game.installdir
        if block is not None:
            items.append(make_item(f"steam:{game.appid}", "steam-games", game.name, game.game_dir or game.manifest,
                                   dest, bytes_=game.size, selected=False,
                                   notes=[f"{game.name} uses anti-cheat that blocks Linux - copying it would not make "
                                          f"it run. Other ways to play: lindos-game route \"{block['route']}\""]))
            continue
        if game.game_dir is None:
            skipped.append({"path": str(game.manifest), "reason": f"{game.name}: game files are not in the transfer "
                            "folder (tick 'Steam games' in the Windows kit to include them)"})
            continue
        items.append(make_item(f"steam:{game.appid}", "steam-games", game.name, game.game_dir, dest,
                               bytes_=game.size, notes=[FORCE_PROTON_NOTE, "Close Steam before the transfer."]))
    if items and not ctx.which("steam") and not (ctx.home / ".var" / "app" / "com.valvesoftware.Steam").is_dir():
        warnings.append("Steam is not installed on Lindos yet: install it first (lindos-game install steam) so the "
                        "copied games can be found.")
    return items, skipped, warnings


def run_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    appid = item["id"].split(":", 1)[-1]
    game = next((g for g in installed_games(ctx) if g.appid == appid), None)
    if game is None or game.game_dir is None:
        return ItemResult.skipped_item(item, "the game is no longer on the source")
    block = _blocked(ctx, game)
    if block is not None:
        return ItemResult.skipped_item(item, f"not copied: anti-cheat blocks Linux. Other ways to play: "
                                             f"lindos-game route \"{block['route']}\"")
    root = linux_steam_root(ctx.home)
    manifest_dest = root / "steamapps" / f"appmanifest_{game.appid}.acf"
    if os.path.lexists(manifest_dest):
        return ItemResult.skipped_item(item, "already installed in Steam on Lindos")
    dest = root / "steamapps" / "common" / game.installdir
    stats = ctx.engine.copy_tree(game.game_dir, dest, item=item["id"], skip_appdata=False)
    notes = [FORCE_PROTON_NOTE]
    if not stats.errors:
        try:
            text = vdf.dumps(rewrite_manifest(vdf.load(game.manifest)))
        except (OSError, vdf.VdfError) as exc:
            notes.append(f"Steam's install record could not be copied ({exc}); press Install in Steam and it "
                         "will find the files.")
        else:
            write_generated(manifest_dest, text.encode("utf-8"), dry_run=ctx.dry_run)
    return ItemResult.from_stats(item, stats, dest=str(dest), notes=notes)
