"""lindos-desktop: the first layer of making Lindos unrecognisable as Linux Mint (docs/BUILDING.md, "Unrecognisable").

Hermetic (fake roots, stdlib, bash only where a shell script is the thing under test):

  * the sweep: apps Lindos replaces, Mint's extras and the Xfce/Thunar duplicates are hidden by glob, the update /
    driver tools get Lindos names and icons, running windows do not fall back to a Mint icon, theme packs are hidden
    from the pickers (Hidden=true), the copy of Firefox's distribution.ini that mint-adjust restores at every boot is
    branded too - all idempotent and revertible;
  * the precedence fix: /etc/xdg/xdg-* diversions in preinst/postrm (kept in step), Lindos's own /etc/xdg/xdg-xfce, the
    Xsession.d filter, mint-adjust's .preserve list, the GRUB drop-in that sorts after Mint's;
  * default apps: /etc/xdg/mimeapps.list merge helper and the shipped list; nothing shipped names a Mint app;
  * the legal notices text, the cursor theme rename, the package metadata.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
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
REPO = PKG.parent.parent
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
DATA = ROOT / "usr" / "share" / "lindos" / "branding" / "base-sweep.json"
FRAGMENT = ROOT / "usr" / "share" / "lindos" / "os-release.d" / "lindos.conf"
XSESSION = ROOT / "etc" / "X11" / "Xsession.d" / "61lindos-xdg-config-dirs"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load("lindos_rebrand_base_unrec", LIBEXEC / "rebrand-base.py")
mm = _load("lindos_merge_mimeapps", LIBEXEC / "merge-mimeapps.py")
RULES = json.loads(DATA.read_text(encoding="utf-8"))


def _posix(p: Path) -> str:
    """A path bash understands (Git Bash on Windows wants /c/... and would split C:/... at the colon)."""
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _get(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _entry(name: str, exec_: str = "true", extra: str = "") -> str:
    return "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n%s" % (name, exec_, extra)


def _sweep(root: Path, *extra: str) -> int:
    return rb.main(["--root", str(root), "--data", str(DATA), "--fragment", str(FRAGMENT), "--quiet", *extra])


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


A = "usr/share/applications/"
HIDDEN_APPS = {
    "xed.desktop": "Text Editor", "xviewer.desktop": "Image Viewer", "xreader.desktop": "Document Viewer", "pix.desktop": "Pix",
    "io.github.celluloid_player.Celluloid.desktop": "Celluloid", "org.x.Warpinator.desktop": "Warpinator",
    "org.x.sticky.desktop": "Notes", "hypnotix.desktop": "Hypnotix", "thingy.desktop": "Library", "mintstick.desktop": "USB Image Writer",
    "mintbackup.desktop": "Backup Tool", "mintdesktop.desktop": "Desktop Settings", "lightdm-settings.desktop": "Login Window",
    "fingwit.desktop": "Fingerprints", "webapp-manager.desktop": "Web Apps", "thunar.desktop": "Thunar File Manager",
    "thunar-settings.desktop": "File Manager Settings", "thunar-bulk-rename.desktop": "Bulk Rename",
    "xfce4-terminal.desktop": "Xfce Terminal", "xfce4-settings-manager.desktop": "Settings Manager",
    "xfce4-appearance-settings.desktop": "Appearance", "xfce-display-settings.desktop": "Display", "xfwm4-settings.desktop": "Window Manager",
    "xfce4-notifyd-config.desktop": "Notifications", "xfce4-appfinder.desktop": "Application Finder", "ccsm.desktop": "CompizConfig",
    "menulibre.desktop": "Menu Editor", "exo-preferred-applications.desktop": "Preferred Applications", "xfce4-about.desktop": "About Xfce",
}
VISIBLE_APPS = {
    "org.xfce.mousepad.desktop": "Text Editor", "org.xfce.ristretto.desktop": "Image Viewer", "org.gnome.Evince.desktop": "Document Viewer",
    "vlc.desktop": "VLC media player", "firefox.desktop": "Firefox Web Browser", "thunderbird.desktop": "Thunderbird Mail",
    "xfce4-taskmanager.desktop": "Task Manager", "xfce4-screenshooter.desktop": "Screenshot", "libreoffice-writer.desktop": "LibreOffice Writer",
    "mintupdate.desktop": "Update Manager", "mintdrivers.desktop": "Driver Manager",
    "lindos-files.desktop": "File Explorer", "lindos-terminal.desktop": "Terminal",
}


def _seed_apps(root: Path) -> None:
    for name, title in {**HIDDEN_APPS, **VISIBLE_APPS}.items():
        icon = "Icon=" + name.split(".")[0] + "\n"
        _put(root, A + name, _entry(title, name.split(".")[0], icon))
    _put(root, A + "codecs.desktop", _entry("Install Multimedia Codecs", "mint-meta-codecs", "Icon=mint-meta-codecs\n"))
    _put(root, A + "matrix.desktop", _entry("Matrix", "mintchat", "Icon=mintchat\n"))
    _put(root, "etc/skel/.local/share/applications/webapp-OnlineChat4519.desktop", _entry("Matrix", "mintchat", "Icon=mintchat\n"))
    _put(root, A + "mintsources.desktop", _entry("Software Sources", "mintsources", "Icon=mintsources\nName[fr]=Sources de logiciels\n"))
    _put(root, A + "mintreport.desktop", _entry("System Reports", "mintreport", "Icon=mintreport\n"))
    _put(root, A + "mintlocale.desktop", _entry("Languages", "mintlocale", "Icon=mintlocale\n"))
    _put(root, A + "whatever.desktop", _entry("Whatever", "whatever", "Icon=mint-something\n"))
    _put(root, "etc/xdg/autostart/warpinator-autostart.desktop", _entry("Warpinator", "warpinator --autostart"))
    _put(root, "etc/xdg/autostart/sticky.desktop", _entry("Sticky", "sticky --autostart"))
    _put(root, "etc/xdg/autostart/blueman.desktop", _entry("Blueman", "blueman-applet"))
    _put(root, "etc/xdg/autostart/mintupdate.desktop", _entry("Update Manager", "mintupdate-launcher"))


# --------------------------------------------------------------------------- the sweep: menu and autostart
def test_replaced_apps_mint_extras_and_xfce_duplicates_are_hidden_and_everything_else_is_not(tmp_path):
    _seed_apps(tmp_path)
    assert _sweep(tmp_path) == 0
    for name in HIDDEN_APPS:
        assert re.search(r"^NoDisplay=true$", _get(tmp_path, A + name), flags=re.M), name
    for name in ("codecs.desktop", "matrix.desktop"):
        assert "NoDisplay=true" in _get(tmp_path, A + name), name                     # found by the command they run
    assert "NoDisplay=true" in _get(tmp_path, "etc/skel/.local/share/applications/webapp-OnlineChat4519.desktop")
    for name in VISIBLE_APPS:
        assert "NoDisplay" not in _get(tmp_path, A + name), name


def test_the_default_apps_and_the_interim_tools_are_never_hidden_by_any_rule():
    globs = [g.lower() for g in RULES["applications"]["hide_globs"]]
    names = set(RULES["applications"]["hide"])
    for keep in ("org.xfce.mousepad.desktop", "org.xfce.ristretto.desktop", "org.gnome.Evince.desktop", "vlc.desktop",
                 "firefox.desktop", "thunderbird.desktop", "xfce4-taskmanager.desktop", "xfce4-screenshooter.desktop",
                 "mintupdate.desktop", "mintdrivers.desktop", "mintsources.desktop", "mintreport.desktop", "mintlocale.desktop",
                 "libreoffice-writer.desktop", "timeshift-gtk.desktop", "org.gnome.Calculator.desktop"):
        assert keep not in names and not any(fnmatch.fnmatchcase(keep.lower(), g) for g in globs), keep
    assert "mintinstall.desktop" in names, "Lindos Store replaces the Software Manager entry"


def test_update_driver_and_report_tools_get_lindos_names_and_icons(tmp_path):
    _seed_apps(tmp_path)
    _sweep(tmp_path)
    upd = _get(tmp_path, A + "mintupdate.desktop")
    assert rb.get_key(upd, "Name") == "Lindos Updates" and rb.get_key(upd, "Icon") == "lindos-update"
    assert rb.get_key(_get(tmp_path, A + "mintdrivers.desktop"), "Name") == "Lindos Drivers"
    src = _get(tmp_path, A + "mintsources.desktop")
    assert rb.get_key(src, "Name") == "Lindos Update Sources" and rb.get_key(src, "Icon") == "lindos-update"
    assert "Name[fr]" not in src, "a stale translation would still show the old name"
    rep = _get(tmp_path, A + "mintreport.desktop")
    assert rb.get_key(rep, "Name") == "Lindos System Reports" and rb.get_key(rep, "Icon") == "lindos-settings"
    loc = _get(tmp_path, A + "mintlocale.desktop")
    assert rb.get_key(loc, "Name") == "Language and Region" and rb.get_key(loc, "Icon") == "lindos-settings"
    for exe, rel in (("mintupdate", "mintupdate.desktop"), ("mintsources", "mintsources.desktop"), ("mintlocale", "mintlocale.desktop")):
        assert rb.get_key(_get(tmp_path, A + rel), "Exec") == exe, "the command is never touched"


def test_any_other_mint_icon_becomes_a_lindos_icon_and_other_icons_stay(tmp_path):
    _seed_apps(tmp_path)
    _sweep(tmp_path)
    assert rb.get_key(_get(tmp_path, A + "whatever.desktop"), "Icon") == "lindos-settings"
    assert rb.get_key(_get(tmp_path, A + "firefox.desktop"), "Icon") == "firefox"
    assert rb.get_key(_get(tmp_path, A + "vlc.desktop"), "Icon") == "vlc"


def test_lindos_entries_are_left_byte_for_byte(tmp_path):
    _seed_apps(tmp_path)
    before = {n: _get(tmp_path, A + n) for n in ("lindos-files.desktop", "lindos-terminal.desktop")}
    _sweep(tmp_path)
    for n, text in before.items():
        assert _get(tmp_path, A + n) == text, n


def test_file_sharing_and_notes_daemons_do_not_autostart_but_the_update_tray_and_blueman_do(tmp_path):
    _seed_apps(tmp_path)
    _sweep(tmp_path)
    for name in ("warpinator-autostart.desktop", "sticky.desktop"):
        assert re.search(r"^Hidden=true$", _get(tmp_path, "etc/xdg/autostart/" + name), flags=re.M), name
    for name in ("blueman.desktop", "mintupdate.desktop"):
        assert "Hidden" not in _get(tmp_path, "etc/xdg/autostart/" + name), name


def test_the_extended_sweep_is_idempotent_and_reverts_exactly(tmp_path):
    _seed_apps(tmp_path)
    _put(tmp_path, "usr/share/icons/Yaru/index.theme", "[Icon Theme]\nName=Yaru\n")
    before = _snapshot(tmp_path)
    _sweep(tmp_path)
    after = _snapshot(tmp_path)
    assert after != before
    ctx = rb.Ctx(str(tmp_path), quiet=True)
    rb.run_steps(ctx, RULES, rb.load_brand(ctx, str(FRAGMENT)), list(rb.STEPS))
    assert ctx.changes == 0 and _snapshot(tmp_path) == after
    assert rb.main(["--root", str(tmp_path), "--revert", "--quiet"]) == 0
    restored = {k: v for k, v in _snapshot(tmp_path).items() if not k.startswith("var/lib/lindos/rebrand/")}
    assert restored == before


# --------------------------------------------------------------------------- the sweep: theme packs
def _seed_icon_themes(root: Path) -> None:
    for name in ("Mint-Y-Sand", "Mint-X", "Yaru", "Yaru-dark", "Papirus", "ePapirus", "Humanity", "ubuntu-mono-dark", "Bibata-Modern-Classic",
                 "GoogleDot-Blue", "DMZ-White", "XCursor-Pro-Dark", "LoginIcons"):
        _put(root, "usr/share/icons/%s/index.theme" % name, "[Icon Theme]\nName=%s\nComment=x\nDirectories=16/apps\n\n[16/apps]\nSize=16\n" % name)
    for name in ("hicolor", "Adwaita", "Lindos", "Lindos-dark", "Lindos-Cursors-Dark", "Fluent-dark-cursors"):
        _put(root, "usr/share/icons/%s/index.theme" % name, "[Icon Theme]\nName=%s\nDirectories=16/apps\n" % name)


def test_the_base_theme_packs_are_hidden_from_the_pickers_and_lindos_themes_are_not(tmp_path):
    _seed_icon_themes(tmp_path)
    lindos = {n: _get(tmp_path, "usr/share/icons/%s/index.theme" % n) for n in ("hicolor", "Adwaita", "Lindos", "Lindos-dark", "Lindos-Cursors-Dark", "Fluent-dark-cursors")}
    assert _sweep(tmp_path) == 0
    for name in ("Mint-Y-Sand", "Mint-X", "Yaru", "Yaru-dark", "Papirus", "ePapirus", "Humanity", "ubuntu-mono-dark", "Bibata-Modern-Classic",
                 "GoogleDot-Blue", "DMZ-White", "XCursor-Pro-Dark", "LoginIcons"):
        text = _get(tmp_path, "usr/share/icons/%s/index.theme" % name)
        head = text.split("[16/apps]")[0]
        assert re.search(r"^Hidden=true$", head, flags=re.M), name             # in the [Icon Theme] group
        assert "Name=%s" % name in text and "Directories=16/apps" in text, "nothing else changes: the theme still works as a fallback"
    for name, text in lindos.items():
        assert _get(tmp_path, "usr/share/icons/%s/index.theme" % name) == text, name


def test_hidden_false_is_flipped_a_file_without_an_icon_theme_group_is_left_alone(tmp_path):
    text, changed = rb.patch_index_theme("[Icon Theme]\nName=Yaru\nHidden=false\nInherits=hicolor\n")
    assert changed and "Hidden=true" in text and "Hidden=false" not in text and "Inherits=hicolor" in text
    again, changed2 = rb.patch_index_theme(text)
    assert not changed2 and again == text
    assert rb.patch_index_theme("[X-GNOME-Metatheme]\nName=x\n") == ("[X-GNOME-Metatheme]\nName=x\n", False)
    crlf, _ = rb.patch_index_theme("[Icon Theme]\r\nName=Yaru\r\n")
    assert crlf == "[Icon Theme]\r\nX-Lindos-Rebranded=true\r\nHidden=true\r\nName=Yaru\r\n"


def test_a_symlinked_theme_directory_is_never_followed(tmp_path):
    real = tmp_path / "elsewhere" / "Yaru"
    _put(tmp_path, "elsewhere/Yaru/index.theme", "[Icon Theme]\nName=Yaru\n")
    (tmp_path / "usr" / "share" / "icons").mkdir(parents=True)
    try:
        os.symlink(str(real), str(tmp_path / "usr" / "share" / "icons" / "Yaru"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    _sweep(tmp_path)
    assert (real / "index.theme").read_text(encoding="utf-8") == "[Icon Theme]\nName=Yaru\n"


def test_the_audit_no_longer_lists_a_hidden_mint_theme_but_still_lists_a_visible_one(tmp_path, capsys):
    _put(tmp_path, "usr/share/icons/Mint-Y-Sand/index.theme", "[Icon Theme]\nName=Mint-Y-Sand\n")
    _put(tmp_path, "usr/share/themes/Mint-Y/index.theme", "[Desktop Entry]\nName=Mint-Y\n")
    rb.main(["--root", str(tmp_path), "--audit"])
    out = capsys.readouterr().out
    assert "audit: /usr/share/icons/Mint-Y-Sand:" in out and "audit: /usr/share/themes/Mint-Y:" in out
    _sweep(tmp_path)
    rb.main(["--root", str(tmp_path), "--audit"])
    out = capsys.readouterr().out
    assert "Mint-Y-Sand" not in out and "audit: /usr/share/themes/Mint-Y:" in out, "GTK themes cannot be hidden: purge removes them"


# --------------------------------------------------------------------------- mint-adjust and Firefox
DIST_INI = "[Global]\nid=mint-001\nversion=1.0\nabout=Mozilla Firefox for Linux Mint\n\n[Preferences]\napp.distributor=mint\n"


def test_the_copy_mint_adjust_puts_back_at_every_boot_is_branded_too(tmp_path):
    live = _put(tmp_path, "usr/lib/firefox/distribution/distribution.ini", DIST_INI)
    source = _put(tmp_path, "usr/share/ubuntu-system-adjustments/firefox/distribution.ini", DIST_INI)
    _sweep(tmp_path)
    assert "about=Mozilla Firefox for Lindos" in live.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == live.read_text(encoding="utf-8"), \
        "source and destination agree, so mint-adjust has nothing to copy over Lindos's edit"


def test_mint_adjust_is_told_to_preserve_the_files_lindos_edits():
    preserve = (ROOT / "usr" / "share" / "linuxmint" / "adjustments" / "99-lindos.preserve").read_text(encoding="utf-8").split()
    assert preserve == ["/usr/lib/firefox/distribution/distribution.ini", "/usr/share/applications/mimeapps.list"]
    assert "usr/share/ubuntu-system-adjustments" in " ".join(RULES["firefox"]["dirs"]) or "/usr/share/ubuntu-system-adjustments" in RULES["firefox"]["dirs"]


# --------------------------------------------------------------------------- xdg precedence
def _list(text: str, var: str) -> List[str]:
    m = re.search(r'^%s="\n(.*?)\n"$' % var, text, flags=re.M | re.S)
    assert m, var
    return [ln for ln in m.group(1).split("\n") if ln]


def test_preinst_and_postrm_divert_and_restore_the_same_paths():
    pre = (DEBIAN / "preinst").read_text(encoding="utf-8")
    post = (DEBIAN / "postrm").read_text(encoding="utf-8")
    diverted = _list(pre, "DIVERT_FILES")
    wrappers = _list(pre, "MINT_WRAPPERS")
    assert sorted(diverted + wrappers) == sorted(_list(post, "DIVERTED"))
    for p in ("/etc/xdg/xdg-xfce", "/etc/xdg/xdg-default", "/etc/xdg/xdg-default.desktop"):
        assert p in diverted, p
    for p in ("/usr/local/bin/apt", "/usr/local/bin/search", "/usr/local/bin/highlight-mint", "/usr/bin/rtfm"):
        assert p in wrappers, p
    # the wrappers are diverted only when they exist; the xdg links and configs unconditionally
    assert re.search(r'for f in \$MINT_WRAPPERS; do\s+if \[ -e "\$R\$f" \] \|\| \[ -L "\$R\$f" \]; then\s+divert_one "\$f"', pre)
    assert '--divert "$1.lindos-orig"' in pre


@needs_bash
@pytest.mark.parametrize("script", ["preinst", "postinst", "postrm", "prerm"])
def test_maintainer_scripts_parse(script):
    res = subprocess.run([BASH, "-n", str(DEBIAN / script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert (DEBIAN / script).read_bytes().startswith(b"#!/bin/sh\n")


def test_lindos_owns_etc_xdg_xdg_xfce_through_the_postinst_not_through_a_packaged_path():
    """dpkg would refuse to unpack a directory over the symlink another package owns, so nothing under
    /etc/xdg/xdg-* is packaged; the postinst creates the directory from a template."""
    assert not list((ROOT / "etc" / "xdg").glob("xdg-*"))
    template = ROOT / "usr" / "share" / "lindos" / "xdg-xfce-README"
    text = template.read_text(encoding="utf-8")
    assert "mint-artwork" in text and "61lindos-xdg-config-dirs" in text
    conf = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    assert not [c for c in conf if c.startswith("/etc/xdg/xdg-")]
    assert "/etc/X11/Xsession.d/61lindos-xdg-config-dirs" in conf
    post = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "xdg-xfce-README" in post and 'mkdir -p "$XDG_XFCE"' in post


def _fake_tools(tmp: Path, *, divert: str = "refuse") -> Path:
    """No-op stand-ins for everything the maintainer scripts call outside the fake root, first in PATH.
    dpkg-divert: 'refuse' exits 2 (as if it will not divert a symlink to a directory), 'rename' moves the file."""
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    log = _posix(tmp / "calls.log")
    for name in ("gtk-update-icon-cache", "update-icon-caches", "fc-cache", "update-desktop-database", "update-alternatives",
                 "systemctl", "plymouth-set-default-theme"):
        _put(fake, name, "#!/bin/sh\necho \"%s $*\" >> \"%s\"\nexit 0\n" % (name, log))
    if divert == "rename":
        body = ('#!/bin/sh\necho "dpkg-divert $*" >> "%s"\n'
                'case " $* " in *" --list "*) exit 0 ;; esac\n'
                'for last; do :; done\n'
                'new=""; prev=""; for a; do [ "$prev" = "--divert" ] && new="$a"; prev="$a"; done\n'
                '[ -n "$new" ] && { [ -e "$LINDOS_ROOT$last" ] || [ -L "$LINDOS_ROOT$last" ]; } && mv "$LINDOS_ROOT$last" "$LINDOS_ROOT$new"\n'
                'exit 0\n' % log)
    else:
        body = '#!/bin/sh\necho "dpkg-divert $*" >> "%s"\ncase " $* " in *" --list "*) exit 0 ;; esac\nexit 2\n' % log
    _put(fake, "dpkg-divert", body)
    for f in fake.iterdir():
        os.chmod(f, 0o755)
    return fake


def _run_script(tmp: Path, root: Path, script: str, *args: str, divert: str = "refuse") -> subprocess.CompletedProcess:
    fake = _fake_tools(tmp, divert=divert)
    env = dict(os.environ)
    env["PATH"] = _posix(fake) + os.pathsep + env.get("PATH", "")
    env["LINDOS_ROOT"] = _posix(root)
    return subprocess.run([BASH, "-c", '. "%s"' % _posix(DEBIAN / script), script, *args], capture_output=True, text=True,
                          env=env, timeout=120)


def _mint_links(root: Path, *names: str) -> None:
    target = root / "usr" / "share" / "mint-artwork" / "xfce"
    target.mkdir(parents=True, exist_ok=True)
    (root / "etc" / "xdg").mkdir(parents=True, exist_ok=True)
    try:
        for n in names:
            os.symlink(str(target), str(root / "etc" / "xdg" / n))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")


def _calls(tmp: Path) -> str:
    p = tmp / "calls.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@needs_bash
def test_preinst_moves_the_mint_symlinks_aside_by_hand_when_dpkg_divert_refuses(tmp_path):
    root = tmp_path / "root"
    _mint_links(root, "xdg-xfce", "xdg-default", "xdg-default.desktop")
    _put(root, "usr/local/bin/apt", "#!/bin/sh\n")
    res = _run_script(tmp_path, root, "preinst", "install")
    assert res.returncode == 0, res.stderr
    for n in ("xdg-xfce", "xdg-default", "xdg-default.desktop"):
        assert not (root / "etc" / "xdg" / n).exists() and not (root / "etc" / "xdg" / n).is_symlink(), n
        assert (root / "etc" / "xdg" / (n + ".lindos-orig")).is_symlink(), n
    calls = _calls(tmp_path)
    assert "--add --rename --quiet --divert /etc/xdg/xdg-xfce.lindos-orig /etc/xdg/xdg-xfce" in calls
    assert "/usr/local/bin/apt.lindos-orig /usr/local/bin/apt" in calls, "an existing Mint wrapper is diverted"
    assert "/usr/bin/rtfm" not in calls and "highlight-mint" not in calls, "wrappers that do not exist are not diverted"
    assert (root / "usr/local/bin/apt").is_file(), "wrappers are left to dpkg-divert, never moved by hand"
    for f in _list((DEBIAN / "preinst").read_text(encoding="utf-8"), "DIVERT_FILES"):
        assert "%s.lindos-orig %s" % (f, f) in calls, f


@needs_bash
def test_preinst_leaves_a_link_alone_when_dpkg_divert_took_it_or_it_is_not_mints(tmp_path):
    root = tmp_path / "root"
    _mint_links(root, "xdg-xfce")
    res = _run_script(tmp_path, root, "preinst", "upgrade", divert="rename")
    assert res.returncode == 0, res.stderr
    assert (root / "etc/xdg/xdg-xfce.lindos-orig").is_symlink() and not (root / "etc/xdg/xdg-xfce").exists()
    other = tmp_path / "other"
    (other / "etc" / "xdg").mkdir(parents=True)
    (other / "somewhere").mkdir()
    try:
        os.symlink(str(other / "somewhere"), str(other / "etc" / "xdg" / "xdg-xfce"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    again = tmp_path / "again"
    again.mkdir()
    assert _run_script(again, other, "preinst", "install").returncode == 0
    assert (other / "etc" / "xdg" / "xdg-xfce").is_symlink(), "only links into mint-artwork are moved"


@needs_bash
def test_preinst_does_nothing_on_other_actions(tmp_path):
    root = tmp_path / "root"
    _mint_links(root, "xdg-xfce")
    assert _run_script(tmp_path, root, "preinst", "abort-upgrade").returncode == 0
    assert (root / "etc/xdg/xdg-xfce").is_symlink() and _calls(tmp_path) == ""


@needs_bash
def test_postinst_creates_lindos_own_xdg_xfce_directory_and_is_idempotent(tmp_path):
    root = tmp_path / "root"
    _mint_links(root, "xdg-xfce")                                       # the preinst could not move it
    _put(root, "usr/share/lindos/xdg-xfce-README", (ROOT / "usr/share/lindos/xdg-xfce-README").read_text(encoding="utf-8"))
    _put(root, "etc/default/grub.d/49-lindos-distributor.cfg", 'GRUB_DISTRIBUTOR="Lindos"\n')
    res = _run_script(tmp_path, root, "postinst", "configure")
    assert res.returncode == 0, res.stderr
    d = root / "etc" / "xdg" / "xdg-xfce"
    assert d.is_dir() and not d.is_symlink()
    assert sorted(p.name for p in d.iterdir()) == ["README"] and "mint-artwork" in (d / "README").read_text(encoding="utf-8")
    assert (root / "etc/xdg/xdg-xfce.lindos-orig").is_symlink()
    assert not (root / "etc/default/grub.d/49-lindos-distributor.cfg").exists(), "the renamed boot-menu drop-in is dropped"
    (d / "README").write_text("edited by the administrator\n", encoding="utf-8")
    again = tmp_path / "again"
    again.mkdir()
    assert _run_script(again, root, "postinst", "configure").returncode == 0
    assert (d / "README").read_text(encoding="utf-8") == "edited by the administrator\n"


@needs_bash
def test_postinst_leaves_a_foreign_xdg_xfce_alone(tmp_path):
    root = tmp_path / "root"
    (root / "etc" / "xdg" / "xdg-xfce" / "xfce4").mkdir(parents=True)
    _put(root, "usr/share/lindos/xdg-xfce-README", "readme\n")
    assert _run_script(tmp_path, root, "postinst", "configure").returncode == 0
    assert (root / "etc/xdg/xdg-xfce/README").is_file() and (root / "etc/xdg/xdg-xfce/xfce4").is_dir()


@needs_bash
def test_postrm_removes_the_directory_and_puts_mints_symlink_back(tmp_path):
    root = tmp_path / "root"
    _mint_links(root, "xdg-xfce")
    os.rename(str(root / "etc/xdg/xdg-xfce"), str(root / "etc/xdg/xdg-xfce.lindos-orig"))    # moved aside by hand
    _put(root, "etc/xdg/xdg-xfce/README", "x\n")
    res = _run_script(tmp_path, root, "postrm", "remove")
    assert res.returncode == 0, res.stderr
    assert (root / "etc/xdg/xdg-xfce").is_symlink() and not (root / "etc/xdg/xdg-xfce.lindos-orig").exists()


@needs_bash
def test_postrm_keeps_a_directory_that_holds_more_than_the_readme(tmp_path):
    root = tmp_path / "root"
    _put(root, "etc/xdg/xdg-xfce/README", "x\n")
    _put(root, "etc/xdg/xdg-xfce/xfce4/panel/mine.rc", "x\n")
    res = _run_script(tmp_path, root, "postrm", "remove")
    assert res.returncode == 0, res.stderr
    assert not (root / "etc/xdg/xdg-xfce/README").exists() and (root / "etc/xdg/xdg-xfce/xfce4/panel/mine.rc").is_file()


@needs_bash
def test_postrm_never_aborts_and_does_not_touch_files_outside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for action in ("remove", "upgrade", "abort-upgrade"):
        run_dir = tmp_path / action
        run_dir.mkdir()
        assert _run_script(run_dir, root, "postrm", action).returncode == 0, action


def _run_xsession(tmp: Path, etc: Path, dirs: str) -> str:
    env = dict(os.environ)
    env["LINDOS_XDG_ETC"] = _posix(etc)
    if dirs:
        env["XDG_CONFIG_DIRS"] = dirs
    else:
        env.pop("XDG_CONFIG_DIRS", None)
    script = _posix(XSESSION)
    res = subprocess.run([BASH, "--posix", "-c", '. "%s"; printf "%%s" "${XDG_CONFIG_DIRS-UNSET}"' % script], capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout


@needs_bash
def test_xsession_filter_drops_a_config_dir_that_resolves_into_mint_artwork(tmp_path):
    etc = tmp_path / "mint-artwork"                      # every xdg-* entry below resolves into a path with mint-artwork
    (etc / "xdg-xfce").mkdir(parents=True)
    out = _run_xsession(tmp_path, etc, "%s/xdg-xfce:/etc/xdg" % _posix(etc))
    assert out == "/etc/xdg"


@needs_bash
def test_xsession_filter_keeps_lindos_and_other_dirs_and_leaves_an_unset_variable_unset(tmp_path):
    etc = tmp_path / "etc-xdg"
    (etc / "xdg-xfce").mkdir(parents=True)
    dirs = "%s/xdg-xfce:%s:/usr/share/xdg" % (_posix(etc), _posix(etc))
    assert _run_xsession(tmp_path, etc, dirs) == dirs
    assert _run_xsession(tmp_path, etc, "") == "UNSET"


@needs_bash
def test_xsession_filter_falls_back_to_etc_xdg_when_only_mint_entries_are_left(tmp_path):
    etc = tmp_path / "mint-artwork"
    (etc / "xdg-xfce").mkdir(parents=True)
    assert _run_xsession(tmp_path, etc, "%s/xdg-xfce" % _posix(etc)) == _posix(etc)


@needs_bash
def test_xsession_filter_follows_a_real_symlink_into_mint_artwork(tmp_path):
    target = tmp_path / "usr" / "share" / "mint-artwork" / "xfce"
    target.mkdir(parents=True)
    etc = tmp_path / "etc-xdg"
    etc.mkdir()
    try:
        os.symlink(str(target), str(etc / "xdg-xfce"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    assert _run_xsession(tmp_path, etc, "%s/xdg-xfce:/etc/xdg" % _posix(etc)) == "/etc/xdg"


def test_xsession_script_conventions():
    raw = XSESSION.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw
    text = raw.decode("utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in ("exit ", "local ", "IFS=", "sudo", "[["):
        assert forbidden not in body, forbidden
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", XSESSION.name), "run-parts only lists names without dots"
    assert XSESSION.name > "60x11-common_xdg_path", "it must run after the script that puts xdg-$DESKTOP_SESSION in front"


# --------------------------------------------------------------------------- GRUB
def test_the_grub_drop_in_sorts_after_mints_and_the_old_one_is_gone():
    d = ROOT / "etc" / "default" / "grub.d"
    assert [p.name for p in d.iterdir()] == ["60-lindos-distributor.cfg"]
    assert "50_linuxmint.cfg" < "60-lindos-distributor.cfg"          # C locale: the glob order update-grub sources them in
    conf = (DEBIAN / "conffiles").read_text(encoding="utf-8").split()
    assert "/etc/default/grub.d/60-lindos-distributor.cfg" in conf and "/etc/default/grub.d/49-lindos-distributor.cfg" not in conf
    post = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert re.search(r"rm -f \"\$\{LINDOS_ROOT:-\}/etc/default/grub.d/49-lindos-distributor.cfg\"", post)


# --------------------------------------------------------------------------- default apps
def _shipped_text_files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and p.suffix in (".rc", ".list", ".desktop", ".json", ".xml", ".ini", ".conf", ""):
            try:
                yield p, p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def test_nothing_shipped_names_a_mint_app_as_a_default_favourite_or_pin():
    bad = re.compile(r"(?<![\w-])(xed|xviewer|xreader|pix|celluloid|warpinator|sticky|hypnotix|thingy)\.desktop|"
                     r"(?<![\w-])(io\.github\.celluloid_player\.Celluloid|org\.x\.(sticky|warpinator|hypnotix))\.desktop", re.I)
    hits = [str(p.relative_to(ROOT)) for p, t in _shipped_text_files()
            if p.name != "base-sweep.json" and not p.name.endswith(".pyc") and bad.search(t)]
    assert hits == [], hits
    for rc in list((ROOT / "etc" / "xdg" / "xfce4" / "panel").glob("*.rc")) + list((ROOT / "usr" / "share" / "lindos" / "modes").rglob("*.rc")):
        assert not bad.search(rc.read_text(encoding="utf-8")), rc


def test_shipped_default_apps_cover_text_images_pdf_and_media_with_real_desktop_ids():
    text = (ROOT / "usr" / "share" / "lindos" / "mimeapps-desktop.list").read_text(encoding="utf-8")
    _order, sections = mm.parse(text)
    defaults = dict((k, v) for k, v in sections["Default Applications"] if k)
    for mime, app in (("text/plain", "org.xfce.mousepad.desktop"), ("image/png", "org.xfce.ristretto.desktop"),
                      ("image/jpeg", "org.xfce.ristretto.desktop"), ("application/pdf", "org.gnome.Evince.desktop"),
                      ("video/mp4", "vlc.desktop"), ("audio/mpeg", "vlc.desktop"), ("video/x-matroska", "vlc.desktop")):
        assert defaults[mime].split(";")[0] == app, mime
    for mime, value in defaults.items():
        ids = [i for i in value.split(";") if i]
        assert ids and all(i.endswith(".desktop") and " " not in i for i in ids), (mime, value)
        assert re.fullmatch(r"[a-z0-9.+-]+/[A-Za-z0-9.+_-]+", mime), mime
    assert set(defaults) >= {"text/markdown", "application/x-shellscript", "image/svg+xml", "image/webp", "audio/flac", "video/webm"}


def test_the_mimeapps_merge_only_adds(tmp_path):
    src = (ROOT / "usr" / "share" / "lindos" / "mimeapps-desktop.list").read_text(encoding="utf-8")
    dst = "[Default Applications]\ntext/plain=kate.desktop\napplication/x-msi=lindos-run.desktop\n\n[Added Associations]\nimage/png=gimp.desktop;\n"
    new = mm.merge(src, dst)
    _o, sec = mm.parse(new)
    d = dict((k, v) for k, v in sec["Default Applications"] if k)
    assert d["text/plain"] == "kate.desktop", "a default somebody else set is never replaced"
    assert d["application/x-msi"] == "lindos-run.desktop" and d["image/jpeg"].startswith("org.xfce.ristretto.desktop")
    added = dict((k, v) for k, v in sec["Added Associations"] if k)
    assert added["image/png"] == "gimp.desktop;org.xfce.ristretto.desktop;"
    assert mm.merge(src, new) == new, "idempotent"


def test_the_merge_helper_works_through_lindos_root_and_never_fails(tmp_path, monkeypatch, capsys):
    _put(tmp_path, "usr/share/lindos/mimeapps-desktop.list", "[Default Applications]\ntext/plain=org.xfce.mousepad.desktop;\n")
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    assert mm.main([]) == 0
    assert "text/plain=org.xfce.mousepad.desktop;" in _get(tmp_path, "etc/xdg/mimeapps.list")
    before = _snapshot(tmp_path)
    assert mm.main([]) == 0 and _snapshot(tmp_path) == before
    assert mm.main(["--root", str(tmp_path / "nothing-here")]) == 0
    assert "not found" in capsys.readouterr().err
    assert mm.main(["--dry-run", "--root", str(tmp_path)]) == 0


def test_the_merge_keeps_comments_and_unknown_sections():
    new = mm.merge("[Default Applications]\nx/y=z.desktop;\n", "# my comment\n[Default Applications]\na/b=c.desktop;\n\n[Removed Associations]\nq/r=s.desktop;\n")
    assert new.startswith("# my comment\n") and "[Removed Associations]\nq/r=s.desktop;" in new and "x/y=z.desktop;" in new


def test_the_merge_helper_and_the_postinst_wiring():
    raw = (LIBEXEC / "merge-mimeapps.py").read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in raw
    post = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "/usr/libexec/lindos/merge-mimeapps.py" in post and post.count("merge-mimeapps.py") >= 2
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    recommends = re.search(r"^Recommends:(.*)$", control, flags=re.M).group(1)
    for pkg in ("mousepad", "ristretto", "evince", "vlc"):
        assert re.search(r"\b%s\b" % pkg, recommends), pkg


# --------------------------------------------------------------------------- notices, cursors, metadata
def test_the_legal_notices_say_what_lindos_is_built_on_and_are_honest_about_windows():
    text = (ROOT / "usr" / "share" / "lindos" / "legal" / "open-source-notices.txt").read_text(encoding="utf-8")
    for needle in ("GPL-3.0-or-later", "Ubuntu 24.04", "Debian", "Linux Mint", "not affiliated", "not Windows", "Selawik", "OFL",
                   "Fluent", "Mozilla", "/usr/share/doc/<package>/copyright", "THIRD_PARTY.md"):
        assert needle in text, needle
    assert "Steam is Valve's proprietary client" in text and "never part of the Lindos image" in text


def test_the_cursor_theme_has_the_lindos_name_everywhere_it_is_configured():
    for rel, key in (("etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml", 'name="CursorThemeName" type="string" value="Lindos-Cursors-Dark"'),
                     ("etc/xdg/gtk-3.0/settings.ini", "gtk-cursor-theme-name=Lindos-Cursors-Dark"),
                     ("etc/lightdm/slick-greeter.conf", "cursor-theme-name=Lindos-Cursors-Dark")):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert key in text and "Fluent-dark-cursors" not in text and "Fluent-cursors" not in text, rel
    # ... and in the code that writes the setting later (Setup, the dark/light switch, Settings' fallback and hint)
    for rel in ("packages/lindos-core/root/usr/lib/python3/dist-packages/lindos/theme.py",
                "packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/backend.py",
                "packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/pages/personalization.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "Fluent-dark-cursors" not in text and "Fluent-cursors" not in text, rel
    theme = (REPO / "packages/lindos-core/root/usr/lib/python3/dist-packages/lindos/theme.py").read_text(encoding="utf-8")
    assert 'CURSOR_DARK = "Lindos-Cursors-Dark"' in theme and 'CURSOR_LIGHT = "Lindos-Cursors"' in theme


def test_the_package_description_names_no_upstream_theme_as_the_cursor_and_no_base_distribution():
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    assert "Lindos cursors" in control and "Fluent cursors" not in control
    assert not re.search(r"\bMint\b", control.replace("mintinstall", ""))


def test_sweep_rules_stay_well_formed():
    for section in ("applications", "autostart"):
        for key in ("hide_if_exec_matches",):
            for pattern in RULES[section].get(key, []):
                re.compile(pattern)
        assert all(g == g.strip() and " " not in g for g in RULES[section].get("hide_globs", []))
    assert RULES["applications"]["icon_fallback"].startswith("lindos-")
    assert all(v.startswith("lindos-") for v in RULES["applications"]["icons"].values())
    assert all(isinstance(k, str) and k for k in RULES["applications"]["rename"])
    assert RULES["themes"]["icon_dirs"] == ["/usr/share/icons"]
    assert not any(fnmatch.fnmatchcase(n.lower(), g.lower()) for g in RULES["themes"]["hide_globs"] for n in ("hicolor", "Adwaita", "Lindos", "Lindos-dark", "Lindos-Cursors-Dark"))


def test_a_startup_wm_class_can_be_set_per_entry_so_a_running_window_matches_its_lindos_launcher(tmp_path):
    _put(tmp_path, A + "mintupdate.desktop", _entry("Update Manager", "mintupdate", "Icon=mintupdate\n"))
    rules = {"applications": {"dirs": ["/usr/share/applications"], "set": {"mintupdate.desktop": {"StartupWMClass": "mintUpdate.py"}}}}
    ctx = rb.Ctx(str(tmp_path), quiet=True)
    assert rb.sweep_desktop_dir(ctx, rules["applications"], "Lindos", "applications") == 1
    text = _get(tmp_path, A + "mintupdate.desktop")
    assert rb.get_key(text, "StartupWMClass") == "mintUpdate.py" and rb.get_key(text, "Exec") == "mintupdate"
    assert rb.sweep_desktop_dir(ctx, rules["applications"], "Lindos", "applications") == 0
