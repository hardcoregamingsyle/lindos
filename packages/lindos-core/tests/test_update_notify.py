"""lindos-update-notify.sh: the read-only update-available desktop toast (SPEC-UPDATE.md §37).

Never touches a real `lindos-update` or `notify-send`: both are fake PATH stubs driven by
environment variables, and HOME/XDG_STATE_HOME/XDG_RUNTIME_DIR all point into a scratch
``tmp_path`` -- nothing here ever reads or writes a real user's state. Skipped cleanly when bash
is unavailable (bare Windows CI, matching every other build/tests/test_*.py and this package's
own test_publish_apt_repo.py-style scripts).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                    # packages/lindos-core
SCRIPT = PKG_ROOT / "root" / "usr" / "libexec" / "lindos" / "lindos-update-notify.sh"
DESKTOP_FILE = (PKG_ROOT.parent / "lindos-desktop" / "root" / "etc" / "xdg" / "autostart"
                / "lindos-update-notify.desktop")
SERVICE_FILE = PKG_ROOT / "root" / "usr" / "lib" / "systemd" / "user" / "lindos-update-notify.service"
TIMER_FILE = PKG_ROOT / "root" / "usr" / "lib" / "systemd" / "user" / "lindos-update-notify.timer"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_LINDOS_UPDATE = """#!/bin/bash
printf '%s' "${FAKE_LINDOS_UPDATE_JSON:-null}"
exit "${FAKE_LINDOS_UPDATE_RC:-0}"
"""

FAKE_NOTIFY_SEND = """#!/bin/bash
printf 'CALLED %s\\n' "$*" >> "${FAKE_NOTIFY_LOG}"
exit 0
"""


def _write_fake_tool(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _msys_path(value: str) -> str:
    """Convert a native Windows path ('C:\\Users\\...') to the MSYS form ('/c/Users/...') Git
    Bash's own runtime expects for values it does not auto-translate (PATH/HOME are special-
    cased by MSYS itself; custom env vars like XDG_STATE_HOME are not) -- same helper as
    build/tests/test_publish_apt_repo.py, duplicated here to keep this test self-contained."""
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _fake_bin(tmp_path: Path, *, with_lindos_update: bool = True, with_notify_send: bool = True) -> Path:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    if with_lindos_update:
        _write_fake_tool(fake_bin, "lindos-update", FAKE_LINDOS_UPDATE)
    if with_notify_send:
        _write_fake_tool(fake_bin, "notify-send", FAKE_NOTIFY_SEND)
    return fake_bin


def _run(tmp_path: Path, *, json_out: Optional[str] = None, rc: int = 0, boot_id: str = "boot-aaa",
         with_lindos_update: bool = True, with_notify_send: bool = True,
         env_extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    assert BASH is not None
    fake_bin = _fake_bin(tmp_path, with_lindos_update=with_lindos_update, with_notify_send=with_notify_send)
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    notify_log = tmp_path / "notify.log"

    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["HOME"] = _msys_path(str(home))
    env["XDG_RUNTIME_DIR"] = ""          # force the durable XDG_STATE_HOME fallback, deterministically
    env["XDG_STATE_HOME"] = _msys_path(str(state))
    env["FAKE_NOTIFY_LOG"] = _msys_path(str(notify_log))
    env["LINDOS_BOOT_ID"] = boot_id
    env["LINDOS_PYTHON"] = sys.executable
    if json_out is not None:
        env["FAKE_LINDOS_UPDATE_JSON"] = json_out
    env["FAKE_LINDOS_UPDATE_RC"] = str(rc)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30, check=False, env=env)
    result.notify_log = notify_log          # type: ignore[attr-defined]
    result.state_dir = state / "lindos"     # type: ignore[attr-defined]
    return result


ONE_UPDATE = json.dumps({
    "lindos_updates": [{"name": "lindos-core", "installed": "1.0.0", "candidate": "1.0.1", "channel": "lindos"}],
})
TWO_UPDATES = json.dumps({
    "lindos_updates": [
        {"name": "lindos-core", "installed": "1.0.0", "candidate": "1.0.2", "channel": "lindos"},
        {"name": "lindos-tune", "installed": "1.0.0", "candidate": "1.0.1", "channel": "lindos"},
    ],
})
NO_UPDATES = json.dumps({"lindos_updates": []})


def _notify_lines(result: subprocess.CompletedProcess) -> list:
    log_path = result.notify_log       # type: ignore[attr-defined]
    if not log_path.exists():
        return []
    return [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --- static sanity --------------------------------------------------------------------------
def test_script_exists_and_executable_shebang() -> None:
    assert SCRIPT.is_file(), SCRIPT
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/bin/bash"


def test_bash_syntax_ok() -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30, check=False)
    assert res.returncode == 0, res.stderr


def test_no_crlf_and_set_euo() -> None:
    raw = SCRIPT.read_bytes()
    assert b"\r\n" not in raw
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in text
    for line in text.splitlines():
        assert not line.strip().startswith("sudo "), line
    assert "pkexec" not in text and "systemctl restart" not in text   # never elevates


def test_desktop_service_and_timer_reference_the_same_script_path() -> None:
    assert DESKTOP_FILE.is_file(), DESKTOP_FILE
    assert SERVICE_FILE.is_file(), SERVICE_FILE
    assert TIMER_FILE.is_file(), TIMER_FILE
    desktop_text = DESKTOP_FILE.read_text(encoding="utf-8")
    service_text = SERVICE_FILE.read_text(encoding="utf-8")
    assert "Exec=/usr/libexec/lindos/lindos-update-notify.sh" in desktop_text
    assert "ExecStart=/usr/libexec/lindos/lindos-update-notify.sh" in service_text
    assert "OnUnitActiveSec=6h" in TIMER_FILE.read_text(encoding="utf-8")
    assert "OnlyShowIn=XFCE;" in desktop_text
    assert "NoDisplay=true" in desktop_text


# --- behaviour -------------------------------------------------------------------------------
def test_updates_available_sends_one_notification_and_writes_marker(tmp_path: Path) -> None:
    res = _run(tmp_path, json_out=ONE_UPDATE, rc=0)
    assert res.returncode == 0, res.stderr
    lines = _notify_lines(res)
    assert len(lines) == 1
    assert "lindos-core" in lines[0]
    marker = res.state_dir / "update-notify-seen"          # type: ignore[attr-defined]
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == "boot-aaa|lindos-core=1.0.1"


def test_no_updates_never_notifies(tmp_path: Path) -> None:
    res = _run(tmp_path, json_out=NO_UPDATES, rc=3)
    assert res.returncode == 0, res.stderr
    assert _notify_lines(res) == []
    assert not (res.state_dir / "update-notify-seen").exists()   # type: ignore[attr-defined]


def test_error_exit_code_suppresses_notification(tmp_path: Path) -> None:
    res = _run(tmp_path, json_out="not valid json", rc=1)
    assert res.returncode == 0, res.stderr
    assert _notify_lines(res) == []


def test_missing_lindos_update_binary_is_quiet(tmp_path: Path) -> None:
    res = _run(tmp_path, json_out=ONE_UPDATE, rc=0, with_lindos_update=False)
    assert res.returncode == 0, res.stderr
    assert _notify_lines(res) == []


def test_missing_notify_send_is_quiet_and_writes_no_marker(tmp_path: Path) -> None:
    res = _run(tmp_path, json_out=ONE_UPDATE, rc=0, with_notify_send=False)
    assert res.returncode == 0, res.stderr
    assert not (res.state_dir / "update-notify-seen").exists()   # type: ignore[attr-defined]


def test_throttled_on_second_run_same_boot_and_same_updates(tmp_path: Path) -> None:
    first = _run(tmp_path, json_out=ONE_UPDATE, rc=0, boot_id="boot-aaa")
    assert len(_notify_lines(first)) == 1
    # re-run against the exact same HOME/XDG_STATE_HOME/notify log (same tmp_path) with the
    # same boot id and the same update set: must not notify again.
    second = _run(tmp_path, json_out=ONE_UPDATE, rc=0, boot_id="boot-aaa")
    assert second.returncode == 0, second.stderr
    assert len(_notify_lines(second)) == 1   # still just the one line from the first run


def test_new_boot_resets_the_throttle(tmp_path: Path) -> None:
    first = _run(tmp_path, json_out=ONE_UPDATE, rc=0, boot_id="boot-aaa")
    assert len(_notify_lines(first)) == 1
    second = _run(tmp_path, json_out=ONE_UPDATE, rc=0, boot_id="boot-bbb")
    assert len(_notify_lines(second)) == 2


def test_changed_update_set_resets_the_throttle(tmp_path: Path) -> None:
    first = _run(tmp_path, json_out=ONE_UPDATE, rc=0, boot_id="boot-aaa")
    assert len(_notify_lines(first)) == 1
    second = _run(tmp_path, json_out=TWO_UPDATES, rc=0, boot_id="boot-aaa")
    assert len(_notify_lines(second)) == 2
    assert "lindos-tune" in _notify_lines(second)[-1]


def test_never_calls_anything_privileged(tmp_path: Path) -> None:
    # a systemd --user/no-root context has no 'sudo'/'pkexec' on PATH at all in this test
    # (the fake bin dir is prepended but does not shadow a real one); the script must still
    # behave identically -- this is really just re-confirming test_no_crlf_and_set_euo's static
    # check dynamically, i.e. it never even tries to invoke anything privileged.
    res = _run(tmp_path, json_out=ONE_UPDATE, rc=0)
    assert res.returncode == 0
    assert "pkexec" not in (res.stdout + res.stderr)
