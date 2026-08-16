"""``lindos-tune status`` — RAM snapshot, zram, top RSS, units, governor, compositor, verdict.

Everything reads through :func:`lindos_tune.common.path` so the test-suite can point
``LINDOS_ROOT`` at a directory holding fake ``proc/meminfo``, ``proc/<pid>/status``,
``sys/block/zram0/…`` files.  Sub-processes (``systemctl``, ``pgrep``, ``xfconf-query``,
``powerprofilesctl``) run through the :class:`~lindos_tune.common.Context` runner so tests can
inject canned output; when a ``LINDOS_ROOT`` is set and no runner was injected those calls are
skipped and reported as ``"unknown"``.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import common
from .common import Context

TOP_N = 10

# zram ``mm_stat`` columns (Documentation/admin-guide/blockdev/zram.rst)
_MM_STAT_FIELDS = ("orig_data_size", "compr_data_size", "mem_used_total", "mem_limit",
                   "mem_used_max", "same_pages", "pages_compacted", "huge_pages")


# --- RAM ------------------------------------------------------------------------------------------
def parse_meminfo(text: Optional[str]) -> Dict[str, int]:
    """``/proc/meminfo`` text → ``{key: kB}``."""
    out: Dict[str, int] = {}
    if not text:
        return out
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            out[key.strip()] = int(parts[0])
        except ValueError:
            continue
    return out


def meminfo(root_dir: Optional[str] = None) -> Dict[str, int]:
    return parse_meminfo(common.read_text(common.path("/proc/meminfo", root_dir)))


def ram_from_meminfo(mi: Dict[str, int]) -> Dict[str, int]:
    """MB figures the way ``free -m`` (procps ≥ 4) reports them: used = total − available."""
    kb = 1024
    total = mi.get("MemTotal", 0) // kb
    free = mi.get("MemFree", 0) // kb
    available = mi.get("MemAvailable", mi.get("MemFree", 0)) // kb
    buffers = mi.get("Buffers", 0) // kb
    cached = (mi.get("Cached", 0) + mi.get("SReclaimable", 0)) // kb
    swap_total = mi.get("SwapTotal", 0) // kb
    swap_free = mi.get("SwapFree", 0) // kb
    return {
        "total": total,
        "used": max(0, total - available),
        "available": available,
        "free": free,
        "buff_cache": buffers + cached,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
        "swap_free": swap_free,
    }


def _core_ram_snapshot() -> Optional[Dict[str, Any]]:
    """``lindos.ram.snapshot()`` from lindos-core when available (live system only)."""
    try:
        from lindos import ram as core_ram  # type: ignore
    except Exception:
        return None
    try:
        snap = core_ram.snapshot()
    except Exception:
        return None
    return snap if isinstance(snap, dict) and snap.get("total") else None


def ram_snapshot(root_dir: Optional[str] = None) -> Dict[str, Any]:
    """RAM figures in MB.  Uses ``lindos.ram.snapshot`` on a live system, ``/proc/meminfo``
    under ``LINDOS_ROOT`` otherwise (Windows dev boxes report zeros)."""
    base = common.root() if root_dir is None else root_dir
    if not base:
        core = _core_ram_snapshot()
        if core:
            data = dict(core)
            data.setdefault("source", "lindos.ram")
            for key in ("total", "used", "available"):
                data[key] = int(data.get(key, 0) or 0)
            data.setdefault("swap_total", 0)
            data.setdefault("swap_used", 0)
            return data
    data: Dict[str, Any] = dict(ram_from_meminfo(meminfo(base)))
    data["source"] = "/proc/meminfo"
    return data


# --- processes ------------------------------------------------------------------------------------
def parse_proc_status(text: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not text:
        return out
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if sep:
            out[key.strip()] = rest.strip()
    return out


def _kb(value: str) -> int:
    parts = value.split()
    try:
        return int(parts[0]) if parts else 0
    except ValueError:
        return 0


def top_processes(n: int = TOP_N, root_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Top-*n* processes by ``VmRSS`` from ``/proc/<pid>/status`` (``[{"pid","name","rss_mb"}]``)."""
    base = common.root() if root_dir is None else root_dir
    proc_dir = common.path("/proc", base)
    rows: List[Tuple[int, int, str]] = []
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        status = parse_proc_status(common.read_text(os.path.join(proc_dir, entry, "status")))
        if not status or "VmRSS" not in status:
            continue
        rss_kb = _kb(status.get("VmRSS", "0"))
        if rss_kb <= 0:
            continue
        rows.append((rss_kb, int(entry), status.get("Name", "?")))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [{"pid": pid, "name": name, "rss_mb": round(rss_kb / 1024.0, 1)} for rss_kb, pid, name in rows[:n]]


