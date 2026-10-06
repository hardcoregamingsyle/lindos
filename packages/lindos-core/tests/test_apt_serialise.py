"""The apt discipline of the first-boot jobs: apt-serialise, install-browser.sh, and the ordering of the two
first-boot retry units.

Review finding: the Chrome retry and the driver retry are both queued the moment oem-config ends, both end up
in apt, and apt does not queue - the loser failed at once ("Could not get lock /var/lib/dpkg/lock-frontend"),
a lost driver attempt even counted against its three tries.  Hermetic: every system tool is a fake on PATH,
system paths come from LINDOS_ROOT, flock(1) is replaced by a fake through LINDOS_FLOCK (the real one is
only used, on Linux, by the two tests that prove the queueing itself).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = _PKG_ROOT / "root"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
DEBIAN = _PKG_ROOT / "DEBIAN"
SERIALISE = LIBEXEC / "apt-serialise"
INSTALL_BROWSER = LIBEXEC / "install-browser.sh"
BROWSER_UNIT = ROOT / "usr" / "lib" / "systemd" / "system" / "lindos-browser-firstboot.service"
DRIVER_UNIT = _PKG_ROOT.parent / "lindos-gaming" / "root" / "usr" / "lib" / "systemd" / "system" / "lindos-driver-firstboot.service"
DRIVER_SCRIPT = _PKG_ROOT.parent / "lindos-gaming" / "root" / "usr" / "libexec" / "lindos" / "driver-firstboot.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
needs_flock = pytest.mark.skipif(BASH is None or shutil.which("flock") is None,
                                 reason="the real flock(1) needs Linux (util-linux)")


def _posix(path: Path) -> str:
    return path.as_posix()


def _fake(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


#: stands in for flock(1): records its arguments, honours -o/-w/-E, and either runs the command (the lock was
#: free) or exits with the -E status (still held after the wait)
FAKE_FLOCK = r'''
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

#: the "apt job": says what it was started with, then exits with $PROBE_RC
PROBE = r'''
{
    printf 'args=%s\n' "$*"
    printf 'serialised=%s\n' "${LINDOS_APT_SERIALISED:-}"
    printf 'apt_config=%s\n' "${APT_CONFIG:-}"
    if [ -n "${APT_CONFIG:-}" ] && [ -f "${APT_CONFIG}" ]; then
        printf 'apt_config_text=%s\n' "$(tr '\n' ' ' <"${APT_CONFIG}")"
    fi
    printf 'apt_conf=%s\n' "${LINDOS_APT_CONF:-}"
    if [ -n "${LINDOS_APT_CONF:-}" ] && [ -f "${LINDOS_APT_CONF}" ]; then
        printf 'apt_conf_text=%s\n' "$(tr '\n' ' ' <"${LINDOS_APT_CONF}")"
    fi
} >>"${PROBE_LOG}"
exit "${PROBE_RC:-0}"
'''


class Sandbox:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        self.root.mkdir()
        self.bin = tmp / "fakebin"
        self.bin.mkdir()
        self.flock_log = tmp / "flock.log"
        self.probe_log = tmp / "probe.log"
        self.calls = tmp / "calls.log"
        self.state = tmp / "state"
        self.state.mkdir()
        self.flock = _fake(self.bin, "flock-fake", FAKE_FLOCK)
        self.probe = _fake(self.bin, "probe.sh", PROBE)

    def env(self, **extra: str) -> Dict[str, str]:
        env = dict(os.environ)
        for name in ("APT_CONFIG", "LINDOS_APT_CONF", "LINDOS_APT_SERIALISED", "LINDOS_FLOCK", "FAKE_FLOCK_BUSY", "LINDOS_APT_LOCK_WAIT",
                     "LINDOS_APT_DPKG_WAIT", "LINDOS_APT_LOCK_TIMEOUT", "LINDOS_APT_UPDATE_TRIES",
                     "LINDOS_APT_UPDATE_RETRY_DELAY", "LINDOS_ROOT", "LINDOS_INSTALLER"):
            env.pop(name, None)
        env.update({"LINDOS_ROOT": _posix(self.root), "LINDOS_FLOCK": _posix(self.flock),
                    "FAKE_FLOCK_LOG": _posix(self.flock_log), "PROBE_LOG": _posix(self.probe_log),
                    "FAKE_CALLS": _posix(self.calls), "FAKE_STATE": _posix(self.state),
                    "PATH": str(self.bin) + os.pathsep + env.get("PATH", "")})
        env.update(extra)
        return env

    def serialise(self, *args: str, **extra: str) -> "subprocess.CompletedProcess[str]":
        assert BASH is not None
        return subprocess.run([BASH, str(SERIALISE), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=self.env(**extra), stdin=subprocess.DEVNULL)

    def flock_calls(self) -> List[str]:
        return self.flock_log.read_text(encoding="utf-8").splitlines() if self.flock_log.exists() else []

    def probe_lines(self) -> List[str]:
        return self.probe_log.read_text(encoding="utf-8").splitlines() if self.probe_log.exists() else []

    def apt_calls(self) -> List[str]:
        lines = self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []
        return [ln for ln in lines if ln.startswith("apt-get ")]


@pytest.fixture()
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# --- apt-serialise: shape ------------------------------------------------------------------------------------
def test_apt_serialise_is_a_shipped_lf_bash_script_with_the_house_style() -> None:
    raw = SERIALISE.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw and b"set -Eeuo pipefail" in raw
    text = raw.decode("utf-8")
    for needle in ("flock", "DPkg::Lock::Timeout", "APT_CONFIG", "LINDOS_APT_CONF", "--apt-config", "LINDOS_APT_SERIALISED",
                   "LINDOS_ROOT", "/run/lindos"):
        assert needle in text, needle
    assert "sudo " not in text


@needs_bash
def test_apt_serialise_syntax_and_usage() -> None:
    assert subprocess.run([BASH, "-n", str(SERIALISE)], capture_output=True).returncode == 0
    for args in ([], ["--wait"], ["--wait", "5"], ["--bogus", "true"]):
        proc = subprocess.run([BASH, str(SERIALISE), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert proc.returncode == 2 and "usage:" in proc.stderr, args


def test_postinst_and_control_know_the_helper() -> None:
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "/usr/libexec/lindos/apt-serialise" in postinst and 'chmod 0755 "$APT_SERIALISE"' in postinst
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    assert "apt-serialise" in control and "sync-default" in control


# --- apt-serialise: behaviour --------------------------------------------------------------------------------
@needs_bash
def test_the_command_takes_its_turn_and_apt_gets_a_dpkg_lock_wait(sb: Sandbox) -> None:
    proc = sb.serialise("--", _posix(sb.probe), "one", "two")
    assert proc.returncode == 0, proc.stderr
    # queued: one flock on <root>/run/lindos/apt.lock, close-on-exec (-o), 180 s, a distinct "gave up" status
    calls = sb.flock_calls()
    assert len(calls) == 1
    lock = _posix(sb.root / "run" / "lindos" / "apt.lock")
    assert calls[0].startswith("-o -w 180 -E 199 " + lock + " "), calls
    assert calls[0].endswith("probe.sh one two")
    # the lock wait is a one-line configuration file whose path is LINDOS_APT_CONF (the command passes it as '-c FILE') ...
    probe = "\n".join(sb.probe_lines())
    assert "args=one two" in probe and "serialised=1" in probe
    assert 'apt_conf_text=// Written by lindos apt-serialise for one command; removed afterwards. DPkg::Lock::Timeout "300";' in probe
    # ... and NOT the APT_CONFIG environment variable: dpkg's maintainer scripts would inherit it (see below)
    assert "apt_config=" in sb.probe_lines() and "apt_config_text" not in probe
    # the one-line config does not outlive the command
    assert not list((sb.root / "run" / "lindos").glob("apt-config.*"))


@needs_bash
def test_apt_config_is_not_exported_by_default_because_every_maintainer_script_would_inherit_it(sb: Sandbox) -> None:
    """Review finding: google-chrome-stable's postinst assigns APT_CONFIG=/usr/bin/apt-config (a shell variable it runs
    later).  With APT_CONFIG already exported that assignment changes the exported value and apt-config reads its own
    binary as a configuration file ('E: Syntax error /usr/bin/apt-config:13').  The installer hook avoids it with '-c FILE';
    on the installed system the Chrome retry and 'lindos-browser install chrome' run under apt-serialise."""
    postinst = _fake(sb.bin, "postinst-in-miniature", 'APT_CONFIG="$1"\n"${APT_CONFIG}" dump\n')
    fake_apt_config = _fake(sb.bin, "fake-apt-config",
                            'if [ -n "${APT_CONFIG:-}" ]; then\n'
                            '    echo "E: Syntax error ${APT_CONFIG}:13: Extra junk after value" >&2\n'
                            '    exit 100\n'
                            'fi\n'
                            'echo "Dir \\"/\\";"\n')
    ok = sb.serialise("--", _posix(postinst), _posix(fake_apt_config))
    assert ok.returncode == 0, ok.stderr
    assert "Syntax error" not in ok.stderr
    # the same helper with the opt-in flag documents the hazard it is for: never use --apt-config for an install of Chrome
    bad = sb.serialise("--apt-config", "--", _posix(postinst), _posix(fake_apt_config))
    assert bad.returncode == 100 and "Syntax error" in bad.stderr


@needs_bash
def test_apt_config_is_exported_on_request_for_commands_that_start_apt_through_tools_it_cannot_give_options_to(sb: Sandbox) -> None:
    """lindos-drivers / ubuntu-drivers (the driver retry) start apt-get themselves: they get APT_CONFIG, and only they."""
    proc = sb.serialise("--apt-config", "--", _posix(sb.probe), "drivers")
    assert proc.returncode == 0, proc.stderr
    probe = sb.probe_lines()
    conf = next(ln for ln in probe if ln.startswith("apt_conf=")).split("=", 1)[1]
    assert conf and f"apt_config={conf}" in probe, "APT_CONFIG is the same file as LINDOS_APT_CONF"
    assert 'apt_config_text=// Written by lindos apt-serialise for one command; removed afterwards. DPkg::Lock::Timeout "300";' in "\n".join(probe)
    assert not list((sb.root / "run" / "lindos").glob("apt-config.*")), "removed afterwards, also then"


@needs_bash
def test_a_callers_own_apt_config_wins_over_the_flag(sb: Sandbox) -> None:
    mine = sb.tmp / "mine.conf"
    mine.write_text('DPkg::Lock::Timeout "5";\n', encoding="utf-8")
    proc = sb.serialise("--apt-config", "--", _posix(sb.probe), APT_CONFIG=_posix(mine))
    assert proc.returncode == 0, proc.stderr
    assert f"apt_config={_posix(mine)}" in sb.probe_lines()


@needs_bash
def test_no_dpkg_wait_means_no_configuration_at_all(sb: Sandbox) -> None:
    proc = sb.serialise("--dpkg-wait", "0", "--apt-config", "--", _posix(sb.probe), LINDOS_APT_CONF="/stale/earlier.conf")
    assert proc.returncode == 0, proc.stderr
    assert "apt_conf=" in sb.probe_lines() and "apt_config=" in sb.probe_lines(), "a stale path from an earlier helper is dropped"


@needs_bash
def test_the_help_text_names_the_flag(sb: Sandbox) -> None:
    proc = subprocess.run([BASH, str(SERIALISE), "--bogus"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 2 and "[--apt-config]" in proc.stderr
    assert subprocess.run([BASH, str(SERIALISE), "--apt-config"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).returncode == 2, "the flag alone is no command"


@needs_bash
def test_the_wait_settings_are_tunable(sb: Sandbox) -> None:
    proc = sb.serialise("--wait", "7", "--dpkg-wait", "42", "--", _posix(sb.probe))
    assert proc.returncode == 0, proc.stderr
    assert " -w 7 " in sb.flock_calls()[0]
    assert 'DPkg::Lock::Timeout "42";' in "\n".join(sb.probe_lines())
    sb.flock_log.unlink()
    proc = sb.serialise("--", _posix(sb.probe), LINDOS_APT_LOCK_WAIT="9", LINDOS_APT_DPKG_WAIT="11")
    assert " -w 9 " in sb.flock_calls()[0] and 'DPkg::Lock::Timeout "11";' in "\n".join(sb.probe_lines())
    sb.flock_log.unlink()
    sb.serialise("--", _posix(sb.probe), LINDOS_APT_LOCK_WAIT="lots", LINDOS_APT_DPKG_WAIT="")
    assert " -w 180 " in sb.flock_calls()[0], "a junk wait falls back to the default"


@needs_bash
def test_the_commands_exit_status_is_passed_on(sb: Sandbox) -> None:
    assert sb.serialise("--", _posix(sb.probe), PROBE_RC="7").returncode == 7
    assert sb.serialise("--", _posix(sb.probe), PROBE_RC="0").returncode == 0
    assert sb.serialise("--", "definitely-not-a-command-lindos").returncode != 0


@needs_bash
def test_waiting_forever_is_not_an_option_the_command_runs_anyway_when_the_turn_does_not_come(sb: Sandbox) -> None:
    proc = sb.serialise("--wait", "1", "--", _posix(sb.probe), "late", FAKE_FLOCK_BUSY="1")
    assert proc.returncode == 0, proc.stderr
    assert "still holds" in proc.stderr and "running anyway" in proc.stderr
    assert sb.probe_lines().count("args=late") == 1, "run exactly once, not once per attempt"
    # the lock-timeout config is still in force for the late run
    assert 'DPkg::Lock::Timeout "300";' in "\n".join(sb.probe_lines())


@needs_bash
def test_a_callers_apt_config_is_kept(sb: Sandbox) -> None:
    """The installer's own apt.conf (already with DPkg::Lock::Timeout) must not be replaced."""
    mine = sb.tmp / "installer-apt.conf"
    mine.write_text('DPkg::Lock::Timeout "120";\n', encoding="utf-8")
    proc = sb.serialise("--", _posix(sb.probe), APT_CONFIG=_posix(mine))
    assert proc.returncode == 0, proc.stderr
    assert f"apt_config={_posix(mine)}" in sb.probe_lines()
    assert not list((sb.root / "run" / "lindos").glob("apt-config.*"))


