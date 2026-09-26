"""Browsers: find profiles on the source, plan and run bookmark / Firefox items (SPEC-WINDOWS §29.8).

* **Chromium family** (Chrome, Edge, Brave, Opera, Vivaldi, Chromium; ``User Data/<profile>``):
  ``Bookmarks`` + ``AccountBookmarks`` -> ``Bookmarks - <Browser> (<profile>).html`` in
  ``~/Documents/Transferred from Windows/``.  When only encrypted bookmark files exist the item
  says "use the browser's own Export bookmarks".  Passwords, cookies and payment data are never
  read (deny-listed; Windows-encrypted) -- the report points to browser sync / password export.
* **Firefox**: never the whole profile.  A new Linux profile ``windows-import`` (in
  ``~/.mozilla/firefox`` if ``~/.mozilla`` exists, else ``$XDG_CONFIG_HOME/mozilla/firefox`` --
  Firefox 147+) receives ``places.sqlite`` (+ ``-wal``) and ``favicons.sqlite`` (+ ``-wal``), plus
  ``key4.db`` + ``logins.json`` only with ``--firefox-passwords``; it is registered in
  ``profiles.ini`` (made default only when Linux Firefox has no profile yet).  ``profiles.ini``,
  ``installs.ini``, ``extensions.json`` and ``compatibility.ini`` are never copied.  A bookmarks HTML
  file is always written as well.
* Internet Explorer / old Edge *Favorites* (``.url`` files) also become a bookmarks HTML file.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import TransferError, secrets
from .bookmarks import (BookmarkError, Folder, count_links, favorites_roots, firefox_roots, netscape_html,
                        read_chromium_file)
from .copyengine import CopyStats, write_generated
from .profiles import windows_path_to_local
from .report import ItemResult
from .sources import ci_child, ci_path, is_link

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = [
    "CHROMIUM_BROWSERS",
    "BOOKMARK_FILES",
    "ENCRYPTED_BOOKMARK_FILES",
    "FIREFOX_DATA_FILES",
    "FIREFOX_NEVER_COPY",
    "ChromiumProfile",
    "FirefoxProfile",
    "safe_filename",
    "chromium_profiles",
    "firefox_profiles",
    "linux_firefox_root",
    "firefox_running",
    "register_firefox_profile",
    "plan_bookmark_items",
    "plan_firefox_items",
    "run_bookmarks_item",
    "run_firefox_item",
]

log = logging.getLogger("lindos-transfer.browsers")

#: (id, label, AppData sub-folder, path below it) -- the folder is a Chromium "User Data" directory.
CHROMIUM_BROWSERS: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    ("chrome", "Google Chrome", "Local", ("Google", "Chrome", "User Data")),
    ("chrome-beta", "Google Chrome Beta", "Local", ("Google", "Chrome Beta", "User Data")),
    ("chrome-dev", "Google Chrome Dev", "Local", ("Google", "Chrome Dev", "User Data")),
    ("chrome-canary", "Google Chrome Canary", "Local", ("Google", "Chrome SxS", "User Data")),
    ("chromium", "Chromium", "Local", ("Chromium", "User Data")),
    ("edge", "Microsoft Edge", "Local", ("Microsoft", "Edge", "User Data")),
    ("edge-beta", "Microsoft Edge Beta", "Local", ("Microsoft", "Edge Beta", "User Data")),
    ("edge-dev", "Microsoft Edge Dev", "Local", ("Microsoft", "Edge Dev", "User Data")),
    ("brave", "Brave", "Local", ("BraveSoftware", "Brave-Browser", "User Data")),
    ("vivaldi", "Vivaldi", "Local", ("Vivaldi", "User Data")),
    ("opera", "Opera", "Roaming", ("Opera Software", "Opera Stable")),
    ("opera-gx", "Opera GX", "Roaming", ("Opera Software", "Opera GX Stable")),
)
_LABELS = {b[0]: b[1] for b in CHROMIUM_BROWSERS}
BOOKMARK_FILES = ("Bookmarks", "AccountBookmarks")
ENCRYPTED_BOOKMARK_FILES = ("EncryptedBookmarks2", "EncryptedAccountBookmarks2", "EncryptedBookmarks",
                            "EncryptedAccountBookmarks")
_SKIP_PROFILE_DIRS = frozenset({"system profile", "guest profile", "crashpad", "safe browsing",
                                "shadercache", "grshadercache", "snapshots"})
#: Firefox files copied into the new Linux profile (bookmarks + history + site icons).
FIREFOX_DATA_FILES = ("places.sqlite", "places.sqlite-wal", "favicons.sqlite", "favicons.sqlite-wal")
#: Never copied (Windows paths, install hashes, downgrade protection).
FIREFOX_NEVER_COPY = ("profiles.ini", "installs.ini", "extensions.json", "compatibility.ini")
IMPORT_PROFILE_NAME = "windows-import"
PASSWORD_NOTE = ("Passwords, cookies and payment cards are not transferred (Windows encrypts them): use "
                 "the browser's sync, or export your passwords on Windows and import them on Lindos.")
ENCRYPTED_NOTE = ("This browser keeps its bookmarks encrypted with Windows. On Windows open the browser, choose "
                  "Bookmarks > Export bookmarks, and import that file on Lindos (or turn on browser sync).")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\\/:*?"<>|]+')


def safe_filename(text: str, *, limit: int = 100) -> str:
    """A file-name-safe version of *text* (no separators, controls, leading/trailing dots)."""
    out = _UNSAFE_NAME.sub("_", str(text)).strip(" .")
    out = out.replace("..", "_")
    return (out[:limit].rstrip(" .") or "profile")


@dataclass
class ChromiumProfile:
    browser: str
    label: str
    profile: str
    path: Path
    files: List[Path] = field(default_factory=list)
    encrypted_only: bool = False


@dataclass
class FirefoxProfile:
    name: str
    path: Path
    files: List[Path] = field(default_factory=list)
    password_files: List[Path] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def _profile_files(folder: Path) -> Tuple[List[Path], bool]:
    files: List[Path] = []
    encrypted = False
    for name in BOOKMARK_FILES:
        p = ci_path(folder, name)
        if p is not None and p.is_file():
            files.append(p)
    for name in ENCRYPTED_BOOKMARK_FILES:
        p = ci_child(folder, name)
        if p is not None and not is_link(p):
            encrypted = True  # existence only: encrypted files are never opened
    return files, encrypted


def chromium_profiles(ctx: "Context") -> List[ChromiumProfile]:
    """Chromium-family profiles that have bookmarks (or only encrypted ones)."""
    out: List[ChromiumProfile] = []
    src = ctx.source
    if src.is_bundle:
        for entry in src.manifest.get("browsers") or []:
            browser = str(entry.get("browser") or "").lower()
            if not browser or browser == "firefox" or not entry.get("bookmarks"):
                continue
            path = src.path(str(entry["bookmarks"]))
            if path is None or not path.is_file():
                continue
            files, encrypted = _profile_files(path.parent)
            if path not in files:
                files.insert(0, path)
            out.append(ChromiumProfile(browser, _LABELS.get(browser, browser.title()),
                                       str(entry.get("profile") or entry.get("name") or "Default"),
                                       path.parent, files, False))
        return out
    prof = ctx.user.profile_dir
    if prof is None:
        return out
    for bid, label, area, rel in CHROMIUM_BROWSERS:
        base = ci_path(prof, ["AppData", area, *rel])
        if base is None or not base.is_dir():
            continue
        candidates: List[Tuple[str, Path]] = []
        files, enc = _profile_files(base)
        if files or enc:  # Opera keeps the default profile's files directly in its folder
            candidates.append(("Default", base))
        try:
            children = sorted(base.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.name.lower() in _SKIP_PROFILE_DIRS or is_link(child) or not child.is_dir():
                continue
            if any(c[1] == child for c in candidates):
                continue
            candidates.append((child.name, child))
        for name, folder in candidates:
            files, enc = _profile_files(folder)
            if not files and not enc:
                continue
            out.append(ChromiumProfile(bid, label, name, folder, files, encrypted_only=not files))
    return out


def _read_ini(data: bytes) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    text = data.decode("utf-8-sig", errors="replace")
    try:
        cp.read_string(text)
    except configparser.Error as exc:
        log.warning("profiles.ini is damaged: %s", exc)
    return cp


def _firefox_profile_files(folder: Path, name: str) -> Optional[FirefoxProfile]:
    places = ci_path(folder, "places.sqlite")
    if places is None or not places.is_file():
        return None
    prof = FirefoxProfile(name=name, path=folder)
    for fname in FIREFOX_DATA_FILES:
        p = ci_path(folder, fname)
        if p is not None and p.is_file():
            prof.files.append(p)
    for fname in secrets.FIREFOX_PASSWORD_FILES:
        p = ci_path(folder, fname)
        if p is not None and p.is_file():
            prof.password_files.append(p)
    return prof


def firefox_profiles(ctx: "Context") -> List[FirefoxProfile]:
    """Firefox profiles with ``places.sqlite`` (partition: profiles.ini, then Profiles/*; bundle: manifest)."""
    out: List[FirefoxProfile] = []
    src = ctx.source
    if src.is_bundle:
        for entry in src.manifest.get("browsers") or []:
            if str(entry.get("browser") or "").lower() != "firefox" or not entry.get("path"):
                continue
            folder = src.path(str(entry["path"]))
            if folder is None or not folder.is_dir():
                continue
            prof = _firefox_profile_files(folder, str(entry.get("profile") or folder.name))
            if prof is not None:
                out.append(prof)
        return out
    home = ctx.user.profile_dir
    if home is None:
        return out
    roots: List[Path] = []
    classic = ci_path(home, ["AppData", "Roaming", "Mozilla", "Firefox"])
    if classic is not None:
        roots.append(classic)
    packages = ci_path(home, ["AppData", "Local", "Packages"])
    if packages is not None:
        try:
            for pkg in sorted(packages.iterdir()):
                if pkg.name.lower().startswith("mozilla.firefox_") and not is_link(pkg):
                    store = ci_path(pkg, ["LocalCache", "Roaming", "Mozilla", "Firefox"])
                    if store is not None:
                        roots.append(store)
        except OSError:
            pass
    seen: set = set()
    for root in roots:
        folders: List[Tuple[str, Path]] = []
        ini = ci_path(root, "profiles.ini")
        if ini is not None and ini.is_file():
            try:
                cp = _read_ini(secrets.read_bytes(ini, 1 << 20))
            except OSError:
                cp = configparser.RawConfigParser()
            for section in cp.sections():
                if not section.lower().startswith("profile"):
                    continue
                path = cp.get(section, "Path", fallback="")
                if not path:
                    continue
                name = cp.get(section, "Name", fallback=path.split("/")[-1])
                if cp.get(section, "IsRelative", fallback="1").strip() == "1":
                    folder = ci_path(root, path)
                else:
                    folder, _why = windows_path_to_local(path, ctx.drives)
                if folder is not None:
                    folders.append((name, folder))
        profs = ci_path(root, "Profiles")
        if profs is not None:
            try:
                for child in sorted(profs.iterdir()):
                    if child.is_dir() and not is_link(child) and all(f != child for _n, f in folders):
                        folders.append((child.name.split(".", 1)[-1] or child.name, child))
            except OSError:
                pass
        for name, folder in folders:
            key = os.path.normcase(str(folder))
            if key in seen:
                continue
            seen.add(key)
            prof = _firefox_profile_files(folder, name)
            if prof is not None:
                out.append(prof)
    return out


# --------------------------------------------------------------------------- #
# Linux Firefox
# --------------------------------------------------------------------------- #
def linux_firefox_root(home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    """Where Linux Firefox keeps profiles: ``~/.mozilla/firefox`` when ``~/.mozilla`` exists (or
    ``MOZ_LEGACY_HOME=1``), else ``$XDG_CONFIG_HOME/mozilla/firefox`` (Firefox 147+)."""
    env = dict(os.environ if env is None else env)
    if env.get("MOZ_LEGACY_HOME") == "1" or (home / ".mozilla").is_dir():
        return home / ".mozilla" / "firefox"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) and Path(xdg).parent == home else home / ".config"
    return base / "mozilla" / "firefox"


def _ini_sections(text: str) -> Dict[str, Dict[str, str]]:
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        cp.read_string(text)
    except configparser.Error:
        return {}
    return {s: dict(cp.items(s)) for s in cp.sections()}


def firefox_running(root: Path) -> bool:
    """True when a Linux Firefox profile under *root* is in use (its ``lock`` link exists)."""
    try:
        text = (root / "profiles.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    dirs: List[Path] = []
    for sec in _ini_sections(text).values():
        path = sec.get("Path")
        if path:
            dirs.append(root / path if sec.get("IsRelative", "1") == "1" else Path(path))
    try:
        dirs.extend(p for p in root.iterdir() if p.is_dir())
    except OSError:
        pass
    return any(os.path.lexists(d / "lock") for d in dirs)


def register_firefox_profile(root: Path, dirname: str, *, name: str = IMPORT_PROFILE_NAME) -> Tuple[str, bool]:
    """Add ``[ProfileN]`` for *dirname* to ``profiles.ini``; returns ``(name used, made default)``."""
    ini = root / "profiles.ini"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    sections = _ini_sections(text)
    indexes = {int(m.group(1)) for s in sections for m in [re.match(r"^Profile(\d+)$", s)] if m}
    names = {sec.get("Name", "") for s, sec in sections.items() if s.startswith("Profile")}
    idx = 0
    while idx in indexes:
        idx += 1
    final = name
    n = 2
    while final in names:
        final = f"{name}-{n}"
        n += 1
    make_default = not indexes
    block = [f"[Profile{idx}]", f"Name={final}", "IsRelative=1", f"Path={dirname}"]
    if make_default:
        block.append("Default=1")
    if not text.strip():
        text = "[General]\nStartWithLastProfile=1\nVersion=2\n"
    elif os.path.lexists(ini) and not os.path.lexists(root / "profiles.ini.lindos-backup"):
        (root / "profiles.ini.lindos-backup").write_text(text, encoding="utf-8")
    new_text = text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "profiles.ini.lindos-tmp"
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, ini)
    return final, make_default


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _size(paths: List[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += os.lstat(p).st_size
        except OSError:
            pass
    return total


def plan_bookmark_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import make_item

    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for prof in chromium_profiles(ctx):
        item_id = f"bookmarks:{prof.browser}:{prof.profile}"
        dest = ctx.out_dir / f"Bookmarks - {safe_filename(prof.label)} ({safe_filename(prof.profile)}).html"
        notes = [PASSWORD_NOTE]
        if prof.encrypted_only:
            notes.insert(0, ENCRYPTED_NOTE)
            skipped.append({"path": str(prof.path), "reason": "bookmarks are encrypted by Windows - use the "
                            "browser's Export bookmarks"})
        items.append(make_item(item_id, "bookmarks", f"{prof.label} bookmarks ({prof.profile})", prof.path, dest,
                               files=len(prof.files), bytes_=_size(prof.files), selected=not prof.encrypted_only,
                               notes=notes))
    fav = ctx.folders.get("favorites")
    if fav is not None and fav.path is not None:
        items.append(make_item("bookmarks:favorites", "bookmarks", "Internet Explorer favorites as bookmarks",
                               fav.path, ctx.out_dir / "Bookmarks - Internet Explorer Favorites.html",
                               files=1, notes=["Your Favorites folder is turned into a bookmarks file you can "
                                               "import into any browser."]))
    return items, skipped, []


def plan_firefox_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import make_item

    items: List[Dict[str, Any]] = []
    root = linux_firefox_root(ctx.home, dict(ctx.env))
    with_pw = bool(ctx.options.get("firefox_passwords"))
    for prof in firefox_profiles(ctx):
        files = list(prof.files) + (list(prof.password_files) if with_pw else [])
        notes = [f"A new Firefox profile '{IMPORT_PROFILE_NAME}' with your bookmarks and history is created; "
                 "a bookmarks HTML file is written too."]
        if with_pw and prof.password_files:
            notes.append("Your Firefox saved passwords are moved as they are (still protected by your Firefox "
                         "Primary Password, if you set one).")
        elif prof.password_files:
            notes.append("Saved passwords stay behind unless you choose 'Firefox passwords' (or use Firefox Sync).")
        notes.append("Add-ons are not copied: reinstall them from addons.mozilla.org (Firefox Sync can do it).")
        items.append(make_item(f"firefox:{prof.path.name}", "firefox", f"Firefox ({prof.name})", prof.path, root,
                               files=len(files), bytes_=_size(files), notes=notes))
    return items, [], []


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def _write_html(ctx: "Context", item: Dict[str, Any], roots: List[Any], dest: Path, notes: List[str]) -> ItemResult:
    stats = CopyStats()
    if not count_links(roots):
        return ItemResult.skipped_item(item, "no bookmarks found")
    data = netscape_html(roots).encode("utf-8")
    status, final = write_generated(dest, data, dry_run=ctx.dry_run)
    if status == "identical":
        stats.identical = 1
    else:
        stats.files, stats.bytes = 1, len(data)
        if status == "renamed":
            stats.renamed = 1
    notes = [f"{count_links(roots)} bookmarks"] + notes
    return ItemResult.from_stats(item, stats, dest=str(final), notes=notes)


def run_bookmarks_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    src = Path(item["src"])
    dest = Path(item["dest"])
    if item["id"] == "bookmarks:favorites":
        return _write_html(ctx, item, favorites_roots(src, engine=ctx.engine), dest, [])
    files, encrypted = _profile_files(src)
    if not files:
        return ItemResult.skipped_item(item, ENCRYPTED_NOTE if encrypted else "no bookmarks file found")
    roots: List[Any] = []
    problems: List[str] = []
    for f in files:
        try:
            part = read_chromium_file(f)
        except BookmarkError as exc:
            problems.append(str(exc))
            continue
        if f.name.lower() == "accountbookmarks":
            if part:
                roots.append(Folder("Account bookmarks", children=list(part)))
        else:
            roots.extend(part)
    res = _write_html(ctx, item, roots, dest, [PASSWORD_NOTE])
    for p in problems:
        res.errors.append({"path": str(src), "reason": p})
    if problems and res.status == "done":
        res.status = "partial"
    return res


def _new_profile_dir(root: Path) -> Path:
    for _ in range(100):
        cand = root / f"{os.urandom(4).hex()}.{IMPORT_PROFILE_NAME}"
        if not os.path.lexists(cand):
            return cand
    raise TransferError("could not create a new Firefox profile folder")


def _label_inner(label: str, fallback: str) -> str:
    if label.startswith("Firefox (") and label.endswith(")"):
        return label[len("Firefox ("):-1] or fallback
    return fallback


def run_firefox_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    src = Path(item["src"])
    root = linux_firefox_root(ctx.home, dict(ctx.env))
    prof = _firefox_profile_files(src, src.name)
    if prof is None:
        return ItemResult.skipped_item(item, "Firefox bookmarks database (places.sqlite) not found")
    with_pw = bool(ctx.options.get("firefox_passwords"))
    notes: List[str] = []
    stats = CopyStats()
    if not ctx.dry_run and firefox_running(root):
        raise TransferError("Firefox is open. Close Firefox and run the transfer again.")
    target = _new_profile_dir(root)
    if not ctx.dry_run:
        target.mkdir(parents=True, mode=0o700)
    for f in prof.files + (prof.password_files if with_pw else []):
        stats.merge(ctx.engine.copy_file(f, target / f.name.lower(), item=item["id"], rel=f.name.lower()))
    if not ctx.dry_run and (stats.errors or not (target / "places.sqlite").is_file()):
        return ItemResult.from_stats(item, stats, dest=str(target),
                                     notes=["The Firefox profile could not be completed; it was not registered."])
    used, default = IMPORT_PROFILE_NAME, False
    if not ctx.dry_run:
        used, default = register_firefox_profile(root, target.name)
    notes.append(f"New Firefox profile '{used}'" + (" (now the default profile)." if default else
                 " - open about:profiles in Firefox to use it."))
    if with_pw and prof.password_files:
        notes.append("Saved passwords moved (Firefox may ask for your Primary Password).")
    places = next(f for f in prof.files if f.name.lower() == "places.sqlite")
    try:
        roots = firefox_roots(places)
    except BookmarkError as exc:
        roots = []
        notes.append(f"Bookmarks HTML not written: {exc}")
    if roots:
        name = safe_filename(_label_inner(str(item.get("label") or ""), src.name))
        html_res = _write_html(ctx, item, roots, ctx.out_dir / f"Bookmarks - Firefox ({name}).html", [])
        stats.files += html_res.files
        stats.bytes += html_res.bytes
        stats.identical += html_res.identical
        notes.append(f"Bookmarks also saved to {html_res.dest}")
    return ItemResult.from_stats(item, stats, dest=str(target), notes=notes)
