"""``lindos-tune sched list|status|set <profile>`` — sched_ext / SCX schedulers (SPEC-KERNEL §16, §18).

The pluggable BPF schedulers of ``sched_ext`` (``CONFIG_SCHED_CLASS_EXT``, kernel ≥ 6.12,
shipped by the ``lindos-kernel`` build) give low-latency gaming without a custom scheduler in the
mainline tree.  This module

* detects which SCX schedulers are **available** — the ``scx_loader`` D-Bus service when present
  (queried through ``busctl``), else the ``scx_*`` binaries on ``PATH``, else the built-in known
  list marked *not installed*;
* reports the **active** scheduler by reading ``/sys/kernel/sched_ext/state`` +
  ``/sys/kernel/sched_ext/root/ops`` (or ``scx_loader``'s ``CurrentScheduler``);
* **sets** one — through the privileged helper action ``set-sched`` — by driving ``scx_loader``
  when present, else the documented systemd fallback (``/etc/default/scx`` + ``scx.service`` from
  the ``scx-scheds`` package).  ``set none`` stops any running scx scheduler.

Every path goes through :func:`lindos_tune.common.path` so tests can point ``LINDOS_ROOT`` at a
fake ``/sys``/``/boot``/``/proc`` tree; every sub-process goes through the injected
:class:`~lindos_tune.common.Context` runner.  Nothing runs at import time and the module imports
on any OS (all Linux calls are guarded).
"""

from __future__ import annotations

import gzip
import os
import re
from typing import Any, Dict, List, Optional

from . import common
from .common import Context, Report

#: the profiles ``lindos-tune sched set`` accepts (SPEC-KERNEL §16) — ``none`` stops scx.
SETTABLE_PROFILES = ("scx_lavd", "scx_bpfland", "scx_flash", "scx_rustland", "none")
#: SCX schedulers we know about (for ``list`` when nothing is installed to probe).
KNOWN_SCHEDULERS = ("scx_lavd", "scx_bpfland", "scx_flash", "scx_rustland",
                    "scx_rusty", "scx_simple", "scx_central", "scx_nest", "scx_layered")

CONFIG_SYMBOL = "CONFIG_SCHED_CLASS_EXT"
SCHED_EXT_SYS = "/sys/kernel/sched_ext"
SCX_DEFAULT = "/etc/default/scx"
SCX_UNIT = "scx.service"
BOOT_CONFIG = "/boot/config-{release}"
PROC_CONFIG_GZ = "/proc/config.gz"
OSRELEASE = "/proc/sys/kernel/osrelease"

#: scx_loader D-Bus (org.scx.Loader) — object, interface, StartScheduler sched-mode "Auto".
LOADER_BUS = "org.scx.Loader"
LOADER_OBJ = "/org/scx/Loader"
LOADER_MODE_AUTO = "0"


# --- kernel release / config --------------------------------------------------------------------
def kernel_release(ctx: Context) -> str:
    text = common.read_first_line(ctx.path(OSRELEASE))
    if text:
        return text
    if not ctx.root:
        try:
            return os.uname().release  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return ""
    return ""


def _read_config_symbol(ctx: Context, symbol: str) -> Optional[bool]:
    """``True``/``False`` if *symbol* is set/unset in the running kernel config, ``None`` if the
    config is not readable (no ``/boot/config-*`` and no ``/proc/config.gz``)."""
    want_y = re.compile(r"^" + re.escape(symbol) + r"=(y|m)\b", re.M)
    want_n = re.compile(r"^(# " + re.escape(symbol) + r" is not set|" + re.escape(symbol) + r"=n)\b", re.M)
    boot = ctx.path(BOOT_CONFIG.format(release=kernel_release(ctx))) if kernel_release(ctx) else ""
    text = common.read_text(boot) if boot else None
    if text is None:
        gz = ctx.path(PROC_CONFIG_GZ)
        if os.path.exists(gz):
            try:
                with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = None
    if text is None:
        return None
    if want_y.search(text):
        return True
    if want_n.search(text):
        return False
    return False


def sched_ext_supported(ctx: Context) -> bool:
    """Does the **running** kernel provide sched_ext (``CONFIG_SCHED_CLASS_EXT``)?

    Strongest signal first: the ``/sys/kernel/sched_ext`` directory exists only on a kernel that
    was built with sched_ext; otherwise fall back to the kernel config.  When neither can be read
    (e.g. a Windows dev box with no ``LINDOS_ROOT`` fixture) we answer ``False`` — honest, and it
    makes ``set`` refuse with exit 3 rather than pretend.
    """
    if os.path.isdir(ctx.path(SCHED_EXT_SYS)):
        return True
    return _read_config_symbol(ctx, CONFIG_SYMBOL) is True


