"""Tests for the user-installed-fonts-only transfer item (SPEC-WINDOWS §29.8)."""
from __future__ import annotations

import struct
import types
from pathlib import Path

from lindos_transfer.fonts import (DEST_REL, REASON_MICROSOFT, USER_FONTS_REL, font_files, font_source,
                                   font_vendor, is_microsoft_font, plan_items, run_item)


def _sfnt_font(vendor: bytes) -> bytes:
    """A minimal, structurally valid TrueType font with one OS/2 table carrying *vendor*."""
    os2 = b"\x00" * 58 + vendor + b"\x00" * 2  # achVendID sits at OS/2 offset 58, 4 bytes
    assert len(os2) >= 62
    num_tables = 1
    header = struct.pack(">I H H H H", 0x00010000, num_tables, 0, 0, 0)
    dir_entry = struct.pack(">4s I I I", b"OS/2", 0, 12 + 16, len(os2))
    return header + dir_entry + os2


def test_font_vendor_reads_os2_vendid() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "font.ttf"
        p.write_bytes(_sfnt_font(b"MS  "))
        assert font_vendor(p) == "MS  "
        assert is_microsoft_font(p) is True
        p2 = Path(td) / "other.ttf"
        p2.write_bytes(_sfnt_font(b"GOOG"))
        assert font_vendor(p2) == "GOOG"
        assert is_microsoft_font(p2) is False


def test_font_vendor_returns_none_for_garbage(tmp_path: Path) -> None:
    p = tmp_path / "notafont.ttf"
    p.write_bytes(b"not a font at all")
    assert font_vendor(p) is None
    assert is_microsoft_font(p) is False


def test_font_source_partition_and_bundle(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    fonts_dir = profile.joinpath(*USER_FONTS_REL)
    fonts_dir.mkdir(parents=True)
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                               user=types.SimpleNamespace(profile_dir=profile))
    assert font_source(ctx) == fonts_dir

    class BundleSource:
        is_bundle = True
        manifest = {"fonts": "fonts"}

        def path(self, rel):
            base = tmp_path / "bundle"
            (base / rel).mkdir(parents=True, exist_ok=True)
            return base / rel

    ctx2 = types.SimpleNamespace(source=BundleSource())
    assert ctx2.source.path("fonts").is_dir()


def test_font_files_skips_microsoft_and_non_fonts(tmp_path: Path) -> None:
    from lindos_transfer.copyengine import CopyEngine

    folder = tmp_path / "Fonts"
    folder.mkdir()
    (folder / "MyHand.ttf").write_bytes(_sfnt_font(b"GOOG"))
    (folder / "Arial.ttf").write_bytes(_sfnt_font(b"MS  "))
    (folder / "notes.txt").write_bytes(b"not a font")
    ctx = types.SimpleNamespace(engine=CopyEngine(use_default_xattr=False))
    files, skipped = font_files(ctx, folder)
    names = {p.name for p, _st in files}
    assert names == {"MyHand.ttf"}
    assert any(s["reason"] == REASON_MICROSOFT for s in skipped)


def test_plan_and_run_item_copy_only_user_fonts(tmp_path: Path, home: Path) -> None:
    from lindos_transfer.copyengine import CopyEngine

    profile = tmp_path / "profile"
    fonts_dir = profile.joinpath(*USER_FONTS_REL)
    fonts_dir.mkdir(parents=True)
    (fonts_dir / "MyHand.ttf").write_bytes(_sfnt_font(b"GOOG"))
    (fonts_dir / "Arial.ttf").write_bytes(_sfnt_font(b"MS  "))

    engine = CopyEngine(use_default_xattr=False)
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                               user=types.SimpleNamespace(profile_dir=profile),
                               home=home, engine=engine, dry_run=False,
                               which=lambda name: None, run=lambda *a, **k: types.SimpleNamespace(returncode=0))
    items, skipped, warnings = plan_items(ctx)
    assert len(items) == 1 and items[0]["files"] == 1
    res = run_item(items[0], ctx)
    dest = home.joinpath(*DEST_REL)
    assert (dest / "MyHand.ttf").is_file()
    assert not (dest / "Arial.ttf").exists()
    assert res.files == 1


def test_plan_items_empty_when_no_font_folder(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                               user=types.SimpleNamespace(profile_dir=profile))
    items, skipped, warnings = plan_items(ctx)
    assert items == [] and skipped == [] and warnings == []
