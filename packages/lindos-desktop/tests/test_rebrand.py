"""The Mint sweep: /usr/libexec/lindos/rebrand-base.py + base-sweep.json + the files that wire it in.

Lindos is a remaster of Linux Mint; the base packages ship menu entries, autostarts, release files and
browser defaults that say "Linux Mint".  These tests run the real sweep against fake roots (sandboxed
through --root, nothing host-dependent) and cover:

  * .desktop rewriting: hide (NoDisplay / Hidden) inside the right group, icon swaps, "Linux Mint" text in
    every locale, entries that only open linuxmint.com pages, skipped prefixes, idempotence, backup + revert;
  * KEY=value files (lsb-release, linuxmint/info, casper.conf): display fields only, quoting kept;
  * Firefox homepage prefs / policies / distribution.ini;
  * the whole thing through apply-branding.sh (bash) exactly as the postinst and the apt hook call it;
  * the shipped data + package wiring (conffiles, apt hook, GRUB drop-in, skel autostart override, icons).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict

import pytest

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
REPO = PKG.parent.parent
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SCRIPT = LIBEXEC / "rebrand-base.py"
APPLY = LIBEXEC / "apply-branding.sh"
DATA = ROOT / "usr" / "share" / "lindos" / "branding" / "base-sweep.json"
FRAGMENT = ROOT / "usr" / "share" / "lindos" / "os-release.d" / "lindos.conf"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _load():
    spec = importlib.util.spec_from_file_location("lindos_rebrand_base", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load()


# --------------------------------------------------------------------------- sample base files
MINTWELCOME = """[Desktop Entry]
Name=Welcome Screen
Name[fr]=Ecran de bienvenue
Comment=Introduction to Linux Mint
Comment[de]=Einfuehrung in Linux Mint
Exec=mintwelcome
Icon=mintwelcome
Type=Application
Categories=GNOME;GTK;Utility;
"""
MINTINSTALL = """[Desktop Entry]
Name=Software Manager
Comment=Install, remove and upgrade software packages
Exec=mintinstall %U
Icon=mintinstall
Type=Application
Categories=System;PackageManager;

[Desktop Action refresh]
Name=Refresh
Exec=mintinstall --refresh
"""
MINTUPDATE = """[Desktop Entry]
Name=Update Manager
Name[de]=Aktualisierungsverwaltung
Exec=mintupdate-launcher
Icon=mintupdate
Type=Application
"""
MINTDRIVERS = """[Desktop Entry]
Name=Driver Manager
Exec=pkexec mintdrivers
Icon=mintdrivers
Type=Application
"""
MINT_DOCS = """[Desktop Entry]
Name=Documentation
Comment=Linux Mint user guide
Exec=xdg-open https://www.linuxmint.com/documentation.php
Icon=help-browser
Type=Application
"""
MINT_WORDING = """[Desktop Entry]
Name=Backup Tool
Name[fr]=Sauvegarde de Linux Mint
Comment=Back up your Linux Mint home folder
Keywords=backup;linux mint;
Exec=mintbackup
Icon=mintbackup
Type=Application

[Desktop Action restore]
Name=Restore a Linux Mint backup
Exec=mintbackup --restore
"""
FIREFOX_DESKTOP = """[Desktop Entry]
Name=Firefox Web Browser
Exec=firefox %u
Icon=firefox
Type=Application
"""
LINDOS_OWN = """[Desktop Entry]
Name=Lindos thing
Comment=mentions Linux Mint on purpose (Lindos-owned files are never rewritten)
Exec=lindos-thing
Type=Application
"""
UBIQUITY = """[Desktop Entry]
Name=Install Lindos
Comment=Installer that once said Linux Mint
Exec=ubiquity gtk_ui
Type=Application
"""
WELCOME_AUTOSTART = """[Desktop Entry]
Name=Welcome Screen
Exec=mintwelcome-launcher
Icon=mintwelcome
Type=Application
X-GNOME-Autostart-enabled=true
NotShowIn=KDE;

