"""Case-insensitive C:\\ drives (ext4 casefold, SPEC-WINDOWS §28.8) and ``PrefixState``.

The ``chattr +F`` ioctl only exists on Linux/ext4, so every test here drives
``lindos_compat.prefix`` through a small in-memory stand-in for the inode-flags ioctl
(``_FakeFs.ioctl``) rather than a real filesystem — this passes on Windows, macOS and Linux
alike, and never calls ``tune2fs`` or touches a real ext4 volume.
"""
from __future__ import annotations

import errno
import json
import os
import struct
from pathlib import Path
from typing import Optional

import pytest

from lindos_compat import prefix


class _FakeFs:
    """Tracks one directory's ``chattr`` flags in memory; ``ioctl`` matches ``prefix.IoctlFn``."""

    def __init__(self, initial_flags: int = 0, set_errno: Optional[int] = None):
        self.flags = initial_flags
        self.set_errno = set_errno
        self.get_calls = 0
        self.set_calls = 0

    def ioctl(self, fd: int, request: int, arg: bytes) -> bytes:
        if request == prefix.FS_IOC_GETFLAGS:
            self.get_calls += 1
            return struct.pack("I", self.flags)
        if request == prefix.FS_IOC_SETFLAGS:
            self.set_calls += 1
            if self.set_errno is not None:
                raise OSError(self.set_errno, "simulated: filesystem does not support casefold")
            (self.flags,) = struct.unpack("I", arg[:4])
            return b""
        raise AssertionError(f"unexpected ioctl request {request!r}")


@pytest.fixture()
def fd_stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patches ``prefix._open_dir`` to return a real, valid fd for a plain file.

    ``os.open()`` cannot open a *directory* on Windows (``PermissionError``), so every test
    that exercises the ioctl path needs a stand-in fd that ``os.close()`` still accepts; the
    fake ioctl above never looks at the path, only at the fd number, so this is safe.
    """
    stand_in = tmp_path / "fd-stand-in"
    stand_in.write_bytes(b"")

    def _open_dir(directory: Path) -> int:
        return os.open(str(stand_in), os.O_RDONLY)

    monkeypatch.setattr(prefix, "_open_dir", _open_dir)
    return tmp_path


# --------------------------------------------------------------------------- #
# is_casefolded / set_casefold
# --------------------------------------------------------------------------- #
def test_is_casefolded_true_false_and_unreadable(fd_stand_in: Path):
    fs_on = _FakeFs(initial_flags=prefix.EXT4_CASEFOLD_FL)
    assert prefix.is_casefolded(fd_stand_in, ioctl=fs_on.ioctl) is True
    fs_off = _FakeFs(initial_flags=0)
    assert prefix.is_casefolded(fd_stand_in, ioctl=fs_off.ioctl) is False

    def raising_ioctl(fd: int, request: int, arg: bytes) -> bytes:
        raise OSError(errno.EOPNOTSUPP, "nope")

    assert prefix.is_casefolded(fd_stand_in, ioctl=raising_ioctl) is None


def test_is_casefolded_none_without_an_ioctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No ``fcntl`` on this host (or none injected) -> "not determined", never a crash."""
    monkeypatch.setattr(prefix, "_default_ioctl", lambda: None)
    assert prefix.is_casefolded(tmp_path, ioctl=None) is None


def test_set_casefold_sets_the_flag_once_and_is_idempotent(fd_stand_in: Path):
    fs = _FakeFs(initial_flags=0)
    assert prefix.set_casefold(fd_stand_in, ioctl=fs.ioctl) == 0
    assert fs.flags & prefix.EXT4_CASEFOLD_FL
    assert fs.set_calls == 1
    # already set -> no redundant SETFLAGS ioctl, still "success"
    assert prefix.set_casefold(fd_stand_in, ioctl=fs.ioctl) == 0
    assert fs.set_calls == 1


def test_set_casefold_preserves_other_flags(fd_stand_in: Path):
    other_flag = 0x00000010  # some unrelated FS_*_FL bit already set on the inode
    fs = _FakeFs(initial_flags=other_flag)
    assert prefix.set_casefold(fd_stand_in, ioctl=fs.ioctl) == 0
    assert fs.flags == (other_flag | prefix.EXT4_CASEFOLD_FL)


@pytest.mark.parametrize("err", [errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL])
def test_set_casefold_returns_errno_on_unsupported_filesystem(fd_stand_in: Path, err: int):
    fs = _FakeFs(set_errno=err)
    assert prefix.set_casefold(fd_stand_in, ioctl=fs.ioctl) == err


def test_set_casefold_without_fcntl_is_enosys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(prefix, "_default_ioctl", lambda: None)
    assert prefix.set_casefold(tmp_path, ioctl=None) == errno.ENOSYS


# --------------------------------------------------------------------------- #
# casefold_probe (doctor's §28.8 check): decided by *trying*, never by reading
# /sys/fs/ext4/features/casefold alone, and it never shells out to tune2fs/mkfs.
# --------------------------------------------------------------------------- #
def test_casefold_probe_active(fd_stand_in: Path):
    fs = _FakeFs(initial_flags=0)
    result = prefix.casefold_probe(fd_stand_in / "prefixes", ioctl=fs.ioctl)
    assert result["state"] == "active" and result["errno"] == 0
    assert "case-insensitive" in result["detail"]
    # the probe directory it created is cleaned up again (never left behind)
    assert list((fd_stand_in / "prefixes").iterdir()) == []


