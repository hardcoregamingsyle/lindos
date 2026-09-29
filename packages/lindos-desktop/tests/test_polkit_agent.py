"""The graphical polkit-agent autostart: pkexec (every Lindos password prompt) needs an authentication
agent in the session to show a dialog; without one it falls back to a text prompt.

Covers the autostart entry, the Recommends, the guarantee that nothing here grants privileges without a
password, and the wrapper script's behaviour (pgrep and the agents are faked, nothing real is started).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
SCRIPT = ROOT / "usr" / "libexec" / "lindos" / "polkit-agent-start.sh"
DESKTOP = ROOT / "etc" / "xdg" / "autostart" / "lindos-polkit-agent.desktop"

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _entries(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln and not ln.startswith("#"))


def test_autostart_entry_starts_the_wrapper_in_xfce_only():
    entry = _entries(DESKTOP)
    assert entry["Type"] == "Application"
    assert entry["Exec"] == "/usr/libexec/lindos/polkit-agent-start.sh" == entry["TryExec"]
    assert entry["OnlyShowIn"] == "XFCE;"
    assert entry["NoDisplay"] == "true" and entry["Terminal"] == "false"
    assert entry["Name"] and entry["Comment"] and entry["Icon"]
    assert "/" + DESKTOP.relative_to(ROOT).as_posix() in (DEBIAN / "conffiles").read_text(encoding="utf-8").split()


def test_a_graphical_agent_is_recommended_not_required():
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    fields = dict(re.findall(r"^([A-Z][A-Za-z-]+): (.*)$", control, flags=re.M))
    assert "policykit-1-gnome | mate-polkit | lxpolkit" in [r.strip() for r in fields["Recommends"].split(",")]
    depends = {d.strip().split()[0] for d in fields["Depends"].split(",")}
    assert not depends & {"policykit-1-gnome", "mate-polkit", "lxpolkit"}      # best effort, never breaks install


def test_nothing_in_the_package_authorises_without_a_password():
    """The design stays 'one password prompt, in a proper dialog': no polkit rule/policy is shipped here."""
    assert not list(ROOT.rglob("*.rules")) and not (ROOT / "etc" / "polkit-1").exists()
    assert not (ROOT / "usr" / "share" / "polkit-1").exists()
    for text in (SCRIPT.read_text(encoding="utf-8"), DESKTOP.read_text(encoding="utf-8")):
        assert not re.search(r"allow_any|allow_active|auth_self|\byes\b.*polkit", text)


def test_script_conventions():
    raw = SCRIPT.read_bytes()
    assert b"\r" not in raw and raw.startswith(b"#!/bin/bash\n")
    text = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in text
    assert re.search(r"^\s*(sudo|pkexec)\s", text, flags=re.M) is None
    for candidate in ("/usr/lib/policykit-1-gnome/polkit-gnome-authentication-agent-1",
                      "/usr/bin/lxpolkit", "polkit-mate-authentication-agent-1"):
        assert candidate in text


# --- behaviour -----------------------------------------------------------------------------------
def _sandbox(tmp_path: Path, *, pgrep_running: bool) -> Path:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    pgrep = fakebin / "pgrep"
    pgrep.write_text("#!/bin/sh\necho \"$@\" > ./pgrep-args\nexit %d\n" % (0 if pgrep_running else 1),
                     encoding="utf-8", newline="\n")
    os.chmod(pgrep, 0o755)
    for name in ("good", "second"):
        agent = tmp_path / name
        agent.write_text("#!/bin/sh\necho \"%s $*\" > ./started\n" % name, encoding="utf-8", newline="\n")
        os.chmod(agent, 0o755)
    plain = tmp_path / "plain-file"
    plain.write_text("not executable\n", encoding="utf-8", newline="\n")
    os.chmod(plain, 0o644)
    return fakebin


def _run(tmp_path: Path, fakebin: Path, *args: str, agents: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = fakebin.as_posix() + os.pathsep + env.get("PATH", "")
    env["LINDOS_POLKIT_AGENTS"] = agents
    env["LINDOS_POLKIT_AGENT_PATTERN"] = "lindos-test-agent-pattern"
    return subprocess.run([BASH, SCRIPT.as_posix(), *args], cwd=tmp_path, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60, check=False)


@needs_bash
def test_starts_the_first_installed_agent_and_becomes_it(tmp_path):
    fakebin = _sandbox(tmp_path, pgrep_running=False)
    agents = "./does-not-exist:./plain-file:./good:./second"
    printed = _run(tmp_path, fakebin, "--print", agents=agents)
    assert printed.returncode == 0 and printed.stdout.strip() == "./good"
    assert not (tmp_path / "started").exists()               # --print never starts anything
    proc = _run(tmp_path, fakebin, agents=agents)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "started").read_text(encoding="utf-8").strip() == "good"      # exec'd the agent (first hit)
    assert "starting ./good" in proc.stderr
    assert "lindos-test-agent-pattern" in (tmp_path / "pgrep-args").read_text(encoding="utf-8")


@needs_bash
def test_does_nothing_when_an_agent_is_already_running(tmp_path):
    fakebin = _sandbox(tmp_path, pgrep_running=True)
    proc = _run(tmp_path, fakebin, agents="./good")
    assert proc.returncode == 0 and not (tmp_path / "started").exists()
    assert _run(tmp_path, fakebin, "--print", agents="./good").stdout.strip() == "running"


@needs_bash
def test_no_agent_installed_says_so_and_exits_zero(tmp_path):
    fakebin = _sandbox(tmp_path, pgrep_running=False)
    proc = _run(tmp_path, fakebin, agents="./nope:./plain-file")
    assert proc.returncode == 0 and not (tmp_path / "started").exists()
    assert "no graphical polkit authentication agent installed" in proc.stderr
    assert _run(tmp_path, fakebin, "--print", agents="./nope").stdout.strip() == "none"


@needs_bash
def test_without_pgrep_the_wrapper_assumes_no_agent_is_running(tmp_path):
    fakebin = _sandbox(tmp_path, pgrep_running=True)
    (fakebin / "pgrep").unlink()
    proc = _run(tmp_path, fakebin, "--print", agents="./good")
    # a real pgrep (Linux CI) finds nothing matching the fake pattern either way
    assert proc.returncode == 0 and proc.stdout.strip() == "./good"


@needs_bash
def test_bad_flags_never_fail_the_session_and_help_explains(tmp_path):
    fakebin = _sandbox(tmp_path, pgrep_running=False)
    bad = _run(tmp_path, fakebin, "--bogus", agents="./good")
    assert bad.returncode == 0 and "unknown argument" in bad.stderr and not (tmp_path / "started").exists()
    helped = _run(tmp_path, fakebin, "--help", agents="./good")
    assert helped.returncode == 0 and "graphical polkit authentication agent" in helped.stdout
