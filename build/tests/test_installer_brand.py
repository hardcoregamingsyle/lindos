"""Installer branding: build/chroot/78-installer-brand.sh + build/installer/ (SPEC §2, §8;
docs/BUILDING.md "Installer branding").

The live installer is Ubiquity (Linux Mint's fork).  The hook rewrites its product name, launcher,
artwork and slideshow at ISO build time.  Nothing here needs Linux, Wine or a display:

  * structure — hook exists, is ordered after every apt hook and before 80-cleanup, is staged
    by build-iso.sh, shellcheck/`bash -n` clean;
  * content — the static slideshow is well-formed, offline, script-free, Mint-free and honest
    (anti-cheat caveat, Wine is not Windows, no telemetry); the GTK skin builds on Lindos-Dark;
  * behaviour — the hook is run for real (bash) against a *fake root* (LINDOS_INSTALLER_ROOT):
    launcher, debconf templates, .ui files, artwork, slideshow, idempotency, and graceful
    degradation when files/tools are missing.

The behaviour tests are skipped when bash is not available.  What they cannot show — how the
result looks in a real Ubiquity window — is listed in docs/BUILDING.md.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                      # build/tests -> repo root
HOOK = REPO_ROOT / "build" / "chroot" / "78-installer-brand.sh"
HOOK_DIR = HOOK.parent
BUILD_ISO = REPO_ROOT / "build" / "build-iso.sh"
INSTALLER_SRC = REPO_ROOT / "build" / "installer"
SLIDESHOW = INSTALLER_SRC / "slideshow"
SKIN_CSS = INSTALLER_SRC / "themes" / "Lindos-Setup" / "gtk-3.0" / "gtk.css"
LOGO = REPO_ROOT / "packages" / "lindos-desktop" / "root" / "usr" / "share" / "pixmaps" / "lindos-logo.svg"
BUILDING_MD = REPO_ROOT / "docs" / "BUILDING.md"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")


def _text(path: Path) -> str:
    assert path.is_file(), path
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def test_hook_exists_with_house_style() -> None:
    lines = _text(HOOK).splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "set -Eeuo pipefail" in lines
    assert any('/lib.sh"' in ln and ln.lstrip().startswith(".") for ln in lines), "must source lib.sh"
    assert b"\r" not in HOOK.read_bytes()
    assert "hook_begin" in _text(HOOK) and "hook_end" in _text(HOOK)


def test_hook_runs_after_every_package_hook_and_before_cleanup() -> None:
    """build-iso.sh runs hooks matching [0-9][0-9]-*.sh in sorted order."""
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    assert HOOK.name in names
    i = names.index(HOOK.name)
    before, after = names[:i], names[i + 1:]
    for pkg_hook in ("00-repos.sh", "10-debloat.sh", "20-base.sh", "30-lindos-debs.sh",
                     "40-theme.sh", "60-compat.sh", "70-gaming.sh", "75-vm.sh"):
        assert pkg_hook in before, f"{HOOK.name} must run after {pkg_hook} (nothing may reinstall ubiquity over it)"
    assert "80-cleanup.sh" in after, after
    # the hook globs used by the pipeline still match it
    assert re.fullmatch(r"[0-9][0-9]-.*\.sh", HOOK.name)


def test_build_iso_stages_the_installer_directory() -> None:
    t = _text(BUILD_ISO)
    assert '"${BUILD_DIR}/installer"' in t
    assert '"${STAGE_HOST}/installer/"' in t
    assert "cp -f \"${BUILD_DIR}/chroot/\"*.sh" in t, "hooks are still staged by the glob (no hook list to maintain)"


def test_hook_default_source_matches_where_build_iso_stages() -> None:
    assert '${LINDOS_STAGE_DIR}/installer' in _text(HOOK)
    assert 'STAGE="/tmp/lindos"' in _text(BUILD_ISO)


def _find_shellcheck() -> Optional[str]:
    """PATH first, then the binary bundled by the pip package shellcheck-py (as tests/run.sh does)."""
    found = shutil.which("shellcheck")
    if found:
        return found
    import sysconfig
    for scheme in (None, "nt_user", "posix_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        for name in ("shellcheck", "shellcheck.exe"):
            cand = os.path.join(d or "", name)
            if d and os.path.isfile(cand):
                return cand
    return None


@needs_bash
def test_hook_syntax_and_shellcheck() -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(HOOK)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60, check=False)
    assert res.returncode == 0, res.stderr
    sc = _find_shellcheck()
    if sc is None:
        pytest.skip("shellcheck not installed (tests/run.sh lints the hook when it is)")
    res = subprocess.run([sc, "-S", "warning", "-e", "SC1090,SC1091", "-x", str(HOOK)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=120, check=False, cwd=str(REPO_ROOT))
    assert res.returncode == 0, res.stdout + res.stderr


def test_hook_refuses_to_edit_a_build_host() -> None:
    t = _text(HOOK)
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t
    assert "die " in t


def test_hook_touches_no_partitioning_or_python() -> None:
    t = _text(HOOK)
    for forbidden in ("partman", "grub-installer", "parted", "mkfs", ".py\"", "dpkg-divert", "apt-get", "sudo "):
        assert forbidden not in t, forbidden


# --------------------------------------------------------------------------- #
# the static slideshow
# --------------------------------------------------------------------------- #
def _slideshow_html() -> str:
    return _text(SLIDESHOW / "index.html")


def test_slideshow_files_exist_and_are_lf() -> None:
    for name in ("index.html", "slides.css"):
        p = SLIDESHOW / name
        assert p.is_file(), p
        assert b"\r" not in p.read_bytes(), name


def test_slideshow_html_is_well_formed() -> None:
    root = ET.fromstring(_slideshow_html().split("\n", 1)[1])      # drop the (XML-illegal) doctype line
    assert root.tag == "html"
    assert root.attrib.get("lang") == "en"
    slides = [e for e in root.iter("section") if "slide" in e.attrib.get("class", "").split()]
    assert len(slides) == 6
    classes = [e.attrib["class"].split()[1] for e in slides]
    assert classes == ["s1", "s2", "s3", "s4", "s5", "s6"]
    for s in slides:
        assert s.find("h1") is not None and (s.find("h1").text or "").strip()


def test_slideshow_is_offline_and_script_free() -> None:
    html = _slideshow_html()
    css = _text(SLIDESHOW / "slides.css")
    assert "<script" not in html.lower()
    assert not re.search(r"\bon[a-z]+\s*=", html, re.I), "no inline event handlers"
    for blob in (html, css):
        assert "http://" not in blob and "https://" not in blob and "//cdn" not in blob
        assert "@import" not in blob
        assert not re.search(r"url\(\s*['\"]?(?:https?:|//|ftp:)", blob, re.I)
    # the only resources it references are the two files the hook installs next to it
    refs = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    assert refs == {"slides.css", "lindos-logo.svg"}, refs
    assert not re.search(r"<(iframe|object|embed|form|link\s[^>]*rel=\"(?!stylesheet))", html, re.I)


def test_slideshow_never_mentions_mint() -> None:
    for name in ("index.html", "slides.css"):
        assert "mint" not in _text(SLIDESHOW / name).lower(), name
    assert "mint" not in _text(SKIN_CSS).lower()


def test_slideshow_content_is_honest_and_on_message() -> None:
    plain = re.sub(r"<[^>]+>", " ", _slideshow_html())
    plain = re.sub(r"&#160;", " ", plain)
    text = re.sub(r"\s+", " ", plain)
    for must in ("Welcome to Lindos", "Everyday", "Gaming", "Work", "Creator", "Lite",
                 "Wine", "Proton", "Setup", "Steam", "Lutris", "Sober", "Transfer from Windows"):
        assert must in text, must
    low = text.lower()
    # honesty rules (SPEC 0.1): Wine is not Windows; anti-cheat caveat; browsers are not bundled; no telemetry
    assert "not windows" in low
    assert "no windows inside" in low
    assert "valorant" in low and "fortnite" in low and "anti-cheat" in low
    assert "protondb.com" in low and "areweanticheatyet.com" in low
    assert "never bundled" in low
    assert "no telemetry" in low
    assert "remove the installation medium" in low
    # never overclaim
    for banned in ("runs every", "100%", "all windows", "guaranteed", "bypass", "spoof"):
        assert banned not in low, banned


def test_slideshow_css_cycles_six_slides_and_uses_the_lindos_palette() -> None:
    css = _text(SLIDESHOW / "slides.css")
    for token in ("#202020", "#2B2B2B", "#60CDFF"):
        assert token.lower() in css.lower(), token
    assert css.count("animation-duration: 72s") == 2          # slides + dots
    delays = dict(re.findall(r"\.(s[1-6])\s*\{\s*animation-delay:\s*(\d+)s", css))
    assert delays == {"s1": "0", "s2": "12", "s3": "24", "s4": "36", "s5": "48", "s6": "60"}
    dots = dict(re.findall(r"\.(d[1-6])\s*\{\s*animation-delay:\s*(\d+)s", css))
    assert dots == {"d1": "0", "d2": "12", "d3": "24", "d4": "36", "d5": "48", "d6": "60"}
    assert "prefers-reduced-motion" in css
    assert css.count("{") == css.count("}")


def test_slideshow_logo_is_the_repo_logo() -> None:
    assert LOGO.is_file()
    ET.parse(LOGO)                                             # well-formed, the file the hook copies


# --------------------------------------------------------------------------- #
# the GTK skin
# --------------------------------------------------------------------------- #
def test_skin_builds_on_lindos_dark_and_stays_within_colours_and_buttons() -> None:
    css = _text(SKIN_CSS)
    assert css.lstrip().startswith("/*")
    imports = re.findall(r'@import url\("([^"]+)"\);', css)
    assert imports == ["../../Lindos-Dark/gtk-3.0/gtk.css"]
    assert css.count("{") == css.count("}")
    assert "#60CDFF" in css and "#2B2B2B" in css
    for hook in (".ubiquity-menubar", ".ubiquity-next", "progressbar"):
        assert hook in css, hook
    # colours/buttons only: nothing that changes geometry
    for geometry in ("padding", "margin", "min-height", "min-width", "font-size", "width", "height"):
        assert not re.search(rf"^\s*{geometry}\s*:", css, re.M), geometry
    assert "http" not in css and "mint" not in css.lower()


# --------------------------------------------------------------------------- #
# behaviour: run the hook for real against a fake root
# --------------------------------------------------------------------------- #
MINT_LAUNCHER = """[Desktop Entry]
Type=Application
Version=1.0
Name=Install RELEASE
Name[de]=RELEASE installieren
Name[fr]=Installer RELEASE
Comment=Install this system permanently to your hard disk
Keywords=ubiquity;
# Do not translate the word "RELEASE".  It is used as a marker by casper.
Exec=sudo --preserve-env=DBUS_SESSION_BUS_ADDRESS,XDG_DATA_DIRS,XDG_RUNTIME_DIR,GTK_THEME sh -c 'WEBKIT_DISABLE_COMPOSITING_MODE=1 ubiquity gtk_ui'
Icon=mintubiquity
Terminal=false
Categories=GTK;System;Settings;
#X-Linux Mint-Gettext-Domain=ubiquity-desktop
X-Ayatana-Appmenu-Show-Stubs=False
"""

TEMPLATES = """Name: ubiquity/text/live_installer
Template: ubiquity/text/live_installer
Owners: ubiquity
Type: text
Description: Install
Description-de.UTF-8: Installieren
Description-en_GB.UTF-8: Install

