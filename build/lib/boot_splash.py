#!/usr/bin/env python3
"""boot_splash.py - the Lindos BIOS boot-menu background, drawn from the Lindos logo SVG (pure stdlib).

The base ISO's isolinux/splash.png is Linux Mint's ring logo.  This writes a 640x480 dark Lindos splash instead:
a soft blue glow on a near-black gradient with the Lindos mark (packages/lindos-desktop/.../pixmaps/lindos-logo.svg)
centred above the menu, in the place the base's logo sat.  Only Lindos's own artwork is used - the mark comes from
the repository's SVG (rects with rounded corners, polygons, linear gradients; anything else in the file, such as the
drop-shadow filter, is ignored) and the rest is generated here.  The output is deterministic.

Usage: boot_splash.py --svg lindos-logo.svg --out splash.png [--width 640] [--height 480] [--logo-size 100]
Exit codes: 0 ok, 1 error (unreadable SVG, nothing drawable, unwritable output), 2 usage.
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

Color = Tuple[float, float, float]
SVG_NS = "{http://www.w3.org/2000/svg}"

TOP = (0x26, 0x26, 0x26)          # background gradient, top ...
BOTTOM = (0x12, 0x12, 0x12)       # ... to bottom
GLOW = (0x00, 0x67, 0xC0)         # Lindos blue, blended in around the mark
GLOW_STRENGTH = 0.22
GLOW_RADIUS = 300.0
BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))   # ordered dither: no banding in the gradient


# --- the logo: the small SVG subset the mark uses -------------------------------------------------
class Gradient:
    def __init__(self, x1: float, y1: float, x2: float, y2: float, stops: List[Tuple[float, Color, float]]) -> None:
        self.p1, self.p2, self.stops = (x1, y1), (x2, y2), stops

    def at(self, u: float, v: float) -> Tuple[Color, float]:
        """Colour and opacity at the bounding-box coordinate (u, v) (both 0..1)."""
        dx, dy = self.p2[0] - self.p1[0], self.p2[1] - self.p1[1]
        length = dx * dx + dy * dy or 1.0
        t = max(0.0, min(1.0, ((u - self.p1[0]) * dx + (v - self.p1[1]) * dy) / length))
        prev = self.stops[0]
        for stop in self.stops:
            if t <= stop[0]:
                span = (stop[0] - prev[0]) or 1.0
                f = (t - prev[0]) / span if stop is not prev else 0.0
                color = tuple(prev[1][i] + (stop[1][i] - prev[1][i]) * f for i in range(3))
                return color, prev[2] + (stop[2] - prev[2]) * f  # type: ignore[return-value]
            prev = stop
        return prev[1], prev[2]


class Shape:
    def __init__(self, kind: str, geometry: Sequence[float], radius: float, fill: object, opacity: float) -> None:
        self.kind, self.geometry, self.radius, self.fill, self.opacity = kind, geometry, radius, fill, opacity
        if kind == "rect":
            x, y, w, h = geometry
            self.box = (x, y, x + w, y + h)
        else:
            xs, ys = geometry[0::2], geometry[1::2]
            self.box = (min(xs), min(ys), max(xs), max(ys))

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.box
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        if self.kind == "rect":
            r = self.radius
            dx = max(abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - r), 0.0)
            dy = max(abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - r), 0.0)
            return dx * dx + dy * dy <= r * r
        pts = list(zip(self.geometry[0::2], self.geometry[1::2]))
        inside = False
        for i, (ax, ay) in enumerate(pts):
            bx, by = pts[i - 1]
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
        return inside

    def paint(self, x: float, y: float) -> Tuple[Color, float]:
        x0, y0, x1, y1 = self.box
        if isinstance(self.fill, Gradient):
            color, alpha = self.fill.at((x - x0) / ((x1 - x0) or 1.0), (y - y0) / ((y1 - y0) or 1.0))
        else:
            color, alpha = self.fill, 1.0  # type: ignore[assignment]
        return color, alpha * self.opacity


def _hex(text: str) -> Color:
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", text.strip())
    if not m:
        raise ValueError("unsupported colour %r" % text)
    v = int(m.group(1), 16)
    return float(v >> 16), float((v >> 8) & 255), float(v & 255)


def _number(text: Optional[str], default: float = 0.0) -> float:
    return float(text) if text not in (None, "") else default


def parse_logo(svg_path: str) -> Tuple[Tuple[float, float], List[Shape]]:
    """((width, height) of the viewBox, drawable shapes in paint order)."""
    root = ET.parse(svg_path).getroot()
    vb = [float(p) for p in re.split(r"[ ,]+", (root.get("viewBox") or "").strip()) if p]
    if len(vb) != 4 or vb[0] != 0 or vb[1] != 0:
        raise ValueError("%s: needs a viewBox starting at 0 0" % svg_path)
    gradients: Dict[str, Gradient] = {}
    for g in root.iter(SVG_NS + "linearGradient"):
        stops = [(_number(s.get("offset")), _hex(s.get("stop-color", "#000000")), _number(s.get("stop-opacity"), 1.0))
                 for s in g.findall(SVG_NS + "stop")]
        if stops:
            gradients[g.get("id", "")] = Gradient(_number(g.get("x1")), _number(g.get("y1")), _number(g.get("x2"), 1.0),
                                                  _number(g.get("y2")), stops)
    shapes: List[Shape] = []

    def walk(el: ET.Element) -> None:
        for child in el:
            tag = child.tag.replace(SVG_NS, "")
            if tag == "defs":
                continue
            if tag == "g":
                walk(child)
                continue
            fill = child.get("fill", "#000000")
            m = re.fullmatch(r"url\(#([^)]+)\)", fill)
            paint: object = gradients[m.group(1)] if m else _hex(fill)
            opacity = _number(child.get("opacity"), 1.0)
            if tag == "rect":
                geometry = [_number(child.get(k)) for k in ("x", "y", "width", "height")]
                shapes.append(Shape("rect", geometry, _number(child.get("rx")), paint, opacity))
            elif tag == "polygon":
                nums = [float(n) for n in re.split(r"[ ,]+", (child.get("points") or "").strip()) if n]
                if len(nums) >= 6 and len(nums) % 2 == 0:
                    shapes.append(Shape("polygon", nums, 0.0, paint, opacity))

    walk(root)
    if not shapes:
        raise ValueError("%s: no rect or polygon to draw" % svg_path)
    return (vb[2], vb[3]), shapes


# --- the picture ----------------------------------------------------------------------------------
def _background(width: int, height: int, cx: float, cy: float) -> List[List[Color]]:
    rows: List[List[Color]] = []
    for y in range(height):
        t = y / max(height - 1, 1)
        base = [TOP[i] + (BOTTOM[i] - TOP[i]) * t for i in range(3)]
        row: List[Color] = []
        for x in range(width):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            g = max(0.0, 1.0 - d / GLOW_RADIUS) ** 2 * GLOW_STRENGTH
            row.append((base[0] + (GLOW[0] - base[0]) * g, base[1] + (GLOW[1] - base[1]) * g, base[2] + (GLOW[2] - base[2]) * g))
        rows.append(row)
    return rows


def _draw_logo(pixels: List[List[Color]], size: Tuple[float, float], shapes: List[Shape], left: int, top: int, px: int) -> None:
    scale = px / max(size)
    sub = 3
    for py in range(top, top + px):
        for pxx in range(left, left + px):
            acc = [0.0, 0.0, 0.0]
            backdrop = pixels[py][pxx]
            for sy in range(sub):
                for sx in range(sub):
                    x = (pxx - left + (sx + 0.5) / sub) / scale
                    y = (py - top + (sy + 0.5) / sub) / scale
                    color = backdrop
                    for shape in shapes:
                        if shape.contains(x, y):
                            c, a = shape.paint(x, y)
                            color = (color[0] + (c[0] - color[0]) * a, color[1] + (c[1] - color[1]) * a, color[2] + (c[2] - color[2]) * a)
                    acc[0] += color[0]
                    acc[1] += color[1]
                    acc[2] += color[2]
            pixels[py][pxx] = (acc[0] / (sub * sub), acc[1] / (sub * sub), acc[2] / (sub * sub))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(pixels: List[List[Color]]) -> bytes:
    """8-bit RGB PNG, 'Up' filtered (smooth gradients compress well that way); ordered dither on the way out."""
    height, width = len(pixels), len(pixels[0])
    raw = bytearray()
    prev = bytes(width * 3)
    for y, row in enumerate(pixels):
        line = bytearray()
        for x, (r, g, b) in enumerate(row):
            d = BAYER[y & 3][x & 3] / 16.0 - 0.5
            line.extend(min(255, max(0, int(c + 0.5 + d))) for c in (r, g, b))
        raw.append(2)
        raw.extend((line[i] - prev[i]) & 255 for i in range(len(line)))
        prev = bytes(line)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def render(svg_path: str, width: int = 640, height: int = 480, logo_size: int = 100) -> bytes:
    size, shapes = parse_logo(svg_path)
    left, top = (width - logo_size) // 2, max(18, height // 14)
    pixels = _background(width, height, width / 2.0, top + logo_size / 2.0)
    _draw_logo(pixels, size, shapes, left, top, logo_size)
    return encode_png(pixels)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="boot_splash.py", description="Draw the Lindos BIOS boot-menu background")
    parser.add_argument("--svg", required=True, help="the Lindos logo (lindos-logo.svg)")
    parser.add_argument("--out", required=True, help="PNG to write (isolinux/splash.png)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--logo-size", type=int, default=100)
    args = parser.parse_args(argv)
    if not (64 <= args.logo_size <= min(args.width, args.height)):
        parser.error("--logo-size must fit inside the picture")
    try:
        data = render(args.svg, args.width, args.height, args.logo_size)
        tmp = args.out + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, args.out)
    except (OSError, ValueError, ET.ParseError, KeyError) as exc:
        sys.stderr.write("boot_splash: ERROR: %s\n" % exc)
        return 1
    sys.stderr.write("boot_splash: wrote %s (%dx%d, %d bytes)\n" % (args.out, args.width, args.height, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
