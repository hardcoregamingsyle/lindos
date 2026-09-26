"""``lindos-compat`` -- manage the Windows-app compatibility layer (SPEC §9).

Sub-commands::

    lindos-compat doctor [--json]
    lindos-compat prefixes list [--json] [--size]
    lindos-compat prefixes remove <slug> [--yes] [--keep-apps]
    lindos-compat prefixes open <slug|file>
    lindos-compat prefixes winecfg <slug>
    lindos-compat recipes list [--json] [--status works|partial|broken]
    lindos-compat recipes show <id> [--json]
    lindos-compat recipes apply <id> [--prefix SLUG] [--fresh] [--dry-run] [--json]
    lindos-compat install-umu [--system] [--version TAG] [--no-verify] [--force] [--dry-run] [--json]
    lindos-compat install-bottles [--system|--user] [--dry-run] [--json]
    lindos-compat install-dxvk  <slug> [--tag T] [--arch win64|win32] [--uninstall] [--no-verify] [--dry-run] [--json]
    lindos-compat install-vkd3d <slug> [--tag T] [--arch win64|win32] [--uninstall] [--no-verify] [--dry-run] [--json]
    lindos-compat proton list|update|remove ...      (delegates to lindos-proton)
    lindos-compat formats [--json]                              (SPEC-WINDOWS §28.3)
    lindos-compat binfmt status|enable|disable [--json]         (§28.7; enable/disable via the helper)
    lindos-compat winget search <query> [--json] [--limit N]    (§28.10)
    lindos-compat winget show <PackageIdentifier> [--version V] [--json]
    lindos-compat winget install <PackageIdentifier> [--version V] [--arch x64|x86] [--prefix NAME]
                                 [--interactive] [--accept-package-agreements] [--dry-run] [--json]
    lindos-compat winget list [--json]
    lindos-compat winget update-index [--json]

Exit codes: 0 ok · 1 error · 2 usage · 3 unsupported, explained (a Store id, a web app, an http://
download; or the helper cannot change terminal .exe support on this system).
"""

from __future__ import annotations

import argparse
import importlib
import json
import shlex
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__, get_logger
from .doctor import format_report, run_doctor
from .installers import UMU_VERSION, install_bottles, install_dxvk, install_umu, install_vkd3d
from .prefix import list_prefixes, open_prefix, prefix_path, remove_prefix, run_winecfg
from .recipes import STATUSES, STATUS_LABEL, STATUS_MARK, apply_recipe, format_recipe, get_recipe, load_recipes
from . import winget

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "EXIT_UNSUPPORTED", "build_parser", "main", "helper_command",
           "winget_show_data"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3

#: Canonical path of the privileged helper, as printed in "run this yourself" hints.
HELPER_PATH = "/usr/libexec/lindos/lindos-helper"

log = get_logger("lindos-compat")


OK_MARK = "✓"
BAD_MARK = "✗"


def _mark(ok: bool) -> str:
    return OK_MARK if ok else BAD_MARK


