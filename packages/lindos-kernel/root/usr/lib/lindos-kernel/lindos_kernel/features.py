"""Probe the *running* kernel for the Lindos performance features (SPEC-KERNEL §15.4).

Everything reads from ``LINDOS_ROOT``-relative locations so the whole thing is testable on
Windows against a faked ``/proc`` + ``/sys`` + ``/boot`` + ``/dev`` tree:

* ``/dev/ntsync``                                  → ntsync present
* ``/sys/kernel/sched_ext/``                       → sched_ext present (+ active ops name)
* ``/boot/config-<rel>`` or ``/proc/config.gz``    → CONFIG_HZ, preempt model, kconfig cross-check
* ``/sys/kernel/mm/transparent_hugepage/enabled``  → THP mode
* ``/proc/sys/net/ipv4/tcp_congestion_control``    → active TCP congestion control
* ``/sys/kernel/mm/lru_gen/enabled``               → MGLRU on/off

The running release comes from ``$LINDOS_KERNEL_RELEASE`` (tests), else ``os.uname()``, else the
first ``/boot/config-*`` found.  No probe ever raises: an unreadable/absent source means the
feature reads as absent/unknown, never an error.
"""
from __future__ import annotations

import glob
import gzip
import os
import re
from typing import Dict, List, Optional

from . import resolve
from . import manifest as _manifest

RELEASE_ENV = "LINDOS_KERNEL_RELEASE"
_CONFIG_LINE_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")
_BRACKET_RE = re.compile(r"\[([a-z0-9_-]+)\]")


# --- running kernel identity --------------------------------------------------------------
def running_release() -> str:
    """``uname -r`` equivalent: env override, else ``os.uname()``, else a ``/boot`` guess."""
    override = os.environ.get(RELEASE_ENV)
    if override:
        return override
    fn = getattr(os, "uname", None)
    if fn is not None:
        try:
            return fn().release
        except OSError:  # pragma: no cover
            pass
    for path in sorted(glob.glob(os.path.join(resolve("/boot"), "config-*"))):
        base = os.path.basename(path)
        if base.startswith("config-"):
            return base[len("config-"):]
    return ""


def is_lindos_kernel(release: Optional[str] = None) -> bool:
    """True when the running kernel carries the Lindos localversion (``-lindos``)."""
    rel = running_release() if release is None else release
    return "-lindos" in rel


