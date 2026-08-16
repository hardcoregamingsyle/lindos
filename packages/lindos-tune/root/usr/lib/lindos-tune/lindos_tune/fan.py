"""``lindos-tune fan list|set <profile>`` (SPEC §11).

* ``list`` — fans and temperatures from ``sensors -j`` (lm-sensors), falling back to
  ``/sys/class/hwmon``; plus which *controllers* exist: nbfc-linux (``nbfc``), fancontrol
  (``/etc/fancontrol``), thinkpad_acpi (``/proc/acpi/ibm/fan``).
* ``set <profile>`` — ``auto | quiet | balanced | max``.  With nbfc: ``nbfc set -a`` for auto,
  ``nbfc set -s <percent>`` otherwise (quiet 30 %, balanced 55 %, max 100 %).  With
  thinkpad_acpi (``fan_control=1``): ``level auto | 2 | 4 | 7``.  Otherwise the honest answer
  "no controllable fans detected" with the install hint for nbfc-linux
  (``/usr/libexec/lindos/install-nbfc.sh``).

Reading is unprivileged; setting needs root (the CLI goes through the helper action
``set-fan-profile``).
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import common
from .common import Context, Report

PROFILES = ("auto", "quiet", "balanced", "max")
NBFC_SPEED = {"quiet": 30, "balanced": 55, "max": 100}
THINKPAD_LEVEL = {"auto": "auto", "quiet": "2", "balanced": "4", "max": "7"}
THINKPAD_FAN = "/proc/acpi/ibm/fan"
FANCONTROL_CONF = "/etc/fancontrol"
HWMON_DIR = "/sys/class/hwmon"
NO_FANS_MSG = ("no controllable fans detected; install nbfc-linux (run as root: "
               f"{common.INSTALL_NBFC_SCRIPT} --yes) or configure fancontrol (pwmconfig)")


# --- sensors -j -----------------------------------------------------------------------------------
def parse_sensors_json(data: Any) -> Dict[str, List[Dict[str, Any]]]:
    """``sensors -j`` JSON → ``{"fans": [{chip,label,rpm,min}], "temps": [{chip,label,celsius,high,crit}]}``."""
    fans: List[Dict[str, Any]] = []
    temps: List[Dict[str, Any]] = []
    if not isinstance(data, dict):
        return {"fans": fans, "temps": temps}
    for chip, features in data.items():
        if not isinstance(features, dict):
            continue
        for label, values in features.items():
            if not isinstance(values, dict):
                continue  # "Adapter": "ISA adapter"
            for key, value in values.items():
                if not isinstance(value, (int, float)):
                    continue
                if key.startswith("fan") and key.endswith("_input"):
                    prefix = key[: -len("_input")]
                    fans.append({"chip": chip, "label": label, "rpm": int(round(float(value))),
                                 "min": _num(values.get(prefix + "_min"))})
                elif key.startswith("temp") and key.endswith("_input"):
                    prefix = key[: -len("_input")]
                    temps.append({"chip": chip, "label": label, "celsius": round(float(value), 1),
                                  "high": _num(values.get(prefix + "_max")), "crit": _num(values.get(prefix + "_crit"))})
    fans.sort(key=lambda f: (f["chip"], f["label"]))
    temps.sort(key=lambda t: (t["chip"], t["label"]))
    return {"fans": fans, "temps": temps}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    return None


def parse_sensors_text(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse the raw stdout of ``sensors -j`` (tolerates a trailing warning line)."""
    text = (text or "").strip()
    if not text:
        return {"fans": [], "temps": []}
    try:
        return parse_sensors_json(json.loads(text))
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return parse_sensors_json(json.loads(text[start:end + 1]))
            except ValueError:
                pass
    return {"fans": [], "temps": []}


def read_sensors(ctx: Context) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
    """``(parsed, source)`` — source is ``sensors``, ``hwmon`` or ``none``."""
    if (ctx.commands_allowed or not ctx.root) and ctx.which("sensors"):
        if ctx.injected_runner:
            res = ctx.run(["sensors", "-j"], timeout=20)
            parsed = parse_sensors_text(res.out)
        else:
            code, out, _err = common.run_capture(["sensors", "-j"], timeout=20)
            parsed = parse_sensors_text(out) if code in (0, 1) else {"fans": [], "temps": []}
        if parsed["fans"] or parsed["temps"]:
            return parsed, "sensors"
    parsed = hwmon_sensors(ctx)
    if parsed["fans"] or parsed["temps"]:
        return parsed, "hwmon"
    return {"fans": [], "temps": []}, "none"


