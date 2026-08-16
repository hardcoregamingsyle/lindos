"""Capability-probe tests against a faked LINDOS_ROOT tree (SPEC-VM §26)."""
from __future__ import annotations

import json
from typing import Dict

from lindos_vm import caps


def _plant_ready_host(fake_root: Dict[str, object]) -> None:
    write = fake_root["write"]        # type: ignore[assignment]
    touch = fake_root["touch"]        # type: ignore[assignment]
    pci = fake_root["pci_device"]     # type: ignore[assignment]
    group = fake_root["iommu_group"]  # type: ignore[assignment]

    write("/proc/cpuinfo",
          "vendor_id\t: GenuineIntel\n"
          "flags\t\t: fpu vme de pse vmx lm constant_tsc\n")
    write("/proc/cmdline",
          "BOOT_IMAGE=/vmlinuz root=/dev/sda2 ro quiet intel_iommu=on iommu=pt\n")
    write("/proc/meminfo", "MemTotal:       32000000 kB\nMemAvailable:   20000000 kB\n")
    addr = fake_root["addr"]          # type: ignore[assignment]
    touch("/dev/kvm")
    touch("/usr/bin/qemu-system-x86_64")
    touch("/usr/bin/virsh")
    touch("/usr/share/OVMF/OVMF_CODE_4M.fd")
    # a discrete GPU (NVIDIA) with an HD-audio companion in IOMMU group 12
    pci(addr(0x01, 0, 0), 0x10de, 0x2482, 0x030000)
    pci(addr(0x01, 0, 1), 0x10de, 0x228b, 0x040300)
    group(12, [addr(0x01, 0, 0), addr(0x01, 0, 1)])
    # integrated Intel GPU in its own group
    pci(addr(0x00, 2, 0), 0x8086, 0x9bc4, 0x030000)
    group(0, [addr(0x00, 2, 0)])


def test_cpu_info_detects_vmx(fake_root):
    fake_root["write"]("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: fpu vmx lm\n")
    info = caps.cpu_info()
    assert info["vendor"] == "GenuineIntel"
    assert info["has_virt"] is True
    assert info["virt_ext"] == "vmx"


def test_cpu_info_detects_svm(fake_root):
    fake_root["write"]("/proc/cpuinfo", "vendor_id\t: AuthenticAMD\nflags\t\t: fpu svm lm\n")
    info = caps.cpu_info()
    assert info["virt_ext"] == "svm"
    assert info["vmx"] is False and info["svm"] is True


def test_cpu_info_no_virt(fake_root):
    fake_root["write"]("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: fpu lm\n")
    assert caps.cpu_info()["has_virt"] is False


def test_kvm_present(fake_root):
    assert caps.kvm_present() is False
    fake_root["touch"]("/dev/kvm")
    assert caps.kvm_present() is True


def test_iommu_status(fake_root):
    fake_root["write"]("/proc/cmdline", "ro quiet intel_iommu=on iommu=pt\n")
    fake_root["touch"]("/sys/kernel/iommu_groups/0/devices/" + fake_root["addr"](0, 0, 0))
    st = caps.iommu_status()
    assert st["intel_iommu"] is True
    assert st["iommu_pt"] is True
    assert st["groups"] >= 1
    assert st["enabled"] is True


def test_iommu_not_enabled_without_groups(fake_root):
    fake_root["write"]("/proc/cmdline", "ro quiet intel_iommu=on\n")
    assert caps.iommu_status()["enabled"] is False


def test_gpus_and_groups(fake_root):
    addr = fake_root["addr"]
    _plant_ready_host(fake_root)
    gpus = caps.gpus()
    pcis = {g["pci"]: g for g in gpus}
    assert addr(0x01, 0, 0) in pcis
    assert pcis[addr(0x01, 0, 0)]["vendor_name"] == "NVIDIA"
    assert pcis[addr(0x01, 0, 0)]["iommu_group"] == 12
    assert pcis[addr(0x00, 2, 0)]["vendor_name"] == "Intel"
    # the HD-audio function (class 0403) must NOT be reported as a GPU
    assert addr(0x01, 0, 1) not in pcis


def test_memory(fake_root):
    fake_root["write"]("/proc/meminfo", "MemTotal:       16000000 kB\nMemAvailable: 8000000 kB\n")
    mem = caps.memory()
    assert mem["total_mib"] == 16000000 // 1024
    assert mem["available_mib"] == 8000000 // 1024


def test_probe_ready(fake_root):
    _plant_ready_host(fake_root)
    rep = caps.probe()
    assert rep["vm_ready"] is True
    assert rep["vm_reasons"] == []
    assert rep["passthrough_ready"] is True


def test_probe_not_ready_reports_reasons(fake_root):
    fake_root["write"]("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: fpu lm\n")
    rep = caps.probe()
    assert rep["vm_ready"] is False
    assert any("virtualization" in r for r in rep["vm_reasons"])


def test_cli_check_json(run_cli, fake_root):
    _plant_ready_host(fake_root)
    proc = run_cli("check", "--json",
                   env={"LINDOS_ROOT": str(fake_root["root"]),
                        "LINDOS_HOME": str(fake_root["home"])})
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["vm_ready"] is True
    assert data["cpu"]["virt_ext"] == "vmx"