# --- kernel config (from /boot/config-<rel> or /proc/config.gz) ---------------------------
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def config_source() -> Optional[str]:
    """Resolved path of the readable kernel config, or ``None``."""
    rel = running_release()
    candidates: List[str] = []
    if rel:
        candidates.append(resolve(f"/boot/config-{rel}"))
    candidates.append(resolve("/proc/config.gz"))
    if not rel:
        candidates.extend(sorted(glob.glob(os.path.join(resolve("/boot"), "config-*"))))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def read_kernel_config() -> Dict[str, str]:
    """``CONFIG_* -> value`` from the running kernel's config (``{}`` if none readable)."""
    path = config_source()
    if not path:
        return {}
    text: Optional[str]
    if path.endswith(".gz"):
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except (OSError, ValueError, EOFError):
            text = None
    else:
        text = _read_text(path)
    if text is None:
        return {}
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _CONFIG_LINE_RE.match(line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            out[match.group(1)] = value
    return out


def _config_hz(cfg: Dict[str, str]) -> Optional[int]:
    raw = cfg.get("CONFIG_HZ")
    if raw and raw.isdigit():
        return int(raw)
    for key, hz in (("CONFIG_HZ_1000", 1000), ("CONFIG_HZ_300", 300),
                    ("CONFIG_HZ_250", 250), ("CONFIG_HZ_100", 100)):
        if cfg.get(key) == "y":
            return hz
    return None


def _preempt_model(cfg: Dict[str, str]) -> Optional[str]:
    for key, name in (("CONFIG_PREEMPT_RT", "rt"), ("CONFIG_PREEMPT", "full"),
                      ("CONFIG_PREEMPT_VOLUNTARY", "voluntary"), ("CONFIG_PREEMPT_NONE", "none")):
        if cfg.get(key) == "y":
            return name
    return None


# --- sysfs / dev probes -------------------------------------------------------------------
def has_ntsync() -> bool:
    return os.path.exists(resolve("/dev/ntsync"))


def sched_ext_present() -> bool:
    return os.path.isdir(resolve("/sys/kernel/sched_ext"))


def sched_ext_active() -> Optional[str]:
    """Name of the attached scx scheduler (``root/ops``), or ``None`` when disabled/absent."""
    root_ops = resolve("/sys/kernel/sched_ext/root/ops")
    name = _read_text(root_ops)
    if name and name.strip():
        return name.strip()
    return None


def thp_mode() -> Optional[str]:
    """Active transparent-hugepage mode (``madvise``/``always``/``never``) or ``None``."""
    text = _read_text(resolve("/sys/kernel/mm/transparent_hugepage/enabled"))
    if not text:
        return None
    match = _BRACKET_RE.search(text)
    return match.group(1) if match else None


def mglru_enabled() -> Optional[bool]:
    """MGLRU on/off from ``/sys/kernel/mm/lru_gen/enabled`` (bitmask; ``None`` if absent)."""
    text = _read_text(resolve("/sys/kernel/mm/lru_gen/enabled"))
    if text is None:
        return None
    token = text.strip().split()[0] if text.strip() else "0"
    try:
        if token.lower().startswith("0x"):
            return int(token, 16) != 0
        return int(token) != 0
    except ValueError:
        return None


def tcp_congestion_control() -> Optional[str]:
    text = _read_text(resolve("/proc/sys/net/ipv4/tcp_congestion_control"))
    return text.strip() if text and text.strip() else None


# --- feature roll-up ----------------------------------------------------------------------
def probe() -> Dict[str, Dict[str, object]]:
    """Probe every headline feature.  ``id -> {present: bool|None, detail: str}``."""
    cfg = read_kernel_config()
    hz = _config_hz(cfg)
    preempt = _preempt_model(cfg)
    thp = thp_mode()
    tcp = tcp_congestion_control()
    mglru = mglru_enabled()
    scx = sched_ext_present()
    scx_ops = sched_ext_active()

    result: Dict[str, Dict[str, object]] = {}
    result["ntsync"] = {
        "present": has_ntsync(),
        "detail": resolve("/dev/ntsync") if has_ntsync() else "/dev/ntsync absent (Wine falls back to fsync)",
    }
    result["sched_ext"] = {
        "present": scx,
        "detail": (f"active: {scx_ops}" if scx_ops else "available, no scheduler attached") if scx
        else "no sched_ext (install lindos-kernel)",
    }
    result["preempt_full"] = {
        "present": (preempt == "full") if preempt is not None else None,
        "detail": f"preempt model: {preempt}" if preempt else "preempt model unknown (no kernel config)",
    }
    result["hz1000"] = {
        "present": (hz == 1000) if hz is not None else None,
        "detail": f"CONFIG_HZ={hz}" if hz is not None else "CONFIG_HZ unknown (no kernel config)",
    }
    result["mglru"] = {
        "present": mglru if mglru is not None else (cfg.get("CONFIG_LRU_GEN") == "y" or None),
        "detail": ("enabled" if mglru else "disabled") if mglru is not None else "lru_gen sysfs absent",
    }
    result["bbr"] = {
        "present": (tcp == "bbr") if tcp is not None else None,
        "detail": f"tcp_congestion_control={tcp}" if tcp else "tcp cc unknown",
    }
    result["thp"] = {
        "present": thp is not None,
        "detail": f"transparent_hugepage={thp}" if thp else "thp sysfs absent",
    }
    return result


def status() -> Dict[str, object]:
    """Full status blob for ``lindos-kernel status`` (running kernel + per-feature probe)."""
    rel = running_release()
    return {
        "release": rel,
        "is_lindos": is_lindos_kernel(rel),
        "config_source": config_source(),
        "features": probe(),
        "sched_ext_active": sched_ext_active(),
        "thp": thp_mode(),
        "tcp_congestion_control": tcp_congestion_control(),
    }


def cross_check(manifest: Optional[_manifest.Manifest] = None) -> List[Dict[str, object]]:
    """Manifest features vs the running kernel: one row per manifest feature."""
    man = manifest or _manifest.load()
    probed = probe()
    rows: List[Dict[str, object]] = []
    for feat in man.features:
        info = probed.get(feat.id, {"present": None, "detail": "not probed"})
        rows.append({
            "id": feat.id,
            "kconfig": feat.kconfig,
            "why": feat.why,
            "since": feat.since,
            "present": info.get("present"),
            "detail": info.get("detail"),
        })
    return rows


__all__ = [
    "RELEASE_ENV", "running_release", "is_lindos_kernel", "config_source", "read_kernel_config",
    "has_ntsync", "sched_ext_present", "sched_ext_active", "thp_mode", "mglru_enabled",
    "tcp_congestion_control", "probe", "status", "cross_check",
]