Name: ubiquity/text/install_ubuntu
Template: ubiquity/text/install_ubuntu
Owners: ubiquity
Type: text
Description: Install ${RELEASE}
Description-de.UTF-8: ${RELEASE} installieren

Template: ubiquity/text/try_install_text_label
Owners: ubiquity
Type: text
Description: You can try ${RELEASE} without making any changes to your computer,
 directly from this ${MEDIUM}.
 .
 Or if you're ready, you can install ${RELEASE} alongside (or instead of)
 your current operating system.

Name: ubiquity/text/label_using_bitlocker
Template: ubiquity/text/label_using_bitlocker
Owners: ubiquity
Type: text
Description: You need to turn off BitLocker in Windows before installing Linux Mint. For instructions, open help.ubuntu.com/bitlocker
Description-fr.UTF-8: Il faut desactiver BitLocker avant d'installer Linux Mint.

Name: ubiquity/partitioner/ubuntu_reinstall
Template: ubiquity/partitioner/ubuntu_reinstall
Type: text
Description: Erase ${DISTRO} and reinstall
Extended_description: This will delete all your ${DISTRO} programs.

Name: mintupdate/text/unrelated
Template: mintupdate/text/unrelated
Type: text
Description: Linux Mint stays ${RELEASE} here
"""

UI = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="PartitionBox" id="partitionbox">
    <property name="title">Linux Mint</property>
  </object>
  <object class="GtkLabel" id="l"><property name="label" translatable="yes">Erase Linux Mint and reinstall</property></object>
</interface>
"""

