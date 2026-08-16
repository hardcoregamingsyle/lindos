"""vfio-pci bind/unbind helpers (SPEC-VM §21.1, ``lindos-vm passthrough``).

Reading which driver a PCI device uses is root-free.  *Mutating* the binding needs root, so
these helpers only **build the exact ``pkexec`` command** for the caller to run — this module
never calls ``sudo`` and never writes to sysfs itself.  Binding a device to ``vfio-pci`` is the
standard passthrough procedure and contains no detection-evasion.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import resolve
from .domain import parse_pci

_SYS_DEV = "/sys/bus/pci/devices"


def _read_hex(path: str) -> Optional[int]:
    try:
        with open(resolve(path), "r", encoding="utf-8") as handle:
            return int(handle.read().strip(), 16)
    except (OSError, ValueError):
        return None


def current_driver(pci: str) -> Optional[str]:
    """Name of the kernel driver currently bound to *pci*, or ``None`` if unbound."""
    addr = parse_pci(pci)
    link = resolve(os.path.join(_SYS_DEV, addr, "driver"))
    try:
        if os.path.islink(link):
            return os.path.basename(os.readlink(link))
    except OSError:
        pass
    # Fallback for faked trees where a plain 'driver' dir stands in for the symlink.
    if os.path.isdir(link):
        target = None
        try:
            target = os.path.basename(os.path.realpath(link))
        except OSError:
            target = None
        if target and target != "driver":
            return target
    return None


def iommu_group(pci: str) -> Optional[int]:
    addr = parse_pci(pci)
    base = resolve("/sys/kernel/iommu_groups")
    try:
        for grp in os.listdir(base):
            if grp.isdigit() and addr in os.listdir(os.path.join(base, grp, "devices")):
                return int(grp)
    except OSError:
        return None
    return None


def list_devices() -> List[Dict[str, Any]]:
    """Every PCI device with its vendor/device ids, class, driver and IOMMU group."""
    base = resolve(_SYS_DEV)
    out: List[Dict[str, Any]] = []
    try:
        addrs = sorted(os.listdir(base))
    except OSError:
        return out
    for addr in addrs:
        vendor = _read_hex(os.path.join(_SYS_DEV, addr, "vendor"))
        device = _read_hex(os.path.join(_SYS_DEV, addr, "device"))
        klass = _read_hex(os.path.join(_SYS_DEV, addr, "class"))
        out.append({
            "pci": addr,
            "pci_id": (f"{vendor:04x}:{device:04x}"
                       if vendor is not None and device is not None else None),
            "class": f"0x{klass:06x}" if klass is not None else None,
            "driver": current_driver(addr),
            "iommu_group": iommu_group(addr),
        })
    return out


def bind_command(pci: str) -> List[str]:
    """The exact privileged command to (re)bind *pci* to ``vfio-pci``."""
    addr = parse_pci(pci)
    script = (
        f'd=/sys/bus/pci/devices/{addr}; '
        'echo vfio-pci > "$d/driver_override"; '
        '[ -e "$d/driver" ] && echo ' + addr + ' > "$d/driver/unbind"; '
        f'echo {addr} > /sys/bus/pci/drivers_probe'
    )
    return ["pkexec", "sh", "-c", script]


def unbind_command(pci: str) -> List[str]:
    """The exact privileged command to release *pci* from ``vfio-pci`` back to the host."""
    addr = parse_pci(pci)
    script = (
        f'd=/sys/bus/pci/devices/{addr}; '
        '[ -e /sys/bus/pci/drivers/vfio-pci/' + addr + ' ] && '
        'echo ' + addr + ' > /sys/bus/pci/drivers/vfio-pci/unbind; '
        'echo > "$d/driver_override"; '
        f'echo {addr} > /sys/bus/pci/drivers_probe'
    )
    return ["pkexec", "sh", "-c", script]


__all__ = [
    "current_driver", "iommu_group", "list_devices", "bind_command", "unbind_command",
]
