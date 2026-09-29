"""The live-session-only autostart: on the live USB session a long installation must never be
interrupted by sleep, screen blanking, the screen lock or a closed lid; an installed session is never
touched.  Covers the autostart entry, its package wiring and the script's behaviour (xfconf-query,
gsettings and xset are fakes on PATH, the kernel command line is a fake file)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
SCRIPT = ROOT / "usr" / "libexec" / "lindos" / "live-session-power.sh"
DESKTOP = ROOT / "etc" / "xdg" / "autostart" / "lindos-live-session.desktop"
CORE_LIBEXEC = PKG.parent / "lindos-core" / "root" / "usr" / "libexec" / "lindos"
IS_LIVE_SESSION = CORE_LIBEXEC / "is-live-session"
POWER_XML = ROOT / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml" / "xfce4-power-manager.xml"

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

LIVE = "BOOT_IMAGE=/casper/vmlinuz boot=casper username=liveuser hostname=lindos quiet splash --"
INSTALLED = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=1234 ro quiet splash"


def _entries(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln and not ln.startswith("#"))


def _msys(value: str) -> str:
    """'C:\\x' -> '/c/x' for env vars Git Bash does not translate by itself (same helper as core's tests)."""
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


# --- the autostart entry ------------------------------------------------------------------------------
def test_autostart_entry_is_live_only_by_exec_and_xfce_only():
    entry = _entries(DESKTOP)
    assert entry["Type"] == "Application" and entry["OnlyShowIn"] == "XFCE;"
    assert entry["NoDisplay"] == "true" and entry["Terminal"] == "false"
    assert entry["Name"] and entry["Comment"] and entry["Icon"]
    exec_line = entry["Exec"]
    # gated by the shared helper: only when it says "live" does the power script start
    assert exec_line.startswith('sh -c "/usr/libexec/lindos/is-live-session && exec ')
    assert "/usr/libexec/lindos/live-session-power.sh" in exec_line
    assert exec_line.rstrip().endswith('; exit 0"')                # never a failed autostart on an installed system
    assert entry["TryExec"] == "/usr/libexec/lindos/live-session-power.sh"
    assert entry["X-GNOME-Autostart-Phase"] == "Initialization"     # before xfce4-power-manager reads its settings


def test_autostart_entry_is_a_conffile_and_the_script_is_made_executable():
    conffiles = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    assert "/etc/xdg/autostart/lindos-live-session.desktop" in conffiles
    assert "/usr/libexec/lindos/live-session-power.sh" in (DEBIAN / "postinst").read_text(encoding="utf-8")


def test_the_setup_autostart_is_left_alone_and_gated_in_code_not_here():
    """lindos-setup.desktop (two byte-identical copies, pinned by other tests) is gated by
    lindos.session in lindos-setup's own first-run gate - this package must not add a live check to it."""
    text = (ROOT / "etc" / "xdg" / "autostart" / "lindos-setup.desktop").read_text(encoding="utf-8")
    assert "Exec=lindos-setup --first-run\n" in text and "is-live-session" not in text


def test_polkit_agent_stays_in_the_live_session():
    """Audit result: the polkit agent, the panel seeding and the compositor are what makes the live
    desktop usable (Settings, Lindos look) - they are deliberately NOT gated off."""
    for name in ("lindos-polkit-agent.desktop", "lindos-mode-apply-user.desktop", "lindos-picom.desktop"):
        assert "is-live-session" not in (ROOT / "etc" / "xdg" / "autostart" / name).read_text(encoding="utf-8")


# --- the script -----------------------------------------------------------------------------------------
def test_script_conventions():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw
    text = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in text
    assert re.search(r"^\s*(sudo|pkexec)\s", text, flags=re.M) is None
    assert "/usr/libexec/lindos/is-live-session" in text and "/proc/cmdline" not in text


def _fake_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


def _run(tmp_path: Path, *, cmdline: str, tools: bool = True, display: str = ":0",
         helper: bool = True) -> subprocess.CompletedProcess:
    assert BASH is not None
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    if tools:
        _fake_tool(fakebin, "xfconf-query", 'printf "xfconf-query %s\\n" "$*" >> "$FAKE_LOG"\n')
        _fake_tool(fakebin, "gsettings", 'printf "gsettings %s\\n" "$*" >> "$FAKE_LOG"\nexit 1\n')   # schema missing: ignored
        _fake_tool(fakebin, "xset", 'printf "xset %s\\n" "$*" >> "$FAKE_LOG"\n')
    cmdline_file = tmp_path / "cmdline"
    cmdline_file.write_text(cmdline, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    env["FAKE_LOG"] = _msys(str(log))
    env["LINDOS_TEST_CMDLINE"] = _msys(str(cmdline_file))
    env["LINDOS_IS_LIVE_SESSION"] = str(IS_LIVE_SESSION) if helper else str(tmp_path / "missing-helper")
    if not tools:       # never reach a real xfconf-query/gsettings/xset of the host running the tests
        env.update({"LINDOS_XFCONF_QUERY": "lindos-test-no-such-xfconf-query",
                    "LINDOS_GSETTINGS": "lindos-test-no-such-gsettings", "LINDOS_XSET": "lindos-test-no-such-xset"})
    if display:
        env["DISPLAY"] = display
    else:
        env.pop("DISPLAY", None)
    proc = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, env=env)
    proc.calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []   # type: ignore[attr-defined]
    return proc


def _xfconf_props(calls: List[str]) -> Dict[str, str]:
    """{'channel property': 'type value'} from the recorded xfconf-query calls."""
    props: Dict[str, str] = {}
    for line in calls:
        m = re.match(r"xfconf-query -c (\S+) -p (\S+) -n -t (\S+) -s (\S+)$", line.strip())
        assert m, line
        props[f"{m.group(1)} {m.group(2)}"] = f"{m.group(3)} {m.group(4)}"
    return props


@needs_bash
def test_live_session_turns_off_sleep_blanking_lock_and_lid_suspend(tmp_path: Path):
    proc = _run(tmp_path, cmdline=LIVE)
    assert proc.returncode == 0, proc.stderr
    calls = proc.calls      # type: ignore[attr-defined]
    xf = _xfconf_props([c for c in calls if c.startswith("xfconf-query")])
    pm = "xfce4-power-manager /xfce4-power-manager/"
    for prop, value in {
        "presentation-mode": "bool true", "dpms-enabled": "bool false",
        "dpms-on-ac-sleep": "uint 0", "dpms-on-ac-off": "uint 0",
        "dpms-on-battery-sleep": "uint 0", "dpms-on-battery-off": "uint 0",
        "blank-on-ac": "int 0", "blank-on-battery": "int 0",
        "inactivity-on-ac": "uint 14", "inactivity-on-battery": "uint 14",
        "lid-action-on-ac": "uint 0", "lid-action-on-battery": "uint 0",
        "lock-screen-suspend-hibernate": "bool false",
        "power-button-action": "uint 3", "sleep-button-action": "uint 0", "hibernate-button-action": "uint 0",
    }.items():
        assert xf[pm + prop] == value, prop
    # the screensavers of the base image: xfce4-screensaver (xfconf) and light-locker (gsettings)
    assert xf["xfce4-screensaver /saver/enabled"] == "bool false"
    assert xf["xfce4-screensaver /lock/enabled"] == "bool false"
    assert any(c.startswith("gsettings set apps.light-locker late-locking false") for c in calls)
    assert any(c.startswith("gsettings set apps.light-locker lock-on-lid false") for c in calls)
    # and the X server itself
    assert "xset s off" in calls and "xset s noblank" in calls and "xset -dpms" in calls
    assert "sleep, screen blanking, lock and lid-suspend are off" in proc.stderr


@needs_bash
def test_the_never_values_match_the_shipped_defaults_vocabulary(tmp_path: Path):
    """Every property the script sets is one the shipped power-manager defaults already use (so the
    property names / types are the ones xfce4-power-manager really reads); 14 is the 'Never' slider."""
    shipped = {}
    for m in re.finditer(r'<property name="([^"]+)" type="(\w+)" value="([^"]*)"/>', POWER_XML.read_text(encoding="utf-8")):
        shipped[m.group(1)] = m.group(2)
    proc = _run(tmp_path, cmdline=LIVE)
    for key, spec in _xfconf_props([c for c in proc.calls if c.startswith("xfconf-query")]).items():   # type: ignore[attr-defined]
        channel, prop = key.split(" ", 1)
        if channel != "xfce4-power-manager":
            continue
        name = prop.rsplit("/", 1)[1]
        assert name in shipped, name
        assert spec.split()[0] == shipped[name] or (spec.split()[0], shipped[name]) == ("int", "uint"), (name, spec, shipped[name])


@needs_bash
@pytest.mark.parametrize("cmdline", [INSTALLED, "", "xboot=casper"])
def test_installed_session_is_never_touched(tmp_path: Path, cmdline: str):
    proc = _run(tmp_path, cmdline=cmdline)
    assert proc.returncode == 0, proc.stderr
    assert proc.calls == []             # type: ignore[attr-defined]


@needs_bash
def test_boot_live_counts_as_live_too(tmp_path: Path):
    proc = _run(tmp_path, cmdline="initrd=/live/initrd.img boot=live quiet")
    assert proc.returncode == 0 and any(c.startswith("xfconf-query") for c in proc.calls)     # type: ignore[attr-defined]


@needs_bash
def test_unknown_liveness_means_installed(tmp_path: Path):
    """Without the shared helper the script cannot tell - the safe answer is 'installed': touch nothing."""
    proc = _run(tmp_path, cmdline=LIVE, helper=False)
    assert proc.returncode == 0 and proc.calls == []            # type: ignore[attr-defined]


@needs_bash
def test_missing_tools_and_no_display_never_fail_the_session(tmp_path: Path):
    proc = _run(tmp_path, cmdline=LIVE, tools=False, display="")
    assert proc.returncode == 0, proc.stderr
    assert "xfconf-query not found" in proc.stderr
    second = tmp_path / "second"
    second.mkdir()
    no_display = _run(second, cmdline=LIVE, display="")
    assert no_display.returncode == 0
    assert not any(c.startswith("xset") for c in no_display.calls)          # type: ignore[attr-defined]
    assert any(c.startswith("xfconf-query") for c in no_display.calls)      # type: ignore[attr-defined]


@needs_bash
def test_a_failing_xfconf_query_is_logged_and_skipped(tmp_path: Path):
    assert BASH is not None
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _fake_tool(fakebin, "xfconf-query", 'printf "xfconf-query %s\\n" "$*" >> "$FAKE_LOG"\nexit 1\n')
    cmdline_file = tmp_path / "cmdline"
    cmdline_file.write_text(LIVE, encoding="utf-8")
    env = dict(os.environ)
    env.update({"PATH": str(fakebin) + os.pathsep + env.get("PATH", ""), "FAKE_LOG": _msys(str(tmp_path / "calls.log")),
                "LINDOS_TEST_CMDLINE": _msys(str(cmdline_file)), "LINDOS_IS_LIVE_SESSION": str(IS_LIVE_SESSION)})
    env.pop("DISPLAY", None)
    proc = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, env=env)
    assert proc.returncode == 0
    assert "could not set xfce4-power-manager /xfce4-power-manager/presentation-mode" in proc.stderr
