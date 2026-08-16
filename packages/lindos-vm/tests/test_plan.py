"""Setup-plan tests (SPEC-VM §26): the plan prints steps and never applies anything."""
from __future__ import annotations

import json
from typing import Dict

from lindos_vm import caps, plan


def _ready(fake_root: Dict[str, object]) -> Dict:
    write = fake_root["write"]
    touch = fake_root["touch"]
    pci = fake_root["pci_device"]
    group = fake_root["iommu_group"]
    addr = fake_root["addr"]
    write("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: vmx lm\n")
    write("/proc/cmdline", "ro quiet intel_iommu=on iommu=pt\n")
    write("/proc/meminfo", "MemTotal: 32000000 kB\nMemAvailable: 20000000 kB\n")
    touch("/dev/kvm")
    pci(addr(0x01, 0, 0), 0x10de, 0x2482, 0x030000)
    pci(addr(0x01, 0, 1), 0x10de, 0x228b, 0x040300)
    group(12, [addr(0x01, 0, 0), addr(0x01, 0, 1)])
    return caps.probe()


def test_iommu_flags_intel():
    assert plan.iommu_flags("GenuineIntel") == ["intel_iommu=on", "iommu=pt"]


def test_iommu_flags_amd():
    assert plan.iommu_flags("AuthenticAMD") == ["amd_iommu=on", "iommu=pt"]


def test_group_device_ids(fake_root):
    _ready(fake_root)
    ids = plan.group_device_ids(12)
    assert "10de:2482" in ids   # GPU
    assert "10de:228b" in ids   # its HD-audio companion


def test_render_vfio_conf(fake_root):
    text = plan.render_vfio_conf(["10de:2482", "10de:228b"])
    assert "ids=10de:2482,10de:228b" in text
    assert "@@IDS@@" not in text


def test_make_plan_single_gpu(fake_root):
    rep = _ready(fake_root)
    p = plan.make_plan(rep, mode="single-gpu")
    assert p.mode == "single-gpu"
    assert p.gpu_pci == fake_root["addr"](0x01, 0, 0)
    assert "10de:2482" in p.gpu_ids and "10de:228b" in p.gpu_ids
    titles = [s.title for s in p.steps]
    assert any("IOMMU" in t for t in titles)
    assert any("vfio-pci" in t for t in titles)
    assert any("initramfs" in t for t in titles)
    assert any("hook" in t.lower() for t in titles)


def test_plan_only_prints_no_sudo(fake_root):
    rep = _ready(fake_root)
    p = plan.make_plan(rep, mode="single-gpu")
    for step in p.steps:
        for cmd in step.commands:
            # never a bare sudo; privileged steps use pkexec (or are comments)
            assert not cmd.strip().startswith("sudo ")


def test_make_plan_dual_gpu_note(fake_root):
    write = fake_root["write"]
    touch = fake_root["touch"]
    pci = fake_root["pci_device"]
    group = fake_root["iommu_group"]
    addr = fake_root["addr"]
    write("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: vmx lm\n")
    write("/proc/cmdline", "ro intel_iommu=on iommu=pt\n")
    write("/proc/meminfo", "MemTotal: 32000000 kB\nMemAvailable: 20000000 kB\n")
    touch("/dev/kvm")
    pci(addr(0x00, 2, 0), 0x8086, 0x9bc4, 0x030000)  # iGPU (host keeps this)
    group(0, [addr(0x00, 2, 0)])
    pci(addr(0x01, 0, 0), 0x10de, 0x2482, 0x030000)  # dGPU (passed through)
    group(12, [addr(0x01, 0, 0)])
    rep = caps.probe()
    p = plan.make_plan(rep, mode="dual-gpu")
    assert p.gpu_pci == addr(0x01, 0, 0)
    assert any("Dual-GPU" in s.title for s in p.steps)


def test_make_plan_bad_mode(fake_root):
    rep = _ready(fake_root)
    try:
        plan.make_plan(rep, mode="triple-gpu")
    except ValueError:
        return
    raise AssertionError("expected ValueError for bad mode")


def test_cli_setup_json(run_cli, fake_root):
    _ready(fake_root)
    proc = run_cli("setup", "--single-gpu", "--json",
                   env={"LINDOS_ROOT": str(fake_root["root"]),
                        "LINDOS_HOME": str(fake_root["home"])})
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "single-gpu"
    assert any("pkexec" in c for s in data["steps"] for c in s["commands"])
