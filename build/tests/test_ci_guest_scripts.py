"""Behavioural tests of the guest-side CI scripts, run against fake system tools (no systemd, no X, no root).

  * packages/lindos-core/.../qa/ci-live-checks.sh  - judges the LIVE session for the boot test
  * build/qa/ci-observer.sh                        - logs the installer / the first boot for the install test
  * ci-boot-smoke-test.sh's start_live_watch()     - how the live checks are launched

Each script is driven by a table of fake processes (a 'pgrep' that reads a file), fake 'systemctl' state files and
fake xfconf values, so every promise the scripts print (LINDOS_CHECK live-*, oem-*) is proven both to pass on a
right system and to fail, with the reason, on a wrong one.  The output is then fed to the REAL parsers of the
harnesses (boot_test.parse_report / install_test.parse_oem_serial) so grammar and scripts cannot drift apart.
Needs bash; skipped where it is missing.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
QA_DIR = REPO / "build" / "qa"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import boot_test as bt  # noqa: E402
import install_test as it  # noqa: E402

BASH = shutil.which("bash")
LIVE_CHECKS = REPO / "packages" / "lindos-core" / "root" / "usr" / "libexec" / "lindos" / "qa" / "ci-live-checks.sh"
SMOKE = REPO / "packages" / "lindos-core" / "root" / "usr" / "libexec" / "lindos" / "qa" / "ci-boot-smoke-test.sh"
OBSERVER = REPO / "build" / "qa" / "ci-observer.sh"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_PGREP = r"""#!/bin/bash
# table-driven pgrep: FAKE_PROCS has lines user|comm|args (bash's own regex matching: no grep processes to fork)
user=""; exact=0; full=0; list=0; count=0; pat=""
while [ $# -gt 0 ]; do
    case "$1" in
        -u) user="$2"; shift ;;
        -x) exact=1 ;;
        -f) full=1 ;;
        -a) list=1 ;;
        -c) count=1 ;;
        -af|-fa) full=1; list=1 ;;
        -fc|-cf) full=1; count=1 ;;
        -*) ;;
        *) pat="$1" ;;
    esac
    shift
done
n=0; ln=0
while IFS='|' read -r u comm args; do
    ln=$((ln + 1))
    [ -n "$comm" ] || continue
    if [ -n "$user" ] && [ "$u" != "$user" ]; then continue; fi
    if [ "$exact" = 1 ]; then
        [[ "$comm" =~ ^($pat)$ ]] || continue
    elif [ "$full" = 1 ]; then
        [[ "$comm $args" =~ $pat ]] || continue
    else
        [[ "$comm" =~ $pat ]] || continue
    fi
    n=$((n + 1))
    # (a pid is the line number in the table: the tests put /proc/<line>/environ files there)
    if [ "$list" = 1 ]; then printf '%s %s %s\n' 1 "$comm" "$args"; elif [ "$count" = 0 ]; then echo "$ln"; fi
done < "$FAKE_PROCS"
if [ "$count" = 1 ]; then echo "$n"; fi
[ "$n" -gt 0 ]
"""

FAKE_SYSTEMCTL = r"""#!/bin/bash
D="$FAKE_SYSTEMD"
case "$1" in
    is-active)
        shift; quiet=0
        if [ "$1" = "--quiet" ]; then quiet=1; shift; fi
        s="$(cat "$D/active.$1" 2>/dev/null || echo inactive)"
        if [ "$quiet" = 0 ]; then echo "$s"; fi
        [ "$s" = active ] ;;
    get-default) cat "$D/default" 2>/dev/null || echo graphical.target ;;
    is-system-running) cat "$D/running" 2>/dev/null || echo starting ;;
    show) cat "$D/show.$2.$4" 2>/dev/null ;;
    *) exit 0 ;;
esac
"""

FAKE_RUNUSER = r"""#!/bin/bash
# runuser -u USER -- CMD...
[ -z "$FAKE_RUNUSER_FAILS" ] || exit 1
shift 3
exec "$@"
"""

FAKE_XFCONF = r"""#!/bin/bash
# xfconf-query -c CHANNEL -p /xfce4-power-manager/NAME
name="${4##*/}"
cat "$FAKE_XFCONF/$name" 2>/dev/null
"""

FAKE_ID = """#!/bin/bash
if [ "$1" = "-u" ]; then echo 1000; else echo "uid=29999(oem) gid=29999(oem)"; fi
"""

FAKE_PS = r"""#!/bin/bash
# ps -eo user,pid,comm,args --no-headers, from the process table
while IFS='|' read -r u comm args; do
    if [ -n "$comm" ]; then printf '%s %s %s %s\n' "$u" 1 "$comm" "$args"; fi
