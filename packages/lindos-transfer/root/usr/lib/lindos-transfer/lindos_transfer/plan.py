"""The transfer plan (JSON schema 1, SPEC-WINDOWS §29.5, §29.6) and running it.

``build_plan`` looks at a :class:`~lindos_transfer.sources.Source` and one user and lists what can
move (categories below), where it goes on Lindos, how many files/bytes, what is skipped and why.
The plan is plain JSON so the GUI and scripts can review it, untick items and hand it back:

``run --plan plan.json`` re-reads the source, rebuilds the items and applies the plan's
*selections* (item ids).  Every item's destination *under* the Lindos home is always recomputed by
Lindos, never taken from the file.  The home itself (``dest_home``) is not trusted at face value
either: ``lindos_transfer.cli.cmd_run`` only honours a loaded plan's ``dest_home`` when it still
matches the invoking user's real home, and otherwise refuses unless ``--dest`` is passed explicitly
-- so an edited or foreign plan cannot silently redirect a whole transfer into some other directory.

Categories (binding ids): ``desktop documents downloads music pictures videos saved-games favorites
onedrive bookmarks firefox wallpaper fonts wifi apps steam-games``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import TransferError, state_dir, user_home
from .copyengine import CopyEngine, DiskFullError, Journal, check_free_space, human_size, open_private
from .profiles import FolderInfo, UserProfile, drive_map, find_user, onedrive_roots, open_ntuser, resolve_folders
from .regf import Hive
from .report import ItemResult, build_report, save_report
from .sources import Source

__all__ = [
    "SCHEMA",
    "CATEGORIES",
    "FOLDER_CATEGORIES",
    "LABELS",
    "OUT_DIR_NAME",
    "PlanError",
    "Context",
    "parse_categories",
    "xdg_dirs",
    "new_plan_id",
    "make_context",
    "build_plan",
    "validate_plan",
    "load_plan",
    "save_plan",
    "apply_selection",
    "selected_items",
    "run_plan",
]

log = logging.getLogger("lindos-transfer.plan")

SCHEMA = 1
CATEGORIES: Tuple[str, ...] = ("desktop", "documents", "downloads", "music", "pictures", "videos",
                               "saved-games", "favorites", "onedrive", "bookmarks", "firefox", "wallpaper",
                               "fonts", "wifi", "apps", "steam-games")
FOLDER_CATEGORIES: Tuple[str, ...] = ("desktop", "documents", "downloads", "music", "pictures", "videos",
                                      "saved-games", "favorites")
#: Categories that are not selected unless asked for explicitly (``--only``) or ticked by the user.
OPT_IN_CATEGORIES = frozenset({"steam-games"})
LABELS: Dict[str, str] = {
    "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "music": "Music",
    "pictures": "Pictures", "videos": "Videos", "saved-games": "Saved games",
    "favorites": "Favorites (Internet Explorer / old Edge)", "onedrive": "OneDrive",
    "bookmarks": "Browser bookmarks", "firefox": "Firefox bookmarks and history", "wallpaper": "Desktop wallpaper",
    "fonts": "Your fonts", "wifi": "Wi-Fi networks", "apps": "Apps", "steam-games": "Steam games",
}
OUT_DIR_NAME = "Transferred from Windows"
_PLAN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_XDG_KEYS = {"desktop": ("XDG_DESKTOP_DIR", "Desktop"), "download": ("XDG_DOWNLOAD_DIR", "Downloads"),
             "documents": ("XDG_DOCUMENTS_DIR", "Documents"), "music": ("XDG_MUSIC_DIR", "Music"),
             "pictures": ("XDG_PICTURES_DIR", "Pictures"), "videos": ("XDG_VIDEOS_DIR", "Videos")}
MAX_PLAN_SKIPPED = 5000
MAX_PLAN_BYTES = 64 << 20


class PlanError(TransferError):
    """The plan file is not a valid Lindos transfer plan."""


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    """Everything planning and running needs (one source, one user, one Lindos home)."""

    source: Source
    user: UserProfile
    home: Path
    dirs: Dict[str, Path]
    out_dir: Path
    engine: CopyEngine
    ntuser: Optional[Hive] = None
    drives: Dict[str, Path] = field(default_factory=dict)
    folders: Dict[str, FolderInfo] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=lambda: {"firefox_passwords": False})
    explicit: Set[str] = field(default_factory=set)
    run: Callable[..., Any] = subprocess.run
    which: Callable[[str], Optional[str]] = shutil.which
    dry_run: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    helper_mod: Any = None
    theme_mod: Any = None
    app_map: Optional[Dict[str, Any]] = None

    def helper(self) -> Any:
        """``lindos.helper`` (lindos-core), imported lazily."""
        if self.helper_mod is None:
            try:
                from lindos import helper as _helper  # type: ignore[import-not-found]
            except ImportError as exc:
                raise TransferError("lindos-core is not installed (needed for system changes)") from exc
            self.helper_mod = _helper
        return self.helper_mod

    def theme(self) -> Any:
        """``lindos.theme`` (lindos-core), or ``None`` when unavailable."""
        if self.theme_mod is None:
            try:
                from lindos import theme as _theme  # type: ignore[import-not-found]
            except ImportError:
                return None
            self.theme_mod = _theme
        return self.theme_mod

    def close(self) -> None:
        if self.ntuser is not None:
            self.ntuser.close()
            self.ntuser = None
        self.source.close()


def parse_categories(values: Optional[Iterable[str]]) -> Optional[Set[str]]:
    """``["documents,pictures", "wifi"]`` -> ``{"documents","pictures","wifi"}``; unknown ids raise."""
    if values is None:
        return None
    out: Set[str] = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part not in CATEGORIES:
                raise TransferError(f"unknown category '{part}' (choose from: {', '.join(CATEGORIES)})")
            out.add(part)
    return out


def xdg_dirs(home: Path) -> Dict[str, Path]:
    """The user's XDG folders from ``~/.config/user-dirs.dirs`` (Mint localises their names)."""
    dirs = {k: home / default for k, (_var, default) in _XDG_KEYS.items()}
    cfg = home / ".config" / "user-dirs.dirs"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return dirs
    by_var = {var: k for k, (var, _d) in _XDG_KEYS.items()}
    for line in text.splitlines():
        m = re.match(r'^\s*(XDG_[A-Z]+_DIR)\s*=\s*"(.*)"\s*$', line)
        if not m or m.group(1) not in by_var:
            continue
        value = m.group(2)
        if value.startswith("$HOME"):
            rest = value[len("$HOME"):].lstrip("/")
            if not rest:
                continue  # "$HOME/" means the folder is disabled: keep the default name
            path = home.joinpath(*rest.split("/"))
        elif value.startswith("/") and value.strip("/"):
            path = Path(value)
        else:
            continue
        dirs[by_var[m.group(1)]] = path
    return dirs


