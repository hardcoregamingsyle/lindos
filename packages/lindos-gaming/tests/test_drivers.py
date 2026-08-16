"""lindos-drivers — pure parsing/planning logic (lspci, ubuntu-drivers, package choice, plan)."""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import load_bin  # noqa: E402

drivers = load_bin("lindos-drivers")

LSPCI = """\
00:00.0 Host bridge [0600]: Intel Corporation 12th Gen Core Processor Host Bridge/DRAM Registers [8086:4648] (rev 02)
00:02.0 VGA compatible controller [0300]: Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics] [8086:46a6] (rev 0c)
01:00.0 3D controller [0302]: NVIDIA Corporation GA107M [GeForce RTX 3050 Mobile] [10de:25a2] (rev a1)
03:00.0 Network controller [0280]: Intel Corporation Wi-Fi 6 AX201 [8086:a0f0] (rev 20)
"""

LSPCI_AMD = """\
0b:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Navi 22 [Radeon RX 6700/6700 XT/6750 XT / 6800M/6850M XT] [1002:73df] (rev c1)
"""

UBUNTU_DRIVERS = """\
== /sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0 ==
modalias : pci:v000010DEd000025A2sv00001028sd00000B10bc03sc02i00
vendor   : NVIDIA Corporation
model    : GA107M [GeForce RTX 3050 Mobile]
driver   : nvidia-driver-535-server - distro non-free
driver   : nvidia-driver-550-open - distro non-free recommended
driver   : nvidia-driver-550 - distro non-free
driver   : nvidia-driver-535 - distro non-free
driver   : nvidia-driver-545-open - distro non-free
driver   : xserver-xorg-video-nouveau - distro free builtin
"""


def test_parse_lspci_keeps_display_devices_only():
    gpus = drivers.parse_lspci(LSPCI)
    assert [g["vendor"] for g in gpus] == ["intel", "nvidia"]
    intel, nv = gpus
    assert intel["slot"] == "00:02.0" and intel["class"] == "VGA" and intel["vendor_id"] == "8086"
    assert intel["device_id"] == "46a6" and intel["revision"] == "0c"
    assert "Iris Xe" in intel["model"]
    assert nv["class"] == "3D" and nv["vendor_id"] == "10de" and nv["device_id"] == "25a2"
    assert "RTX 3050" in nv["model"]
    assert drivers.vendors_present(gpus) == ["intel", "nvidia"]


def test_parse_lspci_amd_and_garbage():
    gpus = drivers.parse_lspci(LSPCI_AMD)
    assert len(gpus) == 1 and gpus[0]["vendor"] == "amd" and gpus[0]["device_id"] == "73df"
    assert drivers.parse_lspci("") == []
    assert drivers.parse_lspci("garbage line\nanother\n") == []


def test_parse_ubuntu_drivers():
    rows = drivers.parse_ubuntu_drivers(UBUNTU_DRIVERS)
    pkgs = [r["package"] for r in rows]
    assert "nvidia-driver-550-open" in pkgs and "xserver-xorg-video-nouveau" in pkgs
    rec = [r["package"] for r in rows if r["recommended"]]
    assert rec == ["nvidia-driver-550-open"]
    assert drivers.parse_ubuntu_drivers("") == []


def test_choose_nvidia_package_flavours(monkeypatch):
    monkeypatch.delenv("LINDOS_NVIDIA_DRIVER", raising=False)
    rows = drivers.parse_ubuntu_drivers(UBUNTU_DRIVERS)
    apt = ["nvidia-driver-535", "nvidia-driver-550", "nvidia-driver-550-open", "nvidia-driver-545-open"]
    assert drivers.choose_nvidia_package("auto", rows, apt) == "nvidia-driver-550-open"
    assert drivers.choose_nvidia_package("open", rows, apt) == "nvidia-driver-550-open"
    assert drivers.choose_nvidia_package("proprietary", rows, apt) == "nvidia-driver-550"
    # no ubuntu-drivers output: highest apt branch
    assert drivers.choose_nvidia_package("auto", [], apt) == "nvidia-driver-550-open"
    assert drivers.choose_nvidia_package("proprietary", [], ["nvidia-driver-535", "nvidia-driver-470"]) == "nvidia-driver-535"
    # requested flavour not offered for the recommended branch -> best branch that has it
    assert drivers.choose_nvidia_package("open", [], ["nvidia-driver-535", "nvidia-driver-545-open"]) == "nvidia-driver-545-open"
    assert drivers.choose_nvidia_package("auto", [], []) is None
    monkeypatch.setenv("LINDOS_NVIDIA_DRIVER", "nvidia-driver-570")
    assert drivers.choose_nvidia_package("auto", rows, apt) == "nvidia-driver-570"