done < "$FAKE_PROCS"
"""

FAKE_TAIL = "#!/bin/bash\nexit 0\n"        # 'tail -F' would run for ever behind the pipes of the live mode

FAKE_INHIBIT = r"""#!/bin/bash
# systemd-inhibit --list --no-legend: the table of held inhibitors, from a file of the fake systemd state
cat "$FAKE_SYSTEMD/inhibit" 2>/dev/null
exit 0
"""


def _tool(bin_dir: Path, name: str, body: str) -> None:
    f = bin_dir / name
    f.write_text(body, encoding="utf-8", newline="\n")
    f.chmod(0o755)


class Guest:
    """A fake guest: process table, systemd state, xfconf values, a live user's home."""

    def __init__(self, tmp_path: Path, *, tail: bool = False) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.procs = tmp_path / "procs"
        self.systemd = tmp_path / "systemd"
        self.systemd.mkdir()
        self.xfconf = tmp_path / "xfconf"
        self.xfconf.mkdir()
        self.home = tmp_path / "home" / "liveuser"
        (self.home / "Desktop").mkdir(parents=True)
        self.proc = tmp_path / "proc"
        self.proc.mkdir()
        self.cmdline = tmp_path / "cmdline"
        self.cmdline.write_text("boot=casper username=liveuser hostname=lindos\n", encoding="utf-8")
        self.is_live = tmp_path / "is-live"
        self.is_live.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8", newline="\n")
        for name, body in (("pgrep", FAKE_PGREP), ("systemctl", FAKE_SYSTEMCTL), ("runuser", FAKE_RUNUSER),
                           ("xfconf-query", FAKE_XFCONF), ("id", FAKE_ID), ("journalctl", "#!/bin/bash\nexit 0\n"),
                           ("ps", FAKE_PS), ("systemd-inhibit", FAKE_INHIBIT)):
            _tool(self.bin, name, body)
        if tail:
            _tool(self.bin, "tail", FAKE_TAIL)
        self.set_procs([])

    def set_procs(self, procs: Iterable[str]) -> None:
        self.procs.write_text("\n".join(procs) + "\n", encoding="utf-8")

    def state(self, name: str, value: str) -> None:
        (self.systemd / name).write_text(value + "\n", encoding="utf-8")

    def process_env(self, line: int, **variables: str) -> None:
        """/proc/<line>/environ of the process on that line of the process table (NUL separated, like the kernel's)."""
        d = self.proc / str(line)
        d.mkdir(parents=True, exist_ok=True)
        (d / "environ").write_bytes(b"".join(("%s=%s" % kv).encode() + b"\0" for kv in variables.items()))

    def power(self, **values: str) -> None:
        for k, v in values.items():
            (self.xfconf / k.replace("_", "-")).write_text(v + "\n", encoding="utf-8")

    def launcher(self, text: str) -> None:
        (self.home / "Desktop" / "ubiquity.desktop").write_text(text, encoding="utf-8", newline="\n")

    def env(self, **extra: str) -> Dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.update(FAKE_PROCS=self.procs.as_posix(), FAKE_SYSTEMD=self.systemd.as_posix(), FAKE_XFCONF=self.xfconf.as_posix(),
                   LINDOS_CI_CONSOLE="-", LINDOS_CI_OUT="-", LINDOS_CI_CMDLINE=self.cmdline.as_posix(),
                   LINDOS_CI_IS_LIVE_SESSION=self.is_live.as_posix(), LINDOS_CI_LIVE_USER="liveuser",
                   LINDOS_CI_LIVE_HOME=self.home.as_posix(), LINDOS_CI_PANEL_WAIT="3", LINDOS_CI_SETTLE="0",
                   LINDOS_CI_PROC=self.proc.as_posix(),
                   LINDOS_CI_TICK="0.01", LINDOS_CI_STEP="0.01", LINDOS_CI_OEM_WAIT="3", LINDOS_CI_MAX_TICKS="2",
                   LINDOS_CI_BEAT_EVERY="1000")
        env.update(extra)
        return env

    def run(self, script: Path, *args: str, **extra: str) -> str:
        proc = subprocess.run([BASH, script.as_posix(), *args], capture_output=True, text=True, timeout=60, check=False,
                              env=self.env(**extra))
        assert proc.returncode == 0, proc.stderr
        return proc.stdout


GOOD_LAUNCHER = """\
[Desktop Entry]
Type=Application
Name=Install Lindos
Name[de]=Lindos installieren
Icon=lindos-logo
Exec=sh -c 'GTK_THEME=Lindos-Setup ubiquity gtk_ui'
"""

LIVE_SESSION_PROCS = [
    "liveuser|xfce4-session|xfce4-session", "liveuser|xfwm4|xfwm4 --replace", "liveuser|xfce4-panel|xfce4-panel --disable-wm-check",
    "liveuser|xfdesktop|xfdesktop", "root|Xorg|/usr/lib/xorg/Xorg :0", "root|lightdm|lightdm",
]


def live_guest(tmp_path: Path) -> Guest:
    g = Guest(tmp_path)
    g.set_procs(LIVE_SESSION_PROCS)
    g.launcher(GOOD_LAUNCHER)
    g.state("active.lindos-live-inhibit.service", "active")
    g.power(inactivity_on_ac="14", inactivity_on_battery="14", lid_action_on_ac="0", lid_action_on_battery="0")
    return g


def checks_of(out: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"^LINDOS_CHECK (\S+)=(OK|FAIL)", out, re.M)}


def why(out: str, name: str) -> str:
    m = re.search(r"^LINDOS_FAIL_LOG %s: (.*)$" % re.escape(name), out, re.M)
    return m.group(1) if m else ""


# ============================================================================================ ci-live-checks.sh
def test_a_working_live_desktop_passes_every_check(tmp_path):
    out = live_guest(tmp_path).run(LIVE_CHECKS)
    got = checks_of(out)
    assert got == {name: "OK" for name in bt.REQUIRED_LIVE_CHECKS}
    assert out.splitlines()[0] == "LINDOS_LIVE_WATCH_STARTED" and out.rstrip().endswith("LINDOS_LIVE_CHECKS_DONE fails=0")
    assert "LINDOS_LIVE_DIAG" not in out                          # diagnostics only appear when something failed


def test_the_scripts_output_satisfies_the_boot_tests_own_verdict(tmp_path):
    out = live_guest(tmp_path).run(LIVE_CHECKS)
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\n" + out + "LINDOS_SMOKE_DONE rc=0\n", encoding="utf-8")
    report = bt.parse_report(log)
    assert report["live_watch_started"] and report["live_checks_done"] and report["live_fails"] == 0
    assert bt.live_verdict(report) == []
    assert all(v["status"] == "OK" for v in report["checks"].values())