@needs_bash
def test_nested_calls_never_wait_for_their_own_parents_lock(sb: Sandbox) -> None:
    proc = sb.serialise("--", _posix(sb.probe), "inner", LINDOS_APT_SERIALISED="1")
    assert proc.returncode == 0, proc.stderr
    assert sb.flock_calls() == [], "a wrapped script calling another wrapped script would deadlock on its own lock"
    assert "args=inner" in sb.probe_lines()


@needs_bash
def test_without_flock_the_job_still_runs(sb: Sandbox) -> None:
    proc = sb.serialise("--", _posix(sb.probe), "alone", LINDOS_FLOCK=_posix(sb.tmp / "no-such-flock"))
    assert proc.returncode == 0, proc.stderr
    assert "args=alone" in sb.probe_lines() and sb.flock_calls() == []
    assert 'DPkg::Lock::Timeout "300";' in "\n".join(sb.probe_lines()), "the lock wait does not depend on flock"


@needs_bash
def test_a_zero_wait_does_not_queue(sb: Sandbox) -> None:
    assert sb.serialise("--wait", "0", "--", _posix(sb.probe)).returncode == 0
    assert sb.flock_calls() == [] and sb.probe_lines()


@needs_flock
def test_two_jobs_really_take_turns_with_the_real_flock(sb: Sandbox) -> None:
    log = sb.tmp / "turns.log"
    job = _fake(sb.bin, "job.sh", f'echo "start $1" >>"{_posix(log)}"\nsleep 1\necho "end $1" >>"{_posix(log)}"\n')
    env = sb.env()
    env.pop("LINDOS_FLOCK")            # the real flock(1)
    first = subprocess.Popen([BASH, str(SERIALISE), "--", _posix(job), "A"], env=env, stdin=subprocess.DEVNULL)
    time.sleep(0.4)
    second = subprocess.Popen([BASH, str(SERIALISE), "--", _posix(job), "B"], env=env, stdin=subprocess.DEVNULL)
    assert first.wait(60) == 0 and second.wait(60) == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and lines[0].startswith("start ") and lines[2].startswith("start ")
    assert lines[1] == "end " + lines[0].split()[1] and lines[3] == "end " + lines[2].split()[1], lines


