"""dm-noblank.sh, the ubiquity-dm hook that keeps the 'Install Lindos' (only-ubiquity) session's X server from
blanking the screen, run for real against a fake ``xset``.

The 'Install Lindos' boot entries start no desktop session: ubiquity-dm starts a bare X server and the installer.
X's own screensaver/DPMS defaults blank that display after ten minutes without input, and a logind inhibitor
(lindos-live-inhibit.service) cannot stop them.  ubiquity-dm runs this hook once, synchronously, in its start-up
(before the installer window exists), so it must never hang and always exits 0.

What this cannot show (real Linux only): that ubiquity-dm really runs the file, and what a real X server does.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List

from installer_testlib import BASH, LIBEXEC, REPO, needs_bash, write_exec

pytestmark = needs_bash

HOOK = LIBEXEC / "dm-noblank.sh"

#: records every call ('xset ARGS...'), fails on the settings listed in FAKE_XSET_FAIL, sleeps when told to
FAKE_XSET = r'''#!/bin/bash
echo "$*" >>"${FAKE_XSET_LOG}"
case " ${FAKE_XSET_FAIL:-} " in
    *" $* "*) exit 1 ;;
esac
[ -z "${FAKE_XSET_SLEEP:-}" ] || exec sleep "${FAKE_XSET_SLEEP}"
if [ "$1" = "q" ]; then
    printf 'Screen Saver:\n  prefer blanking:  no    allow exposures:  yes\n  timeout:  0    cycle:  0\nDPMS (Energy Star):\n  DPMS is Disabled\n'
fi
exit 0
'''


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log = root / "xset.log"
        self.xset = write_exec(root / "bin" / "xset", FAKE_XSET)

    def env(self, **over: str) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY" and not k.startswith(("LINDOS_", "FAKE_"))}
        env.update({"DISPLAY": ":0", "LINDOS_XSET": self.xset.as_posix(), "FAKE_XSET_LOG": self.log.as_posix()})
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, **over: str) -> "subprocess.CompletedProcess[str]":
        assert BASH is not None
        return subprocess.run([BASH, str(HOOK)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, env=self.env(**over), stdin=subprocess.DEVNULL)

    def calls(self) -> List[str]:
        return [ln.strip() for ln in self.log.read_text(encoding="utf-8").splitlines()] if self.log.is_file() else []


def _rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_it_turns_the_screensaver_blanking_and_dpms_off_each_as_its_own_call(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    proc = rig.run()
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    # three separate calls: a server without the DPMS extension refuses '-dpms' and must not take the others along
    assert rig.calls() == ["s off", "s noblank", "-dpms", "q"]
    assert "screen blanking and DPMS are off" in proc.stderr
    assert "DPMS is Disabled" in proc.stderr and "timeout: 0" in proc.stderr, "what the server says now is logged"


def test_without_a_display_nothing_is_run(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    env = rig.env()
    env.pop("DISPLAY")
    assert BASH is not None
    proc = subprocess.run([BASH, str(HOOK)], capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and proc.stdout == "" and rig.calls() == []
    assert "no DISPLAY" in proc.stderr


def test_without_xset_it_says_so_and_still_exits_zero(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    proc = rig.run(LINDOS_XSET=(tmp_path / "no-such-xset").as_posix())
    assert proc.returncode == 0 and proc.stdout == "" and "no xset" in proc.stderr


def test_a_failing_call_does_not_stop_the_other_settings(tmp_path: Path) -> None:
    for failing in ("-dpms", "s off", "s noblank"):
        rig = Rig(tmp_path / failing.replace(" ", "_").replace("-", "x"))
        proc = rig.run(FAKE_XSET_FAIL=failing)
        assert proc.returncode == 0 and proc.stdout == "", proc.stderr
        assert rig.calls()[:3] == ["s off", "s noblank", "-dpms"], (failing, rig.calls())
        assert f"xset {failing} failed" in proc.stderr and "may still be on" in proc.stderr


def test_a_server_that_does_not_answer_cannot_hang_the_installer(tmp_path: Path) -> None:
    """ubiquity-dm waits for this hook before it shows the installer: every call is time-boxed, and after the first
    timeout the other calls are not waited for one by one."""
    rig = _rig(tmp_path)
    started = time.time()
    proc = rig.run(FAKE_XSET_SLEEP="60", LINDOS_XSET_TIMEOUT="1")
    assert time.time() - started < 30
    assert proc.returncode == 0 and proc.stdout == ""
    assert rig.calls() == ["s off"], "a hung server is not asked three more times"
    assert "xset s off failed" in proc.stderr and "may still be on" in proc.stderr


def test_the_hook_is_a_posix_sh_script_that_never_fails_and_never_uses_stdout() -> None:
    raw = HOOK.read_bytes()
    assert b"\r" not in raw and raw.startswith(b"#!/bin/sh\n"), "ubiquity-dm execs the file itself: a CR in the shebang kills it silently"
    code = "\n".join(ln for ln in raw.decode("utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*[eu]", code, re.M), "no set -e / set -u: a failing xset must not end the hook"
    assert code.rstrip().endswith("exit 0")
    assert not re.search(r"\bsudo\b", code)
    for setting in ("s off", "s noblank", "-dpms"):
        assert setting in code, setting
    assert "timeout -k" in code, "an untimed xset could hang the start of the installer"
    assert "[[" not in code and "local " not in code and "<<<" not in code, "sh (dash) runs it, not bash"
    for line in code.splitlines():
        if re.search(r"\b(echo|printf)\b", line):
            assert ">&2" in line, "nothing goes to stdout: %s" % line


def test_the_name_the_build_deploys_is_the_one_finalize_cleans_up_and_the_service_names() -> None:
    """One name in four places: 79-installer-flow.sh deploys it, finalize.sh removes the copy from the new system,
    lindos-live-inhibit.service documents who covers X blanking."""
    repo = REPO
    build = (repo / "build" / "chroot" / "79-installer-flow.sh").read_text(encoding="utf-8")
    name = re.search(r'^DM_NAME="([^"]+)"$', build, re.M).group(1)
    assert "." not in name and re.fullmatch(r"[0-9]{2}[A-Za-z0-9_-]+", name)
    assert re.search(r'^DM_DIR="\$\{ROOT\}/usr/lib/ubiquity/dm-scripts/install"$', build, re.M)
    assert name in (LIBEXEC / "finalize.sh").read_text(encoding="utf-8")
    service = (repo / "packages" / "lindos-core" / "root" / "usr" / "lib" / "systemd" / "system" / "lindos-live-inhibit.service").read_text(encoding="utf-8")
    assert name in service and "dm-scripts/install" in service