def test_the_first_run_wizard_in_the_live_session_fails_the_check(tmp_path):
    g = live_guest(tmp_path)
    g.set_procs(LIVE_SESSION_PROCS + ["liveuser|python3|python3 /usr/lib/lindos-setup/main.py --first-run"])
    out = g.run(LIVE_CHECKS)
    assert checks_of(out)["live-no-oobe"] == "FAIL"
    assert "lindos-setup/main.py --first-run" in why(out, "live-no-oobe")
    assert out.rstrip().endswith("LINDOS_LIVE_CHECKS_DONE fails=1") and "LINDOS_LIVE_DIAG ps:" in out
    assert bt.live_verdict(bt.parse_report(_serial(tmp_path, out)))[0].startswith("live check live-no-oobe FAILED")


def _serial(tmp_path: Path, out: str) -> Path:
    log = tmp_path / "serial.log"
    log.write_text(out, encoding="utf-8")
    return log


@pytest.mark.parametrize("launcher, needle", [
    (None, "does not exist"),
    (GOOD_LAUNCHER.replace("Install Lindos", "Install Linux Mint"), "Name=Install Linux Mint"),
    (GOOD_LAUNCHER.replace("Name=Install Lindos", "Name=Install RELEASE"), "Name=Install RELEASE"),
    (GOOD_LAUNCHER + "Comment=Linux Mint installer\n", "Linux Mint"),
    (GOOD_LAUNCHER.replace("ubiquity gtk_ui", "true"), "Exec="),
])
def test_the_desktop_needs_the_branded_installer_launcher(tmp_path, launcher, needle):
    g = live_guest(tmp_path)
    if launcher is None:
        (g.home / "Desktop" / "ubiquity.desktop").unlink()
    else:
        g.launcher(launcher)
    out = g.run(LIVE_CHECKS)
    assert checks_of(out)["live-installer-launcher"] == "FAIL" and needle in why(out, "live-installer-launcher")


def test_panel_and_desktop_manager_must_run_for_the_live_user(tmp_path):
    g = live_guest(tmp_path)
    g.set_procs([p for p in LIVE_SESSION_PROCS if "xfce4-panel" not in p and "xfdesktop" not in p])
    out = g.run(LIVE_CHECKS)
    got = checks_of(out)
    assert got["live-panel"] == "FAIL" and got["live-desktop"] == "FAIL" and "xfdesktop" in why(out, "live-desktop")
    # a panel of ANOTHER user does not count
    g.set_procs(["root|xfce4-panel|xfce4-panel", "root|xfdesktop|xfdesktop", "root|xfwm4|xfwm4", "root|xfce4-session|xfce4-session"])
    assert checks_of(g.run(LIVE_CHECKS))["live-panel"] == "FAIL"


def test_the_session_is_given_time_to_come_up_before_the_panel_check_fails(tmp_path):
    """The panel appears while the script waits: the checks must then run against the finished session."""
    g = live_guest(tmp_path)
    g.set_procs([p for p in LIVE_SESSION_PROCS if "xfce4-panel" not in p])
    out = g.run(LIVE_CHECKS, LINDOS_CI_PANEL_WAIT="1")
    assert checks_of(out)["live-panel"] == "FAIL"


@pytest.mark.parametrize("mutation, needle", [
    (lambda g: g.state("active.lindos-live-inhibit.service", "inactive"), "lindos-live-inhibit.service is not active"),
    (lambda g: g.power(inactivity_on_ac="4"), "inactivity-on-ac is '4'"),
    (lambda g: g.power(lid_action_on_battery="1"), "lid-action-on-battery is '1'"),
])
def test_the_live_session_must_never_sleep(tmp_path, mutation, needle):
    g = live_guest(tmp_path)
    mutation(g)
    out = g.run(LIVE_CHECKS)
    assert checks_of(out)["live-no-sleep"] == "FAIL" and needle in why(out, "live-no-sleep")


def test_power_settings_fall_back_to_the_saved_channel_file(tmp_path):
    """From a system service the user's session bus may be out of reach: the saved xfconf file must then decide."""
    g = live_guest(tmp_path)
    for f in g.xfconf.iterdir():
        f.unlink()
    xml = g.home / ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text('<channel name="xfce4-power-manager">\n <property name="xfce4-power-manager" type="empty">\n'
                   '  <property name="inactivity-on-ac" type="uint" value="14"/>\n  <property name="inactivity-on-battery" type="uint" value="14"/>\n'
                   '  <property name="lid-action-on-ac" type="uint" value="0"/>\n  <property name="lid-action-on-battery" type="uint" value="0"/>\n'
                   ' </property>\n</channel>\n', encoding="utf-8")
    out = g.run(LIVE_CHECKS, FAKE_RUNUSER_FAILS="1")
    assert checks_of(out)["live-no-sleep"] == "OK"
    xml.write_text(xml.read_text(encoding="utf-8").replace('"inactivity-on-ac" type="uint" value="14"', '"inactivity-on-ac" type="uint" value="30"'),
                   encoding="utf-8")
    out = g.run(LIVE_CHECKS, FAKE_RUNUSER_FAILS="1")
    assert "inactivity-on-ac is '30'" in why(out, "live-no-sleep")


