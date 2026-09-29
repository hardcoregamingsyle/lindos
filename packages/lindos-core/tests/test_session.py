"""lindos.session + the shell helpers is-live-session / oem-config-pending, and the live-session
guards shipped with them (units, postinst wiring).  Hermetic: the kernel command line comes from
LINDOS_TEST_CMDLINE and system paths from LINDOS_ROOT, never from the host."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict

import pytest

from lindos import session

_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = _PKG_ROOT / "root"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SYSTEM_UNITS = ROOT / "usr" / "lib" / "systemd" / "system"
USER_UNITS = ROOT / "usr" / "lib" / "systemd" / "user"
DEBIAN = _PKG_ROOT / "DEBIAN"
BASH = shutil.which("bash")

LIVE_CASPER = "BOOT_IMAGE=/casper/vmlinuz boot=casper username=liveuser hostname=lindos quiet splash --"
LIVE_LIVE = "initrd=/live/initrd.img boot=live quiet"
INSTALLED = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=1234 ro quiet splash"


def _cmdline_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cmdline"
    path.write_bytes(text.encode("utf-8"))
    return path


# --- lindos.session ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    (LIVE_CASPER, True),
    (LIVE_LIVE, True),
    ("boot=casper", True),
    (INSTALLED, False),
    ("", False),
    ("xboot=casper", False),                 # exact words only
    ("boot=casper2", False),
    ("root=boot=casper", False),
    ("noboot=live", False),
    ("boot=live\\n", False),                # a literal backslash-n is not a word boundary
])
def test_is_live_session_reads_the_fake_cmdline_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     text: str, expected: bool) -> None:
    monkeypatch.setenv(session.CMDLINE_ENV, str(_cmdline_file(tmp_path, text)))
    assert session.is_live_session() is expected
    assert session.is_live_session(text) is expected      # the text form agrees with the file form


def test_is_live_session_handles_newline_crlf_and_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(session.CMDLINE_ENV, str(_cmdline_file(tmp_path, LIVE_CASPER + "\r\n")))
    assert session.is_live_session() is True
    monkeypatch.setenv(session.CMDLINE_ENV, str(tmp_path / "does-not-exist"))
    assert session.is_live_session() is False and session.read_cmdline() == ""


def test_cmdline_path_defaults_to_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(session.CMDLINE_ENV, raising=False)
    assert session.cmdline_path() == "/proc/cmdline"
    monkeypatch.setenv(session.CMDLINE_ENV, "/somewhere/else")
    assert session.cmdline_path() == "/somewhere/else"


def test_is_installer_chroot_is_the_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(session.INSTALLER_ENV, raising=False)
    assert session.is_installer_chroot() is False
    monkeypatch.setenv(session.INSTALLER_ENV, "1")
    assert session.is_installer_chroot() is True
    for other in ("0", "", "true", "yes"):
        monkeypatch.setenv(session.INSTALLER_ENV, other)
        assert session.is_installer_chroot() is False


def test_is_oem_temp_user(monkeypatch: pytest.MonkeyPatch) -> None:
    assert session.is_oem_temp_user("oem") is True
    assert session.is_oem_temp_user(" oem ") is True
    for name in ("", "liveuser", "OEM", "oem2", "root", "nitish"):
        assert session.is_oem_temp_user(name) is False, name
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "oem")
    assert session.is_oem_temp_user() is True
    monkeypatch.setattr(getpass, "getuser", lambda: "nitish")
    assert session.is_oem_temp_user() is False

    def boom() -> str:
        raise OSError("no login name")

    monkeypatch.setattr(getpass, "getuser", boom)
    assert session.is_oem_temp_user() is False


def test_session_module_needs_no_posix_only_imports_at_import_time() -> None:
    """Importable on Windows/macOS: pwd/getpass must only be touched lazily."""
    code = ("import sys; sys.modules['pwd'] = None; sys.modules['getpass'] = None; "
            "from lindos import session; print(session.is_live_session('boot=casper'), session.is_oem_temp_user('oem'))")
    lib = str(ROOT / "usr" / "lib" / "python3" / "dist-packages")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": lib})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True True"


# --- is-live-session (shell twin) -----------------------------------------------------------------
def _run(script: Path, env_extra: Dict[str, str], *args: str) -> subprocess.CompletedProcess:
    assert BASH is not None
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run([BASH, str(script), *args], capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("text, live", [
    (LIVE_CASPER, True), (LIVE_LIVE, True), (LIVE_CASPER + "\r\n", True), ("boot=casper", True),
    (INSTALLED, False), ("", False), ("xboot=casper", False), ("boot=casper2", False),
])
def test_is_live_session_script_agrees_with_the_python_module(tmp_path: Path, text: str, live: bool) -> None:
    proc = _run(LIBEXEC / "is-live-session", {session.CMDLINE_ENV: str(_cmdline_file(tmp_path, text))})
    assert proc.returncode == (0 if live else 1), proc.stderr
    assert proc.stdout == "" and proc.stderr == ""            # silent: the exit status is the answer
    assert session.is_live_session(text) is live


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_is_live_session_script_missing_file_is_not_live(tmp_path: Path) -> None:
    proc = _run(LIBEXEC / "is-live-session", {session.CMDLINE_ENV: str(tmp_path / "nope")})
    assert proc.returncode == 1


def _msys(value: str) -> str:
    """'C:\\x' -> '/c/x' for paths that Git Bash does not translate inside a script body."""
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _smoke_liveness_function() -> str:
    """The real check_live_session_helper() body, lifted out of the QA smoke test (never re-typed)."""
    script = (LIBEXEC / "qa" / "ci-boot-smoke-test.sh").read_text(encoding="utf-8")
    match = re.search(r"^check_live_session_helper\(\) \{\n(.*?\n)^\}\n", script, re.M | re.S)
    assert match, "check_live_session_helper() missing from the smoke test"
    return match.group(1)


def _run_smoke_liveness(tmp_path: Path, cmdline: str, helper: Path) -> subprocess.CompletedProcess:
    body = _smoke_liveness_function().replace("/proc/cmdline", cmdline)
    body = body.replace("/usr/libexec/lindos/is-live-session", _msys(str(helper)))
    harness = tmp_path / "harness.sh"
    harness.write_text("#!/bin/bash\ncheck_live_session_helper() {\n" + body
                       + "}\ncheck_live_session_helper\necho RC=$?\n", encoding="utf-8", newline="\n")
    return _run(harness, {session.CMDLINE_ENV: cmdline})


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("text", [LIVE_CASPER, LIVE_LIVE, INSTALLED, "xboot=casper", ""])
def test_smoke_test_liveness_check_agrees_with_the_kernel_command_line(tmp_path: Path, text: str) -> None:
    """The QA smoke test compares is-live-session with the kernel command line, so the check passes on a
    live USB boot AND on an installed disk (it fails only when the helper is wrong)."""
    cmdline = _msys(str(_cmdline_file(tmp_path, text)))
    proc = _run_smoke_liveness(tmp_path, cmdline, LIBEXEC / "is-live-session")
    rc = 0 if session.is_live_session(text) else 1
    assert "RC=0" in proc.stdout, (proc.stdout, proc.stderr)
    assert f"live_session_helper_rc={rc} expected={rc}" in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_smoke_test_liveness_check_fails_when_the_helper_disagrees(tmp_path: Path) -> None:
    cmdline = _msys(str(_cmdline_file(tmp_path, LIVE_CASPER)))
    liar = tmp_path / "liar.sh"
    liar.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8", newline="\n")      # says "installed" on a live boot
    proc = _run_smoke_liveness(tmp_path, cmdline, liar)
    assert "RC=1" in proc.stdout, (proc.stdout, proc.stderr)


def test_is_live_session_script_shape() -> None:
    text = (LIBEXEC / "is-live-session").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r" not in text
    assert "set -Eeuo pipefail" in text
    assert "LINDOS_TEST_CMDLINE" in text and "boot=casper" in text and "boot=live" in text
    assert "sudo " not in text


# --- oem-config-pending ---------------------------------------------------------------------------
@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_oem_config_pending_follows_the_wizard_units(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    script = LIBEXEC / "oem-config-pending"
    env = {"LINDOS_ROOT": str(root)}
    assert _run(script, env).returncode == 1                       # nothing armed: not pending
    for where in ("lib/systemd/system", "usr/lib/systemd/system", "etc/systemd/system"):
        unit = root / where / "oem-config.target"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\nDescription=oem-config\n", encoding="utf-8")
        assert _run(script, env).returncode == 0, where          # armed
        unit.unlink()
        assert _run(script, env).returncode == 1, where          # oem-config-firstboot removed it: done
    # the package's own copy under /usr/lib/oem-config/ does NOT count (it stays after the wizard)
    packaged = root / "usr" / "lib" / "oem-config" / "oem-config.target"
    packaged.parent.mkdir(parents=True)
    packaged.write_text("[Unit]\n", encoding="utf-8")
    assert _run(script, env).returncode == 1


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_oem_config_pending_sees_default_target_pointing_at_the_wizard(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    link = root / "etc" / "systemd" / "system" / "default.target"
    try:
        os.symlink("/lib/systemd/system/oem-config.target", link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("cannot create symlinks on this host")
    script = LIBEXEC / "oem-config-pending"
    assert _run(script, {"LINDOS_ROOT": str(root)}).returncode == 0
    link.unlink()
    os.symlink("/lib/systemd/system/graphical.target", link)
    assert _run(script, {"LINDOS_ROOT": str(root)}).returncode == 1


def test_oem_config_pending_script_shape() -> None:
    text = (LIBEXEC / "oem-config-pending").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r" not in text and "set -Eeuo pipefail" in text
    assert "oem-config.target" in text and "default.target" in text and "LINDOS_ROOT" in text


# --- units: never in the live session ---------------------------------------------------------------
def _lines(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    return [ln.strip() for ln in text.splitlines()]


@pytest.mark.parametrize("unit", [
    USER_UNITS / "lindos-update-notify.service",
    USER_UNITS / "lindos-update-notify.timer",
    SYSTEM_UNITS / "lindos-browser-firstboot.service",
])
def test_units_that_make_no_sense_in_the_live_session_refuse_to_start_there(unit: Path) -> None:
    lines = _lines(unit)
    assert "ConditionKernelCommandLine=!boot=casper" in lines
    assert "ConditionKernelCommandLine=!boot=live" in lines


def test_first_boot_units_wait_for_the_oem_config_wizard() -> None:
    lines = _lines(SYSTEM_UNITS / "lindos-browser-firstboot.service")
    assert "ConditionPathExists=!/lib/systemd/system/oem-config.target" in lines


def test_live_inhibit_unit_only_runs_in_the_live_session() -> None:
    lines = _lines(SYSTEM_UNITS / "lindos-live-inhibit.service")
    # positive, triggering conditions: an installed system never starts it
    assert "ConditionKernelCommandLine=|boot=casper" in lines and "ConditionKernelCommandLine=|boot=live" in lines
    assert not any(ln.startswith("ConditionKernelCommandLine=!") for ln in lines)
    assert "ConditionVirtualization=!container" in lines
    exec_start = next(ln for ln in lines if ln.startswith("ExecStart="))
    assert "/usr/bin/systemd-inhibit" in exec_start and "--mode=block" in exec_start
    for what in ("sleep", "idle", "handle-lid-switch", "handle-suspend-key"):
        assert what in exec_start, what
    assert exec_start.endswith("/usr/bin/sleep infinity")
    assert "Type=simple" in lines and "WantedBy=multi-user.target" in lines


def test_postinst_wires_the_live_inhibit_unit_and_the_new_helpers() -> None:
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "lindos-live-inhibit.service" in postinst and "multi-user.target.wants" in postinst
    assert "is-live-session" in postinst and "oem-config-pending" in postinst
    assert "/var/lib/lindos" in postinst
    postrm = (DEBIAN / "postrm").read_text(encoding="utf-8")
    assert "lindos-live-inhibit.service" in postrm       # the symlink postinst made is removed again


def test_shared_helper_scripts_exist_under_the_names_other_packages_call() -> None:
    for name in ("is-live-session", "oem-config-pending"):
        assert (LIBEXEC / name).is_file(), name
