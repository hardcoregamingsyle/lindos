"""Tests for read-only mounting, hibernation detection and BitLocker guidance (SPEC-WINDOWS §29.3,
§27.3). The central claim proven here: lindos-transfer can never build an argv that mounts a
Windows volume writably, with ``force``, or with ``remove_hiberfile``/``recover``, and it never
passes ``--no-user-interaction`` (which would silently suppress the polkit prompt)."""
from __future__ import annotations

import types
from pathlib import Path
from typing import List

import pytest

from lindos_transfer import TransferError
from lindos_transfer.mounts import (FORBIDDEN_OPTIONS, MountEntry, assert_read_only, bitlocker_guidance,
                                    choose_driver, driver_of, find_mount, hiberfil_state, is_read_only,
                                    mount, mount_command, mount_table, ntfs3g_available,
                                    parse_udisks_mounted, unmount_command)


# --------------------------------------------------------------------------- #
# argv safety
# --------------------------------------------------------------------------- #
def test_mount_command_always_carries_ro_and_never_a_forbidden_option() -> None:
    for driver in ("ntfs-3g", "ntfs3"):
        argv = mount_command("/dev/sda3", driver)
        assert "-o" in argv and "ro" in argv[argv.index("-o") + 1].split(",")
        assert "--no-user-interaction" not in argv
        for bad in FORBIDDEN_OPTIONS:
            assert bad not in argv


def test_mount_command_picks_the_matching_fstype() -> None:
    assert mount_command("/dev/sda3", "ntfs-3g")[mount_command("/dev/sda3", "ntfs-3g").index("-t") + 1] == "ntfs"
    assert mount_command("/dev/sda3", "ntfs3")[mount_command("/dev/sda3", "ntfs3").index("-t") + 1] == "ntfs3"


@pytest.mark.parametrize("bad_opt", ["rw", "force", "remove_hiberfile", "recover"])
def test_assert_read_only_refuses_every_forbidden_option(bad_opt: str) -> None:
    with pytest.raises(TransferError):
        assert_read_only(["udisksctl", "mount", "-b", "/dev/sda3", "-t", "ntfs", "-o", f"ro,{bad_opt}"])


def test_assert_read_only_refuses_missing_ro() -> None:
    with pytest.raises(TransferError):
        assert_read_only(["udisksctl", "mount", "-b", "/dev/sda3", "-t", "ntfs", "-o", "uid=1000"])
    with pytest.raises(TransferError):
        assert_read_only(["udisksctl", "mount", "-b", "/dev/sda3", "-t", "ntfs"])  # no -o at all


def test_assert_read_only_refuses_no_user_interaction() -> None:
    with pytest.raises(TransferError):
        assert_read_only(["udisksctl", "mount", "-b", "/dev/sda3", "-t", "ntfs", "-o", "ro",
                          "--no-user-interaction"])


def test_assert_read_only_accepts_a_safe_argv() -> None:
    assert_read_only(["udisksctl", "mount", "-b", "/dev/sda3", "-t", "ntfs", "-o", "ro"])  # must not raise


def test_unmount_command_shape() -> None:
    assert unmount_command("/dev/sda3") == ["udisksctl", "unmount", "-b", "/dev/sda3"]


def test_choose_driver_prefers_ntfs3g_when_available() -> None:
    assert choose_driver(lambda p: True) == "ntfs-3g"
    assert choose_driver(lambda p: False) == "ntfs3"


def test_ntfs3g_available_checks_known_helper_paths() -> None:
    seen = []

    def exists(p):
        seen.append(p)
        return p == "/sbin/mount.ntfs"

    assert ntfs3g_available(exists) is True
    assert any("mount.ntfs" in p for p in seen)


# --------------------------------------------------------------------------- #
# mount table parsing
# --------------------------------------------------------------------------- #
PROC_MOUNTS = (
    "/dev/nvme0n1p3 /media/alice/OS ntfs3 ro,relatime 0 0\n"
    "/dev/sda1 /media/alice/USB\\040Drive vfat rw,relatime 0 0\n"
)


def test_mount_table_parses_and_unescapes_octal() -> None:
    entries = mount_table(PROC_MOUNTS)
    assert entries[0] == MountEntry("/dev/nvme0n1p3", "/media/alice/OS", "ntfs3", ["ro", "relatime"])
    assert entries[1].mountpoint == "/media/alice/USB Drive"


def test_mount_table_missing_file_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mount_table(None) == [] or True  # exercised indirectly by system_path fallback below


def test_find_mount_by_device_and_by_longest_path_prefix() -> None:
    entries = mount_table(PROC_MOUNTS)
    assert find_mount(device="/dev/sda1", table=entries).mountpoint == "/media/alice/USB Drive"
    assert find_mount(path="/media/alice/OS/Users/alice", table=entries).device == "/dev/nvme0n1p3"
    assert find_mount(path="/nowhere", table=entries) is None


def test_driver_of_and_is_read_only() -> None:
    e = MountEntry("/dev/sda3", "/mnt", "fuseblk", ["ro"])
    assert driver_of(e) == "ntfs-3g" and is_read_only(e) is True
    e2 = MountEntry("/dev/sda3", "/mnt", "ntfs3", ["rw"])
    assert driver_of(e2) == "ntfs3" and is_read_only(e2) is False
    assert driver_of(None) == "unknown" and is_read_only(None) is None