@pytest.mark.parametrize("proc, needle", [
    ("root|install-browser.|/usr/libexec/lindos/install-browser.sh chrome", "install-browser.sh"),
    ("root|python3|python3 /usr/libexec/lindos/lindos-helper apply-mode", "lindos-helper"),
    ("root|pkexec|pkexec /usr/libexec/lindos/lindos-helper", "pkexec"),
    ("root|flatpak|flatpak install flathub org.x", "flatpak"),
])
def test_nothing_installs_in_the_live_session(tmp_path, proc, needle):
    g = live_guest(tmp_path)
    g.set_procs(LIVE_SESSION_PROCS + [proc])
    out = g.run(LIVE_CHECKS)
    assert checks_of(out)["live-no-installs"] == "FAIL" and needle in why(out, "live-no-installs")


def test_the_first_boot_retry_units_must_not_have_started_in_the_live_session(tmp_path):
    g = live_guest(tmp_path)
    g.state("active.lindos-browser-firstboot.service", "activating")
    assert "lindos-browser-firstboot.service is active" in why(g.run(LIVE_CHECKS), "live-no-installs")


def test_a_background_package_manager_is_recorded_but_not_a_failure(tmp_path):
    g = live_guest(tmp_path)
    g.set_procs(LIVE_SESSION_PROCS + ["root|dpkg|dpkg --configure -a"])
    out = g.run(LIVE_CHECKS)
    assert checks_of(out)["live-no-installs"] == "OK" and "LINDOS_INFO live_package_processes=" in out


def test_the_live_user_comes_from_the_kernel_command_line(tmp_path):
    g = live_guest(tmp_path)
    g.cmdline.write_text("boot=casper username=mint hostname=mint\n", encoding="utf-8")
    out = g.run(LIVE_CHECKS, LINDOS_CI_LIVE_USER="")
    assert "LINDOS_INFO live_user=mint" in out


def test_an_installed_system_is_not_judged(tmp_path):
    g = live_guest(tmp_path)
    g.is_live.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8", newline="\n")
    out = g.run(LIVE_CHECKS)
    assert checks_of(out) == {} and "LINDOS_LIVE_CHECKS_DONE fails=0 skipped=1" in out


def test_the_watcher_never_installs_or_modifies_anything():
    text = LIVE_CHECKS.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r" not in text
    assert not re.search(r"^\s*set -[a-zA-Z]*e", text, re.M)          # one failing check must not stop the rest
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for pattern in (r"apt(-get)? +(install|remove|upgrade|update)", r"dpkg +-[iPr]\b", r"\brm +-",
                    r"systemctl +(start|stop|restart|enable|disable)", r"\bsudo\b", r"\bmv +", r"\bcp +", r"\bchmod\b",
                    r"\bchown\b", r"flatpak +install"):
        assert not re.search(pattern, code), pattern


# ============================================================================================ ci-boot-smoke-test.sh
def _function(name: str) -> str:
    text = SMOKE.read_text(encoding="utf-8")
    m = re.search(r"^%s\(\) \{\n(.*?\n)^\}\n" % re.escape(name), text, re.M | re.S)
    assert m, name
    return "%s() {\n%s}\n" % (name, m.group(1))


