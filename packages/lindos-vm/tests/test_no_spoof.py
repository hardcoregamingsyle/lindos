"""No-spoof / honesty assertions (SPEC-VM §20, §26, BINDING).

Greps the generated domain XML and every shipped template / config and fails if ANY
VM-detection / anti-cheat evasion token is present.  lindos-vm is a truthful, standard Windows
VM: it ships and generates no evasion of any kind.
"""
from __future__ import annotations

import re

import pytest

from lindos_vm import domain

# Case-insensitive evasion tokens that must never appear in generated XML, templates, or
# configs (SPEC-VM §20, §26).
#
# Both the QEMU command-line spelling AND the libvirt-domain-XML spelling of each evasion are
# listed, because lindos_vm.domain generates libvirt XML: `kvm=off` becomes
# `<kvm><hidden state='on'/></kvm>`, `hv-vendor-id=...` becomes a `<vendor_id .../>` element,
# and SMBIOS/serial forging becomes `<sysinfo>`.  Grepping only the QEMU-cmdline spellings
# would let the XML-form evasion slip through.
FORBIDDEN = [
    "kvm=off",       # QEMU-cmdline: hide KVM from the guest
    "<hidden",       # libvirt-XML: <kvm><hidden state='on'/></kvm> (hides KVM)
    "hv-vendor-id",  # QEMU-cmdline: Hyper-V vendor-id masquerading
    "vendor_id",     # libvirt-XML: <hyperv>/<cpu> <vendor_id .../> masquerading
    "smbios",        # SMBIOS / motherboard / serial forging (QEMU + libvirt <smbios>)
    "sysinfo",       # libvirt-XML: <sysinfo> injecting forged firmware/board identity
    "acpitable",     # forged ACPI tables (QEMU -acpitable / qemu:commandline)
    "spoof",         # any spoofing knob
    "hwid",          # hardware-id evasion
]


def _all_sources(shipped_xml_template, shipped_vfio_template, shipped_hook):
    """Every artefact that ships or is generated: both XML variants + all templates + hook.

    Every path is fixed (not derived from LINDOS_HOME) so the pytest tmp-dir name — which
    embeds the parametrized token, e.g. ``..._hwid_0`` — cannot leak into the rendered XML and
    cause a false positive.
    """
    fixed = dict(name="RDPWindows", windows_iso="/data/win.iso", disk_path="/data/vm.qcow2",
                 nvram_path="/data/vm_VARS.fd", uuid="11111111-2222-3333-4444-555555555555")
    default_xml = domain.render(**fixed)
    gpu_xml = domain.render(gpu="0000:01:00.0", **fixed)
    virtio_xml = domain.render(virtio_iso="/data/virtio.iso", **fixed)
    sources = {
        "generated-default.xml": default_xml,
        "generated-gpu.xml": gpu_xml,
        "generated-virtio.xml": virtio_xml,
        "win.xml.template": shipped_xml_template.read_text(encoding="utf-8"),
        "vfio.conf.template": shipped_vfio_template.read_text(encoding="utf-8"),
        "hooks/qemu": shipped_hook.read_text(encoding="utf-8"),
    }
    return sources


@pytest.mark.parametrize("token", FORBIDDEN)
def test_no_forbidden_token(token, fake_root, shipped_xml_template, shipped_vfio_template,
                            shipped_hook):
    sources = _all_sources(shipped_xml_template, shipped_vfio_template, shipped_hook)
    pat = re.compile(re.escape(token), re.IGNORECASE)
    for label, text in sources.items():
        assert not pat.search(text), (
            f"HONESTY VIOLATION: forbidden token {token!r} found in {label}")


def test_no_hypervisor_hiding_feature(fake_root):
    """The hypervisor CPUID bit must not be disabled (that would hide the VM)."""
    xml = domain.render(name="vm", windows_iso="/x.iso")
    lowered = xml.lower()
    assert "policy=\"disable\" name=\"hypervisor\"" not in lowered.replace("'", '"')
    assert "name=\"hypervisor\"" not in lowered.replace("'", '"')


def test_honest_features_present(fake_root):
    """Positive checks: the VM truthfully advertises itself and uses the real host CPU."""
    xml = domain.render(name="vm", windows_iso="/x.iso")
    assert 'mode="host-passthrough"' in xml       # exposes the real CPU, not a forged model
    assert "<hyperv" in xml                        # advertises Hyper-V enlightenments (honest)
    assert 'type="emulator" version="2.0"' in xml  # a genuine emulated TPM, not a forged EK


def test_domain_type_is_kvm_not_hidden(fake_root):
    xml = domain.render(name="vm", windows_iso="/x.iso")
    assert xml.lstrip().startswith("<!--") or "<domain type=\"kvm\">" in xml
    assert "<domain type=\"kvm\">" in xml
