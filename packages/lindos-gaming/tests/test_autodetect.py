"""lindos-drivers autodetect — Broadcom Wi-Fi + audio mapping, wifi.json, `install --auto`,
and the first-boot service/script (SPEC-VM.md section 24)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import ROOT, load_bin, read_text  # noqa: E402

drivers = load_bin("lindos-drivers")

WIFI_JSON = ROOT / "usr" / "share" / "lindos" / "drivers" / "wifi.json"
SERVICE = ROOT / "usr" / "lib" / "systemd" / "system" / "lindos-driver-firstboot.service"
FIRSTBOOT = ROOT / "usr" / "libexec" / "lindos" / "driver-firstboot.sh"
CORE_ROOT = ROOT.parent.parent / "lindos-core" / "root"
CORE_LIBEXEC = CORE_ROOT / "usr" / "libexec" / "lindos"
CORE_PYLIB = CORE_ROOT / "usr" / "lib" / "python3" / "dist-packages"
BASH = shutil.which("bash")

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
    assert "ConditionKernelCommandLine=!boot=casper" in unit and "ConditionKernelCommandLine=!boot=live" in unit
    # not before the new user has finished Ubiquity's oem-config first-boot wizard
    assert "ConditionPathExists=!/lib/systemd/system/oem-config.target" in unit
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
    # the silent retry: free-only by default, the full auto install only with the installer's consent
    assert "ubuntu-drivers install --free-only" in text
    assert "lindos-drivers install --auto" in text and "driver-proprietary-consent" in text
    # goes through the existing tools, never calls apt itself
    for forbidden in ("apt-get install", "apt install", "apt-get -y"):
        assert forbidden not in text, forbidden
    # install-state first; the shared helpers instead of private copies; online gating
    assert "lindos.installstate" in text and "status drivers" in text and "mark drivers" in text
    assert "is-live-session" in text and "oem-config-pending" in text and "/proc/cmdline" not in text
    assert "online" in text
    # the "install offer" nobody consumed is gone
    assert "driver-install-offer" not in text and "write_offer" not in text
    assert "\r\n" not in text


# --------------------------------------------------------------------------- first-boot behaviour (hermetic)
NVIDIA_AUTODETECT = json.dumps({"gpus": [], "gpu_vendors": ["nvidia"], "wifi": {"devices": [], "recommended": []},
                                "audio": {"devices": [], "recommended": []},
                                "recommended": {"gpu": ["nvidia"], "wifi": [], "audio": []}})
INTEL_AUTODETECT = json.dumps({"gpus": [], "gpu_vendors": ["intel"], "wifi": {"devices": [], "recommended": []},
                               "audio": {"devices": [], "recommended": []},
                               "recommended": {"gpu": ["intel"], "wifi": [], "audio": []}})
INSTALLED_CMDLINE = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=1234 ro quiet splash"


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _fake(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body if body.startswith("#!") else "#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


#: stands in for flock(1): records its arguments, honours -o/-w/-E, and either runs the command (the lock was
#: free) or exits with the -E status (still held after the wait)
FAKE_FLOCK = r'''#!/bin/bash
printf '%s\n' "$*" >>"${FAKE_FLOCK_LOG}"
conflict=1
while [ $# -gt 0 ]; do
    case "$1" in
        -o) shift ;;
        -w) shift 2 ;;
        -E) conflict="$2"; shift 2 ;;
        *) break ;;
    esac
done
lock="$1"; shift
[ "${FAKE_FLOCK_BUSY:-0}" = 1 ] && exit "${conflict}"
exec "$@"
'''


class Firstboot:
    """A scratch LINDOS_ROOT with the shared lindos-core helpers and fake system tools."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        libexec = self.root / "usr" / "libexec" / "lindos"
        libexec.mkdir(parents=True)
        for name in ("is-live-session", "oem-config-pending", "wait-for-network"):
            shutil.copy(CORE_LIBEXEC / name, libexec / name)
        self.state_dir = self.root / "var" / "lib" / "lindos"
        self.bin = tmp / "fakebin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        _fake(self.bin, "id", '[ "$1" = "-u" ] && echo 0 || echo root\n')
        _fake(self.bin, "nm-online", "exit 0\n")
        _fake(self.bin, "lindos-drivers",
              'printf "lindos-drivers %s\\n" "$*" >> "$FAKE_LOG"\n'
              'case "$1" in\n'
              '  autodetect) [ -n "${FAKE_AUTODETECT_FAIL:-}" ] && exit 1; out="${FAKE_AUTODETECT:-}"; '
              '[ -n "${out}" ] || out="{}"; printf "%s\\n" "${out}"; exit 0 ;;\n'
              '  *) exit "${FAKE_DRIVERS_RC:-0}" ;;\n'
              "esac\n")
        _fake(self.bin, "ubuntu-drivers",
              'printf "ubuntu-drivers %s\\n" "$*" >> "$FAKE_LOG"\nexit "${FAKE_UBUNTU_RC:-0}"\n')
        _fake(self.bin, "mokutil", 'printf "%s\\n" "${FAKE_SB_STATE:-SecureBoot disabled}"\n')
        # the host's timeout could be Windows' timeout.exe: a fake that runs the command (or pretends it timed out)
        _fake(self.bin, "timeout",
              '[ -n "${FAKE_TIMEOUT_RC:-}" ] && exit "${FAKE_TIMEOUT_RC}"\n'
              '[ "$1" = "-k" ] && shift 2\nshift\nexec "$@"\n')
        self.serialiser_env: Dict[str, str] = {}

    def enable_serialiser(self) -> None:
        """Ship lindos-core's apt-serialise next to the script (as the package does), with a fake flock(1)."""
        shutil.copy(CORE_LIBEXEC / "apt-serialise", self.root / "usr" / "libexec" / "lindos" / "apt-serialise")
        _fake(self.bin, "flock-fake", FAKE_FLOCK)
        self.serialiser_env = {"LINDOS_FLOCK": _msys(str(self.bin / "flock-fake")),
                               "FAKE_FLOCK_LOG": _msys(str(self.tmp / "flock.log"))}

    def flock_calls(self) -> List[str]:
        log = self.tmp / "flock.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def write_state(self, **steps: str) -> Path:
        path = self.state_dir / "install-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "updated": "t", "online": True, "steps": {
            name: {"status": status, "detail": "", "time": "t"} for name, status in steps.items()}}), encoding="utf-8")
        return path

    def state(self) -> dict:
        return json.loads((self.state_dir / "install-state.json").read_text(encoding="utf-8"))

    def calls(self) -> List[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, *args: str, cmdline: str = INSTALLED_CMDLINE, offline: bool = False,
            extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        assert BASH is not None
        cmdline_file = self.tmp / "cmdline"
        cmdline_file.write_text(cmdline, encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.update({"LINDOS_ROOT": str(self.root), "LINDOS_TEST_CMDLINE": _msys(str(cmdline_file)),
                    "LINDOS_TEST_IN_CHROOT": "0", "LINDOS_PYTHON": sys.executable,
                    "PYTHONPATH": str(CORE_PYLIB) + os.pathsep + env.get("PYTHONPATH", ""),
                    "FAKE_LOG": _msys(str(self.log))})
        env.pop("LINDOS_OFFLINE", None)
        for name in ("APT_CONFIG", "LINDOS_APT_SERIALISED", "FAKE_FLOCK_BUSY", "LINDOS_DRIVER_LOCK_WAIT"):
            env.pop(name, None)
        env.update(self.serialiser_env)
        if offline:
            env["LINDOS_OFFLINE"] = "1"
        if extra:
            env.update(extra)
        return subprocess.run([BASH, str(FIRSTBOOT), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=env)

    @property
    def marker(self) -> Path:
        return self.state_dir / "driver-firstboot.done"


needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@needs_bash
def test_firstboot_script_syntax():
    assert subprocess.run([BASH, "-n", str(FIRSTBOOT)], capture_output=True).returncode == 0


@needs_bash
def test_firstboot_never_runs_in_the_live_session(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    for cmdline in ("BOOT_IMAGE=/casper/vmlinuz boot=casper quiet splash", "boot=live quiet"):
        proc = fb.run(cmdline=cmdline)
        assert proc.returncode == 0 and "refusing to run" in proc.stderr
    assert fb.calls() == [] and not fb.marker.exists()
    assert fb.state()["steps"]["drivers"]["status"] == "pending"


@needs_bash
def test_firstboot_waits_for_the_oem_config_wizard(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    unit = fb.root / "lib" / "systemd" / "system" / "oem-config.target"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    proc = fb.run()
    assert proc.returncode == 0 and "oem-config first-boot wizard is still pending" in proc.stderr
    assert fb.calls() == [] and not fb.marker.exists()
    unit.unlink()                                                  # the wizard finished
    assert fb.run().returncode == 0 and any(c.startswith("ubuntu-drivers") for c in fb.calls())


@needs_bash
def test_firstboot_without_the_shared_helper_assumes_live(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    (fb.root / "usr" / "libexec" / "lindos" / "is-live-session").unlink()
    proc = fb.run()
    assert proc.returncode == 0 and "refusing to run" in proc.stderr and fb.calls() == []


@needs_bash
@pytest.mark.parametrize("recorded", ["done", "skipped"])
def test_firstboot_terminal_install_state_exits_at_once(tmp_path, recorded):
    fb = Firstboot(tmp_path)
    state_file = fb.write_state(drivers=recorded)
    before = state_file.read_text(encoding="utf-8")
    proc = fb.run()
    assert proc.returncode == 0, proc.stderr
    assert f"drivers={recorded}" in proc.stderr
    assert fb.marker.exists()
    assert fb.calls() == [], "nothing may be detected or installed when the installer handled the step"
    assert state_file.read_text(encoding="utf-8") == before
    assert not (fb.state_dir / "driver-install-offer.json").exists()


@needs_bash
def test_firstboot_waits_for_networkmanager_before_it_says_offline(tmp_path):
    """NetworkManager-wait-online is masked on Lindos, so the unit starts before Wi-Fi/DHCP is up: the script
    asks the shared wait-for-network helper (bounded) and only then decides."""
    fb = Firstboot(tmp_path)
    _fake(fb.bin, "nm-online",
          'printf "nm-online %s\\n" "$*" >> "$FAKE_LOG"\nexit "${FAKE_NM_RC:-0}"\n')
    proc = fb.run(extra={"FAKE_NM_RC": "1"})                       # still offline when the wait is over
    assert proc.returncode == 0, proc.stderr
    assert "offline" in proc.stderr
    assert fb.calls() == ["nm-online -q -t 90"], "the bounded wait, then nothing: no detection, no install"
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == "pending" and "offline" in entry["detail"] and not fb.marker.exists()
    proc = fb.run(extra={"FAKE_NM_RC": "0"})                       # the connection came up during the wait
    assert proc.returncode == 0, proc.stderr
    assert fb.calls()[1] == "nm-online -q -t 90" and any(c.startswith("ubuntu-drivers") for c in fb.calls())


@needs_bash
def test_firstboot_without_the_wait_helper_probes_by_itself(tmp_path):
    fb = Firstboot(tmp_path)
    (fb.root / "usr" / "libexec" / "lindos" / "wait-for-network").unlink()
    proc = fb.run()
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("ubuntu-drivers") for c in fb.calls())


@needs_bash
def test_firstboot_offline_records_pending_and_retries_later(tmp_path):
    fb = Firstboot(tmp_path)
    proc = fb.run(offline=True)
    assert proc.returncode == 0, proc.stderr
    # nm-online is a fake that says "online"; LINDOS_OFFLINE=1 beats every probe
    assert "offline" in proc.stderr and fb.calls() == []
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == "pending" and "offline" in entry["detail"]
    assert not fb.marker.exists()
    # an already recorded 'failed' is not overwritten by an offline boot
    fb.write_state(drivers="failed")
    fb.run(offline=True)
    assert fb.state()["steps"]["drivers"]["status"] == "failed"


@needs_bash
def test_firstboot_default_retry_is_free_only_and_records_done(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending", browser="done")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT})
    assert proc.returncode == 0, proc.stderr
    calls = fb.calls()
    assert "ubuntu-drivers install --free-only" in calls
    assert not any(c.startswith("lindos-drivers install") for c in calls), calls       # no proprietary path
    state = fb.state()
    assert state["steps"]["drivers"]["status"] == "done" and "free drivers only" in state["steps"]["drivers"]["detail"]
    assert state["steps"]["browser"]["status"] == "done"                                # other steps untouched
    assert fb.marker.exists()
    assert (fb.state_dir / "driver-recommendations.json").is_file()                       # diagnostic record
    assert not (fb.state_dir / "driver-install-offer.json").exists()                      # no offer file any more
    assert (fb.state_dir / "driver-firstboot.attempts").read_text(encoding="utf-8").strip() == "1"


@needs_bash
def test_firstboot_unknown_state_is_retried_like_pending(tmp_path):
    fb = Firstboot(tmp_path)                       # no install-state.json at all (legacy install)
    assert fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT}).returncode == 0
    assert "ubuntu-drivers install --free-only" in fb.calls()
    assert fb.state()["steps"]["drivers"]["status"] == "done"


@needs_bash
def test_firstboot_consent_allows_the_full_auto_install(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    fb.state_dir.mkdir(parents=True, exist_ok=True)
    (fb.state_dir / "driver-proprietary-consent").write_text("", encoding="utf-8")
    proc = fb.run(extra={"FAKE_AUTODETECT": NVIDIA_AUTODETECT, "FAKE_SB_STATE": "SecureBoot disabled"})
    assert proc.returncode == 0, proc.stderr
    assert "lindos-drivers install --auto" in fb.calls()
    assert not any(c.startswith("ubuntu-drivers") for c in fb.calls())
    assert fb.state()["steps"]["drivers"]["status"] == "done" and fb.marker.exists()


@needs_bash
def test_firstboot_never_installs_an_nvidia_dkms_driver_under_secure_boot(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    (fb.state_dir / "driver-proprietary-consent").write_text("", encoding="utf-8")
    proc = fb.run(extra={"FAKE_AUTODETECT": NVIDIA_AUTODETECT, "FAKE_SB_STATE": "SecureBoot enabled"})
    assert proc.returncode == 0, proc.stderr
    calls = fb.calls()
    assert "ubuntu-drivers install --free-only" in calls
    assert not any(c.startswith("lindos-drivers install") for c in calls), calls
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == "skipped" and "Secure Boot" in entry["detail"]
    assert fb.marker.exists()


@needs_bash
def test_firstboot_secure_boot_with_unknown_hardware_is_treated_like_nvidia(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    (fb.state_dir / "driver-proprietary-consent").write_text("", encoding="utf-8")
    proc = fb.run(extra={"FAKE_AUTODETECT_FAIL": "1", "FAKE_SB_STATE": "SecureBoot enabled"})
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("lindos-drivers install") for c in fb.calls())
    assert fb.state()["steps"]["drivers"]["status"] == "skipped"


@needs_bash
def test_firstboot_secure_boot_with_intel_gpu_and_consent_still_installs(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="failed")
    (fb.state_dir / "driver-proprietary-consent").write_text("", encoding="utf-8")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, "FAKE_SB_STATE": "SecureBoot enabled"})
    assert proc.returncode == 0, proc.stderr
    assert "lindos-drivers install --auto" in fb.calls()
    assert fb.state()["steps"]["drivers"]["status"] == "done"


@needs_bash
@pytest.mark.parametrize("rc_env, status, detail", [
    ({"FAKE_UBUNTU_RC": "3"}, "pending", "offline"),
    ({"FAKE_UBUNTU_RC": "1"}, "failed", "exit 1"),
    ({"FAKE_TIMEOUT_RC": "124"}, "failed", "timed out"),
])
def test_firstboot_retry_outcomes_are_recorded_and_left_for_a_later_boot(tmp_path, rc_env, status, detail):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, **rc_env})
    assert proc.returncode == 0, proc.stderr
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == status and detail in entry["detail"]
    assert not fb.marker.exists()                       # a later boot retries


@needs_bash
def test_firstboot_gives_up_after_three_attempts(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="failed")
    (fb.state_dir / "driver-firstboot.attempts").write_text("3\n", encoding="utf-8")
    proc = fb.run()
    assert proc.returncode == 0 and "giving up" in proc.stderr
    assert fb.calls() == [] and fb.marker.exists()
    assert fb.state()["steps"]["drivers"]["status"] == "failed"      # Settings can still offer 'Install now'
    # --force ignores both the marker and the cap
    forced = fb.run("--force", extra={"FAKE_AUTODETECT": INTEL_AUTODETECT})
    assert forced.returncode == 0 and "ubuntu-drivers install --free-only" in fb.calls()


@needs_bash
def test_firstboot_missing_ubuntu_drivers_is_a_recorded_skip(tmp_path):
    fb = Firstboot(tmp_path)
    (fb.bin / "ubuntu-drivers").unlink()
    fb.write_state(drivers="pending")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT})
    assert proc.returncode == 0
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == "skipped" and "ubuntu-drivers" in entry["detail"]


