"""pytest bootstrap for lindos-vm (runs on Windows/macOS/Linux, no root, no real hypervisor).

* makes ``lindos_vm`` importable from ``root/usr/lib/lindos-vm``;
* ``fake_root`` fixture: a scratch ``LINDOS_ROOT`` tree with the shipped templates copied in,
  plus helpers to plant a faked ``/proc`` + ``/sys`` + ``/dev`` so the capability probe and the
  passthrough helpers can be exercised off Linux;
* ``run_cli`` fixture: run ``root/usr/bin/lindos-vm`` through the current interpreter.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                        # packages/lindos-vm
ROOT = PKG_ROOT / "root"
LIB = ROOT / "usr" / "lib" / "lindos-vm"
BIN = ROOT / "usr" / "bin"
SHARE = ROOT / "usr" / "share" / "lindos" / "vm"
XML_TEMPLATE = SHARE / "win.xml.template"
VFIO_TEMPLATE = SHARE / "vfio.conf.template"
HOOK = ROOT / "etc" / "libvirt" / "hooks" / "qemu"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

# Real sysfs names PCI devices "DDDD:BB:SS.F", but Windows forbids ':' in filenames.  The
# probe treats these names as opaque strings (no colon-specific logic), so the faked tree may
# use a filesystem-safe separator on Windows; tests assert against the same rendered name.
PCI_SEP = ":" if os.name != "nt" else "-"


def make_pci_addr(bus: int, slot: int = 0, func: int = 0, dom: int = 0) -> str:
    return f"{dom:04x}{PCI_SEP}{bus:02x}{PCI_SEP}{slot:02x}.{func}"


@pytest.fixture(scope="session")
def pkg_root() -> Path:
    return PKG_ROOT


@pytest.fixture(scope="session")
def shipped_xml_template() -> Path:
    return XML_TEMPLATE


@pytest.fixture(scope="session")
def shipped_vfio_template() -> Path:
    return VFIO_TEMPLATE


@pytest.fixture(scope="session")
def shipped_hook() -> Path:
    return HOOK


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, object]:
    """Scratch LINDOS_ROOT + LINDOS_HOME with the shipped templates; helpers plant a probe tree."""
    root = tmp_path / "root"
    share = root / "usr" / "share" / "lindos" / "vm"
    share.mkdir(parents=True)
    shutil.copy2(XML_TEMPLATE, share / "win.xml.template")
    shutil.copy2(VFIO_TEMPLATE, share / "vfio.conf.template")
    hjson = tmp_path / "home"
    hjson.mkdir()
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(hjson))

    def write(rel: str, text: str) -> Path:
        target = root / rel.lstrip("/\\")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return target

    def touch(rel: str) -> Path:
        target = root / rel.lstrip("/\\")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return target

    def mkdir(rel: str) -> Path:
        target = root / rel.lstrip("/\\")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def pci_device(addr: str, vendor: int, device: int, klass: int) -> None:
        base = f"/sys/bus/pci/devices/{addr}"
        write(f"{base}/vendor", f"0x{vendor:04x}\n")
        write(f"{base}/device", f"0x{device:04x}\n")
        write(f"{base}/class", f"0x{klass:06x}\n")

    def iommu_group(group: int, addrs: List[str]) -> None:
        for addr in addrs:
            touch(f"/sys/kernel/iommu_groups/{group}/devices/{addr}")

    return {
        "root": root, "home": hjson, "write": write, "touch": touch, "mkdir": mkdir,
        "pci_device": pci_device, "iommu_group": iommu_group, "addr": make_pci_addr,
        "tmp": tmp_path,
    }


@pytest.fixture()
def run_cli() -> Callable[..., subprocess.CompletedProcess]:
    """``run_cli("check", "--json", env=...)`` → CompletedProcess for /usr/bin/lindos-vm."""

    def _run(*args: str, env: Optional[Dict[str, str]] = None,
             timeout: float = 120) -> subprocess.CompletedProcess:
        script = BIN / "lindos-vm"
        assert script.is_file(), script
        full_env = dict(os.environ)
        full_env["PYTHONIOENCODING"] = "utf-8"
        full_env["PYTHONPATH"] = str(LIB) + os.pathsep + full_env.get("PYTHONPATH", "")
        if env:
            full_env.update(env)
        cmd: List[str] = [sys.executable, str(script), *args]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=full_env, timeout=timeout, check=False)

    return _run