# --- state --------------------------------------------------------------------------------------
def sched_ext_state(ctx: Context) -> Dict[str, Any]:
    """``{"present", "state", "scheduler"}`` from ``/sys/kernel/sched_ext`` (guarded)."""
    present = os.path.isdir(ctx.path(SCHED_EXT_SYS))
    state = common.read_first_line(ctx.path(SCHED_EXT_SYS + "/state")) or None
    ops = common.read_first_line(ctx.path(SCHED_EXT_SYS + "/root/ops")) or None
    return {"present": present, "state": state, "scheduler": ops}


def _loader_present(ctx: Context) -> bool:
    if ctx.which("scx_loader"):
        return True
    if not (ctx.root and not ctx.commands_allowed) and ctx.which("busctl"):
        res = ctx.run(["busctl", "--system", "list", "--no-pager"], timeout=10)
        return res.ok and LOADER_BUS in res.out
    return False


def _busctl_strings(text: str) -> List[str]:
    """Parse ``as 4 "scx_lavd" "scx_flash" …`` (busctl string-array) → ``[names]``."""
    return re.findall(r'"([^"]+)"', text or "")


def _loader_supported(ctx: Context) -> List[str]:
    if ctx.root and not ctx.commands_allowed:
        return []
    if not ctx.which("busctl"):
        return []
    res = ctx.run(["busctl", "--system", "get-property", LOADER_BUS, LOADER_OBJ, LOADER_BUS,
                   "SupportedSchedulers"], timeout=10)
    return _busctl_strings(res.out) if res.ok else []


def _loader_current(ctx: Context) -> Optional[str]:
    if ctx.root and not ctx.commands_allowed:
        return None
    if not ctx.which("busctl"):
        return None
    res = ctx.run(["busctl", "--system", "get-property", LOADER_BUS, LOADER_OBJ, LOADER_BUS,
                   "CurrentScheduler"], timeout=10)
    if not res.ok:
        return None
    names = _busctl_strings(res.out)
    name = names[0] if names else ""
    return name or None


def active_scheduler(ctx: Context) -> Optional[str]:
    """The running scx scheduler name, or ``None`` when the default CFS/EEVDF is in charge."""
    st = sched_ext_state(ctx)
    if st["scheduler"] and (st["state"] in (None, "enabled") or st["state"] != "disabled"):
        if st["state"] and st["state"].lower().startswith("disab"):
            return None
        return st["scheduler"]
    if _loader_present(ctx):
        return _loader_current(ctx)
    return None


def list_schedulers(ctx: Context) -> Dict[str, Any]:
    """Available scx schedulers with their source (``scx_loader`` / ``PATH`` / ``known``)."""
    active = active_scheduler(ctx)
    source = "known"
    available: Dict[str, bool] = {}
    loader = _loader_present(ctx)
    if loader:
        for name in _loader_supported(ctx):
            available[name] = True
        source = "scx_loader"
    if not available:
        for name in KNOWN_SCHEDULERS:
            if ctx.which(name):
                available[name] = True
        if available:
            source = "PATH"
    names = sorted(set(KNOWN_SCHEDULERS) | set(available))
    schedulers = [{"name": n, "installed": bool(available.get(n)),
                   "active": n == active, "settable": n in SETTABLE_PROFILES} for n in names]
    return {"source": source, "loader": loader, "active": active,
            "sched_ext": sched_ext_supported(ctx), "schedulers": schedulers}


def status(ctx: Context) -> Dict[str, Any]:
    st = sched_ext_state(ctx)
    return {"sched_ext": sched_ext_supported(ctx), "present": st["present"], "state": st["state"],
            "active": active_scheduler(ctx), "loader": _loader_present(ctx),
            "settable": list(SETTABLE_PROFILES)}


# --- rendering ----------------------------------------------------------------------------------
def render_scx_default(profile: str) -> str:
    sched = "" if profile == "none" else profile
    return ("# /etc/default/scx — managed by lindos-tune (lindos-tune sched set <profile>)\n"
            "# Read by scx.service (scx-scheds package). Empty SCX_SCHEDULER = no scx scheduler.\n"
            f"SCX_SCHEDULER={sched}\n"
            "# SCX_FLAGS='-m all'\n")