def hwmon_sensors(ctx: Context) -> Dict[str, List[Dict[str, Any]]]:
    """Fallback reader for ``/sys/class/hwmon/hwmon*/{fan,temp}*_input``."""
    fans: List[Dict[str, Any]] = []
    temps: List[Dict[str, Any]] = []
    for hw in sorted(glob.glob(os.path.join(ctx.path(HWMON_DIR), "hwmon*"))):
        chip = common.read_first_line(os.path.join(hw, "name")) or os.path.basename(hw)
        for path in sorted(glob.glob(os.path.join(hw, "fan*_input"))):
            base = os.path.basename(path)[: -len("_input")]
            label = common.read_first_line(os.path.join(hw, base + "_label")) or base
            try:
                rpm = int(common.read_first_line(path) or "0")
            except ValueError:
                continue
            fans.append({"chip": chip, "label": label, "rpm": rpm, "min": None})
        for path in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            base = os.path.basename(path)[: -len("_input")]
            label = common.read_first_line(os.path.join(hw, base + "_label")) or base
            try:
                milli = int(common.read_first_line(path) or "0")
            except ValueError:
                continue
            temps.append({"chip": chip, "label": label, "celsius": round(milli / 1000.0, 1),
                          "high": _milli(common.read_first_line(os.path.join(hw, base + "_max"))),
                          "crit": _milli(common.read_first_line(os.path.join(hw, base + "_crit")))})
    return {"fans": fans, "temps": temps}


def _milli(text: str) -> Optional[float]:
    try:
        return round(int(text) / 1000.0, 1) if text else None
    except ValueError:
        return None


# --- controllers ----------------------------------------------------------------------------------
def nbfc_available(ctx: Context) -> bool:
    if ctx.overrides.get("nbfc") is not None:
        return bool(ctx.overrides["nbfc"])
    if ctx.root and not ctx.commands_allowed:
        return False
    return ctx.which("nbfc") is not None


def nbfc_info(ctx: Context) -> Dict[str, Any]:
    """``nbfc status -a`` / ``nbfc config -l`` summary (best effort, never raises)."""
    info: Dict[str, Any] = {"available": nbfc_available(ctx), "status": "", "configured": None, "configs": []}
    if not info["available"] or ctx.dry_run and not ctx.injected_runner:
        return info
    res = ctx.run(["nbfc", "status", "-a"], timeout=15)
    info["status"] = res.tail(6) if res.out else ("" if res.ok else f"exit {res.code}")
    if res.ok and res.out:
        info["configured"] = True
        for line in res.out.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() in ("selected config name", "config", "selected config"):
                info["config_name"] = value.strip()
    elif res.code != 127:
        info["configured"] = False
    res = ctx.run(["nbfc", "config", "-l"], timeout=15)
    if res.ok:
        info["configs"] = [ln.strip() for ln in res.out.splitlines() if ln.strip()][:400]
    return info


def thinkpad_available(ctx: Context) -> bool:
    if ctx.overrides.get("thinkpad") is not None:
        return bool(ctx.overrides["thinkpad"])
    return ctx.exists(THINKPAD_FAN)


def thinkpad_status(ctx: Context) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = common.read_text(ctx.path(THINKPAD_FAN)) or ""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def fancontrol_configured(ctx: Context) -> bool:
    return ctx.exists(FANCONTROL_CONF)


def controllers(ctx: Context) -> Dict[str, Any]:
    tp = thinkpad_available(ctx)
    tp_status = thinkpad_status(ctx) if tp else {}
    return {
        "nbfc": nbfc_info(ctx),
        "thinkpad_acpi": {"available": tp, "status": tp_status.get("status"), "level": tp_status.get("level"),
                          "writable": tp and "commands" in tp_status},
        "fancontrol": {"configured": fancontrol_configured(ctx)},
    }


def controllable(ctrl: Dict[str, Any]) -> bool:
    return bool(ctrl.get("nbfc", {}).get("available")) or bool(ctrl.get("thinkpad_acpi", {}).get("available"))


# --- list -----------------------------------------------------------------------------------------
def list_fans(ctx: Optional[Context] = None) -> Dict[str, Any]:
    ctx = ctx or Context()
    sensors, source = read_sensors(ctx)
    ctrl = controllers(ctx)
    return {"source": source, "fans": sensors["fans"], "temps": sensors["temps"], "controllers": ctrl,
            "controllable": controllable(ctrl), "profiles": list(PROFILES),
            "hint": None if controllable(ctrl) else NO_FANS_MSG}