@needs_bash
def test_firstboot_marker_and_chroot_guards(tmp_path):
    fb = Firstboot(tmp_path)
    fb.write_state(drivers="pending")
    fb.state_dir.mkdir(parents=True, exist_ok=True)
    fb.marker.write_text("", encoding="utf-8")
    proc = fb.run()
    assert proc.returncode == 0 and "already done" in proc.stderr and fb.calls() == []
    fb.marker.unlink()
    chroot = fb.run(extra={"LINDOS_TEST_IN_CHROOT": "1"})
    assert chroot.returncode == 0 and "chroot" in chroot.stderr and fb.calls() == []


# --------------------------------------------------------------------------- apt discipline (review finding: both retries hit apt at once)
def test_firstboot_service_is_ordered_after_the_chrome_retry():
    """oem-config ends -> both retry units are queued together; apt does not queue by itself, so this one waits."""
    lines = [ln.strip() for ln in read_text(SERVICE).splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    after = next(ln for ln in lines if ln.startswith("After=")).split("=", 1)[1].split()
    assert "lindos-browser-firstboot.service" in after and "network-online.target" in after
    assert not any(ln.startswith(("Requires=", "BindsTo=", "Requisite=", "PartOf=")) for ln in lines), \
        "ordering only: a failed Chrome retry must never stop the driver retry"


def _record_env(fb: Firstboot, log: Path) -> None:
    """Replace the fake driver tools by ones that also say what apt-related environment they were started with."""
    record = ('{ printf "serialised=%s\n" "${LINDOS_APT_SERIALISED:-}"; '
              '[ -f "${APT_CONFIG:-/nonexistent}" ] && tr "\n" " " <"${APT_CONFIG}"; printf "\n"; } >>"' + _msys(str(log)) + '"')
    _fake(fb.bin, "ubuntu-drivers",
          'printf "ubuntu-drivers %s\n" "$*" >> "$FAKE_LOG"\n' + record + '\nexit "${FAKE_UBUNTU_RC:-0}"\n')
    _fake(fb.bin, "lindos-drivers",
          'printf "lindos-drivers %s\n" "$*" >> "$FAKE_LOG"\n'
          'case "$1" in\n'
          '  autodetect) out="${FAKE_AUTODETECT:-}"; [ -n "${out}" ] || out="{}"; printf "%s\n" "${out}"; exit 0 ;;\n'
          '  *) ' + record + '; exit "${FAKE_DRIVERS_RC:-0}" ;;\n'
          'esac\n')


@needs_bash
@pytest.mark.parametrize("consent, tool, args", [
    (False, "ubuntu-drivers", "install --free-only"),
    (True, "lindos-drivers", "install --auto"),
])
def test_firstboot_retry_takes_its_turn_and_every_apt_get_it_starts_waits_for_the_dpkg_lock(tmp_path, consent, tool, args):
    fb = Firstboot(tmp_path)
    fb.enable_serialiser()
    fb.write_state(drivers="pending")
    if consent:
        (fb.state_dir / "driver-proprietary-consent").write_text("", encoding="utf-8")
    env_log = tmp_path / "tool-env.log"
    _record_env(fb, env_log)
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, "FAKE_SB_STATE": "SecureBoot disabled"})
    assert proc.returncode == 0, proc.stderr
    calls = fb.flock_calls()
    assert len(calls) == 1, calls
    line = calls[0].replace("\\", "/")
    # one flock on the shared lock, 3 minutes of queueing; the 15-minute limit sits INSIDE the queue
    assert line.startswith("-o -w 180 -E 199 ") and "/run/lindos/apt.lock timeout -k 30 900 " + tool + " " + args in line, line
    seen = env_log.read_text(encoding="utf-8")
    assert "serialised=1" in seen and 'DPkg::Lock::Timeout "300";' in seen, seen      # what the tool's apt-get inherits
    assert tool + " " + args in fb.calls()
    assert fb.state()["steps"]["drivers"]["status"] == "done" and fb.marker.exists()


