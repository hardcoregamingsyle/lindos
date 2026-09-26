"""Tests for Windows users and known-folder resolution (SPEC-WINDOWS §29.4)."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from lindos_transfer import TransferError
from lindos_transfer.profiles import (decode_mounted_device, expand_vars, find_user, list_users,
                                      onedrive_roots, open_ntuser, resolve_folders, windows_path_to_local)
from lindos_transfer.sources import open_source


# --------------------------------------------------------------------------- #
# expand_vars / windows_path_to_local
# --------------------------------------------------------------------------- #
def test_expand_vars_is_case_insensitive_and_recursive() -> None:
    env = {"USERPROFILE": "C:\\Users\\alice", "OneDrive": "%USERPROFILE%\\OneDrive"}
    assert expand_vars("%userprofile%\\Documents", env) == "C:\\Users\\alice\\Documents"
    assert expand_vars("%OneDrive%\\Pictures", env) == "C:\\Users\\alice\\OneDrive\\Pictures"
    assert expand_vars("no vars here", env) == "no vars here"
    assert expand_vars("%Unknown%\\x", env) == "%Unknown%\\x"


def test_windows_path_to_local_maps_drive_letter(tmp_path: Path) -> None:
    docs = tmp_path / "Documents"
    docs.mkdir()
    drives = {"C:": tmp_path}
    local, why = windows_path_to_local("C:\\Documents", drives)
    assert local == docs and why == ""


def test_windows_path_to_local_rejects_unc_and_unknown_drive(tmp_path: Path) -> None:
    local, why = windows_path_to_local("\\\\server\\share\\x", {"C:": tmp_path})
    assert local is None and "network" in why
    local, why = windows_path_to_local("D:\\Data", {"C:": tmp_path})
    assert local is None and "Lindos cannot see" in why


def test_windows_path_to_local_hints_at_unmounted_drive(tmp_path: Path) -> None:
    drives = {"C:": tmp_path, "D:unmounted": Path("/dev/sdb1")}
    local, why = windows_path_to_local("D:\\Data", drives)
    assert local is None and "lindos-transfer mount" in why and "sdb1" in why


def test_windows_path_to_local_missing_path_reports_reason(tmp_path: Path) -> None:
    local, why = windows_path_to_local("C:\\NoSuchFolder", {"C:": tmp_path})
    assert local is None and "not found" in why


def test_windows_path_to_local_refuses_a_link_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "OneDrive"
    try:
        import os

        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    local, why = windows_path_to_local("C:\\OneDrive", {"C:": tmp_path})
    assert local is None and "link" in why


# --------------------------------------------------------------------------- #
# MountedDevices decoding
# --------------------------------------------------------------------------- #
def test_decode_mounted_device_mbr_and_gpt_and_junk() -> None:
    mbr = (0xAABBCCDD).to_bytes(4, "little") + (1048576).to_bytes(8, "little")
    assert decode_mounted_device(mbr) == ("mbr", "aabbccdd", "1048576")
    u = uuid.uuid4()
    gpt = b"DMIO:ID:" + u.bytes_le
    assert decode_mounted_device(gpt) == ("gpt", str(u))
    assert decode_mounted_device(b"\\??\\x") is None
    assert decode_mounted_device(b"short") is None


# --------------------------------------------------------------------------- #
# users: ProfileList + skip list + NTUSER.DAT requirement
# --------------------------------------------------------------------------- #
def test_list_users_from_profile_list(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path, user="alice", sid="S-1-5-21-1-2-3-1001")
    src = open_source(root)
    users = list_users(src)
    assert [u.name for u in users] == ["alice"]
    assert users[0].sid == "S-1-5-21-1-2-3-1001"
    src.close()


def test_list_users_skips_builtin_and_service_profiles(tmp_path: Path, winbuild) -> None:
    software = winbuild.software(users={
        "S-1-5-21-1-2-3-1001": "C:\\Users\\alice",
        "S-1-5-18": "C:\\Windows\\system32\\config\\systemprofile",
        "S-1-5-21-1-2-3-1002": "C:\\Users\\Default",  # a real SID but a skip-listed folder name
    })
    root = winbuild.root(tmp_path, software=software)
    (root / "Users" / "Default" / "NTUSER.DAT").parent.mkdir(parents=True, exist_ok=True)
    (root / "Users" / "Default" / "NTUSER.DAT").write_bytes(winbuild.ntuser())
    src = open_source(root)
    users = list_users(src)
    assert [u.name for u in users] == ["alice"]
    src.close()


def test_list_users_requires_ntuser_dat(tmp_path: Path, winbuild) -> None:
    software = winbuild.software(users={"S-1-5-21-1-2-3-1001": "C:\\Users\\alice"})
    root = winbuild.root(tmp_path, software=software)
    # remove the NTUSER.DAT the fixture wrote by default -> alice must be dropped
    (root / "Users" / "alice" / "NTUSER.DAT").unlink()
    src = open_source(root)
    assert list_users(src) == []
    src.close()


def test_list_users_falls_back_to_folder_scan_without_profilelist(tmp_path: Path, winbuild) -> None:
    empty_software = winbuild.software(users={})
    root = winbuild.root(tmp_path, software=empty_software, user="bob")
    src = open_source(root)
    users = list_users(src)
    assert [u.name for u in users] == ["bob"]
    assert "could not be read" in users[0].notes[0]
    src.close()


def test_find_user_by_name_and_ambiguity(tmp_path: Path, winbuild) -> None:
    software = winbuild.software(users={"S-1-5-21-1-2-3-1001": "C:\\Users\\alice",
                                        "S-1-5-21-1-2-3-1002": "C:\\Users\\bob"})
    root = winbuild.root(tmp_path, software=software, user="alice")
    (root / "Users" / "bob").mkdir(parents=True)
    (root / "Users" / "bob" / "NTUSER.DAT").write_bytes(winbuild.ntuser())
    src = open_source(root)
    assert find_user(src, "bob").name == "bob"
    with pytest.raises(TransferError, match="several Windows users"):
        find_user(src)
    with pytest.raises(TransferError, match="No Windows user"):
        find_user(src, "carol")
    src.close()


def test_find_user_no_users_at_all(tmp_path: Path) -> None:
    (tmp_path / "root" / "Windows").mkdir(parents=True)
    (tmp_path / "root" / "Users").mkdir()
    src = open_source(tmp_path / "root")
    with pytest.raises(TransferError, match="No Windows user profiles"):
        find_user(src)
    src.close()


def test_list_users_bundle_source_has_exactly_one_user(tmp_path: Path) -> None:
    import json

    bundle = tmp_path / "usb"
    bundle.mkdir()
    (bundle / "lindos-transfer.json").write_text(
        json.dumps({"schema": 1, "computer": "OLD", "user": {"name": "carol", "profile": "C:\\Users\\carol"}}))
    src = open_source(bundle)
    users = list_users(src)
    assert [u.name for u in users] == ["carol"]
    src.close()


# --------------------------------------------------------------------------- #
# resolve_folders(): registry value wins, then default, GUID vs legacy, OneDrive tagging
# --------------------------------------------------------------------------- #
def test_resolve_folders_uses_registry_value_when_present(tmp_path: Path, winbuild) -> None:
    ntuser = winbuild.ntuser(shell_folders={"Personal": "%USERPROFILE%\\MyDocs"})
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    (root / "Users" / "alice" / "MyDocs").mkdir(parents=True)
    src = open_source(root)
    user = find_user(src)
    nt = open_ntuser(user)
    folders = resolve_folders(src, user, ntuser=nt)
    assert folders["documents"].path == root / "Users" / "alice" / "MyDocs"
    assert folders["documents"].source == "registry"
    src.close()


def test_resolve_folders_falls_back_to_default_when_registry_path_is_missing(tmp_path: Path, winbuild) -> None:
    ntuser = winbuild.ntuser(shell_folders={"Personal": "%USERPROFILE%\\Gone"})
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    (root / "Users" / "alice" / "Documents").mkdir(parents=True)
    src = open_source(root)
    user = find_user(src)
    nt = open_ntuser(user)
    folders = resolve_folders(src, user, ntuser=nt)
    assert folders["documents"].path == root / "Users" / "alice" / "Documents"
    assert folders["documents"].source == "default"
    assert "Gone" in folders["documents"].note
    src.close()


def test_resolve_folders_guid_names_never_overridden_by_local_guids(tmp_path: Path, winbuild) -> None:
    # A "Local*" duplicate GUID must never win over the real Downloads GUID.
    ntuser = winbuild.ntuser(shell_folders={
        "{374DE290-123F-4565-9164-39C4925E467B}": "%USERPROFILE%\\RealDownloads",
        "{7d83ee9b-2244-4e70-b1f5-5393042af1e4}": "%USERPROFILE%\\WrongDownloads",
    })
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    (root / "Users" / "alice" / "RealDownloads").mkdir(parents=True)
    src = open_source(root)
    user = find_user(src)
    nt = open_ntuser(user)
    folders = resolve_folders(src, user, ntuser=nt)
    assert folders["downloads"].path == root / "Users" / "alice" / "RealDownloads"
    src.close()


def test_resolve_folders_bundle_source_uses_manifest_folders(tmp_path: Path) -> None:
    import json

    bundle = tmp_path / "usb"
    (bundle / "files" / "documents").mkdir(parents=True)
    (bundle / "lindos-transfer.json").write_text(json.dumps({
        "schema": 1, "computer": "OLD", "user": {"name": "carol"},
        "folders": {"documents": "files/documents"},
    }))
    src = open_source(bundle)
    user = find_user(src)
    folders = resolve_folders(src, user)
    assert folders["documents"].path == bundle / "files" / "documents"
    assert folders["desktop"].path is None and "not in the transfer folder" in folders["desktop"].note
    src.close()


def test_onedrive_roots_from_registry_accounts(tmp_path: Path, winbuild) -> None:
    b = winbuild.HiveBuilder(minor=5)
    from hive_builder import REG_SZ

    def u(t):
        return t.encode("utf-16-le") + b"\x00\x00"

    spec = {"name": "ROOT", "children": [{"name": "Software", "children": [{"name": "Microsoft", "children": [
        {"name": "OneDrive", "children": [{"name": "Accounts", "children": [
            {"name": "Personal", "values": [("UserFolder", REG_SZ, u("C:\\Users\\alice\\OneDrive"))]}]}]}]}]}]}
    root_off = b.add_key_tree(spec)
    ntuser = b.to_bytes(root_offset=root_off)
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    (root / "Users" / "alice" / "OneDrive").mkdir(parents=True)
    src = open_source(root)
    user = find_user(src)
    nt = open_ntuser(user)
    roots = onedrive_roots(src, user, nt, {"C:": root})
    assert len(roots) == 1 and roots[0].onedrive is True
    assert roots[0].path == root / "Users" / "alice" / "OneDrive"
    src.close()
