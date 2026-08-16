"""RAM snapshot / report (SPEC §4.9, §0.1).

``snapshot()`` returns ``{"total", "used", "available", "top": [(name, rss_mb), …], …}`` in
MB, measured the way ``free -m`` reports "used" (total − available) — this is the number the
**350–500 MB idle target** refers to.  ``report(fmt)`` renders it as text or JSON.

Linux data comes from ``/proc/meminfo`` and ``/proc/<pid>/{comm,status}``; other OSes get
totals from :mod:`lindos.hardware` (Windows: ``tasklist`` for the top list) so the module and
its tests work everywhere.
"""

from __future__ import annotations

import csv
import glob
import io
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import hardware

TARGET_MB: Tuple[int, int] = (350, 500)   # SPEC §0.1 idle target after login, no apps open
LITE_TARGET_MB: Tuple[int, int] = (300, 380)
STOCK_MINT_MB: Tuple[int, int] = (600, 750)  # measured baseline of stock Mint 22 XFCE (docs/RAM-BUDGET.md)


# --- per-process RSS ---------------------------------------------------------------------------
def _linux_processes() -> List[Dict[str, Any]]:
    procs: List[Dict[str, Any]] = []
    for status_path in glob.glob("/proc/[0-9]*/status"):
        pid_dir = os.path.dirname(status_path)
        try:
            with open(status_path, "r", encoding="utf-8", errors="replace") as fh:
                name = ""
                rss_kb = 0
                for line in fh:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            rss_kb = int(parts[1])
                        break
        except OSError:
            continue
        if not rss_kb:
            continue  # kernel threads / zombies
        try:
            pid = int(os.path.basename(pid_dir))
        except ValueError:
            continue
        procs.append({"pid": pid, "name": name or str(pid), "rss_mb": round(rss_kb / 1024.0, 1)})
    return procs


def _windows_processes() -> List[Dict[str, Any]]:
    tasklist = shutil.which("tasklist")
    if not tasklist:
        return []
    try:
        proc = subprocess.run([tasklist, "/FO", "CSV", "/NH"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    procs: List[Dict[str, Any]] = []
    for row in csv.reader(io.StringIO(proc.stdout or "")):
        if len(row) < 5:
            continue
        digits = "".join(ch for ch in row[4] if ch.isdigit())
        if not digits or not row[1].isdigit():
            continue
        procs.append({"pid": int(row[1]), "name": row[0], "rss_mb": round(int(digits) / 1024.0, 1)})
    return procs


def processes() -> List[Dict[str, Any]]:
    """``[{"pid", "name", "rss_mb"}]`` for every process (empty when unsupported)."""
    if os.path.isdir("/proc"):
        procs = _linux_processes()
        if procs:
            return procs
    if os.name == "nt":
        return _windows_processes()
    return []


def top_processes(n: int = 10, aggregate: bool = False) -> List[Tuple[str, float]]:
    """Top *n* ``(name, rss_mb)``; ``aggregate=True`` sums same-named processes (e.g. browsers)."""
    procs = processes()
    if aggregate:
        totals: Dict[str, float] = {}
        for p in procs:
            totals[p["name"]] = totals.get(p["name"], 0.0) + float(p["rss_mb"])
        items = [(name, round(mb, 1)) for name, mb in totals.items()]
    else:
        items = [(p["name"], float(p["rss_mb"])) for p in procs]
    items.sort(key=lambda t: t[1], reverse=True)
    return items[:max(0, n)]


# --- zram -------------------------------------------------------------------------------------
def zram_info() -> Dict[str, Any]:
    """``{"devices": [{"name", "disksize_mb", "used_mb", "orig_mb", "algorithm"}], "total_mb"}``."""
    devices: List[Dict[str, Any]] = []
    for dev in sorted(glob.glob("/sys/block/zram*")):
        entry: Dict[str, Any] = {"name": os.path.basename(dev), "disksize_mb": 0, "used_mb": 0.0,
                                 "orig_mb": 0.0, "algorithm": ""}
        try:
            with open(os.path.join(dev, "disksize"), "r", encoding="utf-8") as fh:
                entry["disksize_mb"] = int(fh.read().strip() or 0) // (1024 * 1024)
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(dev, "mm_stat"), "r", encoding="utf-8") as fh:
                parts = fh.read().split()
            if len(parts) >= 3:
                entry["orig_mb"] = round(int(parts[0]) / (1024 * 1024), 1)
                entry["used_mb"] = round(int(parts[2]) / (1024 * 1024), 1)
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(dev, "comp_algorithm"), "r", encoding="utf-8") as fh:
                text = fh.read()
            start, end = text.find("["), text.find("]")
            entry["algorithm"] = text[start + 1:end] if 0 <= start < end else text.strip()
        except OSError:
            pass
        if entry["disksize_mb"]:
            devices.append(entry)
    return {"devices": devices, "total_mb": sum(d["disksize_mb"] for d in devices)}


