"""``lindos-run`` -- run a Windows program (``.exe`` / ``.msi`` / ``.bat`` / ``.lnk``).

Flow (SPEC §9):

1. analyse the file (``lindos.compat.analyze_exe``) and choose the runner
   (umu / wine / bottles);
2. prepare the program's own C:\\ drive (Wine prefix) under ``PREFIXES_DIR``;
3. run it (output → ``~/.local/state/lindos/run-<slug>.log``);
4. after an installer: find what was installed (Start Menu shortcuts, Program
   Files) and create Start Menu entries + apps-database records;
5. desktop feedback (zenity/yad/notify-send) when started from the file manager.

Exit codes: 0 ok · 1 error · 2 usage.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import CoreMissing, __version__, core, get_logger, load_user_config
from .gui import Feedback, gui_wanted
from .icons import GENERIC_ICON, install_app_icon
from .perf import GamescopeSpec, parse_geometry, perf_env
from .profiles import Profile, gamescope_from_profile, resolve_profile
from .lnk import LnkError, LnkInfo, is_windows_path, parse_lnk, unix_to_windows, windows_to_unix
from .prefix import (
    SHARED_SLUG,
    PrefixState,
    derive_slug,
    ensure_prefix,
    find_prefix_for_path,
    link_windows_apps,
    list_prefixes,
    prefix_path,
    read_marker,
    safe_slug,
)
from .recipes import Recipe, get_recipe
from .runner import RUNNERS, RunPlan, bottle_prefix_path, build_plan, choose_runner, ensure_bottle, load_prefix_settings, log_path_for, run_plan
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

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "SUPPORTED_SUFFIXES", "build_parser", "main",
           "split_windows_args", "info_to_dict", "analyze_file", "resolve_shortcut", "pick_slug",
           "gamescope_spec", "resolved_perf_env"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

SUPPORTED_SUFFIXES = (".exe", ".msi", ".bat", ".cmd", ".lnk")
INSTALL_HINT = ("Install Windows program support first: run  pkexec /usr/libexec/lindos/install-compat.sh  "
                "(or open Lindos Settings > Windows apps > Doctor for the exact fix commands)")

log = get_logger("lindos-run")

_WIN_ARG_RE = re.compile(r'"[^"]*"|\S+')
_TEMP_DIR_RE = re.compile(r"(^|/)(tmp|temp|\.cache|Temp)(/|$)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lindos-run",
        description="Run a Windows program (.exe, .msi, .bat or .lnk shortcut) on Lindos through "
                    "Wine or Proton (no virtual machine).",
        epilog="Examples:\n"
               "  lindos-run ~/Downloads/npp.8.6.Installer.x64.exe\n"
               "  lindos-run --prefix photoshop-cc-2021 ~/Downloads/Set-up.exe\n"
               "  lindos-run --runner umu --gamemode ~/Games/game.exe\n"
               "  lindos-run --info setup.exe\n"
               "Program arguments go after the file (use -- if they start with a dash):\n"
               "  lindos-run installer.exe /S\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", help="the Windows program: .exe, .msi, .bat or a .lnk shortcut")
    p.add_argument("args", nargs="*", help="arguments passed to the Windows program")
    p.add_argument("--runner", choices=RUNNERS, metavar="umu|wine|bottles",
                   help="force a runner (default: games/unknown -> umu (Proton), installers/apps -> wine)")
    p.add_argument("--prefix", metavar="NAME",
                   help="use the C:\\ drive (Wine prefix) called NAME instead of the program's own one")
    p.add_argument("--new-prefix", action="store_true",
                   help="start from a fresh C:\\ drive (the old one is moved aside, not deleted)")
    p.add_argument("--shared", action="store_true",
                   help="use the shared 'default' C:\\ drive (fine for small utilities)")
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
                   help="only print what Lindos knows about the file (JSON) - nothing is run")
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


# ---------------------------------------------------------------------------
# registration helpers
# ---------------------------------------------------------------------------


def _register_found(app: FoundApp, *, prefix_slug: str, runner: str, kind: str = "app") -> Optional[str]:
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
    icon, icon_file = install_app_icon(app.icon_source or app.exe, app_slug)
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
        desktop = write_desktop_file(app_slug, name=app.name, exe=app.exe, prefix_slug=prefix_slug, runner=runner,
                                     icon=icon, workdir=workdir)
    except OSError as exc:
        log.warning("could not write the Start Menu entry for %s: %s", app.name, exc)
        desktop = None
    try:
        register_app(app, prefix_slug=prefix_slug, runner=runner, kind=kind, icon=icon, icon_file=icon_file,
                     desktop_file=desktop, db=db, save=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save the apps database: %s", exc)
    log.info("Start Menu entry created: %s (%s)", app.name, desktop or "no .desktop file")
    return app_slug


def _refresh_desktop_database() -> None:
    tool = shutil.which("update-desktop-database")
    if not tool:
        return
    try:
        subprocess.run([tool, "-q", str(applications_dir())], check=False, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


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


def _main(ns: argparse.Namespace, prog_args: List[str]) -> int:  # noqa: C901 - the launch flow is linear
    fb = Feedback(gui_wanted() and not (ns.info or ns.dry_run))

    def fail(msg: str, *, code: int = EXIT_ERROR) -> int:
        log.error("%s", msg)
        fb.error(msg)
        return code

    # -- 1. the file ---------------------------------------------------------
    raw = Path(os.path.expanduser(str(ns.file)))
    if not raw.exists():
        return fail(f"File not found: {raw}", code=EXIT_USAGE)
    try:
        path = raw.resolve()
    except OSError:
        path = raw.absolute()
    suffix = path.suffix.lower()

    lnk_info: Optional[LnkInfo] = None
    if suffix == ".lnk":
        target, lnk_info, slug_hint, err = resolve_shortcut(path, ns.prefix)
        if target is None:
            return fail(err or f"Cannot resolve the shortcut {path.name}")
        log.info("Shortcut '%s' -> %s", path.name, target)
        path = target
        suffix = path.suffix.lower()
        if lnk_info is not None and lnk_info.arguments and not prog_args:
            prog_args = split_windows_args(lnk_info.arguments)
        if slug_hint and not ns.prefix:
            ns.prefix = slug_hint
    if suffix not in SUPPORTED_SUFFIXES:
        log.warning("'%s' is not a typical Windows program file (.exe/.msi/.bat) - trying anyway", path.name)
    if path.is_dir():
        return fail(f"{path} is a folder, not a Windows program.", code=EXIT_USAGE)

    # -- 2. analyse ---------------------------------------------------------------
    try:
        info = analyze_file(path)
    except CoreMissing as exc:
        return fail(str(exc))
    kind = str(getattr(info, "kind", "unknown") or "unknown")
    display_name = _display_name(info, path)
    config = load_user_config()
    registered = find_registered(path)
    if registered and not prog_args and registered.get("args"):
        prog_args = split_windows_args(str(registered["args"]))

    slug = pick_slug(path, info, prefix_opt=ns.prefix, shared=ns.shared, registered=registered)
    pfx_path = prefix_path(slug)
    marker = read_marker(pfx_path) if not ns.new_prefix else {}
    recipe: Optional[Recipe] = None
    recipe_id = str(marker.get("recipe") or "") if marker else ""
    if recipe_id:
        recipe = get_recipe(recipe_id)
    if recipe is None and slug != SHARED_SLUG:
        recipe = get_recipe(slug)  # `--prefix photoshop-cc-2021` picks the recipe of the same name

    # -- per-title profile (SPEC-KERNEL §17.3): match by exe name / Steam appid --------
    profile: Optional[Profile] = resolve_profile(exe=path.name, appid=ns.appid)
    requested_runner = ns.runner
    if profile is not None and profile.runner and not requested_runner and profile.possible:
        requested_runner = profile.runner

    runner, reason = choose_runner(info, config, requested=requested_runner, recipe=recipe,
                                   prefix_runner=str(marker.get("runner")) if marker.get("runner") else None)
    if ns.runner and runner != ns.runner:
        log.warning("Runner '%s' is not available: %s", ns.runner, reason)

    # effective toggles (command-line flag wins over the profile)
    dxvk_async = ns.dxvk_async if ns.dxvk_async is not None else (profile.dxvk_async if profile else None)
    gs_spec = gamescope_spec(ns, profile)
    mangohud_flag = bool(ns.mangohud or (profile is not None and profile.mangohud))

    if ns.info:
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
            "shortcut": lnk_info.as_dict() if lnk_info else None,
            "log": str(log_path_for(slug)),
        })
        _print_json(data)
        return EXIT_OK

    if profile is not None and not profile.possible:
        note = profile.notes or "this title uses kernel-level anti-cheat that does not run on Linux"
        return fail(f"'{profile.title}' cannot run on Lindos: {note}\n"
                    "Lindos does not fake anti-cheat/attestation - doing so only gets you hardware-banned. "
                    "See docs/ANTI-CHEAT.md.")

    # -- 3. plan --------------------------------------------------------------------
    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    extra_env, overrides = load_prefix_settings(slug)
    if profile is not None:
        extra_env = {**_profile_env(profile), **extra_env}
    if recipe is not None:
        extra_env = {**recipe.env, **extra_env}
        overrides = {**recipe.dll_overrides, **overrides}
    arch = str(marker.get("arch") or (recipe.arch if recipe else "win64"))
    workdir = str(registered.get("workdir") or "") if registered else (lnk_info.working_dir if lnk_info else "")
    try:
        plan: RunPlan = build_plan(runner=runner, slug=slug, exe=path, kind=kind, config=config, args=prog_args,
                                   gamemode_flag=ns.gamemode, mangohud_flag=mangohud_flag, headless=headless,
                                   extra_env=extra_env, dll_overrides=overrides, arch=arch, reason=reason,
                                   dxvk_async=dxvk_async, hdr=gs_spec.hdr, gamescope=gs_spec)
    except FileNotFoundError as exc:
        what = {"umu": "umu-launcher (umu-run)", "bottles": "Bottles (flatpak)", "wine": "Wine"}.get(runner, runner)
        return fail(f"{what} is not installed, so '{path.name}' cannot be started ({exc}).\n{INSTALL_HINT}")
    if workdir:
        wd = windows_to_unix(workdir, pfx_path, must_exist=True) if is_windows_path(workdir) else Path(workdir)
        if wd is not None and wd.is_dir():
            plan.cwd = str(wd)
    for w in plan.warnings:
        log.warning("%s", w)

    if ns.dry_run:
        out = plan.as_dict()
        out["info"] = info_to_dict(info)
        out["recipe"] = recipe.id if recipe else None
        _print_json(out)
        return EXIT_OK

    # -- 4. C:\ drive -------------------------------------------------------------
    fb.progress("Preparing Windows compatibility\u2026")
    log.info("Runner: %s (%s); C:\\ drive: %s", runner, reason, pfx_path)
    if runner == "bottles":
        if not ensure_bottle(slug, environment="gaming" if kind == "game" else "application", log_file=plan.log_path):
            return fail(f"Bottles could not create the bottle '{slug}'. Log: {plan.log_path}")
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
    deep = kind in ("installer", "msi", "unknown")
    before = snapshot(scan_prefix, deep=deep)

    # -- 5. run -----------------------------------------------------------------------
    fb.close_progress()
    log.info("Starting %s (%s) - log: %s", path.name, kind, plan.log_path)
    started = time.time()
    rc = run_plan(plan)
    log.info("'%s' exited with code %s after %.0f s", path.name, rc, time.time() - started)

    # -- 6. what got installed? -----------------------------------------------------
    after = snapshot(scan_prefix, deep=deep)
    new_files = diff_snapshots(before, after)
    apps = discover_apps(new_files, scan_prefix, product_hint=str(getattr(info, "product", "") or path.stem))
    created: List[str] = []
    for app in apps:
        app_slug = _register_found(app, prefix_slug=slug, runner=runner, kind="app")
        if app_slug:
            created.append(app.name)
    if not apps and kind in ("app", "game") and suffix == ".exe" and not registered \
            and not SKIP_NAME_RE.search(path.stem) and not _TEMP_DIR_RE.search(str(path.parent)):
        direct = FoundApp(name=display_name, exe=path, source=str(path), icon_source=path,
                          win_exe=unix_to_windows(path, scan_prefix) or "")
        if _register_found(direct, prefix_slug=slug, runner=runner, kind=kind):
            log.info("'%s' was added to the Start Menu and to Lindos Settings > Windows apps", display_name)
    if created or apps or kind in ("app", "game"):
        _refresh_desktop_database()
    link_windows_apps(slug, created[0] if len(created) == 1 else display_name)

    # -- 7. feedback ------------------------------------------------------------------
    if created:
        names = ", ".join(created[:4]) + (" \u2026" if len(created) > 4 else "")
        fb.notify("Windows program installed", f"{names} - now in the Start Menu (Wine / Windows apps)")
        print(f"Installed: {names}\nStart Menu entries were created; also listed in Lindos Settings > Windows apps.")
    if rc != 0 and not created:
        msg = (f"'{path.name}' ended with exit code {rc}.\n"
               f"If it did not work, the log is here (copy it into a bug report):\n{plan.log_path}")
        if rc == 127:
            msg = f"'{path.name}' could not be started.\n{INSTALL_HINT}\nLog: {plan.log_path}"
        log.error("%s", msg.replace("\n", " "))
        fb.error(msg)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
