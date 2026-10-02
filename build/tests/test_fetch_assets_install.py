"""build/fetch-assets.sh: the generated install-into-chroot.sh gives the Lindos themes Lindos names.

The fetch script is run for real (bash, --offline --no-themes --no-fonts: nothing is downloaded) to generate the
installer; the installer is then run against a fake assets tree whose "upstream" install scripts imitate what the
Fluent scripts leave behind, into scratch THEMES_DEST / ICONS_DEST / HICOLOR_DEST directories.  Proven: cursor themes
are Lindos-Cursors / Lindos-Cursors-Dark with Lindos display names, the old Fluent-* names survive only as hidden
compatibility themes that inherit them, the GTK/icon theme index.theme files carry the Lindos names, the icon names
of the base's update/driver/store tools resolve to Lindos artwork, and no Mint theme is used as a fallback.
Needs bash (skipped otherwise).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
FETCH = REPO_ROOT / "build" / "fetch-assets.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_GTK_INSTALL = """#!/bin/bash
# imitates Fluent-gtk-theme/install.sh -d DEST -n NAME -c dark light
dest=""; name=""
while [ $# -gt 0 ]; do
    case "$1" in -d) dest="$2"; shift 2 ;; -n) name="$2"; shift 2 ;; *) shift ;; esac
done
for v in Dark Light; do
    d="${dest}/${name}-${v}"
    mkdir -p "${d}/xfwm4" "${d}/gtk-3.0"
    printf '[Desktop Entry]\\nType=X-GNOME-Metatheme\\nName=Fluent-%s\\nComment=Fluent Gtk+ theme\\n' "${v}" > "${d}/index.theme"
    printf 'button_layout=O|HMC\\n' > "${d}/xfwm4/themerc"
done
"""

FAKE_ICON_INSTALL = """#!/bin/bash
# imitates Fluent-icon-theme/install.sh -d DEST -n NAME
dest=""; name=""
while [ $# -gt 0 ]; do
    case "$1" in -d) dest="$2"; shift 2 ;; -n) name="$2"; shift 2 ;; *) shift ;; esac
done
for v in "" -dark -light; do
    d="${dest}/${name}${v}"
    mkdir -p "${d}/scalable/apps"
    printf '[Icon Theme]\\nName=Fluent%s\\nComment=Fluent icon theme for linux desktops\\nDirectories=scalable/apps\\n\\n[scalable/apps]\\nSize=128\\nType=Scalable\\n' "${v}" > "${d}/index.theme"
    printf '<svg/>\\n' > "${d}/scalable/apps/firefox.svg"
