"""The BIOS boot path stops showing Linux Mint: no /.disk/mint_iso marker, no Mint ring-logo splash, no Mint boot theme.

build/lib/boot_splash.py draws the Lindos splash from the Lindos logo SVG (pure stdlib); build-iso.sh's
lindos_bios_boot_art() applies it to the ISO tree.  The shell function is pulled out of the script and run in bash
against a fake ISO tree, so what the build does is tested, not just grepped.  What none of this can show - how
ISOLINUX/vesamenu really draws the picture and the menu on top of it - needs a BIOS boot in QEMU or on hardware.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import List, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
BUILD_ISO = REPO / "build" / "build-iso.sh"
SPLASH_PY = REPO / "build" / "lib" / "boot_splash.py"
LOGO = REPO / "packages" / "lindos-desktop" / "root" / "usr" / "share" / "pixmaps" / "lindos-logo.svg"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

spec = importlib.util.spec_from_file_location("lindos_boot_splash", SPLASH_PY)
assert spec and spec.loader
splash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splash)

MINT_SPLASH = b"\x89PNG-pretend-this-is-the-base-distribution-ring-logo"
MINT_LIVE_CFG = """\

timeout 100

menu background splash.png
menu title Welcome to Linux Mint 22.2 64-bit

menu color screen	37;40      #80ffffff #00000000 std
MENU COLOR border       30;44   #40ffffff #a0000000 std
MENU COLOR sel          7;37;40 #e0ffffff #20ffffff all
MENU COLOR unsel        37;44   #50ffffff #a0000000 std

label live
	menu label Start Lindos
	kernel /casper/vmlinuz
"""
MINT_STDMENU_CFG = """\
menu background splash.png
menu color title	* #FFFFFFFF *
menu color sel		* #ffffffff #76a1d0ff *
menu color hotsel	1;7;37;40 #ffffffff #76a1d0ff *
"""


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _decode(png: bytes) -> Tuple[int, int, List[bytes]]:
    """(width, height, rows of RGB bytes) of a PNG written by boot_splash.py (8-bit RGB, 'Up' filter)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])[0]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data)
            assert (depth, color, interlace) == (8, 2, 0)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    assert len(raw) == stride * height
    rows: List[bytes] = []
    prev = bytes(width * 3)
    for y in range(height):
        assert raw[y * stride] == 2
        prev = bytes((raw[y * stride + 1 + i] + prev[i]) & 255 for i in range(width * 3))
        rows.append(prev)
    return width, height, rows


@pytest.fixture(scope="module")
def lindos_splash() -> bytes:
    return splash.render(str(LOGO))


# ----------------------------------------------------------------------------- the picture
def test_the_splash_is_a_dark_640x480_picture_with_the_lindos_mark_where_the_menu_leaves_room(lindos_splash: bytes) -> None:
    width, height, rows = _decode(lindos_splash)
    assert (width, height) == (640, 480)

    def px(x: int, y: int) -> Tuple[int, int, int]:
        return rows[y][x * 3], rows[y][x * 3 + 1], rows[y][x * 3 + 2]

    # dark everywhere the menu text goes (light text on a dark picture stays readable)
    lower = [px(x, y) for y in range(160, 480, 8) for x in range(0, 640, 8)]
    assert max(max(p) for p in lower) < 0x48
    assert px(2, 2)[0] < 0x40 and px(637, 477)[0] < 0x20
    # the mark: Lindos blue tile and white "L" in the top-centre, nothing of the kind elsewhere
    mark = [px(x, y) for y in range(30, 140) for x in range(260, 380)]
    assert sum(1 for r, g, b in mark if b > 0xB0 and r < 0x90) > 2500          # gradient tile (#60CDFF .. #0067C0)
    assert sum(1 for r, g, b in mark if min(r, g, b) > 0xF0) > 300              # the white L and the spark
    rest = [px(x, y) for y in range(0, 480, 4) for x in range(0, 640, 4) if not (250 < x < 390 and 20 < y < 150)]
    assert sum(1 for r, g, b in rest if b > 0x60) == 0
    # centred
    blue_x = [x for x in range(640) if any(px(x, y)[2] > 0xB0 and px(x, y)[0] < 0x90 for y in range(30, 140, 3))]
    assert abs((min(blue_x) + max(blue_x)) / 2 - 320) <= 3


