"""Installed-programs inventory, app-map matching and install executors (SPEC-WINDOWS §29.9).

Inventory, best first: a bundle's ``apps-winget.json`` (exact winget ids, informational) and
``apps.json``; otherwise the offline ``SOFTWARE`` hive (64-bit + ``WOW6432Node``) and the user's own
``NTUSER.DAT`` ``Uninstall`` keys (:func:`lindos_transfer.winreg.uninstall_entries`, already filtered
the way "Programs and Features" is); if neither the registry nor a bundle produced anything, the
Start-Menu ``.lnk`` *names* are offered as a last resort (never their binary content).

:data:`app-map.json` (``/usr/share/lindos/transfer/app-map.json``, schema 1) maps a Windows program
name (regex, optionally narrowed by publisher) to concrete Lindos *actions*.  Action ``type``:
``builtin`` (already included in Lindos), ``apt``/``flatpak``/``browser``/``launcher`` (installed
through the root helper's ``install-packages``/``install-flatpaks``/``install-browser``/
``install-gaming``), ``recipe``/``winget`` (``lindos-compat recipes apply``/``winget install``),
``web`` (a ``.desktop`` shortcut to the site), ``winapps``/``vm`` (use ``lindos-winapps``/the Windows
VM) and ``not_possible`` (kernel-mode anti-cheat blocks Linux; points at ``lindos-game route``).
**Nothing is installed without the user selecting it** in the plan (``selected`` + ``chosen``); and,
for ``install-apps --plan <file>``, nothing is installed unless :func:`verify_apps_against_source`
also confirms the selected action still matches a fresh ``app-map.json`` match against the plan's
own live source -- a plan file loaded from disk is untrusted input, and :func:`validate_action`
alone only checks an action's *shape*, never that its content is genuine.

:func:`blocked_game` gives :mod:`lindos_transfer.steam` an honest anti-cheat check for installed
Steam titles: it reads the *shared*, read-only ``lindos-gaming`` compatibility matrix
(``/usr/share/lindos/compat-matrix.json``) by game name and only reports a game as blocked when that
matrix says so (``status == "not_possible"``) -- never a guess, and it degrades to "not blocked" (so
the game is still offered for a plain file copy) when the matrix is missing, unreadable or silent
about that title.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import TransferError, share_dir, system_path
from .copyengine import write_generated
from .sources import ci_path, is_link

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = [
    "ACTION_TYPES",
    "HELPER_ACTION_TYPES",
    "APP_MAP_FILENAME",
    "COMPAT_MATRIX_PATH",
    "STOP_WORDS",
    "PACKAGE_NAME_RE",
    "FLATPAK_ID_RE",
    "SLUG_RE",
    "AppMapError",
    "load_app_map",
    "match_app_map_entry",
    "validate_action",
    "start_menu_names",
    "inventory",
    "plan_apps",
    "blocked_game",
    "run_app_action",
    "install_apps",
    "actions_match",
    "verify_apps_against_source",
]

log = logging.getLogger("lindos-transfer.apps")

APP_MAP_FILENAME = "app-map.json"
COMPAT_MATRIX_PATH = "/usr/share/lindos/compat-matrix.json"

#: SPEC-WINDOWS §29.9 action types (binding).  Executors: apt/flatpak/browser/launcher -> the
#: privileged helper; recipe/winget -> ``lindos-compat``; web -> a ``.desktop`` shortcut;
#: builtin/winapps/vm/not_possible -> information only (nothing is run).
ACTION_TYPES: Tuple[str, ...] = ("builtin", "apt", "flatpak", "browser", "launcher", "recipe", "winget",
                                "web", "winapps", "vm", "not_possible")
#: Action types the root helper executes.
HELPER_ACTION_TYPES: Tuple[str, ...] = ("apt", "flatpak", "browser", "launcher")
#: Action types that perform no install step (text/information only).
_INFO_ACTION_TYPES: Tuple[str, ...] = ("builtin", "winapps", "vm", "not_possible")

PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
FLATPAK_ID_RE = re.compile(r"^[A-Za-z0-9]+(\.[A-Za-z0-9_-]+){2,}$")
BROWSER_IDS = ("chrome", "edge", "firefox")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_APP_MAP_BYTES = 8 << 20
MAX_COMPAT_MATRIX_BYTES = 4 << 20

#: Start-Menu shortcut names that are never a "program you use" (fallback inventory only).
STOP_WORDS = re.compile(
    r"uninstall|read\s*me|readme|release\s*notes|help\b|website|homepage|licen[cs]e|"
    r"user\s*guide|documentation|check\s*for\s*updates|support\b", re.IGNORECASE)
_START_MENU_NOISE_DIRS = frozenset({
    "startup", "windows powershell", "windows ease of access", "windows accessories",
    "windows administrative tools", "windows system", "accessibility", "maintenance",
    "administrative tools", "system tools",
})


class AppMapError(TransferError):
    """``app-map.json`` could not be read (missing, damaged or the wrong schema)."""


# --------------------------------------------------------------------------- #
# app-map.json
# --------------------------------------------------------------------------- #
def load_app_map(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load ``app-map.json`` (LINDOS_ROOT-aware).  Never raises: an empty map on any problem."""
    p = Path(path) if path is not None else (share_dir() / APP_MAP_FILENAME)
    try:
        raw = p.read_bytes()
        if len(raw) > MAX_APP_MAP_BYTES:
            raise ValueError("app-map.json is unreasonably large")
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning("cannot read %s: %s", p, exc)
        return {"schema": 1, "apps": []}
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("apps"), list):
        log.warning("%s is not a schema-1 app map", p)
        return {"schema": 1, "apps": []}
    return data