@needs_bash
def test_firstboot_a_busy_queue_delays_the_retry_but_never_skips_it_or_runs_it_twice(tmp_path):
    fb = Firstboot(tmp_path)
    fb.enable_serialiser()
    fb.write_state(drivers="pending")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, "FAKE_FLOCK_BUSY": "1"})
    assert proc.returncode == 0, proc.stderr
    assert fb.calls().count("ubuntu-drivers install --free-only") == 1
    assert fb.state()["steps"]["drivers"]["status"] == "done" and fb.marker.exists()
    log = (fb.root / "var" / "log" / "lindos" / "driver-firstboot.log").read_text(encoding="utf-8")
    assert "running anyway" in log


@needs_bash
def test_firstboot_the_queue_wait_is_tunable(tmp_path):
    fb = Firstboot(tmp_path)
    fb.enable_serialiser()
    fb.write_state(drivers="pending")
    assert fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, "LINDOS_DRIVER_LOCK_WAIT": "12"}).returncode == 0
    assert " -w 12 " in fb.flock_calls()[0]


@needs_bash
def test_firstboot_a_failed_queued_retry_is_still_recorded_like_before(tmp_path):
    """The queue must not change what a failing retry looks like: 'failed', attempt counted, no marker."""
    fb = Firstboot(tmp_path)
    fb.enable_serialiser()
    fb.write_state(drivers="pending")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT, "FAKE_UBUNTU_RC": "100"})
    assert proc.returncode == 0, proc.stderr
    entry = fb.state()["steps"]["drivers"]
    assert entry["status"] == "failed" and "exit 100" in entry["detail"]
    assert not fb.marker.exists()
    assert (fb.state_dir / "driver-firstboot.attempts").read_text(encoding="utf-8").strip() == "1"


@needs_bash
def test_firstboot_without_the_apt_helper_runs_the_retry_as_before(tmp_path):
    fb = Firstboot(tmp_path)                 # a lindos-core without apt-serialise: nothing to queue behind
    fb.write_state(drivers="pending")
    proc = fb.run(extra={"FAKE_AUTODETECT": INTEL_AUTODETECT})
    assert proc.returncode == 0, proc.stderr
    assert "ubuntu-drivers install --free-only" in fb.calls() and fb.flock_calls() == []
    assert fb.state()["steps"]["drivers"]["status"] == "done"
