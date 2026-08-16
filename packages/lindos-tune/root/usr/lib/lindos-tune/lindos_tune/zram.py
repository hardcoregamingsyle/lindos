"""zram configuration for both Debian/Ubuntu back-ends (SPEC §11).

* **systemd-zram-generator** — detected via ``/usr/lib/systemd/system-generators/zram-generator``
  (or the dpkg info file); configured through ``/etc/systemd/zram-generator.conf``::

      [zram0]
      zram-size = ram * 0.50
      compression-algorithm = zstd
      swap-priority = 100

  ``zram-size`` accepts an arithmetic expression over the ``ram`` variable (MB), so a percent
  becomes ``ram * <fraction>``.  Activated by ``systemctl daemon-reload`` +
  ``systemctl restart systemd-zram-setup@zram0.service`` on a live system.
* **zram-tools** — detected via ``/usr/sbin/zramswap`` / ``zramswap.service``; configured through
  ``/etc/default/zramswap`` (``ALGO=zstd``, ``PERCENT=<n>``, ``PRIORITY=100``); activated by
  ``systemctl restart zramswap.service``.

Neither present → the step is recorded as *skipped* with an install hint.  Tests inject the
back-end via ``ctx.overrides["zram_backend"]``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from . import common
from .common import Context, Report

BACKEND_GENERATOR = "zram-generator"
BACKEND_ZRAMTOOLS = "zram-tools"
BACKENDS = (BACKEND_GENERATOR, BACKEND_ZRAMTOOLS)

GENERATOR_PATHS = (
    "/usr/lib/systemd/system-generators/zram-generator",
    "/lib/systemd/system-generators/zram-generator",
    "/var/lib/dpkg/info/systemd-zram-generator.list",
)
ZRAMTOOLS_PATHS = (
    "/usr/sbin/zramswap",
    "/usr/lib/systemd/system/zramswap.service",
    "/lib/systemd/system/zramswap.service",
    "/var/lib/dpkg/info/zram-tools.list",
)
GENERATOR_UNIT = "systemd-zram-setup@zram0.service"
ZRAMTOOLS_UNIT = "zramswap.service"
DEFAULT_ALGO = "zstd"
DEFAULT_PRIORITY = 100
MIN_PERCENT = 0
MAX_PERCENT = 200
INSTALL_HINT = "install 'systemd-zram-generator' (preferred) or 'zram-tools': apt install systemd-zram-generator"

MANAGED_HEADER = "# Managed by lindos-tune — edit with: lindos-tune zram <percent> (or lindos-tune apply --mode …)"


def clamp_percent(percent: Any) -> int:
    try:
        value = int(percent)
    except (TypeError, ValueError):
        value = 50
    return common.clamp(value, MIN_PERCENT, MAX_PERCENT)


def detect_backend(ctx: Context) -> Optional[str]:
    """Which zram back-end is installed under ``ctx.root`` (override: ``ctx.overrides["zram_backend"]``)."""
    override = ctx.overrides.get("zram_backend", "missing")
    if override != "missing":
        return override if override in BACKENDS else None
    for candidate in GENERATOR_PATHS:
        if ctx.exists(candidate):
            return BACKEND_GENERATOR
    for candidate in ZRAMTOOLS_PATHS:
        if ctx.exists(candidate):
            return BACKEND_ZRAMTOOLS
    if not ctx.root and ctx.which("zramswap"):
        return BACKEND_ZRAMTOOLS
    return None


def planned(ctx: Context, percent: int) -> bool:
    """Will zram be active after apply (back-end present and percent > 0)?"""
    return clamp_percent(percent) > 0 and detect_backend(ctx) is not None


# --- rendering ------------------------------------------------------------------------------------
def render_generator_conf(percent: int, algo: str = DEFAULT_ALGO, priority: int = DEFAULT_PRIORITY) -> str:
    fraction = clamp_percent(percent) / 100.0
    return (
        f"{MANAGED_HEADER}\n"
        "# systemd-zram-generator(8): zram-size is an expression over `ram` (MemTotal in MB).\n"
        "[zram0]\n"
        f"zram-size = ram * {fraction:.2f}\n"
        f"compression-algorithm = {algo}\n"
        f"swap-priority = {priority}\n"
    )


def render_zramswap_default(percent: int, algo: str = DEFAULT_ALGO, priority: int = DEFAULT_PRIORITY) -> str:
    return (
        f"{MANAGED_HEADER}\n"
        "# zram-tools: /usr/sbin/zramswap reads this file (see zramswap(8)).\n"
        f"ALGO={algo}\n"
        f"PERCENT={clamp_percent(percent)}\n"
        f"PRIORITY={priority}\n"
    )


_SIZE_RE = re.compile(r"^\s*zram-size\s*=\s*ram\s*\*\s*([0-9.]+)\s*$", re.M)
_ZRAM_FRACTION_RE = re.compile(r"^\s*zram-fraction\s*=\s*([0-9.]+)\s*$", re.M)


def parse_generator_percent(text: Optional[str]) -> Optional[int]:
    """Best-effort: percent encoded in an existing zram-generator.conf (our format or zram-fraction)."""
    if not text:
        return None
    m = _SIZE_RE.search(text) or _ZRAM_FRACTION_RE.search(text)
    if not m:
        return None
    try:
        return int(round(float(m.group(1)) * 100))
    except ValueError:
        return None


def parse_zramswap_percent(text: Optional[str]) -> Optional[int]:
    value = common.parse_kv(text).get("PERCENT")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def current_config(ctx: Context) -> Dict[str, Any]:
    """What is configured on disk (not necessarily active)."""
    backend = detect_backend(ctx)
    info: Dict[str, Any] = {"backend": backend, "percent": None, "path": None}
    if backend == BACKEND_GENERATOR:
        info["path"] = common.ZRAM_GENERATOR_CONF
        info["percent"] = parse_generator_percent(common.read_text(ctx.path(common.ZRAM_GENERATOR_CONF)))
    elif backend == BACKEND_ZRAMTOOLS:
        info["path"] = common.ZRAMSWAP_DEFAULT
        info["percent"] = parse_zramswap_percent(common.read_text(ctx.path(common.ZRAMSWAP_DEFAULT)))
    return info


# --- apply ----------------------------------------------------------------------------------------
def configure(ctx: Context, percent: int, report: Optional[Report] = None, *, backend: Optional[str] = None,
              algo: str = DEFAULT_ALGO, priority: int = DEFAULT_PRIORITY) -> Report:
    """Write the back-end config for *percent* and (when live) restart the zram unit.

    ``percent == 0`` disables zram (config removed / ``PERCENT=0``) and stops the unit.
    Every action is recorded in *report*; the function never raises.
    """
    report = report or Report(title=f"zram {percent}%")
    percent = clamp_percent(percent)
    backend = backend or detect_backend(ctx)
    if backend is None:
        report.skip("zram", f"no zram back-end installed — {INSTALL_HINT}")
        return report

    if backend == BACKEND_GENERATOR:
        conf = common.ZRAM_GENERATOR_CONF
        unit = GENERATOR_UNIT
        if percent > 0:
            ok, changed = ctx.write(conf, render_generator_conf(percent, algo, priority))
        else:
            ok, changed = _write_disabled_generator(ctx, conf)
    else:
        conf = common.ZRAMSWAP_DEFAULT
        unit = ZRAMTOOLS_UNIT
        ok, changed = ctx.write(conf, render_zramswap_default(percent, algo, priority))
    verb = "would write" if ctx.dry_run else ("wrote" if changed else "unchanged")
    if not ok:
        report.add("zram-config", False, f"cannot write {conf}")
        return report
    report.add("zram-config", True, f"{verb} {conf} ({backend}, {percent}% of RAM, {algo}, prio {priority})")

    # unit handling
    if backend == BACKEND_ZRAMTOOLS and ctx.can_systemctl:
        if percent > 0:
            res = ctx.systemctl("enable", ZRAMTOOLS_UNIT)
            report.add("zram-enable", res.ok or ctx.dry_run, res.tail() if not res.ok else "zramswap.service enabled")
        else:
            res = ctx.systemctl("disable", ZRAMTOOLS_UNIT)
            report.add("zram-disable", res.ok or ctx.dry_run, res.tail() if not res.ok else "zramswap.service disabled")
    if not ctx.live:
        why = "dry-run" if ctx.dry_run else ("offline/chroot" if (ctx.offline or ctx.chroot) else "no running systemd")
        report.skip("zram-restart", f"{why}: {unit} takes effect at next boot")
        return report
    if backend == BACKEND_GENERATOR:
        ctx.systemctl("daemon-reload")
        if percent > 0:
            res = ctx.systemctl("restart", unit, timeout=120)
        else:
            res = ctx.systemctl("stop", unit, timeout=120)
            if res.ok and ctx.which("swapoff") and ctx.exists("/dev/zram0"):
                ctx.run(["swapoff", "/dev/zram0"], timeout=120)
    else:
        res = ctx.systemctl("restart" if percent > 0 else "stop", unit, timeout=120)
    report.add("zram-restart", res.ok, res.tail() if not res.ok else f"{unit} {'restarted' if percent > 0 else 'stopped'}")
    return report


def _write_disabled_generator(ctx: Context, conf: str) -> Tuple[bool, bool]:
    """A ``[zram0]`` section with ``zram-size = 0`` disables the device but keeps the file managed."""
    content = (f"{MANAGED_HEADER}\n# zram disabled by lindos-tune (percent 0)\n[zram0]\nzram-size = 0\n")
    return ctx.write(conf, content)


def set_percent(ctx: Context, percent: int) -> Report:
    """``lindos-tune zram <percent>``: configure and remember in the tune state file."""
    report = configure(ctx, percent, Report(title=f"zram {clamp_percent(percent)}%", dry_run=ctx.dry_run))
    if report.ok and not ctx.dry_run:
        state = common.read_json(ctx.path(common.STATE_FILE))
        state["zram_percent"] = clamp_percent(percent)
        state["zram_backend"] = detect_backend(ctx)
        common.write_json(ctx.path(common.STATE_FILE), state)
    return report


__all__ = [
    "BACKEND_GENERATOR", "BACKEND_ZRAMTOOLS", "BACKENDS", "GENERATOR_UNIT", "ZRAMTOOLS_UNIT",
    "DEFAULT_ALGO", "DEFAULT_PRIORITY", "INSTALL_HINT", "clamp_percent", "detect_backend", "planned",
    "render_generator_conf", "render_zramswap_default", "parse_generator_percent",
    "parse_zramswap_percent", "current_config", "configure", "set_percent",
]
