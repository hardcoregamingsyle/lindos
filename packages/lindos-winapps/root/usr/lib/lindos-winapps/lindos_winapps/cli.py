"""``lindos-winapps`` argparse CLI (SPEC-VM §22).

  lindos-winapps setup   [--backend libvirt|podman] [--host H] [--user U] [--domain D]
                         [--vm-name N] [--port P] [--flags "..."]
  lindos-winapps check   [--json]
  lindos-winapps list    [--json]
  lindos-winapps install <id>
  lindos-winapps run     <id> [-- args...]
  lindos-winapps remove  <id>
  lindos-winapps --version

Exit codes: 0 ok · 1 error · 2 usage · 3 backend unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import (EXIT_ERROR, EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE, __version__, get_logger,
               winapps_conf_path)
from . import apps as apps_mod
from . import backend as backend_mod
from . import rdp as rdp_mod

log = get_logger("lindos-winapps")


def _load_catalog():
    try:
        return apps_mod.load_catalog()
    except apps_mod.CatalogError as exc:
        log.error("%s", exc)
        return None


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_setup(ns: argparse.Namespace) -> int:
    cfg = backend_mod.load_config()
    if ns.backend:
        cfg.backend = ns.backend
    if ns.host:
        cfg.host = ns.host
    if ns.user is not None:
        cfg.user = ns.user
    if ns.domain is not None:
        cfg.domain = ns.domain
    if ns.vm_name:
        cfg.vm_name = ns.vm_name
    if ns.port:
        cfg.port = ns.port
    if ns.flags:
        cfg.flags = ns.flags
    path = backend_mod.write_config(cfg)
    print(f"Wrote {path}")
    print("Backend:  " + cfg.backend + (f" (domain '{cfg.vm_name}')" if cfg.backend == "libvirt" else ""))
    print(f"RDP host: {cfg.host}:{cfg.port}   user: {cfg.user or '(set with --user)'}")
    print("")
    print("Your Windows password is NOT stored.  Provide it at launch via ONE of:")
    print("  * FreeRDP's prompt (default when no password is given),")
    print("  * the RDP_PASS environment variable, or")
    print(f"  * a file you create: {rdp_mod.rdp_pass_file()}  (chmod 600).")
    print("This needs your own licensed Windows + the app installed in the backend.")
    return EXIT_OK


def cmd_check(ns: argparse.Namespace) -> int:
    cfg = backend_mod.load_config()
    report = backend_mod.check(cfg)
    if ns.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        conf = winapps_conf_path()
        print("Lindos WinApps - readiness check")
        print(f"  config file:      {conf} " + ("(present)" if conf.is_file() else "(missing - run: lindos-winapps setup)"))
        print(f"  backend:          {report['backend']}  (domain '{report['vm_name']}')")
        fr = report["freerdp"] or "not found (install freerdp3-x11 or freerdp2-x11)"
        print(f"  FreeRDP client:   {fr}")
        running = report["backend_running"]
        running_txt = "running" if running else ("stopped" if running is False else "unknown (no virsh/podman)")
        print(f"  backend state:    {running_txt}")
        print(f"  RDP {report['host']}:{report['port']}:  " + ("open" if report["rdp_port_open"] else "closed"))
        print("  " + ("READY" if report["reachable"] and report["freerdp_present"]
                      else "NOT READY - see items above"))
    if not report["freerdp_present"]:
        return EXIT_ERROR
    return EXIT_OK if report["reachable"] else EXIT_UNREACHABLE


def cmd_list(ns: argparse.Namespace) -> int:
    catalog = _load_catalog()
    if catalog is None:
        return EXIT_ERROR
    rows = apps_mod.app_rows(catalog)
    cfg = backend_mod.load_config()
    report = backend_mod.check(cfg)
    if ns.json:
        print(json.dumps({"reachable": report["reachable"], "apps": rows}, indent=2, sort_keys=True))
        return EXIT_OK
    print("Lindos WinApps - catalog")
    print("  backend " + ("reachable" if report["reachable"] else "not reachable (apps still installable)"))
    width = max((len(r["id"]) for r in rows), default=8)
    for r in rows:
        mark = "*" if r["installed"] else " "
        print(f"  [{mark}] {str(r['id']).ljust(width)}  {r['name']}")
    print("  ([*] = added to the menu; install with: lindos-winapps install <id>)")
    return EXIT_OK


def cmd_install(ns: argparse.Namespace) -> int:
    catalog = _load_catalog()
    if catalog is None:
        return EXIT_ERROR
    try:
        app = apps_mod.get_app(ns.id, catalog)
    except apps_mod.CatalogError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    path = apps_mod.install_app(app)
    print(f"Installed '{app.name}' -> {path}")
    print(f"Note: {app.note}")
    return EXIT_OK


def cmd_remove(ns: argparse.Namespace) -> int:
    if apps_mod.remove_app(ns.id):
        print(f"Removed launcher for '{ns.id}'")
    else:
        print(f"No launcher installed for '{ns.id}' (nothing to do)")
    return EXIT_OK


def cmd_run(ns: argparse.Namespace) -> int:
    catalog = _load_catalog()
    if catalog is None:
        return EXIT_ERROR
    try:
        app = apps_mod.get_app(ns.id, catalog)
    except apps_mod.CatalogError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    cfg = backend_mod.load_config()
    if not rdp_mod.find_freerdp():
        log.error("no FreeRDP client found; install freerdp3-x11 or freerdp2-x11")
        return EXIT_ERROR
    if not backend_mod.rdp_port_open(cfg.host, cfg.port):
        log.error("backend not reachable at %s:%s (start the VM: lindos-vm start %s)",
                  cfg.host, cfg.port, cfg.vm_name)
        return EXIT_UNREACHABLE
    extra = list(ns.args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    rc = rdp_mod.launch(app, cfg, args=extra or None)
    return EXIT_OK if rc == 0 else EXIT_ERROR


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lindos-winapps",
                                description="Seamless Windows apps over RDP (Adobe / Office).")
    p.add_argument("--version", action="version", version=f"lindos-winapps {__version__}")
    sub = p.add_subparsers(dest="command", metavar="{setup,check,list,install,run,remove}")

    sp = sub.add_parser("setup", help="write ~/.config/lindos/winapps/winapps.conf (no password)")
    sp.add_argument("--backend", choices=backend_mod.BACKENDS)
    sp.add_argument("--host")
    sp.add_argument("--user")
    sp.add_argument("--domain")
    sp.add_argument("--vm-name", dest="vm_name")
    sp.add_argument("--port", type=int)
    sp.add_argument("--flags")
    sp.set_defaults(func=cmd_setup)

    cp = sub.add_parser("check", help="backend reachable? FreeRDP present? RDP port open?")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_check)

    lp = sub.add_parser("list", help="list the app catalog (and which are installed)")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=cmd_list)

    ip = sub.add_parser("install", help="add a Windows app to the Lindos menu")
    ip.add_argument("id")
    ip.set_defaults(func=cmd_install)

    rp = sub.add_parser("run", help="launch a Windows app over RDP RemoteApp")
    rp.add_argument("id")
    rp.add_argument("args", nargs=argparse.REMAINDER, help="extra args after -- passed to the app")
    rp.set_defaults(func=cmd_run)

    xp = sub.add_parser("remove", help="remove a Windows app from the menu")
    xp.add_argument("id")
    xp.set_defaults(func=cmd_remove)
    return p


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not getattr(ns, "command", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(ns.func(ns))
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_ERROR