def _patterns_match(text: str, patterns: Sequence[Any]) -> bool:
    for pat in patterns:
        if not isinstance(pat, str) or not pat:
            continue
        try:
            if re.search(pat, text, re.IGNORECASE):
                return True
        except re.error:
            log.warning("bad regex in app-map.json: %r", pat)
    return False


def match_app_map_entry(app_map: Dict[str, Any], name: str, publisher: str = "") -> Optional[Dict[str, Any]]:
    """The first ``app-map.json`` entry whose ``match`` (and, if given, ``publisher``) fits."""
    name = (name or "").strip()
    if not name:
        return None
    for entry in app_map.get("apps") or []:
        if not isinstance(entry, dict):
            continue
        patterns = entry.get("match")
        if not isinstance(patterns, list) or not _patterns_match(name, patterns):
            continue
        pub_patterns = entry.get("publisher")
        if isinstance(pub_patterns, list) and pub_patterns:
            if not publisher or not _patterns_match(publisher, pub_patterns):
                continue
        return entry
    return None


# --------------------------------------------------------------------------- #
# action validation
# --------------------------------------------------------------------------- #
def validate_action(act: Any) -> None:
    """Raise :class:`ValueError` unless *act* is a well-formed plan action (SPEC-WINDOWS §29.9)."""
    if not isinstance(act, dict):
        raise ValueError("an action must be an object")
    kind = act.get("type")
    if kind not in ACTION_TYPES:
        raise ValueError(f"unknown action type {kind!r}")
    ident = act.get("id")
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError(f"a {kind!r} action needs a non-empty 'id'")
    label = act.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"a {kind!r} action needs a non-empty 'label'")
    if kind == "apt":
        pkgs = act.get("packages")
        if not isinstance(pkgs, list) or not pkgs:
            raise ValueError("an 'apt' action needs a non-empty 'packages' list")
        for pkg in pkgs:
            if not isinstance(pkg, str) or not PACKAGE_NAME_RE.match(pkg):
                raise ValueError(f"'apt' action has an invalid package name: {pkg!r}")
    elif kind == "flatpak":
        if not FLATPAK_ID_RE.match(ident):
            raise ValueError(f"'flatpak' action id must be a reverse-DNS Flatpak id, not {ident!r}")
    elif kind == "browser":
        if ident not in BROWSER_IDS:
            raise ValueError(f"'browser' action id must be one of {BROWSER_IDS}, not {ident!r}")
    elif kind == "launcher":
        if not SLUG_RE.match(ident):
            raise ValueError(f"'launcher' action id must be a lower-case slug, not {ident!r}")
    elif kind == "web":
        url = act.get("url")
        if not isinstance(url, str) or not re.match(r"^https://", url):
            raise ValueError("a 'web' action needs an https:// 'url'")
    elif kind in ("recipe", "winget"):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", ident):
            raise ValueError(f"{kind!r} action id has unexpected characters: {ident!r}")


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def _normalize_entry(name: str, *, version: str = "", publisher: str = "", install_location: str = "",
                     scope: str = "machine", arch: str = "", source: str = "registry") -> Dict[str, Any]:
    return {"name": name.strip(), "version": (version or "").strip(), "publisher": (publisher or "").strip(),
            "install_location": (install_location or "").strip(), "scope": scope or "machine",
            "arch": arch or "", "source": source}


