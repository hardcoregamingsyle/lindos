"""lindos.compat: PE analysis on synthetic files, slugify, apps DB, runner choice (SPEC §4.8/§9)."""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from lindos import compat, paths

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
SUBSYSTEM_GUI = 2
SUBSYSTEM_CUI = 3


def make_pe(*, machine: int = IMAGE_FILE_MACHINE_AMD64, subsystem: int = SUBSYSTEM_GUI, dll: bool = False,
            payload: bytes = b"", product: str = "", company: str = "") -> bytes:
    """Build a minimal, structurally valid PE image: MZ header → e_lfanew → PE\\0\\0 → COFF → optional header."""
    e_lfanew = 0x80
    buf = bytearray(b"MZ" + b"\x00" * (0x3C - 2))
    buf += struct.pack("<I", e_lfanew)                          # e_lfanew at 0x3C
    buf += b"\x00" * (e_lfanew - len(buf))
    characteristics = 0x0022 | (0x2000 if dll else 0)           # EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE (| DLL)
    pe32plus = machine == IMAGE_FILE_MACHINE_AMD64
    opt_size = 240 if pe32plus else 224
    buf += b"PE\x00\x00"
    buf += struct.pack("<HHIIIHH", machine, 3, 0, 0, 0, opt_size, characteristics)   # COFF file header
    opt = bytearray(struct.pack("<H", 0x20B if pe32plus else 0x10B))
    opt += b"\x00" * (68 - len(opt))
    opt += struct.pack("<H", subsystem)                         # Subsystem at optional-header offset 68
    opt += b"\x00" * (opt_size - len(opt))
    buf += opt
    if product or company:
        # a fake VS_VERSION_INFO string table: key\0 (aligned) value\0
        def entry(key: str, value: str) -> bytes:
            raw = key.encode("utf-16-le") + b"\x00\x00"
            raw += b"\x00" * ((-len(raw)) % 4)
            return raw + value.encode("utf-16-le") + b"\x00\x00"
        buf += b"\x00" * 16 + entry("ProductName", product) + b"\x00" * 8 + entry("CompanyName", company)
    buf += b"\x00" * 64 + payload + b"\x00" * 64
    return bytes(buf)


@pytest.fixture()
def exe_dir(tmp_path: Path) -> Path:
    d = tmp_path / "exes"
    d.mkdir()
    return d


def test_nsis_installer_x64(exe_dir: Path) -> None:
    p = exe_dir / "coolapp-setup.exe"
    p.write_bytes(make_pe(payload=b"...Nullsoft.NSIS...", product="CoolApp 2.1", company="Cool Corp"))
    info = compat.analyze_exe(str(p))
    assert isinstance(info, compat.ExeInfo)
    assert info.kind == "installer"
    assert info.installer_type == "nsis"
    assert info.arch == "x64"
    assert info.is_pe and not info.is_dll
    assert info.subsystem == "gui"
    assert info.product == "CoolApp 2.1"
    assert info.company == "Cool Corp"
    assert len(info.sha256_prefix) == 16 and all(c in "0123456789abcdef" for c in info.sha256_prefix)
    assert info.name == "coolapp-setup.exe" and info.path == str(p.resolve()) or info.path.endswith("coolapp-setup.exe")
    assert "Nullsoft.NSIS" in info.markers
    assert info.slug == "coolapp"          # product name, version stripped
    d = info.to_dict()
    assert d["kind"] == "installer" and json.dumps(d)


def test_inno_installer_x86(exe_dir: Path) -> None:
    p = exe_dir / "npp.8.6.Installer.exe"
    p.write_bytes(make_pe(machine=IMAGE_FILE_MACHINE_I386, payload=b"Inno Setup Setup Data (6.2.0)"))
    info = compat.analyze_exe(str(p))
    assert (info.kind, info.installer_type, info.arch) == ("installer", "inno", "x86")


def test_unity_game_detected(exe_dir: Path) -> None:
    p = exe_dir / "SuperGame.exe"
    p.write_bytes(make_pe(payload=b"\x00UnityPlayer.dll\x00steam_api64.dll\x00", product="Super Game"))
    info = compat.analyze_exe(str(p))
    assert info.kind == "game"
    assert info.installer_type is None
    assert compat.app_slug(info) == "super-game"


def test_plain_gui_and_console_apps(exe_dir: Path) -> None:
    gui = exe_dir / "editor.exe"
    gui.write_bytes(make_pe(subsystem=SUBSYSTEM_GUI))
    cui = exe_dir / "tool.exe"
    cui.write_bytes(make_pe(subsystem=SUBSYSTEM_CUI, machine=IMAGE_FILE_MACHINE_I386))
    assert compat.analyze_exe(str(gui)).kind == "app"
    ci = compat.analyze_exe(str(cui))
    assert ci.kind == "app" and ci.subsystem == "console" and ci.arch == "x86"


def test_uninstaller_is_app_not_installer(exe_dir: Path) -> None:
    p = exe_dir / "unins000.exe"
    p.write_bytes(make_pe())
    assert compat.analyze_exe(str(p)).kind == "app"


def test_setup_by_name_without_markers(exe_dir: Path) -> None:
    p = exe_dir / "Setup.exe"
    p.write_bytes(make_pe())
    info = compat.analyze_exe(str(p))
    assert info.kind == "installer" and info.installer_type is None


def test_dll_is_unknown(exe_dir: Path) -> None:
    p = exe_dir / "helper.dll"
    p.write_bytes(make_pe(dll=True))
    info = compat.analyze_exe(str(p))
    assert info.is_dll and info.kind == "unknown"