CASPER_STOP = '#! /bin/sh\nMSG="Please remove the installation medium, then press ENTER: "\n# Linux Mint\n'
MINT_SLIDE = "<html><body>Welcome to Linux Mint</body></html>\n"

FAKE_RSVG = r"""#!/bin/bash
# fake rsvg-convert: rsvg-convert -o OUT.png CANVAS.svg  ->  a PNG header of the canvas' size
out="$2"; svg="$3"
w="$(sed -n 's/.*width="\([0-9]*\)" height="\([0-9]*\)" viewBox.*/\1/p' "$svg")"
h="$(sed -n 's/.*width="\([0-9]*\)" height="\([0-9]*\)" viewBox.*/\2/p' "$svg")"
[ -n "${FAKE_RSVG_LOG:-}" ] && cp "$svg" "${FAKE_RSVG_LOG}.$(basename "$out" .png).$$" || true
[ -n "${FAKE_RSVG_SKEW:-}" ] && w=$((w + 1))
be32() { printf "\\$(printf %03o $(( ($1>>24)&255 )))\\$(printf %03o $(( ($1>>16)&255 )))\\$(printf %03o $(( ($1>>8)&255 )))\\$(printf %03o $(( $1&255 )))"; }
{ printf '\211PNG\r\n\032\n'; printf '\000\000\000\rIHDR'; be32 "$w"; be32 "$h"; printf '\010\006\000\000\000'; } > "$out"
"""