def _run_start_live_watch(tmp_path: Path, *, sdr_rc: Optional[int], script_exists: bool = True) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    if sdr_rc is not None:
        _tool(bin_dir, "systemd-run", '#!/bin/bash\necho "systemd-run $*" >>"%s"\n[ %d -eq 0 ] || { echo "no bus" >&2; exit %d; }\n'
              % (calls.as_posix(), sdr_rc, sdr_rc))
    _tool(bin_dir, "setsid", '#!/bin/bash\necho "setsid $*" >>"%s"\n' % calls.as_posix())
    checks = tmp_path / "ci-live-checks.sh"
    if script_exists:
        checks.write_text("#!/bin/bash\n", encoding="utf-8")
    body = _function("start_live_watch").replace("/usr/libexec/lindos/qa/ci-live-checks.sh", checks.as_posix()) \
        .replace("/dev/console", (tmp_path / "console").as_posix()).replace("/bin/bash", BASH)
    harness = tmp_path / "harness.sh"
    harness.write_text("#!/bin/bash\n" + body + "\nstart_live_watch\nsleep 0.3\n", encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + (env.get("PATH", "") if sdr_rc is not None else "/usr/bin:/bin")
    proc = subprocess.run([BASH, harness.as_posix()], capture_output=True, text=True, timeout=30, check=False, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout + "\n--calls--\n" + (calls.read_text(encoding="utf-8") if calls.exists() else "")


def test_start_live_watch_gives_the_checks_a_unit_of_their_own(tmp_path):
    out = _run_start_live_watch(tmp_path, sdr_rc=0)
    assert "LINDOS_INFO live_watch_launch=systemd-run rc=0" in out
    assert "systemd-run --no-block --unit=lindos-live-watch --collect" in out and "ci-live-checks.sh" in out
    assert "setsid" not in out.split("--calls--")[1]                  # never both: the checks print verdicts, not one sentinel


def test_start_live_watch_falls_back_to_a_detached_process_only_when_systemd_run_fails(tmp_path):
    out = _run_start_live_watch(tmp_path, sdr_rc=1)
    assert "live_watch_launch=systemd-run rc=1" in out and "LINDOS_INFO live_watch_launch=setsid" in out
    assert "setsid" in out.split("--calls--")[1]


def test_start_live_watch_says_so_when_the_script_is_not_on_the_iso(tmp_path):
    out = _run_start_live_watch(tmp_path, sdr_rc=0, script_exists=False)
    assert "live_watch_launch=missing" in out and "systemd-run" not in out.split("--calls--")[1]


def test_the_smoke_test_starts_the_live_watch_after_the_desktop_watch():
    text = SMOKE.read_text(encoding="utf-8")
    assert re.search(r"^start_desktop_watch\nstart_live_watch\n", text, re.M)
    assert "ci-live-checks.sh" in text and "--unit=lindos-live-watch" in text


# ============================================================================================ ci-observer.sh (oem)
OEM_PROCS = ["root|ubiquity-dm|/usr/bin/python3 /usr/bin/ubiquity-dm vt7 :0 oem /usr/sbin/oem-config-wrapper --only",
             "root|oem-config-first|/bin/bash /usr/sbin/oem-config-firstboot", "root|Xorg|/usr/lib/xorg/Xorg :0 vt7",
             "oem|xfwm4|xfwm4 --compositor=off", "root|python3|/usr/bin/python3 /usr/sbin/oem-config --only"]
OEM_GTK_LINE = 5          # the line of the wizard's own GTK program in the table above: /proc/5/environ


def oem_guest(tmp_path: Path) -> Guest:
    g = Guest(tmp_path)
    g.set_procs(OEM_PROCS)
    g.state("default", "oem-config.target")
    g.state("active.oem-config.service", "activating")
    g.process_env(OEM_GTK_LINE, PATH="/usr/bin", GTK_THEME="Lindos-Setup")
    return g


def test_the_first_boot_into_oem_config_is_recognised(tmp_path):
    out = oem_guest(tmp_path).run(OBSERVER, "oem")
    got = checks_of(out)
    assert got == {name: "OK" for name in ("oem-default-target", "oem-config-service", "oem-wizard-process", "oem-x-server",
                                           "oem-no-lightdm", "oem-no-lindos-setup", "oem-no-installs", "oem-wizard-theme")}
    assert "LINDOS_INFO oem_wizard_gtk_theme=Lindos-Setup" in out
    assert "LINDOS_OBSERVER_STARTED mode=oem" in out and "LINDOS_OEM_READY fails=0" in out and "LINDOS_OEM_TIMEOUT" not in out
    parsed = it.parse_oem_serial(out)                       # the harness reads exactly what the script prints
    assert parsed["observer"] and parsed["ready"] and parsed["fails"] == 0 and not ic_failures(it.judge_first_boot(parsed, screenshot_ok=True))


def ic_failures(findings):
    return [f for f in findings if f.level == "fail"]


@pytest.mark.parametrize("extra_proc, check, needle", [
    ("root|lightdm|/usr/sbin/lightdm", "oem-no-lightdm", "LightDM is running"),
    ("alice|python3|python3 /usr/lib/lindos-setup/main.py --first-run", "oem-no-lindos-setup", "first-run wizard"),
    ("root|install-browser.|/usr/libexec/lindos/install-browser.sh chrome", "oem-no-installs", "installer/retry script"),
])
def test_a_first_boot_that_is_not_the_wizard_fails_the_matching_check(tmp_path, extra_proc, check, needle):
    g = oem_guest(tmp_path)
    g.set_procs(OEM_PROCS + [extra_proc])
    out = g.run(OBSERVER, "oem")
    assert checks_of(out)[check] == "FAIL" and needle in why(out, check) and "LINDOS_OEM_READY fails=1" in out
    findings = it.judge_first_boot(it.parse_oem_serial(out), screenshot_ok=True)
    assert "first-boot-" + check in {f.name for f in findings if f.level == "fail"}


@pytest.mark.parametrize("environment", [{"PATH": "/usr/bin"}, {"GTK_THEME": "Adwaita"}, {"GTK_THEME": "Lindos-Dark"}])
def test_a_wizard_whose_gtk_program_did_not_get_the_skin_fails_the_theme_check(tmp_path, environment):
    """The systemd drop-in's GTK_THEME must reach the GTK program (through oem-config-firstboot and ubiquity-dm): the
    observer reads the environment the program was started with."""
    g = oem_guest(tmp_path)
    g.process_env(OEM_GTK_LINE, **environment)
    out = g.run(OBSERVER, "oem")
    assert checks_of(out)["oem-wizard-theme"] == "FAIL" and "not Lindos-Setup" in why(out, "oem-wizard-theme")
    assert "LINDOS_OEM_READY fails=1" in out
    findings = it.judge_first_boot(it.parse_oem_serial(out), screenshot_ok=True)
    assert "first-boot-oem-wizard-theme" in {f.name for f in findings if f.level == "fail"}


def test_no_environment_to_read_is_a_failed_theme_check_not_a_silent_pass(tmp_path):
    g = oem_guest(tmp_path)
    (g.proc / str(OEM_GTK_LINE) / "environ").unlink()
    out = g.run(OBSERVER, "oem")
    assert checks_of(out)["oem-wizard-theme"] == "FAIL" and "oem_wizard_gtk_theme=none" in out


def test_an_unarmed_system_that_boots_the_normal_target_fails_the_default_target_check(tmp_path):
    g = oem_guest(tmp_path)
    g.state("default", "graphical.target")
    g.state("active.oem-config.service", "inactive")
    out = g.run(OBSERVER, "oem")
    got = checks_of(out)
    assert got["oem-default-target"] == "FAIL" and got["oem-config-service"] == "FAIL"
    assert "graphical.target" in why(out, "oem-default-target")


def test_a_wizard_that_never_comes_up_times_out_with_diagnostics(tmp_path):
    g = oem_guest(tmp_path)
    g.set_procs(["root|systemd|/sbin/init"])
    out = g.run(OBSERVER, "oem")
    assert "LINDOS_OEM_TIMEOUT fails=" in out and "LINDOS_OEM_READY" not in out
    assert checks_of(out)["oem-wizard-process"] == "FAIL" and checks_of(out)["oem-x-server"] == "FAIL"
    parsed = it.parse_oem_serial(out)
    assert parsed["timeout"] and not parsed["ready"]
    assert "boot-oem-config" in {f.name for f in ic_failures(it.judge_first_boot(parsed, screenshot_ok=None))}


def test_the_observer_keeps_talking_after_ready(tmp_path):
    out = oem_guest(tmp_path).run(OBSERVER, "oem", LINDOS_CI_BEAT_EVERY="1", LINDOS_CI_MAX_TICKS="3")
    assert out.count("LINDOS_OEM_HEARTBEAT") == 3 and "wizard=up" in out


def test_the_observer_rejects_an_unknown_mode(tmp_path):
    proc = subprocess.run([BASH, OBSERVER.as_posix(), "bogus"], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 2 and "usage" in proc.stderr


# ============================================================================================ ci-observer.sh (live)
def live_observer_guest(tmp_path: Path) -> Guest:
    g = Guest(tmp_path, tail=True)
    g.set_procs(["root|ubiquity|/usr/bin/python3 /usr/bin/ubiquity --automatic"])
    (tmp_path / "version").write_text("ubiquity 24.04.3\n", encoding="utf-8")
    return g


def test_a_failing_installer_is_reported_once(tmp_path):
    g = live_observer_guest(tmp_path)
    g.state("show.ubiquity.service.Result", "exit-code")
    g.state("show.ubiquity.service.ActiveState", "failed")
    out = g.run(OBSERVER, "live", LINDOS_CI_UBIQUITY_VERSION=(tmp_path / "version").as_posix(), LINDOS_CI_MAX_TICKS="3")
    assert out.count("LINDOS_INSTALL_FAILED ubiquity.service result=exit-code state=failed") == 1
    parsed = it.parse_install_serial(out)
    assert parsed["observer"] and parsed["failed"] == ["ubiquity.service result=exit-code state=failed"]


def test_a_finished_installer_is_reported_only_after_ubiquity_really_ran(tmp_path):
    g = live_observer_guest(tmp_path)
    g.state("show.ubiquity.service.Result", "success")
    g.state("show.ubiquity.service.ActiveState", "inactive")
    version = tmp_path / "version"
    out = g.run(OBSERVER, "live", LINDOS_CI_UBIQUITY_VERSION=version.as_posix())
    assert "LINDOS_INSTALL_UBIQUITY_EXIT result=success" in out
    version.unlink()                       # not started yet: 'inactive + success' is also what a service that never ran looks like
    out = g.run(OBSERVER, "live", LINDOS_CI_UBIQUITY_VERSION=version.as_posix())
    assert "LINDOS_INSTALL_UBIQUITY_EXIT" not in out and "LINDOS_INSTALL_FAILED" not in out


def test_a_finalized_installation_is_announced_once(tmp_path):
    """finalize.sh's last log line tells the harness that only the unmount and the poweroff are left."""
    g = live_observer_guest(tmp_path)
    hook_log = tmp_path / "hook.log"
    hook_log.write_text("2026-09-29 10:25:00 lindos-installer: finalize: start\n", encoding="utf-8")
    out = g.run(OBSERVER, "live", LINDOS_CI_HOOK_LOG=hook_log.as_posix(), LINDOS_CI_UBIQUITY_VERSION=(tmp_path / "version").as_posix())
    assert "LINDOS_INSTALL_FINALIZED" not in out
    hook_log.write_text(hook_log.read_text(encoding="utf-8") + "2026-09-29 10:25:02 lindos-installer: finalize: done\n", encoding="utf-8")
    out = g.run(OBSERVER, "live", LINDOS_CI_HOOK_LOG=hook_log.as_posix(), LINDOS_CI_UBIQUITY_VERSION=(tmp_path / "version").as_posix(),
                LINDOS_CI_MAX_TICKS="3")
    assert out.count("LINDOS_INSTALL_FINALIZED") == 1
    assert it.parse_install_serial(out)["finalized"] is True


def test_a_running_installer_prints_heartbeats(tmp_path):
    g = live_observer_guest(tmp_path)
    g.state("show.ubiquity.service.ActiveState", "active")
    out = g.run(OBSERVER, "live", LINDOS_CI_BEAT_EVERY="1", LINDOS_CI_MAX_TICKS="2", LINDOS_CI_UBIQUITY_VERSION=(tmp_path / "version").as_posix())
    assert out.count("LINDOS_INSTALL_HEARTBEAT") == 2 and "ubiquity=active" in out


def test_observer_scripts_are_read_only_and_never_use_set_e():
    text = OBSERVER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r" not in text
    assert re.search(r"^set -u$", text, re.M) and not re.search(r"^set -[a-zA-Z]*e", text, re.M)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for pattern in (r"apt(-get)? +(install|remove|upgrade|update)", r"dpkg +-[iPr]\b", r"\brm +-",
                    r"systemctl +(start|stop|restart|enable|disable)", r"\bsudo\b", r"\bmount +", r"\bchmod\b",
                    r"flatpak +install"):
        assert not re.search(pattern, code), pattern


# ============================================================================================ ci-observer.sh (live): the Install session
# The default 'Install Lindos' entry is only-ubiquity: ubiquity-dm starts the installer on its OWN X server with its own window
# manager (xfwm4 --compositor=off) and nothing else - no LightDM, no XFCE session, no first-run wizard, no pkexec.
SESSION_PROCS = [
    "root|python3|/usr/bin/python3 /usr/lib/ubiquity/bin/ubiquity-dm vt1 :0 ubuntu /usr/bin/ubiquity --only",
    "root|python3|/usr/bin/python3 /usr/bin/ubiquity --only",
    "root|Xorg|/usr/lib/xorg/Xorg :0 vt1",
    "root|xfwm4|xfwm4 --compositor=off",
]
SESSION_CHECKS = ("live-only-ubiquity", "live-installer-up", "live-no-lightdm", "live-no-xfce-session", "live-no-lindos-setup",
                  "live-no-pkexec", "live-inhibitor-active", "live-installer-theme")
INHIBIT_LIST = ("Lindos 0 root 512 systemd-inhibit sleep:idle:handle-lid-switch:handle-suspend-key:handle-hibernate-key "
                "Live session: do not interrupt the installation block")


def install_session_guest(tmp_path: Path) -> Guest:
    g = live_observer_guest(tmp_path)
    g.set_procs(SESSION_PROCS)
    g.cmdline.write_text("BOOT_IMAGE=/casper/vmlinuz boot=casper only-ubiquity oem-config/enable=true username=liveuser "
                         "hostname=lindos lindos.ci_install_test --\n", encoding="utf-8")
    g.state("active.lindos-live-inhibit.service", "active")
    g.state("inhibit", INHIBIT_LIST)
    for line in (1, 2):       # ubiquity-dm and the GTK program: both were started with the environment of ubiquity.service
        g.process_env(line, PATH="/usr/bin", GTK_THEME="Lindos-Setup")
    return g


def test_an_installer_that_did_not_get_the_skin_fails_the_theme_check(tmp_path):
    g = install_session_guest(tmp_path)
    for line in (1, 2):
        g.process_env(line, PATH="/usr/bin")
    out = session_run(g, tmp_path)
    assert checks_of(out)["live-installer-theme"] == "FAIL" and "not Lindos-Setup" in why(out, "live-installer-theme")
    assert "LINDOS_INFO installer_gtk_theme=none" in out
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, completed(it.parse_install_serial(out)))
    assert "install-live-installer-theme" in {f.name for f in ic_failures(findings)}


def test_a_process_that_only_mentions_the_desktop_in_its_arguments_is_not_a_desktop_session(tmp_path):
    """earlyoom's own command line names xfce4-panel in its --avoid regular expression (first real install: a false FAIL)."""
    g = install_session_guest(tmp_path)
    g.set_procs(SESSION_PROCS + ["root|earlyoom|/usr/bin/earlyoom -m 4 -s 100 --avoid (^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$"])
    out = session_run(g, tmp_path)
    got = checks_of(out)
    assert got["live-no-xfce-session"] == "OK" and got["live-no-lightdm"] == "OK", why(out, "live-no-xfce-session")
    assert out.count("LINDOS_INSTALL_SESSION_CHECKED fails=0") == 1


def session_run(g: Guest, tmp_path: Path, **extra: str) -> str:
    env = {"LINDOS_CI_UBIQUITY_VERSION": (tmp_path / "version").as_posix(), "LINDOS_CI_LIVE_CHECK_TICKS": "1", "LINDOS_CI_MAX_TICKS": "3"}
    env.update(extra)
    return g.run(OBSERVER, "live", **env)


def completed(parsed: dict) -> dict:
    """The serial facts of an install that ran to its end (a kernel booted, the guest powered off)."""
    return dict(parsed, booted=True, power_down=True)


def test_the_install_session_is_judged_once_and_a_correct_one_passes_every_check(tmp_path):
    out = session_run(install_session_guest(tmp_path), tmp_path)
    assert {k: v for k, v in checks_of(out).items() if k.startswith("live-")} == {name: "OK" for name in SESSION_CHECKS}
    assert out.count("LINDOS_INSTALL_SESSION_CHECKED fails=0") == 1 and "LINDOS_INSTALL_DIAG" not in out
    # ubiquity-dm's own xfwm4 is what a healthy only-ubiquity session runs: it must not count as a desktop session
    assert "xfwm4" in (tmp_path / "procs").read_text(encoding="utf-8")
    parsed = it.parse_install_serial(out)
    assert parsed["session_checked"] and parsed["session_fails"] == 0
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, completed(parsed))
    assert not ic_failures(findings) and {f.name for f in findings if f.level == "ok"} >= {"install-" + n for n in SESSION_CHECKS}


def test_the_session_checks_wait_for_their_moment_and_run_only_once(tmp_path):
    g = install_session_guest(tmp_path)
    out = g.run(OBSERVER, "live", LINDOS_CI_UBIQUITY_VERSION=(tmp_path / "version").as_posix(), LINDOS_CI_MAX_TICKS="3")     # default: 24 probes
    assert "LINDOS_CHECK live-" not in out and "LINDOS_INSTALL_SESSION_CHECKED" not in out
    out = session_run(g, tmp_path, LINDOS_CI_MAX_TICKS="6")
    assert out.count("LINDOS_INSTALL_SESSION_CHECKED") == 1 and out.count("LINDOS_CHECK live-no-lightdm=") == 1


@pytest.mark.parametrize("extra_proc, check, needle", [
    ("root|lightdm|/usr/sbin/lightdm", "live-no-lightdm", "LightDM is running"),
    ("liveuser|xfce4-session|xfce4-session", "live-no-xfce-session", "desktop session runs"),
    ("liveuser|xfce4-panel|xfce4-panel --disable-wm-check", "live-no-xfce-session", "desktop session runs"),
    ("liveuser|xfdesktop|xfdesktop", "live-no-xfce-session", "desktop session runs"),
    ("liveuser|python3|python3 /usr/lib/lindos-setup/main.py --first-run", "live-no-lindos-setup", "first-run wizard"),
    ("liveuser|pkexec|pkexec /usr/libexec/lindos/install-browser.sh", "live-no-pkexec", "pkexec is running"),
])
def test_a_session_that_is_more_than_the_installer_fails_the_matching_check(tmp_path, extra_proc, check, needle):
    g = install_session_guest(tmp_path)
    g.set_procs(SESSION_PROCS + [extra_proc])
    out = session_run(g, tmp_path)
    assert checks_of(out)[check] == "FAIL" and needle in why(out, check)
    assert "LINDOS_INSTALL_SESSION_CHECKED fails=1" in out and "LINDOS_INSTALL_DIAG ps:" in out      # the process list follows a failure
    parsed = it.parse_install_serial(out)
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, completed(parsed))
    assert "install-" + check in {f.name for f in ic_failures(findings)}
    assert any(f.name == "install-session-diag" for f in findings)


