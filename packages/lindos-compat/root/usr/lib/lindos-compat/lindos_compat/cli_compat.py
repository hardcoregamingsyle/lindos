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

Exit codes: 0 ok · 1 error · 2 usage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import List, Optional, Sequence

from . import __version__, get_logger
from .doctor import format_report, run_doctor
from .installers import UMU_VERSION, install_bottles, install_dxvk, install_umu, install_vkd3d
from .prefix import list_prefixes, open_prefix, prefix_path, remove_prefix, run_winecfg
from .recipes import STATUSES, STATUS_LABEL, STATUS_MARK, apply_recipe, format_recipe, get_recipe, load_recipes

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "build_parser", "main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

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
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