def _print_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lindos-compat",
        description="Windows program support on Lindos: health check, C:\\ drives (Wine prefixes), "
                    "recipes for well-known programs, umu-launcher / Bottles / Proton-GE installers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"lindos-compat {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug output")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    d = sub.add_parser("doctor", help="check that this PC can run Windows programs (prints fix commands)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--ascii", action="store_true", help="use OK/X instead of \u2713/\u2717")

    pr = sub.add_parser("prefixes", help="manage C:\\ drives (Wine prefixes)")
    prs = pr.add_subparsers(dest="sub", metavar="ACTION")
    a = prs.add_parser("list", help="list C:\\ drives and the programs in them")
    a.add_argument("--json", action="store_true")
    a.add_argument("--size", action="store_true", help="also compute disk usage (slower)")
    a = prs.add_parser("remove", help="delete a C:\\ drive and its Start Menu entries")
    a.add_argument("slug")
    a.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    a.add_argument("--keep-apps", action="store_true", help="keep the Start Menu entries / apps-database records")
    a = prs.add_parser("open", help="open the C:\\ drive in the file manager (accepts a slug or a program path)")
    a.add_argument("slug")
    a = prs.add_parser("winecfg", help="open Wine's configuration for a C:\\ drive")
    a.add_argument("slug")

    rc = sub.add_parser("recipes", help="ready-made setups for well-known Windows programs")
    rcs = rc.add_subparsers(dest="sub", metavar="ACTION")
    a = rcs.add_parser("list", help="list recipes with their honest status")
    a.add_argument("--json", action="store_true")
    a.add_argument("--status", choices=STATUSES)
    a = rcs.add_parser("show", help="show one recipe")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a = rcs.add_parser("apply", help="prepare a C:\\ drive for a program (winetricks, DLL overrides, registry)")
    a.add_argument("id")
    a.add_argument("--prefix", metavar="SLUG", help="C:\\ drive to prepare (default: the recipe id)")
    a.add_argument("--fresh", action="store_true", help="start from an empty C:\\ drive (old one moved aside)")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--json", action="store_true")

    u = sub.add_parser("install-umu", help="install umu-launcher (Proton runner for games)")
    u.add_argument("--system", action="store_true", help="install to /usr/local/bin (asks for admin rights)")
    u.add_argument("--version", dest="umu_version", default=UMU_VERSION, metavar="TAG",
                   help=f"release tag to install (default {UMU_VERSION}; 'latest' asks GitHub)")
    u.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check")
    u.add_argument("--force", action="store_true", help="reinstall even if umu-run exists")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--json", action="store_true")

    b = sub.add_parser("install-bottles", help="install Bottles from Flathub (flatpak)")
    scope = b.add_mutually_exclusive_group()
    scope.add_argument("--system", action="store_true", help="system-wide only")
    scope.add_argument("--user", action="store_true", help="per-user only")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--json", action="store_true")

    for comp, label in (("install-dxvk", "DXVK"), ("install-vkd3d", "VKD3D-Proton")):
        c = sub.add_parser(comp, help=f"install/refresh {label} into a Wine C:\\ drive (pinned release)")
        c.add_argument("prefix", metavar="SLUG", help="the C:\\ drive (Wine prefix) to install into")
        c.add_argument("--tag", help="release tag to install (default: the pin in components.json)")
        c.add_argument("--url", help="tarball URL (overrides the pinned repo/asset)")
        c.add_argument("--arch", choices=("win64", "win32"), default="win64")
        c.add_argument("--uninstall", action="store_true", help="remove it and restore Wine's built-ins")
        c.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check")
        c.add_argument("--dry-run", action="store_true")
        c.add_argument("--json", action="store_true")

    pt = sub.add_parser("proton", help="Proton-GE builds: list | update | remove <tag> (via lindos-proton)",
                        add_help=False)
    pt.add_argument("proton_args", nargs=argparse.REMAINDER)

    fm = sub.add_parser("formats", help="the Windows file types Lindos opens, and how well each works")
    fm.add_argument("--json", action="store_true")

    bf = sub.add_parser("binfmt", help="run .exe files straight from a terminal (./setup.exe): "
                                       "status | enable | disable")
    bfs = bf.add_subparsers(dest="sub", metavar="ACTION")
    for action, text in (("status", "is ./program.exe in a terminal handled by Lindos?"),
                         ("enable", "turn it on (asks for your password)"),
                         ("disable", "turn it off (asks for your password)")):
        a = bfs.add_parser(action, help=text)
        a.add_argument("--json", action="store_true")

    wg = sub.add_parser("winget", help="find and install Windows programs from Microsoft's winget catalogue "
                                       "(every download is checked against its SHA-256)")
    wgs = wg.add_subparsers(dest="sub", metavar="ACTION")
    a = wgs.add_parser("search", help="search the catalogue by name, id, command or tag")
    a.add_argument("query", nargs="+")
    a.add_argument("--limit", type=int, default=20, metavar="N", help="at most N results (default 20)")
    a.add_argument("--json", action="store_true")
    a = wgs.add_parser("show", help="details of one package (licence, installers, which one Lindos would use)")
    a.add_argument("id", metavar="PackageIdentifier")
    a.add_argument("--version", dest="pkg_version", metavar="V", help="a specific version (default: newest)")
    a.add_argument("--json", action="store_true")
    a = wgs.add_parser("install", help="download (hash-checked) and install a package into a C:\\ drive")
    a.add_argument("id", metavar="PackageIdentifier")
    a.add_argument("--version", dest="pkg_version", metavar="V", help="a specific version (default: newest)")
    a.add_argument("--arch", choices=("x64", "x86"), help="installer architecture (default: the best available)")
    a.add_argument("--prefix", metavar="NAME", help="the C:\\ drive to install into (default: the program's own)")
    a.add_argument("--interactive", action="store_true",
                   help="show the installer's own windows instead of installing silently")
    a.add_argument("--accept-package-agreements", action="store_true",
                   help="agree to the package's licence terms without being asked")
    a.add_argument("--dry-run", action="store_true", help="show what would happen; download and run nothing")
    a.add_argument("--json", action="store_true")
    a = wgs.add_parser("list", help="packages installed with 'lindos-compat winget install'")
    a.add_argument("--json", action="store_true")
    a = wgs.add_parser("update-index", help="refresh the catalogue index now")
    a.add_argument("--json", action="store_true")
    return p


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_doctor(ns: argparse.Namespace) -> int:
    rep = run_doctor()
    if ns.json:
        _print_json(rep.as_dict())
    else:
        print(format_report(rep, use_unicode=not ns.ascii))
    return EXIT_OK if rep.ok else EXIT_ERROR


def cmd_prefixes(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if ns.sub == "list":
        items = list_prefixes(with_size=ns.size)
        if ns.json:
            _print_json(items)
            return EXIT_OK
        if not items:
            print("No C:\\ drives yet. Run a Windows program (double-click an .exe) to create one.")
            return EXIT_OK
        header = "C:\\ drive"
        print(f"{header:<28} {'runner':<8} {'arch':<6} {'programs':<30} location")
        for it in items:
            apps = ", ".join(str(a) for a in it.get("apps", [])) or "-"
            extra = f" ({it['size_mb']} MB)" if "size_mb" in it else ""
            print(f"{str(it['slug']):<28} {str(it['runner']):<8} {str(it['arch']):<6} {apps[:30]:<30} {it['path']}{extra}")
        return EXIT_OK
    if ns.sub == "remove":
        target = prefix_path(ns.slug)
        if not target.exists():
            log.error("No C:\\ drive named '%s' (%s)", ns.slug, target)
            return EXIT_ERROR
        if not ns.yes and sys.stdin.isatty():
            try:
                answer = input(f"Delete the whole C:\\ drive '{ns.slug}' ({target}) and its Start Menu entries? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return EXIT_ERROR
        return EXIT_OK if remove_prefix(ns.slug, keep_apps=ns.keep_apps) else EXIT_ERROR
    if ns.sub == "open":
        return EXIT_OK if open_prefix(ns.slug) else EXIT_ERROR
    if ns.sub == "winecfg":
        return EXIT_OK if run_winecfg(ns.slug) == 0 else EXIT_ERROR
    parser.parse_args(["prefixes", "--help"])
    return EXIT_USAGE


def cmd_recipes(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if ns.sub == "list":
        recipes = load_recipes()
        items = [r for r in recipes.values() if not ns.status or r.status == ns.status]
        if ns.json:
            _print_json([r.as_dict() for r in items])
            return EXIT_OK
        if not items:
            print("No recipes found.")
            return EXIT_OK
        width = max(len(r.id) for r in items)
        for r in sorted(items, key=lambda x: (STATUSES.index(x.status), x.name.lower())):
            alt = ""
            if r.status == "broken" and r.alternatives:
                alt = "  -> " + "; ".join(a.get("name", "") for a in r.alternatives[:3])
            print(f" {STATUS_MARK.get(r.status, '?')} {r.id.ljust(width)}  {r.name} ({r.vendor}) - {STATUS_LABEL.get(r.status, r.status)}{alt}")
        print("\nDetails: lindos-compat recipes show <id>   |   Prepare: lindos-compat recipes apply <id>")
        return EXIT_OK
    if ns.sub == "show":
        r = get_recipe(ns.id)
        if r is None:
            log.error("Unknown recipe '%s' (see: lindos-compat recipes list)", ns.id)
            return EXIT_ERROR
        if ns.json:
            _print_json(r.as_dict())
        else:
            print(format_recipe(r))
        return EXIT_OK
    if ns.sub == "apply":
        r = get_recipe(ns.id)
        if r is None:
            log.error("Unknown recipe '%s' (see: lindos-compat recipes list)", ns.id)
            return EXIT_ERROR

        def progress(text: str) -> None:
            if not ns.json:
                print(f"  ... {text}", flush=True)

        if not ns.json:
            print(format_recipe(r, verbose=False))
            print()
        res = apply_recipe(r, prefix_slug=ns.prefix, fresh=ns.fresh, dry_run=ns.dry_run, on_progress=progress)
        if ns.json:
            _print_json(res.as_dict())
        else:
            for name, ok, detail in res.steps:
                print(f" {_mark(ok)} {name}: {detail}")
            print()
            print(res.message)
            if r.status == "partial" and res.ok:
                print("\nNote (partial support): " + r.notes.splitlines()[0])
        return EXIT_OK if res.ok else EXIT_ERROR
    parser.parse_args(["recipes", "--help"])
    return EXIT_USAGE


def cmd_install_umu(ns: argparse.Namespace) -> int:
    def progress(text: str) -> None:
        if not ns.json:
            print(f"  ... {text}", flush=True)

    res = install_umu(system=ns.system, version=ns.umu_version, verify=not ns.no_verify, force=ns.force,
                      on_progress=progress, dry_run=ns.dry_run)
    if ns.json:
        _print_json(res.as_dict())
    else:
        for name, ok, detail in res.steps:
            print(f" {_mark(ok)} {name}: {detail}")
        print(res.message)
    return EXIT_OK if res.ok else EXIT_ERROR


def cmd_install_bottles(ns: argparse.Namespace) -> int:
    def progress(text: str) -> None:
        if not ns.json:
            print(f"  ... {text}", flush=True)

    system: Optional[bool] = True if ns.system else (False if ns.user else None)
    res = install_bottles(system=system, on_progress=progress, dry_run=ns.dry_run)
    if ns.json:
        _print_json(res.as_dict())
    else:
        for name, ok, detail in res.steps:
            print(f" {_mark(ok)} {name}: {detail}")
        print(res.message)
    return EXIT_OK if res.ok else EXIT_ERROR


def cmd_install_component(ns: argparse.Namespace, component: str) -> int:
    def progress(text: str) -> None:
        if not ns.json:
            print(f"  ... {text}", flush=True)

    target = prefix_path(ns.prefix)
    if not (target / "drive_c").is_dir() and not (target / "system.reg").exists() and not ns.dry_run:
        log.error("No Wine C:\\ drive named '%s' (%s). Run the program once first, or: lindos-compat prefixes list",
                  ns.prefix, target)
        return EXIT_ERROR
    installer = install_dxvk if component == "dxvk" else install_vkd3d
    res = installer(target, tag=ns.tag, url=ns.url, arch=ns.arch, uninstall=ns.uninstall,
                    verify=not ns.no_verify, dry_run=ns.dry_run, on_progress=progress)
    if ns.json:
        _print_json(res.as_dict())
    else:
        for name, ok, detail in res.steps:
            print(f" {_mark(ok)} {name}: {detail}")
        print(res.message)
    return EXIT_OK if res.ok else EXIT_ERROR


def cmd_proton(ns: argparse.Namespace) -> int:
    args: List[str] = list(ns.proton_args or [])
    if args[:1] == ["--"]:
        args = args[1:]
    tool = shutil.which("lindos-proton")
    if not tool:
        log.error("lindos-proton is not installed (package lindos-gaming). Proton-GE builds are managed there:  "
                  "apt install lindos-gaming")
        return EXIT_ERROR
    try:
        rc = subprocess.call([tool] + args)
    except OSError as exc:
        log.error("cannot run lindos-proton: %s", exc)
        return EXIT_ERROR
    return rc if rc in (EXIT_OK, EXIT_ERROR, EXIT_USAGE) else EXIT_ERROR


# ---------------------------------------------------------------------------
# formats / binfmt (SPEC-WINDOWS §28.3, §28.7)
# ---------------------------------------------------------------------------

_FORMAT_MARKS = {"works": OK_MARK, "partial": "~", "unsupported": BAD_MARK}
_FORMAT_ORDER = ("works", "partial", "unsupported")


def _sibling(name: str) -> ModuleType:
    """Import ``lindos_compat.<name>`` lazily (a missing optional module only breaks its own command)."""
    return importlib.import_module(f".{name}", __package__)


def _suffix_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def cmd_formats(ns: argparse.Namespace) -> int:
    try:
        rows = [dict(r) for r in _sibling("formats").formats_table()]
    except ImportError as exc:
        log.error("The Windows file-type table is not available (%s). Reinstall lindos-compat.", exc)
        return EXIT_ERROR
    if ns.json:
        _print_json(rows)
        return EXIT_OK
    print(f"Windows file types Lindos opens ({OK_MARK} works, ~ partly, {BAD_MARK} not possible - with the reason):")
    width = max([len(str(r.get("label", r.get("id", "")))) for r in rows] + [10])
    ext_width = min(max([len(_suffix_text(r.get("suffixes"))) for r in rows] + [10]), 32)

    def rank(row: Dict[str, object]) -> int:
        status = str(row.get("status", ""))
        return _FORMAT_ORDER.index(status) if status in _FORMAT_ORDER else len(_FORMAT_ORDER)

    for row in sorted(rows, key=rank):
        mark = _FORMAT_MARKS.get(str(row.get("status", "")), "?")
        label = str(row.get("label", row.get("id", "")))
        exts = _suffix_text(row.get("suffixes"))
        print(f" {mark} {label:<{width}}  {exts:<{ext_width}}  {row.get('note', '')}")
    return EXIT_OK


def helper_command(action: str, payload: Dict[str, object]) -> str:
    """The exact command a user can run themselves when the helper cannot be reached."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"pkexec {HELPER_PATH} {action} {shlex.quote(body)}"


def _format_binfmt(st: Dict[str, object]) -> str:
    registered = bool(st.get("registered"))
    enabled = bool(st.get("enabled"))
    if registered and enabled:
        state = "on - ./program.exe in a terminal opens through Lindos"
    elif st.get("masked"):
        state = "off (turned off with 'lindos-compat binfmt disable')"
    elif registered:
        state = "off (registered but disabled)"
    else:
        state = "off"
    lines = [f"Run .exe files from a terminal: {state}"]
    if st.get("interpreter"):
        lines.append(f"  handler: {st['interpreter']}")
    conflicts = st.get("conflicts")
    for item in conflicts if isinstance(conflicts, list) else []:
        if isinstance(item, dict):
            lines.append(f"  also registered for Windows programs: {item.get('name', '?')} -> "
                         f"{item.get('interpreter', '?')} (Lindos does not change it)")
    if st.get("note"):
        lines.append(f"  {st['note']}")
    lines.append("  Turn on: lindos-compat binfmt enable    Turn off: lindos-compat binfmt disable")
    return "\n".join(lines)


def _set_binfmt(enabled: bool) -> Dict[str, object]:
    """Ask the privileged helper (action ``set-binfmt``) to turn terminal .exe support on or off."""
    payload: Dict[str, object] = {"enabled": enabled}
    command = helper_command("set-binfmt", payload)
    unavailable: Dict[str, object] = {
        "ok": False, "code": 127, "command": command,
        "message": "The Lindos helper cannot do this on this system. Run this command yourself:\n  " + command,
    }
    try:
        helper = importlib.import_module("lindos.helper")
    except ImportError:
        return unavailable
    if "set-binfmt" not in list(getattr(helper, "ACTIONS", [])):
        return unavailable
    try:
        res = helper.run_privileged("set-binfmt", payload)
    except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
        out = dict(unavailable)
        out["message"] = f"The Lindos helper failed ({exc}). Run this command yourself:\n  {command}"
        return out
    code = int(getattr(res, "code", 1))
    if getattr(res, "ok", False):
        return {"ok": True, "code": 0, "message": "Turned on." if enabled else "Turned off."}
    if code == 127:
        return unavailable
    if code == 126:
        return {"ok": False, "code": 126, "message": "Cancelled - the administrator password was not given."}
    detail = getattr(res, "message", "") or f"exit code {code}"
    return {"ok": False, "code": code, "command": command, "message": f"The change failed: {detail}"}


def cmd_binfmt(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if ns.sub not in ("status", "enable", "disable"):
        parser.parse_args(["binfmt", "--help"])
        return EXIT_USAGE
    try:
        binfmt = _sibling("binfmt")
    except ImportError as exc:
        log.error("Terminal .exe support is not available (%s). Reinstall lindos-compat.", exc)
        return EXIT_ERROR
    if ns.sub == "status":
        st = dict(binfmt.status())
        if ns.json:
            _print_json(st)
        else:
            print(_format_binfmt(st))
        return EXIT_OK
    res = _set_binfmt(ns.sub == "enable")
    res["action"] = ns.sub
    try:
        res["status"] = dict(binfmt.status())
    except Exception as exc:  # noqa: BLE001 - the status is informative only
        log.debug("binfmt status failed: %s", exc)
    if ns.json:
        _print_json(res)
    else:
        print(res["message"])
        status = res.get("status")
        if isinstance(status, dict):
            print(_format_binfmt(status))
    if res.get("ok"):
        return EXIT_OK
    return EXIT_UNSUPPORTED if res.get("code") == 127 else EXIT_ERROR


# ---------------------------------------------------------------------------
# winget (SPEC-WINDOWS §28.10)
# ---------------------------------------------------------------------------


def _ask(question: str, default: bool = False) -> bool:
    """Yes/no question on the terminal (False when there is no terminal)."""
    if not sys.stdin.isatty():
        return False
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix)
    except EOFError:
        return False
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _stderr_target() -> Any:
    try:
        return sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def _run_lindos_quiet(argv: List[str], env: Dict[str, str]) -> int:
    """lindos-run with its output on stderr (keeps ``--json`` output on stdout clean)."""
    try:
        return subprocess.run(argv, env=env, stdout=_stderr_target(), check=False).returncode
    except OSError as exc:
        log.error("cannot start %s: %s", argv[0], exc)
        return 127


def _same_installer(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    keys = ("InstallerUrl", "Architecture", "BaseInstallerType", "Scope", "InstallerLocale")
    return all(a.get(k) == b.get(k) for k in keys)


def winget_show_data(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """What ``winget show --json`` prints: summary, every effective installer, Lindos' choice."""
    installers = winget.effective_installers(manifest)
    agreements = manifest.get("Agreements")
    data: Dict[str, Any] = {
        "id": manifest.get("PackageIdentifier", ""),
        "version": manifest.get("PackageVersion", ""),
        "name": manifest.get("PackageName", ""),
        "publisher": manifest.get("Publisher", ""),
        "description": manifest.get("ShortDescription") or manifest.get("Description") or "",
        "homepage": manifest.get("PackageUrl") or manifest.get("PublisherUrl") or "",
        "license": manifest.get("License", ""),
        "license_url": manifest.get("LicenseUrl", ""),
        "agreements": agreements if isinstance(agreements, list) else [],
        "source": manifest.get("LindosSource", {}),
        "installers": installers,
        "selected": None,
        "selection_error": "",
        "notes": [],
        "manifest": manifest,
    }
    try:
        chosen = winget.select_installer(manifest)
    except winget.WingetError as exc:
        data["selection_error"] = str(exc)
        return data
    data["selected"] = next((i for i, inst in enumerate(installers) if _same_installer(inst, chosen)), None)
    data["notes"] = winget.installer_notes(manifest, chosen)
    return data


def _print_winget_show(data: Dict[str, Any]) -> None:
    print(f"{data['name'] or data['id']} {data['version']}  ({data['id']})")
    for label, key in (("Publisher", "publisher"), ("About", "description"), ("Homepage", "homepage")):
        text = str(data.get(key) or "").strip()
        if text:
            print(f"  {label + ':':<11}{text.splitlines()[0]}")
    lic = " - ".join(str(x) for x in (data.get("license"), data.get("license_url")) if x)
    if lic:
        print(f"  {'Licence:':<11}{lic}")
    for item in data.get("agreements") or []:
        if isinstance(item, dict):
            label = str(item.get("AgreementLabel") or "Agreement") + ":"
            print(f"  {label:<11}{item.get('AgreementUrl') or str(item.get('Agreement', ''))[:200]}")
    source = data.get("source")
    if isinstance(source, dict) and source.get("channel"):
        where = "Microsoft's winget CDN" if source["channel"] == "cdn" else "GitHub (microsoft/winget-pkgs)"
        print(f"  {'Source:':<11}{where}, checked by hash ({source.get('chain', '')})")
    print("  Installers:")
    for i, inst in enumerate(data["installers"]):
        mark = "->" if data["selected"] == i else "  "
        kind = str(inst.get("EffectiveInstallerType", ""))
        if inst.get("BaseInstallerType") == "zip":
            kind = f"zip/{kind}"
        print(f"   {mark} {str(inst.get('Architecture', '?')):<8}{kind:<17}{str(inst.get('Scope') or '-'):<9}"
              f"{str(inst.get('InstallerLocale') or ''):<7}{inst.get('InstallerUrl', '')}")
    if data["selected"] is not None:
        print("   (-> is the one Lindos would use)")
    if data["selection_error"]:
        print(data["selection_error"])
    for note in data["notes"]:
        print(f"  Note: {note}")
    if data["selected"] is not None:
        print(f"\nInstall it:  lindos-compat winget install {data['id']}")


def _print_install_result(res: Dict[str, Any]) -> None:
    if res.get("status") == "dry-run":
        print(winget.format_plan(res))
        if res.get("argv"):
            print("Would run:  " + " ".join(shlex.quote(str(a)) for a in res["argv"]))
        print(res.get("message", ""))
        return
    print(res.get("message", ""))
    for dep in res.get("dependency_results") or []:
        if isinstance(dep, dict):
            print(f"  dependency {dep.get('id', '')}: {dep.get('status', '')}")
    if res.get("apps"):
        print("Added to the Start Menu / Lindos Settings > Windows apps: " + ", ".join(res["apps"]))


def _winget_search(ns: argparse.Namespace) -> int:
    if ns.limit < 1 or ns.limit > 1000:
        log.error("--limit must be between 1 and 1000")
        return EXIT_USAGE
    query = " ".join(ns.query)
    results = winget.search(query, limit=ns.limit)
    if ns.json:
        _print_json(results)
        return EXIT_OK
    if not results:
        print(f"No package matches '{query}'.")
        return EXIT_OK
    nw = min(max(len(r["name"]) for r in results), 40)
    iw = min(max(len(r["id"]) for r in results), 45)
    print(f"{'Name':<{nw}}  {'Id':<{iw}}  {'Version':<16} Match")
    for r in results:
        print(f"{r['name'][:nw]:<{nw}}  {r['id']:<{iw}}  {r['version'][:16]:<16} {r['match']}")
    print("\nDetails: lindos-compat winget show <Id>   |   Install: lindos-compat winget install <Id>")
    return EXIT_OK


def _winget_install(ns: argparse.Namespace) -> int:
    say: Callable[[str], None] = (lambda text: print(text, file=sys.stderr)) if ns.json else print
    kwargs: Dict[str, Any] = {
        "version": ns.pkg_version, "arch": ns.arch, "prefix": ns.prefix, "interactive": ns.interactive,
        "accept_package_agreements": ns.accept_package_agreements, "dry_run": ns.dry_run, "out": say,
    }
    if sys.stdin.isatty() and not ns.json:
        kwargs["confirm"] = _ask
    if ns.json:
        kwargs["run_lindos"] = _run_lindos_quiet
    res = winget.install(ns.id, **kwargs)
    if ns.json:
        _print_json(res)
    else:
        _print_install_result(res)
    if res.get("ok"):
        return EXIT_OK
    return EXIT_UNSUPPORTED if res.get("status") == "unsupported" else EXIT_ERROR


def _winget_list(ns: argparse.Namespace) -> int:
    items = winget.list_installed()
    if ns.json:
        _print_json(items)
        return EXIT_OK
    if not items:
        print("Nothing installed with 'lindos-compat winget install' yet.")
        return EXIT_OK
    for it in items:
        newer = f"   (newer version: {it['latest']})" if it.get("update_available") else ""
        print(f" {it['name'] or it['id']} {it['version']}  [{it['id']}]  C:\\ drive: {it['prefix']}{newer}")
    return EXIT_OK


def _winget_update_index(ns: argparse.Namespace) -> int:
    con = winget.load_index(max_age_s=0)
    try:
        info = winget.index_info(con)
    finally:
        con.close()
    if ns.json:
        _print_json(info)
    else:
        published = f" (published {info['last_modified']})" if info.get("last_modified") else ""
        print(f"The winget catalogue index is up to date: {info['packages']} packages{published}.")
    return EXIT_OK


def _winget_show(ns: argparse.Namespace) -> int:
    data = winget_show_data(winget.load_manifest(ns.id, ns.pkg_version))
    if ns.json:
        _print_json(data)
    else:
        _print_winget_show(data)
    return EXIT_OK


def cmd_winget(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    handlers: Dict[str, Callable[[argparse.Namespace], int]] = {
        "search": _winget_search,
        "show": _winget_show,
        "install": _winget_install,
        "list": _winget_list,
        "update-index": _winget_update_index,
    }
    handler = handlers.get(str(ns.sub))
    if handler is None:
        parser.parse_args(["winget", "--help"])
        return EXIT_USAGE
    try:
        return handler(ns)
    except winget.WingetError as exc:
        status = "unsupported" if isinstance(exc, winget.WingetUnsupported) else "error"
        if getattr(ns, "json", False):
            _print_json({"ok": False, "status": status, "error": str(exc)})
        else:
            log.error("%s", exc)
        return int(getattr(exc, "exit_code", EXIT_ERROR))
    except OSError as exc:
        log.error("%s", exc)
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    global log
    log = get_logger("lindos-compat", verbose=bool(ns.verbose) or None)
    if not ns.cmd:
        parser.print_help()
        return EXIT_USAGE
    try:
        if ns.cmd == "doctor":
            return cmd_doctor(ns)
        if ns.cmd == "prefixes":
            return cmd_prefixes(ns, parser)
        if ns.cmd == "recipes":
            return cmd_recipes(ns, parser)
        if ns.cmd == "install-umu":
            return cmd_install_umu(ns)
        if ns.cmd == "install-bottles":
            return cmd_install_bottles(ns)
        if ns.cmd == "install-dxvk":
            return cmd_install_component(ns, "dxvk")
        if ns.cmd == "install-vkd3d":
            return cmd_install_component(ns, "vkd3d")
        if ns.cmd == "proton":
            return cmd_proton(ns)
        if ns.cmd == "formats":
            return cmd_formats(ns)
        if ns.cmd == "binfmt":
            return cmd_binfmt(ns, parser)
        if ns.cmd == "winget":
            return cmd_winget(ns, parser)
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