def test_casefold_probe_not_enabled_when_the_kernel_feature_is_present(fd_stand_in: Path):
    fs = _FakeFs(set_errno=errno.EOPNOTSUPP)
    sys_root = fd_stand_in / "sysroot"
    feature = sys_root / "sys" / "fs" / "ext4" / "features" / "casefold"
    feature.parent.mkdir(parents=True)
    feature.write_text("1\n", encoding="utf-8")
    result = prefix.casefold_probe(fd_stand_in / "prefixes", ioctl=fs.ioctl, sys_root=str(sys_root))
    assert result["state"] == "not-enabled" and result["errno"] == errno.EOPNOTSUPP
    assert "tune2fs -O casefold" in result["detail"]


def test_casefold_probe_no_kernel_support_without_the_feature_file(fd_stand_in: Path):
    fs = _FakeFs(set_errno=errno.EOPNOTSUPP)
    result = prefix.casefold_probe(fd_stand_in / "prefixes", ioctl=fs.ioctl, sys_root=str(fd_stand_in / "empty-sysroot"))
    assert result["state"] == "no-kernel-support" and "CONFIG_UNICODE" in result["detail"]


def test_casefold_probe_unsupported_without_fcntl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(prefix, "_default_ioctl", lambda: None)
    result = prefix.casefold_probe(tmp_path / "prefixes")
    assert result["state"] == "unsupported" and result["errno"] == errno.ENOSYS


def test_casefold_probe_other_errno_is_reported_as_error_not_silently_ok(fd_stand_in: Path):
    fs = _FakeFs(set_errno=errno.EACCES)  # not one of the "unsupported filesystem" errnos
    result = prefix.casefold_probe(fd_stand_in / "prefixes", ioctl=fs.ioctl)
    assert result["state"] == "error" and result["errno"] == errno.EACCES


def test_casefold_probe_never_shells_out(fd_stand_in: Path, fake_run):
    """§28.8: "Lindos never runs tune2fs" - casefold_probe takes no ``run`` seam at all,
    so there is nothing here that could invoke it; this pins that contract down."""
    import inspect

    assert "run" not in inspect.signature(prefix.casefold_probe).parameters
    fs = _FakeFs(set_errno=errno.EOPNOTSUPP)
    prefix.casefold_probe(fd_stand_in / "prefixes", ioctl=fs.ioctl)
    assert not fake_run.calls  # nothing was ever executed as a subprocess


# --------------------------------------------------------------------------- #
# ensure_prefix wiring: a *new* wine/umu prefix gets an empty drive_c and a casefold
# attempt before wineboot runs; an existing prefix is left alone.
# --------------------------------------------------------------------------- #
def test_ensure_prefix_records_casefold_result_for_a_new_prefix(fake_core, home: Path, fake_which, fake_run):
    w = fake_which("wine", "wineboot", "wineserver")
    state = prefix.ensure_prefix("newapp", runner="wine", which=w, run=fake_run, casefold=lambda p: 0)
    assert state.created is True
    assert state.casefold is True
    assert state.marker["casefold"] is True
    on_disk = prefix.read_marker(state.path)
    assert on_disk["casefold"] is True
    assert (state.path / "drive_c").is_dir()


def test_ensure_prefix_records_casefold_failure_silently(fake_core, home: Path, fake_which, fake_run):
    w = fake_which("wine", "wineboot", "wineserver")
    state = prefix.ensure_prefix("newapp2", runner="wine", which=w, run=fake_run,
                                 casefold=lambda p: errno.EOPNOTSUPP)
    assert state.created is True
    assert state.casefold is False
    assert state.marker["casefold"] is False
    assert not state.warnings  # a casefold failure is never surfaced as a warning (silent, per §28.8)


def test_ensure_prefix_skips_casefold_for_bottles_and_existing_prefixes(fake_core, home: Path, fake_which, fake_run):
    calls = []
    w = fake_which("wine", "wineboot", "wineserver")
    prefix.ensure_prefix("bottled", runner="bottles", which=w, run=fake_run,
                         casefold=lambda p: calls.append(p) or 0)
    assert calls == []  # casefold is only attempted for wine/umu prefixes
    # a prefix that already exists (system.reg present) is never re-probed
    slug = "existing"
    pfx = prefix.prefix_path(slug)
    pfx.mkdir(parents=True)
    (pfx / "system.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    state = prefix.ensure_prefix(slug, runner="wine", which=w, run=fake_run,
                                 casefold=lambda p: calls.append(p) or 0)
    assert calls == [] and state.created is False and state.casefold is None


# --------------------------------------------------------------------------- #
# PrefixState.as_dict() - regression test: this method must be a real method of the
# dataclass, JSON-serialisable, and must include every field (incl. "casefold").
# --------------------------------------------------------------------------- #
def test_prefix_state_as_dict_is_json_serialisable_and_complete(tmp_path: Path):
    state = prefix.PrefixState(slug="foo", path=tmp_path / "foo", created=True, initialized=True,
                               runner="wine", arch="win64", marker={"runner": "wine"}, warnings=["w"],
                               casefold=True)
    d = state.as_dict()
    assert d["path"] == str(tmp_path / "foo")  # Path -> str
    assert d["slug"] == "foo" and d["casefold"] is True and d["warnings"] == ["w"]
    json.dumps(d)  # must round-trip through JSON (e.g. for a future --json doctor/prefix view)
