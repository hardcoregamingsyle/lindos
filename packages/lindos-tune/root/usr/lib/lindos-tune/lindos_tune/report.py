"""``lindos-tune report`` — Markdown bug report (SPEC §11).

Collects everything ``status`` knows plus kernel, CPU/GPU, tune state, the managed config
files and fan/temperature readings into one paste-able Markdown document.  Read-only and
unprivileged; every reader goes through ``LINDOS_ROOT``-aware paths so it can be unit-tested.
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import re
from typing import Any, Dict, List, Optional

from . import __version__ as _VERSION
from . import common, fan as lfan, power as lpower, status as lstatus, zram as lzram
from .common import Context

MAX_FILE_LINES = 60


# --- hardware ------------------------------------------------------------------------------------
def cpu_model(root_dir: Optional[str] = None) -> Dict[str, Any]:
    """``{"model", "count"}`` from ``/proc/cpuinfo`` (``platform.processor()`` fallback)."""
    text = common.read_text(common.path("/proc/cpuinfo", root_dir)) or ""
    model = ""
    count = 0
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key == "processor":
            count += 1
        elif key in ("model name", "hardware", "cpu model") and not model:
            model = value.strip()
    if not model and not root_dir and not common.root():
        model = platform.processor() or platform.machine() or ""
        count = count or (os.cpu_count() or 0)
    return {"model": model or "unknown", "count": count}


def gpu_summary(ctx: Context) -> str:
    """GPU line: lindos-core ``hardware.gpu_info`` when importable, else ``lspci`` (live only)."""
    if not ctx.root:
        try:
            from lindos import hardware as core_hw  # type: ignore

            info = core_hw.gpu_info()
            if isinstance(info, dict) and info:
                vendor = info.get("vendor") or "other"
                name = info.get("name") or info.get("model") or info.get("description") or ""
                driver = info.get("driver") or info.get("kernel_driver") or ""
                parts = [str(name) if name else "", f"vendor {vendor}", f"driver {driver}" if driver else ""]
                return ", ".join(p for p in parts if p) or "unknown"
            if isinstance(info, list) and info:
                lines = []
                for gpu in info:
                    if isinstance(gpu, dict):
                        lines.append(f"{gpu.get('name') or gpu.get('model') or '?'} (vendor {gpu.get('vendor') or 'other'})")
                if lines:
                    return "; ".join(lines)
        except Exception:
            pass
    if (ctx.commands_allowed or not ctx.root) and ctx.which("lspci"):
        res = ctx.run(["lspci", "-nn"], timeout=15)
        gpus = [ln.strip() for ln in res.out.splitlines() if re.search(r"VGA compatible|3D controller|Display controller", ln)]
        if gpus:
            return "; ".join(re.sub(r"^\S+\s+", "", g) for g in gpus)
    return "unknown"


def battery_present(root_dir: Optional[str] = None) -> Optional[bool]:
    base = common.path("/sys/class/power_supply", root_dir)
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for entry in entries:
        if common.read_first_line(os.path.join(base, entry, "type")).lower() == "battery":
            return True
    return False


def lindos_release(root_dir: Optional[str] = None) -> str:
    return common.read_first_line(common.path(common.LINDOS_RELEASE, root_dir)) or "Lindos (release file missing)"


def os_release(root_dir: Optional[str] = None) -> Dict[str, str]:
    return common.parse_kv(common.read_text(common.path("/etc/os-release", root_dir)))


# --- files ----------------------------------------------------------------------------------------
def _file_block(ctx: Context, system_path: str, lang: str = "") -> List[str]:
    text = common.read_text(ctx.path(system_path))
    lines: List[str] = [f"`{system_path}`", ""]
    if text is None:
        lines.append("_absent_")
        lines.append("")
        return lines
    body = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if len(body) > MAX_FILE_LINES:
        body = body[:MAX_FILE_LINES] + [f"… ({len(body) - MAX_FILE_LINES} more lines)"]
    lines.append(f"```{lang}")
    lines.extend(body or ["(only comments)"])
    lines.append("```")
    lines.append("")
    return lines


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


# --- build ----------------------------------------------------------------------------------------
def build(ctx: Optional[Context] = None, snap: Optional[Dict[str, Any]] = None) -> str:
    """Return the Markdown report."""
    ctx = ctx or Context()
    snap = snap if snap is not None else lstatus.snapshot(ctx)
    now = _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    ram = snap.get("ram", {})
    mode = snap.get("mode", "?")
    cpu = cpu_model(ctx.root)
    osr = os_release(ctx.root)
    state = common.read_json(ctx.path(common.STATE_FILE))
    zconf = lzram.current_config(ctx)
    pw = lpower.current(ctx)
    fans = lfan.list_fans(ctx)

    md: List[str] = []
    md.append("# Lindos tune report")
    md.append("")
    md.append(f"_Generated {now} by `lindos-tune report` {_VERSION}. Paste this into a bug report; "
              "it contains no user names, files or network identifiers._")
    md.append("")
    md.append("## System")
    md.append("")
    md.extend(_table(["item", "value"], [
        ["Lindos", lindos_release(ctx.root)],
        ["Base", osr.get("PRETTY_NAME") or "unknown"],
        ["Kernel", snap.get("kernel") or "unknown"],
        ["Mode", mode],
        ["CPU", f"{cpu['model']} ({cpu['count']} threads)" if cpu["count"] else cpu["model"]],
        ["GPU", gpu_summary(ctx)],
        ["RAM total", f"{ram.get('total', 0)} MB"],
        ["Battery", {True: "yes (laptop)", False: "no", None: "unknown"}[battery_present(ctx.root)]],
        ["Chroot / offline", "yes" if ctx.chroot else "no"],
    ]))
    md.append("")

    md.append("## RAM")
    md.append("")
    verdict = snap.get("verdict", {})
    md.append(f"**{verdict.get('line', '')}**  ")
    md.append(f"score {snap.get('score', 0)}/100 — used {ram.get('used', 0)} MB, available {ram.get('available', 0)} MB, "
              f"swap {ram.get('swap_used', 0)}/{ram.get('swap_total', 0)} MB (source: {ram.get('source', '?')})")
    md.append("")
    md.append("### zram")
    md.append("")
    z = snap.get("zram", {})
    if z.get("devices"):
        md.extend(_table(["device", "size MB", "algo", "swap", "used MB", "orig→compr MB", "ratio", "prio"],
                         [[d["name"], d.get("disksize_mb", 0), d.get("algorithm") or "?",
                           "active" if d.get("swap_active") else "inactive", d.get("swap_used_mb", 0),
                           f"{d.get('orig_data_mb', 0)}→{d.get('compr_data_mb', 0)}", d.get("ratio", 0),
                           d.get("swap_priority") if d.get("swap_priority") is not None else "-"] for d in z["devices"]]))
    else:
        md.append("_no zram device_")
    md.append("")
    md.append(f"configured: backend {zconf.get('backend') or 'none'}, "
              f"{str(zconf.get('percent')) + ' %' if zconf.get('percent') is not None else 'no percent'}"
              + (f" ({zconf.get('path')})" if zconf.get("path") else ""))
    if z.get("other_swaps"):
        md.append("")
        md.append("other swap: " + ", ".join(f"{s['device']} {s['size_mb']} MB (prio {s['priority']})" for s in z["other_swaps"]))
    md.append("")

    md.append("### Top processes by RSS")
    md.append("")
    top = snap.get("top", [])
    if top:
        md.extend(_table(["#", "process", "pid", "RSS MB"],
                         [[i + 1, p["name"], p["pid"], p["rss_mb"]] for i, p in enumerate(top)]))
    else:
        md.append("_per-process data unavailable_")
    md.append("")

    md.append("## CPU / power")
    md.append("")
    gov = snap.get("governor", {})
    md.extend(_table(["item", "value"], [
        ["governor", gov.get("current") or "n/a"],
        ["available", " ".join(gov.get("available", [])) or "n/a"],
        ["driver", gov.get("driver") or "n/a"],
        ["EPP", gov.get("epp") or "n/a"],
        ["power profile", snap.get("power_profile") or pw.get("profile") or "n/a"],
        ["power back-end", pw.get("backend") or "n/a"],
        ["compositor", snap.get("compositor", {}).get("state", "unknown")],
    ]))
    md.append("")

    md.append("## Services")
    md.append("")
    enabled = snap.get("units_enabled", [])
    active = snap.get("units_active", [])
    md.append("enabled units of interest: " + (", ".join(f"`{u}`" for u in enabled) if enabled else "_none / unknown_"))
    md.append("")
    md.append("active now: " + (", ".join(f"`{u}`" for u in active) if active else "_none / unknown_"))
    md.append("")
    units = snap.get("units", {})
    if units and all(st.get("enabled") == "unknown" for st in units.values()):
        md.append("_(unit states unavailable here — systemctl not reachable)_")
        md.append("")

    md.append("## Fans / temperatures")
    md.append("")
    if fans.get("fans"):
        md.extend(_table(["chip", "fan", "RPM"], [[f["chip"], f["label"], f["rpm"]] for f in fans["fans"]]))
        md.append("")
    if fans.get("temps"):
        md.extend(_table(["chip", "sensor", "°C", "high", "crit"],
                         [[t["chip"], t["label"], t["celsius"], t.get("high") if t.get("high") is not None else "-",
                           t.get("crit") if t.get("crit") is not None else "-"] for t in fans["temps"]]))
        md.append("")
    if not fans.get("fans") and not fans.get("temps"):
        md.append(f"_no sensor data (source: {fans.get('source')})_")
        md.append("")
    ctrl = fans.get("controllers", {})
    md.append(f"controllers: nbfc {'yes' if ctrl.get('nbfc', {}).get('available') else 'no'}, "
              f"thinkpad_acpi {'yes' if ctrl.get('thinkpad_acpi', {}).get('available') else 'no'}, "
              f"fancontrol {'configured' if ctrl.get('fancontrol', {}).get('configured') else 'no'}")
    md.append("")

    md.append("## Tune state")
    md.append("")
    if state:
        md.extend(_table(["key", "value"], [[k, state[k]] for k in sorted(state)]))
    else:
        md.append(f"_`{common.STATE_FILE}` absent — `lindos-tune apply` has not run yet_")
    md.append("")

    md.append("## Managed configuration")
    md.append("")
    for path_, lang in ((common.SYSCTL_BASE_CONF, "ini"), (common.SYSCTL_MODE_CONF, "ini"),
                        (common.EARLYOOM_DEFAULT, "sh"), (f"{common.TUNE_D_DIR}/{mode}.conf", "sh"),
                        (common.TMPFILES_GOVERNOR, ""), (common.JOURNALD_DROPIN, "ini")):
        md.extend(_file_block(ctx, path_, lang))
    if zconf.get("path"):
        md.extend(_file_block(ctx, str(zconf["path"]), "ini"))
    md.append("---")
    md.append("Targets: idle RAM 350–500 MB (Lite 300–380 MB), measured as `free -m` \"used\" after login with no apps open. "
              "Figures in docs/RAM-BUDGET.md are estimates until measured.")
    md.append("")
    return "\n".join(md)


def run_report(ctx: Optional[Context] = None, output: Optional[str] = None) -> str:
    """Build the report; write it to *output* when given.  Returns the Markdown."""
    text = build(ctx)
    if output and output != "-":
        common.write_text(output, text)
    return text


__all__ = ["MAX_FILE_LINES", "cpu_model", "gpu_summary", "battery_present", "lindos_release", "os_release",
           "build", "run_report"]