def test_the_default_entry_must_really_be_only_ubiquity(tmp_path):
    g = install_session_guest(tmp_path)
    g.cmdline.write_text("boot=casper oem-config/enable=true username=liveuser --\n", encoding="utf-8")      # the Try entry's words
    out = session_run(g, tmp_path)
    assert checks_of(out)["live-only-ubiquity"] == "FAIL" and "not the 'Install Lindos' session" in why(out, "live-only-ubiquity")


def test_no_installer_process_means_the_session_never_started(tmp_path):
    g = install_session_guest(tmp_path)
    g.set_procs(["root|Xorg|/usr/lib/xorg/Xorg :0 vt1"])
    assert checks_of(session_run(g, tmp_path))["live-installer-up"] == "FAIL"


def test_a_short_lived_helper_is_not_a_desktop_session(tmp_path):
    """The 'gone' test needs the process on all three looks: something that is there for one probe only passes."""
    g = install_session_guest(tmp_path)
    # a pgrep that finds lightdm on the first look only
    fake = g.bin / "pgrep"
    body = fake.read_text(encoding="utf-8")
    fake.write_text(body.replace('n=0\n', 'n=0\nif [ "$exact" = 1 ] && [ "$pat" = lightdm ] && [ ! -e "$FAKE_SYSTEMD/looked" ]; then : > "$FAKE_SYSTEMD/looked"; echo 1; exit 0; fi\n', 1),
                    encoding="utf-8", newline="\n")
    out = session_run(g, tmp_path)
    assert checks_of(out)["live-no-lightdm"] == "OK"


