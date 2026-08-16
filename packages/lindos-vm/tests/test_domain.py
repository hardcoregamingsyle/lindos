"""Domain-XML generation tests (SPEC-VM §26)."""
from __future__ import annotations

import json
import os
from xml.etree import ElementTree as ET

import pytest

from lindos_vm import domain


def _render(fake_root, **kw):
    kw.setdefault("name", "RDPWindows")
    kw.setdefault("windows_iso", "/data/win11.iso")
    kw.setdefault("uuid", "11111111-2222-3333-4444-555555555555")
    return domain.render(**kw)


def test_render_is_well_formed(fake_root):
    xml = _render(fake_root)
    root = ET.fromstring(xml)
    assert root.tag == "domain"
    assert root.attrib["type"] == "kvm"
    assert root.find("name").text == "RDPWindows"
    assert root.find("uuid").text == "11111111-2222-3333-4444-555555555555"


def test_render_ram_and_cpus(fake_root):
    xml = _render(fake_root, ram_mib=8192, vcpus=6)
    root = ET.fromstring(xml)
    assert root.find("memory").text == str(8192 * 1024)
    assert root.find("vcpu").text == "6"
    topo = root.find("cpu/topology")
    assert topo.attrib["cores"] == "6" and topo.attrib["threads"] == "1"


def test_default_graphics_is_spice(fake_root):
    xml = _render(fake_root)
    root = ET.fromstring(xml)
    assert root.find("devices/graphics").attrib["type"] == "spice"
    assert root.find("devices/video/model").attrib["type"] == "qxl"
    assert root.find("devices/hostdev") is None


def test_gpu_passthrough_emits_hostdev(fake_root):
    xml = _render(fake_root, gpu="0000:01:00.0")
    root = ET.fromstring(xml)
    hostdev = root.find("devices/hostdev")
    assert hostdev is not None
    assert hostdev.attrib["type"] == "pci"
    assert hostdev.attrib["managed"] == "yes"
    addr = hostdev.find("source/address")
    assert addr.attrib["bus"] == "0x01"
    assert addr.attrib["slot"] == "0x00"
    assert addr.attrib["function"] == "0x0"
    # passthrough replaces the emulated SPICE display
    assert root.find("devices/graphics") is None


def test_virtio_cdrom_optional(fake_root):
    without = ET.fromstring(_render(fake_root))
    cdroms = without.findall("devices/disk[@device='cdrom']")
    assert len(cdroms) == 1
    with_virtio = ET.fromstring(_render(fake_root, virtio_iso="/data/virtio-win.iso"))
    cdroms = with_virtio.findall("devices/disk[@device='cdrom']")
    assert len(cdroms) == 2


def test_uuid_is_generated_when_absent(fake_root):
    xml = domain.render(name="vm1", windows_iso="/x.iso")
    root = ET.fromstring(xml)
    text = root.find("uuid").text
    assert text and len(text) == 36 and text.count("-") == 4


def test_parse_pci_normalises_domain():
    assert domain.parse_pci("01:00.0") == "0000:01:00.0"
    assert domain.parse_pci("0000:0a:00.1") == "0000:0a:00.1"


def test_parse_pci_rejects_garbage():
    with pytest.raises(domain.DomainError):
        domain.parse_pci("not-a-pci")


def test_render_rejects_bad_name(fake_root):
    with pytest.raises(domain.DomainError):
        domain.render(name="bad name!", windows_iso="/x.iso")


def test_render_rejects_low_ram(fake_root):
    with pytest.raises(domain.DomainError):
        domain.render(name="vm", windows_iso="/x.iso", ram_mib=512)


def test_iso_path_is_escaped(fake_root):
    xml = _render(fake_root, windows_iso="/data/win & more.iso")
    root = ET.fromstring(xml)  # would raise if '&' were left unescaped
    sources = [d.find("source").attrib["file"] for d in root.findall("devices/disk")]
    assert "/data/win & more.iso" in sources


def test_write_domain(fake_root):
    xml = _render(fake_root)
    path = domain.write_domain("RDPWindows", xml)
    assert os.path.isfile(path)
    assert path.endswith("RDPWindows.xml")
    with open(path, "rb") as handle:
        assert b"\r" not in handle.read()  # LF only


def test_define_command():
    cmd = domain.define_command("/home/u/.local/share/lindos/vm/vm.xml")
    assert cmd[0] == "virsh" and "define" in cmd


def test_cli_create_json(run_cli, fake_root, tmp_path):
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"stub")
    # plant a ready host so create() passes the capability gate
    fake_root["write"]("/proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: vmx\n")
    fake_root["write"]("/proc/cmdline", "intel_iommu=on iommu=pt\n")
    fake_root["write"]("/proc/meminfo", "MemTotal: 32000000 kB\nMemAvailable: 20000000 kB\n")
    fake_root["touch"]("/dev/kvm")
    fake_root["touch"]("/usr/bin/qemu-system-x86_64")
    fake_root["touch"]("/usr/bin/virsh")
    fake_root["touch"]("/usr/share/OVMF/OVMF_CODE_4M.fd")
    proc = run_cli("create", "--name", "TestVM", "--windows-iso", str(iso), "--json",
                   env={"LINDOS_ROOT": str(fake_root["root"]),
                        "LINDOS_HOME": str(fake_root["home"])})
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["name"] == "TestVM"
    assert os.path.isfile(data["xml"])
