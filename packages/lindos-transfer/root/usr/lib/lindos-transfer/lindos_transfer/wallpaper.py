"""The desktop picture and how it fits the screen (SPEC-WINDOWS §29.8).

Windows keeps a re-encoded copy of the current wallpaper in
``%APPDATA%\\Microsoft\\Windows\\Themes\\TranscodedWallpaper`` (no extension: the format is sniffed from
its first bytes).  It is copied to ``~/Pictures/Wallpapers/windows-wallpaper.<ext>`` and applied
through ``lindos.theme`` with the matching xfdesktop image style.

Fit mode: the policy key ``Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System`` (managed PCs)
wins and uses its own enum (0 Center, 1 Tile, 2 Stretch, 3 Fit, 4 Fill, 5 Span); otherwise
``Control Panel\\Desktop`` ``WallpaperStyle``/``TileWallpaper`` (0+1 Tile, 0 Center, 2 Stretch, 6 Fit,
10 Fill, 22 Span).  Microsoft's own pictures (``C:\\Windows\\Web``) and Windows Spotlight images are not
copied: they belong to Microsoft, and Lindos has its own.
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import secrets, winreg
from .copyengine import CopyStats
from .report import ItemResult
from .sources import ci_path

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = ["TRANSCODED_REL", "XFCE_STYLES", "POLICY_STYLES", "windows_style", "sniff_extension", "is_microsoft_picture",
           "find_wallpaper", "apply_wallpaper", "plan_items", "run_item"]

log = logging.getLogger("lindos-transfer.wallpaper")

TRANSCODED_REL = ("AppData", "Roaming", "Microsoft", "Windows", "Themes", "TranscodedWallpaper")
#: fit mode -> xfdesktop ``image-style`` (0 none, 1 centered, 2 tiled, 3 stretched, 4 scaled, 5 zoomed, 6 spanning)
XFCE_STYLES: Dict[str, int] = {"center": 1, "tile": 2, "stretch": 3, "fit": 4, "fill": 5, "span": 6}
POLICY_STYLES: Dict[int, str] = {0: "center", 1: "tile", 2: "stretch", 3: "fit", 4: "fill", 5: "span"}
_CONTROL_PANEL_STYLES: Dict[str, str] = {"2": "stretch", "6": "fit", "10": "fill", "22": "span"}
_STYLE_WORDS = {"centre": "center", "centered": "center", "tiled": "tile", "stretched": "stretch",
                "fit": "fit", "fill": "fill", "span": "span", "center": "center", "tile": "tile", "stretch": "stretch"}
_MICROSOFT_PICTURE = re.compile(
    r"(^|\\)windows\\web\\|\\packages\\microsoftwindows\.client\.cbs_|contentdeliverymanager|"
    r"\\packages\\microsoft\.windows\.contentdelivery|\\irisservice\\|\\windows\\systemapps\\",
    re.IGNORECASE)
MAX_WALLPAPER = 256 << 20


def windows_style(settings: Dict[str, Optional[str]]) -> str:
    """Fit mode from :func:`winreg.wallpaper_settings` (policy first, with its own enum)."""
    pol = (settings.get("policy_style") or "").strip()
    if pol.isdigit() and int(pol) in POLICY_STYLES and (settings.get("policy_wallpaper") or "").strip():
        return POLICY_STYLES[int(pol)]
    style = (settings.get("style") or "").strip()
    tile = (settings.get("tile") or "").strip()
    if style in ("", "0"):
        return "tile" if tile == "1" else ("center" if style == "0" else "fill")
    return _CONTROL_PANEL_STYLES.get(style, "fill")


def style_from_word(word: Optional[str]) -> str:
    return _STYLE_WORDS.get((word or "").strip().lower(), "fill")


def sniff_extension(head: bytes) -> Optional[str]:
    """``jpg``/``png``/``bmp``/``gif``/``webp`` from the first bytes, else ``None``."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def is_microsoft_picture(path_text: Optional[str]) -> bool:
    return bool(path_text) and bool(_MICROSOFT_PICTURE.search(str(path_text)))


