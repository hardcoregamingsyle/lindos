"""Icon extraction from Windows programs (``wrestool`` + ``icotool`` from *icoutils*).

Extracted PNGs are installed into the user's hicolor theme
(``~/.local/share/icons/hicolor/<size>x<size>/apps/lindos-<slug>.png``) so the
generated ``.desktop`` file can simply say ``Icon=lindos-<slug>``.  Without icoutils
(or on failure) the generic ``lindos-exe`` icon is used.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import get_logger, user_home

__all__ = [
    "GENERIC_ICON",
    "STANDARD_SIZES",
    "icons_dir",
    "icon_target_path",
    "extract_icon_png",
    "install_app_icon",
    "pick_best_png",
    "remove_app_icon",
]

log = get_logger("lindos-compat.icons")

GENERIC_ICON = "lindos-exe"
STANDARD_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
_PNG_NAME_RE = re.compile(r"_(\d+)x(\d+)x(\d+)\.png$")


def icons_dir(home: Optional[Path] = None) -> Path:
    return (home or user_home()) / ".local/share/icons/hicolor"


def icon_target_path(slug: str, size: int = 256, home: Optional[Path] = None) -> Path:
    return icons_dir(home) / f"{size}x{size}" / "apps" / f"lindos-{slug}.png"


def _png_dims(path: Path) -> Tuple[int, int, int]:
    """(width, height, bpp) from icotool's ``name_N_WxHxB.png`` naming, else from the PNG header."""
    m = _PNG_NAME_RE.search(path.name)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        with open(path, "rb") as fh:
            head = fh.read(29)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w = int.from_bytes(head[16:20], "big")
            h = int.from_bytes(head[20:24], "big")
            return w, h, 32
    except OSError:
        pass
    return 0, 0, 0


def pick_best_png(pngs: list[Path], preferred: int = 256) -> Optional[Path]:
    """Prefer exactly ``preferred`` px, then the largest; higher bit depth wins ties."""
    if not pngs:
        return None

    def key(p: Path) -> Tuple[int, int, int]:
        w, _h, bpp = _png_dims(p)
        exact = 1 if w == preferred else 0
        return (exact, w, bpp)

    return max(pngs, key=key)


def extract_icon_png(source: Path, out_dir: Path, *, preferred: int = 256,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Optional[Path]:
    """Extract the best icon of an ``.exe``/``.dll``/``.ico`` into ``out_dir``; returns the PNG path."""
    icotool = which("icotool")
    if not icotool:
        return None
    source = Path(source)
    if not source.is_file():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lindos-icon-") as td:
        tmp = Path(td)
        ico_files: list[Path] = []
        if source.suffix.lower() == ".ico":
            ico_files = [source]
        else:
            wrestool = which("wrestool")
            if not wrestool:
                return None
            ico_dir = tmp / "ico"
            ico_dir.mkdir()
            try:
                run([wrestool, "-x", "-t", "14", "-o", str(ico_dir), str(source)], capture_output=True,
                    text=True, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("wrestool failed on %s: %s", source, exc)
                return None
            ico_files = sorted(ico_dir.glob("*.ico"), key=lambda p: p.stat().st_size, reverse=True)
            if not ico_files:
                return None
        png_dir = tmp / "png"
        png_dir.mkdir()
        for ico in ico_files[:3]:  # the biggest group icons are almost always the app icon
            try:
                run([icotool, "-x", "-o", str(png_dir), str(ico)], capture_output=True, text=True,
                    timeout=120, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("icotool failed on %s: %s", ico, exc)
                continue
            if any(png_dir.glob("*.png")):
                break
        best = pick_best_png(list(png_dir.glob("*.png")), preferred)
        if not best:
            return None
        w, _h, _bpp = _png_dims(best)
        dest = out_dir / f"icon_{w or preferred}.png"
        shutil.copyfile(best, dest)
        return dest


def install_app_icon(source: Path, slug: str, *, home: Optional[Path] = None,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Tuple[str, Optional[Path]]:
    """Extract ``source``'s icon into the user's hicolor theme.

    Returns ``(icon_name, installed_path)``: ``("lindos-<slug>", path)`` on success,
    ``("lindos-exe", None)`` otherwise.
    """
    with tempfile.TemporaryDirectory(prefix="lindos-icon-") as td:
        png = extract_icon_png(Path(source), Path(td), which=which, run=run)
        if not png:
            return GENERIC_ICON, None
        w, _h, _bpp = _png_dims(png)
        size = w if w in STANDARD_SIZES else 256
        target = icon_target_path(slug, size, home)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(png, target)
        except OSError as exc:
            log.debug("cannot install icon %s: %s", target, exc)
            return GENERIC_ICON, None
    # nudge icon caches (GTK checks the theme dir mtime; the cache tool is optional)
    try:
        os.utime(icons_dir(home), (time.time(), time.time()))
    except OSError:
        pass
    updater = which("gtk-update-icon-cache")
    if updater:
        try:
            run([updater, "-q", "-t", "-f", str(icons_dir(home))], capture_output=True, text=True,
                timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return f"lindos-{slug}", target


def remove_app_icon(slug: str, home: Optional[Path] = None) -> int:
    """Delete every ``lindos-<slug>.png`` from the user hicolor theme; returns count."""
    root = icons_dir(home)
    n = 0
    if not root.is_dir():
        return 0
    for p in root.glob(f"*/apps/lindos-{slug}.png"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
