"""wait-for-network: the bounded wait the silent first-boot retries make before they call the machine
offline (NetworkManager-wait-online is masked on Lindos, so network-online.target comes too early), and
its use by browser-firstboot.sh.  Hermetic: nm-online is a fake program, paths come from LINDOS_ROOT."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = _PKG_ROOT / "root" / "usr" / "libexec" / "lindos"
PYLIB = _PKG_ROOT / "root" / "usr" / "lib" / "python3" / "dist-packages"
HELPER = LIBEXEC / "wait-for-network"
FIRSTBOOT = LIBEXEC / "browser-firstboot.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

INSTALLED = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=1234 ro quiet splash"


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _fake(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)
    return path


class Net:
    """A fake nm-online that records its arguments and exits with a chosen status."""

    def __init__(self, tmp: Path, rc: Optional[int] = 0) -> None:
        self.tmp = tmp
        self.bin = tmp / "netbin"
        self.bin.mkdir()
        self.log = tmp / "nm-online.log"
        if rc is not None:
            _fake(self.bin, "nm-online", f'printf "%s\\n" "$*" >> "{_msys(str(self.log))}"\nexit {rc}\n')

    def calls(self) -> list:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env.pop("LINDOS_OFFLINE", None)
        env.pop("LINDOS_FORCE_OFFLINE", None)
        env.pop("LINDOS_NETWORK_WAIT", None)
        env["LINDOS_NM_ONLINE"] = _msys(str(self.bin / "nm-online"))
        if extra:
            env.update(extra)
        return env


def _run_helper(net: Net, *args: str, extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    assert BASH is not None
    return subprocess.run([BASH, str(HELPER), *args], capture_output=True, text=True, timeout=60, env=net.env(extra))


# --- the helper --------------------------------------------------------------------------------------
@needs_bash
@pytest.mark.parametrize("nm_rc, expected", [(0, 0), (1, 1), (2, 0), (3, 0)])
def test_exit_status_follows_nm_online(tmp_path: Path, nm_rc: int, expected: int) -> None:
    """0 online, 1 still offline after the wait; anything else (NetworkManager not running...) = cannot tell."""
    net = Net(tmp_path, nm_rc)
    proc = _run_helper(net)
    assert proc.returncode == expected, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""                    # silent: the exit status is the answer
    assert net.calls() == ["-q -t 90"]                                # bounded: quiet, default 90 s


@needs_bash
def test_seconds_argument_and_environment(tmp_path: Path) -> None:
    net = Net(tmp_path)
    assert _run_helper(net, "12").returncode == 0
    assert _run_helper(net, extra={"LINDOS_NETWORK_WAIT": "30"}).returncode == 0
    assert _run_helper(net, "7", extra={"LINDOS_NETWORK_WAIT": "30"}).returncode == 0     # the argument wins
    for junk in ("soon", "-5", "1.5", ""):
        assert _run_helper(net, junk).returncode == 0
    assert net.calls() == ["-q -t 12", "-q -t 30", "-q -t 7"] + ["-q -t 90"] * 4


@needs_bash
def test_zero_seconds_means_do_not_wait(tmp_path: Path) -> None:
    net = Net(tmp_path, 1)
    assert _run_helper(net, "0").returncode == 0                      # cannot tell: the caller's probe decides
    assert _run_helper(net, extra={"LINDOS_NETWORK_WAIT": "0"}).returncode == 0
    assert net.calls() == []


@needs_bash
def test_no_nm_online_means_cannot_tell(tmp_path: Path) -> None:
    net = Net(tmp_path, None)
    assert _run_helper(net).returncode == 0


@needs_bash
@pytest.mark.parametrize("extra", [{"LINDOS_OFFLINE": "1"}, {"LINDOS_FORCE_OFFLINE": "1"}])
def test_the_offline_test_switches_win_without_waiting(tmp_path: Path, extra: Dict[str, str]) -> None:
    net = Net(tmp_path, 0)
    assert _run_helper(net, extra=extra).returncode == 1
    assert net.calls() == []


def test_helper_script_shape() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r" not in text and "set -Eeuo pipefail" in text
    assert "sudo " not in text and "LINDOS_NM_ONLINE" in text and "LINDOS_NETWORK_WAIT" in text


def test_postinst_makes_the_helper_executable() -> None:
    assert "wait-for-network" in (_PKG_ROOT / "DEBIAN" / "postinst").read_text(encoding="utf-8")


# --- browser-firstboot.sh uses it --------------------------------------------------------------------
def _sandbox(tmp_path: Path, *, with_helper: bool = True):
    root = tmp_path / "root"
    libexec = root / "usr" / "libexec" / "lindos"
    libexec.mkdir(parents=True)
    (root / "etc" / "lindos").mkdir(parents=True)
    shutil.copy(FIRSTBOOT, libexec / "browser-firstboot.sh")
    names = ["is-live-session", "oem-config-pending"] + (["wait-for-network"] if with_helper else [])
    for name in names:
        shutil.copy(LIBEXEC / name, libexec / name)
    canary = tmp_path / "install-browser-was-called"
    fake_install = libexec / "install-browser.sh"
    fake_install.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$*\" > \"{canary.as_posix()}\"\nexit 0\n",
                            encoding="utf-8", newline="\n")
    fake_install.chmod(0o755)
    (root / "etc" / "lindos" / "system.json").write_text(
        json.dumps({"mode": "everyday", "browser": "chrome", "oem": False}), encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _fake(fakebin, "id", '[ "$1" = "-u" ] && echo 0 || echo root\n')
    return root, fakebin, canary


def _run_firstboot(root: Path, fakebin: Path, net: Net, tmp_path: Path) -> subprocess.CompletedProcess:
    assert BASH is not None
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(INSTALLED, encoding="utf-8")
    env = net.env({"LINDOS_ROOT": str(root), "LINDOS_TEST_CMDLINE": str(cmdline), "LINDOS_PYTHON": sys.executable,
                   "PYTHONPATH": str(PYLIB) + os.pathsep + os.environ.get("PYTHONPATH", "")})
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    return subprocess.run([BASH, str(root / "usr" / "libexec" / "lindos" / "browser-firstboot.sh")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)


def _state(root: Path) -> dict:
    return json.loads((root / "var" / "lib" / "lindos" / "install-state.json").read_text(encoding="utf-8"))


@needs_bash
def test_browser_firstboot_gives_up_for_now_when_no_connection_comes_up(tmp_path: Path) -> None:
    root, fakebin, canary = _sandbox(tmp_path)
    net = Net(tmp_path, 1)                                              # NetworkManager: still offline after the wait
    proc = _run_firstboot(root, fakebin, net, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "no network connection yet" in proc.stderr
    assert not canary.exists(), "install-browser.sh must not run without a connection"
    entry = _state(root)["steps"]["browser"]
    assert entry["status"] == "pending" and "offline" in entry["detail"]
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()    # retried on a later boot


@needs_bash
def test_browser_firstboot_installs_once_the_connection_is_up(tmp_path: Path) -> None:
    root, fakebin, canary = _sandbox(tmp_path)
    net = Net(tmp_path, 0)
    proc = _run_firstboot(root, fakebin, net, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert net.calls() == ["-q -t 90"] and canary.read_text(encoding="utf-8").strip() == "chrome"
    assert _state(root)["steps"]["browser"]["status"] == "done"
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@needs_bash
def test_browser_firstboot_without_the_helper_lets_install_browser_probe(tmp_path: Path) -> None:
    root, fakebin, canary = _sandbox(tmp_path, with_helper=False)
    net = Net(tmp_path, 1)                                              # would say offline - but nobody asks it
    proc = _run_firstboot(root, fakebin, net, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert net.calls() == [] and canary.exists()


@needs_bash
def test_browser_firstboot_does_not_wait_when_the_installer_already_did_it(tmp_path: Path) -> None:
    root, fakebin, canary = _sandbox(tmp_path)
    state = root / "var" / "lib" / "lindos" / "install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"schema": 1, "updated": "t", "online": True, "steps": {
        "browser": {"status": "done", "detail": "", "time": "t"}}}), encoding="utf-8")
    net = Net(tmp_path, 1)
    proc = _run_firstboot(root, fakebin, net, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert net.calls() == [] and not canary.exists()                    # terminal state: exit at once, no waiting