@pytest.mark.parametrize("unit_state, listing", [
    ("inactive", INHIBIT_LIST),                       # the unit is not active
    ("active", ""),                                    # ... or it is, but nothing holds a lock
    ("active", "Lindos installer 0 root 700 systemd-inhibit sleep Installing Lindos block"),    # only the hook's own lock: not the live one
    ("active", "Lindos 0 root 512 systemd-inhibit idle Live session: do not interrupt the installation block"),   # not on sleep
])
def test_the_sleep_inhibitor_must_be_active_and_listed(tmp_path, unit_state, listing):
    g = install_session_guest(tmp_path)
    g.state("active.lindos-live-inhibit.service", unit_state)
    g.state("inhibit", listing)
    out = session_run(g, tmp_path)
    assert checks_of(out)["live-inhibitor-active"] == "FAIL" and "inhibitor" in why(out, "live-inhibitor-active")


def test_a_session_that_never_got_its_checks_fails_only_a_completed_run(tmp_path):
    parsed = it.parse_install_serial("LINDOS_OBSERVER_STARTED mode=live uname=6.14\nLinux version x\nreboot: Power down\n")
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 60}, parsed)
    assert {"install-" + n for n in SESSION_CHECKS} <= {f.name for f in ic_failures(findings)}
    findings = it.judge_install_phase({"outcome": "timeout", "seconds": 5400}, parsed)
    assert not {f.name for f in ic_failures(findings)} & {"install-" + n for n in SESSION_CHECKS}      # the timeout is the finding
    assert any(f.name == "install-live-no-lightdm" and f.level == "info" for f in findings)
    # no observer at all: the existing warning covers it, no invented session failures
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 60}, it.parse_install_serial("Linux version x\nreboot: Power down\n"))
    assert not any(f.name.startswith("install-live-") for f in findings)


def test_the_inhibitor_the_observer_looks_for_is_the_one_the_shipped_unit_takes():
    """ci-observer.sh matches the WHO/WHY of lindos-live-inhibit.service: keep the two in step."""
    unit = (REPO / "packages" / "lindos-core" / "root" / "usr" / "lib" / "systemd" / "system" / "lindos-live-inhibit.service").read_text(encoding="utf-8")
    assert "--who=Lindos" in unit and '--why="Live session:' in unit and "--what=sleep:" in unit
    obs = OBSERVER.read_text(encoding="utf-8")
    assert "lindos-live-inhibit.service" in obs and '*"Live session"*' in obs and "*sleep*" in obs


def test_the_serial_grammar_of_the_session_checks_is_the_one_install_test_parses():
    obs = OBSERVER.read_text(encoding="utf-8")
    for token in ("LINDOS_INSTALL_SESSION_CHECKED", "LINDOS_INSTALL_DIAG", "LINDOS_CHECK"):
        assert token in obs, token
    for name in SESSION_CHECKS:
        assert name in obs and name in it.REQUIRED_SESSION_CHECKS, name
    assert set(it.REQUIRED_SESSION_CHECKS) == set(SESSION_CHECKS)
