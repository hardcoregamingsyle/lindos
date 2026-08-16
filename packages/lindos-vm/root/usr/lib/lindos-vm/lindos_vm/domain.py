"""libvirt domain XML generation (SPEC-VM §21, ``lindos-vm create``).

Renders :data:`lindos_vm.XML_TEMPLATE` into a complete, **standard** Windows VM description.
There is deliberately no evasion knob anywhere: no ``kvm=off``, no ``hv-vendor-id``, no hiding
the hypervisor CPUID bit, and no SMBIOS / motherboard-UUID / serial / ACPI-table / CPUID
spoofing.  ``<uuid>`` is a freshly generated per-VM identifier (libvirt requires one); it is
not copied from any real machine.  The Hyper-V enlightenments are performance features and
honestly advertise the VM.
"""
from __future__ import annotations

import os
import re
import uuid as _uuid
from typing import Optional
from xml.etree import ElementTree as ET

from . import (OVMF_CODE, OVMF_VARS, resolve, vm_home_dir, xml_template_path)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PCI_RE = re.compile(r"^(?:([0-9a-fA-F]{4}):)?([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")

MIN_RAM_MIB = 2048
MIN_DISK_GB = 20
EMULATOR = "/usr/bin/qemu-system-x86_64"


class DomainError(ValueError):
    """Raised on invalid create parameters or an unreadable template."""


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise DomainError(
            f"invalid VM name {name!r}: use letters, digits, '.', '_' or '-'")
    return name


def parse_pci(addr: str) -> str:
    """Normalise a PCI address to ``DDDD:BB:SS.F`` (prepending domain ``0000`` if absent)."""
    m = PCI_RE.match(addr.strip())
    if not m:
        raise DomainError(f"invalid PCI address {addr!r} (want [DDDD:]BB:DD.F)")
    dom, bus, slot, func = m.groups()
    dom = dom or "0000"
    return f"{dom.lower()}:{bus.lower()}:{slot.lower()}.{func}"


def _pci_hostdev(addr: str) -> str:
    dom, bus, slotfunc = addr.split(":")
    slot, func = slotfunc.split(".")
    return (
        '    <hostdev mode="subsystem" type="pci" managed="yes">\n'
        "      <source>\n"
        f'        <address domain="0x{dom}" bus="0x{bus}" slot="0x{slot}" function="0x{func}"/>\n'
        "      </source>\n"
        "    </hostdev>"
    )


_SPICE_GRAPHICS = (
    '    <graphics type="spice" autoport="yes">\n'
    '      <listen type="address"/>\n'
    '      <image compression="off"/>\n'
    "    </graphics>\n"
    "    <video>\n"
    '      <model type="qxl" ram="65536" vram="65536" vgamem="16384" heads="1"/>\n'
    "    </video>\n"
    '    <channel type="spicevmc">\n'
    '      <target type="virtio" name="com.redhat.spice.0"/>\n'
    "    </channel>"
)


def _virtio_cdrom(virtio_iso: str) -> str:
    return (
        '    <disk type="file" device="cdrom">\n'
        '      <driver name="qemu" type="raw"/>\n'
        f'      <source file="{_xml_attr(virtio_iso)}"/>\n'
        '      <target dev="sdb" bus="sata"/>\n'
        "      <readonly/>\n"
        "    </disk>"
    )


def _xml_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def default_disk_path(name: str) -> str:
    return os.path.join(vm_home_dir(), f"{name}.qcow2")


def default_xml_path(name: str) -> str:
    return os.path.join(vm_home_dir(), f"{name}.xml")


def default_nvram_path(name: str) -> str:
    return os.path.join(vm_home_dir(), f"{name}_VARS.fd")


def render(
    name: str,
    windows_iso: str,
    disk_path: Optional[str] = None,
    ram_mib: int = 8192,
    vcpus: int = 4,
    gpu: Optional[str] = None,
    virtio_iso: Optional[str] = None,
    uuid: Optional[str] = None,
    ovmf_code: Optional[str] = None,
    ovmf_vars: Optional[str] = None,
    nvram_path: Optional[str] = None,
    emulator: str = EMULATOR,
    template_path: Optional[str] = None,
) -> str:
    """Return the complete, validated libvirt domain XML for a standard Windows VM."""
    validate_name(name)
    if not windows_iso:
        raise DomainError("a Windows install ISO path is required (--windows-iso)")
    if int(ram_mib) < MIN_RAM_MIB:
        raise DomainError(f"RAM must be at least {MIN_RAM_MIB} MiB")
    if int(vcpus) < 1:
        raise DomainError("vcpus must be >= 1")

    tpl_path = template_path or xml_template_path()
    try:
        with open(tpl_path, "r", encoding="utf-8") as handle:
            template = handle.read()
    except OSError as exc:
        raise DomainError(f"cannot read domain template {tpl_path}: {exc}") from exc

    vm_uuid = uuid or str(_uuid.uuid4())
    disk = disk_path or default_disk_path(name)
    nvram = nvram_path or default_nvram_path(name)
    graphics = _pci_hostdev(parse_pci(gpu)) if gpu else _SPICE_GRAPHICS
    virtio_block = _virtio_cdrom(virtio_iso) if virtio_iso else ""

    replacements = {
        "@@NAME@@": _xml_attr(name),
        "@@UUID@@": vm_uuid,
        "@@RAM_KIB@@": str(int(ram_mib) * 1024),
        "@@VCPUS@@": str(int(vcpus)),
        "@@CORES@@": str(int(vcpus)),
        "@@THREADS@@": "1",
        "@@OVMF_CODE@@": _xml_attr(ovmf_code or OVMF_CODE),
        "@@OVMF_VARS@@": _xml_attr(ovmf_vars or OVMF_VARS),
        "@@NVRAM@@": _xml_attr(nvram),
        "@@EMULATOR@@": _xml_attr(emulator),
        "@@DISK_PATH@@": _xml_attr(disk),
        "@@WINDOWS_ISO@@": _xml_attr(windows_iso),
        "@@VIRTIO_CDROM@@": virtio_block,
        "@@GRAPHICS@@": graphics,
    }
    xml = template
    for token, value in replacements.items():
        xml = xml.replace(token, value)

    if "@@" in xml:
        leftover = sorted(set(re.findall(r"@@[A-Z_]+@@", xml)))
        raise DomainError(f"template has unfilled tokens: {', '.join(leftover)}")

    try:
        ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DomainError(f"generated XML is not well-formed: {exc}") from exc
    return xml


def write_domain(name: str, xml: str, path: Optional[str] = None) -> str:
    """Write the rendered XML to ``~/.local/share/lindos/vm/<name>.xml`` (LF endings)."""
    target = path or default_xml_path(name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(xml)
    return target


def define_command(xml_path: str) -> list:
    """The exact ``virsh define`` command to register the domain (system connection)."""
    return ["virsh", "--connect", "qemu:///system", "define", xml_path]


__all__ = [
    "DomainError", "NAME_RE", "PCI_RE", "MIN_RAM_MIB", "MIN_DISK_GB", "EMULATOR",
    "validate_name", "parse_pci", "render", "write_domain", "define_command",
    "default_disk_path", "default_xml_path", "default_nvram_path",
]