def _png(w: int, h: int, filler: bytes = b"ORIGINAL") -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + filler)


def _png_size(p: Path) -> tuple:
    d = p.read_bytes()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", p
    return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")


def _write(root: Path, rel: str, data) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_bytes(data.encode("utf-8"))                     # bytes: keep LF on Windows
    return p


def _mint_root(tmp: Path, *, with_theme: bool = True, with_logo: bool = True,
               with_slideshow_conf: bool = True) -> Path:
    root = tmp / "root"
    _write(root, "usr/share/applications/ubiquity.desktop", MINT_LAUNCHER)
    _write(root, "var/cache/debconf/templates.dat", TEMPLATES)
    _write(root, "usr/share/ubiquity/gtk/stepPartAuto.ui", UI)
    _write(root, "usr/share/ubiquity/gtk/stepLanguage.ui", "<interface>no brand here</interface>\n")
    _write(root, "usr/share/ubiquity/pixmaps/ubuntu_installed.png", _png(234, 165))
    _write(root, "usr/share/ubiquity/pixmaps/cd_in_tray.png", _png(154, 165))
    _write(root, "usr/share/ubiquity/pixmaps/windows_square.png", _png(48, 48))
    _write(root, "usr/share/ubiquity-slideshow/slides/index.html", MINT_SLIDE)
    _write(root, "usr/share/ubiquity-slideshow/slides/screenshots/welcome.png", b"MINT-SHOT")
    if with_slideshow_conf:
        _write(root, "usr/share/ubiquity-slideshow/slideshow.conf", "[Slideshow]\nwidth:800\nheight:500\n")
    _write(root, "usr/share/icons/hicolor/48x48/apps/mintubiquity.svg", "<svg>mint</svg>\n")
    _write(root, "usr/share/icons/hicolor/scalable/apps/ubiquity.svg", "<svg>ubuntu</svg>\n")
    _write(root, "usr/share/icons/hicolor/scalable/apps/keepme.svg", "<svg>other app</svg>\n")
    _write(root, "sbin/casper-stop", CASPER_STOP)
    if with_theme:
        _write(root, "usr/share/themes/Lindos-Dark/gtk-3.0/gtk.css", "/* Lindos-Dark */\n")
    if with_logo:
        _write(root, "usr/share/pixmaps/lindos-logo.svg", LOGO.read_bytes())
    return root


