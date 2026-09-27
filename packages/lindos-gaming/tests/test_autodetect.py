"""lindos-drivers autodetect — Broadcom Wi-Fi + audio mapping, wifi.json, `install --auto`,
and the first-boot service/script (SPEC-VM.md section 24)."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import ROOT, load_bin, read_text  # noqa: E402

drivers = load_bin("lindos-drivers")

WIFI_JSON = ROOT / "usr" / "share" / "lindos" / "drivers" / "wifi.json"
SERVICE = ROOT / "usr" / "lib" / "systemd" / "system" / "lindos-driver-firstboot.service"
FIRSTBOOT = ROOT / "usr" / "libexec" / "lindos" / "driver-firstboot.sh"

LSPCI_ALL = """\
00:02.0 VGA compatible controller [0300]: Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics] [8086:46a6] (rev 0c)
01:00.0 3D controller [0302]: NVIDIA Corporation GA107M [GeForce RTX 3050 Mobile] [10de:25a2] (rev a1)
03:00.0 Network controller [0280]: Broadcom Inc. BCM4352 802.11ac Wireless [14e4:43b1] (rev 03)
04:00.0 Network controller [0280]: Broadcom Corporation BCM4318 [AirForce One 54g] [14e4:4318] (rev 02)
05:00.0 Ethernet controller [0200]: Broadcom Inc. NetXtreme BCM5762 [14e4:1687] (rev 10)
00:1f.3 Audio device [0403]: Intel Corporation Alder Lake PCH-P High Definition Audio [8086:51c8] (rev 01)
"""


# --------------------------------------------------------------------------- wifi.json data
def test_wifi_json_valid_and_consistent():
    data = json.loads(read_text(WIFI_JSON))
    assert data["vendor"] == "14e4"
    assert data["wireless_classes"] == ["0280"]
    driver_keys = set(data["drivers"])
    assert {"wl", "b43"} <= driver_keys
    # the three package names the SPEC names must appear in the driver table
    all_pkgs = {p for d in data["drivers"].values() for p in d["recommended"]}
    for pkg in ("bcmwl-kernel-source", "broadcom-sta-dkms", "firmware-b43-installer"):
        assert pkg in all_pkgs, pkg
    # every chip references a defined driver table entry
    for devid, chip in data["chips"].items():
        assert chip["driver"] in driver_keys, f"{devid} -> unknown driver {chip['driver']}"
    assert data["default"]["driver"] in driver_keys


def test_wifi_json_path_honours_lindos_root(tmp_path, monkeypatch):
    fake = tmp_path / "usr" / "share" / "lindos" / "drivers"
    fake.mkdir(parents=True)
    (fake / "wifi.json").write_text('{"vendor":"14e4"}', encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    assert drivers.wifi_json_path() == fake / "wifi.json"
    # unset -> falls back to the copy shipped beside the script
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "nonexistent"))
    assert drivers.wifi_json_path().name == "wifi.json"
    assert drivers.wifi_json_path().is_file()


# --------------------------------------------------------------------------- parsing
def test_parse_lspci_all_keeps_every_class():
    devs = drivers.parse_lspci_all(LSPCI_ALL)
    assert len(devs) == 6
    classes = {d["class_id"] for d in devs}
    assert {"0300", "0302", "0280", "0200", "0403"} <= classes
    assert drivers.parse_lspci_all("") == []


# --------------------------------------------------------------------------- Broadcom Wi-Fi mapping
def test_detect_wifi_maps_broadcom_chips():
    devs = drivers.parse_lspci_all(LSPCI_ALL)
    wifi = drivers.detect_wifi(devs)
    by_id = {d["device_id"]: d for d in wifi["devices"]}
    # BCM4352 (43b1) is a wl chip
    assert by_id["43b1"]["recommended"] == ["bcmwl-kernel-source", "broadcom-sta-dkms"]
    assert by_id["43b1"]["known"] is True
    # BCM4318 (4318) is an open-b43 chip
    assert by_id["4318"]["recommended"] == ["firmware-b43-installer"]
    # the Broadcom *wired* NIC (class 0200) must NOT be treated as Wi-Fi
    assert "1687" not in by_id, "Broadcom ethernet leaked into Wi-Fi recommendations"
    assert "bcmwl-kernel-source" in wifi["recommended"]
    assert "firmware-b43-installer" in wifi["recommended"]


def test_detect_wifi_unknown_broadcom_uses_default():
    devs = drivers.parse_lspci_all(
        "06:00.0 Network controller [0280]: Broadcom Inc. Unknown [14e4:aaaa] (rev 01)\n")
    wifi = drivers.detect_wifi(devs)
    assert len(wifi["devices"]) == 1
    d = wifi["devices"][0]
    assert d["known"] is False
    assert d["recommended"] == ["bcmwl-kernel-source", "broadcom-sta-dkms", "firmware-b43-installer"]


def test_detect_wifi_no_broadcom():
    devs = drivers.parse_lspci_all(
        "03:00.0 Network controller [0280]: Intel Corporation Wi-Fi 6 AX201 [8086:a0f0] (rev 20)\n")
    wifi = drivers.detect_wifi(devs)
    assert wifi == {"devices": [], "recommended": []}


# --------------------------------------------------------------------------- audio firmware
def test_detect_audio_when_controller_present():
    devs = drivers.parse_lspci_all(LSPCI_ALL)
    audio = drivers.detect_audio(devs)
    assert audio["recommended"] == ["sof-firmware", "alsa-ucm-conf"]
    assert len(audio["devices"]) == 1
    assert "firmware-sof-signed" in audio["note"]


def test_detect_audio_absent():
    devs = drivers.parse_lspci_all(
        "00:02.0 VGA compatible controller [0300]: Intel [Iris Xe] [8086:46a6] (rev 0c)\n")
    audio = drivers.detect_audio(devs)
    assert audio["recommended"] == [] and audio["devices"] == []


# --------------------------------------------------------------------------- sysfs (LINDOS_ROOT)
def test_sysfs_devices_lindos_root(tmp_path, monkeypatch):
    base = tmp_path / "sys" / "bus" / "pci" / "devices"
    try:
        dev = base / "0000:03:00.0"
        dev.mkdir(parents=True)
    except OSError:
        pytest.skip("filesystem rejects ':' in path components (Windows) — covered via lspci text")
    (dev / "class").write_text("0x028000\n", encoding="utf-8")
    (dev / "vendor").write_text("0x14e4\n", encoding="utf-8")
    (dev / "device").write_text("0x43b1\n", encoding="utf-8")
    (dev / "revision").write_text("0x03\n", encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    monkeypatch.setattr(drivers, "run", lambda cmd, timeout=30.0, env=None: (127, "", "not found"))
    devs = drivers.all_pci_devices()
    assert any(d["vendor_id"] == "14e4" and d["device_id"] == "43b1" and d["class_id"] == "0280"
               for d in devs)
    wifi = drivers.detect_wifi(devs)
    assert wifi["devices"] and wifi["devices"][0]["recommended"][0] == "bcmwl-kernel-source"


# --------------------------------------------------------------------------- autodetect_dict + CLI
def _patch_lspci(monkeypatch, text=LSPCI_ALL):
    monkeypatch.setattr(drivers, "run",
                        lambda cmd, timeout=30.0, env=None: (0, text, "") if cmd[:1] == ["lspci"] else (127, "", ""))
    monkeypatch.setattr(drivers, "sysfs_driver", lambda slot: None)


def test_autodetect_dict_structure(monkeypatch):
    _patch_lspci(monkeypatch)
    auto = drivers.autodetect_dict()
    assert auto["gpu_vendors"] == ["intel", "nvidia"]
    assert auto["recommended"]["wifi"]
    assert auto["recommended"]["audio"] == ["sof-firmware", "alsa-ucm-conf"]
    # fully JSON-serialisable
    json.dumps(auto)


def test_autodetect_cli_json(monkeypatch, capsys):
    _patch_lspci(monkeypatch)
    assert drivers.main(["autodetect", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["recommended"]) == {"gpu", "wifi", "audio"}
    assert out["gpu_vendors"] == ["intel", "nvidia"]


# --------------------------------------------------------------------------- build_plan extra
def test_build_plan_extra_packages_gpu_and_extras(monkeypatch):
    monkeypatch.setattr(drivers, "i386_enabled", lambda: True)
    plan = drivers.build_plan(["amd"], drivers.parse_lspci_all(LSPCI_ALL), "auto",
                              extra_packages=["bcmwl-kernel-source", "sof-firmware", "alsa-ucm-conf"])
    assert "mesa-vulkan-drivers" in plan["packages"]
    assert "bcmwl-kernel-source" in plan["packages"] and "sof-firmware" in plan["packages"]
    assert plan["commands"][0] == ["apt-get", "update"]  # i386 already enabled


def test_build_plan_extra_only_skips_i386(monkeypatch):
    # no GPU target: the i386 dance must be skipped even when i386 is disabled
    monkeypatch.setattr(drivers, "i386_enabled", lambda: False)
    plan = drivers.build_plan([], [], "auto", extra_packages=["bcmwl-kernel-source", "sof-firmware"])
    assert not any(c[:2] == ["dpkg", "--add-architecture"] for c in plan["commands"])
    assert plan["commands"][0] == ["apt-get", "update"]
    assert plan["packages"] == ["bcmwl-kernel-source", "sof-firmware"]
    assert plan["reboot"] is False


# --------------------------------------------------------------------------- install --auto
def test_install_auto_dry_run_json(monkeypatch, capsys):
    _patch_lspci(monkeypatch)
    monkeypatch.setattr(drivers, "i386_enabled", lambda: True)
    assert drivers.main(["install", "--auto", "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry-run"
    pkgs = out["plan"]["packages"]
    assert "sof-firmware" in pkgs and "bcmwl-kernel-source" in pkgs


def test_install_auto_offline_exit_3(monkeypatch, capsys):
    _patch_lspci(monkeypatch)
    monkeypatch.setattr(drivers, "online", lambda: False)
    assert drivers.main(["install", "--auto", "--json"]) == drivers.EXIT_OFFLINE
    assert json.loads(capsys.readouterr().out)["status"] == "offline"


def test_install_auto_rejects_vendor_flags():
    assert drivers.main(["install", "--auto", "--amd"]) == drivers.EXIT_USAGE


def test_installable_wifi_packages_first_available(monkeypatch):
    wifi = drivers.detect_wifi(drivers.parse_lspci_all(LSPCI_ALL))
    # only broadcom-sta-dkms available for the wl chip; firmware-b43-installer for the b43 chip
    avail = {"broadcom-sta-dkms", "firmware-b43-installer"}
    monkeypatch.setattr(drivers, "apt_candidate", lambda pkg: pkg in avail)
    chosen = drivers.installable_wifi_packages(wifi)
    assert chosen == ["broadcom-sta-dkms", "firmware-b43-installer"]


def test_installable_wifi_packages_falls_back_to_first(monkeypatch):
    wifi = drivers.detect_wifi(drivers.parse_lspci_all(
        "03:00.0 Network controller [0280]: Broadcom BCM4352 [14e4:43b1] (rev 03)\n"))
    monkeypatch.setattr(drivers, "apt_candidate", lambda pkg: False)
    assert drivers.installable_wifi_packages(wifi) == ["bcmwl-kernel-source"]


def test_escalate_auto_uses_install_drivers_and_install_packages(monkeypatch):
    calls = []

    class _Res:
        def __init__(self):
            self.ok, self.code, self.out, self.err = True, 0, "", ""

    def fake_run_privileged(action, payload=None, log=None, timeout=None):
        calls.append((action, payload))
        return _Res()

    fake_helper = types.ModuleType("lindos.helper")
    fake_helper.run_privileged = fake_run_privileged
    fake_pkg = types.ModuleType("lindos")
    monkeypatch.setitem(sys.modules, "lindos", fake_pkg)
    monkeypatch.setitem(sys.modules, "lindos.helper", fake_helper)

    auto = {"gpu_vendors": ["nvidia"], "wifi": {"devices": [], "recommended": []},
            "audio": {"devices": [], "recommended": []}}
    rc = drivers.escalate_auto(auto, ["bcmwl-kernel-source", "sof-firmware"], as_json=True)
    assert rc == drivers.EXIT_OK
    actions = [a for a, _ in calls]
    assert actions == ["install-drivers", "install-packages"]
    assert calls[0][1] == {"args": []}          # GPU: auto-resolve vendors as root
    assert calls[1][1] == {"packages": ["bcmwl-kernel-source", "sof-firmware"]}


def test_escalate_auto_no_gpu_only_packages(monkeypatch):
    calls = []

    class _Res:
        ok, code, out, err = True, 0, "", ""

    fake_helper = types.ModuleType("lindos.helper")
    fake_helper.run_privileged = lambda action, payload=None, log=None, timeout=None: (
        calls.append((action, payload)) or _Res())
    monkeypatch.setitem(sys.modules, "lindos", types.ModuleType("lindos"))
    monkeypatch.setitem(sys.modules, "lindos.helper", fake_helper)
    auto = {"gpu_vendors": [], "wifi": {"devices": [], "recommended": []},
            "audio": {"devices": [], "recommended": []}}
    drivers.escalate_auto(auto, ["sof-firmware"], as_json=True)
    assert [a for a, _ in calls] == ["install-packages"]


# --------------------------------------------------------------------------- first-boot unit/script
def test_firstboot_service_unit():
    unit = read_text(SERVICE)
    # Never on a live/ISO boot (same guard as lindos-browser-firstboot.service): a boot-test or
    # live-USB session would otherwise run full hardware autodetect on every single boot, since
    # it can never persist the done-marker to a real, writable /var/lib.
    assert "ConditionKernelCommandLine=!boot=casper" in unit
    assert "ConditionPathExists=!/var/lib/lindos/driver-firstboot.done" in unit
    assert "ConditionVirtualization=!container" in unit
    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/libexec/lindos/driver-firstboot.sh" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "\r\n" not in unit


def test_firstboot_script_shape():
    text = read_text(FIRSTBOOT)
    assert text.startswith("#!/bin/bash\n")
    assert "set -Eeuo pipefail" in text
    assert "/var/lib/lindos/driver-firstboot.done" in text
    assert "lindos-drivers autodetect --json" in text
    assert "lindos-drivers install --auto" in text
    # never installs by itself
    for forbidden in ("apt-get install", "apt install"):
        assert forbidden not in text, forbidden
    # honours OEM / online gating
    assert "is_oem" in text and "online" in text
    assert "\r\n" not in text