def _bundle_inventory(ctx: "Context") -> List[Dict[str, Any]]:
    man = ctx.source.manifest
    rel = man.get("apps")
    out: List[Dict[str, Any]] = []
    if rel:
        p = ctx.source.path(str(rel))
        if p is not None and p.is_file():
            try:
                from .sources import read_json_file

                data = read_json_file(p)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                log.warning("cannot read %s: %s", p, exc)
                data = []
            for e in data if isinstance(data, list) else []:
                if isinstance(e, dict) and str(e.get("name") or "").strip():
                    out.append(_normalize_entry(str(e["name"]), version=str(e.get("version") or ""),
                                                publisher=str(e.get("publisher") or ""),
                                                install_location=str(e.get("install_location") or ""),
                                                scope=str(e.get("scope") or "machine"),
                                                arch=str(e.get("arch") or ""), source="bundle"))
    return out


def _partition_inventory(ctx: "Context") -> List[Dict[str, Any]]:
    from . import winreg

    out: List[Dict[str, Any]] = []
    software = ctx.source.hive("SOFTWARE")
    if software is not None:
        for e in winreg.uninstall_entries(software):
            out.append(_normalize_entry(e["name"], version=e["version"], publisher=e["publisher"],
                                        install_location=e["install_location"], scope=e["scope"],
                                        arch=e["arch"]))
    if ctx.ntuser is not None:
        for e in winreg.uninstall_entries(ctx.ntuser):
            out.append(_normalize_entry(e["name"], version=e["version"], publisher=e["publisher"],
                                        install_location=e["install_location"], scope=e["scope"],
                                        arch=e["arch"]))
    return out


def start_menu_names(ctx: "Context") -> List[str]:
    """Program names from Start-Menu ``.lnk`` shortcuts (names only; never opened/copied)."""
    if ctx.source.is_bundle or ctx.user.profile_dir is None:
        return []
    roots = []
    common = ctx.source.path(["ProgramData", "Microsoft", "Windows", "Start Menu", "Programs"])
    if common is not None:
        roots.append(common)
    per_user = ci_path(ctx.user.profile_dir, ["AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs"])
    if per_user is not None:
        roots.append(per_user)
    names: List[str] = []
    seen: set = set()

    def walk(folder: Path, depth: int) -> None:
        if depth > 6:
            return
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for e in entries:
            if is_link(e):
                continue
            if e.is_dir():
                if e.name.lower() not in _START_MENU_NOISE_DIRS:
                    walk(e, depth + 1)
                continue
            if e.suffix.lower() != ".lnk":
                continue
            name = e.stem.strip()
            if not name or STOP_WORDS.search(name):
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                names.append(name)

    for root in roots:
        walk(root, 0)
    return sorted(names, key=str.lower)


def inventory(ctx: "Context") -> List[Dict[str, Any]]:
    """The Windows programs found on *ctx*'s source, best source first (SPEC-WINDOWS §29.9)."""
    if ctx.source.is_bundle:
        out = _bundle_inventory(ctx)
    else:
        out = _partition_inventory(ctx)
    dedup: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for e in out:
        key = (e["name"].lower(), e["scope"], e["arch"])
        dedup.setdefault(key, e)
    result = sorted(dedup.values(), key=lambda e: e["name"].lower())
    if not result:
        result = [_normalize_entry(name, source="start-menu") for name in start_menu_names(ctx)]
    return result


# --------------------------------------------------------------------------- #
# plan integration
# --------------------------------------------------------------------------- #
def plan_apps(ctx: "Context") -> List[Dict[str, Any]]:
    """The plan's ``apps`` array (SPEC-WINDOWS §29.6): every found program, matched where possible."""
    if ctx.app_map is None:
        ctx.app_map = load_app_map()
    apps: List[Dict[str, Any]] = []
    for entry in inventory(ctx):
        match = match_app_map_entry(ctx.app_map, entry["name"], entry["publisher"])
        actions: List[Dict[str, Any]] = []
        winget_id: Optional[str] = None
        if match is not None:
            for act in match.get("actions") or []:
                try:
                    validate_action(act)
                except ValueError as exc:
                    log.warning("app-map entry %r has a bad action: %s", match.get("id"), exc)
                    continue
                actions.append(dict(act))
            wg = match.get("winget")
            if isinstance(wg, list) and wg and isinstance(wg[0], str):
                winget_id = wg[0]
        apps.append({
            "windows_name": entry["name"],
            "publisher": entry["publisher"],
            "version": entry["version"],
            "winget_id": winget_id,
            "actions": actions,
            "chosen": 0 if actions else None,
            "selected": bool(actions),
        })
    return apps


