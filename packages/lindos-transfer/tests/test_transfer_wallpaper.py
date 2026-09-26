"""Tests for the desktop wallpaper transfer item (SPEC-WINDOWS §29.8)."""
from __future__ import annotations

import types
from pathlib import Path

from lindos_transfer.wallpaper import (POLICY_STYLES, TRANSCODED_REL, XFCE_STYLES, apply_wallpaper,
                                       find_wallpaper, is_microsoft_picture, plan_items, run_item,
                                       sniff_extension, windows_style)


def test_sniff_extension_recognises_common_formats() -> None:
    assert sniff_extension(b"\xff\xd8\xff\xe0") == "jpg"
    assert sniff_extension(b"\x89PNG\r\n\x1a\n") == "png"
    assert sniff_extension(b"BM\x00\x00") == "bmp"
    assert sniff_extension(b"GIF89a") == "gif"
    assert sniff_extension(b"RIFF????WEBP") == "webp"
    assert sniff_extension(b"\x00\x00\x00\x00") is None


def test_is_microsoft_picture_detects_windows_web_and_spotlight() -> None:
    assert is_microsoft_picture("C:\\Windows\\Web\\Wallpaper\\Windows\\img0.jpg") is True
    assert is_microsoft_picture(
        "C:\\Users\\a\\AppData\\Local\\Packages\\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\\x.jpg") is True
    assert is_microsoft_picture("C:\\Users\\a\\Pictures\\me.jpg") is False
    assert is_microsoft_picture("") is False


def test_windows_style_control_panel_mapping() -> None:
    assert windows_style({"style": "10", "tile": "0"}) == "fill"
    assert windows_style({"style": "0", "tile": "1"}) == "tile"
    assert windows_style({"style": "0", "tile": "0"}) == "center"
    assert windows_style({"style": "2"}) == "stretch"
    assert windows_style({"style": "6"}) == "fit"
    assert windows_style({"style": "22"}) == "span"
    assert windows_style({"style": "999"}) == "fill"  # unknown -> safe default


def test_windows_style_policy_key_overrides_and_uses_its_own_enum() -> None:
    # policy enum: 0 Center 1 Tile 2 Stretch 3 Fit 4 Fill 5 Span (different from Control Panel's!)
    settings = {"style": "10", "policy_wallpaper": "C:\\policy.jpg", "policy_style": "3"}
    assert windows_style(settings) == "fit"
    # a policy key present but no policy_wallpaper must not apply (nothing to enforce)
    settings2 = {"style": "10", "policy_style": "3", "policy_wallpaper": ""}
    assert windows_style(settings2) == "fill"


def test_xfce_and_policy_style_tables_match_spec() -> None:
    assert XFCE_STYLES == {"center": 1, "tile": 2, "stretch": 3, "fit": 4, "fill": 5, "span": 6}
    assert POLICY_STYLES == {0: "center", 1: "tile", 2: "stretch", 3: "fit", 4: "fill", 5: "span"}


def test_find_wallpaper_skips_microsoft_stock_pictures(tmp_path: Path) -> None:
    class Ntuser:
        pass

    profile = tmp_path / "profile"
    profile.mkdir()
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False), user=types.SimpleNamespace(
        profile_dir=profile), ntuser=object())
    import lindos_transfer.wallpaper as wp

    orig = wp.winreg.wallpaper_settings
    wp.winreg.wallpaper_settings = lambda ntuser: {"wallpaper": "C:\\Windows\\Web\\img.jpg", "style": "10",
                                                   "tile": "0", "policy_wallpaper": None, "policy_style": None}
    try:
        pic, style, notes = find_wallpaper(ctx)
    finally:
        wp.winreg.wallpaper_settings = orig
    assert pic is None
    assert any("Microsoft" in n for n in notes)


def test_find_wallpaper_bundle_source_uses_manifest() -> None:
    class BundleSource:
        is_bundle = True
        manifest = {"wallpaper": "wallpaper.jpg", "wallpaper_style": "fit"}

        def path(self, rel):
            return Path("/bundle") / rel

    ctx = types.SimpleNamespace(source=BundleSource())
    pic, style, notes = find_wallpaper(ctx)
    assert pic == Path("/bundle/wallpaper.jpg") and style == "fit"


def test_plan_and_run_item_copy_and_sniff(tmp_path: Path, home: Path) -> None:
    from lindos_transfer.copyengine import CopyEngine

    profile = tmp_path / "profile"
    wall = profile.joinpath(*TRANSCODED_REL)
    wall.parent.mkdir(parents=True)
    wall.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)

    import lindos_transfer.wallpaper as wp

    orig = wp.winreg.wallpaper_settings
    wp.winreg.wallpaper_settings = lambda ntuser: {"wallpaper": "C:\\Users\\a\\wall.jpg", "style": "10",
                                                   "tile": "0", "policy_wallpaper": None, "policy_style": None}
    try:
        engine = CopyEngine(use_default_xattr=False)
        ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                                   user=types.SimpleNamespace(profile_dir=profile), ntuser=object(),
                                   dirs={"pictures": home / "Pictures"}, engine=engine, dry_run=False,
                                   home=home, theme=lambda: None)
        items, skipped, warnings = plan_items(ctx)
        assert len(items) == 1 and items[0]["files"] == 1
        res = run_item(items[0], ctx)
    finally:
        wp.winreg.wallpaper_settings = orig
    dest = home / "Pictures" / "Wallpapers" / "windows-wallpaper.jpg"
    assert dest.is_file()
    assert res.files == 1


def test_apply_wallpaper_calls_theme_setter_with_style() -> None:
    calls = []

    class FakeTheme:
        def set_wallpaper(self, path, style=None):
            calls.append((path, style))
            return True

    ok = apply_wallpaper(FakeTheme(), "/x.jpg", "fill")
    assert ok is True
    assert calls == [("/x.jpg", XFCE_STYLES["fill"])]


def test_apply_wallpaper_falls_back_when_setter_has_no_style_param() -> None:
    class FakeTheme:
        def set_wallpaper(self, path):
            return True

    ok = apply_wallpaper(FakeTheme(), "/x.jpg", "fit")
    assert ok is True


def test_apply_wallpaper_returns_false_without_setter() -> None:
    class FakeTheme:
        pass

    assert apply_wallpaper(FakeTheme(), "/x.jpg", "fill") is False
