#!/usr/bin/env python3
"""plymouth-gen-assets.py — generate the raster assets of the Lindos Plymouth theme.

The lindos-desktop package ships only vector/text sources (SVG + lindos.script) because
binary PNGs cannot be authored in the repository.  This helper (called from the postinst
and from build/chroot/40-theme.sh, pure stdlib) writes into
``/usr/share/plymouth/themes/lindos/``:

* ``bar-bg.png``  1×1 #404040 pixel  (progress bar trough)
* ``bar-fg.png``  1×1 #60CDFF pixel  (progress bar fill / entry underline)
* ``dot.png``     anti-aliased #60CDFF disc, 32×32 (spinner)
* ``logo.png``    192×192 render of logo.svg — only when ``rsvg-convert`` (librsvg2-bin) is
                  installed; otherwise the script's text fallback ("Lindos") is used.

Everything is idempotent (files are rewritten only when missing or ``--force``) and the
theme works without any of these files.  Exit 0 unless the theme dir is unwritable.

Usage: plymouth-gen-assets.py [--theme-dir DIR] [--force] [--quiet]
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import struct
import subprocess
import sys
import zlib
from typing import List, Optional, Tuple

log = logging.getLogger("plymouth-gen-assets")

THEME_DIR = "/usr/share/plymouth/themes/lindos"
ACCENT = (0x60, 0xCD, 0xFF)
DIM = (0x40, 0x40, 0x40)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png_rgba(width: int, height: int, pixels: List[List[Tuple[int, int, int, int]]]) -> bytes:
    """Encode an RGBA pixel grid as a PNG file (8-bit, no interlace)."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def solid_png(rgb: Tuple[int, int, int]) -> bytes:
    r, g, b = rgb
    return png_rgba(1, 1, [[(r, g, b, 255)]])


def disc_png(size: int, rgb: Tuple[int, int, int], supersample: int = 4) -> bytes:
    """Anti-aliased filled circle on a transparent background."""
    r, g, b = rgb
    radius = size / 2.0
    grid: List[List[Tuple[int, int, int, int]]] = []
    for y in range(size):
        row: List[Tuple[int, int, int, int]] = []
        for x in range(size):
            inside = 0
            for sy in range(supersample):
                for sx in range(supersample):
                    px = x + (sx + 0.5) / supersample - radius
                    py = y + (sy + 0.5) / supersample - radius
                    if px * px + py * py <= (radius - 0.5) ** 2:
                        inside += 1
            alpha = int(round(255 * inside / (supersample * supersample)))
            row.append((r, g, b, alpha))
        grid.append(row)
    return png_rgba(size, size, grid)


def write_if_needed(path: str, data: bytes, force: bool) -> bool:
    if os.path.exists(path) and not force:
        log.info("keep %s", path)
        return False
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o644)
    log.info("wrote %s", path)
    return True


def render_logo(theme_dir: str, force: bool, size: int = 192) -> Optional[str]:
    svg = os.path.join(theme_dir, "logo.svg")
    png = os.path.join(theme_dir, "logo.png")
    if not os.path.isfile(svg):
        log.warning("%s missing; Plymouth uses the text fallback", svg)
        return None
    if os.path.isfile(png) and not force:
        log.info("keep %s", png)
        return png
    tool = shutil.which("rsvg-convert")
    if not tool:
        log.warning("rsvg-convert not found (install librsvg2-bin); Plymouth uses the text fallback")
        return None
    tmp = png + ".tmp"
    try:
        subprocess.run([tool, "-w", str(size), "-h", str(size), "-o", tmp, svg],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(tmp, png)
        os.chmod(png, 0o644)
        log.info("rendered %s", png)
        return png
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("rsvg-convert failed (%s); Plymouth uses the text fallback", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Lindos Plymouth theme raster assets")
    parser.add_argument("--theme-dir", default=os.environ.get("LINDOS_PLYMOUTH_DIR", THEME_DIR))
    parser.add_argument("--force", action="store_true", help="rewrite existing files")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="plymouth-gen-assets: %(levelname)s: %(message)s")
    theme_dir = args.theme_dir
    if not os.path.isdir(theme_dir):
        log.error("theme directory not found: %s", theme_dir)
        return 1
    try:
        write_if_needed(os.path.join(theme_dir, "bar-bg.png"), solid_png(DIM), args.force)
        write_if_needed(os.path.join(theme_dir, "bar-fg.png"), solid_png(ACCENT), args.force)
        write_if_needed(os.path.join(theme_dir, "dot.png"), disc_png(32, ACCENT), args.force)
    except OSError as exc:
        log.error("cannot write into %s: %s", theme_dir, exc)
        return 1
    render_logo(theme_dir, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
