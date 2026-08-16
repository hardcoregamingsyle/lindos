"""argparse front-end behind ``/usr/bin/lindos-tune`` (SPEC §11).

Subcommands (exactly)::

    lindos-tune status [--json]
    lindos-tune apply --mode <id> [--system] [--offline] [--dry-run] [--json]
    lindos-tune services list [--json] | disable <unit>… | enable <unit>…  [--dry-run] [--json]
    lindos-tune zram <percent> [--dry-run] [--json]
    lindos-tune governor <g> [--dry-run] [--json]
    lindos-tune fan list [--json] | fan set <profile> [--dry-run] [--json]
    lindos-tune power <performance|balanced|power-saver> [--dry-run] [--json]
    lindos-tune sched list [--json] | sched status [--json] | sched set <profile> [--dry-run] [--json]
    lindos-tune report [-o FILE]

Exit codes: 0 ok · 1 error (a step failed, refused unit, escalation failed) · 2 usage ·
3 ``sched set`` on a kernel without sched_ext (CONFIG_SCHED_CLASS_EXT).

Privileges: ``status``, ``report``, ``services list``, ``fan list`` and every ``--dry-run`` are
unprivileged.  The rest need root; when ``euid != 0`` (and no ``LINDOS_ROOT`` staging tree is
set) the request is handed to lindos-core's helper through :mod:`lindos_tune.privileged`
(``pkexec lindos-helper …``), which calls ``lindos-tune`` back as root — the helper is the only
privileged entry point.  ``LINDOS_TUNE_NO_ESCALATE=1`` turns that into an error with a
``sudo lindos-tune …`` hint (CI, scripts).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from . import __version__ as _VERSION
from . import common
from .common import EXIT_ERROR, EXIT_NO_SCHED_EXT, EXIT_OK, EXIT_USAGE, Context, Report

PROG = "lindos-tune"
log = logging.getLogger("lindos.tune.cli")


# --- helpers ---------------------------------------------------------------------------------------
def _setup_logging(verbose: bool) -> None:
    root_logger = logging.getLogger("lindos.tune")
    if root_logger.handlers:
        root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        return
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stream.setFormatter(logging.Formatter(f"{PROG}: %(levelname)s: %(message)s"))
    root_logger.addHandler(stream)
    # file log (root only; never fatal)
    if common.is_root():
        try:
            path = common.path(common.TUNE_LOG)
            common.ensure_dir(os.path.dirname(path))
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root_logger.addHandler(fh)
        except OSError:
            pass


def _print(text: str) -> None:
    try:
        sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    except UnicodeEncodeError:  # e.g. cp1252 consoles: degrade instead of crashing
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")
    sys.stdout.flush()


def _emit_report(report: Report, as_json: bool) -> int:
    if as_json:
        _print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        _print(report.render())
    return EXIT_OK if report.ok else EXIT_ERROR


def _staging() -> bool:
    """A ``LINDOS_ROOT`` tree is being edited (tests / ISO staging) — no privileges needed."""
    return bool(common.root())


def _needs_escalation(dry_run: bool) -> bool:
    return not dry_run and not common.is_root() and not _staging()


def _escalate(request: "Any", as_json: bool, verbose: bool) -> int:
    from . import privileged

    logger: Optional[Callable[[str], None]]
    if as_json:
        collected: List[str] = []
        logger = collected.append
    else:
        logger = lambda line: _print(line)  # noqa: E731 - tiny adapter
    if verbose:
        _print(privileged.describe(request))
    result = privileged.escalate(request, log=logger)
    if as_json:
        data: Dict[str, Any] = {"ok": result.ok, "code": result.code, "escalated": True, "action": result.action,
                                "payload": result.payload, "op": request.to_payload(), "out": result.out, "err": result.err}
        _print(json.dumps(data, indent=2, sort_keys=True))
    elif not result.ok:
        _print(f"{PROG}: {result.message}")
    return EXIT_OK if result.ok else (EXIT_USAGE if result.code == EXIT_USAGE else EXIT_ERROR)


def _context(ns: argparse.Namespace) -> Context:
    return Context(dry_run=bool(getattr(ns, "dry_run", False)), offline=bool(getattr(ns, "offline", False)),
                   log_fn=(log.debug if getattr(ns, "verbose", False) else None))


# --- subcommands -----------------------------------------------------------------------------------
def cmd_status(ns: argparse.Namespace) -> int:
    from . import status as lstatus

    _print(lstatus.run_status(_context(ns), as_json=bool(ns.json)))
    return EXIT_OK


def cmd_apply(ns: argparse.Namespace) -> int:
    from . import apply as lapply, privileged

    if ns.mode not in common.MODE_IDS:
        sys.stderr.write(f"{PROG}: unknown mode '{ns.mode}' (choose from {', '.join(common.MODE_IDS)})\n")
        return EXIT_USAGE
    if _needs_escalation(ns.dry_run):
        return _escalate(privileged.Request.apply(ns.mode, offline=bool(ns.offline)), ns.json, ns.verbose)
    ctx = _context(ns)
    log.info("apply --mode %s%s%s%s", ns.mode, " --system" if ns.system else "", " --offline" if ns.offline else "",
             " --dry-run" if ns.dry_run else "")
    report = lapply.run(ns.mode, ctx=ctx, system=True, offline=bool(ns.offline), dry_run=bool(ns.dry_run))
    if not ns.system and not ns.dry_run and not ns.json:
        report.add("note", True, "--system is implied: lindos-tune only has a system-wide part (the per-user part is lindos-mode)")
    return _emit_report(report, ns.json)


def cmd_services(ns: argparse.Namespace) -> int:
    from . import privileged, services as lservices

    ctx = _context(ns)
    if ns.action == "list":
        rows = lservices.list_units(ctx)
        if ns.json:
            _print(json.dumps({"units": rows, "whitelist": common.SERVICES_WHITELIST}, indent=2, sort_keys=True))
        else:
            _print(lservices.render_units(rows))
        return EXIT_OK
    units = list(ns.units or [])
    if not units:
        sys.stderr.write(f"{PROG}: services {ns.action} needs at least one <unit>\n")
        return EXIT_USAGE
    try:
        units = lservices.check_units(units, ctx=ctx)
    except lservices.ServiceError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        if ns.json:
            _print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return EXIT_ERROR
    if _needs_escalation(ns.dry_run):
        req = privileged.Request.services(enable=units if ns.action == "enable" else [],
                                          disable=units if ns.action == "disable" else [])
        return _escalate(req, ns.json, ns.verbose)
    report = Report(title=f"services {ns.action} {' '.join(units)}", dry_run=ctx.dry_run)
    for unit in units:
        if ns.action == "enable":
            lservices.enable(ctx, unit, report)
            lservices.sync_conditional_autostart(ctx, report, unit, disabled=False)
        else:
            lservices.disable(ctx, unit, report)
            lservices.sync_conditional_autostart(ctx, report, unit, disabled=True)
    return _emit_report(report, ns.json)


def cmd_zram(ns: argparse.Namespace) -> int:
    from . import privileged, zram as lzram

    percent = ns.percent
    if not 0 <= percent <= 200:
        sys.stderr.write(f"{PROG}: percent must be between 0 and 200\n")
        return EXIT_USAGE
    if _needs_escalation(ns.dry_run):
        return _escalate(privileged.Request.zram(percent), ns.json, ns.verbose)
    ctx = _context(ns)
    return _emit_report(lzram.set_percent(ctx, percent), ns.json)


def cmd_governor(ns: argparse.Namespace) -> int:
    from . import governor as lgovernor, privileged

    if ns.governor not in lgovernor.GOVERNORS:
        sys.stderr.write(f"{PROG}: unknown governor '{ns.governor}' (choose from {', '.join(lgovernor.GOVERNORS)})\n")
        return EXIT_USAGE
    if _needs_escalation(ns.dry_run):
        return _escalate(privileged.Request.governor(ns.governor), ns.json, ns.verbose)
    ctx = _context(ns)
    return _emit_report(lgovernor.apply(ctx, ns.governor, persist=True, prefer_ppd=True), ns.json)


def cmd_fan(ns: argparse.Namespace) -> int:
    from . import fan as lfan, privileged

    ctx = _context(ns)
    if ns.action == "list":
        data = lfan.list_fans(ctx)
        _print(json.dumps(data, indent=2, sort_keys=True) if ns.json else lfan.render_table(data))
        return EXIT_OK
    if not ns.profile:
        sys.stderr.write(f"{PROG}: fan set needs a <profile> ({' | '.join(lfan.PROFILES)})\n")
        return EXIT_USAGE
    prof = lfan.normalize_profile(ns.profile)
    if prof is None:
        sys.stderr.write(f"{PROG}: unknown fan profile '{ns.profile}' (choose from {', '.join(lfan.PROFILES)})\n")
        return EXIT_USAGE
    if _needs_escalation(ns.dry_run) and (lfan.nbfc_available(ctx) or lfan.thinkpad_available(ctx)):
        return _escalate(privileged.Request.fan(prof), ns.json, ns.verbose)
    # no controllable fans: answer honestly without asking for a password
    return _emit_report(lfan.set_profile(ctx, prof), ns.json)


def cmd_power(ns: argparse.Namespace) -> int:
    from . import power as lpower, privileged

    prof = lpower.normalize(ns.profile or "")
    if prof is None:
        sys.stderr.write(f"{PROG}: unknown power profile '{ns.profile}' (choose from {', '.join(lpower.PROFILES)})\n")
        return EXIT_USAGE
    ctx = _context(ns)
    backend = lpower.backend(ctx)
    if backend != "ppd" and _needs_escalation(ns.dry_run):
        if backend == "tlp":
            sys.stderr.write(f"{PROG}: TLP detected — full TLP profile switching needs root (sudo lindos-tune power {prof}); "
                             "applying the cpufreq governor through the Lindos helper instead\n")
        return _escalate(privileged.Request.power(prof), ns.json, ns.verbose)
    return _emit_report(lpower.set_profile(ctx, prof), ns.json)


def cmd_sched(ns: argparse.Namespace) -> int:
    from . import privileged, sched as lsched

    ctx = _context(ns)
    if ns.action == "list":
        data = lsched.list_schedulers(ctx)
        _print(json.dumps(data, indent=2, sort_keys=True) if ns.json else lsched.render_list(data))
        return EXIT_OK
    if ns.action == "status":
        data = lsched.status(ctx)
        _print(json.dumps(data, indent=2, sort_keys=True) if ns.json else lsched.render_status(data))
        return EXIT_OK
    # set
    if not ns.profile:
        sys.stderr.write(f"{PROG}: sched set needs a <profile> ({' | '.join(lsched.SETTABLE_PROFILES)})\n")
        return EXIT_USAGE
    if ns.profile not in lsched.SETTABLE_PROFILES:
        sys.stderr.write(f"{PROG}: unknown scheduler '{ns.profile}' "
                         f"(choose from {', '.join(lsched.SETTABLE_PROFILES)})\n")
        return EXIT_USAGE
    if not lsched.sched_ext_supported(ctx):
        msg = ("running kernel has no sched_ext (CONFIG_SCHED_CLASS_EXT) — "
               "install lindos-kernel and reboot into it, then retry")
        if ns.json:
            _print(json.dumps({"ok": False, "code": EXIT_NO_SCHED_EXT, "error": msg,
                               "profile": ns.profile}, indent=2, sort_keys=True))
        else:
            sys.stderr.write(f"{PROG}: {msg}\n")
        return EXIT_NO_SCHED_EXT
    if _needs_escalation(ns.dry_run):
        return _escalate(privileged.Request.sched(ns.profile), ns.json, ns.verbose)
    return _emit_report(lsched.set_scheduler(ctx, ns.profile), ns.json)


def cmd_report(ns: argparse.Namespace) -> int:
    from . import report as lreport

    text = lreport.run_report(_context(ns), output=ns.output)
    if ns.output and ns.output != "-":
        _print(f"report written to {ns.output}")
    else:
        _print(text)
    return EXIT_OK


# --- parser ----------------------------------------------------------------------------------------
def _add_common(sp: argparse.ArgumentParser, *, dry_run: bool = True, as_json: bool = True) -> None:
    if dry_run:
        sp.add_argument("--dry-run", action="store_true", help="show what would be done; touch nothing (no root needed)")
    if as_json:
        sp.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG,
                                     description="Lindos RAM / performance / hardware tuning (SPEC §11). "
                                                 "Idle RAM target 350–500 MB (Lite 300–380 MB).")
    parser.add_argument("--version", action="version", version=f"{PROG} {_VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging on stderr")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("status", help="RAM used/available, zram, top 10 RSS, units, governor, compositor, verdict")
    _add_common(p, dry_run=False)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("apply", help="apply the base tune + mode overrides (sysctl, zram, earlyoom, journald, tmp, presets, services, governor, ananicy)")
    p.add_argument("--mode", required=True, metavar="<id>", help="mode id: " + ", ".join(common.MODE_IDS))
    p.add_argument("--system", action="store_true", help="apply the system-wide tune (implied; kept for SPEC parity)")
    p.add_argument("--offline", action="store_true", help="never start/restart units or touch the network (ISO chroot)")
    _add_common(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("services", help="list / enable / disable whitelisted units")
    p.add_argument("action", choices=("list", "disable", "enable"))
    p.add_argument("units", nargs="*", metavar="<unit>", help="unit name(s), e.g. bluetooth.service")
    _add_common(p)
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("zram", help="set zram size as percent of RAM (0 = off); zram-generator or zram-tools")
    p.add_argument("percent", type=int, metavar="<percent>")
    _add_common(p)
    p.set_defaults(func=cmd_zram)

    p = sub.add_parser("governor", help="set the cpufreq governor (+EPP, persisted; power-profiles-daemon when present)")
    p.add_argument("governor", metavar="<g>", help="schedutil | performance | powersave | ondemand | conservative | userspace")
    _add_common(p)
    p.set_defaults(func=cmd_governor)

    p = sub.add_parser("fan", help="list fans/temperatures or set a fan profile (nbfc-linux / thinkpad_acpi)")
    p.add_argument("action", choices=("list", "set"))
    p.add_argument("profile", nargs="?", metavar="<profile>", help="auto | quiet | balanced | max (for 'set')")
    _add_common(p)
    p.set_defaults(func=cmd_fan)

    p = sub.add_parser("power", help="power profile: powerprofilesctl → tlp → governor fallback")
    p.add_argument("profile", metavar="<profile>", help="performance | balanced | power-saver")
    _add_common(p)
    p.set_defaults(func=cmd_power)

    p = sub.add_parser("sched", help="sched_ext / SCX schedulers: list available, show active, or set one (scx_lavd, scx_bpfland, scx_flash, scx_rustland, none)")
    p.add_argument("action", choices=("list", "status", "set"))
    p.add_argument("profile", nargs="?", metavar="<profile>",
                   help="scx_lavd | scx_bpfland | scx_flash | scx_rustland | none (for 'set')")
    _add_common(p)
    p.set_defaults(func=cmd_sched)

    p = sub.add_parser("report", help="Markdown report for bug reports (RAM, zram, top RSS, units, kernel, mode, GPU, config)")
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: 0 for --help/--version, 2 for usage errors
        code = exc.code
        return int(code) if isinstance(code, int) else EXIT_USAGE
    _setup_logging(bool(ns.verbose))
    try:
        return int(ns.func(ns))
    except KeyboardInterrupt:
        sys.stderr.write(f"{PROG}: interrupted\n")
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # never a traceback for the user; -v shows it
        log.exception("unhandled error")
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return EXIT_ERROR


__all__ = ["PROG", "build_parser", "main"]
