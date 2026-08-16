"""Package-level sanity: DEBIAN/control, .desktop, MIME/Thunar XML, maintainer scripts, bins."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# Paths computed here (not imported from conftest) so the module also loads under
# pytest's --import-mode=importlib used by tests/run.sh.
PKG_ROOT = Path(__file__).resolve().parent.parent          # packages/lindos-compat
ROOT = PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIB = ROOT / "usr" / "lib" / "lindos-compat"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SHARE = ROOT / "usr" / "share" / "lindos"
APPS = ROOT / "usr" / "share" / "applications"
DEBIAN = PKG_ROOT / "DEBIAN"


def _control() -> dict:
    fields = {}
    for line in (DEBIAN / "control").read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or not line.strip():
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    return fields


def test_control_fields():
    c = _control()
    assert c["Package"] == "lindos-compat"
    assert c["Version"] == "1.0.0"
    assert c["Architecture"] == "all"
    assert c["Maintainer"] == "Lindos Team <team@lindos.dev>"
    for dep in ("python3", "lindos-core", "cabextract", "winbind", "xdg-utils", "desktop-file-utils", "shared-mime-info"):
        assert dep in c["Depends"], dep
    for rec in ("winehq-staging | wine-staging | wine", "winetricks", "umu-launcher", "icoutils", "zenity", "gamemode",
                "mangohud", "libvulkan1", "mesa-vulkan-drivers", "fonts-liberation"):
        assert rec in c["Recommends"], rec
    assert "bottles" in c["Suggests"]
    assert not (DEBIAN / "conffiles").exists()


def test_maintainer_scripts_are_posix_sh_and_idempotent_by_design():
    for name in ("postinst", "postrm"):
        text = (DEBIAN / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert "set -e" in text.splitlines()[:20] or "\nset -e\n" in text
        assert "\r" not in text
    post = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert "mimeapps-lindos.list" in post and "/etc/xdg/mimeapps.list" in post
    assert "thunar-uca-lindos.xml" in post and "/etc/xdg/Thunar/uca.xml" in post
    assert "update-desktop-database" in post and "update-mime-database" in post
    rm = (DEBIAN / "postrm").read_text(encoding="utf-8")
    assert "lindos-run.desktop" in rm and "unique-id" in rm


def test_desktop_file():
    text = (APPS / "lindos-run.desktop").read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n")
    fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("["))
    assert fields["Name"] == "Windows App Runner (Lindos)"
    assert fields["Exec"] == "lindos-run %f"
    assert fields["NoDisplay"] == "true"
    assert fields["Icon"] == "lindos-exe"
    for mt in ("application/x-ms-dos-executable", "application/x-msdownload", "application/x-msi",
               "application/x-ms-shortcut", "application/x-bat"):
        assert mt + ";" in fields["MimeType"], mt
    assert (ROOT / "usr/share/icons/hicolor/scalable/apps/lindos-exe.svg").is_file()


def test_mimeapps_list_and_xml_fragments():
    text = (SHARE / "mimeapps-lindos.list").read_text(encoding="utf-8")
    assert "[Default Applications]" in text
    for mt in ("application/x-ms-dos-executable", "application/x-msdownload", "application/x-msi",
               "application/x-ms-shortcut", "application/x-bat"):
        assert f"{mt}=lindos-run.desktop" in text
    uca = ET.parse(SHARE / "thunar-uca-lindos.xml").getroot()
    assert uca.tag == "actions"
    names = [a.findtext("name") for a in uca.findall("action")]
    assert names == ["Run with Lindos (Windows app)", "Run with Proton (game)", "Open C:\\ drive"]
    ids = [a.findtext("unique-id") for a in uca.findall("action")]
    assert len(set(ids)) == 3 and all(ids)
    cmds = [a.findtext("command") for a in uca.findall("action")]
    assert cmds[0] == "lindos-run %f" and "--runner umu" in cmds[1] and cmds[2].startswith("lindos-compat prefixes open")
    mime = ET.parse(ROOT / "usr/share/mime/packages/lindos-windows.xml").getroot()
    ns = "{http://www.freedesktop.org/standards/shared-mime-info}"
    types = {t.get("type"): t for t in mime.findall(f"{ns}mime-type")}
    assert set(types) >= {"application/x-ms-dos-executable", "application/x-msi", "application/x-bat",
                          "application/x-ms-shortcut"}
    globs = {g.get("pattern") for t in types.values() for g in t.findall(f"{ns}glob")}
    assert {"*.exe", "*.msi", "*.bat", "*.lnk"} <= globs
    assert types["application/x-ms-dos-executable"].findtext(f"{ns}comment") == "Windows program"


def test_bins_and_lib_layout():
    for name in ("lindos-run", "lindos-compat"):
        text = (BIN / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env python3\n")
        assert "/usr/lib/lindos-compat" in text and "lindos_compat" in text
    for mod in ("__init__", "lnk", "prefix", "runner", "recipes", "doctor", "icons", "scan", "gui", "installers",
                "cli_run", "cli_compat"):
        assert (LIB / "lindos_compat" / f"{mod}.py").is_file(), mod
    assert (LIBEXEC / "install-compat.sh").read_text(encoding="utf-8").startswith("#!/bin/bash\n")
    assert "set -Eeuo pipefail" in (LIBEXEC / "install-compat.sh").read_text(encoding="utf-8")
    assert (SHARE / "compat" / "README.md").is_file()
    assert len(list((SHARE / "recipes").glob("*.json"))) >= 15


def test_bin_scripts_run_help():
    """The thin launchers must find the package relative to themselves (repo checkout layout)."""
    env = dict(os.environ)
    core_lib = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(core_lib), env.get("PYTHONPATH", "")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    for name, expect in (("lindos-run", "--info"), ("lindos-compat", "doctor")):
        proc = subprocess.run([sys.executable, str(BIN / name), "--help"], capture_output=True, text=True, env=env,
                              timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        assert expect in proc.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_postinst_postrm_merge_roundtrip(tmp_path: Path):
    """Run the real maintainer scripts against scratch files (paths are env-overridable)."""
    sh = shutil.which("sh")
    xdg = tmp_path / "xdg"
    (xdg / "Thunar").mkdir(parents=True)
    mimeapps = xdg / "mimeapps.list"
    uca = xdg / "Thunar" / "uca.xml"
    mimeapps.write_text("[Default Applications]\ntext/plain=mousepad.desktop\napplication/x-msi=other.desktop\n\n"
                        "[Added Associations]\napplication/x-msi=other.desktop;\n", encoding="utf-8")
    uca.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n<action>\n<icon>utilities-terminal</icon>\n'
                   '<name>Open Terminal Here</name>\n<unique-id>1-1</unique-id>\n<command>xfce4-terminal</command>\n'
                   '<description/>\n<patterns>*</patterns>\n<directories/>\n</action>\n</actions>\n', encoding="utf-8")
    env = dict(os.environ, LINDOS_MIMEAPPS_SRC=str(SHARE / "mimeapps-lindos.list"), LINDOS_MIMEAPPS_DST=str(mimeapps),
               LINDOS_UCA_SRC=str(SHARE / "thunar-uca-lindos.xml"), LINDOS_UCA_DST=str(uca))
    if sys.platform.startswith("win"):
        # Git Bash: the heredoc'ed python must be the same interpreter as the test run
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        if not shutil.which("python3", path=env["PATH"]):
            pytest.skip("no python3 on PATH for the maintainer scripts")

    def run(script: str, arg: str) -> None:
        proc = subprocess.run([sh, str(DEBIAN / script), arg], capture_output=True, text=True, env=env, timeout=120,
                              check=False)
        assert proc.returncode == 0, proc.stderr

    run("postinst", "configure")
    text = mimeapps.read_text(encoding="utf-8")
    assert "application/x-ms-dos-executable=lindos-run.desktop" in text
    assert "application/x-msi=other.desktop\n" in text                     # admin's default kept
    assert "application/x-msi=other.desktop;lindos-run.desktop;" in text  # but we are an added association
    assert text.count("application/x-ms-dos-executable=lindos-run.desktop\n") == 1
    ids = [a.findtext("unique-id") for a in ET.parse(uca).getroot().findall("action")]
    assert ids == ["1-1", "1755000000000001-1", "1755000000000002-2", "1755000000000003-3"]
    run("postinst", "configure")  # idempotent
    assert mimeapps.read_text(encoding="utf-8") == text
    assert [a.findtext("unique-id") for a in ET.parse(uca).getroot().findall("action")] == ids
    run("postrm", "remove")
    text = mimeapps.read_text(encoding="utf-8")
    assert "lindos-run.desktop" not in text and "text/plain=mousepad.desktop" in text
    assert "application/x-msi=other.desktop;" in text
    assert [a.findtext("unique-id") for a in ET.parse(uca).getroot().findall("action")] == ["1-1"]
    # fresh system: files are created, then removed again on purge
    mimeapps.unlink()
    uca.unlink()
    run("postinst", "configure")
    assert "application/x-msi=lindos-run.desktop" in mimeapps.read_text(encoding="utf-8")
    assert len(ET.parse(uca).getroot().findall("action")) == 3
    run("postrm", "purge")
    assert not mimeapps.exists() and not uca.exists()