@needs_flock
def test_a_job_that_waited_too_long_runs_anyway_with_the_real_flock(sb: Sandbox) -> None:
    log = sb.tmp / "late.log"
    holder = _fake(sb.bin, "holder.sh", f'echo "holder-start" >>"{_posix(log)}"\nsleep 6\necho "holder-end" >>"{_posix(log)}"\n')
    quick = _fake(sb.bin, "quick.sh", f'echo "quick" >>"{_posix(log)}"\n')
    env = sb.env()
    env.pop("LINDOS_FLOCK")
    first = subprocess.Popen([BASH, str(SERIALISE), "--", _posix(holder)], env=env, stdin=subprocess.DEVNULL)
    time.sleep(1.2)                     # the holder has started and holds the lock
    started = time.monotonic()
    second = subprocess.run([BASH, str(SERIALISE), "--wait", "1", "--", _posix(quick)], env=env, capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=60)
    waited = time.monotonic() - started
    assert first.wait(60) == 0
    assert second.returncode == 0 and "running anyway" in second.stderr, second.stderr
    assert 0.8 <= waited < 4.5, waited
    assert log.read_text(encoding="utf-8").splitlines() == ["holder-start", "quick", "holder-end"]


# --- install-browser.sh -----------------------------------------------------------------------------------------
def _browser_tools(sb: Sandbox) -> None:
    """The system tools a real (non-dry) run of install-browser.sh touches, as fakes on PATH."""
    _fake(sb.bin, "id", '[ "$1" = "-u" ] && echo 0 || echo root\n')
    _fake(sb.bin, "dpkg-query", "exit 1\n")                       # nothing is installed yet
    _fake(sb.bin, "curl", 'out=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
                          '[ -n "${out}" ] && echo KEY >"${out}"\nexit "${FAKE_CURL_RC:-0}"\n')
    _fake(sb.bin, "apt-get",
          'printf "apt-get %s\\n" "$*" >>"${FAKE_CALLS}"\n'
          'printf "apt-config: %s\\n" "$( [ -f "${APT_CONFIG:-/nonexistent}" ] && tr "\\n" " " <"${APT_CONFIG}")" >>"${FAKE_CALLS}"\n'
          'printf "env-apt-config: %s\\n" "${APT_CONFIG:-}" >>"${FAKE_CALLS}"\n'
          'printf "serialised: %s\\n" "${LINDOS_APT_SERIALISED:-}" >>"${FAKE_CALLS}"\n'
          'case " $* " in\n'
          '  *" update "*)\n'
          '    n="$(cat "${FAKE_STATE}/updates" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "${n}" >"${FAKE_STATE}/updates"\n'
          '    if [ "${n}" -le "${FAKE_APT_UPDATE_FAILS:-0}" ]; then\n'
          '        echo "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 42 (apt-get)" >&2\n'
          '        exit 100\n'
          '    fi ;;\n'
          'esac\n'
          'exit 0\n')


