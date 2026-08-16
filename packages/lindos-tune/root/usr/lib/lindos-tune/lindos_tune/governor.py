"""CPU frequency governor + energy-performance preference (SPEC §11).

Three layers, used in this order of preference:

1. **power-profiles-daemon** present → ``powerprofilesctl set <profile>`` (performance for the
   ``performance`` governor, ``power-saver`` for ``powersave``, ``balanced`` otherwise).  PPD
   owns the governor/EPP on such systems and persists its own choice, so we do **not** fight it
   with a tmpfiles line (an old ``lindos-governor.conf`` is removed).
2. Otherwise write ``/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`` directly (mapped
   onto what the driver offers — intel_pstate has no ``schedutil``), set the intel_pstate /
   amd_pstate EPP through ``energy_performance_preference`` when available, and persist with
   ``/etc/tmpfiles.d/lindos-governor.conf`` (``w`` lines with globs, applied at every boot by
   ``systemd-tmpfiles-setup``).
3. Nothing writable (VM without cpufreq) → recorded as skipped, never an error.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional

from . import common
from .common import Context, Report

GOVERNORS = ("schedutil", "performance", "powersave", "ondemand", "conservative", "userspace")
EPP_FOR_GOVERNOR = {
    "performance": "performance",
    "powersave": "power",
    "schedutil": "balance_performance",
    "ondemand": "balance_performance",
    "conservative": "balance_power",
}
PPD_FOR_GOVERNOR = {"performance": "performance", "powersave": "power-saver"}
FALLBACKS = {
    "schedutil": ["powersave", "ondemand", "conservative"],   # intel_pstate: powersave == dynamic
    "performance": ["performance"],
    "powersave": ["powersave", "conservative", "ondemand", "schedutil"],
    "ondemand": ["schedutil", "powersave"],
    "conservative": ["powersave", "schedutil"],
    "userspace": ["userspace"],
}
TMPFILES_HEADER = "# Managed by lindos-tune — CPU governor/EPP applied at boot by systemd-tmpfiles.\n"


def _cpu0(ctx: Context) -> str:
    return os.path.join(ctx.path(common.CPU_DIR), "cpu0", "cpufreq")


def available(ctx: Context) -> List[str]:
    return common.read_first_line(os.path.join(_cpu0(ctx), "scaling_available_governors")).split()


def current(ctx: Context) -> Optional[str]:
    return common.read_first_line(os.path.join(_cpu0(ctx), "scaling_governor")) or None


def driver(ctx: Context) -> Optional[str]:
    return common.read_first_line(os.path.join(_cpu0(ctx), "scaling_driver")) or None


def epp_available(ctx: Context) -> List[str]:
    return common.read_first_line(os.path.join(_cpu0(ctx), "energy_performance_available_preferences")).split()


def epp_current(ctx: Context) -> Optional[str]:
    return common.read_first_line(os.path.join(_cpu0(ctx), "energy_performance_preference")) or None


def governor_paths(ctx: Context) -> List[str]:
    return sorted(glob.glob(os.path.join(ctx.path(common.CPU_DIR), "cpu[0-9]*", "cpufreq", "scaling_governor")))


def epp_paths(ctx: Context) -> List[str]:
    return sorted(glob.glob(os.path.join(ctx.path(common.CPU_DIR), "cpu[0-9]*", "cpufreq", "energy_performance_preference")))


def map_governor(governor: str, avail: Optional[List[str]]) -> Optional[str]:
    """Map the requested governor onto one the driver offers (``None`` = impossible)."""
    if not avail:
        return governor
    if governor in avail:
        return governor
    for candidate in FALLBACKS.get(governor, []):
        if candidate in avail:
            return candidate
    return None


def ppd_available(ctx: Context) -> bool:
    if ctx.overrides.get("ppd") is not None:
        return bool(ctx.overrides["ppd"])
    if ctx.root and not ctx.commands_allowed:
        return False
    return ctx.which("powerprofilesctl") is not None and (ctx.unit_exists("power-profiles-daemon.service") or not ctx.root)


def ppd_profile_for(governor: str) -> str:
    return PPD_FOR_GOVERNOR.get(governor, "balanced")


def render_tmpfiles(governor: str, epp: Optional[str]) -> str:
    lines = [TMPFILES_HEADER,
             f"w /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor - - - - {governor}\n"]
    if epp:
        lines.append(f"w /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference - - - - {epp}\n")
    return "".join(lines)


def info(ctx: Context) -> Dict[str, Any]:
    return {"current": current(ctx), "available": available(ctx), "driver": driver(ctx),
            "epp": epp_current(ctx), "epp_available": epp_available(ctx), "ppd": ppd_available(ctx)}


# --- apply ----------------------------------------------------------------------------------------
def apply(ctx: Context, governor: str, report: Optional[Report] = None, *, persist: bool = True,
          prefer_ppd: bool = True) -> Report:
    """Set *governor* now (when live) and persist it (tmpfiles or PPD).  Never raises."""
    report = report or Report(title=f"governor {governor}", dry_run=ctx.dry_run)
    if governor not in GOVERNORS:
        report.add("governor", False, f"unknown governor {governor!r} (choose from {', '.join(GOVERNORS)})")
        return report

    # 1. power-profiles-daemon
    if prefer_ppd and ppd_available(ctx):
        profile = ppd_profile_for(governor)
        stale = ctx.path(common.TMPFILES_GOVERNOR)
        if os.path.exists(stale) and not ctx.dry_run:
            try:
                os.unlink(stale)
                report.add("governor-tmpfiles", True, f"removed {common.TMPFILES_GOVERNOR} (power-profiles-daemon owns the governor)")
            except OSError as exc:
                report.add("governor-tmpfiles", False, f"cannot remove {common.TMPFILES_GOVERNOR}: {exc}")
        if ctx.dry_run:
            report.add("governor", True, f"would run: powerprofilesctl set {profile} (for {governor})")
            return report
        if not ctx.live:
            report.skip("governor", f"powerprofilesctl set {profile} needs a running session (offline/chroot); PPD keeps its own state")
            return report
        res = ctx.run(["powerprofilesctl", "set", profile], timeout=20)
        if res.ok:
            report.add("governor", True, f"power profile {profile} via power-profiles-daemon (for {governor})")
            return report
        report.add("governor-ppd", False, f"powerprofilesctl set {profile} failed: {res.tail()} — falling back to sysfs")

    # 2. sysfs + tmpfiles
    avail = available(ctx)
    target = map_governor(governor, avail)
    if target is None:
        report.skip("governor", f"{governor} not offered by {driver(ctx) or 'cpufreq'} (available: {' '.join(avail) or 'none'})")
        return report
    epp = EPP_FOR_GOVERNOR.get(governor)
    epp_avail = epp_available(ctx)
    epp_ok = bool(epp) and bool(epp_paths(ctx)) and (not epp_avail or epp in epp_avail)
    if persist:
        ok, changed = ctx.write(common.TMPFILES_GOVERNOR, render_tmpfiles(target, epp if epp_ok else None))
        verb = "would write" if ctx.dry_run else ("wrote" if changed else "unchanged")
        report.add("governor-tmpfiles", ok, f"{verb} {common.TMPFILES_GOVERNOR} ({target}{', EPP ' + str(epp) if epp_ok else ''})")
    paths = governor_paths(ctx)
    if not paths:
        report.skip("governor", "no cpufreq scaling_governor files (VM or unsupported driver?)")
        return report
    if not ctx.live and not ctx.overrides.get("allow_sysfs_writes"):
        why = "dry-run" if ctx.dry_run else "offline/chroot"
        report.skip("governor", f"{why}: {target} is applied at boot" + (" via tmpfiles" if persist else ""))
        return report
    written = 0
    for path in paths:
        if _write_sysfs(path, target):
            written += 1
    epp_written = 0
    if epp_ok:
        for path in epp_paths(ctx):
            if _write_sysfs(path, str(epp)):
                epp_written += 1
    if written:
        detail = f"{target} on {written} CPU(s)"
        if epp_written:
            detail += f", EPP {epp} on {epp_written}"
        if target != governor:
            detail += f" (mapped from {governor})"
        report.add("governor", True, detail)
    else:
        report.add("governor", False, "could not write scaling_governor (permission denied? run as root)")
    return report


def _write_sysfs(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        return True
    except OSError:
        return False


__all__ = [
    "GOVERNORS", "EPP_FOR_GOVERNOR", "PPD_FOR_GOVERNOR", "FALLBACKS", "available", "current", "driver",
    "epp_available", "epp_current", "governor_paths", "epp_paths", "map_governor", "ppd_available",
    "ppd_profile_for", "render_tmpfiles", "info", "apply",
]
