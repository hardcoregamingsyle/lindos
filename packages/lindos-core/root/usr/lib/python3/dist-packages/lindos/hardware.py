"""Hardware inspection helpers (SPEC §4.5).

Everything reads ``/proc``, ``/sys`` or well-known CLIs (``lspci``, ``sensors``, ``xrandr``)
and degrades to empty/unknown values on other operating systems — nothing runs at import
time and nothing raises for a missing tool.
"""

from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from . import helper as lhelper

log = logging.getLogger("lindos.hardware")

CPUFREQ_DIR = "/sys/devices/system/cpu"
GPU_VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel"}
GPU_CLASS_RE = re.compile(r"(VGA compatible controller|3D controller|Display controller)", re.I)


# --- small utilities ---------------------------------------------------------------------
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        return True
    except OSError:
        return False


def _run(cmd: List[str], timeout: float = 15) -> Optional[str]:
    exe = shutil.which(cmd[0])
    if not exe:
        return None
    try:
        proc = subprocess.run([exe] + cmd[1:], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return proc.stdout


# --- CPU ---------------------------------------------------------------------------------
def cpu_info() -> Dict[str, Any]:
    """``{"model", "vendor", "cores", "threads", "arch", "mhz", "flags_hint"}``."""
    info: Dict[str, Any] = {
        "model": platform.processor() or "",
        "vendor": "",
        "cores": 0,
        "threads": os.cpu_count() or 0,
        "arch": platform.machine() or "",
        "mhz": 0.0,
        "flags_hint": [],
    }
    text = _read("/proc/cpuinfo")
    if not text:
        info["vendor"] = _guess_vendor(info["model"])
        return info
    physical = set()
    cores_per_pkg = 0
    flags: List[str] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "model name" and not info["model"]:
            info["model"] = value
        elif key == "vendor_id" and not info["vendor"]:
            info["vendor"] = {"GenuineIntel": "intel", "AuthenticAMD": "amd"}.get(value, value.lower())
        elif key == "physical id":
            physical.add(value)
        elif key == "cpu cores":
            try:
                cores_per_pkg = int(value)
            except ValueError:
                pass
        elif key == "cpu MHz" and not info["mhz"]:
            try:
                info["mhz"] = float(value)
            except ValueError:
                pass
        elif key == "flags" and not flags:
            flags = value.split()
    if not info["vendor"]:
        info["vendor"] = _guess_vendor(info["model"])
    info["cores"] = (cores_per_pkg * max(1, len(physical))) if cores_per_pkg else info["threads"]
    info["flags_hint"] = [f for f in ("avx2", "avx512f", "sse4_2", "vmx", "svm", "hypervisor") if f in flags]
    return info


def _guess_vendor(model: str) -> str:
    m = model.lower()
    if "intel" in m:
        return "intel"
    if "amd" in m or "ryzen" in m:
        return "amd"
    return "other" if m else ""


# --- GPU ---------------------------------------------------------------------------------
def _sysfs_gpus() -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if "-" in os.path.basename(card):  # connectors like card0-HDMI-A-1
            continue
        dev = os.path.join(card, "device")
        vendor = (_read(os.path.join(dev, "vendor")) or "").lower().replace("0x", "")
        device = (_read(os.path.join(dev, "device")) or "").lower().replace("0x", "")
        if not vendor:
            continue
        driver = ""
        try:
            driver = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except OSError:
            pass
        gpus.append({
            "vendor": GPU_VENDORS.get(vendor, "other"),
            "name": f"PCI {vendor}:{device}",
            "pci_id": f"{vendor}:{device}",
            "driver": driver,
            "slot": os.path.basename(os.path.realpath(dev)),
        })
    return gpus


def gpu_info() -> List[Dict[str, Any]]:
    """List of ``{"vendor": nvidia|amd|intel|other, "name", "pci_id", "driver", "slot"}``.

    Parses ``lspci -nnk`` when available, else sysfs.  Empty list on other OSes.
    """
    out = _run(["lspci", "-nnk"], timeout=20)
    gpus: List[Dict[str, Any]] = []
    if out:
        current: Optional[Dict[str, Any]] = None
        for line in out.splitlines():
            if not line.startswith(("\t", " ")):
                current = None
                if not GPU_CLASS_RE.search(line):
                    continue
                m = re.match(r"^(\S+)\s+(.*?)\s*\[([0-9a-f]{4}):([0-9a-f]{4})\](?:\s*\(rev\s+\S+\))?\s*$", line, re.I)
                if not m:
                    m2 = re.match(r"^(\S+)\s+(.*)$", line)
                    slot, desc = (m2.group(1), m2.group(2)) if m2 else ("", line)
                    vendor_id, device_id = "", ""
                else:
                    slot, desc, vendor_id, device_id = m.group(1), m.group(2), m.group(3).lower(), m.group(4).lower()
                desc = re.sub(r"^\S.*?:\s*", "", desc, count=1)  # drop "VGA compatible controller: "
                current = {
                    "vendor": GPU_VENDORS.get(vendor_id, _guess_gpu_vendor(desc)),
                    "name": desc.strip(),
                    "pci_id": f"{vendor_id}:{device_id}" if vendor_id else "",
                    "driver": "",
                    "slot": slot,
                }
                gpus.append(current)
            elif current is not None:
                stripped = line.strip()
                if stripped.lower().startswith("kernel driver in use:"):
                    current["driver"] = stripped.split(":", 1)[1].strip()
    if not gpus:
        gpus = _sysfs_gpus()
    return gpus


def _guess_gpu_vendor(desc: str) -> str:
    d = desc.lower()
    if "nvidia" in d:
        return "nvidia"
    if "amd" in d or "ati " in d or "radeon" in d:
        return "amd"
    if "intel" in d:
        return "intel"
    return "other"


def primary_gpu_vendor() -> str:
    """Vendor of the first (or discrete, if any) GPU: nvidia|amd|intel|other|unknown."""
    gpus = gpu_info()
    if not gpus:
        return "unknown"
    for g in gpus:
        if g["vendor"] == "nvidia":
            return "nvidia"
    for g in gpus:
        if g["vendor"] == "amd" and any(other["vendor"] == "intel" for other in gpus):
            return "amd"
    return gpus[0]["vendor"]


# --- RAM ---------------------------------------------------------------------------------
def meminfo() -> Dict[str, int]:
    """Raw ``/proc/meminfo`` in kB (empty dict when unavailable)."""
    text = _read("/proc/meminfo")
    if not text:
        return {}
    out: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[key.strip()] = int(parts[0])
            except ValueError:
                pass
    return out


def _windows_mem() -> Dict[str, int]:
    """Total/available MB via GlobalMemoryStatusEx (development convenience only)."""
    if os.name != "nt":
        return {}
    try:
        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = MemStatus()
        stat.dwLength = ctypes.sizeof(MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return {}
        return {"total": int(stat.ullTotalPhys // (1024 * 1024)), "available": int(stat.ullAvailPhys // (1024 * 1024))}
    except Exception:
        return {}


def ram_info() -> Dict[str, int]:
    """``{"total", "used", "available", "free", "swap_total", "swap_used"}`` in MB.

    ``used`` = total − available (what ``free -m`` prints on procps ≥ 4).  Zeros when
    ``/proc/meminfo`` is unavailable (Windows gets total/available from the Win32 API).
    """
    mi = meminfo()
    if not mi:
        win = _windows_mem()
        total = win.get("total", 0)
        avail = win.get("available", 0)
        return {"total": total, "used": max(0, total - avail), "available": avail, "free": avail,
                "swap_total": 0, "swap_used": 0}
    kb = 1024
    total = mi.get("MemTotal", 0) // kb
    free = mi.get("MemFree", 0) // kb
    available = mi.get("MemAvailable", mi.get("MemFree", 0)) // kb
    swap_total = mi.get("SwapTotal", 0) // kb
    swap_free = mi.get("SwapFree", 0) // kb
    return {
        "total": total,
        "used": max(0, total - available),
        "available": available,
        "free": free,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


# --- power ---------------------------------------------------------------------------------
def battery_present() -> bool:
    """True when ``/sys/class/power_supply`` lists a battery."""
    for entry in glob.glob("/sys/class/power_supply/*"):
        if (_read(os.path.join(entry, "type")) or "").strip().lower() == "battery":
            return True
    return False


# --- CPU frequency governor ------------------------------------------------------------------
def available_governors() -> List[str]:
    text = _read(os.path.join(CPUFREQ_DIR, "cpu0", "cpufreq", "scaling_available_governors"))
    return text.split() if text else []


def current_governor() -> Optional[str]:
    return _read(os.path.join(CPUFREQ_DIR, "cpu0", "cpufreq", "scaling_governor")) or None


def governor_paths() -> List[str]:
    """``scaling_governor`` files of every CPU (empty when cpufreq is unavailable)."""
    return sorted(glob.glob(os.path.join(CPUFREQ_DIR, "cpu[0-9]*", "cpufreq", "scaling_governor")))


def epp_paths() -> List[str]:
    return sorted(glob.glob(os.path.join(CPUFREQ_DIR, "cpu[0-9]*", "cpufreq", "energy_performance_preference")))


def available_epp() -> List[str]:
    text = _read(os.path.join(CPUFREQ_DIR, "cpu0", "cpufreq", "energy_performance_available_preferences"))
    return text.split() if text else []


def map_governor(governor: str, available: Optional[List[str]] = None) -> Optional[str]:
    """Map a requested governor onto one the driver offers (intel_pstate has no schedutil)."""
    avail = available if available is not None else available_governors()
    if not avail:
        return governor
    if governor in avail:
        return governor
    fallbacks = {
        "schedutil": ["powersave", "ondemand", "conservative"],
        "performance": ["performance"],
        "powersave": ["powersave", "conservative", "ondemand", "schedutil"],
        "ondemand": ["schedutil", "powersave"],
        "conservative": ["powersave", "schedutil"],
    }
    for candidate in fallbacks.get(governor, []):
        if candidate in avail:
            return candidate
    return None


def write_governor(governor: str) -> Dict[str, Any]:
    """Write *governor* to every CPU (needs root).  Returns ``{"ok", "written", "governor", "epp"}``."""
    target = map_governor(governor)
    result: Dict[str, Any] = {"ok": False, "written": 0, "governor": target, "epp": None}
    if not target:
        result["error"] = f"governor {governor} not available (have: {' '.join(available_governors()) or 'none'})"
        return result
    for path in governor_paths():
        if _write(path, target):
            result["written"] += 1
    epp = {"performance": "performance", "powersave": "power", "schedutil": "balance_performance"}.get(governor)
    if epp and epp_paths():
        avail = available_epp()
        if not avail or epp in avail:
            for path in epp_paths():
                _write(path, epp)
            result["epp"] = epp
    result["ok"] = result["written"] > 0
    if not result["ok"]:
        result["error"] = "no cpufreq scaling_governor files (VM or unsupported driver?)"
    return result


def set_governor(g: str) -> bool:
    """Set the governor through the helper (``set-governor``)."""
    return lhelper.run_privileged("set-governor", {"governor": g}).ok


# --- fans / temperatures -------------------------------------------------------------------------
def _sensors_json() -> Dict[str, Any]:
    out = _run(["sensors", "-j"], timeout=15)
    if not out:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def fan_sensors() -> List[Dict[str, Any]]:
    """``[{"chip", "name", "rpm"}]`` parsed from ``sensors -j`` (empty when unavailable)."""
    fans: List[Dict[str, Any]] = []
    for chip, features in _sensors_json().items():
        if not isinstance(features, dict):
            continue
        for name, values in features.items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key.startswith("fan") and key.endswith("_input"):
                    try:
                        fans.append({"chip": chip, "name": name, "rpm": int(float(value))})
                    except (TypeError, ValueError):
                        pass
    return fans


def temperatures() -> List[Dict[str, Any]]:
    """``[{"chip", "name", "celsius"}]`` from ``sensors -j``; falls back to sysfs thermal zones."""
    temps: List[Dict[str, Any]] = []
    for chip, features in _sensors_json().items():
        if not isinstance(features, dict):
            continue
        for name, values in features.items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key.startswith("temp") and key.endswith("_input"):
                    try:
                        temps.append({"chip": chip, "name": name, "celsius": round(float(value), 1)})
                    except (TypeError, ValueError):
                        pass
    if not temps:
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            raw = _read(os.path.join(zone, "temp"))
            if raw and raw.lstrip("-").isdigit():
                temps.append({"chip": os.path.basename(zone), "name": _read(os.path.join(zone, "type")) or "zone",
                              "celsius": round(int(raw) / 1000.0, 1)})
    return temps


# --- displays ------------------------------------------------------------------------------------
def refresh_rates() -> Dict[str, Dict[str, Any]]:
    """Parse ``xrandr``: ``{output: {"connected", "current_mode", "current_rate", "modes": {res: [rates]}}}``."""
    out = _run(["xrandr", "--query"], timeout=15)
    result: Dict[str, Dict[str, Any]] = {}
    if not out:
        return result
    current: Optional[str] = None
    for line in out.splitlines():
        head = re.match(r"^(\S+)\s+(connected|disconnected)\b", line)
        if head:
            current = head.group(1)
            result[current] = {"connected": head.group(2) == "connected", "current_mode": None,
                               "current_rate": None, "modes": {}, "primary": " primary " in line + " "}
            continue
        if current is None or not line.startswith(" "):
            continue
        parts = line.split()
        if not parts or not re.match(r"^\d+x\d+", parts[0]):
            continue
        res = parts[0]
        rates: List[float] = []
        for token in parts[1:]:
            m = re.match(r"^(\d+(?:\.\d+)?)([*+]*)$", token)
            if not m:
                continue
            rate = float(m.group(1))
            rates.append(rate)
            if "*" in m.group(2):
                result[current]["current_mode"] = res
                result[current]["current_rate"] = rate
        if rates:
            result[current]["modes"].setdefault(res, []).extend(rates)
    return result


def summary() -> Dict[str, Any]:
    """Everything at once (used by ``lindos-settings about`` and bug reports)."""
    return {
        "cpu": cpu_info(),
        "gpus": gpu_info(),
        "ram": ram_info(),
        "battery": battery_present(),
        "governor": current_governor(),
        "governors": available_governors(),
        "kernel": platform.release(),
        "arch": platform.machine(),
    }


__all__ = [
    "cpu_info", "gpu_info", "primary_gpu_vendor", "ram_info", "meminfo", "battery_present",
    "available_governors", "current_governor", "governor_paths", "map_governor", "write_governor",
    "set_governor", "fan_sensors", "temperatures", "refresh_rates", "summary", "GPU_VENDORS",
]