[Desktop Action later]
Name=Later
Exec=true
"""
WELCOME_OTHER_NAME = """[Desktop Entry]
Name=Whatever
Exec=/usr/lib/linuxmint/mintWelcome/mintwelcome.py
Type=Application
"""
BLUEMAN = """[Desktop Entry]
Name=Blueman Applet
Exec=blueman-applet
Type=Application
"""
LSB = """DISTRIB_ID=LinuxMint
DISTRIB_RELEASE=22.2
DISTRIB_CODENAME=zara
DISTRIB_DESCRIPTION="Linux Mint 22.2 Zara"
"""
MINT_INFO = """RELEASE=22.2
CODENAME=zara
EDITION="Xfce"
DESCRIPTION="Linux Mint 22.2 Zara"
DESKTOP=Gnome
TOOLKIT=GTK
NEW_FEATURES_URL=https://www.linuxmint.com/rel_zara_xfce_whatsnew.php
GRUB_TITLE=Linux Mint 22.2 Xfce
"""
CASPER = """# This file should go in /etc/casper.conf
export USERNAME="mint"
export USERFULLNAME="Live session user"
export HOST="mint"
export BUILD_SYSTEM="Ubuntu"
export FLAVOUR="Linux Mint"
"""
OS_RELEASE = """NAME="Linux Mint"
VERSION="22.2 (Zara)"
ID=linuxmint
ID_LIKE="ubuntu debian"
PRETTY_NAME="Linux Mint 22.2"
VERSION_ID="22.2"
HOME_URL="https://www.linuxmint.com/"
SUPPORT_URL="https://forums.linuxmint.com/"
BUG_REPORT_URL="http://linuxmint-troubleshooting-guide.readthedocs.io/en/latest/"
VERSION_CODENAME=zara
UBUNTU_CODENAME=noble
"""


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _get(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _seed_base(root: Path) -> Dict[str, str]:
    files = {
        "usr/share/applications/mintwelcome.desktop": MINTWELCOME,
        "usr/share/applications/mintinstall.desktop": MINTINSTALL,
        "usr/share/applications/mintupdate.desktop": MINTUPDATE,
        "usr/share/applications/mintdrivers.desktop": MINTDRIVERS,
        "usr/share/applications/mint-docs.desktop": MINT_DOCS,
        "usr/share/applications/mintbackup.desktop": MINT_WORDING,
        "usr/share/applications/firefox.desktop": FIREFOX_DESKTOP,
        "usr/share/applications/lindos-thing.desktop": LINDOS_OWN,
        "usr/share/applications/ubiquity.desktop": UBIQUITY,
        "etc/xdg/autostart/mintwelcome.desktop": WELCOME_AUTOSTART,
        "etc/xdg/autostart/zz-other.desktop": WELCOME_OTHER_NAME,
        "etc/xdg/autostart/blueman.desktop": BLUEMAN,
        "etc/lsb-release": LSB,
        "etc/linuxmint/info": MINT_INFO,
        "etc/casper.conf": CASPER,
    }
    for rel, text in files.items():
        _put(root, rel, text)
    return files


def _sweep(root: Path, *extra: str) -> int:
    return rb.main(["--root", str(root), "--data", str(DATA), "--fragment", str(FRAGMENT), "--quiet", *extra])


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------- .desktop entries
def test_menu_duplicates_are_hidden_inside_the_main_group(tmp_path):
    _seed_base(tmp_path)
    assert _sweep(tmp_path) == 0
    welcome = _get(tmp_path, "usr/share/applications/mintwelcome.desktop")
    assert re.search(r"^NoDisplay=true$", welcome, flags=re.M)
    assert rb.get_key(welcome, "Exec") == "mintwelcome"                 # the tool itself is untouched
    install = _get(tmp_path, "usr/share/applications/mintinstall.desktop")
    head, _, actions = install.partition("[Desktop Action refresh]")
    assert "NoDisplay=true" in head and "NoDisplay" not in actions      # not appended to the trailing group
    assert rb.get_key(install, "Exec") == "mintinstall %U"


def test_mint_tools_that_stay_get_lindos_names_and_icons_but_keep_their_command(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    upd = _get(tmp_path, "usr/share/applications/mintupdate.desktop")
    drv = _get(tmp_path, "usr/share/applications/mintdrivers.desktop")
    assert rb.get_key(upd, "Icon") == "lindos-update" and "NoDisplay" not in upd
    assert rb.get_key(drv, "Icon") == "lindos-drivers" and "NoDisplay" not in drv
    assert rb.get_key(upd, "Name") == "Lindos Updates" and rb.get_key(drv, "Name") == "Lindos Drivers"
    assert "Name[de]" not in upd, "a stale translation would still say Update Manager"
    assert rb.get_key(upd, "Exec") == "mintupdate-launcher"
    assert rb.get_key(drv, "Exec") == "pkexec mintdrivers"


def test_linux_mint_text_becomes_lindos_in_every_locale_and_group(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    text = _get(tmp_path, "usr/share/applications/mintbackup.desktop")
    assert "Linux Mint" not in text
    assert "Name[fr]=Sauvegarde de Lindos" in text
    assert "Comment=Back up your Lindos home folder" in text
    assert "Keywords=backup;Lindos;" in text
    assert "Name=Restore a Lindos backup" in text                       # inside the [Desktop Action] group too
    assert "Exec=mintbackup --restore" in text                            # names of tools are not text
    assert "NoDisplay=true" in text and "Icon=lindos-settings" in text     # the tool is hidden and its icon is not Mint's


def test_entries_that_only_open_linux_mint_pages_are_hidden(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    docs = _get(tmp_path, "usr/share/applications/mint-docs.desktop")
    assert "NoDisplay=true" in docs and "Comment=Lindos user guide" in docs


def test_unrelated_and_lindos_owned_entries_are_left_byte_for_byte(tmp_path):
    files = _seed_base(tmp_path)
    _sweep(tmp_path)
    for rel in ("usr/share/applications/firefox.desktop", "usr/share/applications/lindos-thing.desktop",
                "usr/share/applications/ubiquity.desktop", "etc/xdg/autostart/blueman.desktop"):
        assert _get(tmp_path, rel) == files[rel], rel


def test_autostart_mint_welcome_is_hidden_whatever_the_file_is_called(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    for rel in ("etc/xdg/autostart/mintwelcome.desktop", "etc/xdg/autostart/zz-other.desktop"):
        text = _get(tmp_path, rel)
        assert re.search(r"^Hidden=true$", text, flags=re.M), rel
        assert "NoDisplay" not in text, rel                              # autostart uses Hidden
    first = _get(tmp_path, "etc/xdg/autostart/mintwelcome.desktop")
    assert "Hidden=true" in first.partition("[Desktop Action later]")[0]


def test_a_second_run_changes_nothing(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    before = _snapshot(tmp_path)
    ctx = rb.Ctx(str(tmp_path), quiet=True)
    rules = json.loads(DATA.read_text(encoding="utf-8"))
    rb.run_steps(ctx, rules, rb.load_brand(ctx, str(FRAGMENT)), list(rb.STEPS))
    assert ctx.changes == 0
    assert _snapshot(tmp_path) == before


def test_every_changed_entry_is_marked_and_backed_up_then_reverted_exactly(tmp_path):
    files = _seed_base(tmp_path)
    _sweep(tmp_path)
    marked = [rel for rel in files if rel.endswith(".desktop") and rb.is_marked(_get(tmp_path, rel))]
    assert "usr/share/applications/mintwelcome.desktop" in marked and "usr/share/applications/firefox.desktop" not in marked
    orig = tmp_path / "var" / "lib" / "lindos" / "rebrand" / "orig"
    assert len(list(orig.iterdir())) >= len(marked)
    assert rb.main(["--root", str(tmp_path), "--revert", "--quiet"]) == 0
    for rel, text in files.items():
        assert _get(tmp_path, rel) == text, rel                          # desktop files, lsb-release, casper.conf ...
    assert not list(orig.iterdir())


def test_revert_leaves_an_entry_alone_when_the_base_replaced_it_meanwhile(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    newer = MINTWELCOME.replace("Introduction to Linux Mint", "Introduction to Linux Mint 23")
    _put(tmp_path, "usr/share/applications/mintwelcome.desktop", newer)     # an upgrade brought a fresh file
    rb.main(["--root", str(tmp_path), "--revert", "--quiet"])
    assert _get(tmp_path, "usr/share/applications/mintwelcome.desktop") == newer


def test_an_upgrade_that_restores_the_mint_file_is_swept_again_with_a_fresh_backup(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    newer = MINTWELCOME.replace("Welcome Screen", "Welcome Screen 2")
    _put(tmp_path, "usr/share/applications/mintwelcome.desktop", newer)
    _sweep(tmp_path)
    assert "NoDisplay=true" in _get(tmp_path, "usr/share/applications/mintwelcome.desktop")
    rb.main(["--root", str(tmp_path), "--revert", "--quiet"])
    assert _get(tmp_path, "usr/share/applications/mintwelcome.desktop") == newer


def test_patch_desktop_entry_details():
    text, changed = rb.patch_desktop_entry("[Desktop Entry]\nName=X\nNoDisplay=false\n", hide="NoDisplay")
    assert changed and "NoDisplay=true" in text and "NoDisplay=false" not in text
    again, changed2 = rb.patch_desktop_entry(text, hide="NoDisplay")
    assert not changed2 and again == text
    # no [Desktop Entry] group: nothing to do, nothing invented
    assert rb.patch_desktop_entry("Name=X\n", hide="NoDisplay") == ("Name=X\n", False)
    # replaced keys drop their localised variants (a stale "Name[de]" would show the old name)
    text, changed = rb.patch_desktop_entry("[Desktop Entry]\nName=Old\nName[de]=Alt\nExec=x\n", set_keys={"Name": "New"})
    assert changed and "Name=New" in text and "Name[de]" not in text
    # CRLF files keep CRLF; a file without a final newline stays without
    crlf, _ = rb.patch_desktop_entry("[Desktop Entry]\r\nName=Linux Mint tool\r\nExec=x", scrub=True)
    assert "\r\n" in crlf and crlf.endswith("Exec=x") and "Name=Lindos tool" in crlf


def test_symlinked_entries_are_never_followed(tmp_path):
    target = _put(tmp_path, "elsewhere/mintwelcome.desktop", MINTWELCOME)
    link_dir = tmp_path / "usr" / "share" / "applications"
    link_dir.mkdir(parents=True)
    try:
        os.symlink(target, link_dir / "mintwelcome.desktop")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    _sweep(tmp_path)
    assert target.read_text(encoding="utf-8") == MINTWELCOME


# --------------------------------------------------------------------------- KEY=value files
def test_release_files_change_display_fields_only(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path)
    lsb = _get(tmp_path, "etc/lsb-release")
    assert 'DISTRIB_DESCRIPTION="Lindos 1.0 (Aurora)"' in lsb
    for kept in ("DISTRIB_ID=LinuxMint", "DISTRIB_RELEASE=22.2", "DISTRIB_CODENAME=zara"):
        assert kept in lsb.splitlines()
    info = _get(tmp_path, "etc/linuxmint/info")
    assert 'DESCRIPTION="Lindos 1.0 (Aurora)"' in info
    assert "GRUB_TITLE=Lindos 1.0" in info.splitlines()                  # unquoted in, unquoted out
    for kept in ("RELEASE=22.2", "CODENAME=zara", 'EDITION="Xfce"', "DESKTOP=Gnome", "TOOLKIT=GTK"):
        assert kept in info.splitlines()
    casper = _get(tmp_path, "etc/casper.conf")
    assert 'export FLAVOUR="Lindos"' in casper and 'export USERNAME="liveuser"' in casper and 'export HOST="lindos"' in casper
    assert 'export USERFULLNAME="Live session user"' in casper and casper.startswith("# This file")


def test_kv_patch_keeps_quoting_export_and_never_adds_keys():
    text, changed = rb.patch_kv('export A="x"\nB=y\nC=\'z\'\n', {"A": "1 2", "B": "3 4", "C": "5", "NEW": "v"})
    assert changed
    assert text == 'export A="1 2"\nB=3 4\nC=\'5\'\n'
    assert rb.patch_kv(text, {"A": "1 2", "B": "3 4", "C": "5"}) == (text, False)


def test_missing_release_files_are_skipped_quietly(tmp_path, capsys):
    (tmp_path / "usr" / "share" / "applications").mkdir(parents=True)
    assert _sweep(tmp_path) == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- Firefox
POLICIES = {
    "policies": {
        "DisableAppUpdate": True,
        "Homepage": {"URL": "https://start.linuxmint.com/", "Additional": ["https://www.linuxmint.com/", "https://example.org/"], "StartPage": "homepage"},
        "Bookmarks": [{"Title": "Linux Mint", "URL": "https://www.linuxmint.com/"}, {"Title": "Mozilla", "URL": "https://www.mozilla.org/"}],
        "ManagedBookmarks": [{"toplevel_name": "Extra"}, {"name": "Community", "children": [{"name": "Forums", "url": "https://forums.linuxmint.com/"}, {"name": "Wiki", "url": "https://example.org/wiki"}]}],
        "SearchEngines": {"Default": "Linux Mint Search", "Add": [{"Name": "Linux Mint Search", "URLTemplate": "https://search.linuxmint.com/?q={searchTerms}"}, {"Name": "Other", "URLTemplate": "https://example.org/?q={searchTerms}"}]},
    }
}
PREFS_JS = """// Mint defaults
pref("browser.startup.homepage", "https://start.linuxmint.com/");
pref("startup.homepage_welcome_url", "https://www.linuxmint.com/welcome");
pref("startup.homepage_welcome_url.additional", "https://www.linuxmint.com/x");
pref("app.update.auto", false);
pref("browser.startup.page", 1);
pref("browser.search.order.1", "Linux Mint Search");
"""


def _seed_firefox(root: Path) -> None:
    _put(root, "usr/lib/firefox/distribution/policies.json", json.dumps(POLICIES))
    _put(root, "usr/lib/firefox/distribution/distribution.ini", "[Global]\nid=linuxmint\nversion=1.0\nabout=Linux Mint\n\n[Preferences]\nbrowser.startup.homepage=https://start.linuxmint.com/\n")
    _put(root, "usr/lib/firefox/browser/defaults/preferences/mint.js", PREFS_JS)
    _put(root, "usr/lib/firefox/defaults/pref/other.js", 'pref("browser.startup.homepage", "https://example.org/");\n')
    _put(root, "etc/firefox/syspref.js", 'pref("browser.startup.homepage", "https://www.linuxmint.com/");\n')
    (root / "usr" / "lib" / "firefox" / "browser").mkdir(parents=True, exist_ok=True)
    (root / "usr" / "lib" / "firefox" / "browser" / "omni.ja").write_bytes(b"PK\x03\x04 linuxmint binary")


def test_firefox_homepage_prefs_policies_and_distribution_ini(tmp_path):
    _seed_firefox(tmp_path)
    _sweep(tmp_path)
    js = _get(tmp_path, "usr/lib/firefox/browser/defaults/preferences/mint.js")
    assert 'pref("browser.startup.homepage", "about:home");' in js
    assert 'pref("startup.homepage_welcome_url", "");' in js and 'pref("startup.homepage_welcome_url.additional", "");' in js
    assert 'pref("app.update.auto", false);' in js and 'pref("browser.startup.page", 1);' in js     # other prefs untouched
    assert 'pref("browser.search.order.1", "Linux Mint Search");' in js     # only the homepage prefs are rewritten
    assert 'about:home' in _get(tmp_path, "etc/firefox/syspref.js")
    assert 'example.org' in _get(tmp_path, "usr/lib/firefox/defaults/pref/other.js")
    pol = json.loads(_get(tmp_path, "usr/lib/firefox/distribution/policies.json"))["policies"]
    assert pol["DisableAppUpdate"] is True
    assert pol["Homepage"] == {"URL": "about:home", "Additional": ["https://example.org/"], "StartPage": "homepage"}
    assert pol["Bookmarks"] == [{"Title": "Mozilla", "URL": "https://www.mozilla.org/"}]
    assert pol["ManagedBookmarks"][1]["children"] == [{"name": "Wiki", "url": "https://example.org/wiki"}]
    assert "Default" not in pol["SearchEngines"] and [e["Name"] for e in pol["SearchEngines"]["Add"]] == ["Other"]
    ini = _get(tmp_path, "usr/lib/firefox/distribution/distribution.ini")
    assert "about=Lindos" in ini and "id=linuxmint" in ini                # the partner id is an identifier, not text
    assert "browser.startup.homepage=about:home" in ini
    assert (tmp_path / "usr/lib/firefox/browser/omni.ja").read_bytes().startswith(b"PK")


def test_firefox_sweep_is_idempotent_and_survives_broken_json(tmp_path):
    _seed_firefox(tmp_path)
    _put(tmp_path, "etc/firefox/policies/policies.json", '{"policies": linuxmint not json')
    _sweep(tmp_path)
    before = _snapshot(tmp_path)
    _sweep(tmp_path)
    assert _snapshot(tmp_path) == before
    assert _get(tmp_path, "etc/firefox/policies/policies.json") == '{"policies": linuxmint not json'


def test_policies_without_mint_content_are_not_rewritten():
    text = json.dumps({"policies": {"Homepage": {"URL": "https://example.org/"}}}, indent=4)
    assert rb.patch_policies(text, "about:home") == (text, False)
    assert rb.patch_policies("[1, 2]", "about:home") == ("[1, 2]", False)


# --------------------------------------------------------------------------- CLI
def test_dry_run_changes_nothing(tmp_path):
    _seed_base(tmp_path)
    _seed_firefox(tmp_path)
    before = _snapshot(tmp_path)
    assert _sweep(tmp_path, "--dry-run") == 0
    assert _snapshot(tmp_path) == before


def test_steps_can_be_limited_and_unknown_steps_are_a_usage_error(tmp_path):
    _seed_base(tmp_path)
    _sweep(tmp_path, "--steps", "files")
    assert "NoDisplay" not in _get(tmp_path, "usr/share/applications/mintwelcome.desktop")
    assert "Lindos" in _get(tmp_path, "etc/lsb-release")
    with pytest.raises(SystemExit) as exc:
        rb.main(["--steps", "bogus"])
    assert exc.value.code == 2


def test_missing_or_broken_rules_never_fail_the_caller(tmp_path, capsys):
    assert rb.main(["--root", str(tmp_path), "--data", str(tmp_path / "nope.json"), "--quiet"]) == 0
    bad = _put(tmp_path, "bad.json", "{not json")
    assert rb.main(["--root", str(tmp_path), "--data", str(bad), "--quiet"]) == 0
    assert "WARNING" in capsys.readouterr().err


def test_a_failing_step_is_reported_and_the_others_still_run(tmp_path, monkeypatch, capsys):
    _seed_base(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rb, "sweep_desktop_dir", boom)
    assert _sweep(tmp_path, "--steps", "applications,files") == 0
    assert "step applications failed: boom" in capsys.readouterr().err
    assert "Lindos 1.0" in _get(tmp_path, "etc/lsb-release")


def test_root_defaults_to_lindos_root_env(tmp_path, monkeypatch):
    _seed_base(tmp_path)
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    assert rb.main(["--data", str(DATA), "--fragment", str(FRAGMENT), "--quiet"]) == 0
    assert "NoDisplay=true" in _get(tmp_path, "usr/share/applications/mintwelcome.desktop")


def test_audit_lists_what_still_says_mint_and_changes_nothing(tmp_path, capsys):
    _seed_base(tmp_path)
    _put(tmp_path, "etc/skel/.config/somefile", "homepage=https://www.linuxmint.com/\n")
    (tmp_path / "usr" / "share" / "backgrounds" / "linuxmint-zara").mkdir(parents=True)
    before = _snapshot(tmp_path)
    assert rb.main(["--root", str(tmp_path), "--audit"]) == 0
    out = capsys.readouterr().out
    assert "audit: /etc/lsb-release:" in out and "audit: /etc/skel/.config/somefile:" in out
    assert "audit: /usr/share/backgrounds/linuxmint-zara:" in out
    assert re.search(r"audit: \d+ item\(s\) still mention Linux Mint", out)
    assert _snapshot(tmp_path) == before
    _sweep(tmp_path)
    capsys.readouterr()
    rb.main(["--root", str(tmp_path), "--audit"])
    after = capsys.readouterr().out
    assert "mintwelcome.desktop" not in after and "audit: /etc/lsb-release:" in after    # ID=LinuxMint is still reported


def test_audit_also_finds_base_themes_in_skel_xdg_and_gsettings_overrides(tmp_path, capsys):
    _put(tmp_path, "etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml", '<property name="ThemeName" value="Mint-Y-Dark-Aqua"/>\n')
    _put(tmp_path, "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml", '<property name="theme" value="Mint-Y"/>\n')
    _put(tmp_path, "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml.lindos-orig", '<property name="theme" value="Mint-Y"/>\n')
    _put(tmp_path, "usr/share/glib-2.0/schemas/x_mint-artwork.gschema.override", "[org.gnome.desktop.interface]\ngtk-theme='Mint-Y-Aqua'\n")
    _put(tmp_path, "usr/share/glib-2.0/schemas/10_other.gschema.override", "[org.gnome.desktop.interface]\ngtk-theme='Adwaita'\n")
    assert rb.main(["--root", str(tmp_path), "--audit"]) == 0
    out = capsys.readouterr().out
    assert "audit: /etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml:" in out
    assert "audit: /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml:" in out
    assert "audit: /usr/share/glib-2.0/schemas/x_mint-artwork.gschema.override:" in out
    assert ".lindos-orig" not in out and "10_other" not in out              # diverted originals / harmless overrides are not noise


# --------------------------------------------------------------------------- through apply-branding.sh
def _shim_python(tmp_path: Path) -> str:
    """A 'python3' for the script under test: Git Bash on Windows only has the Store stub."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    shim = fake / "python3"
    shim.write_text('#!/bin/sh\nexec "%s" "$@"\n' % Path(sys.executable).as_posix(), encoding="utf-8", newline="\n")
    os.chmod(shim, 0o755)
    return fake.as_posix() + os.pathsep + os.environ.get("PATH", "")