def _fake_rsvg(tmp: Path) -> Path:
    p = tmp / "fake-rsvg-convert"
    p.write_bytes(FAKE_RSVG.encode("utf-8"))
    p.chmod(0o755)
    return p


def _posix(p: Path) -> str:
    """Path as the bash under test wants it (Git Bash accepts C:/… too, cygpath is nicer)."""
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _run_hook(root: Path, *, src: Optional[Path] = INSTALLER_SRC, rsvg: Optional[str] = None,
              extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    assert BASH is not None
    env = dict(os.environ)
    env.update({
        "LINDOS_INSTALLER_ROOT": _posix(root),
        "LINDOS_INSTALLER_SRC": _posix(src) if src is not None else _posix(root / "no-such-stage-dir"),
        "LINDOS_RSVG": rsvg or "no-such-rsvg-convert",
        "LINDOS_STAGE_DIR": _posix(root / "no-such-stage"),
        "LINDOS_CONFIG_ENV": _posix(root / "no-such-config.env"),
    })
    env.update(extra_env or {})
    return subprocess.run([BASH, "-Eeuo", "pipefail", _posix(HOOK)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, check=False, env=env)


def _snapshot(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


@needs_bash
def test_full_run_rebrands_everything(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr

    # --- launcher ------------------------------------------------------------------------
    launcher = _read(root, "usr/share/applications/ubiquity.desktop")
    assert "\nName=Install Lindos\n" in launcher
    assert "\nName[de]=Lindos installieren\n" in launcher
    assert "\nName[fr]=Installer Lindos\n" in launcher
    assert "Icon=lindos-logo" in launcher and "mintubiquity" not in launcher
    assert "#X-Lindos-Gettext-Domain" in launcher and "Mint" not in launcher
    assert launcher.count("GTK_THEME=Lindos-Setup") == 1
    exec_line = next(ln for ln in launcher.splitlines() if ln.startswith("Exec="))
    assert exec_line.endswith("sh -c 'GTK_THEME=Lindos-Setup WEBKIT_DISABLE_COMPOSITING_MODE=1 ubiquity gtk_ui'")
    assert exec_line.startswith("Exec=sudo --preserve-env=DBUS_SESSION_BUS_ADDRESS,XDG_DATA_DIRS,XDG_RUNTIME_DIR,GTK_THEME sh -c '")
    # everything else in the launcher is untouched
    for keep in ("Type=Application", "Comment=Install this system permanently to your hard disk",
                 "Terminal=false", "Categories=GTK;System;Settings;", "X-Ayatana-Appmenu-Show-Stubs=False"):
        assert keep in launcher
    # casper's marker substitution (sed s/RELEASE/…/ on the Name= lines) now has nothing to do
    assert not re.search(r"^Name(\[[^\]]*\])?=.*RELEASE", launcher, re.M)

    # --- skin ----------------------------------------------------------------------------
    skin = root / "usr/share/themes/Lindos-Setup/gtk-3.0/gtk.css"
    assert skin.read_bytes() == SKIN_CSS.read_bytes()
    assert not (root / "usr/share/themes/Lindos-Setup/index.theme").exists()

    # --- debconf templates ---------------------------------------------------------------
    tpl = _read(root, "var/cache/debconf/templates.dat")
    assert "Description: Lindos Setup\n" in tpl                 # window title
    assert "Description-en_GB.UTF-8: Lindos Setup\n" in tpl
    assert "Description-de.UTF-8: Installieren\n" in tpl        # other languages keep their word
    assert "Description: Install Lindos\n" in tpl
    assert "Description-de.UTF-8: Lindos installieren\n" in tpl
    assert "You can try Lindos without making any changes" in tpl
    assert "you can install Lindos alongside" in tpl and "${MEDIUM}" in tpl   # other variables untouched
    assert "before installing Lindos. For instructions" in tpl
    assert "avant d'installer Lindos." in tpl
    assert "Erase Lindos and reinstall" in tpl and "delete all your Lindos programs" in tpl
    assert "Description: Linux Mint stays ${RELEASE} here" in tpl   # not a ubiquity stanza: untouched
    assert len(tpl.splitlines()) == len(TEMPLATES.splitlines())
    assert "${RELEASE}" not in tpl.replace("Linux Mint stays ${RELEASE} here", "")

    # --- .ui files and casper's eject prompt -----------------------------------------------
    ui = _read(root, "usr/share/ubiquity/gtk/stepPartAuto.ui")
    assert '<property name="title">Lindos</property>' in ui and "Erase Lindos and reinstall" in ui
    ET.fromstring(ui.split("\n", 1)[1])                          # still well-formed XML
    stop = _read(root, "sbin/casper-stop")
    assert "Please remove the installation medium, then press ENTER: " in stop and "Mint" not in stop

    # --- artwork ------------------------------------------------------------------------
    assert _png_size(root / "usr/share/ubiquity/pixmaps/ubuntu_installed.png") == (234, 165)
    assert _png_size(root / "usr/share/ubiquity/pixmaps/cd_in_tray.png") == (154, 165)
    assert b"ORIGINAL" not in (root / "usr/share/ubiquity/pixmaps/ubuntu_installed.png").read_bytes()
    assert (root / "usr/share/ubiquity/pixmaps/windows_square.png").read_bytes() == _png(48, 48)   # Windows detection icon stays
    logo = LOGO.read_bytes()
    assert (root / "usr/share/icons/hicolor/48x48/apps/mintubiquity.svg").read_bytes() == logo
    assert (root / "usr/share/icons/hicolor/scalable/apps/ubiquity.svg").read_bytes() == logo
    assert (root / "usr/share/icons/hicolor/scalable/apps/mintubiquity.svg").read_bytes() == logo
    assert _read(root, "usr/share/icons/hicolor/scalable/apps/keepme.svg") == "<svg>other app</svg>\n"

    # --- slideshow ------------------------------------------------------------------------
    slides = root / "usr/share/ubiquity-slideshow/slides"
    assert sorted(p.name for p in slides.iterdir()) == ["index.html", "lindos-logo.svg", "slides.css"]
    assert (slides / "index.html").read_bytes() == (SLIDESHOW / "index.html").read_bytes()
    assert (slides / "slides.css").read_bytes() == (SLIDESHOW / "slides.css").read_bytes()
    assert (slides / "lindos-logo.svg").read_bytes() == logo
    assert _read(root, "usr/share/ubiquity-slideshow/slideshow.conf").count("width:800") == 1   # base window size kept

    # --- nothing Mint-ish left in the installer dirs ---------------------------------------
    assert "no text file in the installer directories mentions Linux Mint any more" in res.stderr


@needs_bash
def test_second_run_changes_nothing(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    rsvg = _posix(_fake_rsvg(tmp_path))
    first = _run_hook(root, rsvg=rsvg)
    assert first.returncode == 0, first.stderr
    snap = _snapshot(root)
    second = _run_hook(root, rsvg=rsvg)
    assert second.returncode == 0, second.stderr
    assert _snapshot(root) == snap, "the hook is not idempotent"
    assert "already branded" in second.stderr
    assert _read(root, "usr/share/applications/ubiquity.desktop").count("GTK_THEME=Lindos-Setup") == 1


@needs_bash
def test_artwork_is_redrawn_from_the_logo_at_the_original_size(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    _write(root, "usr/share/ubiquity/pixmaps/ubuntu_installed.png", _png(300, 200))     # size is read, not assumed
    log = tmp_path / "rsvg-canvas"
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)), extra_env={"FAKE_RSVG_LOG": _posix(log)})
    assert res.returncode == 0, res.stderr
    assert _png_size(root / "usr/share/ubiquity/pixmaps/ubuntu_installed.png") == (300, 200)
    canvases = [p.read_text(encoding="utf-8") for p in tmp_path.glob("rsvg-canvas.*")]
    assert len(canvases) == 2, "one render per existing image (ubuntu_installed.png, cd_in_tray.png)"
    joined = "\n".join(canvases)
    assert 'width="300" height="200"' in joined and 'width="154" height="165"' in joined
    assert 'xlink:href="lindos-logo.svg"' in joined and 'preserveAspectRatio="xMidYMid meet"' in joined
    assert 'opacity="0.55"' in joined and 'opacity="1"' in joined
    assert "http://" not in joined.replace("http://www.w3.org/", "")


@needs_bash
def test_bad_or_missing_rsvg_keeps_original_artwork_and_still_succeeds(tmp_path: Path) -> None:
    for label, rsvg, env in (("missing", None, {}),
                             ("wrong-size", _posix(_fake_rsvg(tmp_path)), {"FAKE_RSVG_SKEW": "1"})):
        root = _mint_root(tmp_path / label)
        res = _run_hook(root, rsvg=rsvg, extra_env=env)
        assert res.returncode == 0, (label, res.stderr)
        assert (root / "usr/share/ubiquity/pixmaps/ubuntu_installed.png").read_bytes() == _png(234, 165), label
        assert "keeps its original artwork" in res.stderr or "keeping the original image" in res.stderr, label
        # the parts that need no rsvg still happened
        assert "Name=Install Lindos" in _read(root, "usr/share/applications/ubiquity.desktop")
        assert (root / "usr/share/ubiquity-slideshow/slides/index.html").read_bytes() == (SLIDESHOW / "index.html").read_bytes()


@needs_bash
def test_without_lindos_dark_the_skin_is_not_used(tmp_path: Path) -> None:
    root = _mint_root(tmp_path, with_theme=False)
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    launcher = _read(root, "usr/share/applications/ubiquity.desktop")
    assert "GTK_THEME=Lindos-Setup" not in launcher
    assert "sh -c 'WEBKIT_DISABLE_COMPOSITING_MODE=1 ubiquity gtk_ui'" in launcher      # Exec untouched
    assert not (root / "usr/share/themes/Lindos-Setup").exists()
    assert "Lindos-Dark GTK theme missing" in res.stderr


@needs_bash
def test_a_stale_skin_reference_is_removed_when_the_skin_is_gone(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    rsvg = _posix(_fake_rsvg(tmp_path))
    assert _run_hook(root, rsvg=rsvg).returncode == 0
    shutil.rmtree(root / "usr/share/themes")
    res = _run_hook(root, rsvg=rsvg)
    assert res.returncode == 0, res.stderr
    assert "GTK_THEME=Lindos-Setup" not in _read(root, "usr/share/applications/ubiquity.desktop")


@needs_bash
def test_unexpected_launcher_exec_is_left_alone_with_a_warning(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    odd = MINT_LAUNCHER.replace(
        "Exec=sudo --preserve-env=DBUS_SESSION_BUS_ADDRESS,XDG_DATA_DIRS,XDG_RUNTIME_DIR,GTK_THEME sh -c 'WEBKIT_DISABLE_COMPOSITING_MODE=1 ubiquity gtk_ui'",
        "Exec=ubiquity gtk_ui %U")
    _write(root, "usr/share/applications/ubiquity.desktop", odd)
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    launcher = _read(root, "usr/share/applications/ubiquity.desktop")
    assert "Exec=ubiquity gtk_ui %U" in launcher and "GTK_THEME" not in launcher
    assert "Name=Install Lindos" in launcher
    assert "not in the expected sh -c form" in res.stderr


@needs_bash
def test_launcher_without_logo_keeps_its_icon(tmp_path: Path) -> None:
    root = _mint_root(tmp_path, with_logo=False)
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    launcher = _read(root, "usr/share/applications/ubiquity.desktop")
    assert "Icon=mintubiquity" in launcher          # a launcher without any icon would be worse than Mint's
    assert (root / "usr/share/ubiquity/pixmaps/ubuntu_installed.png").read_bytes() == _png(234, 165)
    assert (root / "usr/share/icons/hicolor/48x48/apps/mintubiquity.svg").read_text(encoding="utf-8") == "<svg>mint</svg>\n"


@needs_bash
def test_slideshow_conf_is_created_only_when_absent(tmp_path: Path) -> None:
    root = _mint_root(tmp_path, with_slideshow_conf=False)
    assert _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path))).returncode == 0
    conf = _read(root, "usr/share/ubiquity-slideshow/slideshow.conf")
    assert "width:752" in conf and "height:442" in conf


@needs_bash
def test_slideshow_is_installed_even_when_the_base_had_none(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    shutil.rmtree(root / "usr/share/ubiquity-slideshow")
    assert _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path))).returncode == 0
    assert (root / "usr/share/ubiquity-slideshow/slides/index.html").is_file()