def test_build_plan_nvidia_hybrid(monkeypatch):
    monkeypatch.delenv("LINDOS_NVIDIA_DRIVER", raising=False)
    gpus = drivers.parse_lspci(LSPCI)
    monkeypatch.setattr(drivers, "i386_enabled", lambda: False)
    monkeypatch.setattr(drivers, "have", lambda b: b == "ubuntu-drivers")
    monkeypatch.setattr(drivers, "ubuntu_drivers_devices", lambda: drivers.parse_ubuntu_drivers(UBUNTU_DRIVERS))
    monkeypatch.setattr(drivers, "nvidia_available_from_apt", lambda: ["nvidia-driver-550", "nvidia-driver-550-open"])
    plan = drivers.build_plan(["nvidia"], gpus, "auto")
    assert plan["reboot"] is True
    assert plan["packages"][:2] == ["nvidia-driver-550-open", "nvidia-settings"]
    assert "libnvidia-gl-550:i386" in plan["packages"]
    assert "nvidia-prime" in plan["packages"], "hybrid Intel+NVIDIA laptop gets nvidia-prime"
    cmds = plan["commands"]
    assert cmds[0] == ["dpkg", "--add-architecture", "i386"]
    assert cmds[1] == ["apt-get", "update"]
    assert cmds[-2] == ["write", str(drivers.MODPROBE_CONF)]
    assert cmds[-1] == ["update-initramfs", "-u"]
    assert "options nvidia-drm modeset=1 fbdev=1" in drivers.MODPROBE_TEXT
    assert str(drivers.MODPROBE_CONF).endswith("nvidia-lindos.conf")
    assert any("open kernel modules" in n for n in plan["notes"])


def test_build_plan_amd_intel_no_reboot(monkeypatch):
    monkeypatch.setattr(drivers, "i386_enabled", lambda: True)
    plan = drivers.build_plan(["amd", "intel"], drivers.parse_lspci(LSPCI_AMD), "auto")
    assert plan["reboot"] is False
    assert "mesa-vulkan-drivers" in plan["packages"] and "mesa-vulkan-drivers:i386" in plan["packages"]
    assert "linux-firmware" in plan["packages"]
    assert plan["packages"].count("linux-firmware") == 1, "de-duplicated"
    assert plan["commands"][0] == ["apt-get", "update"]
    assert not any(c[0] == "write" for c in plan["commands"])


def test_resolve_targets_and_helper_flags():
    ns = drivers.build_parser().parse_args(["install", "--nvidia-open", "--amd"])
    targets, flavour = drivers.resolve_targets(ns, [])
    assert targets == ["nvidia", "amd"] and flavour == "open"
    assert drivers.helper_flags(ns) == ["--nvidia-open", "--amd"]
    ns2 = drivers.build_parser().parse_args(["install"])
    targets, flavour = drivers.resolve_targets(ns2, drivers.parse_lspci(LSPCI))
    assert targets == ["intel", "nvidia"] and flavour == "auto"
    assert drivers.helper_flags(ns2) == []


def test_helper_flags_are_whitelisted_by_core():
    try:
        from lindos import helper  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover
        pytest.skip("lindos-core not importable")
    ns = drivers.build_parser().parse_args(["install", "--nvidia-open", "--nvidia-proprietary", "--amd", "--intel"])
    flags = drivers.helper_flags(ns)
    assert set(flags) == set(helper.DRIVER_ARGS)
    assert helper.validate_payload("install-drivers", {"args": flags})["args"] == flags


def test_install_mutually_exclusive_flags(capsys):
    rc = drivers.main(["install", "--nvidia-open", "--nvidia-proprietary"])
    assert rc == drivers.EXIT_USAGE


def test_install_dry_run_json(monkeypatch, capsys):
    monkeypatch.setattr(drivers, "detect_gpus", lambda: drivers.parse_lspci(LSPCI_AMD))
    monkeypatch.setattr(drivers, "i386_enabled", lambda: True)
    rc = drivers.main(["install", "--dry-run", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry-run" and out["plan"]["targets"] == ["amd"]


def test_install_offline_exit_3_when_root(monkeypatch, capsys):
    monkeypatch.setattr(drivers, "detect_gpus", lambda: drivers.parse_lspci(LSPCI_AMD))
    monkeypatch.setattr(drivers, "is_root", lambda: True)
    monkeypatch.setattr(drivers, "online", lambda: False)
    rc = drivers.main(["install", "--json"])
    assert rc == drivers.EXIT_OFFLINE
    assert json.loads(capsys.readouterr().out)["status"] == "offline"


def test_detect_json_cli(monkeypatch, capsys):
    monkeypatch.setattr(drivers, "run", lambda cmd, timeout=30.0, env=None: (0, LSPCI, "") if cmd[:1] == ["lspci"] else (127, "", ""))
    monkeypatch.setattr(drivers, "sysfs_driver", lambda slot: "nvidia" if slot == "01:00.0" else "i915")
    rc = drivers.main(["detect", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["vendors"] == ["intel", "nvidia"]
    assert out["gpus"][1]["driver"] == "nvidia"


def test_status_json_is_serialisable(monkeypatch, capsys):
    monkeypatch.setattr(drivers, "run", lambda cmd, timeout=30.0, env=None: (127, "", "not found"))
    monkeypatch.setattr(drivers, "sysfs_gpus", lambda: [])
    monkeypatch.setattr(drivers, "have", lambda b: False)
    rc = drivers.main(["--status", "--json"])
    assert rc == 0
    st = json.loads(capsys.readouterr().out)
    for key in ("gpus", "vendors", "nvidia_packages", "modules_loaded", "opengl_renderer", "vulkan_devices",
                "mesa_vulkan", "secure_boot", "reboot_required", "ubuntu_drivers"):
        assert key in st
    assert st["gpus"] == [] and st["nvidia_packages"] == []