def render_table(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    fans = data.get("fans", [])
    temps = data.get("temps", [])
    lines.append("Fans" + (f" (source: {data.get('source')})" if data.get("source") else ""))
    if fans:
        for f in fans:
            extra = f" (min {int(f['min'])})" if f.get("min") else ""
            lines.append(f"  {f['chip']:<24} {f['label']:<16} {f['rpm']:>6} RPM{extra}")
    else:
        lines.append("  no fan sensors reported (lm-sensors: run 'sensors-detect' as root, or the fan is not exposed by the EC)")
    lines.append("Temperatures")
    if temps:
        for t in temps:
            hi = f"  high {t['high']:.0f}" if t.get("high") is not None else ""
            crit = f"  crit {t['crit']:.0f}" if t.get("crit") is not None else ""
            lines.append(f"  {t['chip']:<24} {t['label']:<16} {t['celsius']:>6.1f} °C{hi}{crit}")
    else:
        lines.append("  no temperature sensors reported")
    ctrl = data.get("controllers", {})
    lines.append("Controllers")
    nb = ctrl.get("nbfc", {})
    if nb.get("available"):
        state = "configured" if nb.get("configured") else ("no config applied — try: nbfc config -r (recommend), nbfc config -a <name>" if nb.get("configured") is False else "present")
        lines.append(f"  nbfc-linux: {state}" + (f" [{nb['config_name']}]" if nb.get("config_name") else ""))
    else:
        lines.append("  nbfc-linux: not installed")
    tp = ctrl.get("thinkpad_acpi", {})
    if tp.get("available"):
        lines.append(f"  thinkpad_acpi: level {tp.get('level') or '?'}" + ("" if tp.get("writable") else " (read-only: load thinkpad_acpi with fan_control=1 to control)"))
    fc = ctrl.get("fancontrol", {})
    lines.append(f"  fancontrol: {'configured (/etc/fancontrol)' if fc.get('configured') else 'not configured (pwmconfig to set up)'}")
    if data.get("controllable"):
        lines.append(f"Profiles: {', '.join(data.get('profiles', PROFILES))}  →  lindos-tune fan set <profile>")
    else:
        lines.append(data.get("hint") or NO_FANS_MSG)
    return "\n".join(lines)


# --- set ------------------------------------------------------------------------------------------
def normalize_profile(profile: str) -> Optional[str]:
    p = (profile or "").strip().lower()
    aliases = {"silent": "quiet", "normal": "balanced", "default": "auto", "full": "max", "maximum": "max"}
    p = aliases.get(p, p)
    return p if p in PROFILES else None


def set_profile(ctx: Context, profile: str, report: Optional[Report] = None) -> Report:
    report = report or Report(title=f"fan {profile}", dry_run=ctx.dry_run)
    prof = normalize_profile(profile)
    if prof is None:
        report.add("fan", False, f"unknown profile {profile!r} (choose from {', '.join(PROFILES)})")
        return report
    if nbfc_available(ctx):
        argv = ["nbfc", "set", "-a"] if prof == "auto" else ["nbfc", "set", "-s", str(NBFC_SPEED[prof])]
        if ctx.dry_run:
            report.add("fan", True, "would run: " + common.describe_cmd(argv))
            return report
        res = ctx.run(argv, timeout=30)
        if res.ok:
            report.add("fan", True, f"{prof} via nbfc ({'auto' if prof == 'auto' else str(NBFC_SPEED[prof]) + '%'})")
            return report
        hint = ""
        low = res.out.lower()
        if "config" in low or "not running" in low or "service" in low:
            hint = " — apply a model config first: nbfc config -r; nbfc config -a <name>; nbfc start"
        report.add("fan", False, f"nbfc failed: {res.tail()}{hint}")
        return report
    if thinkpad_available(ctx):
        level = THINKPAD_LEVEL[prof]
        if ctx.dry_run:
            report.add("fan", True, f"would write 'level {level}' to {THINKPAD_FAN}")
            return report
        try:
            with open(ctx.path(THINKPAD_FAN), "w", encoding="utf-8") as fh:
                fh.write(f"level {level}\n")
            report.add("fan", True, f"{prof} via thinkpad_acpi (level {level})")
        except OSError as exc:
            report.add("fan", False, f"thinkpad_acpi: cannot set level {level}: {exc} (needs root and fan_control=1)")
        return report
    report.skip("fan", NO_FANS_MSG)
    return report


__all__ = [
    "PROFILES", "NBFC_SPEED", "THINKPAD_LEVEL", "THINKPAD_FAN", "FANCONTROL_CONF", "NO_FANS_MSG",
    "parse_sensors_json", "parse_sensors_text", "read_sensors", "hwmon_sensors", "nbfc_available",
    "nbfc_info", "thinkpad_available", "thinkpad_status", "fancontrol_configured", "controllers",
    "controllable", "list_fans", "render_table", "normalize_profile", "set_profile",
]