@needs_bash
def test_slides_symlink_from_the_base_is_replaced_not_followed(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    shutil.rmtree(root / "usr/share/ubiquity-slideshow/slides")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        (root / "usr/share/ubiquity-slideshow/slides").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"                 # never wrote through the link
    assert not (root / "usr/share/ubiquity-slideshow/slides").is_symlink()
    assert (root / "usr/share/ubiquity-slideshow/slides/index.html").is_file()


@needs_bash
def test_empty_root_only_warns(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    res = _run_hook(root)
    assert res.returncode == 0, res.stderr
    assert "WARNING" in res.stderr
    assert "no ubiquity.desktop" in res.stderr and "templates.dat" in res.stderr
    # the only thing it may create on its own is the slideshow (the base image had none)
    created = sorted(k.split("/")[0] for k in _snapshot(root))
    assert set(created) <= {"usr"}
    assert not (root / "var").exists()


@needs_bash
def test_missing_staged_inputs_only_warn(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    before_slides = _snapshot(root / "usr/share/ubiquity-slideshow")
    res = _run_hook(root, src=None, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    assert "no Lindos slideshow staged" in res.stderr and "skin staged" in res.stderr
    assert _snapshot(root / "usr/share/ubiquity-slideshow") == before_slides        # the base slideshow stays
    launcher = _read(root, "usr/share/applications/ubiquity.desktop")
    assert "Name=Install Lindos" in launcher and "GTK_THEME=Lindos-Setup" not in launcher


@needs_bash
def test_only_the_ubiquity_window_title_stanza_is_retitled(tmp_path: Path) -> None:
    """Another package's "Description: Install" stays; the rewrite adds and removes no line."""
    root = _mint_root(tmp_path)
    _write(root, "var/cache/debconf/templates.dat",
           "Name: other/text/x\nDescription: Install\n\nName: ubiquity/text/live_installer\nDescription: Install\n")
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    tpl = _read(root, "var/cache/debconf/templates.dat")
    assert tpl == ("Name: other/text/x\nDescription: Install\n\n"
                   "Name: ubiquity/text/live_installer\nDescription: Lindos Setup\n")


@needs_bash
def test_windows_detection_icon_and_partition_code_are_untouched(tmp_path: Path) -> None:
    root = _mint_root(tmp_path)
    _write(root, "usr/lib/ubiquity/plugins/ubi-partman.py", "title = 'linux mint'  # Linux Mint\n")
    before = _snapshot(root / "usr/lib")
    res = _run_hook(root, rsvg=_posix(_fake_rsvg(tmp_path)))
    assert res.returncode == 0, res.stderr
    assert _snapshot(root / "usr/lib") == before, "Python code must never be rewritten"
    assert "ubi-partman.py" in res.stderr                        # ... but the audit names it for the follow-up


# --------------------------------------------------------------------------- #
# docs
# --------------------------------------------------------------------------- #
def test_building_md_documents_the_hook_and_its_limits() -> None:
    doc = _text(BUILDING_MD)
    assert "78-installer-brand.sh" in doc
    assert "Installer branding" in doc
    for topic in ("templates.dat", "ubiquity-slideshow", "Lindos-Setup", "LINDOS_INSTALLER_ROOT",
                  "casper", ".mo"):
        assert topic in doc, topic
    assert "build/installer" in doc