def test_msi_by_magic_and_extension(exe_dir: Path) -> None:
    magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    p = exe_dir / "office.msi"
    p.write_bytes(magic + b"\x00" * 512 + b"Intel64;1033" + b"\x00" * 64)
    info = compat.analyze_exe(str(p))
    assert (info.kind, info.installer_type, info.arch) == ("msi", "msi", "x64")
    q = exe_dir / "thing.msi"
    q.write_bytes(b"not really ole but the extension says msi")
    assert compat.analyze_exe(str(q)).kind == "msi"


def test_non_pe_file_is_unknown(exe_dir: Path) -> None:
    p = exe_dir / "readme.exe"
    p.write_bytes(b"this is not a program at all")
    info = compat.analyze_exe(str(p))
    assert info.kind == "unknown" and info.arch == "unknown" and not info.is_pe


def test_missing_file_raises(exe_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        compat.analyze_exe(str(exe_dir / "nope.exe"))


def test_describe_and_digest(exe_dir: Path) -> None:
    p = exe_dir / "a.exe"
    p.write_bytes(make_pe(product="Alpha"))
    info = compat.analyze_exe(str(p))
    text = info.describe()
    assert "Kind:" in text and "Alpha" in text
    assert compat.file_digest(str(p))[:16] == info.sha256_prefix
    assert compat.file_digest(str(exe_dir / "missing")) == ""


@pytest.mark.parametrize("raw, expected", [
    ("Notepad++ 8.6 Setup", "notepad-plus-plus-8-6-setup"),
    ("  Adobe Photoshop CC 2021 ", "adobe-photoshop-cc-2021"),
    ("Ünïcödé & Co @ home", "unicode-and-co-at-home"),
    ("", "app"),
    ("---", "app"),
    ("x" * 100, "x" * 48),
])
def test_slugify(raw: str, expected: str) -> None:
    assert compat.slugify(raw) == expected


def test_app_slug_strips_noise() -> None:
    info = compat.ExeInfo(path="/x/foo-setup-x64-1.2.3.exe", name="foo-setup-x64-1.2.3.exe")
    assert compat.app_slug(info) == "foo"
    info2 = compat.ExeInfo(path="/x/setup.exe", name="setup.exe", product="Notepad++")
    assert compat.app_slug(info2) == "notepad-plus-plus"


def test_apps_db_roundtrip(core_env) -> None:
    assert compat.apps_db_load() == {}
    entry = {"name": "Foo", "exe": "C:/Program Files/Foo/foo.exe", "prefix": "foo", "runner": "wine",
             "installed_at": "2026-01-01T00:00:00", "kind": "app"}
    compat.apps_db_save({"foo": entry})
    db_path = Path(paths.apps_db())
    assert db_path.is_file() and str(core_env["home"]) in str(db_path)
    assert compat.apps_db_load() == {"foo": entry}
    compat.apps_db_upsert("foo", {"runner": "umu"})
    compat.apps_db_upsert("bar", {"name": "Bar"})
    db = compat.apps_db_load()
    assert db["foo"]["runner"] == "umu" and db["foo"]["name"] == "Foo" and "bar" in db
    assert compat.apps_db_remove("bar") is True
    assert compat.apps_db_remove("bar") is False
    assert "bar" not in compat.apps_db_load()
    with pytest.raises(TypeError):
        compat.apps_db_save(["not", "a", "dict"])  # type: ignore[arg-type]


def test_choose_runner_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    game = compat.ExeInfo(path="g.exe", name="g.exe", kind="game")
    unknown = compat.ExeInfo(path="u.exe", name="u.exe", kind="unknown")
    installer = compat.ExeInfo(path="s.exe", name="s.exe", kind="installer")
    app = compat.ExeInfo(path="a.exe", name="a.exe", kind="app")
    adobe = compat.ExeInfo(path="ps.exe", name="ps.exe", kind="installer", company="Adobe Systems")

    monkeypatch.setattr(compat, "umu_available", lambda: True)
    monkeypatch.setattr(compat, "wine_available", lambda: True)
    monkeypatch.setattr(compat, "bottles_installed", lambda: False)
    assert compat.choose_runner(game, {}) == "umu"
    assert compat.choose_runner(unknown, None) == "umu"
    assert compat.choose_runner(installer, {}) == "wine"
    assert compat.choose_runner(app, {}) == "wine"
    assert compat.choose_runner(adobe, {}) == "wine"          # bottles missing → wine

    monkeypatch.setattr(compat, "bottles_installed", lambda: True)
    assert compat.choose_runner(adobe, {}) == "bottles"
    assert compat.choose_runner(app, {"runner": "bottles"}) == "bottles"

    monkeypatch.setattr(compat, "umu_available", lambda: False)
    assert compat.choose_runner(game, {}) == "wine"
    assert compat.choose_runner(game, {"runner": "umu"}) == "wine"   # forced but unavailable → rules

    monkeypatch.setattr(compat, "umu_available", lambda: True)
    monkeypatch.setattr(compat, "wine_available", lambda: False)
    assert compat.choose_runner(installer, {}) == "umu"       # no wine at all → umu


def test_module_constants() -> None:
    assert set(compat.KINDS) == {"installer", "app", "game", "msi", "unknown"}
    assert set(compat.ARCHES) == {"x86", "x64", "unknown"}
    assert set(compat.INSTALLER_TYPES) == {"nsis", "inno", "installshield", "msi", "wix", "squirrel"}
    assert compat.SCAN_BYTES == 4 * 1024 * 1024
    assert os.path.basename(paths.APPS_DB) == "apps.json"