# --------------------------------------------------------------------------- #
# re-verifying a loaded plan's app actions against the live source
# --------------------------------------------------------------------------- #
#: The fields that must match, per action ``type``, for a saved action to count as the same action
#: a fresh match against the live source + ``app-map.json`` would produce.  ``validate_action`` only
#: checks *shape* (a package-name regex, a reverse-DNS Flatpak id, ...); this is the separate check
#: that the *content* itself was not edited (see :func:`verify_apps_against_source`).
_ACTION_MATCH_FIELDS: Dict[str, Tuple[str, ...]] = {
    "apt": ("id", "label", "packages"),
    "flatpak": ("id", "label"),
    "browser": ("id", "label"),
    "launcher": ("id", "label"),
    "web": ("id", "label", "url"),
    "recipe": ("id", "label"),
    "winget": ("id", "label"),
}


def actions_match(saved: Any, fresh: Any) -> bool:
    """Whether *saved* (an action from a loaded plan) is the same action as *fresh* (from a live
    re-match of that plan's source against ``app-map.json``)."""
    if not isinstance(saved, dict) or not isinstance(fresh, dict):
        return False
    kind = saved.get("type")
    if kind != fresh.get("type"):
        return False
    for key in _ACTION_MATCH_FIELDS.get(str(kind), ("id", "label")):
        sval, fval = saved.get(key), fresh.get(key)
        if key == "packages":
            sval = list(sval) if isinstance(sval, list) else sval
            fval = list(fval) if isinstance(fval, list) else fval
        if sval != fval:
            return False
    return True


