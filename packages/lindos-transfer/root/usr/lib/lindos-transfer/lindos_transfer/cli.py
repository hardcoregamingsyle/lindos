"""``lindos-transfer`` command line (SPEC-WINDOWS §29.2).

::

    lindos-transfer sources [--json]
    lindos-transfer mount <device> [--json]
    lindos-transfer users --from <PATH> [--json]
    lindos-transfer plan  --from <PATH> [--user NAME] [--only CAT,...] [--exclude CAT,...]
                          [--dest DIR] [--firefox-passwords] [--json] [-o plan.json]
    lindos-transfer run   (--plan plan.json | --from PATH [--user NAME] [--only ...] [--exclude ...]
                          [--dest DIR] [--firefox-passwords]) [--yes] [--dry-run] [--json-progress]
    lindos-transfer apps  --from <PATH> [--user NAME] [--json]
    lindos-transfer install-apps --plan plan.json [--yes] [--dry-run] [--json]
    lindos-transfer make-usb-kit <DIR> [--json]
    lindos-transfer report [--json]

Exit codes: ``0`` ok, ``1`` error, ``2`` usage, ``4`` nothing to transfer.  ``--from`` accepts a
mounted Windows root (``Windows/`` + ``Users/``, matched case-insensitively) or a transfer folder
(``lindos-transfer.json``); a block device (``/dev/...``) is refused with a pointer to ``mount``.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import EXIT_ERROR, EXIT_NOTHING, EXIT_OK, EXIT_USAGE, TransferError, __version__, get_logger, \
    share_dir, user_home
from .apps import install_apps as apps_install_apps, plan_apps, verify_apps_against_source
from .copyengine import human_size
from .mounts import bitlocker_guidance, mount as mount_device
from .plan import CATEGORIES, apply_selection, build_plan, load_plan, make_context, run_plan, save_plan, \
    selected_items
from .profiles import list_users
from .report import format_text, load_last_report
from .sources import LSBLK_ARGV, detect_sources, is_link, open_source

__all__ = ["build_parser", "main"]

log = get_logger("lindos-transfer")


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(question + " [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lindos-transfer",
        description="Bring your files, browser bookmarks, Wi-Fi networks, wallpaper, fonts, Steam "
                    "games and a list of your apps from Windows to Lindos.",
    )
    p.add_argument("--version", action="version", version=f"lindos-transfer {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug output")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("sources", help="find Windows drives on this PC and transfer folders on USB drives")
    s.add_argument("--json", action="store_true")

    m = sub.add_parser("mount", help="open a Windows drive read-only")
    m.add_argument("device", help="e.g. /dev/nvme0n1p3 (see: lindos-transfer sources)")
    m.add_argument("--json", action="store_true")

    u = sub.add_parser("users", help="list the Windows user accounts on a drive or transfer folder")
    u.add_argument("--from", dest="from_path", required=True, metavar="PATH")
    u.add_argument("--json", action="store_true")

    def add_selection_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--user", metavar="NAME", help="Windows user (default: the only one found)")
        sp.add_argument("--only", action="append", metavar="CAT,...",
                        help=f"only these categories ({', '.join(CATEGORIES)}); may be repeated")
        sp.add_argument("--exclude", action="append", metavar="CAT,...", help="leave these categories out")
        sp.add_argument("--dest", metavar="DIR", help="Lindos home to copy into (default: yours)")
        sp.add_argument("--firefox-passwords", action="store_true",
                        help="also move Firefox's saved passwords (still protected by your Primary Password)")

    pl = sub.add_parser("plan", help="see what would be transferred, without copying anything")
    pl.add_argument("--from", dest="from_path", required=True, metavar="PATH")
    add_selection_args(pl)
    pl.add_argument("-o", "--output", metavar="plan.json", help="save the plan to this file")
    pl.add_argument("--json", action="store_true")

    r = sub.add_parser("run", help="copy the selected files and settings to Lindos")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--plan", metavar="plan.json", help="a plan made with 'lindos-transfer plan -o'")
    src.add_argument("--from", dest="from_path", metavar="PATH", help="build and run a plan directly")
    add_selection_args(r)
    r.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    r.add_argument("--dry-run", action="store_true", help="show what would happen; copy nothing")
    r.add_argument("--json-progress", action="store_true", help="one JSON object per line as it copies")

    a = sub.add_parser("apps", help="list the Windows programs found, and how Lindos would bring each one")
    a.add_argument("--from", dest="from_path", required=True, metavar="PATH")
    a.add_argument("--user", metavar="NAME")
    a.add_argument("--json", action="store_true")

    ia = sub.add_parser("install-apps", help="install the apps chosen in a plan (nothing runs unselected)")
    ia.add_argument("--plan", required=True, metavar="plan.json")
    ia.add_argument("-y", "--yes", action="store_true")
    ia.add_argument("--dry-run", action="store_true")
    ia.add_argument("--json", action="store_true")

    k = sub.add_parser("make-usb-kit", help="copy the Windows-side transfer kit onto a USB drive")
    k.add_argument("dir", metavar="DIR")
    k.add_argument("--json", action="store_true")

    rp = sub.add_parser("report", help="show the report of the last transfer")
    rp.add_argument("--json", action="store_true")
    return p


# --------------------------------------------------------------------------- #
# sources / mount
# --------------------------------------------------------------------------- #
def cmd_sources(ns: argparse.Namespace) -> int:
    data = detect_sources()
    if ns.json:
        _print_json(data)
        return EXIT_OK
    parts, bundles = data["partitions"], data["bundles"]
    if not parts and not bundles:
        print("No Windows drives or Lindos transfer folders were found.")
        print("On a USB stick from the old PC's kit, or run: lindos-transfer make-usb-kit <folder> on that PC.")
        return EXIT_OK
    for part in parts:
        tags = [t for t, ok in (("Windows", part["windows"]), ("BitLocker", part["bitlocker"]),
                                ("hibernated", part["hibernated"])) if ok]
        where = part["mountpoint"] or "(not opened yet)"
        print(f"{part['device']:<18} {(part['label'] or '-'):<16} {human_size(part['size']):>10}  "
              f"{part['fstype']:<10} {where}  [{', '.join(tags) or 'not Windows'}]")
        if part["note"]:
            print(f"    {part['note']}")
    for bundle in bundles:
        print(f"{bundle['path']}  (from {bundle['computer'] or '?'}, user {bundle['user'] or '?'}, "
              f"made {bundle['created'] or '?'})")
    return EXIT_OK


def _device_fstype(device: str, run: Callable[..., Any] = subprocess.run) -> str:
    argv = [LSBLK_ARGV[0], "-J", "-b", "-o", "FSTYPE", "-n", device]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    try:
        devs = json.loads(getattr(proc, "stdout", "") or "{}").get("blockdevices") or []
    except ValueError:
        return ""
    return str(devs[0].get("fstype") or "") if devs else ""


def cmd_mount(ns: argparse.Namespace) -> int:
    fstype = _device_fstype(ns.device)
    try:
        result = mount_device(ns.device, fstype=fstype)
    except TransferError as exc:
        if fstype.lower() == "bitlocker":
            guidance = bitlocker_guidance(ns.device)
            if ns.json:
                _print_json({"ok": False, "error": str(exc), "bitlocker": guidance})
            else:
                print(guidance["summary"])
                for step in guidance["steps"]:
                    print("  " + (" ".join(step["command"]) if step["command"] else step["what"]))
                print(guidance["clear_key"]["why"])
                print("  " + " ".join(guidance["clear_key"]["command"]))
                print("  " + " ".join(guidance["clear_key"]["then"]))
                print("Recovery key: " + guidance["recovery_key_url"])
            return EXIT_ERROR
        if ns.json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            log.error("%s", exc)
        return EXIT_ERROR
    if ns.json:
        _print_json({"ok": True, **result})
    else:
        print(f"Mounted {result['device']} at {result['mountpoint']} ({result['driver']}, read-only).")
        for note in result["notes"]:
            print(f"  ! {note}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def cmd_users(ns: argparse.Namespace) -> int:
    source = open_source(ns.from_path)
    try:
        users = [u.as_dict() for u in list_users(source)]
    finally:
        source.close()
    if ns.json:
        _print_json(users)
        return EXIT_OK
    if not users:
        print("No Windows user accounts were found.")
        return EXIT_OK
    for u in users:
        flag = " (Windows may not have shut down fully - settings could be out of date)" if u["stale"] else ""
        print(f"{u['name']}{flag}")
        for note in u["notes"]:
            print(f"    {note}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# plan / run
# --------------------------------------------------------------------------- #
def _print_plan_summary(plan: Dict[str, Any]) -> None:
    src = plan["source"]
    print(f"Plan {plan['id']} for '{plan['user']}' from {src.get('computer') or 'Windows'} "
          f"({src.get('windows') or ''}) -> {plan['dest_home']}")
    total_files = total_bytes = 0
    for it in plan["items"]:
        mark = "x" if it["selected"] else " "
        if it["selected"]:
            total_files += it["files"]
            total_bytes += it["bytes"]
        print(f" [{mark}] {it['label']:<48} {it['files']:>6} files  {human_size(it['bytes']):>10}")
        for note in it["notes"]:
            print(f"       {note}")
    if plan["apps"]:
        chosen = sum(1 for a in plan["apps"] if a["actions"])
        print(f" Apps found: {len(plan['apps'])} ({chosen} with a known Lindos route) - "
              "see: lindos-transfer apps --from ...")
    for w in plan["warnings"]:
        print(f"! {w}")
    print(f"Selected: {total_files} files, {human_size(total_bytes)}")


def cmd_plan(ns: argparse.Namespace) -> int:
    source = open_source(ns.from_path)
    plan, ctx = build_plan(source, user=ns.user, only=ns.only, exclude=ns.exclude, dest=ns.dest,
                           firefox_passwords=ns.firefox_passwords)
    try:
        if ns.output:
            save_plan(plan, ns.output)
        if ns.json:
            _print_json(plan)
        else:
            _print_plan_summary(plan)
        return EXIT_OK if plan["items"] or plan["apps"] else EXIT_NOTHING
    finally:
        ctx.close()


def _json_progress_emitter() -> Callable[[Dict[str, Any]], None]:
    def emit(event: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return emit


def _text_progress_emitter() -> Callable[[Dict[str, Any]], None]:
    def emit(event: Dict[str, Any]) -> None:
        if event["event"] in ("start", "item", "error", "skip"):
            text = event.get("message") or event.get("path") or ""
            print(f"  {event['event']}: {text}")

    return emit


def cmd_run(ns: argparse.Namespace) -> int:
    emit = _json_progress_emitter() if ns.json_progress else _text_progress_emitter()
    if ns.plan:
        saved = load_plan(ns.plan)
        source = open_source(saved["source"]["root"])
        dest = ns.dest
        if not dest:
            # A plan's own dest_home is not trusted at face value (SPEC-WINDOWS §29.6): an edited
            # or foreign plan file could otherwise redirect a whole transfer into any directory the
            # invoking user can write to. Only a dest_home that still matches the real Lindos home
            # is honoured; anything else needs an explicit --dest to confirm the destination.
            real_home = str(user_home())
            saved_home = str(saved.get("dest_home") or "")
            if saved_home != real_home:
                source.close()
                raise TransferError(
                    f"This plan's destination ({saved_home!r}) is not your Lindos home "
                    f"({real_home!r}). Pass --dest to confirm where to copy to, or make a new plan.")
            dest = saved_home
        fresh, ctx = build_plan(source, user=saved.get("user"), dest=dest,
                                firefox_passwords=bool((saved.get("options") or {}).get("firefox_passwords")))
        for note in apply_selection(fresh, saved):
            log.warning("%s", note)
        plan = fresh
    else:
        source = open_source(ns.from_path)
        plan, ctx = build_plan(source, user=ns.user, only=ns.only, exclude=ns.exclude, dest=ns.dest,
                               firefox_passwords=ns.firefox_passwords)
    ctx.dry_run = bool(ns.dry_run)
    try:
        chosen = selected_items(plan)
        if not chosen:
            print("Nothing selected to transfer.")
            return EXIT_NOTHING
        if not ns.yes and not ns.dry_run:
            if not sys.stdin.isatty():
                log.error("Refusing to copy files without --yes (there is no terminal to ask).")
                return EXIT_USAGE
            total = sum(it["bytes"] for it in chosen)
            if not _confirm(f"Copy {len(chosen)} item(s), {human_size(total)}, to {plan['dest_home']}?"):
                print("Cancelled.")
                return EXIT_ERROR
        report = run_plan(plan, ctx, emit=emit)
        if report is None:
            print("Nothing selected to transfer.")
            return EXIT_NOTHING
        if not ns.json_progress:
            print(format_text(report))
        return EXIT_OK if report["totals"]["errors"] == 0 else EXIT_ERROR
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# apps / install-apps
# --------------------------------------------------------------------------- #
def cmd_apps(ns: argparse.Namespace) -> int:
    source = open_source(ns.from_path)
    ctx = make_context(source, user=ns.user)
    try:
        apps = plan_apps(ctx)
    finally:
        ctx.close()
    if ns.json:
        _print_json(apps)
        return EXIT_OK
    if not apps:
        print("No installed programs were found.")
        return EXIT_OK
    for a in apps:
        if a["actions"]:
            route = a["actions"][a["chosen"]]["label"]
        else:
            route = "no known Lindos route yet"
        print(f"{a['windows_name']:<42} {a['version']:<14} -> {route}")
    return EXIT_OK


def cmd_install_apps(ns: argparse.Namespace) -> int:
    plan = load_plan(ns.plan)
    selected = [a for a in plan.get("apps") or [] if a.get("selected")]
    if not selected:
        print("No apps are selected to install in this plan.")
        return EXIT_NOTHING
    if not ns.yes and not ns.dry_run:
        if not sys.stdin.isatty():
            log.error("Refusing to install apps without --yes (there is no terminal to ask).")
            return EXIT_USAGE
        if not _confirm(f"Install {len(selected)} app(s) chosen in this plan?"):
            print("Cancelled.")
            return EXIT_ERROR
    # The plan file's own actions are never trusted at face value: re-derive them from the plan's
    # live source and app-map.json, and only run what still matches (see the untrusted-plan-file
    # honesty rule in apps.verify_apps_against_source's docstring).
    try:
        verified_apps, notes = verify_apps_against_source(plan)
    except TransferError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    for note in notes:
        log.warning("%s", note)
    plan = dict(plan, apps=verified_apps)
    if not verified_apps:
        print("No apps are selected to install in this plan.")
        return EXIT_NOTHING
    # The destination for a web-app shortcut is always the invoking user's own home, never the
    # plan file's (possibly tampered) dest_home.
    home = user_home()
    emit = _json_progress_emitter() if ns.json else None
    results = apps_install_apps(plan, home=home, dry_run=ns.dry_run, emit=emit)
    if ns.json:
        if emit is None:
            _print_json(results)
    else:
        for r in results:
            mark = "OK" if r["status"] in ("done", "dry-run") else "FAILED"
            print(f"[{mark}] {r['windows_name']}: {r['message']}")
    return EXIT_OK if all(r["status"] in ("done", "dry-run") for r in results) else EXIT_ERROR


# --------------------------------------------------------------------------- #
# make-usb-kit / report
# --------------------------------------------------------------------------- #
def cmd_make_usb_kit(ns: argparse.Namespace) -> int:
    src_dir = share_dir() / "windows"
    if not src_dir.is_dir():
        msg = (f"The Windows transfer kit is not installed on this Lindos yet (missing {src_dir}). "
               "Reinstall lindos-transfer.")
        if ns.json:
            _print_json({"ok": False, "error": msg})
        else:
            log.error("%s", msg)
        return EXIT_ERROR
    dest = Path(ns.dir).expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"cannot create {dest}: {exc.strerror or exc}"
        if ns.json:
            _print_json({"ok": False, "error": msg})
        else:
            log.error("%s", msg)
        return EXIT_ERROR
    copied: List[str] = []
    for f in sorted(src_dir.iterdir()):
        if f.is_file() and not is_link(f):
            (dest / f.name).write_bytes(f.read_bytes())
            copied.append(f.name)
    if not copied:
        msg = f"No transfer kit files were found in {src_dir}."
        if ns.json:
            _print_json({"ok": False, "error": msg})
        else:
            log.error("%s", msg)
        return EXIT_ERROR
    if ns.json:
        _print_json({"ok": True, "dest": str(dest), "files": copied})
    else:
        print(f"Copied {len(copied)} file(s) to {dest}.")
        print("On the old Windows PC, plug in this drive and double-click LindosTransfer.cmd, then "
              "bring the folder it creates back here.")
    return EXIT_OK


def cmd_report(ns: argparse.Namespace) -> int:
    report = load_last_report()
    if report is None:
        if ns.json:
            _print_json(None)
        else:
            print("No transfer has been run yet.")
        return EXIT_NOTHING
    if ns.json:
        _print_json(report)
    else:
        print(format_text(report))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
_HANDLERS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "sources": cmd_sources, "mount": cmd_mount, "users": cmd_users, "plan": cmd_plan, "run": cmd_run,
    "apps": cmd_apps, "install-apps": cmd_install_apps, "make-usb-kit": cmd_make_usb_kit, "report": cmd_report,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    global log
    log = get_logger("lindos-transfer", verbose=bool(getattr(ns, "verbose", False)) or None)
    if not ns.cmd:
        parser.print_help()
        return EXIT_USAGE
    handler = _HANDLERS.get(ns.cmd)
    if handler is None:  # pragma: no cover - argparse already restricts ns.cmd
        parser.print_help()
        return EXIT_USAGE
    try:
        return handler(ns)
    except TransferError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
