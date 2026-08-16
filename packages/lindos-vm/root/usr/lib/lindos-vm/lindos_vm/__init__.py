"""lindos_vm — the honest KVM/QEMU Windows-VM package's pure-Python library (SPEC-VM §21).

Sub-modules:

* :mod:`lindos_vm.caps`        — probe host virtualization capability (CPU vmx/svm, /dev/kvm,
  IOMMU, GPUs + their IOMMU groups, RAM, installed tools).  All reads are ``LINDOS_ROOT``-aware.
* :mod:`lindos_vm.plan`        — build the GPU-passthrough *setup plan* (IOMMU cmdline, vfio-pci
  ``ids=`` line, initramfs, single-GPU hook).  It only prints copy-pasteable ``pkexec`` steps;
  it never applies anything itself.
* :mod:`lindos_vm.domain`      — render the libvirt domain XML from ``win.xml.template``.  The
  output is a **standard** Windows VM: no hypervisor hiding, no SMBIOS/UUID/ACPI/CPUID spoofing.
* :mod:`lindos_vm.passthrough` — vfio-pci bind/unbind helpers.  Reads are direct; mutations
  print the exact privileged ``pkexec`` command (this package never calls ``sudo``).

Every module is importable on any OS: Linux-only reads (``/proc``, ``/sys``, ``/dev``) are
guarded and honour the ``LINDOS_ROOT`` / ``LINDOS_HOME`` env overrides exactly like
``lindos.paths`` in lindos-core, so the whole package is unit-testable on Windows against a
faked root tree.  Nothing here touches the filesystem at import time.

HONESTY (SPEC-VM §20, binding): this VM is a truthful, standard Windows virtual machine.  It
ships and generates NO anti-cheat / VM-detection evasion of any kind.
"""
from __future__ import annotations

import os

__version__ = "1.0.0"

ROOT_ENV = "LINDOS_ROOT"
HOME_ENV = "LINDOS_HOME"

# System locations owned by / used by this package (canonical, unresolved).
SHARE_DIR = "/usr/share/lindos/vm"
XML_TEMPLATE = "/usr/share/lindos/vm/win.xml.template"
VFIO_TEMPLATE = "/usr/share/lindos/vm/vfio.conf.template"
QEMU_HOOK = "/etc/libvirt/hooks/qemu"
MODPROBE_VFIO = "/etc/modprobe.d/vfio.conf"
LOG_DIR = "/var/log/lindos"

# Per-user VM storage (under LINDOS_HOME).
VM_HOME_DIR = "~/.local/share/lindos/vm"

# Firmware defaults (Ubuntu 24.04 / Debian trixie 4M split OVMF).  Overridable in the template.
OVMF_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS = "/usr/share/OVMF/OVMF_VARS_4M.fd"


def root() -> str:
    """Return the ``LINDOS_ROOT`` prefix ("" when unset)."""
    return os.environ.get(ROOT_ENV, "") or ""


def home() -> str:
    """Return the effective home directory (``LINDOS_HOME`` or the real one)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return override
    return os.path.expanduser("~")


def resolve(path: str) -> str:
    """Apply ``LINDOS_HOME`` (``~`` paths) or ``LINDOS_ROOT`` (absolute paths).

    Mirrors ``lindos.paths.resolve`` so tests can redirect ``/proc``/``/sys``/``/dev``/``/etc``
    and the shipped data files under a scratch directory.
    """
    if path.startswith("~"):
        rest = path[1:].lstrip("/\\")
        return os.path.normpath(os.path.join(home(), rest)) if rest else home()
    prefix = root()
    if prefix and (path.startswith("/") or os.path.isabs(path)):
        _drive, tail = os.path.splitdrive(path)
        return os.path.normpath(os.path.join(prefix, tail.lstrip("/\\")))
    return path


def xml_template_path() -> str:
    """Resolved path of the shipped libvirt domain XML template."""
    return resolve(XML_TEMPLATE)


def vfio_template_path() -> str:
    """Resolved path of the shipped modprobe vfio.conf template."""
    return resolve(VFIO_TEMPLATE)


def vm_home_dir() -> str:
    """Resolved per-user VM directory (``~/.local/share/lindos/vm``)."""
    return resolve(VM_HOME_DIR)


def is_linux() -> bool:
    """True on a real Linux host (privileged / probe actions only make sense there)."""
    return os.name == "posix" and _uname_sysname() == "Linux"


def _uname_sysname() -> str:
    fn = getattr(os, "uname", None)
    if fn is None:
        return ""
    try:
        return fn().sysname
    except OSError:  # pragma: no cover
        return ""


__all__ = [
    "__version__", "ROOT_ENV", "HOME_ENV", "SHARE_DIR", "XML_TEMPLATE", "VFIO_TEMPLATE",
    "QEMU_HOOK", "MODPROBE_VFIO", "LOG_DIR", "VM_HOME_DIR", "OVMF_CODE", "OVMF_VARS",
    "root", "home", "resolve", "xml_template_path", "vfio_template_path", "vm_home_dir",
    "is_linux",
]
