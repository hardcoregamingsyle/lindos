"""Host virtualization capability probe (SPEC-VM §21.1, ``lindos-vm check``).

Every read is routed through :func:`lindos_vm.resolve` so the whole probe is exercisable on
Windows against a faked ``LINDOS_ROOT`` tree containing ``/proc/cpuinfo``, ``/proc/cmdline``,
``/proc/meminfo``, ``/dev/kvm``, ``/sys/kernel/iommu_groups/`` and ``/sys/bus/pci/devices/``.

Nothing here mutates anything or needs root.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

from . import OVMF_CODE, resolve

# PCI base classes we care about for passthrough (high byte of the 24-bit class code).
_PCI_CLASS_DISPLAY = 0x03  # VGA / 3D / display controllers


def _read_text(path: str) -> Optional[str]:
    try:
        with open(resolve(path), "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _first_line_kv(text: str, key: str) -> Optional[str]:
    for line in text.splitlines():
        if line.startswith(key):
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


def cpu_info() -> Dict[str, Any]:
    """CPU vendor + whether hardware virtualization (``vmx``/``svm``) is advertised."""
    text = _read_text("/proc/cpuinfo") or ""
    vendor = _first_line_kv(text, "vendor_id") or ""
    flags: List[str] = []
    for line in text.splitlines():
        if line.startswith("flags") or line.startswith("Features"):
            flags = line.split(":", 1)[1].split() if ":" in line else []
            break
    has_vmx = "vmx" in flags
    has_svm = "svm" in flags
    ext = "vmx" if has_vmx else ("svm" if has_svm else None)
    return {
        "vendor": vendor,
        "virt_ext": ext,
        "has_virt": bool(ext),
        "vmx": has_vmx,
        "svm": has_svm,
    }


def kvm_present() -> bool:
    """True when ``/dev/kvm`` exists (KVM acceleration available)."""
    return os.path.exists(resolve("/dev/kvm"))


def iommu_status() -> Dict[str, Any]:
    """IOMMU: kernel cmdline flag present AND ``/sys/kernel/iommu_groups/`` populated."""
    cmdline = _read_text("/proc/cmdline") or ""
    tokens = cmdline.split()
    intel = "intel_iommu=on" in tokens
    amd = "amd_iommu=on" in tokens or "amd_iommu=force" in tokens
    passthrough = "iommu=pt" in tokens
    groups_dir = resolve("/sys/kernel/iommu_groups")
    groups: List[str] = []
    try:
        groups = sorted(
            (e for e in os.listdir(groups_dir) if e.isdigit()),
            key=lambda x: int(x),
        )
    except OSError:
        groups = []
    return {
        "cmdline_flag": intel or amd,
        "intel_iommu": intel,
        "amd_iommu": amd,
        "iommu_pt": passthrough,
        "groups": len(groups),
        "enabled": (intel or amd) and bool(groups),
    }


def _iommu_group_of(pci_addr: str) -> Optional[int]:
    """Map a PCI address (e.g. ``0000:01:00.0``) to its IOMMU group, if discoverable.

    Reads ``/sys/kernel/iommu_groups/<n>/devices/`` (a plain directory tree, so it is
    fakeable on Windows).  Falls back to the per-device symlink when readable.
    """
    base = resolve("/sys/kernel/iommu_groups")
    try:
        for grp in os.listdir(base):
            if not grp.isdigit():
                continue
            devdir = os.path.join(base, grp, "devices")
            try:
                if pci_addr in os.listdir(devdir):
                    return int(grp)
            except OSError:
                continue
    except OSError:
        pass
    return None


def _read_hex(path: str) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    try:
        return int(text, 16)
    except ValueError:
        return None


def gpus() -> List[Dict[str, Any]]:
    """Discover display controllers from sysfs (vendor/device/class + IOMMU group).

    Purely sysfs-driven so it is testable on Windows; ``lspci`` is not required.
    """
    base = resolve("/sys/bus/pci/devices")
    out: List[Dict[str, Any]] = []
    try:
        addrs = sorted(os.listdir(base))
    except OSError:
        return out
    for addr in addrs:
        dev = os.path.join(base, addr)
        klass = _read_hex(os.path.join("/sys/bus/pci/devices", addr, "class"))
        if klass is None:
            continue
        if (klass >> 16) != _PCI_CLASS_DISPLAY:
            continue
        vendor = _read_hex(os.path.join("/sys/bus/pci/devices", addr, "vendor"))
        device = _read_hex(os.path.join("/sys/bus/pci/devices", addr, "device"))
        driver = None
        drv_link = os.path.join(dev, "driver")
        try:
            if os.path.islink(drv_link):
                driver = os.path.basename(os.readlink(drv_link))
        except OSError:
            driver = None
        out.append({
            "pci": addr,
            "vendor_id": f"{vendor:04x}" if vendor is not None else None,
            "device_id": f"{device:04x}" if device is not None else None,
            "pci_id": (f"{vendor:04x}:{device:04x}"
                       if vendor is not None and device is not None else None),
            "vendor_name": _VENDOR_NAMES.get(vendor, None) if vendor is not None else None,
            "iommu_group": _iommu_group_of(addr),
            "driver": driver,
        })
    return out


_VENDOR_NAMES = {0x10de: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel"}


def memory() -> Dict[str, Any]:
    """Total / available RAM in MiB from ``/proc/meminfo`` (0 when unreadable)."""
    text = _read_text("/proc/meminfo") or ""
    total_kb = _meminfo_kb(text, "MemTotal:")
    avail_kb = _meminfo_kb(text, "MemAvailable:")
    return {
        "total_mib": total_kb // 1024 if total_kb else 0,
        "available_mib": avail_kb // 1024 if avail_kb else 0,
    }


def _meminfo_kb(text: str, key: str) -> int:
    for line in text.splitlines():
        if line.startswith(key):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def tools() -> Dict[str, bool]:
    """Which host tools/firmware are installed (PATH lookup + LINDOS_ROOT-aware path check)."""

    def _have(binary: str, *paths: str) -> bool:
        if shutil.which(binary):
            return True
        return any(os.path.exists(resolve(p)) for p in paths)

    qemu = _have("qemu-system-x86_64", "/usr/bin/qemu-system-x86_64")
    virsh = _have("virsh", "/usr/bin/virsh")
    libvirtd = _have("libvirtd", "/usr/sbin/libvirtd", "/usr/bin/libvirtd")
    ovmf = any(os.path.exists(resolve(p)) for p in (
        OVMF_CODE, "/usr/share/OVMF/OVMF_CODE.fd",
        "/usr/share/OVMF/OVMF_CODE.secboot.fd", "/usr/share/edk2/ovmf/OVMF_CODE.fd",
    ))
    return {"qemu": qemu, "virsh": virsh, "libvirtd": libvirtd, "ovmf": ovmf}


def probe() -> Dict[str, Any]:
    """Full capability report used by ``lindos-vm check``."""
    cpu = cpu_info()
    kvm = kvm_present()
    iommu = iommu_status()
    gpu_list = gpus()
    mem = memory()
    tool = tools()

    reasons: List[str] = []
    if not cpu["has_virt"]:
        reasons.append("CPU virtualization (vmx/svm) not present or disabled in firmware")
    if not kvm:
        reasons.append("/dev/kvm missing (KVM module not loaded or no permission)")
    if not tool["qemu"]:
        reasons.append("qemu-system-x86_64 not installed")
    if not tool["ovmf"]:
        reasons.append("OVMF/UEFI firmware (ovmf package) not installed")
    if not tool["virsh"]:
        reasons.append("virsh/libvirt-clients not installed")

    passthrough_reasons: List[str] = []
    if not iommu["enabled"]:
        passthrough_reasons.append(
            "IOMMU not enabled (need intel_iommu=on/amd_iommu=on and populated iommu_groups)")
    if not gpu_list:
        passthrough_reasons.append("no display controller found in sysfs")

    return {
        "cpu": cpu,
        "kvm": kvm,
        "iommu": iommu,
        "gpus": gpu_list,
        "memory": mem,
        "tools": tool,
        "vm_ready": not reasons,
        "vm_reasons": reasons,
        "passthrough_ready": iommu["enabled"] and bool(gpu_list),
        "passthrough_reasons": passthrough_reasons,
    }


__all__ = [
    "cpu_info", "kvm_present", "iommu_status", "gpus", "memory", "tools", "probe",
]
