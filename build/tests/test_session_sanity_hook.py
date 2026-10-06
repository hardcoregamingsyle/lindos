"""build/chroot/82-session-sanity.sh: can the finished image start an XFCE session at all?

Why the hook exists: the CI boot test of the ISO built from 7fc3aae ended in xfce4-session's "Unable to load a failsafe
session - Unable to determine failsafe session name".  Four of Lindos's xfconf defaults (xfce4-session.xml among them) had
never been installed - dpkg kept the stock conffile of the same name as "deleted" and parked the Lindos copy as *.dpkg-dist
(the c020281 image has exactly that) - and Mint's /etc/xdg/xdg-xfce, which carries an xfce4-session.xml of its own, had
hidden it.  A hook that reads the finished image would have stopped the build.

The hook is run for real (bash + Python for the XML) against a fake root (LINDOS_MINT_ROOT) with a fake dpkg-query first in
PATH (lib.sh seam LINDOS_HOOK_PATH_PREFIX).  Proven: a complete image passes; every unambiguous problem fails the build with
a SESSION-FAIL line that says what is wrong - the missing xfce4-session.xml above all, with the *.dpkg-dist hint - while
stock files that do not parse, a symlinked /etc/xdg/xdg-xfce and missing optional packages are only warnings; the report-only
and off switches; the hook only reads.  Needs bash (skipped otherwise).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK_DIR = REPO_ROOT / "build" / "chroot"
HOOK = HOOK_DIR / "82-session-sanity.sh"
LIB = HOOK_DIR / "lib.sh"
LINDOS_XFCONF = REPO_ROOT / "packages" / "lindos-desktop" / "root" / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
LIGHTDM_CONF = REPO_ROOT / "packages" / "lindos-desktop" / "root" / "etc" / "lightdm" / "lightdm.conf.d" / "50-lindos.conf"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

PERCHANNEL = "etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
PACKAGES = ("xfce4-session xfwm4 xfce4-panel xfdesktop4 xfconf xfce4-settings lightdm slick-greeter xserver-xorg-core "
            "dbus-x11 dbus-user-session libpam-systemd network-manager plymouth libc6").split()
FILES = ["usr/bin/xfce4-session", "usr/bin/startxfce4", "usr/bin/xfwm4", "usr/bin/xfce4-panel", "usr/bin/xfdesktop",
         "usr/bin/xfsettingsd", "usr/lib/x86_64-linux-gnu/xfce4/xfconf/xfconfd", "usr/share/dbus-1/services/org.xfce.Xfconf.service",
         "usr/sbin/lightdm", "usr/lib/xorg/Xorg", "usr/share/xsessions/xfce.desktop", "usr/share/xgreeters/slick-greeter.desktop"]


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _w(root: Path, rel: str, text: str = "x\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def good_root(tmp: Path) -> Path:
    """An image that can start a session: the files, and the Lindos xfconf defaults exactly as the package ships them."""
    root = tmp / "root"
    for rel in FILES:
        _w(root, rel)
    for xml in sorted(LINDOS_XFCONF.glob("*.xml")):
        _w(root, "%s/%s" % (PERCHANNEL, xml.name), xml.read_text(encoding="utf-8"))
    _w(root, "etc/lightdm/lightdm.conf.d/50-lindos.conf", LIGHTDM_CONF.read_text(encoding="utf-8"))
    _w(root, "etc/xdg/xdg-xfce/README", "Lindos: deliberately empty\n")
    return root


class Sanity:
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.proc = proc
        self.err = proc.stderr
        self.fails = re.findall(r"SESSION-FAIL \[(\w+)\] (.*)", proc.stderr)
        self.warns = re.findall(r"SESSION-WARN \[(\w+)\] (.*)", proc.stderr)
        self.notes = re.findall(r"SESSION-NOTE \[(\w+)\] (.*)", proc.stderr)

    def fail_text(self) -> str:
        return "\n".join(t for _, t in self.fails)

    def warn_text(self) -> str:
        return "\n".join(t for _, t in self.warns)


def run_hook(tmp: Path, root: Path, *, packages: Optional[List[str]] = None, mode: Optional[str] = None,
             python: Optional[str] = None) -> Sanity:
    assert BASH is not None
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    listing = "".join("installed %s\n" % n for n in (PACKAGES if packages is None else packages))
    (tmp / "pkgs.txt").write_bytes(listing.encode("utf-8"))
    dq = fake / "dpkg-query"
    dq.write_bytes(('#!/bin/sh\ncat "%s"\n' % _posix(tmp / "pkgs.txt")).encode("utf-8"))
    os.chmod(dq, 0o755)
    config = tmp / "config.env"
    config.write_bytes(b"# empty on purpose\n")
    env = dict(os.environ)
    env.update({"LINDOS_MINT_ROOT": _posix(root), "LINDOS_HOOK_PATH_PREFIX": _posix(fake), "LINDOS_STAGE_DIR": _posix(tmp / "nostage"),
                "LINDOS_CONFIG_ENV": _posix(config), "LINDOS_PYTHON": python or _posix(Path(sys.executable))})
    env.pop("LINDOS_SESSION_SANITY", None)
    if mode is not None:
        env["LINDOS_SESSION_SANITY"] = mode
    proc = subprocess.run([BASH, "-Eeuo", "pipefail", _posix(HOOK)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, check=False, env=env)
    return Sanity(proc)


# --------------------------------------------------------------------------- structure
def test_hook_house_style_and_position() -> None:
    raw = HOOK.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw
    t = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in t and "hook_begin" in t and "hook_end" in t
    assert re.search(r'^\. "\$\(dirname "\$\(readlink -f "\$0"\)"\)/lib\.sh"$', t, flags=re.M)
    assert re.search(r"^\s*(sudo|pkexec)\s", t, flags=re.M) is None
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t and "die " in t, "must refuse to run on a build host"
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    for h in ("76-mint-purge.sh", "77-mint-sweep.sh", "79-installer-flow.sh", "80-cleanup.sh", "81-unrecognisable-gate.sh"):
        assert names.index(h) < names.index(HOOK.name), f"the check must see the image after {h}"
    assert names[-1] == HOOK.name, "nothing runs after the session check"


def test_hook_only_reads() -> None:
    body = "\n".join(ln for ln in _text(HOOK).splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in (" rm ", " mv ", " cp ", "sed -i", "apt-get", "apt-mark", "dpkg-divert", "chmod", "systemctl", "tee ", "> \"${"):
        assert forbidden not in body, forbidden


def test_the_rules_stay_in_step_with_lib_and_the_package() -> None:
    hook = _text(HOOK)
    lib = _text(LIB)
    session = re.search(r"^SESSION_PKGS=\((.*?)\)$", lib, flags=re.M | re.S).group(1).split()
    required = re.search(r'^REQUIRED_PKGS="(.*)"$', hook, flags=re.M).group(1).split()
    assert required and set(required) <= set(session), "what 82 requires, 76 must keep (lib.sh SESSION_PKGS)"
    channels = re.search(r'^LINDOS_CHANNELS="(.*)"$', hook, flags=re.M).group(1).split()
    shipped = sorted(p.stem for p in LINDOS_XFCONF.glob("*.xml"))
    assert sorted(channels) == shipped, "every channel lindos-desktop ships is required in the image"
    for pkg in required:
        assert pkg in PACKAGES


def test_the_docs_name_the_hook_and_its_switch() -> None:
    building = _text(REPO_ROOT / "docs" / "BUILDING.md")
    assert "82-session-sanity.sh" in building and "LINDOS_SESSION_SANITY" in building and "SESSION-FAIL" in building


# --------------------------------------------------------------------------- behaviour
@needs_bash
def test_a_complete_image_passes_and_names_the_failsafe_session(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    s = run_hook(tmp_path, root)
    assert s.proc.returncode == 0, s.err
    assert s.fails == [] and "SESSION-SANITY failures=0" in s.err and "mode=strict" in s.err
    assert any("FailsafeSessionName=Failsafe" in t and "xfce4-session.xml" in t for _, t in s.notes), s.err
    assert s.warns == [], s.err


@needs_bash
def test_the_c020281_state_a_missing_xfce4_session_xml_fails_the_build_and_says_why(tmp_path: Path) -> None:
    """The regression: xfce4-session.xml only as *.dpkg-dist (Lindos's) and *.lindos-orig (the stock one)."""
    root = good_root(tmp_path)
    d = root / PERCHANNEL
    (d / "xfce4-session.xml").rename(d / "xfce4-session.xml.dpkg-dist")
    _w(root, PERCHANNEL + "/xfce4-session.xml.lindos-orig", "<channel name='xfce4-session'/>\n")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0, s.err
    text = s.fail_text()
    assert "xfce4-session.xml is missing" in text and "xfce4-session.xml.dpkg-dist exists" in text
    assert "xfce4-session.xml.lindos-orig exists" in text and "kept another package's conffile" in text
    assert "/general/FailsafeSessionName is not defined" in text and "Unable to determine failsafe session name" in text
    assert "SESSION-SANITY failures=2" in s.err and "the image cannot start an XFCE session" in s.err


@needs_bash
@pytest.mark.parametrize("channel", ["xfce4-session", "xsettings", "xfce4-keyboard-shortcuts", "xfce4-power-manager", "xfwm4",
                                     "xfce4-panel", "xfce4-desktop", "thunar", "keyboards", "xfce4-notifyd"])
def test_every_lindos_channel_file_is_required(tmp_path: Path, channel: str) -> None:
    root = good_root(tmp_path)
    (root / PERCHANNEL / (channel + ".xml")).unlink()
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and "%s.xml is missing" % channel in s.fail_text(), s.err


@needs_bash
def test_mints_xdg_xfce_does_not_make_up_for_a_missing_lindos_file(tmp_path: Path) -> None:
    """On the c020281 image /etc/xdg/xdg-xfce -> mint-artwork supplied an xfce4-session.xml, so the session worked while the
    Lindos file was missing.  The Lindos file is required in its own right, and the directory is reported."""
    root = good_root(tmp_path)
    (root / PERCHANNEL / "xfce4-session.xml").unlink()
    mint = Path(root / "etc/xdg/xdg-xfce/xfce4/xfconf/xfce-perchannel-xml")
    mint.mkdir(parents=True)
    (mint / "xfce4-session.xml").write_bytes((LINDOS_XFCONF / "xfce4-session.xml").read_bytes())
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and "xfce4-session.xml is missing" in s.fail_text()
    assert "/general/FailsafeSessionName is not defined" not in s.fail_text(), "the Mint directory does define it"
    assert "carries 1 xfconf default(s) of its own" in s.warn_text()


@needs_bash
def test_the_failsafe_session_must_be_defined_and_have_something_to_start(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    f = root / PERCHANNEL / "xfce4-session.xml"
    good = f.read_text(encoding="utf-8")
    f.write_bytes(good.replace('<property name="FailsafeSessionName" type="string" value="Failsafe"/>', "").encode("utf-8"))
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and "/general/FailsafeSessionName is not defined" in s.fail_text()
    f.write_bytes(good.replace('<property name="FailsafeSessionName" type="string" value="Failsafe"/>',
                               '<property name="FailsafeSessionName" type="string" value="Rescue"/>').encode("utf-8"))
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and "/sessions/Rescue is not defined" in s.fail_text()


@needs_bash
def test_an_xml_that_does_not_parse_fails_for_a_lindos_channel_and_warns_for_a_stock_one(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    (root / PERCHANNEL / "xsettings.xml").write_bytes(b"<channel name='xsettings'><property></channel>\n")
    _w(root, PERCHANNEL + "/thunar-volman.xml", "<channel name='thunar-volman'")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0
    assert "xsettings.xml does not parse" in s.fail_text()
    assert "thunar-volman.xml does not parse" in s.warn_text() and "thunar-volman" not in s.fail_text()
    # a broken xfce4-session.xml says so, and the failsafe session is then undefined as well
    second = tmp_path / "second"
    second.mkdir()
    root2 = good_root(second)
    (root2 / PERCHANNEL / "xfce4-session.xml").write_bytes(b"<channel")
    s2 = run_hook(second, root2)
    assert "xfce4-session.xml does not parse" in s2.fail_text() and "/general/FailsafeSessionName is not defined" in s2.fail_text()


@needs_bash
def test_a_stock_xml_that_does_not_parse_alone_never_fails_the_build(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    _w(root, PERCHANNEL + "/thunar-volman.xml", "<channel name='thunar-volman'")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode == 0, s.err
    assert s.fails == [] and "thunar-volman.xml does not parse" in s.warn_text()


@needs_bash
def test_files_the_dpkg_list_of_lindos_desktop_names_are_required_too(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    _w(root, "var/lib/dpkg/info/lindos-desktop.list", "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-newchannel.xml\n"
       "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml\n/usr/share/lindos/x\n")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and "xfce4-newchannel.xml is missing" in s.fail_text()
    assert "xfwm4.xml is missing" not in s.fail_text()


@needs_bash
@pytest.mark.parametrize("missing", ["xfce4-session", "xfwm4", "xfce4-panel", "xfdesktop4", "xfconf", "xfce4-settings", "lightdm"])
def test_a_missing_session_package_fails(tmp_path: Path, missing: str) -> None:
    s = run_hook(tmp_path, good_root(tmp_path), packages=[p for p in PACKAGES if p != missing])
    assert s.proc.returncode != 0 and "%s is not installed" % missing in s.fail_text(), s.err


@needs_bash
def test_the_session_needs_a_bus_a_greeter_and_an_x_server(tmp_path: Path) -> None:
    s = run_hook(tmp_path, good_root(tmp_path), packages=[p for p in PACKAGES if p not in ("dbus-x11", "dbus-user-session")])
    assert s.proc.returncode != 0 and "neither dbus-x11 nor dbus-user-session" in s.fail_text()
    for one in ("dbus-x11", "dbus-user-session"):
        t = tmp_path / one
        t.mkdir()
        ok = run_hook(t, good_root(t), packages=[p for p in PACKAGES if p != one])
        assert ok.proc.returncode == 0, ok.err           # either one will do
    t = tmp_path / "greeter"
    t.mkdir()
    s = run_hook(t, good_root(t), packages=[p for p in PACKAGES if p != "slick-greeter"])
    assert s.proc.returncode != 0 and "no LightDM greeter" in s.fail_text()
    t = tmp_path / "xorg"
    t.mkdir()
    s = run_hook(t, good_root(t), packages=[p for p in PACKAGES if p != "xserver-xorg-core"])
    assert s.proc.returncode != 0 and "no X server" in s.fail_text()
    t = tmp_path / "other"
    t.mkdir()
    ok = run_hook(t, good_root(t), packages=[p for p in PACKAGES if p != "xserver-xorg-core"] + ["xorg"])
    assert ok.proc.returncode == 0, ok.err


@needs_bash
def test_optional_packages_are_only_warnings(tmp_path: Path) -> None:
    s = run_hook(tmp_path, good_root(tmp_path), packages=[p for p in PACKAGES if p not in ("libpam-systemd", "network-manager", "plymouth")])
    assert s.proc.returncode == 0, s.err
    assert s.fails == [] and len(s.warns) == 3


@needs_bash
@pytest.mark.parametrize("rel, needle", [
    ("usr/lib/x86_64-linux-gnu/xfce4/xfconf/xfconfd", "the xfconfd binary"),
    ("usr/share/dbus-1/services/org.xfce.Xfconf.service", "D-Bus activation file"),
    ("usr/bin/xfsettingsd", "xfsettingsd"),
    ("usr/bin/xfce4-session", "xfce4-session is missing"),
    ("usr/bin/xfdesktop", "xfdesktop is missing"),
    ("usr/bin/xfwm4", "xfwm4 is missing"),
    ("usr/bin/xfce4-panel", "xfce4-panel is missing"),
    ("usr/sbin/lightdm", "lightdm is missing"),
    ("usr/lib/xorg/Xorg", "the X server is missing"),
    ("usr/share/xsessions/xfce.desktop", "xfce.desktop that lightdm starts"),
    ("usr/share/xgreeters/slick-greeter.desktop", "slick-greeter.desktop"),
])
def test_a_missing_binary_or_session_entry_fails(tmp_path: Path, rel: str, needle: str) -> None:
    root = good_root(tmp_path)
    (root / rel).unlink()
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0 and needle in s.fail_text(), s.err


@needs_bash
def test_the_xfconfd_binary_may_live_in_any_of_the_usual_places(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    (root / "usr/lib/x86_64-linux-gnu/xfce4/xfconf/xfconfd").unlink()
    _w(root, "usr/lib/aarch64-linux-gnu/xfce4/xfconf/xfconfd")
    assert run_hook(tmp_path, root).proc.returncode == 0
    t = tmp_path / "libexec"
    t.mkdir()
    root = good_root(t)
    (root / "usr/lib/x86_64-linux-gnu/xfce4/xfconf/xfconfd").unlink()
    _w(root, "usr/libexec/xfce4/xfconf/xfconfd")
    assert run_hook(t, root).proc.returncode == 0


@needs_bash
def test_the_session_name_comes_from_the_lightdm_configuration(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    (root / "etc/lightdm/lightdm.conf.d/50-lindos.conf").write_bytes(b"[Seat:*]\nuser-session=lindos\ngreeter-session=lightdm-gtk-greeter\n")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode != 0
    assert "lindos.desktop that lightdm starts (user-session=lindos)" in s.fail_text()
    assert "lightdm-gtk-greeter.desktop (greeter-session=lightdm-gtk-greeter)" in s.fail_text()


@needs_bash
def test_xdg_xfce_as_a_symlink_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    shutil.rmtree(root / "etc/xdg/xdg-xfce")
    target = root / "usr/share/mint-artwork/xfce"
    target.mkdir(parents=True)
    try:
        os.symlink(str(target), str(root / "etc/xdg/xdg-xfce"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    s = run_hook(tmp_path, root)
    assert s.proc.returncode == 0, s.err
    assert s.fails == [] and "xdg-xfce is a symlink" in s.warn_text()


@needs_bash
def test_report_mode_never_fails_and_off_skips_the_hook(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    (root / PERCHANNEL / "xfce4-session.xml").unlink()
    rep = run_hook(tmp_path, root, mode="report")
    assert rep.proc.returncode == 0, rep.err
    assert "xfce4-session.xml is missing" in rep.fail_text() and "mode=report-only" in rep.err
    t = tmp_path / "off"
    t.mkdir()
    off = run_hook(t, root, mode="0")
    assert off.proc.returncode == 0 and "switched off" in off.err and "SESSION-SANITY" not in off.err


@needs_bash
def test_without_python_the_xml_checks_are_skipped_with_a_warning(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    (root / PERCHANNEL / "xfce4-session.xml").unlink()
    s = run_hook(tmp_path, root, python="no-such-python-interpreter")
    assert s.proc.returncode == 0, s.err
    assert "is not available: the xfconf XML checks are skipped" in s.warn_text()


@needs_bash
def test_the_hook_changes_nothing(tmp_path: Path) -> None:
    root = good_root(tmp_path)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    run_hook(tmp_path, root)
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------- 30-lindos-debs.sh: the early check after the install
DEBS_HOOK = HOOK_DIR / "30-lindos-debs.sh"


def _early_section() -> str:
    m = re.search(r"^# The xfconf defaults of lindos-desktop.*?^fi\n", _text(DEBS_HOOK), flags=re.M | re.S)
    assert m, "the xfconf defaults check of 30-lindos-debs.sh was not found"
    return m.group(0)


def _run_early(tmp: Path, xfconf_dir: Path, *, installed: bool = True) -> subprocess.CompletedProcess:
    assert BASH is not None
    script = tmp / "early.sh"
    stub = ('log() { echo "log: $*"; }\ndie() { echo "die: $*" >&2; exit 1; }\n'
            'pkg_installed() { [ "$1" = lindos-desktop ] && [ "%s" = yes ]; }\n' % ("yes" if installed else "no"))
    script.write_bytes((stub + _early_section()).encode("utf-8"))
    env = dict(os.environ)
    env["LINDOS_XFCONF_DIR"] = _posix(xfconf_dir)
    return subprocess.run([BASH, _posix(script)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                          check=False, env=env)


@needs_bash
def test_the_build_stops_right_after_the_install_when_a_shared_default_is_missing(tmp_path: Path) -> None:
    d = tmp_path / "xfconf"
    for ch in ("xfce4-session", "xsettings", "xfce4-keyboard-shortcuts", "xfce4-power-manager"):
        _w(d, ch + ".xml", "<channel/>\n")
    ok = _run_early(tmp_path, d)
    assert ok.returncode == 0 and "xfconf defaults are in place" in ok.stdout, ok.stderr
    (d / "xfce4-session.xml").rename(d / "xfce4-session.xml.dpkg-dist")          # the c020281 state
    bad = _run_early(tmp_path, d)
    assert bad.returncode == 1 and "xfce4-session.xml is missing after the install" in bad.stderr and "Session sanity" in bad.stderr
    (d / "xfce4-session.xml.dpkg-dist").rename(d / "xfce4-session.xml")
    (d / "xsettings.xml").write_bytes(b"")                                         # empty is as good as missing
    assert _run_early(tmp_path, d).returncode == 1


@needs_bash
def test_the_early_check_is_skipped_when_lindos_desktop_is_not_installed(tmp_path: Path) -> None:
    res = _run_early(tmp_path, tmp_path / "nothing", installed=False)
    assert res.returncode == 0 and res.stdout == "", res.stderr


def test_the_early_check_names_the_same_four_files_as_the_package() -> None:
    section = _early_section()
    names = re.search(r"for ch in ([a-z0-9 -]+); do", section).group(1).split()
    assert sorted(names) == ["xfce4-keyboard-shortcuts", "xfce4-power-manager", "xfce4-session", "xsettings"]
    post = _text(REPO_ROOT / "packages" / "lindos-desktop" / "DEBIAN" / "postinst")
    assert re.search(r"for ch in %s; do" % " ".join(names), post)
