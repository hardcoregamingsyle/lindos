"""Tests for source detection: partitions, bundles, and the case-insensitive link-refusing path
resolver (SPEC-WINDOWS §29.2, §29.3)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lindos_transfer import TransferError
from lindos_transfer.sources import (Source, bundle_search_roots, ci_child, ci_path, classify_device,
                                     detect_sources, find_bundles, is_bundle, is_link, is_windows_root,
                                     list_partitions, open_source, parse_lsblk, read_manifest,
                                     windows_to_parts)


# --------------------------------------------------------------------------- #
# case-insensitive path resolution
# --------------------------------------------------------------------------- #
def test_windows_to_parts_splits_both_separators_and_refuses_dotdot() -> None:
    assert windows_to_parts("Users\\alice/Documents") == ["Users", "alice", "Documents"]
    assert windows_to_parts("") == []
    with pytest.raises(ValueError):
        windows_to_parts("a\\..\\b")


def test_ci_child_matches_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "Windows").mkdir()
    assert ci_child(tmp_path, "windows") == tmp_path / "Windows"
    assert ci_child(tmp_path, "WINDOWS") == tmp_path / "Windows"
    assert ci_child(tmp_path, "missing") is None


def test_ci_path_resolves_nested_case_insensitively(tmp_path: Path) -> None:
    nested = tmp_path / "Users" / "Alice" / "Documents"
    nested.mkdir(parents=True)
    (nested / "File.TXT").write_text("hi")
    got = ci_path(tmp_path, ["users", "ALICE", "documents", "file.txt"])
    assert got is not None and got.read_text() == "hi"


def test_ci_path_never_follows_a_symlink_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "secret.txt").write_text("x")
    link = tmp_path / "Documents"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    assert ci_path(tmp_path, ["Documents", "secret.txt"]) is None
    assert ci_path(tmp_path, "Documents") is None  # the link itself is refused by default
    assert ci_path(tmp_path, "Documents", allow_final_link=True) is not None


def test_is_windows_root_requires_both_folders(tmp_path: Path) -> None:
    assert is_windows_root(tmp_path) is False
    (tmp_path / "Windows").mkdir()
    assert is_windows_root(tmp_path) is False
    (tmp_path / "USERS").mkdir()  # case-insensitive match for "Users"
    assert is_windows_root(tmp_path) is True


# --------------------------------------------------------------------------- #
# bundle manifest
# --------------------------------------------------------------------------- #
def _write_manifest(folder: Path, data: dict, *, bom: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data)
    raw = ("\ufeff" + text).encode("utf-8") if bom else text.encode("utf-8")
    (folder / "lindos-transfer.json").write_bytes(raw)


def test_read_manifest_accepts_utf8_bom_and_normalizes_shapes(tmp_path: Path) -> None:
    bundle = tmp_path / "USB"
    _write_manifest(bundle, {
        "schema": 1, "computer": "DESKTOP-1", "user": {"name": "alice", "profile": "C:\\Users\\alice"},
        "windows": {"caption": "Microsoft Windows 11 Pro", "version": "10.0.26100"},
        "folders": {"documents": "files/documents"},
        "browsers": {"browser": "chrome", "profile": "Default"},  # PS 5.1 unwraps 1-item arrays
        "skipped": [{"path": "x", "reason": "online-only"}],
    })
    man = read_manifest(bundle)
    assert man["computer"] == "DESKTOP-1"
    assert man["user"] == {"name": "alice", "profile": "C:\\Users\\alice"}
    assert man["browsers"] == [{"browser": "chrome", "profile": "Default"}]
    assert man["folders"] == {"documents": "files/documents"}


def test_read_manifest_rejects_wrong_schema(tmp_path: Path) -> None:
    bundle = tmp_path / "USB"
    _write_manifest(bundle, {"schema": 2, "computer": "X"})
    with pytest.raises(TransferError, match="schema"):
        read_manifest(bundle)


def test_read_manifest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TransferError):
        read_manifest(tmp_path)


def test_is_bundle_true_only_with_manifest(tmp_path: Path) -> None:
    assert is_bundle(tmp_path) is False
    _write_manifest(tmp_path, {"schema": 1})
    assert is_bundle(tmp_path) is True


def test_find_bundles_respects_depth_and_skips_hidden(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "top", {"schema": 1, "computer": "TOP"})
    _write_manifest(tmp_path / "a" / "b", {"schema": 1, "computer": "DEEP"})
    _write_manifest(tmp_path / ".hidden", {"schema": 1, "computer": "HIDDEN"})
    found = find_bundles([tmp_path], max_depth=2)
    names = {b["computer"] for b in found}
    assert "TOP" in names
    assert "DEEP" in names
    assert "HIDDEN" not in names


def test_find_bundles_ignores_symlinked_directories(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write_manifest(real, {"schema": 1, "computer": "REAL"})
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    found = find_bundles([tmp_path], max_depth=2)
    assert {b["computer"] for b in found} == {"REAL"}


# --------------------------------------------------------------------------- #
# lsblk parsing / classification
# --------------------------------------------------------------------------- #
LSBLK_JSON = json.dumps({"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "fstype": None, "children": [
        {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "fstype": "vfat",
         "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "size": 500000000},
        {"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "fstype": "ntfs", "label": "OS",
         "size": 512110190592, "mountpoint": None},
        {"name": "nvme0n1p4", "path": "/dev/nvme0n1p4", "fstype": "BitLocker", "size": 999999},
        {"name": "nvme0n1p5", "path": "/dev/nvme0n1p5", "fstype": "refs", "size": 999999},
    ]},
]})


def test_parse_lsblk_flattens_children_with_parent() -> None:
    devs = parse_lsblk(LSBLK_JSON)
    by_name = {d["name"]: d for d in devs}
    assert by_name["nvme0n1p3"]["parent"] == "nvme0n1"
    assert len(devs) == 5


def test_parse_lsblk_survives_garbage() -> None:
    assert parse_lsblk("not json") == []
    assert parse_lsblk("") == []


def test_classify_device_skips_efi_and_flags_bitlocker_refs() -> None:
    devs = {d["name"]: d for d in parse_lsblk(LSBLK_JSON)}
    assert classify_device(devs["nvme0n1p1"]) is None  # EFI system partition
    ntfs = classify_device(devs["nvme0n1p3"])
    assert ntfs["fstype"] == "ntfs" and ntfs["bitlocker"] is False and ntfs["note"] == ""
    bl = classify_device(devs["nvme0n1p4"])
    assert bl["bitlocker"] is True and "BitLocker" in bl["note"]
    refs = classify_device(devs["nvme0n1p5"])
    assert "ReFS" in refs["note"]


def test_list_partitions_reports_windows_and_hibernation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lindos_transfer import mounts

    mnt = tmp_path / "mnt"
    (mnt / "Windows").mkdir(parents=True)
    (mnt / "Users").mkdir()
    (mnt / "hiberfil.sys").write_bytes(b"hibr" + b"\x00" * 100)

    def run(argv, **kw):
        import types
        text = json.dumps({"blockdevices": [
            {"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "fstype": "ntfs", "size": 1000,
             "mountpoint": str(mnt)},
        ]})
        return types.SimpleNamespace(returncode=0, stdout=text, stderr="")

    entries = [mounts.MountEntry("/dev/nvme0n1p3", str(mnt), "fuseblk", ["ro"])]
    parts = list_partitions(run=run, table=entries)
    assert len(parts) == 1
    assert parts[0]["windows"] is True
    assert parts[0]["hibernated"] is True
    assert "hibernated" in parts[0]["note"] or "Windows was hibernated" in parts[0]["note"]


def test_list_partitions_returns_empty_on_lsblk_failure() -> None:
    import types

    def run(argv, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="lsblk: not found")

    assert list_partitions(run=run, table=[]) == []


def test_detect_sources_merges_partitions_and_bundles(tmp_path: Path) -> None:
    import types

    mnt = tmp_path / "mnt"
    (mnt / "Windows").mkdir(parents=True)
    (mnt / "Users").mkdir()

    def run(argv, **kw):
        text = json.dumps({"blockdevices": [
            {"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "fstype": "ntfs", "size": 1000,
             "mountpoint": str(mnt)}]})
        return types.SimpleNamespace(returncode=0, stdout=text, stderr="")

    usb = tmp_path / "usb"
    _write_manifest(usb, {"schema": 1, "computer": "OLD-PC"})
    data = detect_sources(run=run, bundle_roots=[usb], table=[])
    assert data["partitions"][0]["windows"] is True
    assert data["bundles"][0]["computer"] == "OLD-PC"


# --------------------------------------------------------------------------- #
# open_source()
# --------------------------------------------------------------------------- #
def test_open_source_refuses_a_bare_device_path() -> None:
    with pytest.raises(TransferError, match="mount"):
        open_source("/dev/sda3")


def test_open_source_refuses_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(TransferError):
        open_source(tmp_path / "nope")


def test_open_source_finds_a_single_nested_bundle(tmp_path: Path) -> None:
    usb = tmp_path / "usb"
    inner = usb / "LindosTransfer-DESKTOP-1-20260926-1010"
    _write_manifest(inner, {"schema": 1, "computer": "DESKTOP-1", "user": {"name": "alice"}})
    src = open_source(usb)
    assert src.is_bundle and src.computer == "DESKTOP-1"


def test_open_source_reports_ambiguity_with_several_bundles(tmp_path: Path) -> None:
    usb = tmp_path / "usb"
    _write_manifest(usb / "one", {"schema": 1, "computer": "A"})
    _write_manifest(usb / "two", {"schema": 1, "computer": "B"})
    with pytest.raises(TransferError, match="several transfer folders"):
        open_source(usb)


def test_open_source_neither_windows_nor_bundle_explains_why(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(TransferError, match="neither a Windows drive"):
        open_source(plain)


def test_source_hive_rejects_non_software_system_names(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path)
    src = open_source(root)
    with pytest.raises(ValueError):
        src.hive("SAM")
    src.close()


def test_source_as_plan_dict_and_close(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path)
    src = open_source(root)
    d = src.as_plan_dict()
    assert d["type"] == "partition" and d["root"] == str(root)
    assert src.hive("SOFTWARE") is not None
    src.close()
    # closing twice must never raise
    src.close()