done
"""


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _w(p: Path, text: str, mode: int = 0o644) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    os.chmod(p, mode)


def _generate(tmp: Path) -> Path:
    assert BASH is not None
    assets = tmp / "assets"
    res = subprocess.run([BASH, _posix(FETCH), "--offline", "--no-themes", "--no-fonts", "--out", _posix(assets)], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    assert res.returncode == 0, res.stderr
    installer = assets / "install-into-chroot.sh"
    assert installer.is_file()
    return assets


def _fake_upstream(assets: Path) -> None:
    themes = assets / "themes"
    _w(themes / "Fluent-gtk-theme" / "install.sh", FAKE_GTK_INSTALL, 0o755)
    _w(themes / "Fluent-icon-theme" / "install.sh", FAKE_ICON_INSTALL, 0o755)
    for dist, name in (("dist", "Fluent-cursors"), ("dist-dark", "Fluent-dark-cursors")):
        _w(themes / "Fluent-icon-theme" / "cursors" / dist / "cursors" / "left_ptr", "cursor\n")
        _w(themes / "Fluent-icon-theme" / "cursors" / dist / "index.theme", "[Icon Theme]\nName=%s\n" % name)


def _run_installer(tmp: Path, assets: Path) -> Dict[str, Path]:
    assert BASH is not None
    dirs = {k: tmp / k for k in ("themes", "icons", "fonts", "hicolor")}
    for d in dirs.values():
        d.mkdir(exist_ok=True)
    for icon in ("lindos-update", "lindos-drivers", "lindos-store", "lindos-settings"):
        _w(dirs["hicolor"] / "scalable" / "apps" / (icon + ".svg"), "<svg id='%s'/>\n" % icon)
    fake = tmp / "fakebin"
    for tool in ("sassc", "fc-cache", "gtk-update-icon-cache"):          # no-ops: keep the run hermetic (no apt, no host font cache)
        _w(fake / tool, "#!/bin/sh\nexit 0\n", 0o755)
    env = dict(os.environ)
    env.update({"THEMES_DEST": _posix(dirs["themes"]), "ICONS_DEST": _posix(dirs["icons"]), "FONTS_DEST": _posix(dirs["fonts"]),
                "HICOLOR_DEST": _posix(dirs["hicolor"]), "PATH": _posix(fake) + os.pathsep + os.environ.get("PATH", "")})
    res = subprocess.run([BASH, _posix(assets / "install-into-chroot.sh"), _posix(assets)], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=300, check=False, env=env, cwd=str(tmp))
    dirs["stderr"] = tmp / "installer.stderr"
    dirs["stderr"].write_bytes(res.stderr.encode("utf-8"))
    dirs["rc"] = tmp / ("rc%d" % res.returncode)
    return dirs


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    if BASH is None:
        pytest.skip("bash not available on this host")
    tmp = tmp_path_factory.mktemp("assets")
    assets = _generate(tmp)
    _fake_upstream(assets)
    dirs = _run_installer(tmp, assets)
    assert dirs["rc"].name == "rc0", _text(dirs["stderr"])
    dirs["assets"] = assets
    return dirs


@needs_bash
def test_cursor_themes_are_installed_under_the_lindos_names(installed: Dict[str, Path]) -> None:
    icons = installed["icons"]
    for name, label in (("Lindos-Cursors", "Lindos Cursors"), ("Lindos-Cursors-Dark", "Lindos Cursors (Dark)")):
        assert (icons / name / "cursors" / "left_ptr").is_file(), name
        idx = _text(icons / name / "index.theme")
        assert "Name=%s\n" % label in idx and "Fluent" not in idx, idx


@needs_bash
def test_the_old_fluent_cursor_names_are_hidden_compatibility_themes_that_inherit_the_new_ones(installed: Dict[str, Path]) -> None:
    icons = installed["icons"]
    for old, new in (("Fluent-cursors", "Lindos-Cursors"), ("Fluent-dark-cursors", "Lindos-Cursors-Dark")):
        idx = _text(icons / old / "index.theme")
        assert "Inherits=%s\n" % new in idx and "Hidden=true" in idx
        assert not (icons / old / "cursors").exists(), "no cursors/ directory: no picker lists it, libXcursor follows Inherits"


@needs_bash
def test_gtk_and_icon_theme_index_files_carry_the_lindos_names(installed: Dict[str, Path]) -> None:
    for name in ("Lindos-Dark", "Lindos-Light"):
        idx = _text(installed["themes"] / name / "index.theme")
        assert "Name=%s\n" % name in idx and "Fluent" not in idx, idx
        assert (installed["themes"] / name / "xfwm4" / "themerc").is_file()
    for name in ("Lindos", "Lindos-dark", "Lindos-light"):
        idx = _text(installed["icons"] / name / "index.theme")
        assert "Name=%s\n" % name in idx and "Fluent" not in idx and "Lindos icon theme" in idx, idx
        assert "Directories=scalable/apps" in idx, "everything else in index.theme stays"


@needs_bash
def test_the_base_tools_icon_names_resolve_to_lindos_artwork(installed: Dict[str, Path]) -> None:
    for theme in ("Lindos", "Lindos-dark", "Lindos-light"):
        apps = installed["icons"] / theme / "scalable" / "apps"
        for alias, target in (("mintupdate", "lindos-update"), ("mintsources", "lindos-update"), ("mintdrivers", "lindos-drivers"),
                              ("mintinstall", "lindos-store"), ("mintreport", "lindos-settings"), ("mintlocale", "lindos-settings")):
            assert _text(apps / (alias + ".svg")) == "<svg id='%s'/>\n" % target, (theme, alias)
        assert (apps / "firefox.svg").is_file(), "the theme's own icons are untouched"


@needs_bash
def test_installing_twice_gives_the_same_tree(installed: Dict[str, Path], tmp_path: Path) -> None:
    def snap(d: Path) -> Dict[str, str]:
        return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in sorted(d.rglob("*")) if p.is_file()}
    before = {k: snap(installed[k]) for k in ("themes", "icons")}
    again = _run_installer(installed["themes"].parent, installed["assets"])
    assert again["rc"].name == "rc0", _text(again["stderr"])
    assert {k: snap(installed[k]) for k in ("themes", "icons")} == before


def test_the_installer_no_longer_falls_back_to_a_mint_theme_and_documents_the_new_names() -> None:
    text = FETCH.read_text(encoding="utf-8")
    assert not re.search(r"Mint-Y", text), "the Mint theme packs are purged from the image; an xfwm4 fallback to them would break"
    assert "Lindos-Cursors" in text and "Lindos-Cursors-Dark" in text
    licences = re.search(r"cat >\"\$\{LICENSES\}\" <<'EOS'\n(.*?)\nEOS", text, flags=re.S)
    assert licences and "Lindos-Cursors" in licences.group(1) and "Fluent-cursors" not in licences.group(1)