def test_the_splash_is_deterministic_and_not_a_copy_of_anything(lindos_splash: bytes) -> None:
    small = splash.render(str(LOGO), 160, 120, 64)
    assert small == splash.render(str(LOGO), 160, 120, 64)
    assert _decode(small)[:2] == (160, 120)
    assert lindos_splash != small and lindos_splash[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_generator_reports_problems_instead_of_writing_a_broken_picture(tmp_path: Path) -> None:
    out = tmp_path / "splash.png"
    assert splash.main(["--svg", str(tmp_path / "missing.svg"), "--out", str(out)]) == 1
    bad = tmp_path / "bad.svg"
    bad.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="3"/></svg>', encoding="utf-8")
    assert splash.main(["--svg", str(bad), "--out", str(out)]) == 1
    assert not out.exists() and not (tmp_path / "splash.png.tmp").exists()
    assert splash.main(["--svg", str(LOGO), "--out", str(out), "--width", "160", "--height", "120", "--logo-size", "64"]) == 0
    assert _decode(out.read_bytes())[:2] == (160, 120)


def test_the_generator_is_stdlib_only_lf_and_shipped_as_a_build_helper() -> None:
    raw = SPLASH_PY.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env python3") and b"\r" not in raw
    imports = set(re.findall(r"^(?:import|from)\s+([A-Za-z_]+)", raw.decode("utf-8"), flags=re.M))
    assert imports <= {"argparse", "os", "re", "struct", "sys", "xml", "zlib", "__future__", "typing"}, imports


# ----------------------------------------------------------------------------- build-iso.sh
def _function() -> str:
    text = BUILD_ISO.read_text(encoding="utf-8")
    m = re.search(r"^lindos_bios_boot_art\(\) \{\n.*?^\}\n", text, flags=re.M | re.S)
    assert m, "lindos_bios_boot_art() not found in build-iso.sh"
    return m.group(0)


def _tree(tmp: Path, isolinux: bool = True) -> Tuple[Path, Path, Path]:
    iso, work = tmp / "iso", tmp / "work"
    (iso / ".disk").mkdir(parents=True)
    (iso / ".disk" / "mint_iso").write_text("# Used for troubleshooting.\n# This filename is unique to Mint ISO images.", encoding="utf-8")
    (iso / "boot" / "grub" / "live-theme").mkdir(parents=True)
    (iso / "boot" / "grub" / "live-theme" / "theme.txt").write_text("title-text: \"\"\n", encoding="utf-8")
    (iso / "boot" / "grub" / "theme.cfg").write_text("set theme=/boot/grub/live-theme/theme.txt\n", encoding="utf-8")
    (iso / "boot" / "grub" / "grub.cfg").write_text("menuentry \"Install Lindos\" {\n}\n", encoding="utf-8")
    if isolinux:
        (iso / "isolinux").mkdir()
        (iso / "isolinux" / "splash.png").write_bytes(MINT_SPLASH)
        (iso / "isolinux" / "live.cfg").write_text(MINT_LIVE_CFG, encoding="utf-8", newline="\n")
        (iso / "isolinux" / "stdmenu.cfg").write_text(MINT_STDMENU_CFG, encoding="utf-8", newline="\n")
        (work / "orig" / "isolinux").mkdir(parents=True)
        (work / "orig" / "isolinux" / "splash.png").write_bytes(MINT_SPLASH)
    return iso, work, tmp / "build"


def _run(tmp: Path, iso: Path, work: Path, build: Path, root: Path = REPO) -> subprocess.CompletedProcess:
    shim = tmp / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "python3").write_text('#!/bin/sh\nexec "%s" "$@"\n' % _posix(Path(sys.executable)), encoding="utf-8", newline="\n")
    script = "\n".join([
        "set -Eeuo pipefail",
        "log() { :; }",
        'die() { echo "DIE: $*" >&2; exit 1; }',
        'ROOT="%s"; BUILD_DIR="%s"; ISO_DIR="%s"; WORK_DIR="%s"' % (_posix(root), _posix(build), _posix(iso), _posix(work)),
        "LINDOS_VERSION=1.0.0; LINDOS_CODENAME=Aurora",
        _function(),
        "lindos_bios_boot_art",
    ])
    env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, check=False)


def _stub_generator(build: Path, body: str) -> None:
    (build / "lib").mkdir(parents=True)
    (build / "lib" / "boot_splash.py").write_text(body, encoding="utf-8", newline="\n")