def _branding_root(tmp_path: Path) -> Path:
    root = tmp_path / "sysroot"
    _seed_base(root)
    _put(root, "etc/os-release", OS_RELEASE)
    _put(root, "etc/issue", "Linux Mint 22.2 Zara \\n \\l\n\n")
    _put(root, "etc/issue.net", "Linux Mint 22.2 Zara\n")
    _put(root, "usr/share/lindos/os-release.d/lindos.conf", FRAGMENT.read_text(encoding="utf-8"))
    _put(root, "usr/share/lindos/branding/base-sweep.json", DATA.read_text(encoding="utf-8"))
    return root


def _apply(tmp_path: Path, root: Path, *args: str, script_env: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = _shim_python(tmp_path)
    env["LINDOS_ROOT"] = root.as_posix()
    if script_env:
        env["LINDOS_REBRAND_SCRIPT"] = script_env
    return subprocess.run([BASH, APPLY.as_posix(), *args], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, check=False)


@needs_bash
def test_apply_branding_runs_the_sweep_the_way_the_postinst_and_the_apt_hook_do(tmp_path):
    root = _branding_root(tmp_path)
    proc = _apply(tmp_path, root, "--quiet", "--files-only")
    assert proc.returncode == 0, proc.stderr
    osr = _get(root, "etc/os-release")
    assert 'NAME="Lindos"' in osr and 'PRETTY_NAME="Lindos 1.0 (Aurora)"' in osr
    assert "ID=linuxmint" in osr and 'ID_LIKE="ubuntu debian"' in osr and "VERSION_CODENAME=zara" in osr
    # the display version no longer says "22.2 (Zara)"; VERSION_ID stays because tools read it
    assert 'VERSION="1.0 (Aurora)"' in osr and 'VERSION_ID="22.2"' in osr and "(Zara)" not in osr
    assert 'SUPPORT_URL="https://github.com/hardcoregamingsyle/lindos/issues"' in osr
    assert "linuxmint.com" not in osr and "readthedocs" not in osr
    assert _get(root, "etc/issue").startswith("Lindos 1.0.0 (Aurora) ")
    assert 'DISTRIB_DESCRIPTION="Lindos 1.0 (Aurora)"' in _get(root, "etc/lsb-release")
    assert "NoDisplay=true" in _get(root, "usr/share/applications/mintwelcome.desktop")
    assert "Hidden=true" in _get(root, "etc/xdg/autostart/mintwelcome.desktop")
    # idempotent: a second run (the next apt operation) rewrites nothing
    before = _snapshot(root)
    assert _apply(tmp_path, root, "--quiet", "--files-only").returncode == 0
    assert _snapshot(root) == before


@needs_bash
def test_apply_branding_dry_run_and_revert(tmp_path):
    root = _branding_root(tmp_path)
    before = _snapshot(root)
    assert _apply(tmp_path, root, "--quiet", "--dry-run").returncode == 0
    assert _snapshot(root) == before
    assert _apply(tmp_path, root, "--quiet", "--files-only").returncode == 0
    assert _apply(tmp_path, root, "--quiet", "--revert").returncode == 0
    for rel in ("etc/os-release", "etc/lsb-release", "etc/linuxmint/info", "etc/casper.conf",
                "usr/share/applications/mintwelcome.desktop", "etc/xdg/autostart/mintwelcome.desktop"):
        assert _get(root, rel) == _snapshot_original(rel), rel


def _snapshot_original(rel: str) -> str:
    return {"etc/os-release": OS_RELEASE, "etc/lsb-release": LSB, "etc/linuxmint/info": MINT_INFO, "etc/casper.conf": CASPER,
            "usr/share/applications/mintwelcome.desktop": MINTWELCOME,
            "etc/xdg/autostart/mintwelcome.desktop": WELCOME_AUTOSTART}[rel]


@needs_bash
def test_apply_branding_survives_a_missing_sweep_script(tmp_path):
    root = _branding_root(tmp_path)
    proc = _apply(tmp_path, root, "--files-only", script_env=(tmp_path / "missing.py").as_posix())
    assert proc.returncode == 0
    assert "skipping the Mint sweep" in proc.stderr
    assert 'NAME="Lindos"' in _get(root, "etc/os-release")                    # the rest of the branding still ran
    assert "NoDisplay" not in _get(root, "usr/share/applications/mintwelcome.desktop")


@needs_bash
def test_apply_branding_help_documents_files_only():
    proc = subprocess.run([BASH, APPLY.as_posix(), "--help"], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0 and "--files-only" in proc.stdout and "Mint sweep" in proc.stdout


# --------------------------------------------------------------------------- shipped data + wiring
def _keys(path: Path) -> Dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln and not ln.startswith("#"))


def test_sweep_rules_are_well_formed_and_conservative():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    for section in ("applications", "autostart"):
        for pattern in data[section].get("hide_if_exec_matches", []):
            re.compile(pattern)
        assert all(n.endswith(".desktop") for n in data[section].get("hide", []))
    # The tools Lindos has no equivalent for stay in the menu; only duplicates of Lindos features are hidden.
    assert set(data["applications"]["hide"]) == {"mintwelcome.desktop", "mintinstall.desktop"}
    assert "mintupdate.desktop" not in data["applications"]["hide"] and "mintdrivers.desktop" not in data["applications"]["hide"]
    assert set(data["autostart"]["hide"]) == {"mintwelcome.desktop"}
    assert "mintupdate" not in " ".join(data["autostart"]["hide_if_exec_matches"])      # its tray tells users about updates
    for spec in data["files"]:                                               # display fields only, never identity
        assert not set(spec["set"]) & {"DISTRIB_ID", "DISTRIB_RELEASE", "DISTRIB_CODENAME", "RELEASE", "CODENAME", "EDITION", "ID", "ID_LIKE", "VERSION_CODENAME"}
    assert all(p.startswith("/") for p in data["firefox"]["dirs"])


def test_every_hidden_mint_tool_has_a_lindos_replacement_in_the_package():
    apps = ROOT / "usr" / "share" / "applications"
    store = _keys(apps / "lindos-store.desktop")                              # replaces Software Manager
    assert store["Exec"].split()[0] == "mintinstall" and store["Icon"] == "lindos-store"
    setup = _keys(ROOT / "etc" / "xdg" / "autostart" / "lindos-setup.desktop")  # replaces Mint Welcome
    assert setup["Exec"].startswith("lindos-setup")


def test_store_shim_is_named_honestly():
    e = _keys(ROOT / "usr" / "share" / "applications" / "lindos-store.desktop")
    assert e["Name"] == "Lindos Store"
    blob = " ".join(e.values()).lower()
    assert "microsoft" not in blob and "mint" not in blob.replace("mintinstall", "")
    assert "flatpak" in e["Comment"].lower()


def test_new_icons_exist_and_are_the_icons_the_rules_ask_for():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    icons = ROOT / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    wanted = set(data["applications"]["icons"].values()) | {data["applications"]["icon_fallback"]}
    assert wanted == {"lindos-update", "lindos-drivers", "lindos-store", "lindos-settings"}
    for name in wanted:
        root = ET.parse(str(icons / (name + ".svg"))).getroot()
        assert root.get("viewBox") == "0 0 48 48"
        if name != "lindos-settings":          # the older settings icon has its own header
            assert "Not derived from any Microsoft or Linux Mint artwork" in (icons / (name + ".svg")).read_text(encoding="utf-8")


def test_skel_autostart_override_masks_mint_welcome_for_new_users():
    e = _keys(ROOT / "etc" / "skel" / ".config" / "autostart" / "mintwelcome.desktop")
    assert e["Hidden"] == "true" and e["Type"] == "Application" and e["Name"] and e["Exec"] == "true"
    assert "mintwelcome.desktop" in json.loads(DATA.read_text(encoding="utf-8"))["autostart"]["hide"]


def test_apt_hook_reapplies_branding_but_never_touches_alternatives_or_fails_apt():
    lines = [ln for ln in (ROOT / "etc" / "apt" / "apt.conf.d" / "99lindos-branding").read_text(encoding="utf-8").splitlines() if not ln.startswith("//")]
    conf = "\n".join(lines)
    assert re.fullmatch(r'DPkg::Post-Invoke \{ "[^"]+"; \};', conf.strip()), conf
    assert "/usr/libexec/lindos/apply-branding.sh --quiet --files-only" in conf
    assert "|| true" in conf and "[ -x /usr/libexec/lindos/apply-branding.sh ]" in conf


def test_grub_drop_in_renames_the_boot_menu_only():
    text = (ROOT / "etc" / "default" / "grub.d" / "60-lindos-distributor.cfg").read_text(encoding="utf-8")
    assignments = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert assignments == ['GRUB_DISTRIBUTOR="Lindos"']


def test_new_config_files_are_conffiles_and_the_sweep_is_wired_into_the_maintainer_scripts():
    conffiles = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    for c in ("/etc/apt/apt.conf.d/99lindos-branding", "/etc/default/grub.d/60-lindos-distributor.cfg", "/etc/skel/.config/autostart/mintwelcome.desktop"):
        assert c in conffiles
    postinst = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "/usr/libexec/lindos/rebrand-base.py" in postinst and "apply-branding.sh --quiet" in postinst
    assert "/var/lib/lindos/rebrand" in (DEBIAN / "postrm").read_text(encoding="utf-8")
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    assert re.search(r"^Depends:.*\bpython3\b", control, flags=re.M)         # the sweep is python


def test_script_conventions():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in raw
    text = raw.decode("utf-8")
    assert re.search(r"^\s*(sudo|pkexec)\s", text, flags=re.M) is None
    stdlib_only = {"argparse", "fnmatch", "json", "os", "re", "shutil", "sys", "urllib", "typing", "__future__"}
    imported = set(re.findall(r"^(?:import|from)\s+([A-Za-z_]+)", text, flags=re.M))
    assert imported <= stdlib_only, imported - stdlib_only
    compile(text, str(SCRIPT), "exec")


def test_live_user_and_hostname_agree_between_the_boot_menu_and_casper_rules():
    rules = json.loads(DATA.read_text(encoding="utf-8"))
    casper = next(f for f in rules["files"] if f["path"] == "/etc/casper.conf")["set"]
    for cfg in ("grub.cfg", "loopback.cfg"):
        text = (REPO / "build" / "overlay" / "boot" / "grub" / cfg).read_text(encoding="utf-8")
        for m in re.finditer(r"username=(\S+) hostname=(\S+)", text):
            assert (m.group(1), m.group(2)) == (casper["USERNAME"], casper["HOST"]), cfg
    qa = (REPO / "build" / "qa" / "boot_test.py").read_text(encoding="utf-8")
    assert "username=%s hostname=%s" % (casper["USERNAME"], casper["HOST"]) in qa



def test_the_sweep_is_reverted_by_prerm_while_the_scripts_still_exist():
    prerm = (DEBIAN / "prerm").read_text(encoding="utf-8")
    assert prerm.startswith("#!/bin/sh\n") and re.search(r"^set -e\b", prerm, flags=re.M)
    assert re.search(r"remove\|deconfigure\)", prerm)                  # never on upgrade
    assert "/usr/libexec/lindos/rebrand-base.py --revert" in prerm
    assert "upgrade" not in prerm.split("case", 1)[1].split("esac", 1)[0].replace("*)", "")