def new_plan_id(now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + "-" + os.urandom(2).hex()


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(child)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False


def make_context(source: Source, *, user: Optional[str] = None, dest: Optional[str] = None,
                 firefox_passwords: bool = False, explicit: Optional[Set[str]] = None,
                 run: Callable[..., Any] = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which,
                 dry_run: bool = False, emit: Optional[Callable[[Dict[str, Any]], None]] = None,
                 journal: Optional[Journal] = None, getxattr: Optional[Callable[[str, str], bytes]] = None,
                 env: Optional[Mapping[str, str]] = None, helper_mod: Any = None,
                 theme_mod: Any = None) -> Context:
    """Resolve the user, their folders and the Lindos destinations."""
    home = Path(dest).expanduser() if dest else user_home()
    if not source.is_bundle and _inside(home, source.root):
        raise TransferError("The destination is on the Windows drive. Lindos never writes to Windows; "
                            "choose a folder on the Lindos disk.")
    prof = find_user(source, user)
    ntuser = open_ntuser(prof) if not source.is_bundle else None
    drives = drive_map(source, run=run)
    dirs = xdg_dirs(home)
    engine = CopyEngine(driver=source.driver, journal=journal, emit=emit, dry_run=dry_run, getxattr=getxattr,
                        allow_firefox_passwords=firefox_passwords)
    ctx = Context(source=source, user=prof, home=home, dirs=dirs,
                  out_dir=dirs["documents"] / OUT_DIR_NAME, engine=engine, ntuser=ntuser, drives=drives,
                  options={"firefox_passwords": bool(firefox_passwords)}, explicit=set(explicit or ()),
                  run=run, which=which, dry_run=dry_run, env=dict(env if env is not None else os.environ),
                  helper_mod=helper_mod, theme_mod=theme_mod)
    ctx.folders = resolve_folders(source, prof, ntuser=ntuser, drives=drives)
    return ctx


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
def make_item(item_id: str, category: str, label: str, src: Any, dest: Any, *, files: int = 0,
              bytes_: int = 0, selected: bool = True, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """One plan item in the exact schema-1 shape."""
    return {"id": item_id, "category": category, "label": label, "src": str(src or ""),
            "dest": str(dest or ""), "files": int(files), "bytes": int(bytes_), "selected": bool(selected),
            "notes": list(notes or [])}


def folder_dest(ctx: Context, category: str, src: Optional[Path] = None) -> Path:
    if category in ("desktop", "documents", "music", "pictures", "videos"):
        return ctx.dirs[category]
    if category == "downloads":
        return ctx.dirs["download"]
    if category == "saved-games":
        return ctx.dirs["documents"] / "Saved Games"
    if category == "favorites":
        return ctx.out_dir / "Favorites"
    if category == "onedrive":
        name = src.name if src is not None and src.name.lower().startswith("onedrive") else "OneDrive"
        return ctx.home / f"{name} (from Windows)"
    raise ValueError(category)


def _folder_items(ctx: Context, cats: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for cat in FOLDER_CATEGORIES:
        if cat not in cats:
            continue
        info = ctx.folders.get(cat)
        if info is None or info.path is None:
            if info is not None and info.note and info.source != "bundle":
                skipped.append({"path": info.windows_path or LABELS[cat], "reason": info.note})
            continue
        notes: List[str] = []
        if info.note:
            notes.append(info.note)
        if info.onedrive:
            notes.append("This folder is backed up to OneDrive: only files stored on this disk are copied; "
                         "online-only files are listed in the report.")
        items.append(make_item(cat, cat, LABELS[cat], info.path, folder_dest(ctx, cat), notes=notes))
    if "onedrive" not in cats:
        return items, skipped
    n = 0
    for root in onedrive_roots(ctx.source, ctx.user, ctx.ntuser, ctx.drives):
        if root.path is None:
            skipped.append({"path": root.windows_path or "OneDrive", "reason": root.note or
                            "OneDrive online-only folder (not on this disk)"})
            continue
        n += 1
        item_id = "onedrive" if n == 1 else f"onedrive:{n}"
        label = LABELS["onedrive"] if root.path.name.lower() == "onedrive" else root.path.name
        items.append(make_item(item_id, "onedrive", label, root.path, folder_dest(ctx, "onedrive", root.path),
                               notes=["Only files stored on this disk are copied; online-only OneDrive files "
                                      "are never downloaded by Lindos (they are listed in the report)."]))
    return items, skipped


def nested_excludes(items: Sequence[Dict[str, Any]], item: Dict[str, Any]) -> List[Path]:
    """Folders of *other* folder items that live inside *item* (copied once, by their own item)."""
    if item["category"] not in FOLDER_CATEGORIES + ("onedrive",):
        return []
    src = Path(item["src"])
    out: List[Path] = []
    for other in items:
        if other is item or other["category"] not in FOLDER_CATEGORIES + ("onedrive",):
            continue
        osrc = Path(other["src"])
        if osrc != src and _inside(osrc, src):
            out.append(osrc)
    return out


def build_plan(source: Source, *, user: Optional[str] = None, only: Optional[Iterable[str]] = None,
               exclude: Optional[Iterable[str]] = None, dest: Optional[str] = None,
               firefox_passwords: bool = False, count: bool = True, now: Optional[float] = None,
               context: Optional[Context] = None, **ctx_kw: Any) -> Tuple[Dict[str, Any], Context]:
    """Build a schema-1 plan.  Returns ``(plan, context)``; the caller closes the context."""
    from . import apps as apps_mod
    from . import browsers, fonts, steam, wallpaper, wifi

    only_set = parse_categories(only)
    excl_set = parse_categories(exclude) or set()
    cats = [c for c in CATEGORIES if (only_set is None or c in only_set) and c not in excl_set]
    ctx = context or make_context(source, user=user, dest=dest, firefox_passwords=firefox_passwords,
                                  explicit=only_set or set(), **ctx_kw)
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    warnings: List[str] = list(source.warnings)
    folder_items, folder_skips = _folder_items(ctx, cats)
    items.extend(folder_items)
    skipped.extend(folder_skips)
    producers = (("bookmarks", browsers.plan_bookmark_items), ("firefox", browsers.plan_firefox_items),
                 ("wallpaper", wallpaper.plan_items), ("fonts", fonts.plan_items), ("wifi", wifi.plan_items),
                 ("steam-games", steam.plan_items))
    for cat, producer in producers:
        if cat not in cats:
            continue
        try:
            new_items, new_skips, new_warn = producer(ctx)
        except TransferError as exc:
            warnings.append(f"{LABELS[cat]}: {exc}")
            continue
        items.extend(new_items)
        skipped.extend(new_skips)
        warnings.extend(new_warn)
    if count:
        for it in items:
            if it["category"] in FOLDER_CATEGORIES + ("onedrive",):
                res = ctx.engine.scan(Path(it["src"]), exclude=nested_excludes(items, it))
                it["files"], it["bytes"] = res.files, res.bytes
                skipped.extend(res.skipped)
    for it in items:
        if it["category"] in OPT_IN_CATEGORIES and it["category"] not in ctx.explicit:
            it["selected"] = False
    apps: List[Dict[str, Any]] = []
    if "apps" in cats:
        try:
            apps = apps_mod.plan_apps(ctx)
        except TransferError as exc:
            warnings.append(f"Apps: {exc}")
    if source.is_bundle:
        for s in source.manifest.get("skipped") or []:
            reason = str(s.get("reason") or "skipped on Windows")
            if reason.lower() in ("online-only", "online only", "offline"):
                reason = "OneDrive online-only file (not on this disk)"
            skipped.append({"path": str(s.get("path") or ""), "reason": reason})
    if ctx.user.stale or source.registry_stale:
        msg = ("Some Windows settings may be out of date (Windows did not shut down fully, so its latest "
               "registry changes are not on the disk yet).")
        if msg not in warnings:
            warnings.append(msg)
    if len(skipped) > MAX_PLAN_SKIPPED:
        warnings.append(f"{len(skipped) - MAX_PLAN_SKIPPED} more skipped files are not listed here.")
        skipped = skipped[:MAX_PLAN_SKIPPED]
    now = time.time() if now is None else now
    plan = {
        "schema": SCHEMA,
        "id": new_plan_id(now),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": source.as_plan_dict(),
        "user": ctx.user.name,
        "dest_home": str(ctx.home),
        "items": items,
        "apps": apps,
        "options": {"firefox_passwords": bool(ctx.options.get("firefox_passwords"))},
        "skipped": skipped,
        "warnings": warnings,
    }
    return plan, ctx


# --------------------------------------------------------------------------- #
# validation / IO
# --------------------------------------------------------------------------- #
def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise PlanError(f"invalid plan: {msg}")


def validate_plan(plan: Any) -> Dict[str, Any]:
    """Strictly check a plan loaded from a file (schema 1).  Returns it; raises :class:`PlanError`."""
    from .apps import validate_action

    _expect(isinstance(plan, dict), "not a JSON object")
    _expect(plan.get("schema") == SCHEMA, f"schema must be {SCHEMA}")
    _expect(isinstance(plan.get("id"), str) and bool(_PLAN_ID_RE.match(plan["id"])), "bad id")
    src = plan.get("source")
    _expect(isinstance(src, dict) and src.get("type") in ("partition", "bundle")
            and isinstance(src.get("root"), str) and bool(src.get("root")), "bad source")
    _expect(isinstance(plan.get("user"), str) and bool(plan["user"]), "bad user")
    _expect(isinstance(plan.get("dest_home"), str) and bool(plan["dest_home"]), "bad dest_home")
    items = plan.get("items")
    _expect(isinstance(items, list), "items must be a list")
    seen: Set[str] = set()
    for it in items:
        _expect(isinstance(it, dict), "item must be an object")
        _expect(isinstance(it.get("id"), str) and bool(it["id"]) and it["id"] not in seen, "bad or duplicate item id")
        seen.add(it["id"])
        _expect(it.get("category") in CATEGORIES and it["category"] != "apps", f"bad category in {it['id']}")
        for key in ("label", "src", "dest"):
            _expect(isinstance(it.get(key), str), f"{it['id']}: {key} must be text")
        for key in ("files", "bytes"):
            _expect(isinstance(it.get(key), int) and not isinstance(it[key], bool) and it[key] >= 0,
                    f"{it['id']}: {key} must be a whole number")
        _expect(isinstance(it.get("selected"), bool), f"{it['id']}: selected must be true/false")
        _expect(isinstance(it.get("notes"), list) and all(isinstance(n, str) for n in it["notes"]),
                f"{it['id']}: notes must be a list of text")
    apps = plan.get("apps", [])
    _expect(isinstance(apps, list), "apps must be a list")
    for app in apps:
        _expect(isinstance(app, dict) and isinstance(app.get("windows_name"), str), "bad app entry")
        _expect(isinstance(app.get("actions"), list), f"{app.get('windows_name')}: actions must be a list")
        for act in app["actions"]:
            try:
                validate_action(act)
            except ValueError as exc:
                raise PlanError(f"invalid plan: {app['windows_name']}: {exc}") from exc
        chosen = app.get("chosen")
        _expect(chosen is None or (isinstance(chosen, int) and not isinstance(chosen, bool)
                                   and 0 <= chosen < len(app["actions"])), f"{app['windows_name']}: bad chosen")
        _expect(isinstance(app.get("selected"), bool), f"{app['windows_name']}: selected must be true/false")
        _expect(app.get("winget_id") is None or isinstance(app.get("winget_id"), str), "bad winget_id")
    opts = plan.get("options", {})
    _expect(isinstance(opts, dict) and isinstance(opts.get("firefox_passwords", False), bool), "bad options")
    _expect(isinstance(plan.get("skipped", []), list), "skipped must be a list")
    _expect(isinstance(plan.get("warnings", []), list), "warnings must be a list")
    return plan


def load_plan(path: os.PathLike) -> Dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_PLAN_BYTES + 1)
    except OSError as exc:
        raise PlanError(f"cannot read the plan file: {exc.strerror or exc}") from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanError("the plan file is too large")
    try:
        plan = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlanError(f"the plan file is not valid JSON: {exc}") from exc
    return validate_plan(plan)


def save_plan(plan: Dict[str, Any], path: os.PathLike) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    # A plan names the source computer, the Windows user and every path that would be copied, so
    # it is written 0600 (independent of the umask) like the journal and the report -- not just
    # under ~/.local/state, but wherever it is saved (e.g. ``plan -o plan.json``).
    with open_private(tmp, "w", newline="\n") as fh:
        json.dump(plan, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)


def apply_selection(fresh: Dict[str, Any], saved: Dict[str, Any]) -> List[str]:
    """Copy the saved plan's id, options, item selections and app choices onto a freshly built plan."""
    notes: List[str] = []
    fresh["id"] = saved["id"]
    fresh["options"] = dict(saved.get("options") or fresh.get("options") or {})
    chosen = {it["id"]: it["selected"] for it in saved.get("items") or []}
    for it in fresh["items"]:
        if it["id"] in chosen:
            it["selected"] = chosen[it["id"]]
        else:
            it["selected"] = False
            notes.append(f"'{it['label']}' is new since the plan was made and was left out.")
    missing = set(chosen) - {it["id"] for it in fresh["items"]}
    for mid in sorted(missing):
        if chosen[mid]:
            notes.append(f"'{mid}' from the plan is no longer on the source.")
    saved_apps = {(a.get("windows_name"), a.get("publisher")): a for a in saved.get("apps") or []}
    for app in fresh.get("apps") or []:
        old = saved_apps.get((app.get("windows_name"), app.get("publisher")))
        if old is not None:
            app["selected"] = bool(old.get("selected"))
            if isinstance(old.get("chosen"), int) and 0 <= old["chosen"] < len(app["actions"]):
                app["chosen"] = old["chosen"]
    return notes


def selected_items(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted((it for it in plan.get("items") or [] if it.get("selected")),
                  key=lambda it: order.get(it["category"], 99))


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def _run_folder(item: Dict[str, Any], ctx: Context, items: Sequence[Dict[str, Any]]) -> ItemResult:
    src, dest = Path(item["src"]), Path(item["dest"])
    if _inside(dest, src):
        return ItemResult.failed(item, "the destination is inside the folder being copied")
    stats = ctx.engine.copy_tree(src, dest, item=item["id"], exclude=nested_excludes(items, item))
    return ItemResult.from_stats(item, stats)


def run_plan(plan: Dict[str, Any], ctx: Context, *, emit: Optional[Callable[[Dict[str, Any]], None]] = None,
             state: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Execute the selected items; returns the report (``None`` when nothing is selected)."""
    from . import browsers, fonts, steam, wallpaper, wifi

    chosen = selected_items(plan)
    if not chosen:
        return None
    engine = ctx.engine
    engine.dry_run = ctx.dry_run
    engine.total_bytes = sum(int(it.get("bytes") or 0) for it in chosen)
    engine.done_bytes = 0
    if emit is not None:
        engine.emit_cb = emit
    base_state = Path(state) if state is not None else state_dir()
    if engine.journal.path is None and not ctx.dry_run:
        engine.journal = Journal(base_state / plan["id"] / "journal.jsonl")
    if not ctx.dry_run:
        check_free_space(ctx.home, engine.total_bytes)
        save_plan(plan, base_state / plan["id"] / "plan.json")
    started = time.time()
    engine.emit("start", message=f"{len(chosen)} item(s), {human_size(engine.total_bytes)}")
    results: List[ItemResult] = []
    aborted: Optional[str] = None
    handlers: Dict[str, Callable[[Dict[str, Any], Context], ItemResult]] = {
        "bookmarks": browsers.run_bookmarks_item, "firefox": browsers.run_firefox_item,
        "wallpaper": wallpaper.run_item, "fonts": fonts.run_item, "wifi": wifi.run_item,
        "steam-games": steam.run_item,
    }
    for item in chosen:
        if aborted:
            results.append(ItemResult.skipped_item(item, f"not started: {aborted}"))
            continue
        engine.emit("item", item=item["id"], path=item.get("dest", ""), message=item.get("label", ""))
        try:
            if item["category"] in FOLDER_CATEGORIES or item["category"] == "onedrive":
                res = _run_folder(item, ctx, plan["items"])
            else:
                res = handlers[item["category"]](item, ctx)
        except DiskFullError as exc:
            aborted = str(exc)
            engine.emit("error", item=item["id"], message=aborted)
            res = ItemResult.failed(item, aborted)
        except TransferError as exc:
            engine.emit("error", item=item["id"], message=str(exc))
            res = ItemResult.failed(item, str(exc))
        except Exception as exc:  # noqa: BLE001 - one broken item must not stop the others
            log.exception("item %s failed", item["id"])
            engine.emit("error", item=item["id"], message=f"unexpected problem: {exc}")
            res = ItemResult.failed(item, f"unexpected problem: {exc}")
        if ctx.dry_run and res.status == "done":
            res.status = "dry-run"
        results.append(res)
    engine.journal.close()
    report = build_report(plan, results, started=started, dry_run=ctx.dry_run)
    if not ctx.dry_run:
        paths = save_report(report, html_dir=ctx.out_dir, state=base_state)
        report["report_files"] = paths
    engine.emit("done", message=f"{report['totals']['files']} files, {human_size(report['totals']['bytes'])}")
    return report