# --- zram -----------------------------------------------------------------------------------------
def parse_swaps(text: Optional[str]) -> List[Dict[str, Any]]:
    """``/proc/swaps`` → ``[{"device","type","size_mb","used_mb","priority"}]``."""
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            out.append({"device": parts[0], "type": parts[1], "size_mb": int(parts[2]) // 1024,
                        "used_mb": int(parts[3]) // 1024, "priority": int(parts[4])})
        except ValueError:
            continue
    return out


def parse_comp_algorithm(text: Optional[str]) -> str:
    """``lzo lzo-rle [zstd]`` → ``zstd``."""
    if not text:
        return ""
    m = re.search(r"\[([^\]]+)\]", text)
    return m.group(1) if m else text.split()[0] if text.split() else ""


def parse_mm_stat(text: Optional[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not text:
        return out
    parts = text.split()
    for name, value in zip(_MM_STAT_FIELDS, parts):
        try:
            out[name] = int(value)
        except ValueError:
            out[name] = 0
    return out


def zram_devices(root_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every ``/sys/block/zram*`` device with size, algorithm, mm_stat and swap usage."""
    base = common.root() if root_dir is None else root_dir
    swaps = {s["device"]: s for s in parse_swaps(common.read_text(common.path("/proc/swaps", base)))}
    devices: List[Dict[str, Any]] = []
    for sys_dir in sorted(glob.glob(os.path.join(common.path("/sys/block", base), "zram*"))):
        name = os.path.basename(sys_dir)
        disksize = _kb(common.read_first_line(os.path.join(sys_dir, "disksize")) or "0")
        mm = parse_mm_stat(common.read_first_line(os.path.join(sys_dir, "mm_stat")))
        swap = swaps.get("/dev/" + name, {})
        orig = mm.get("orig_data_size", 0)
        compr = mm.get("compr_data_size", 0)
        devices.append({
            "name": name,
            "disksize_mb": disksize // (1024 * 1024),
            "algorithm": parse_comp_algorithm(common.read_first_line(os.path.join(sys_dir, "comp_algorithm"))),
            "orig_data_mb": round(orig / (1024 * 1024), 1),
            "compr_data_mb": round(compr / (1024 * 1024), 1),
            "mem_used_mb": round(mm.get("mem_used_total", 0) / (1024 * 1024), 1),
            "ratio": round(orig / compr, 2) if compr else 0.0,
            "swap_active": bool(swap),
            "swap_used_mb": swap.get("used_mb", 0),
            "swap_priority": swap.get("priority"),
        })
    return devices


def zram_summary(root_dir: Optional[str] = None) -> Dict[str, Any]:
    devices = zram_devices(root_dir)
    swaps = parse_swaps(common.read_text(common.path("/proc/swaps", common.root() if root_dir is None else root_dir)))
    return {
        "active": any(d["swap_active"] for d in devices),
        "devices": devices,
        "total_mb": sum(d["disksize_mb"] for d in devices),
        "other_swaps": [s for s in swaps if not s["device"].startswith("/dev/zram")],
    }


# --- governor -------------------------------------------------------------------------------------
def governor_info(root_dir: Optional[str] = None) -> Dict[str, Any]:
    base = common.root() if root_dir is None else root_dir
    cpu0 = os.path.join(common.path(common.CPU_DIR, base), "cpu0", "cpufreq")
    current = common.read_first_line(os.path.join(cpu0, "scaling_governor"))
    available = common.read_first_line(os.path.join(cpu0, "scaling_available_governors")).split()
    driver = common.read_first_line(os.path.join(cpu0, "scaling_driver"))
    epp = common.read_first_line(os.path.join(cpu0, "energy_performance_preference"))
    epp_available = common.read_first_line(os.path.join(cpu0, "energy_performance_available_preferences")).split()
    return {"current": current or None, "available": available, "driver": driver or None,
            "epp": epp or None, "epp_available": epp_available}


# --- units ----------------------------------------------------------------------------------------
def unit_states(ctx: Context, units: List[str]) -> Dict[str, Dict[str, str]]:
    """``{unit: {"enabled": ..., "active": ...}}`` via ``systemctl is-enabled/is-active``.

    ``"unknown"`` when systemctl is unavailable (non-Linux, no runner injected under a test
    root).  One batched call per query so status stays fast.
    """
    result: Dict[str, Dict[str, str]] = {u: {"enabled": "unknown", "active": "unknown"} for u in units}
    if not units or not ctx.can_systemctl:
        return result
    if ctx.root and not ctx.commands_allowed:
        return result
    for key, verb in (("enabled", "is-enabled"), ("active", "is-active")):
        res = ctx.run(ctx.systemctl_args(verb, *units), timeout=30)
        lines = [ln.strip() for ln in res.out.splitlines()]
        states = [ln for ln in lines if ln and " " not in ln and not ln.startswith("Failed")]
        if len(states) == len(units):
            for unit, state in zip(units, states):
                result[unit][key] = state
        else:  # unit not found lines interleave; fall back to per-unit queries
            for unit in units:
                single = ctx.run(ctx.systemctl_args(verb, unit), timeout=15)
                first = single.out.strip().splitlines()[0].strip() if single.out.strip() else ""
                result[unit][key] = first if first and " " not in first else ("not-found" if not first else "unknown")
    return result


# --- compositor -----------------------------------------------------------------------------------
def compositor_state(ctx: Context) -> Dict[str, Any]:
    """``{"picom": bool|None, "xfwm_compositing": bool|None, "state": picom|xfwm|none|unknown}``."""
    picom: Optional[bool] = None
    xfwm: Optional[bool] = None
    if not (ctx.root and not ctx.commands_allowed):
        if ctx.which("pgrep"):
            res = ctx.run(["pgrep", "-x", "picom"], timeout=10)
            picom = res.code == 0 and bool(res.out.strip())
        if ctx.which("xfconf-query"):
            res = ctx.run(["xfconf-query", "-c", "xfwm4", "-p", "/general/use_compositing"], timeout=10)
            if res.ok:
                xfwm = res.out.strip().lower() == "true"
    if picom:
        state = "picom"
    elif xfwm:
        state = "xfwm"
    elif picom is None and xfwm is None:
        state = "unknown"
    else:
        state = "none"
    return {"picom": picom, "xfwm_compositing": xfwm, "state": state}


# --- transparent hugepages --------------------------------------------------------------------
def parse_bracketed(text: Optional[str]) -> Optional[str]:
    """``always [madvise] never`` → ``madvise`` (the selected value)."""
    if not text:
        return None
    m = re.search(r"\[([^\]]+)\]", text)
    if m:
        return m.group(1)
    parts = text.split()
    return parts[0] if parts else None


def thp_mode(root_dir: Optional[str] = None) -> Optional[str]:
    base = common.root() if root_dir is None else root_dir
    return parse_bracketed(common.read_first_line(common.path("/sys/kernel/mm/transparent_hugepage/enabled", base)))


# --- power profile --------------------------------------------------------------------------------
def power_profile(ctx: Context) -> Optional[str]:
    if ctx.root and not ctx.commands_allowed:
        return None
    if not ctx.which("powerprofilesctl"):
        return None
    res = ctx.run(["powerprofilesctl", "get"], timeout=10)
    if not res.ok:
        return None
    return res.out.strip() or None


# --- mode / verdict -------------------------------------------------------------------------------
def current_mode(root_dir: Optional[str] = None) -> str:
    """Effective mode: lindos-core's ``effective_mode()`` when importable, else system.json, else everyday."""
    base = common.root() if root_dir is None else root_dir
    if base == common.root():
        try:
            from lindos import config as core_config  # type: ignore

            mode = core_config.effective_mode()
            if isinstance(mode, str) and mode in common.MODE_IDS:
                return mode
        except Exception:
            pass
    data = common.read_json(common.path(common.SYSTEM_CONF, base))
    mode = data.get("mode")
    return mode if isinstance(mode, str) and mode in common.MODE_IDS else "everyday"


def target_range(mode: str) -> Tuple[int, int]:
    if mode == "lite":
        return common.LITE_TARGET_MIN_MB, common.LITE_TARGET_MAX_MB
    return common.TARGET_MIN_MB, common.TARGET_MAX_MB


def verdict(used_mb: int, mode: str = "everyday") -> Dict[str, Any]:
    """The verdict line: ``Idle RAM X MB — target 350–500 MB (Lite: 300–380)``."""
    lo, hi = target_range(mode)
    if used_mb <= 0:
        rating = "unknown"
    elif used_mb < lo:
        rating = "below target"
    elif used_mb <= hi:
        rating = "within target"
    elif used_mb <= hi + 150:
        rating = "slightly above target"
    else:
        rating = "above target"
    line = (f"Idle RAM {used_mb} MB — target {common.TARGET_MIN_MB}–{common.TARGET_MAX_MB} MB "
            f"(Lite: {common.LITE_TARGET_MIN_MB}–{common.LITE_TARGET_MAX_MB})")
    if rating != "unknown":
        line += f" → {rating}" + (f" for {mode} mode" if mode == "lite" else "")
    return {"line": line, "rating": rating, "target_min": lo, "target_max": hi, "used_mb": used_mb}


def score(used_mb: int, mode: str = "everyday") -> int:
    """0–100 score vs the target band (100 inside/below the band, decreasing above it)."""
    lo, hi = target_range(mode)
    if used_mb <= 0:
        return 0
    if used_mb <= hi:
        return 100
    over = used_mb - hi
    return max(0, 100 - int(over * 100 / max(hi, 1)))


# --- scheduler ------------------------------------------------------------------------------------
def _sched_status(ctx: Context) -> Dict[str, Any]:
    """Active sched_ext scheduler + kernel support (via :mod:`lindos_tune.sched`)."""
    from . import sched as lsched

    try:
        return lsched.status(ctx)
    except Exception:  # never let a scheduler probe break `status`
        return {"sched_ext": False, "active": None, "present": False, "state": None, "loader": False}


# --- snapshot -------------------------------------------------------------------------------------
def snapshot(ctx: Optional[Context] = None, whitelist: Optional[List[str]] = None, top_n: int = TOP_N) -> Dict[str, Any]:
    """Everything ``lindos-tune status`` prints, as a JSON-able dict."""
    ctx = ctx or Context()
    if whitelist is None:
        from . import services

        whitelist = services.load_whitelist(ctx)
    ram = ram_snapshot(ctx.root)
    mode = current_mode(ctx.root)
    used = int(ram.get("used", 0) or 0)
    units = unit_states(ctx, [u for u in whitelist if u])
    enabled = sorted(u for u, st in units.items() if st.get("enabled") in ("enabled", "static", "alias", "indirect"))
    active = sorted(u for u, st in units.items() if st.get("active") == "active")
    data: Dict[str, Any] = {
        "mode": mode,
        "ram": ram,
        "zram": zram_summary(ctx.root),
        "top": top_processes(top_n, ctx.root),
        "units": units,
        "units_enabled": enabled,
        "units_active": active,
        "governor": governor_info(ctx.root),
        "power_profile": power_profile(ctx),
        "compositor": compositor_state(ctx),
        "thp": thp_mode(ctx.root),
        "sched": _sched_status(ctx),
        "verdict": verdict(used, mode),
        "score": score(used, mode),
        "kernel": kernel_release(ctx.root),
        "targets": {"default": [common.TARGET_MIN_MB, common.TARGET_MAX_MB],
                    "lite": [common.LITE_TARGET_MIN_MB, common.LITE_TARGET_MAX_MB]},
    }
    return data


def kernel_release(root_dir: Optional[str] = None) -> str:
    base = common.root() if root_dir is None else root_dir
    text = common.read_first_line(common.path("/proc/sys/kernel/osrelease", base))
    if text:
        return text
    if not base:
        try:
            return os.uname().release  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return ""
    return ""


# --- rendering ------------------------------------------------------------------------------------
def _fmt_mb(value: Any) -> str:
    try:
        return f"{int(value)} MB"
    except (TypeError, ValueError):
        return "?"


def render_text(snap: Dict[str, Any]) -> str:
    ram = snap.get("ram", {})
    lines: List[str] = []
    lines.append(f"Lindos tune status — mode: {snap.get('mode', '?')}" + (f", kernel {snap['kernel']}" if snap.get("kernel") else ""))
    lines.append("")
    lines.append(f"RAM      used {_fmt_mb(ram.get('used'))} / total {_fmt_mb(ram.get('total'))}, available {_fmt_mb(ram.get('available'))}"
                 + (f", swap used {_fmt_mb(ram.get('swap_used'))} / {_fmt_mb(ram.get('swap_total'))}" if ram.get("swap_total") else ", no swap"))
    zram = snap.get("zram", {})
    if zram.get("devices"):
        for dev in zram["devices"]:
            state = "active" if dev.get("swap_active") else "inactive"
            lines.append(f"zram     {dev['name']}: {dev.get('disksize_mb', 0)} MB {dev.get('algorithm') or ''} {state}, "
                         f"used {dev.get('swap_used_mb', 0)} MB, compressed {dev.get('orig_data_mb', 0)}→{dev.get('compr_data_mb', 0)} MB"
                         + (f" (ratio {dev['ratio']})" if dev.get("ratio") else "") + (f", prio {dev['swap_priority']}" if dev.get("swap_priority") is not None else ""))
    else:
        lines.append("zram     none configured (lindos-tune zram <percent> to enable)")
    gov = snap.get("governor", {})
    gov_line = f"governor {gov.get('current') or 'n/a'}"
    if gov.get("driver"):
        gov_line += f" ({gov['driver']})"
    if gov.get("epp"):
        gov_line += f", EPP {gov['epp']}"
    if snap.get("power_profile"):
        gov_line += f", power profile {snap['power_profile']}"
    lines.append(gov_line)
    comp = snap.get("compositor", {})
    lines.append(f"compositor {comp.get('state', 'unknown')}" + (" (picom running)" if comp.get("picom") else ""))
    sched = snap.get("sched", {})
    sched_line = "scheduler " + (sched.get("active") or "default (CFS/EEVDF)")
    if not sched.get("sched_ext"):
        sched_line += " — sched_ext not available (install lindos-kernel)"
    lines.append(sched_line)
    if snap.get("thp"):
        lines.append(f"THP      {snap['thp']}")
    lines.append("")
    top = snap.get("top", [])
    if top:
        lines.append("Top processes by RSS:")
        for row in top:
            lines.append(f"  {row['rss_mb']:8.1f} MB  {row['name']} (pid {row['pid']})")
    else:
        lines.append("Top processes by RSS: n/a (no /proc)")
    lines.append("")
    enabled = snap.get("units_enabled", [])
    lines.append("Enabled units of interest: " + (", ".join(enabled) if enabled else "none / unknown"))
    unknown = [u for u, st in snap.get("units", {}).items() if st.get("enabled") == "unknown"]
    if unknown and len(unknown) == len(snap.get("units", {})):
        lines.append("  (unit states unavailable here — systemctl not reachable)")
    lines.append("")
    lines.append(snap.get("verdict", {}).get("line", ""))
    lines.append(f"score: {snap.get('score', 0)}/100")
    return "\n".join(lines)


def render_json(snap: Dict[str, Any]) -> str:
    return json.dumps(snap, indent=2, sort_keys=True)


def run_status(ctx: Optional[Context] = None, as_json: bool = False) -> str:
    snap = snapshot(ctx)
    return render_json(snap) if as_json else render_text(snap)


__all__ = [
    "TOP_N", "parse_meminfo", "meminfo", "ram_from_meminfo", "ram_snapshot", "parse_proc_status",
    "top_processes", "parse_swaps", "parse_comp_algorithm", "parse_mm_stat", "zram_devices",
    "zram_summary", "governor_info", "unit_states", "compositor_state", "power_profile",
    "parse_bracketed", "thp_mode", "current_mode", "target_range", "verdict", "score", "snapshot",
    "kernel_release", "render_text", "render_json", "run_status",
]