@needs_bash
def test_the_build_step_removes_the_mint_marker_and_boot_theme_and_draws_the_lindos_menu(tmp_path: Path) -> None:
    iso, work, _ = _tree(tmp_path)
    res = _run(tmp_path, iso, work, REPO / "build")
    assert res.returncode == 0, res.stderr
    assert not (iso / ".disk" / "mint_iso").exists()
    assert not (iso / "boot" / "grub" / "theme.cfg").exists() and not (iso / "boot" / "grub" / "live-theme").exists()
    assert (iso / "boot" / "grub" / "grub.cfg").is_file()
    data = (iso / "isolinux" / "splash.png").read_bytes()
    assert data != MINT_SPLASH and _decode(data)[:2] == (640, 480)
    live = (iso / "isolinux" / "live.cfg").read_text(encoding="utf-8")
    assert "menu title Lindos 1.0.0 (Aurora)" in live and "Mint" not in live
    assert "MENU COLOR sel          7;37;40 #ff000000 #ff60cdff all" in live        # ANSI and shadow fields survive
    assert "MENU COLOR unsel        37;44   #50ffffff #a0000000 std" in live and "menu background splash.png" in live
    std = (iso / "isolinux" / "stdmenu.cfg").read_text(encoding="utf-8")
    assert "menu color sel\t\t* #ff000000 #ff60cdff *" in std
    assert "menu color hotsel\t1;7;37;40 #ffffffff #76a1d0ff *" in std             # only the selection bar changes


@needs_bash
def test_without_isolinux_only_the_marker_and_theme_go(tmp_path: Path) -> None:
    iso, work, _ = _tree(tmp_path, isolinux=False)
    res = _run(tmp_path, iso, work, tmp_path / "no-generator-needed")
    assert res.returncode == 0, res.stderr
    assert not (iso / ".disk" / "mint_iso").exists() and not (iso / "boot" / "grub" / "theme.cfg").exists()
    assert not (iso / "isolinux").exists()


@needs_bash
def test_a_splash_the_owner_puts_in_the_overlay_wins_over_the_generated_one(tmp_path: Path) -> None:
    iso, work, build = _tree(tmp_path)
    _stub_generator(build, "import sys\nsys.exit(1)\n")                 # would fail the build if it were called
    (build / "overlay" / "isolinux").mkdir(parents=True)
    (build / "overlay" / "isolinux" / "splash.png").write_bytes(b"\x89PNG-the-owners-picture")
    (iso / "isolinux" / "splash.png").write_bytes(b"\x89PNG-the-owners-picture")      # what the overlay rsync put there
    res = _run(tmp_path, iso, work, build)
    assert res.returncode == 0, res.stderr
    assert (iso / "isolinux" / "splash.png").read_bytes() == b"\x89PNG-the-owners-picture"
    assert "menu title Lindos 1.0.0 (Aurora)" in (iso / "isolinux" / "live.cfg").read_text(encoding="utf-8")


@needs_bash
def test_the_build_dies_rather_than_ship_the_base_logo(tmp_path: Path) -> None:
    iso, work, build = _tree(tmp_path)
    _stub_generator(build, "import sys\nsys.exit(1)\n")
    res = _run(tmp_path, iso, work, build)
    assert res.returncode == 1 and "could not draw isolinux/splash.png" in res.stderr
    # a generator that leaves the base's picture in place is caught too
    iso, work, build = _tree(tmp_path / "second")
    _stub_generator(build, "pass\n")
    res = _run(tmp_path / "second", iso, work, build)
    assert res.returncode == 1 and "still the base's Mint logo" in res.stderr
    # and so is a missing logo
    iso, work, build = _tree(tmp_path / "third")
    res = _run(tmp_path / "third", iso, work, build, root=tmp_path / "nowhere")
    assert res.returncode == 1 and "Lindos logo" in res.stderr


def test_apply_overlay_runs_the_step_after_the_bios_menu_is_generated_and_before_the_branding_sed() -> None:
    text = BUILD_ISO.read_text(encoding="utf-8")
    body = text.split("\napply_overlay() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index("boot_menu.py") < body.index("lindos_bios_boot_art") < body.index("brand_file")
    assert "boot_splash.py" in _function() and "mint_iso" in _function()
