"""Tests for the minimal Windows Shell Link (.lnk) parser (lindos_compat.lnk)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

from lindos_compat import lnk

# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_link_info_local_base_path(build_lnk):
    data = build_lnk(local_base_path="C:\\Program Files\\Foo\\foo.exe", working_dir="C:\\Program Files\\Foo",
                     arguments="--flag \"a b\"", icon_location="C:\\Program Files\\Foo\\foo.exe", icon_index=3)
    info = lnk.parse_lnk_bytes(data)
    assert info.link_flags & lnk.HAS_LINK_INFO
    assert info.local_base_path == "C:\\Program Files\\Foo\\foo.exe"
    assert info.target == "C:\\Program Files\\Foo\\foo.exe"
    assert info.working_dir == "C:\\Program Files\\Foo"
    assert info.arguments == "--flag \"a b\""
    assert info.icon_location == "C:\\Program Files\\Foo\\foo.exe"
    assert info.icon_index == 3
    assert info.target_is_exe
    assert not info.is_directory
    assert not info.is_unicode


def test_parse_link_info_with_common_suffix_and_id_list(build_lnk):
    data = build_lnk(local_base_path="C:\\Program Files\\Foo", common_suffix="bin\\foo.exe", with_id_list=True)
    info = lnk.parse_lnk_bytes(data)
    assert info.common_path_suffix == "bin\\foo.exe"
    assert info.target == "C:\\Program Files\\Foo\\bin\\foo.exe"


def test_parse_unicode_string_data_and_unicode_link_info(build_lnk):
    data = build_lnk(local_base_path="C:\\Programme\\Fóo\\fóo.exe", unicode=True, unicode_link_info=True,
                     description="Fóo Editor", arguments="/x", working_dir="C:\\Programme\\Fóo")
    info = lnk.parse_lnk_bytes(data)
    assert info.is_unicode
    assert info.description == "Fóo Editor"
    assert info.local_base_path == "C:\\Programme\\Fóo\\fóo.exe"
    assert info.target == "C:\\Programme\\Fóo\\fóo.exe"
    assert info.working_dir == "C:\\Programme\\Fóo"


def test_relative_path_only(build_lnk):
    data = build_lnk(relative_path="..\\bin\\tool.exe")
    info = lnk.parse_lnk_bytes(data)
    assert info.relative_path == "..\\bin\\tool.exe"
    assert info.target == "..\\bin\\tool.exe"
    assert not lnk.is_windows_path(info.target)


def test_environment_variable_data_block_expands(build_lnk):
    data = build_lnk(env_target="%ProgramFiles%\\Bar\\bar.exe")
    info = lnk.parse_lnk_bytes(data)
    assert info.env_target == "%ProgramFiles%\\Bar\\bar.exe"
    assert info.target == "C:\\Program Files\\Bar\\bar.exe"


def test_directory_shortcut_flag(build_lnk):
    data = build_lnk(local_base_path="C:\\Program Files\\Foo", file_attributes=0x10)
    info = lnk.parse_lnk_bytes(data)
    assert info.is_directory
    assert not info.target_is_exe


@pytest.mark.parametrize("payload", [b"", b"MZ" * 40, b"L" + b"\x00" * 100, bytes(0x4C)])
def test_rejects_garbage(payload: bytes):
    with pytest.raises(lnk.LnkError):
        lnk.parse_lnk_bytes(payload)


def test_truncated_string_data_is_not_fatal(build_lnk):
    data = build_lnk(local_base_path="C:\\Program Files\\Foo\\foo.exe", arguments="/verbose")
    info = lnk.parse_lnk_bytes(data[:-8])  # cut into the StringData / terminal block
    assert info.target == "C:\\Program Files\\Foo\\foo.exe"


# --------------------------------------------------------------------------- #
# path mapping
# --------------------------------------------------------------------------- #
def test_windows_to_unix_case_insensitive(tmp_path: Path):
    prefix = tmp_path / "pfx"
    exe = prefix / "drive_c" / "Program Files" / "Foo" / "foo.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert lnk.windows_to_unix("C:\\PROGRAM FILES\\foo\\FOO.EXE", prefix, must_exist=True) == exe
    assert lnk.windows_to_unix("c:/program files/foo/foo.exe", prefix, must_exist=True) == exe
    assert lnk.windows_to_unix("C:\\Program Files\\Nope\\x.exe", prefix, must_exist=True) is None
    # without must_exist the missing tail is kept verbatim
    guess = lnk.windows_to_unix("C:\\Program Files\\Nope\\x.exe", prefix)
    assert guess is not None and guess.name == "x.exe" and guess.parent.name == "Nope"
    assert lnk.windows_to_unix("\\\\server\\share\\x.exe", prefix) is None
    assert lnk.windows_to_unix("", prefix) is None


@pytest.mark.skipif(sys.platform.startswith("win"), reason="'d:' is a drive, not a directory name, on Windows")
def test_windows_to_unix_other_drive(tmp_path: Path):
    prefix = tmp_path / "pfx"
    (prefix / "dosdevices" / "d:" / "Games").mkdir(parents=True)
    got = lnk.windows_to_unix("D:\\Games\\game.exe", prefix)
    assert got == prefix / "dosdevices" / "d:" / "Games" / "game.exe"


def test_windows_to_unix_env(tmp_path: Path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "users" / "user" / "AppData" / "Roaming" / "App").mkdir(parents=True)
    got = lnk.windows_to_unix("%APPDATA%\\App\\app.exe", prefix)
    assert got == prefix / "drive_c" / "users" / "user" / "AppData" / "Roaming" / "App" / "app.exe"


def test_unix_to_windows_roundtrip(tmp_path: Path):
    prefix = tmp_path / "pfx"
    exe = prefix / "drive_c" / "Program Files" / "Foo" / "foo.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert lnk.unix_to_windows(exe, prefix) == "C:\\Program Files\\Foo\\foo.exe"
    assert lnk.unix_to_windows(tmp_path / "elsewhere.exe", prefix) is None


def test_resolve_lnk_target_from_file(build_lnk, tmp_path: Path):
    prefix = tmp_path / "pfx"
    exe = prefix / "drive_c" / "Program Files" / "Foo" / "foo.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    start_menu = prefix / "drive_c" / "users" / "user" / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    start_menu.mkdir(parents=True)
    shortcut = start_menu / "Foo.lnk"
    shortcut.write_bytes(build_lnk(local_base_path="C:\\Program Files\\Foo\\foo.exe"))
    assert lnk.resolve_lnk_target(shortcut, prefix) == exe
    # relative shortcut next to the target
    rel = exe.parent / "Foo (relative).lnk"
    rel.write_bytes(build_lnk(relative_path=".\\foo.exe"))
    assert lnk.resolve_lnk_target(rel, prefix) == exe
    info = lnk.parse_lnk(rel)
    assert info.path == str(rel)
    assert info.extra.get("relative_to") == str(rel.parent)
    with pytest.raises(lnk.LnkError):
        lnk.parse_lnk(tmp_path / "missing.lnk")


def test_expand_windows_env_and_is_windows_path():
    assert lnk.expand_windows_env("%SystemRoot%\\notepad.exe") == "C:\\windows\\notepad.exe"
    assert lnk.expand_windows_env("%UNKNOWN%\\x") == "%UNKNOWN%\\x"
    assert lnk.expand_windows_env("%USERPROFILE%\\a", user="bob") == "C:\\users\\bob\\a"
    assert lnk.is_windows_path("C:\\x")
    assert lnk.is_windows_path("d:/x")
    assert not lnk.is_windows_path("/usr/bin/x")
    assert not lnk.is_windows_path("")
