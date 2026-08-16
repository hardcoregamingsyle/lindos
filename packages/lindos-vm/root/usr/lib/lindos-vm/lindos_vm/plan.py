"""GPU-passthrough setup plan (SPEC-VM §21.1, ``lindos-vm setup``).

Builds an ordered list of the privileged steps needed to enable IOMMU and bind a GPU to
``vfio-pci`` for passthrough, and the exact vfio ``modprobe`` config.  It **prints** every
step as a copy-pasteable ``pkexec …`` command and **never applies anything itself** — no
``sudo``, no writes to ``/etc``, no ``update-grub``.  Reading sysfs is fine and root-free.

There is no detection-evasion here: enabling IOMMU and binding vfio-pci is the standard,
documented passthrough procedure.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import (MODPROBE_VFIO, QEMU_HOOK, resolve, vfio_template_path)


@dataclass
class Step:
    """One step of the plan: a human title, copy-pasteable commands, and a note."""

    title: str
    commands: List[str] = field(default_factory=list)
    note: str = ""
    privileged: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "commands": self.commands,
                "note": self.note, "privileged": self.privileged}


@dataclass
class Plan:
    """The full setup plan."""

    mode: str
    gpu_pci: Optional[str]
    gpu_ids: List[str]
    vfio_conf: str
    steps: List[Step] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "gpu_pci": self.gpu_pci,
            "gpu_ids": self.gpu_ids,
            "vfio_conf": self.vfio_conf,
            "steps": [s.to_dict() for s in self.steps],
            "warnings": self.warnings,
        }


def iommu_flags(vendor: str) -> List[str]:
    """The kernel cmdline flags that enable IOMMU for this CPU vendor."""
    v = (vendor or "").lower()
    if "amd" in v:
        return ["amd_iommu=on", "iommu=pt"]
    # Intel is the default (GenuineIntel and unknown vendors get the Intel flag).
    return ["intel_iommu=on", "iommu=pt"]


def _read_hex(path: str) -> Optional[int]:
    try:
        with open(resolve(path), "r", encoding="utf-8") as handle:
            return int(handle.read().strip(), 16)
    except (OSError, ValueError):
        return None


def group_device_ids(group: int) -> List[str]:
    """All ``vendor:device`` ids in an IOMMU group (GPU + its HD-audio/USB companions).

    Read from ``/sys/kernel/iommu_groups/<n>/devices/`` so it is testable on Windows.
    """
    devdir = resolve(os.path.join("/sys/kernel/iommu_groups", str(group), "devices"))
    ids: List[str] = []
    try:
        addrs = sorted(os.listdir(devdir))
    except OSError:
        return ids
    for addr in addrs:
        vendor = _read_hex(os.path.join("/sys/bus/pci/devices", addr, "vendor"))
        device = _read_hex(os.path.join("/sys/bus/pci/devices", addr, "device"))
        if vendor is not None and device is not None:
            pci_id = f"{vendor:04x}:{device:04x}"
            if pci_id not in ids:
                ids.append(pci_id)
    return ids


def render_vfio_conf(ids: List[str], template_path: Optional[str] = None) -> str:
    """Fill the shipped vfio.conf template with the ``ids=`` list."""
    tpl = template_path or vfio_template_path()
    try:
        with open(tpl, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = ("options vfio-pci ids=@@IDS@@\n"
                "softdep drm pre: vfio-pci\n")
    return text.replace("@@IDS@@", ",".join(ids))


def _have_lindos_kernel() -> bool:
    return bool(shutil.which("lindos-kernel"))


def make_plan(caps_report: Dict[str, Any], mode: str = "single-gpu",
              gpu_pci: Optional[str] = None) -> Plan:
    """Assemble the passthrough plan from a :func:`lindos_vm.caps.probe` report."""
    if mode not in ("single-gpu", "dual-gpu"):
        raise ValueError(f"mode must be 'single-gpu' or 'dual-gpu', got {mode!r}")

    cpu = caps_report.get("cpu", {})
    gpu_list = caps_report.get("gpus", [])
    warnings: List[str] = []

    chosen = None
    if gpu_pci:
        chosen = next((g for g in gpu_list if g.get("pci") == gpu_pci), None)
        if chosen is None:
            warnings.append(f"PCI {gpu_pci} is not a display controller in sysfs")
    elif mode == "dual-gpu" and len(gpu_list) >= 2:
        chosen = gpu_list[-1]  # pass the second/discrete GPU, keep the first for the host
    elif gpu_list:
        chosen = gpu_list[0]

    group = chosen.get("iommu_group") if chosen else None
    ids: List[str] = []
    if group is not None:
        ids = group_device_ids(group)
    if not ids and chosen and chosen.get("pci_id"):
        ids = [chosen["pci_id"]]
    if chosen and group is None:
        warnings.append("chosen GPU has no discoverable IOMMU group; is IOMMU enabled?")

    flags = iommu_flags(cpu.get("vendor", ""))
    vfio_conf = render_vfio_conf(ids)

    steps: List[Step] = []

    # 1. IOMMU cmdline
    if _have_lindos_kernel():
        steps.append(Step(
            title="Enable IOMMU on the kernel cmdline",
            commands=[f"lindos-kernel cmdline set {' '.join(flags)}",
                      "pkexec update-grub"],
            note="Delegates to lindos-kernel's marker-fenced GRUB drop-in.",
        ))
    else:
        steps.append(Step(
            title="Enable IOMMU on the kernel cmdline",
            commands=[
                "# add these to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub:",
                f"#   {' '.join(flags)}",
                "pkexec update-grub",
            ],
            note="lindos-kernel not found; edit /etc/default/grub by hand, then update-grub.",
        ))

    # 2. vfio.conf
    steps.append(Step(
        title=f"Bind the passthrough GPU to vfio-pci ({MODPROBE_VFIO})",
        commands=[
            f"printf '%s' {_shq(vfio_conf)} | pkexec tee {MODPROBE_VFIO} >/dev/null",
        ],
        note=(f"ids={','.join(ids) or '<none discovered>'} "
              "(GPU plus every function in its IOMMU group)."),
    ))

    # 3. initramfs
    steps.append(Step(
        title="Rebuild the initramfs so vfio-pci binds at boot",
        commands=["pkexec update-initramfs -u -k all"],
    ))

    # 4. single-GPU hook / dual-GPU note
    if mode == "single-gpu":
        steps.append(Step(
            title="Single-GPU passthrough hook",
            commands=[f"# shipped by lindos-vm at {QEMU_HOOK} (conffile)",
                      f"pkexec chmod 0755 {QEMU_HOOK}",
                      "pkexec systemctl enable --now libvirtd"],
            note=("On VM start the hook stops the display manager and hands the GPU to the "
                  "VM; on stop it returns the GPU to the host.  You will lose the host "
                  "display while the VM runs."),
        ))
    else:
        steps.append(Step(
            title="Dual-GPU note",
            commands=["pkexec systemctl enable --now libvirtd"],
            note=(f"The single-GPU hook at {QEMU_HOOK} no-ops for this VM; the discrete GPU "
                  "is bound to vfio-pci at boot and the host keeps its own GPU."),
            privileged=True,
        ))

    steps.append(Step(
        title="Reboot to apply IOMMU + vfio-pci",
        commands=["pkexec reboot"],
        note="After reboot, verify with: lindos-vm check",
    ))

    if not caps_report.get("iommu", {}).get("cmdline_flag"):
        warnings.append("IOMMU is not yet on the running kernel cmdline (step 1 fixes this).")
    if not gpu_list:
        warnings.append("No GPU detected in sysfs; cannot compute vfio ids.")

    return Plan(mode=mode, gpu_pci=(chosen or {}).get("pci"), gpu_ids=ids,
                vfio_conf=vfio_conf, steps=steps, warnings=warnings)


def _shq(text: str) -> str:
    """Single-quote a string for safe shell embedding."""
    return "'" + text.replace("'", "'\\''") + "'"


__all__ = [
    "Step", "Plan", "iommu_flags", "group_device_ids", "render_vfio_conf", "make_plan",
]