def _run_browser(sb: Sandbox, *args: str, **extra: str) -> "subprocess.CompletedProcess[str]":
    assert BASH is not None
    return subprocess.run([BASH, str(INSTALL_BROWSER), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=sb.env(LINDOS_APT_UPDATE_RETRY_DELAY="0", **extra),
                          stdin=subprocess.DEVNULL)


def _apt_lines(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if re.search(r"(?:would run|run): apt-get ", ln)]


@needs_bash
@pytest.mark.parametrize("args", [
    ("chrome", "--dry-run"),
    ("edge", "--dry-run"),
    ("firefox", "--dry-run"),
    ("chrome", "--repo-only", "--dry-run"),
])
def test_every_apt_get_of_install_browser_waits_for_the_dpkg_lock(sb: Sandbox, args: tuple) -> None:
    proc = _run_browser(sb, *args)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = _apt_lines(proc.stdout)
    assert lines, proc.stdout
    for line in lines:
        assert "-o DPkg::Lock::Timeout=300" in line, line
        assert re.search(r"apt-get (update|install) ", line), line       # the verb stays right after apt-get (tools grep for it)


@needs_bash
def test_the_installer_hook_keeps_its_own_lock_wait_and_does_not_queue(sb: Sandbox) -> None:
    proc = _run_browser(sb, "chrome", "--in-installer", "--dry-run")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = _apt_lines(proc.stdout)
    assert len(lines) == 2 and all("-o DPkg::Lock::Timeout=120" in ln for ln in lines), lines   # = the installer's apt.conf
    assert not (sb.root / "run").exists()


@needs_bash
def test_a_dry_run_touches_no_lock(sb: Sandbox) -> None:
    assert _run_browser(sb, "chrome", "--dry-run").returncode == 0
    assert not (sb.root / "run").exists() and sb.flock_calls() == []


@needs_bash
def test_a_real_run_queues_behind_other_apt_jobs_and_every_apt_get_sees_the_lock_wait(sb: Sandbox) -> None:
    _browser_tools(sb)
    proc = _run_browser(sb, "chrome")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = sb.flock_calls()
    assert len(calls) == 1 and "install-browser.sh chrome" in calls[0]
    assert _posix(sb.root / "run" / "lindos" / "apt.lock") in calls[0]
    apt = sb.apt_calls()
    assert any(" update " in c for c in apt) and any(" install " in c and "google-chrome-stable" in c for c in apt), apt
    for line in apt:
        assert "-o DPkg::Lock::Timeout=300" in line, line
    text = sb.calls.read_text(encoding="utf-8")
    # the lock wait reaches apt as the option above - NOT as an APT_CONFIG that dpkg's maintainer scripts (Google Chrome's
    # postinst assigns its own APT_CONFIG and then fails with 'Syntax error /usr/bin/apt-config:13') would inherit
    assert "env-apt-config: \n" in text and not re.search(r"^env-apt-config: .+$", text, re.M), text
    assert "apt-config: // Written by lindos apt-serialise" not in text
    assert "serialised: 1" in text
    assert not list((sb.root / "run" / "lindos").glob("apt-config.*"))


@needs_bash
def test_a_nested_run_does_not_queue_a_second_time(sb: Sandbox) -> None:
    """browser-firstboot.sh -> install-browser.sh under an outer apt-serialise must not deadlock on its own lock."""
    _browser_tools(sb)
    proc = _run_browser(sb, "chrome", LINDOS_APT_SERIALISED="1")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert sb.flock_calls() == []
    assert sb.apt_calls()


@needs_bash
def test_apt_get_update_is_retried_while_another_apt_job_holds_the_lists_lock(sb: Sandbox) -> None:
    """DPkg::Lock::Timeout does not cover the lists lock: a held lock is not 'offline'."""
    _browser_tools(sb)
    proc = _run_browser(sb, "chrome", FAKE_APT_UPDATE_FAILS="2")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    apt = sb.apt_calls()
    assert sum(" update " in c for c in apt) == 3 and sum(" install " in c for c in apt) == 1, apt
    assert "attempt 1 of 3" in proc.stdout and "attempt 2 of 3" in proc.stdout


@needs_bash
def test_a_lists_lock_that_never_frees_up_ends_as_before_with_exit_3_and_no_install(sb: Sandbox) -> None:
    _browser_tools(sb)
    proc = _run_browser(sb, "chrome", FAKE_APT_UPDATE_FAILS="99")
    assert proc.returncode == 3, proc.stderr + proc.stdout          # 3 = "offline": browser-firstboot.sh records it as pending
    apt = sb.apt_calls()
    assert sum(" update " in c for c in apt) == 3 and not any(" install " in c for c in apt), apt


@needs_bash
def test_the_installer_hook_is_not_retried_and_not_queued(sb: Sandbox) -> None:
    _browser_tools(sb)
    proc = _run_browser(sb, "chrome", "--in-installer", "--download-only", FAKE_APT_UPDATE_FAILS="99")
    assert proc.returncode == 3, proc.stderr + proc.stdout
    assert sum(" update " in c for c in sb.apt_calls()) == 1, "one sequential hook with its own time boxes"
    assert sb.flock_calls() == []


@needs_bash
def test_install_browser_syntax() -> None:
    assert subprocess.run([BASH, "-n", str(INSTALL_BROWSER)], capture_output=True).returncode == 0


# --- the two retry units -----------------------------------------------------------------------------------------
def _unit_lines(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _unit_values(path: Path, key: str) -> List[str]:
    return [ln.split("=", 1)[1] for ln in _unit_lines(path) if ln.startswith(key + "=")]


def _unit_value(path: Path, key: str) -> str:
    values = _unit_values(path, key)
    assert len(values) == 1, (path, key, values)
    return values[0]


def test_the_driver_retry_is_ordered_after_the_chrome_retry() -> None:
    """Both units are queued in the same transaction when oem-config ends: exactly one of them goes first."""
    after = _unit_value(DRIVER_UNIT, "After").split()
    assert "lindos-browser-firstboot.service" in after and "network-online.target" in after
    # the other direction would be an ordering cycle: systemd would drop one of the two jobs
    for key in ("After", "Before"):
        for value in _unit_values(BROWSER_UNIT, key):
            assert "lindos-driver-firstboot.service" not in value.split(), (key, value)


def test_neither_unit_requires_the_other() -> None:
    """After= only orders: a failed or skipped Chrome retry must never stop the driver retry."""
    for path in (BROWSER_UNIT, DRIVER_UNIT):
        for line in _unit_lines(path):
            assert not re.match(r"(Requires|BindsTo|Requisite|PartOf)=.*lindos-(browser|driver)-firstboot", line), line


def _default(text: str, variable: str) -> int:
    match = re.search(variable + r'="\$\{[A-Z_]+:-(\d+)\}"', text)
    assert match, variable
    return int(match.group(1))


def test_the_unit_timeouts_leave_room_for_the_queue_the_lock_wait_and_the_work() -> None:
    serialise = SERIALISE.read_text(encoding="utf-8")
    queue, dpkg_wait = _default(serialise, "WAIT"), _default(serialise, "DPKG_WAIT")
    driver = DRIVER_SCRIPT.read_text(encoding="utf-8")
    lock_wait, retry = _default(driver, "LOCK_WAIT"), _default(driver, "RETRY_TIMEOUT")
    assert lock_wait <= queue, "the driver retry never queues longer than the helper's own default"
    assert int(_unit_value(DRIVER_UNIT, "TimeoutStartSec")) >= lock_wait + retry + 30 + 60      # 30 = timeout -k grace
    assert int(_unit_value(BROWSER_UNIT, "TimeoutStartSec")) >= queue + dpkg_wait + 120        # + the download itself


def test_the_driver_script_takes_its_turn_through_the_shared_helper() -> None:
    text = DRIVER_SCRIPT.read_text(encoding="utf-8")
    assert "apt-serialise" in text and '"${LIBEXEC}/apt-serialise"' in text
    # still no apt of its own: the tools it starts do the apt work
    for forbidden in ("apt-get install", "apt install", "apt-get -y"):
        assert forbidden not in text, forbidden


def test_scripts_stay_lf_only() -> None:
    for path in (SERIALISE, INSTALL_BROWSER, DRIVER_SCRIPT, BROWSER_UNIT, DRIVER_UNIT, LIBEXEC / "browser-firstboot.sh"):
        assert b"\r" not in path.read_bytes(), path
