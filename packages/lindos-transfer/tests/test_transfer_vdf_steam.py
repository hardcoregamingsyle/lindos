"""Tests for the Valve KeyValues reader/writer and Steam game discovery (SPEC-WINDOWS §29.8)."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from lindos_transfer import vdf
from lindos_transfer.steam import (FORCE_PROTON_NOTE, STATE_FULLY_INSTALLED, STATE_VALIDATE, Game,
                                   installed_games, library_dirs, linux_steam_root, parse_libraryfolders,
                                   plan_items, read_manifest, rewrite_manifest, run_item, safe_installdir)


# --------------------------------------------------------------------------- #
# vdf.py
# --------------------------------------------------------------------------- #
def test_vdf_loads_nested_blocks_and_escapes() -> None:
    text = ('"AppState"\n{\n  "appid" "620"\n  "name" "Portal \\"2\\""\n  "UserConfig"\n  {\n'
           '    "language" "english"\n  }\n  // a comment\n}\n')
    data = vdf.loads(text)
    assert data["AppState"]["appid"] == "620"
    assert data["AppState"]["name"] == 'Portal "2"'
    assert data["AppState"]["UserConfig"]["language"] == "english"


def test_vdf_get_is_case_insensitive() -> None:
    data = {"AppState": {"appid": "1"}}
    assert vdf.get(data, "APPSTATE")["appid"] == "1"
    assert vdf.get(data, "missing", "default") == "default"


def test_vdf_bare_tokens_and_platform_conditionals_ignored() -> None:
    text = '"key" value [$WIN32]\n'
    assert vdf.loads(text) == {"key": "value"}


def test_vdf_raises_on_malformed_input() -> None:
    with pytest.raises(vdf.VdfError):
        vdf.loads('"unterminated')
    with pytest.raises(vdf.VdfError):
        vdf.loads('"key" {')  # missing closing brace
    with pytest.raises(vdf.VdfError):
        vdf.loads('}')  # stray close


def test_vdf_dumps_round_trips() -> None:
    data = {"AppState": {"appid": "620", "nested": {"x": "1"}}}
    text = vdf.dumps(data)
    assert vdf.loads(text) == data


def test_vdf_load_reads_through_secrets_gate(tmp_path: Path) -> None:
    f = tmp_path / "appmanifest_620.acf"
    f.write_text('"AppState"\n{\n  "appid" "620"\n}\n')
    data = vdf.load(f)
    assert vdf.get(data, "AppState")["appid"] == "620"


# --------------------------------------------------------------------------- #
# steam.py: libraryfolders.vdf (new + legacy layout)
# --------------------------------------------------------------------------- #
def test_parse_libraryfolders_new_style() -> None:
    data = vdf.loads('"libraryfolders"\n{\n "0"\n {\n  "path" "C:\\\\Program Files (x86)\\\\Steam"\n'
                     '  "apps" { "620" "1" }\n }\n "1"\n {\n  "path" "D:\\\\SteamLibrary"\n }\n}\n')
    paths = parse_libraryfolders(data)
    assert paths == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


def test_parse_libraryfolders_legacy_style() -> None:
    data = vdf.loads('"LibraryFolders"\n{\n "TimeNextStatsReport" "123"\n "1" "E:\\\\Games\\\\Steam"\n}\n')
    assert parse_libraryfolders(data) == ["E:\\Games\\Steam"]


def test_safe_installdir_refuses_traversal_and_separators() -> None:
    assert safe_installdir("Portal 2") == "Portal 2"
    assert safe_installdir("..") is None
    assert safe_installdir("a/b") is None
    assert safe_installdir("a\\b") is None
    assert safe_installdir("a:b") is None
    assert safe_installdir(None) is None
    assert safe_installdir("x" * 300) is None


def test_read_manifest_builds_a_game_and_finds_its_folder(tmp_path: Path) -> None:
    lib = tmp_path / "SteamLibrary"
    (lib / "steamapps" / "common" / "Portal 2").mkdir(parents=True)
    manifest = lib / "steamapps" / "appmanifest_620.acf"
    manifest.write_text('"AppState"\n{\n "appid" "620"\n "name" "Portal 2"\n "installdir" "Portal 2"\n'
                        ' "StateFlags" "4"\n "SizeOnDisk" "12772699651"\n}\n')
    game = read_manifest(manifest, lib)
    assert game.appid == "620" and game.name == "Portal 2"
    assert game.state_flags & STATE_FULLY_INSTALLED
    assert game.game_dir == lib / "steamapps" / "common" / "Portal 2"


def test_read_manifest_rejects_bad_installdir_or_appid(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    manifest = lib / "appmanifest_bad.acf"
    manifest.write_text('"AppState"\n{\n "appid" "notanumber"\n "installdir" "X"\n}\n')
    assert read_manifest(manifest, lib) is None
    manifest2 = lib / "appmanifest_bad2.acf"
    manifest2.write_text('"AppState"\n{\n "appid" "1"\n "installdir" ".."\n}\n')
    assert read_manifest(manifest2, lib) is None


def test_linux_steam_root_prefers_native_then_flatpak(tmp_path: Path) -> None:
    assert linux_steam_root(tmp_path) == tmp_path / ".local" / "share" / "Steam"
    (tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam").mkdir(parents=True)
    assert linux_steam_root(tmp_path) == tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    (tmp_path / ".local" / "share" / "Steam").mkdir(parents=True)
    assert linux_steam_root(tmp_path) == tmp_path / ".local" / "share" / "Steam"


def test_rewrite_manifest_drops_launcherpath_and_forces_revalidate() -> None:
    data = {"AppState": {"appid": "620", "LauncherPath": "C:\\Steam\\steam.exe", "StateFlags": "4"}}
    out = rewrite_manifest(data)
    assert "LauncherPath" not in out["AppState"]
    assert out["AppState"]["StateFlags"] == STATE_VALIDATE


# --------------------------------------------------------------------------- #
# installed_games() / library_dirs() from a bundle
# --------------------------------------------------------------------------- #
def _bundle_steam(tmp_path: Path, *, with_game_dir: bool = True):
    steam_dir = tmp_path / "steam"
    lib_common = steam_dir / "steamapps" / "common" / "Portal 2"
    if with_game_dir:
        lib_common.mkdir(parents=True)
        (lib_common / "portal2.exe").write_bytes(b"x")
    else:
        (steam_dir / "steamapps").mkdir(parents=True)
    (steam_dir / "steamapps" / "appmanifest_620.acf").write_text(
        '"AppState"\n{\n "appid" "620"\n "name" "Portal 2"\n "installdir" "Portal 2"\n'
        ' "StateFlags" "4"\n "SizeOnDisk" "1000"\n}\n')

    class BundleSource:
        is_bundle = True
        manifest = {"steam": {"dir": "steam"}}

        def path(self, rel):
            return tmp_path / str(rel)

    return BundleSource(), steam_dir


def test_library_dirs_and_installed_games_from_bundle(tmp_path: Path) -> None:
    source, steam_dir = _bundle_steam(tmp_path)
    ctx = types.SimpleNamespace(source=source)
    libs = library_dirs(ctx)
    assert libs == [steam_dir]
    games = installed_games(ctx)
    assert len(games) == 1 and games[0].name == "Portal 2"
    assert games[0].game_dir == steam_dir / "steamapps" / "common" / "Portal 2"


def test_plan_items_skips_game_without_copied_files(tmp_path: Path, home) -> None:
    source, steam_dir = _bundle_steam(tmp_path, with_game_dir=False)
    ctx = types.SimpleNamespace(source=source, home=home, which=lambda n: None)
    items, skipped, warnings = plan_items(ctx)
    assert items == []
    assert skipped and "Steam games" in skipped[0]["reason"]


def test_plan_items_includes_note_to_force_proton(tmp_path: Path, home) -> None:
    source, steam_dir = _bundle_steam(tmp_path)
    ctx = types.SimpleNamespace(source=source, home=home, which=lambda n: None)

    # patch out the apps.blocked_game lookup so this test does not depend on a real compat-matrix.json
    import lindos_transfer.apps as apps_mod

    monkey_orig = apps_mod.blocked_game
    apps_mod.blocked_game = lambda ctx, *, appid="", name="": None
    try:
        items, skipped, warnings = plan_items(ctx)
    finally:
        apps_mod.blocked_game = monkey_orig
    assert len(items) == 1
    assert FORCE_PROTON_NOTE in items[0]["notes"]


def test_plan_items_lists_anticheat_blocked_game_unselected(tmp_path: Path, home) -> None:
    source, steam_dir = _bundle_steam(tmp_path)
    ctx = types.SimpleNamespace(source=source, home=home, which=lambda n: None)
    import lindos_transfer.apps as apps_mod

    monkey_orig = apps_mod.blocked_game
    apps_mod.blocked_game = lambda ctx, *, appid="", name="": (
        {"route": "Portal 2", "reason": "test-anticheat", "anticheat": "TestAC"} if name == "Portal 2" else None)
    try:
        items, skipped, warnings = plan_items(ctx)
    finally:
        apps_mod.blocked_game = monkey_orig
    assert len(items) == 1
    assert items[0]["selected"] is False
    assert "lindos-game route" in items[0]["notes"][0]
    note = items[0]["notes"][0].lower()
    assert "not supported on lindos yet" in note and "publisher" in note and "no date" in note
    assert "blocks linux" not in note


def test_run_item_skips_an_anticheat_blocked_game_with_the_honest_wording(tmp_path: Path, home) -> None:
    source, steam_dir = _bundle_steam(tmp_path)
    ctx = types.SimpleNamespace(source=source, home=home, which=lambda n: None)
    import lindos_transfer.apps as apps_mod

    monkey_orig = apps_mod.blocked_game
    apps_mod.blocked_game = lambda ctx, *, appid="", name="": {"route": "Portal 2", "reason": "x", "anticheat": "TestAC"}
    try:
        items, _skipped, _warnings = plan_items(ctx)
        res = run_item(items[0], ctx)
    finally:
        apps_mod.blocked_game = monkey_orig
    assert res.status == "skipped"
    text = res.notes[0].lower()
    assert "not supported on lindos yet" in text and "publisher" in text and "lindos-game route" in text
    assert "blocks linux" not in text


def test_run_item_copies_game_and_rewrites_manifest(tmp_path: Path, home) -> None:
    from lindos_transfer.copyengine import CopyEngine

    source, steam_dir = _bundle_steam(tmp_path)
    engine = CopyEngine(use_default_xattr=False)
    ctx = types.SimpleNamespace(source=source, home=home, which=lambda n: None, engine=engine, dry_run=False)
    import lindos_transfer.apps as apps_mod

    monkey_orig = apps_mod.blocked_game
    apps_mod.blocked_game = lambda ctx, *, appid="", name="": None
    try:
        items, _skipped, _warnings = plan_items(ctx)
        res = run_item(items[0], ctx)
    finally:
        apps_mod.blocked_game = monkey_orig
    dest_dir = linux_steam_root(home) / "steamapps" / "common" / "Portal 2"
    assert (dest_dir / "portal2.exe").is_file()
    manifest_dest = linux_steam_root(home) / "steamapps" / "appmanifest_620.acf"
    assert manifest_dest.is_file()
    written = vdf.load(manifest_dest)
    assert vdf.get(written, "AppState")["StateFlags"] == STATE_VALIDATE
    assert res.files >= 1
