"""The root side of the update system as files (SPEC-UPDATE.md §39): the refresh timer and service, the
boot-time repair unit, the ``update-refresh`` / ``update-repair`` / ``reboot-required-hook`` scripts and the
apt ``DPkg::Post-Invoke`` hook, and how the maintainer scripts wire them in.

Hermetic: bash scripts run against fake ``wait-for-network`` / ``apt-serialise`` / python / dpkg / apt-get,
paths come from LINDOS_ROOT, and no repo script is ever executed directly (Git on Windows keeps no exec bit):
they are run as ``bash <script>`` or through executable copies staged in a temp dir.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SYSTEMD = ROOT / "usr" / "lib" / "systemd" / "system"
PYLIB = ROOT / "usr" / "lib" / "python3" / "dist-packages"
DEBIAN = PKG / "DEBIAN"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

REFRESH = LIBEXEC / "update-refresh"
REPAIR = LIBEXEC / "update-repair"
HOOK = LIBEXEC / "reboot-required-hook"
APT_CONF = ROOT / "etc" / "apt" / "apt.conf.d" / "98lindos-reboot-required"
TIMER = SYSTEMD / "lindos-update-refresh.timer"
SERVICE = SYSTEMD / "lindos-update-refresh.service"
REPAIR_UNIT = SYSTEMD / "lindos-update-repair.service"


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _fake(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)
    return path


def _units(path: Path) -> Dict[str, Dict[str, List[str]]]:
    """A tiny systemd-unit reader: ``{section: {key: [values]}}`` (comments skipped)."""
    out: Dict[str, Dict[str, List[str]]] = {}
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        out.setdefault(section, {}).setdefault(key.strip(), []).append(value.strip())
    return out


# =================================================================================================
# files exist, are well formed, and are wired in
# =================================================================================================
@pytest.mark.parametrize("path", [REFRESH, REPAIR, HOOK, APT_CONF, TIMER, SERVICE, REPAIR_UNIT])
def test_files_are_shipped_with_lf_endings(path: Path) -> None:
    assert path.is_file(), path
    assert b"\r" not in path.read_bytes()


@pytest.mark.parametrize("path", [REFRESH, REPAIR, HOOK])
def test_scripts_follow_the_shell_rules(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text
    assert not re.search(r"^\s*sudo\b", text, re.M)


@needs_bash
@pytest.mark.parametrize("path", [REFRESH, REPAIR, HOOK])
def test_scripts_parse(path: Path) -> None:
    assert subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True).returncode == 0


def test_timer_runs_twice_a_day_randomised_persistent_and_after_boot() -> None:
    timer = _units(TIMER)
    t = timer["Timer"]
    assert t["OnBootSec"] == ["5min"]
    assert t["Persistent"] == ["true"] and t["RandomizedDelaySec"] and t["AccuracySec"]
    (calendar,) = t["OnCalendar"]
    hours = re.search(r"\*-\*-\*\s+([0-9,]+):", calendar)
    assert hours and len(hours.group(1).split(",")) == 2                # exactly two runs a day
    assert timer["Install"]["WantedBy"] == ["timers.target"]
    guards = timer["Unit"]["ConditionKernelCommandLine"]
    assert "!boot=casper" in guards and "!boot=live" in guards         # not in the live USB session


def test_service_only_refreshes_and_is_guarded() -> None:
    unit = _units(SERVICE)
    service = unit["Service"]
    assert service["Type"] == ["oneshot"]
    assert service["ExecStart"] == ["/usr/libexec/lindos/update-refresh"]
    assert "Install" not in unit                                        # started by its timer only
    conditions = unit["Unit"]["ConditionPathExists"]
    assert "!/var/lib/lindos/update-in-progress" in conditions          # not while an update runs / awaits repair
    assert "!/lib/systemd/system/oem-config.target" in conditions       # not before the account wizard is done
    guards = unit["Unit"]["ConditionKernelCommandLine"]
    assert "!boot=casper" in guards and "!boot=live" in guards
    assert service["Nice"] and service["IOSchedulingClass"] == ["idle"]
    text = SERVICE.read_text(encoding="utf-8")
    assert "never installs" in text.lower()


def test_repair_unit_only_runs_when_an_update_was_interrupted() -> None:
    unit = _units(REPAIR_UNIT)
    assert unit["Unit"]["ConditionPathExists"] == ["/var/lib/lindos/update-in-progress"]
    assert unit["Service"]["ExecStart"] == ["/usr/libexec/lindos/update-repair"]
    assert unit["Install"]["WantedBy"] == ["multi-user.target"]
    assert "!boot=casper" in unit["Unit"]["ConditionKernelCommandLine"]


def test_every_exec_target_is_shipped() -> None:
    for path in (SERVICE, REPAIR_UNIT):
        for exec_start in _units(path)["Service"]["ExecStart"]:
            assert (ROOT / exec_start.lstrip("/")).is_file(), exec_start


def test_the_refresh_script_uses_the_shipped_network_wait_and_serialiser() -> None:
    text = REFRESH.read_text(encoding="utf-8")
    assert "/usr/libexec/lindos/wait-for-network" in text and "/usr/libexec/lindos/apt-serialise" in text
    assert (LIBEXEC / "wait-for-network").is_file() and (LIBEXEC / "apt-serialise").is_file()
    assert "lindos.updatestate" in text and "update-in-progress" in text


def test_nothing_in_the_refresh_path_installs_anything() -> None:
    for path in (REFRESH, SERVICE, TIMER):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"apt(-get)?\s+(install|upgrade|dist-upgrade|full-upgrade)", text), path
    src = (PYLIB / "lindos" / "updatestate.py").read_text(encoding="utf-8")
    assert '"install"' not in src.split("def refresh_state")[1].split("def marker_path")[0]


def test_apt_hook_is_a_post_invoke_that_can_never_fail_apt() -> None:
    text = APT_CONF.read_text(encoding="utf-8")
    assert "DPkg::Post-Invoke" in text and "reboot-required-hook" in text
    command = re.search(r'DPkg::Post-Invoke\s*\{\s*"(.*)";\s*\};', text).group(1)
    assert command.rstrip().endswith("fi") and "|| true" in command
    assert "if [ -x /usr/libexec/lindos/reboot-required-hook ]" in command   # harmless when the package is gone


@needs_bash
def test_the_apt_hook_command_is_valid_shell_and_succeeds_without_the_script() -> None:
    text = APT_CONF.read_text(encoding="utf-8")
    command = re.search(r'DPkg::Post-Invoke\s*\{\s*"(.*)";\s*\};', text).group(1)
    proc = subprocess.run([BASH, "-c", command], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_postinst_enables_the_timer_and_the_repair_unit_and_makes_the_scripts_executable() -> None:
    post = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert 'ln -sf ../lindos-update-refresh.timer "$SYSTEM_TIMER_WANTS/lindos-update-refresh.timer"' in post
    assert 'ln -sf ../lindos-update-repair.service "$LIVE_INHIBIT_WANTS/lindos-update-repair.service"' in post
    assert 'SYSTEM_TIMER_WANTS="/usr/lib/systemd/system/timers.target.wants"' in post
    for name in ("update-refresh", "update-repair", "reboot-required-hook"):
        assert f"/usr/libexec/lindos/{name}" in post
        assert (LIBEXEC / name).is_file()
    assert "/usr/lib/systemd/system/lindos-update-refresh.timer" in post
    assert "systemctl" not in post.split("update system")[1].split("for cli in")[0]   # never starts anything from postinst


def test_postrm_removes_what_postinst_linked() -> None:
    post = (DEBIAN / "postrm").read_text(encoding="utf-8")
    assert "timers.target.wants/lindos-update-refresh.timer" in post
    assert "multi-user.target.wants/lindos-update-repair.service" in post
    assert "/var/lib/lindos/update-state.json" in post


def test_the_conffiles_did_not_change() -> None:
    assert (DEBIAN / "conffiles").read_text(encoding="utf-8").strip() == "/etc/lindos/system.json"


# =================================================================================================
# update-refresh
# =================================================================================================
class Refresh:
    def __init__(self, tmp: Path, *, network: int = 0) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        self.root.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        self.wfn = _fake(self.bin, "wait-for-network", f'printf "wfn %s\\n" "$*" >> "{_msys(str(self.log))}"\nexit {network}\n')
        self.serialise = _fake(self.bin, "apt-serialise", f'printf "serialise %s\\n" "$*" >> "{_msys(str(self.log))}"\nexit 0\n')

    def marker(self) -> Path:
        path = self.root / "var" / "lib" / "lindos" / "update-in-progress"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def calls(self) -> List[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({"LINDOS_ROOT": _msys(str(self.root)), "LINDOS_WAIT_FOR_NETWORK": _msys(str(self.wfn)),
                    "LINDOS_APT_SERIALISE": _msys(str(self.serialise)), "LINDOS_PYTHON": "fakepython"})
        env.pop("LINDOS_NETWORK_WAIT", None)
        if extra:
            env.update(extra)
        assert BASH is not None
        return subprocess.run([BASH, str(REFRESH)], capture_output=True, text=True, timeout=60, env=env)


@needs_bash
def test_refresh_waits_for_the_network_then_refreshes_through_the_serialiser(tmp_path: Path) -> None:
    r = Refresh(tmp_path)
    proc = r.run()
    assert proc.returncode == 0, proc.stderr
    assert r.calls() == ["wfn 90", "serialise -- fakepython -m lindos.updatestate refresh"]


@needs_bash
def test_refresh_offline_still_writes_the_state_but_says_so(tmp_path: Path) -> None:
    r = Refresh(tmp_path, network=1)
    proc = r.run()
    assert proc.returncode == 0, proc.stderr                            # offline is not a failure
    assert r.calls() == ["wfn 90", "serialise -- fakepython -m lindos.updatestate refresh --offline"]
    assert "no network" in proc.stdout


@needs_bash
def test_refresh_wait_is_bounded_and_configurable(tmp_path: Path) -> None:
    r = Refresh(tmp_path)
    r.run({"LINDOS_NETWORK_WAIT": "7"})
    assert r.calls()[0] == "wfn 7"


@needs_bash
def test_refresh_does_nothing_while_an_update_is_in_progress_or_needs_repair(tmp_path: Path) -> None:
    r = Refresh(tmp_path)
    r.marker().write_text("{}", encoding="utf-8")
    proc = r.run()
    assert proc.returncode == 0 and r.calls() == []
    assert "in progress" in proc.stdout


@needs_bash
def test_refresh_end_to_end_with_the_real_helpers(tmp_path: Path) -> None:
    """The real wait-for-network and apt-serialise scripts, a fake python: the whole chain, no queueing."""
    py_log = tmp_path / "py.log"
    fake_py = _fake(tmp_path, "fakepython", f'printf "%s\\n" "$*" >> "{_msys(str(py_log))}"\nexit 0\n')
    root = tmp_path / "root"
    root.mkdir()
    env = dict(os.environ)
    env.update({"LINDOS_ROOT": _msys(str(root)), "LINDOS_PYTHON": _msys(str(fake_py)),
                "LINDOS_WAIT_FOR_NETWORK": _msys(str(LIBEXEC / "wait-for-network")),
                "LINDOS_APT_SERIALISE": _msys(str(LIBEXEC / "apt-serialise")),
                "LINDOS_FORCE_OFFLINE": "1", "LINDOS_APT_LOCK_WAIT": "0", "LINDOS_APT_DPKG_WAIT": "0"})
    assert BASH is not None
    proc = subprocess.run([BASH, str(REFRESH)], capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert py_log.read_text(encoding="utf-8").strip() == "-m lindos.updatestate refresh --offline"


# =================================================================================================
# update-repair
# =================================================================================================
class Repair:
    def __init__(self, tmp: Path, *, dpkg_rc: int = 0, apt_rc: int = 0) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        self.root.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        log = _msys(str(self.log))
        _fake(self.bin, "dpkg", f'printf "dpkg %s\\n" "$*" >> "{log}"\nexit {dpkg_rc}\n')
        _fake(self.bin, "apt-get", f'printf "apt-get %s\\n" "$*" >> "{log}"\nexit {apt_rc}\n')
        self.marker = self.root / "var" / "lib" / "lindos" / "update-in-progress"

    def arm(self) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text('{"action": "apt-full-upgrade"}', encoding="utf-8")

    def calls(self) -> List[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = _msys(str(self.bin)) + os.pathsep + env.get("PATH", "")
        env.update({"LINDOS_ROOT": _msys(str(self.root)), "LINDOS_APT_LOCK_WAIT": "0", "LINDOS_APT_DPKG_WAIT": "0",
                    "LINDOS_APT_SERIALISE": _msys(str(LIBEXEC / "apt-serialise"))})
        assert BASH is not None
        return subprocess.run([BASH, str(REPAIR)], capture_output=True, text=True, timeout=60, env=env)


@needs_bash
def test_repair_without_a_marker_does_nothing(tmp_path: Path) -> None:
    r = Repair(tmp_path)
    assert r.run().returncode == 0 and r.calls() == []


@needs_bash
def test_repair_configures_and_fixes_then_clears_the_marker(tmp_path: Path) -> None:
    r = Repair(tmp_path)
    r.arm()
    proc = r.run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    calls = r.calls()
    assert calls[0].startswith("dpkg --configure -a") and "--force-confold" in calls[0]
    assert calls[1].startswith("apt-get -f install -y") and "Dpkg::Options::=--force-confold" in calls[1]
    assert not r.marker.exists()
    assert "repaired" in proc.stdout


@needs_bash
def test_repair_that_fails_keeps_the_marker_and_fails_the_unit(tmp_path: Path) -> None:
    r = Repair(tmp_path, dpkg_rc=1)
    r.arm()
    proc = r.run()
    assert proc.returncode == 1
    assert r.marker.exists()                                            # the next boot tries again
    assert len(r.calls()) == 2                                          # apt-get -f still ran after dpkg failed
    second = tmp_path / "second"
    second.mkdir()
    r2 = Repair(second, apt_rc=100)
    r2.arm()
    assert r2.run().returncode == 1 and r2.marker.exists()


def test_repair_installs_nothing_new_on_purpose() -> None:
    text = REPAIR.read_text(encoding="utf-8")
    assert "apt-get -f install" in text and "dist-upgrade" not in text and "apt-get install" not in text


# =================================================================================================
# reboot-required-hook
# =================================================================================================
@needs_bash
def test_the_hook_calls_the_reboot_hook_and_never_fails(tmp_path: Path) -> None:
    log = tmp_path / "py.log"
    good = _fake(tmp_path, "goodpython", f'printf "%s\\n" "$*" >> "{_msys(str(log))}"\nexit 0\n')
    bad = _fake(tmp_path, "badpython", "exit 3\n")
    for python in (good, bad):
        env = dict(os.environ, LINDOS_PYTHON=_msys(str(python)))
        proc = subprocess.run([BASH, str(HOOK)], capture_output=True, text=True, timeout=60, env=env)
        assert proc.returncode == 0                                     # apt must never see a failure from here
        assert proc.stdout == "" and proc.stderr == ""
    assert log.read_text(encoding="utf-8").strip() == "-m lindos.updatestate reboot-hook"
    missing = subprocess.run([BASH, str(HOOK)], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, LINDOS_PYTHON="no-such-python-anywhere"))
    assert missing.returncode == 0


@needs_bash
def test_the_hook_writes_reboot_required_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "proc").mkdir(parents=True)
    (root / "proc" / "stat").write_text("btime 0\n", encoding="utf-8")            # booted in 1970: every log line is newer
    (root / "var" / "log").mkdir(parents=True)
    (root / "var" / "log" / "dpkg.log").write_text(
        "2026-09-30 09:11:00 install linux-image-6.8.0-47-generic:amd64 <none> 6.8.0-47.47\n"
        "2026-09-30 09:12:00 upgrade coreutils:amd64 1 2\n", encoding="utf-8")
    env = dict(os.environ)
    env.update({"LINDOS_ROOT": _msys(str(root)), "LINDOS_PYTHON": _msys(sys.executable), "PYTHONPATH": str(PYLIB),
                "PYTHONDONTWRITEBYTECODE": "1"})
    proc = subprocess.run([BASH, str(HOOK)], capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    assert (root / "run" / "reboot-required").read_text(encoding="utf-8") == "*** System restart required ***\n"
    assert (root / "run" / "reboot-required.pkgs").read_text(encoding="utf-8") == "linux-image-6.8.0-47-generic\n"


# =================================================================================================
# systemd's own opinion (only where systemd-analyze exists, i.e. the Linux CI runner)
# =================================================================================================
SYSTEMD_ANALYZE = shutil.which("systemd-analyze")
_SYNTAX_PROBLEMS = re.compile(r"Unknown key|Unknown section|Unknown lvalue|Failed to parse|Invalid|Unknown assignment|"
                              r"Missing '='|Assignment outside of section", re.I)


@pytest.mark.skipif(SYSTEMD_ANALYZE is None, reason="systemd-analyze not available")
def test_systemd_accepts_the_calendar_expression() -> None:
    (calendar,) = _units(TIMER)["Timer"]["OnCalendar"]
    proc = subprocess.run([SYSTEMD_ANALYZE, "calendar", calendar], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Next elapse" in proc.stdout


@pytest.mark.skipif(SYSTEMD_ANALYZE is None, reason="systemd-analyze not available")
def test_systemd_finds_no_syntax_problem_in_the_units(tmp_path: Path) -> None:
    """Units are copied into a scratch root with stub executables; anything about a missing target or
    dependency is environment noise, but a misspelt key or a malformed line would show here."""
    unit_dir = tmp_path / "usr" / "lib" / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    names = [TIMER.name, SERVICE.name, REPAIR_UNIT.name]
    for path in (TIMER, SERVICE, REPAIR_UNIT):
        shutil.copy(path, unit_dir / path.name)
    libexec = tmp_path / "usr" / "libexec" / "lindos"
    libexec.mkdir(parents=True)
    for script in ("update-refresh", "update-repair"):
        stub = libexec / script
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(stub, 0o755)
    proc = subprocess.run([SYSTEMD_ANALYZE, f"--root={tmp_path}", "verify", *[str(unit_dir / n) for n in names]],
                          capture_output=True, text=True, timeout=120)
    output = proc.stdout + proc.stderr
    if "unrecognized option" in output.lower() or "unknown option" in output.lower():
        pytest.skip("this systemd-analyze has no --root")
    ours = [line for line in output.splitlines() if any(n in line for n in names) and _SYNTAX_PROBLEMS.search(line)]
    assert ours == [], output
