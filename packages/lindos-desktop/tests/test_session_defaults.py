"""lindos-desktop: the xfconf defaults a session cannot start without really reach /etc/xdg.

The story (CI boot test of the ISO built from 7fc3aae, and the c020281 ISO it was compared with): xfce4-session ended in
"Unable to load a failsafe session - Unable to determine failsafe session name".  /etc/xdg/xfce4/xfconf/xfce-perchannel-xml
of the c020281 image held xfce4-session.xml.dpkg-dist (Lindos's file) and xfce4-session.xml.lindos-orig (the stock one)
but no xfce4-session.xml - and the same for xsettings.xml, xfce4-keyboard-shortcuts.xml and xfce4-power-manager.xml.  Each of
the four is a conffile of a stock package (xfce4-session, xfce4-settings, libxfce4ui-common, xfce4-power-manager) *and* was
a conffile of lindos-desktop: the preinst diverts the stock file away (--rename), dpkg takes over the stock package's
recorded hash for the new conffile, finds the file gone, and with --force-confold ("Keeping old config file as default")
leaves it deleted.  The Mint link /etc/xdg/xdg-xfce -> mint-artwork carried an xfce4-session.xml of its own, which hid it;
7fc3aae took that link away and the session lost its failsafe session definition.

Proven here, without dpkg or a Linux host: no conffile of a Lindos package is also a conffile of a stock Mint package (the
stock list is a snapshot of the Mint 22.2 image, data/stock-mint-22.2-conffiles.txt), the four files are shipped, diverted
and valid, xfce4-session.xml defines everything the failsafe session reads, and the postinst puts a file back from its
*.dpkg-dist copy when an older build's dpkg run left it missing.  What it cannot prove: dpkg's own behaviour (a Linux
package build + install does - the ISO build's 82-session-sanity.sh and the CI boot test).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
PACKAGES = PKG.parent
XFCONF = ROOT / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
STOCK = Path(__file__).resolve().parent / "data" / "stock-mint-22.2-conffiles.txt"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

# the four Lindos xfconf defaults that a stock package also ships as a conffile -> that package
SHARED = {
    "xfce4-session": "xfce4-session",
    "xsettings": "xfce4-settings",
    "xfce4-keyboard-shortcuts": "libxfce4ui-common",
    "xfce4-power-manager": "xfce4-power-manager",
}
PERCHANNEL = "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/"


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _stock() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in STOCK.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            path, pkg = line.split()
            out[path] = pkg
    return out


def _list(text: str, name: str) -> List[str]:
    m = re.search(r'^%s="\n(.*?)"' % re.escape(name), text, flags=re.M | re.S)
    assert m, name
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]


# --------------------------------------------------------------------------- the packaging rule
def test_the_snapshot_is_the_stock_image_and_knows_the_four_shared_files() -> None:
    stock = _stock()
    assert len(stock) > 100
    for channel, owner in SHARED.items():
        assert stock[PERCHANNEL + channel + ".xml"] == owner, channel


def test_no_conffile_of_a_lindos_package_is_also_a_conffile_of_a_stock_package() -> None:
    """The regression: as conffiles of lindos-desktop the four defaults were never installed (see the module docstring)."""
    stock = _stock()
    clashes = []
    for conffiles in sorted(PACKAGES.glob("*/DEBIAN/conffiles")):
        for path in conffiles.read_text(encoding="utf-8").split():
            if path in stock:
                clashes.append("%s: %s (a conffile of the stock package %s)" % (conffiles.parent.parent.name, path, stock[path]))
    assert not clashes, "\n".join(clashes)


def test_the_four_shared_defaults_are_plain_files_of_the_package_and_still_diverted() -> None:
    conffiles = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    pre = (DEBIAN / "preinst").read_text(encoding="utf-8")
    post = (DEBIAN / "postrm").read_text(encoding="utf-8")
    diverted_pre = _list(pre, "DIVERT_FILES")
    diverted_post = _list(post, "DIVERTED")
    for channel in SHARED:
        path = PERCHANNEL + channel + ".xml"
        assert (ROOT / path.lstrip("/")).is_file(), "%s must be shipped" % path
        assert path not in conffiles, "%s must not be a conffile (dpkg would keep the deleted stock file)" % path
        assert path in diverted_pre and path in diverted_post, "%s must stay diverted: the stock package owns it too" % path


def test_the_preinst_names_the_trap_so_nobody_makes_them_conffiles_again() -> None:
    pre = (DEBIAN / "preinst").read_text(encoding="utf-8")
    assert "Keeping old config file" in pre or "keeps the old file" in pre
    for channel in SHARED:
        assert channel + ".xml" in pre


# --------------------------------------------------------------------------- what the session reads
def _props(path: Path) -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}

    def walk(el: ET.Element, prefix: str) -> None:
        for child in el.findall("property"):
            p = prefix + "/" + child.get("name", "")
            found[p] = dict(child.attrib)
            walk(child, p)

    walk(ET.parse(str(path)).getroot(), "")
    return found


def test_every_shipped_channel_file_parses() -> None:
    files = sorted(XFCONF.glob("*.xml"))
    assert len(files) >= 10
    for f in files:
        assert ET.parse(str(f)).getroot().tag == "channel", f.name


def test_xfce4_session_xml_defines_what_the_failsafe_session_reads() -> None:
    """xfce4-session asks xfconf for /general/FailsafeSessionName, then loads /sessions/<that name> (xfsm-manager.c)."""
    props = _props(XFCONF / "xfce4-session.xml")
    assert props["/general/FailsafeSessionName"]["value"] == "Failsafe"
    assert "/sessions/Failsafe" in props
    count = int(props["/sessions/Failsafe/Count"]["value"])
    assert count >= 1
    for i in range(count):
        assert "/sessions/Failsafe/Client%d_Command" % i in props, i
    assert props["/sessions/Failsafe/IsFailsafe"]["value"] == "true"
    # the window manager, the settings daemon, the panel and the desktop are what the session is made of
    text = (XFCONF / "xfce4-session.xml").read_text(encoding="utf-8")
    for client in ("xfwm4", "xfsettingsd", "xfce4-panel", "xfdesktop"):
        assert 'value="%s"' % client in text, client


def test_xsettings_and_the_other_shared_files_are_channels_of_the_right_name() -> None:
    for channel in SHARED:
        root = ET.parse(str(XFCONF / (channel + ".xml"))).getroot()
        assert root.get("name") == channel, channel


# --------------------------------------------------------------------------- the postinst safety net
def _run_postinst(tmp: Path, root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LINDOS_ROOT"] = _posix(root)
    return subprocess.run([BASH, "-c", '. "%s"' % _posix(DEBIAN / "postinst"), "postinst", "configure"], capture_output=True,
                          text=True, env=env, timeout=120, cwd=str(tmp))


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@needs_bash
def test_postinst_puts_a_missing_default_back_from_its_dpkg_dist_copy(tmp_path: Path) -> None:
    """The state the c020281 image is in: the stock file is 'deleted', Lindos's copy sits next to it as *.dpkg-dist."""
    root = tmp_path / "root"
    d = root / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
    for channel in SHARED:
        _put(d / (channel + ".xml.dpkg-dist"), '<channel name="%s" version="1.0"><!-- lindos --></channel>\n' % channel)
        _put(d / (channel + ".xml.lindos-orig"), '<channel name="%s" version="1.0"><!-- stock --></channel>\n' % channel)
    res = _run_postinst(tmp_path, root)
    assert res.returncode == 0, res.stderr
    for channel in SHARED:
        got = (d / (channel + ".xml")).read_text(encoding="utf-8")
        assert "lindos" in got and "stock" not in got, channel
        assert (d / (channel + ".xml.lindos-orig")).is_file(), "the diverted original is left alone"
    assert res.stdout.count("restored") == 4, res.stdout


@needs_bash
def test_postinst_leaves_an_installed_default_alone_and_never_fails_on_a_missing_one(tmp_path: Path) -> None:
    root = tmp_path / "root"
    d = root / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
    _put(d / "xfce4-session.xml", "<channel name='xfce4-session'><!-- edited by the administrator --></channel>\n")
    _put(d / "xfce4-session.xml.dpkg-dist", "<channel name='xfce4-session'><!-- the packaged one --></channel>\n")
    res = _run_postinst(tmp_path, root)         # xsettings, shortcuts and power manager: nothing at all
    assert res.returncode == 0, res.stderr
    assert "administrator" in (d / "xfce4-session.xml").read_text(encoding="utf-8")
    assert not (d / "xsettings.xml").exists()
    assert "WARNING" in res.stdout and "xsettings.xml is missing" in res.stdout
    again = _run_postinst(tmp_path, root)
    assert again.returncode == 0 and "administrator" in (d / "xfce4-session.xml").read_text(encoding="utf-8")