def render_list(data: Dict[str, Any]) -> str:
    lines = [f"sched_ext: {'available' if data['sched_ext'] else 'NOT available (install lindos-kernel)'}"
             f" — source: {data['source']}"]
    active = data.get("active")
    lines.append(f"active scheduler: {active or 'none (default CFS/EEVDF)'}")
    lines.append("")
    for sch in data["schedulers"]:
        mark = "*" if sch["active"] else " "
        state = "installed" if sch["installed"] else "not installed"
        tag = " [settable]" if sch["settable"] else ""
        lines.append(f" {mark} {sch['name']:<14} {state}{tag}")
    lines.append("")
    lines.append("set one with: lindos-tune sched set <" + "|".join(SETTABLE_PROFILES) + ">")
    return "\n".join(lines)


def render_status(data: Dict[str, Any]) -> str:
    lines = [f"sched_ext kernel support: {'yes' if data['sched_ext'] else 'no'}",
             f"active scheduler: {data['active'] or 'none (default CFS/EEVDF)'}"]
    if data.get("state"):
        lines.append(f"sched_ext state: {data['state']}")
    lines.append(f"scx_loader: {'present' if data['loader'] else 'absent'}")
    return "\n".join(lines)


# --- apply --------------------------------------------------------------------------------------
def _systemd_set(ctx: Context, profile: str, report: Report) -> None:
    if not ctx.can_systemctl:
        report.skip("sched", "systemctl not available")
        return
    if not ctx.unit_exists(SCX_UNIT) and not ctx.dry_run:
        report.skip("sched", "scx-scheds not installed (apt install scx-scheds) — no scx.service")
        return
    if profile == "none":
        args = ["disable"] + (["--now"] if ctx.live else []) + [SCX_UNIT]
        res = ctx.systemctl(*args, timeout=60)
        report.add("sched", res.ok, ("would stop" if ctx.dry_run else "stopped") + " scx.service (default scheduler)"
                   if res.ok else res.tail())
        return
    if ctx.live:
        res = ctx.systemctl("enable", "--now", SCX_UNIT, timeout=60)
        report.add("sched", res.ok, f"enabled + started scx.service ({profile})" if res.ok else res.tail())
    else:
        res = ctx.systemctl("enable", SCX_UNIT, timeout=60)
        report.add("sched", res.ok, ("would enable" if ctx.dry_run else "enabled") + f" scx.service ({profile}, starts at boot)"
                   if res.ok else res.tail())


def _loader_set(ctx: Context, profile: str, report: Report) -> None:
    if not ctx.which("busctl"):
        _systemd_set(ctx, profile, report)
        return
    if profile == "none":
        res = ctx.run(["busctl", "--system", "call", LOADER_BUS, LOADER_OBJ, LOADER_BUS, "StopScheduler"], timeout=30)
        report.add("sched", res.ok, "scx_loader: stopped scheduler" if res.ok else res.tail())
    else:
        res = ctx.run(["busctl", "--system", "call", LOADER_BUS, LOADER_OBJ, LOADER_BUS,
                       "StartScheduler", "su", profile, LOADER_MODE_AUTO], timeout=30)
        report.add("sched", res.ok, f"scx_loader: started {profile}" if res.ok else res.tail())


def set_scheduler(ctx: Context, profile: str, report: Optional[Report] = None) -> Report:
    """Apply *profile* (write ``/etc/default/scx`` then drive scx_loader / scx.service).

    In an ``apply --mode`` context a missing sched_ext or missing scx-scheds is a **skip**, never a
    failure (SPEC-KERNEL §16: absent features degrade to a logged skip).  The explicit
    ``sched set`` command handles the "no sched_ext" case itself with exit 3 before calling here.
    """
    report = report or Report(title=f"sched set {profile}", dry_run=ctx.dry_run)
    if profile not in SETTABLE_PROFILES:
        report.add("sched", False, f"unknown profile {profile!r} (choose from {', '.join(SETTABLE_PROFILES)})")
        return report
    if not sched_ext_supported(ctx):
        report.skip("sched", "running kernel has no sched_ext (CONFIG_SCHED_CLASS_EXT) — install lindos-kernel")
        return report
    ok, changed = ctx.write(SCX_DEFAULT, render_scx_default(profile))
    verb = "would write" if ctx.dry_run else ("wrote" if changed else "unchanged")
    report.add("scx-config", ok, f"{verb} {SCX_DEFAULT} (SCX_SCHEDULER={'' if profile == 'none' else profile})")
    if _loader_present(ctx):
        _loader_set(ctx, profile, report)
    else:
        _systemd_set(ctx, profile, report)
    return report


__all__ = [
    "SETTABLE_PROFILES", "KNOWN_SCHEDULERS", "CONFIG_SYMBOL", "SCHED_EXT_SYS", "SCX_DEFAULT", "SCX_UNIT",
    "kernel_release", "sched_ext_supported", "sched_ext_state", "active_scheduler", "list_schedulers",
    "status", "render_scx_default", "render_list", "render_status", "set_scheduler",
]