def verify_apps_against_source(plan: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Re-derive *plan*'s app actions from its own live source and keep only the ones that match.

    ``install-apps --plan <file>`` must never trust a plan file's ``apps[].actions`` at face value:
    the file only has its *shape* checked by :func:`validate_plan`/:func:`validate_action` (a
    package-name regex, a reverse-DNS Flatpak id, an ``https://`` url, ...), never that the action
    actually came from matching ``app-map.json`` against software genuinely found on the plan's own
    ``source`` -- an edited or foreign plan file could otherwise make the root-privileged helper
    install arbitrary packages/Flatpaks, or write an arbitrary ``.desktop`` launcher.  This function
    re-opens ``plan['source']['root']``, re-runs :func:`plan_apps` against it exactly the way the
    plan was first built, and returns only the *selected* apps whose saved action is
    :func:`actions_match` to one of the actions a fresh match produces for that same
    ``windows_name``/``publisher``.

    Returns ``(verified_apps, notes)``: *verified_apps* is a new list of plan-shaped app entries
    (only ones worth installing), *notes* explains anything dropped.  Raises :class:`TransferError`
    when the plan's source cannot be reopened or re-planned at all (mount gone, bundle folder
    missing, the user no longer exists, ...) -- refusing to run is safer than trusting the file.
    """
    from .plan import make_context  # local import: plan.py imports this module (avoid a cycle)
    from .sources import open_source

    src_info = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    root = str((src_info or {}).get("root") or "")
    if not root:
        raise TransferError("This plan has no source recorded, so its apps cannot be re-verified.")
    try:
        source = open_source(root)
    except TransferError as exc:
        raise TransferError(f"Cannot re-verify this plan's apps against its source: {exc}") from exc
    try:
        try:
            ctx = make_context(source, user=plan.get("user"))
        except TransferError as exc:
            raise TransferError(f"Cannot re-verify this plan's apps against its source: {exc}") from exc
        try:
            fresh_apps = plan_apps(ctx)
        finally:
            ctx.close()
    finally:
        source.close()

    fresh_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {
        (str(a.get("windows_name") or ""), str(a.get("publisher") or "")): a for a in fresh_apps
    }
    notes: List[str] = []
    verified: List[Dict[str, Any]] = []
    for app in plan.get("apps") or []:
        if not isinstance(app, dict) or not app.get("selected"):
            continue
        name = str(app.get("windows_name") or "")
        key = (name, str(app.get("publisher") or ""))
        actions = app.get("actions") if isinstance(app.get("actions"), list) else []
        chosen = app.get("chosen")
        valid_index = isinstance(chosen, int) and not isinstance(chosen, bool) and 0 <= chosen < len(actions)
        fresh = fresh_by_key.get(key)
        if fresh is None or not valid_index:
            notes.append(f"'{name}' is no longer on the live source; skipping it.")
            continue
        act = actions[chosen]
        if not any(actions_match(act, fa) for fa in fresh.get("actions") or []):
            notes.append(f"'{name}': its install action does not match what app-map.json currently "
                         "matches on the live source; skipping it rather than run an unverified action.")
            continue
        verified.append(app)
    return verified, notes


# --------------------------------------------------------------------------- #
# Steam anti-cheat honesty check
# --------------------------------------------------------------------------- #
def _normalize_game_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def blocked_game(ctx: "Context", *, appid: str = "", name: str = "") -> Optional[Dict[str, Any]]:
    """``{"route","reason","anticheat"}`` from the shared compat matrix, or ``None`` (see module docstring)."""
    want = _normalize_game_name(name)
    if not want:
        return None
    path = Path(system_path(COMPAT_MATRIX_PATH))
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_COMPAT_MATRIX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for e in entries:
        if not isinstance(e, dict):
            continue
        game = str(e.get("game") or "")
        if _normalize_game_name(game) == want and str(e.get("status") or "") == "not_possible":
            return {"route": game, "reason": str(e.get("reason") or ""), "anticheat": str(e.get("anticheat") or "")}
    return None


# --------------------------------------------------------------------------- #
# executors (install-apps)
# --------------------------------------------------------------------------- #
def _run_compat(argv: List[str], *, run: Callable[..., Any]) -> Tuple[bool, str]:
    try:
        proc = run(argv, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {argv[0]}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    out = getattr(proc, "stdout", "") or ""
    err = getattr(proc, "stderr", "") or ""
    try:
        data = json.loads(out)
        msg = str(data.get("message") or ("ok" if ok else "failed"))
    except ValueError:
        tail = (out or err).strip().splitlines()
        msg = tail[-1] if tail else ("ok" if ok else "failed")
    return ok, msg


def _apt_action(act: Dict[str, Any], *, helper_mod: Any, dry_run: bool) -> Tuple[bool, str]:
    packages = list(act["packages"])
    if dry_run:
        return True, "would install: " + ", ".join(packages)
    res = helper_mod.install_packages(packages)
    return bool(res), res.message


def _flatpak_action(act: Dict[str, Any], *, helper_mod: Any, dry_run: bool) -> Tuple[bool, str]:
    if dry_run:
        return True, f"would install the Flatpak {act['id']}"
    res = helper_mod.install_flatpaks([act["id"]])
    return bool(res), res.message


def _browser_action(act: Dict[str, Any], *, helper_mod: Any, dry_run: bool) -> Tuple[bool, str]:
    if dry_run:
        return True, f"would install {act['label']}"
    res = helper_mod.install_browser(act["id"])
    return bool(res), res.message


def _launcher_action(act: Dict[str, Any], *, helper_mod: Any, dry_run: bool) -> Tuple[bool, str]:
    if dry_run:
        return True, f"would install {act['label']}"
    res = helper_mod.install_gaming([act["id"]])
    return bool(res), res.message


def _recipe_action(act: Dict[str, Any], *, run: Callable[..., Any], which: Callable[[str], Optional[str]],
                   dry_run: bool) -> Tuple[bool, str]:
    if not which("lindos-compat"):
        return False, "lindos-compat is not installed (package lindos-compat)"
    argv = ["lindos-compat", "recipes", "apply", act["id"], "--json"]
    if dry_run:
        argv.append("--dry-run")
    return _run_compat(argv, run=run)


def _winget_action(act: Dict[str, Any], *, run: Callable[..., Any], which: Callable[[str], Optional[str]],
                   dry_run: bool) -> Tuple[bool, str]:
    if not which("lindos-compat"):
        return False, "lindos-compat is not installed (package lindos-compat)"
    argv = ["lindos-compat", "winget", "install", act["id"], "--accept-package-agreements", "--json"]
    if dry_run:
        argv.append("--dry-run")
    return _run_compat(argv, run=run)


def _web_action(act: Dict[str, Any], *, home: Path, dry_run: bool) -> Tuple[bool, str]:
    label = act.get("label") or act["id"]
    url = act.get("url", "")
    if dry_run:
        return True, f"would add '{label}' to your applications menu"
    from .browsers import safe_filename

    dest = home / ".local" / "share" / "applications" / f"lindos-webapp-{safe_filename(act['id'])}.desktop"
    content = ("[Desktop Entry]\nVersion=1.0\nType=Application\nName=" + label.replace("\n", " ")
              + "\nExec=xdg-open " + shlex.quote(url) + "\nIcon=web-browser\nCategories=Network;\nTerminal=false\n")
    write_generated(dest, content.encode("utf-8"))
    return True, f"Added '{label}' to your applications menu."


_INFO_MESSAGES = {
    "builtin": "Already included in Lindos.",
    "winapps": "Use lindos-winapps to run this from your own licensed Windows PC over Remote Desktop.",
    "vm": "Use the Lindos Windows VM (lindos-vm) to run this.",
}


def _info_action(act: Dict[str, Any]) -> Tuple[bool, str]:
    if act["type"] == "not_possible":
        return True, str(act.get("note") or "This does not run on Lindos (see the report for why).")
    return True, _INFO_MESSAGES.get(act["type"], act.get("label", ""))


def run_app_action(act: Dict[str, Any], *, run: Callable[..., Any], which: Callable[[str], Optional[str]],
                   home: Path, helper_mod: Any, dry_run: bool) -> Tuple[bool, str]:
    """Perform one plan action.  Raises :class:`ValueError` for a malformed action (caller's bug)."""
    validate_action(act)
    kind = act["type"]
    if kind in HELPER_ACTION_TYPES and helper_mod is None:
        return False, "lindos-core is not installed (it is needed to install apps)"
    if kind == "apt":
        return _apt_action(act, helper_mod=helper_mod, dry_run=dry_run)
    if kind == "flatpak":
        return _flatpak_action(act, helper_mod=helper_mod, dry_run=dry_run)
    if kind == "browser":
        return _browser_action(act, helper_mod=helper_mod, dry_run=dry_run)
    if kind == "launcher":
        return _launcher_action(act, helper_mod=helper_mod, dry_run=dry_run)
    if kind == "recipe":
        return _recipe_action(act, run=run, which=which, dry_run=dry_run)
    if kind == "winget":
        return _winget_action(act, run=run, which=which, dry_run=dry_run)
    if kind == "web":
        return _web_action(act, home=home, dry_run=dry_run)
    return _info_action(act)


def _default_helper() -> Any:
    try:
        from lindos import helper as helper_mod  # type: ignore[import-not-found]
    except ImportError:
        return None
    return helper_mod


def install_apps(plan: Dict[str, Any], *, home: Path, run: Callable[..., Any] = subprocess.run,
                 which: Callable[[str], Optional[str]] = None, helper_mod: Any = None, dry_run: bool = False,
                 emit: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
    """Run every *selected* app action of *plan* (SPEC-WINDOWS §29.2 ``install-apps``).

    Nothing runs for an app whose ``selected`` is false or whose ``chosen`` is not a valid index
    into its ``actions`` -- the user's choice in the plan is the only thing that installs anything.
    """
    import shutil as _shutil

    which = which or _shutil.which
    if helper_mod is None:
        helper_mod = _default_helper()
    results: List[Dict[str, Any]] = []
    for app in plan.get("apps") or []:
        if not app.get("selected"):
            continue
        actions = app.get("actions") or []
        chosen = app.get("chosen")
        if not isinstance(chosen, int) or isinstance(chosen, bool) or not (0 <= chosen < len(actions)):
            continue
        act = actions[chosen]
        name = str(app.get("windows_name") or "")
        if emit is not None:
            emit({"event": "item", "app": name, "message": act.get("label", "")})
        try:
            ok, message = run_app_action(act, run=run, which=which, home=home, helper_mod=helper_mod,
                                         dry_run=dry_run)
        except ValueError as exc:
            ok, message = False, f"invalid action: {exc}"
        except Exception as exc:  # noqa: BLE001 - one broken app must not stop the others
            log.exception("installing %r failed", name)
            ok, message = False, f"unexpected problem: {exc}"
        status = "dry-run" if dry_run and ok else ("done" if ok else "failed")
        result = {"windows_name": name, "type": act.get("type", ""), "label": act.get("label", ""),
                  "status": status, "message": message}
        results.append(result)
        if emit is not None:
            emit({"event": "item-done", "app": name, "status": status, "message": message})
    return results