def find_wallpaper(ctx: "Context") -> Tuple[Optional[Path], str, List[str]]:
    """(picture file, fit mode, notes); ``None`` with a reason in notes when nothing should move."""
    src = ctx.source
    if src.is_bundle:
        rel = src.manifest.get("wallpaper")
        if not rel:
            return None, "fill", []
        return src.path(str(rel)), style_from_word(src.manifest.get("wallpaper_style")), []
    if ctx.user.profile_dir is None:
        return None, "fill", []
    settings = winreg.wallpaper_settings(ctx.ntuser) if ctx.ntuser is not None else {}
    original = (settings.get("policy_wallpaper") or settings.get("wallpaper") or "").strip()
    if ctx.ntuser is not None and not original:
        return None, "fill", ["Windows uses a plain colour (or a slideshow) as background - nothing to copy."]
    if is_microsoft_picture(original):
        return None, "fill", ["The Windows wallpaper is one of Microsoft's own pictures - Lindos keeps its own."]
    pic = ci_path(ctx.user.profile_dir, list(TRANSCODED_REL))
    return pic, windows_style(settings) if settings else "fill", []


def _head(path: Path) -> bytes:
    with secrets.safe_open(path) as fh:
        return fh.read(16)


def plan_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import LABELS, make_item

    pic, style, notes = find_wallpaper(ctx)
    if pic is None:
        return [], [], []
    try:
        ext = sniff_extension(_head(pic))
        size = pic.stat().st_size
    except OSError as exc:
        return [], [{"path": str(pic), "reason": f"cannot be read ({exc.strerror or exc})"}], []
    if ext is None:
        return [], [{"path": str(pic), "reason": "unknown picture format"}], []
    dest = ctx.dirs["pictures"] / "Wallpapers" / f"windows-wallpaper.{ext}"
    notes = notes + [f"Fit: {style}"]
    return [make_item("wallpaper", "wallpaper", LABELS["wallpaper"], pic, dest, files=1, bytes_=size,
                      notes=notes)], [], []


def apply_wallpaper(theme: Any, path: str, style: str) -> bool:
    """Set *path* as the Lindos wallpaper with the xfdesktop style for *style*."""
    xstyle = XFCE_STYLES.get(style, 5)
    setter = getattr(theme, "set_wallpaper", None)
    if setter is None:
        return False
    try:
        if "style" in inspect.signature(setter).parameters:
            return bool(setter(path, style=xstyle))
    except (TypeError, ValueError):
        pass
    ok = bool(setter(path))
    lister, xset = getattr(theme, "xfconf_list", None), getattr(theme, "xfconf_set", None)
    if lister is None or xset is None or xstyle == 5:
        return ok
    props = [p for p in lister("xfce4-desktop", "/backdrop")
             if re.match(r"^/backdrop/screen\d+/monitor[^/]+/workspace\d+/last-image$", p)]
    for prop in props:
        ok &= bool(xset("xfce4-desktop", prop.rsplit("/", 1)[0] + "/image-style", xstyle, "int"))
    return ok


def run_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    pic = Path(item["src"])
    try:
        ext = sniff_extension(_head(pic))
    except OSError as exc:
        return ItemResult.failed(item, f"cannot read the wallpaper ({exc.strerror or exc})")
    if ext is None:
        return ItemResult.skipped_item(item, "unknown picture format")
    _p, style, _n = find_wallpaper(ctx)
    dest = ctx.dirs["pictures"] / "Wallpapers" / f"windows-wallpaper.{ext}"
    stats: CopyStats = ctx.engine.copy_file(pic, dest, item=item["id"], rel="wallpaper")
    notes: List[str] = []
    final = stats.last_dest
    if final and not ctx.dry_run and not stats.errors:
        theme = ctx.theme()
        applied = False
        if theme is not None:
            try:
                applied = apply_wallpaper(theme, final, style)
            except Exception as exc:  # noqa: BLE001 - desktop settings must not fail the transfer
                log.warning("could not set the wallpaper: %s", exc)
        notes.append(f"Set as your wallpaper ({style})." if applied else
                     "Saved - set it in Settings > Personalisation > Background.")
    return ItemResult.from_stats(item, stats, dest=final or str(dest), notes=notes)
