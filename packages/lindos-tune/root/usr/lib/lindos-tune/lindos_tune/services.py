"""``lindos-tune services list|disable|enable <unit>`` — whitelist-guarded unit control.

The whitelist lives in ``/usr/share/lindos/tune/services-whitelist.txt`` (one unit per line,
``#`` comments).  Units outside it are refused with :class:`ServiceError` — this is the
*only* set of units the CLI (and the helper action ``set-services``) may toggle.

Also here: the ``/etc/xdg/autostart`` hiding used by ``lindos-tune apply`` (mintwelcome,
mintreport tray, …) driven by ``/usr/share/lindos/tune/autostart-hide.list``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import common
from .common import Context, Report

UNIT_SUFFIXES = (".service", ".socket", ".timer", ".mount", ".target", ".path")

#: Fallback used only when the whitelist file is missing (mirrors the shipped file).
DEFAULT_WHITELIST: Tuple[str, ...] = (
    "bluetooth.service", "ModemManager.service", "avahi-daemon.service", "cups.service",
    "cups-browsed.service", "NetworkManager-wait-online.service", "apport.service",
    "whoopsie.service", "kerneloops.service", "brltty.service", "speech-dispatcher.service",
    "ubuntu-report.service", "mintreport.service", "systemd-oomd.service", "earlyoom.service",
    "fstrim.timer", "tmp.mount", "zramswap.service", "systemd-zram-setup@zram0.service",
    "ananicy-cpp.service", "power-profiles-daemon.service", "tlp.service", "thermald.service",
    "irqbalance.service", "nbfc_service.service", "fancontrol.service", "lm-sensors.service",
    "openrgb.service", "gamemoded.service", "packagekit.service", "fwupd.service",
    "fwupd-refresh.timer", "motd-news.timer", "apt-daily.timer", "apt-daily-upgrade.timer",
    "unattended-upgrades.service", "geoclue.service", "switcheroo-control.service",
    "colord.service", "upower.service", "udisks2.service", "cron.service", "anacron.service",
    "smartmontools.service", "warpinator.service", "preload.service", "haveged.service",
    "e2scrub_all.timer", "plymouth-quit-wait.service", "snapd.service", "snapd.socket",
    "ufw.service", "wpa_supplicant.service", "blueman-mechanism.service", "cups.socket",
    "cups.path", "avahi-daemon.socket", "cups-browsed.socket",
)


class ServiceError(ValueError):
    """Raised for units outside the whitelist or malformed unit names."""


# --- names ----------------------------------------------------------------------------------------
def normalize_unit(unit: str) -> str:
    """Canonical unit name: add ``.service`` when no known suffix is present."""
    unit = (unit or "").strip()
    if not unit:
        return unit
    if unit == "tmp.mount" or unit.endswith(UNIT_SUFFIXES):
        return unit
    return unit + ".service"


def base_name(unit: str) -> str:
    """``bluetooth.service`` → ``bluetooth`` (``tmp.mount`` stays)."""
    unit = normalize_unit(unit)
    for suffix in UNIT_SUFFIXES:
        if unit.endswith(suffix) and unit != "tmp.mount":
            return unit[: -len(suffix)]
    return unit


def valid_unit_name(unit: str) -> bool:
    return bool(unit) and bool(common.UNIT_RE.match(unit)) and "/" not in unit and ".." not in unit


# --- whitelist ------------------------------------------------------------------------------------
def parse_whitelist(text: Optional[str]) -> List[str]:
    """Parse the whitelist file text (unit per line, ``#`` comments, blank lines)."""
    units: List[str] = []
    if not text:
        return units
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        unit = normalize_unit(line.split()[0])
        if valid_unit_name(unit) and unit not in units:
            units.append(unit)
    return units


def load_whitelist(ctx: Optional[Context] = None) -> List[str]:
    """Units the CLI may toggle.  File under ``LINDOS_ROOT`` → embedded default."""
    ctx = ctx or Context()
    units = parse_whitelist(common.read_text(ctx.path(common.SERVICES_WHITELIST)))
    if units:
        return units
    return list(DEFAULT_WHITELIST)


def is_whitelisted(unit: str, whitelist: Optional[Iterable[str]] = None, ctx: Optional[Context] = None) -> bool:
    unit = normalize_unit(unit)
    if not valid_unit_name(unit):
        return False
    wl = [normalize_unit(u) for u in (whitelist if whitelist is not None else load_whitelist(ctx))]
    return unit in wl


def check_units(units: Iterable[str], whitelist: Optional[Iterable[str]] = None, ctx: Optional[Context] = None) -> List[str]:
    """Normalise *units*; raise :class:`ServiceError` on the first non-whitelisted one."""
    wl = list(whitelist) if whitelist is not None else load_whitelist(ctx)
    out: List[str] = []
    for raw in units:
        unit = normalize_unit(raw)
        if not valid_unit_name(unit):
            raise ServiceError(f"invalid unit name {raw!r}")
        if not is_whitelisted(unit, wl):
            raise ServiceError(f"unit '{unit}' is not in the lindos-tune whitelist ({common.SERVICES_WHITELIST})")
        if unit not in out:
            out.append(unit)
    return out


def filter_whitelisted(units: Iterable[str], whitelist: Optional[Iterable[str]] = None,
                       ctx: Optional[Context] = None) -> Tuple[List[str], List[str]]:
    """Split *units* into ``(allowed, refused)`` without raising."""
    wl = list(whitelist) if whitelist is not None else load_whitelist(ctx)
    allowed: List[str] = []
    refused: List[str] = []
    for raw in units:
        unit = normalize_unit(raw)
        if valid_unit_name(unit) and is_whitelisted(unit, wl):
            if unit not in allowed:
                allowed.append(unit)
        else:
            refused.append(raw)
    return allowed, refused


# --- state queries --------------------------------------------------------------------------------
def unit_state(ctx: Context, unit: str) -> Dict[str, Any]:
    """``{"unit","exists","enabled","active"}`` (``"unknown"`` where systemctl cannot answer)."""
    info: Dict[str, Any] = {"unit": unit, "exists": ctx.unit_exists(unit), "enabled": "unknown", "active": "unknown"}
    if not ctx.can_systemctl or (ctx.root and not ctx.commands_allowed):
        return info
    res = ctx.systemctl("is-enabled", unit, timeout=15)
    first = res.out.strip().splitlines()[0].strip() if res.out.strip() else ""
    if first and " " not in first:
        info["enabled"] = first
    elif res.code == 127:
        info["enabled"] = "unknown"
    else:
        info["enabled"] = "not-found" if not info["exists"] else (first or "unknown")
    if ctx.live or (not ctx.root and not ctx.chroot):
        res = ctx.systemctl("is-active", unit, timeout=15)
        first = res.out.strip().splitlines()[0].strip() if res.out.strip() else ""
        info["active"] = first if first and " " not in first else "unknown"
    else:
        info["active"] = "n/a (offline)"
    return info


def list_units(ctx: Optional[Context] = None) -> List[Dict[str, Any]]:
    ctx = ctx or Context()
    from . import status as lstatus

    units = load_whitelist(ctx)
    states = lstatus.unit_states(ctx, units)
    rows: List[Dict[str, Any]] = []
    for unit in units:
        st = states.get(unit, {})
        rows.append({"unit": unit, "exists": ctx.unit_exists(unit),
                     "enabled": st.get("enabled", "unknown"), "active": st.get("active", "unknown")})
    return rows


def render_units(rows: List[Dict[str, Any]]) -> str:
    width = max((len(r["unit"]) for r in rows), default=4)
    lines = [f"{'UNIT'.ljust(width)}  ENABLED     ACTIVE      PRESENT"]
    for row in rows:
        lines.append(f"{row['unit'].ljust(width)}  {str(row['enabled']).ljust(10)}  {str(row['active']).ljust(10)}  "
                     f"{'yes' if row.get('exists') else 'no'}")
    lines.append("")
    lines.append("Only the units above may be toggled with 'lindos-tune services enable|disable <unit>'.")
    return "\n".join(lines)


# --- toggling -------------------------------------------------------------------------------------
def _toggle(ctx: Context, verb: str, unit: str, report: Report, *, now: bool = True) -> bool:
    name = f"{verb}:{unit}"
    if not ctx.unit_exists(unit) and not ctx.dry_run:
        report.skip(name, "unit not installed")
        return True
    if not ctx.can_systemctl:
        report.skip(name, "systemctl not available")
        return True
    args = [verb]
    if now and ctx.live:
        args.append("--now")
    res = ctx.systemctl(*args, unit)
    if res.ok:
        detail = "done" + (" (--now)" if "--now" in args else " (takes effect at boot)")
        report.add(name, True, detail if not ctx.dry_run else "would run: " + common.describe_cmd(ctx.systemctl_args(*args, unit)))
        return True
    if "does not exist" in res.out or "not found" in res.out.lower() or "No such file" in res.out:
        report.skip(name, "unit not installed")
        return True
    report.add(name, False, res.tail() or f"systemctl exit {res.code}")
    return False


def enable(ctx: Context, unit: str, report: Optional[Report] = None, *, now: bool = True,
           whitelist: Optional[Iterable[str]] = None) -> Report:
    report = report or Report(title=f"enable {unit}")
    unit = check_units([unit], whitelist, ctx)[0]
    _toggle(ctx, "enable", unit, report, now=now)
    return report


def disable(ctx: Context, unit: str, report: Optional[Report] = None, *, now: bool = True,
            whitelist: Optional[Iterable[str]] = None) -> Report:
    report = report or Report(title=f"disable {unit}")
    unit = check_units([unit], whitelist, ctx)[0]
    _toggle(ctx, "disable", unit, report, now=now)
    return report


def apply_lists(ctx: Context, report: Report, *, disable_units: Iterable[str] = (),
                enable_units: Iterable[str] = (), whitelist: Optional[Iterable[str]] = None) -> None:
    """Disable/enable whitelisted units; non-whitelisted names are recorded as skipped."""
    wl = list(whitelist) if whitelist is not None else load_whitelist(ctx)
    for verb, wanted in (("disable", disable_units), ("enable", enable_units)):
        allowed, refused = filter_whitelisted(wanted, wl)
        for unit in refused:
            report.skip(f"{verb}:{unit}", "not in whitelist — refused")
        for unit in allowed:
            _toggle(ctx, verb, unit, report, now=True)


# --- autostart hiding -----------------------------------------------------------------------------
_HIDE_LINE_RE = re.compile(r"^([A-Za-z0-9._@-]+)(?:\s+if-disabled=([A-Za-z0-9@._:-]+))?$")


def parse_hide_list(text: Optional[str]) -> List[Tuple[str, Optional[str]]]:
    """``name [if-disabled=<unit>]`` lines → ``[(desktop-basename, condition-unit|None)]``."""
    out: List[Tuple[str, Optional[str]]] = []
    if not text:
        return out
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _HIDE_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.endswith(".desktop"):
            name = name[: -len(".desktop")]
        cond = normalize_unit(m.group(2)) if m.group(2) else None
        out.append((name, cond))
    return out


MARKER_KEY = "X-Lindos-Hidden"
_PLACEHOLDER = "X-Lindos-Hidden"


def _scan_entry(lines: List[str]) -> Dict[str, int]:
    """Indices of interesting keys inside the ``[Desktop Entry]`` group (-1 = absent)."""
    found = {"start": -1, "end": len(lines), "Hidden": -1, "OnlyShowIn": -1, "NotShowIn": -1, MARKER_KEY: -1}
    in_entry = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_entry:
                found["end"] = idx
                break
            in_entry = stripped == "[Desktop Entry]"
            if in_entry:
                found["start"] = idx
            continue
        if not in_entry or not stripped or stripped.startswith("#"):
            continue
        key, sep, _value = stripped.partition("=")
        key = key.strip()
        if sep and key in found and key not in ("start", "end"):
            found[key] = idx
    return found


def _list_value(line: str) -> List[str]:
    _key, _sep, value = line.partition("=")
    return [v for v in value.split(";") if v.strip()]


def hide_in_xfce(text: str) -> Tuple[str, bool]:
    """Return ``(new_text, changed)`` with the entry hidden for XFCE sessions.

    Idempotent: adds ``XFCE`` to ``NotShowIn`` (creating the key when absent) unless the entry
    already carries ``Hidden=true``.  When the file uses ``OnlyShowIn`` (which may not be
    combined with ``NotShowIn``), ``XFCE`` is removed from that list instead.  Every change is
    marked with ``X-Lindos-Hidden=true`` so :func:`unhide_in_xfce` can revert it exactly.
    """
    lines = text.splitlines()
    f = _scan_entry(lines)
    if f["start"] < 0:
        return text, False
    if f["Hidden"] >= 0 and lines[f["Hidden"]].partition("=")[2].strip().lower() == "true":
        return text, False
    if f["OnlyShowIn"] >= 0:
        items = _list_value(lines[f["OnlyShowIn"]])
        if "XFCE" not in items:
            return text, False
        items = [v for v in items if v != "XFCE"] or [_PLACEHOLDER]
        lines[f["OnlyShowIn"]] = "OnlyShowIn=" + ";".join(items) + ";"
    elif f["NotShowIn"] >= 0:
        items = _list_value(lines[f["NotShowIn"]])
        if "XFCE" in items:
            return text, False
        lines[f["NotShowIn"]] = "NotShowIn=" + ";".join(items + ["XFCE"]) + ";"
    else:
        lines.insert(f["start"] + 1, "NotShowIn=XFCE;")
        f = _scan_entry(lines)
    if f[MARKER_KEY] < 0:
        lines.insert(f["start"] + 1, f"{MARKER_KEY}=true")
    return "\n".join(lines) + "\n", True


def unhide_in_xfce(text: str) -> Tuple[str, bool]:
    """Revert :func:`hide_in_xfce` (only when our ``X-Lindos-Hidden=true`` marker is present)."""
    lines = text.splitlines()
    f = _scan_entry(lines)
    if f["start"] < 0 or f[MARKER_KEY] < 0:
        return text, False
    if lines[f[MARKER_KEY]].partition("=")[2].strip().lower() != "true":
        return text, False
    if f["OnlyShowIn"] >= 0:
        items = [v for v in _list_value(lines[f["OnlyShowIn"]]) if v != _PLACEHOLDER]
        if "XFCE" not in items:
            items.append("XFCE")
        lines[f["OnlyShowIn"]] = "OnlyShowIn=" + ";".join(items) + ";"
    elif f["NotShowIn"] >= 0:
        items = [v for v in _list_value(lines[f["NotShowIn"]]) if v != "XFCE"]
        if items:
            lines[f["NotShowIn"]] = "NotShowIn=" + ";".join(items) + ";"
        else:
            del lines[f["NotShowIn"]]
            f = _scan_entry(lines)
    del lines[f[MARKER_KEY]]
    return "\n".join(lines) + "\n", True


def hide_autostart(ctx: Context, report: Report, entries: Optional[List[Tuple[str, Optional[str]]]] = None,
                   disabled_units: Optional[Iterable[str]] = None) -> None:
    """Hide the listed ``/etc/xdg/autostart/<name>.desktop`` entries from XFCE sessions.

    Conditional entries (``if-disabled=<unit>``) are hidden only while *unit* is in
    *disabled_units* and un-hidden (marker-exact revert) otherwise.
    """
    if entries is None:
        entries = parse_hide_list(common.read_text(ctx.path(common.AUTOSTART_HIDE_LIST)))
    if not entries:
        report.skip("autostart-hide", "no autostart-hide.list entries")
        return
    disabled = set(normalize_unit(u) for u in (disabled_units or ()))
    changed: List[str] = []
    kept: List[str] = []
    missing: List[str] = []
    restored: List[str] = []
    visible: List[str] = []
    for name, cond in entries:
        target = os.path.join(common.XDG_AUTOSTART_DIR, name + ".desktop")
        current = common.read_text(ctx.path(target))
        if current is None:
            missing.append(name)
            continue
        if cond and cond not in disabled:
            new_text, did_change = unhide_in_xfce(current)
            if did_change:
                ok, _ = ctx.write(target, new_text)
                if ok:
                    restored.append(f"{name} ({cond} enabled)")
                else:
                    report.add(f"autostart-unhide:{name}", False, f"cannot write {target}")
            else:
                visible.append(f"{name} (needs {cond} disabled)")
            continue
        new_text, did_change = hide_in_xfce(current)
        if not did_change:
            kept.append(name)
            continue
        ok, _ = ctx.write(target, new_text)
        if ok:
            changed.append(name)
        else:
            report.add(f"autostart-hide:{name}", False, f"cannot write {target}")
    parts: List[str] = []
    if changed:
        parts.append(("would hide" if ctx.dry_run else "hidden") + ": " + ", ".join(changed))
    if kept:
        parts.append("already hidden: " + ", ".join(kept))
    if restored:
        parts.append(("would restore" if ctx.dry_run else "restored") + ": " + ", ".join(restored))
    if visible:
        parts.append("left visible: " + ", ".join(visible))
    if missing:
        parts.append("not installed: " + ", ".join(missing))
    report.add("autostart-hide", True, "; ".join(parts) or "nothing to do")


def sync_conditional_autostart(ctx: Context, report: Report, unit: str, disabled: bool) -> None:
    """After ``services enable|disable <unit>``: hide/unhide the autostart entries tied to *unit*."""
    unit = normalize_unit(unit)
    entries = [(n, c) for n, c in parse_hide_list(common.read_text(ctx.path(common.AUTOSTART_HIDE_LIST))) if c == unit]
    if not entries:
        return
    hide_autostart(ctx, report, entries=entries, disabled_units=[unit] if disabled else [])


__all__ = [
    "UNIT_SUFFIXES", "DEFAULT_WHITELIST", "ServiceError", "normalize_unit", "base_name",
    "valid_unit_name", "parse_whitelist", "load_whitelist", "is_whitelisted", "check_units",
    "filter_whitelisted", "unit_state", "list_units", "render_units", "enable", "disable",
    "apply_lists", "parse_hide_list", "hide_in_xfce", "hide_autostart",
]
