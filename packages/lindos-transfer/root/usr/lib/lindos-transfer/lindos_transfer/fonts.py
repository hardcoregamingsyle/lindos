"""Fonts the user installed themselves (SPEC-WINDOWS §29.8).

Since Windows 10 1809 "Install" puts a font into ``%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`` (per
user).  Only that folder is copied, to ``~/.local/share/fonts/windows-user/``, followed by
``fc-cache -f``.  ``C:\\Windows\\Fonts`` is **never** copied: Microsoft's fonts are licensed for Windows
only.  For the same reason a font whose OS/2 vendor id is Microsoft's (``MS  ``) is skipped even when
it sits in the user folder; Office "cloud fonts" live elsewhere and are never read.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import secrets
from .copyengine import CopyStats
from .report import ItemResult

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = ["FONT_SUFFIXES", "USER_FONTS_REL", "DEST_REL", "REASON_MICROSOFT", "font_vendor", "is_microsoft_font",
           "font_source", "font_files", "plan_items", "run_item"]

log = logging.getLogger("lindos-transfer.fonts")

FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc", ".fon", ".fnt", ".pfb", ".pfm", ".afm")
USER_FONTS_REL = ("AppData", "Local", "Microsoft", "Windows", "Fonts")
DEST_REL = (".local", "share", "fonts", "windows-user")
REASON_MICROSOFT = "Microsoft font (licensed for use with Windows only) - not copied"
_SFNT_VERSIONS = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")


def font_vendor(path: Path) -> Optional[str]:
    """The 4-character OS/2 ``achVendID`` of a TrueType/OpenType font (first face of a collection)."""
    try:
        with secrets.safe_open(path) as fh:
            head = fh.read(12)
            if len(head) < 12:
                return None
            base = 0
            if head[:4] == b"ttcf":
                fh.seek(12)
                off = fh.read(4)
                if len(off) < 4:
                    return None
                base = struct.unpack(">I", off)[0]
                fh.seek(base)
                head = fh.read(12)
                if len(head) < 12:
                    return None
            if head[:4] not in _SFNT_VERSIONS:
                return None
            num_tables = struct.unpack(">H", head[4:6])[0]
            if num_tables > 512:
                return None
            fh.seek(base + 12)
            directory = fh.read(16 * num_tables)
            for i in range(len(directory) // 16):
                tag, _cs, offset, length = struct.unpack(">4sIII", directory[16 * i:16 * i + 16])
                if tag == b"OS/2":
                    if length < 62:
                        return None
                    fh.seek(offset + 58)
                    vend = fh.read(4)
                    return vend.decode("latin-1") if len(vend) == 4 else None
    except OSError:
        return None
    return None


def is_microsoft_font(path: Path) -> bool:
    return font_vendor(path) == "MS  "


def font_source(ctx: "Context") -> Optional[Path]:
    src = ctx.source
    if src.is_bundle:
        rel = src.manifest.get("fonts")
        return src.path(str(rel)) if rel else None
    if ctx.user.profile_dir is None:
        return None
    from .sources import ci_path

    return ci_path(ctx.user.profile_dir, list(USER_FONTS_REL))


def font_files(ctx: "Context", folder: Path) -> Tuple[List[Tuple[Path, Any]], List[Dict[str, str]]]:
    """(font files with their stat, skipped entries) below *folder*."""
    files: List[Tuple[Path, Any]] = []
    skipped: List[Dict[str, str]] = []
    for kind, path, rel, info in ctx.engine.walk(folder):
        if kind == "skip":
            skipped.append({"path": str(path), "reason": info})
            continue
        if not rel.lower().endswith(FONT_SUFFIXES):
            continue
        if is_microsoft_font(path):
            skipped.append({"path": str(path), "reason": REASON_MICROSOFT})
            continue
        files.append((path, info))
    return files, skipped


def plan_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import LABELS, make_item

    folder = font_source(ctx)
    if folder is None or not folder.is_dir():
        return [], [], []
    files, skipped = font_files(ctx, folder)
    if not files:
        return [], skipped, []
    dest = ctx.home.joinpath(*DEST_REL)
    notes = ["Only fonts you installed yourself are copied. Windows' own fonts are licensed for Windows only; "
             "Lindos has compatible free fonts."]
    return [make_item("fonts", "fonts", LABELS["fonts"], folder, dest, files=len(files),
                      bytes_=sum(st.st_size for _p, st in files), notes=notes)], skipped, []


def run_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    folder = Path(item["src"])
    dest = ctx.home.joinpath(*DEST_REL)
    files, skipped = font_files(ctx, folder)
    stats = CopyStats(skipped=skipped)
    for path, _st in files:
        stats.merge(ctx.engine.copy_file(path, dest / path.name, item=item["id"], rel=path.name))
    notes: List[str] = []
    if stats.files and not ctx.dry_run:
        if ctx.which("fc-cache"):
            try:
                proc = ctx.run(["fc-cache", "-f", str(dest)], capture_output=True, text=True, timeout=300,
                               check=False)
                if getattr(proc, "returncode", 1) != 0:
                    notes.append("The font list could not be refreshed; log out and back in to see the new fonts.")
            except OSError:
                notes.append("The font list could not be refreshed; log out and back in to see the new fonts.")
        else:
            notes.append("fontconfig is missing; log out and back in to see the new fonts.")
    return ItemResult.from_stats(item, stats, dest=str(dest), notes=notes)