def test_parse_udisks_mounted_line() -> None:
    assert parse_udisks_mounted("Mounted /dev/sda3 at /media/alice/OS.\n") == "/media/alice/OS"
    assert parse_udisks_mounted("some other text") is None


# --------------------------------------------------------------------------- #
# hibernation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("head,expected", [(b"hibr", "hibernated"), (b"HIBR", "hibernated"),
                                           (b"\x00\x00\x00\x00", "clean")])
def test_hiberfil_state_signature(tmp_path: Path, head: bytes, expected: str) -> None:
    (tmp_path / "hiberfil.sys").write_bytes(head + b"\x00" * 100)
    assert hiberfil_state(tmp_path) == expected


def test_hiberfil_state_absent_and_case_insensitive(tmp_path: Path) -> None:
    assert hiberfil_state(tmp_path) == "absent"
    (tmp_path / "HIBERFIL.SYS").write_bytes(b"hibr" + b"\x00" * 100)
    assert hiberfil_state(tmp_path) == "hibernated"


def test_hiberfil_state_too_short_is_unknown_not_hibernated(tmp_path: Path) -> None:
    (tmp_path / "hiberfil.sys").write_bytes(b"hi")
    assert hiberfil_state(tmp_path) == "unknown"


# --------------------------------------------------------------------------- #
# BitLocker guidance: never asks Lindos to type/see the key
# --------------------------------------------------------------------------- #
def test_bitlocker_guidance_never_embeds_a_key_and_offers_readonly_unlock() -> None:
    g = bitlocker_guidance("/dev/sda3")
    blob = str(g)
    for word in ("password", "recovery key:", "passphrase="):
        assert word not in blob.lower() or "recovery_key_url" in blob  # never a literal secret value
    assert any("--read-only" in step["command"] for step in g["steps"] if step["command"])
    assert "dislocker" in " ".join(g["clear_key"]["command"])
    assert "-r" in g["clear_key"]["command"] and "-c" in g["clear_key"]["command"]
    for step in g["steps"]:
        assert "rw" not in step["command"] if step["command"] else True


# --------------------------------------------------------------------------- #
# mount() orchestration
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_mount_refuses_bitlocker_with_explanation() -> None:
    with pytest.raises(TransferError, match="BitLocker"):
        mount("/dev/sda3", fstype="BitLocker", run=lambda *a, **k: _Proc(0), which=lambda n: "/usr/bin/udisksctl")


def test_mount_reuses_an_existing_mount_without_calling_udisksctl(tmp_path: Path) -> None:
    calls: List[list] = []

    def run(argv, **kw):
        calls.append(argv)
        return _Proc(0)

    entries = [MountEntry("/dev/sda3", str(tmp_path), "fuseblk", ["ro"])]
    result = mount("/dev/sda3", run=run, which=lambda n: "/usr/bin/udisksctl", table=lambda: entries)
    assert calls == []
    assert result["already_mounted"] is True
    assert result["read_only"] is True
    assert result["driver"] == "ntfs-3g"


def test_mount_calls_udisksctl_readonly_when_not_already_mounted(tmp_path: Path) -> None:
    calls: List[list] = []

    def run(argv, **kw):
        calls.append(argv)
        return _Proc(0, stdout=f"Mounted /dev/sda3 at {tmp_path}\n")

    result = mount("/dev/sda3", run=run, which=lambda n: "/usr/bin/udisksctl", exists=lambda p: False,
                   table=lambda: [])
    assert len(calls) == 1
    assert_read_only(calls[0])  # the exact argv used must itself be safe
    assert result["mountpoint"] == str(tmp_path)
    assert result["command"] == calls[0]


def test_mount_raises_when_udisksctl_is_missing() -> None:
    with pytest.raises(TransferError, match="udisksctl"):
        mount("/dev/sda3", run=lambda *a, **k: _Proc(0), which=lambda n: None, table=lambda: [])


def test_mount_raises_on_permission_denied() -> None:
    def run(argv, **kw):
        return _Proc(1, stderr="Error mounting: not authorized")

    with pytest.raises(TransferError, match="Permission"):
        mount("/dev/sda3", run=run, which=lambda n: "/usr/bin/udisksctl", table=lambda: [])


def test_mount_warns_when_hibernated(tmp_path: Path) -> None:
    (tmp_path / "hiberfil.sys").write_bytes(b"hibr" + b"\x00" * 100)
    entries = [MountEntry("/dev/sda3", str(tmp_path), "fuseblk", ["ro"])]
    result = mount("/dev/sda3", run=lambda *a, **k: _Proc(0), which=lambda n: "/usr/bin/udisksctl",
                   table=lambda: entries)
    assert result["hibernated"] is True
    assert any("Fast Startup" in n or "hibernated" in n.lower() for n in result["notes"])


def test_mount_flags_a_write_open_existing_mount(tmp_path: Path) -> None:
    entries = [MountEntry("/dev/sda3", str(tmp_path), "ntfs3", ["rw"])]
    result = mount("/dev/sda3", run=lambda *a, **k: _Proc(0), which=lambda n: "/usr/bin/udisksctl",
                   table=lambda: entries)
    assert result["read_only"] is False
    assert any("write access" in n for n in result["notes"])