# --- snapshot / report -----------------------------------------------------------------------
def score(used_mb: int, target: Tuple[int, int] = TARGET_MB) -> str:
    """``"excellent"`` (below target), ``"on-target"``, ``"above-target"`` or ``"high"`` (>1.5×)."""
    lo, hi = target
    if used_mb <= 0:
        return "unknown"
    if used_mb < lo:
        return "excellent"
    if used_mb <= hi:
        return "on-target"
    if used_mb <= hi * 1.5:
        return "above-target"
    return "high"


def snapshot(top_n: int = 10, aggregate: bool = False) -> Dict[str, Any]:
    """Current RAM picture in MB (see module docs)."""
    ram = hardware.ram_info()
    used = int(ram.get("used", 0))
    return {
        "total": int(ram.get("total", 0)),
        "used": used,
        "available": int(ram.get("available", 0)),
        "free": int(ram.get("free", 0)),
        "swap_total": int(ram.get("swap_total", 0)),
        "swap_used": int(ram.get("swap_used", 0)),
        "zram": zram_info(),
        "top": top_processes(top_n, aggregate=aggregate),
        "target": list(TARGET_MB),
        "score": score(used),
    }


def report(fmt: str = "text", top_n: int = 10, aggregate: bool = False) -> str:
    """Render :func:`snapshot` as ``"text"`` (human) or ``"json"``."""
    snap = snapshot(top_n=top_n, aggregate=aggregate)
    if fmt == "json":
        return json.dumps(snap, indent=2, sort_keys=True)
    if fmt != "text":
        raise ValueError("fmt must be 'text' or 'json'")
    lines = [
        f"RAM used: {snap['used']} MB of {snap['total']} MB (available {snap['available']} MB)",
        f"Idle target: {TARGET_MB[0]}–{TARGET_MB[1]} MB → {snap['score']}",
    ]
    if snap["swap_total"]:
        lines.append(f"Swap: {snap['swap_used']} / {snap['swap_total']} MB")
    z = snap["zram"]
    if z["devices"]:
        devs = ", ".join(f"{d['name']} {d['disksize_mb']} MB ({d['algorithm'] or '?'}, {d['used_mb']} MB used)"
                         for d in z["devices"])
        lines.append(f"zram: {devs}")
    if snap["top"]:
        lines.append(f"Top {len(snap['top'])} by RSS:")
        width = max(len(name) for name, _ in snap["top"])
        for name, mb in snap["top"]:
            lines.append(f"  {name.ljust(width)}  {mb:8.1f} MB")
    else:
        lines.append("(per-process data unavailable on this system)")
    return "\n".join(lines)


def idle_ok(used_mb: Optional[int] = None) -> bool:
    """True when *used_mb* (default: now) is within or below the 350–500 MB target."""
    used = int(used_mb if used_mb is not None else hardware.ram_info().get("used", 0))
    return 0 < used <= TARGET_MB[1]


__all__ = ["TARGET_MB", "LITE_TARGET_MB", "STOCK_MINT_MB", "processes", "top_processes", "zram_info",
           "score", "snapshot", "report", "idle_ok"]
