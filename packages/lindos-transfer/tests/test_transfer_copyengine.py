"""Tests for the read-only, resumable, placeholder-aware copy engine (SPEC-WINDOWS §29.7)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

import pytest

from lindos_transfer.copyengine import (ATTR_OFFLINE, ATTR_RECALL_ON_DATA_ACCESS, ATTR_REPARSE_POINT,
                                        CopyEngine, DiskFullError, Journal, REASON_LINK,
                                        REASON_PLACEHOLDER, check_free_space, conflict_name,
                                        copy_to_scratch, human_size, open_private, placeholder_reason,
                                        write_generated)


def test_human_size_units() -> None:
    assert human_size(0) == "0 bytes"
    assert human_size(500) == "500 bytes"
    assert human_size(1536) == "1.5 KB"
    assert human_size(1_500_000) == "1.5 MB"
    assert human_size(5_000_000_000) == "5.0 GB"


def test_conflict_name_never_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "photo.jpg"
    dest.write_bytes(b"x")
    c1 = conflict_name(dest)
    assert c1.name == "photo (from Windows).jpg"
    c1.write_bytes(b"y")
    c2 = conflict_name(dest)
    assert c2.name == "photo (from Windows 2).jpg"


def test_conflict_name_handles_dotfiles(tmp_path: Path) -> None:
    dest = tmp_path / ".bashrc"
    dest.write_bytes(b"x")
    c1 = conflict_name(dest)
    assert c1.name == ".bashrc (from Windows)"


def test_check_free_space_raises_when_not_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil as _shutil

    class Fake:
        free = 100

    monkeypatch.setattr(_shutil, "disk_usage", lambda p: Fake())
    from lindos_transfer import TransferError

    with pytest.raises(TransferError, match="Not enough free space"):
        check_free_space(tmp_path, 10_000_000)
    Fake.free = 10_000_000_000
    check_free_space(tmp_path, 10)  # must not raise


# --------------------------------------------------------------------------- #
# placeholder_reason(): ntfs3 xattr path, ntfs-3g link path, ntfs3 fallback heuristic
# --------------------------------------------------------------------------- #
def _stat_like(size: int, blocks: int):
    import types

    return types.SimpleNamespace(st_size=size, st_blocks=blocks)


def test_placeholder_reason_ntfs3_xattr_recall_on_data_access() -> None:
    attrs = (ATTR_REPARSE_POINT | ATTR_RECALL_ON_DATA_ACCESS).to_bytes(4, "little")
    reason = placeholder_reason("C:/x", _stat_like(1000, 0), driver="ntfs3",
                                getxattr=lambda p, n: attrs)
    assert reason == REASON_PLACEHOLDER


def test_placeholder_reason_ntfs3_xattr_offline_only() -> None:
    attrs = (ATTR_OFFLINE).to_bytes(4, "little")
    reason = placeholder_reason("C:/x", _stat_like(1000, 5), driver="ntfs3", getxattr=lambda p, n: attrs)
    assert reason is not None and "offline" in reason.lower()


def test_placeholder_reason_ntfs3_sparse_or_compressed_is_not_a_placeholder() -> None:
    attrs = (ATTR_REPARSE_POINT | 0x200).to_bytes(4, "little")  # sparse, not cloud
    reason = placeholder_reason("C:/x", _stat_like(1000, 0), driver="ntfs3", getxattr=lambda p, n: attrs)
    assert reason is None


def test_placeholder_reason_ntfs3_fallback_heuristic_without_xattr() -> None:
    reason = placeholder_reason("C:/x", _stat_like(100000, 0), driver="ntfs3", getxattr=None)
    assert reason == REASON_PLACEHOLDER


def test_placeholder_reason_ntfs3g_never_flags_regular_files() -> None:
    # ntfs-3g shows cloud files as symlinks (handled in walk()), never as a "placeholder" regular file
    reason = placeholder_reason("C:/x", _stat_like(100000, 0), driver="ntfs-3g", getxattr=None)
    assert reason is None


def test_placeholder_reason_small_local_file_is_not_flagged() -> None:
    reason = placeholder_reason("C:/x", _stat_like(50, 1), driver="ntfs3", getxattr=None)
    assert reason is None


# --------------------------------------------------------------------------- #
# walk(): skip rules, legacy junctions, cloud-placeholder symlinks, deny-listed files
# --------------------------------------------------------------------------- #
def _touch(p: Path, data: bytes = b"x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_walk_skips_noise_files_and_dirs(tmp_path: Path) -> None:
    src = tmp_path / "Documents"
    _touch(src / "letter.txt", b"hello")
    _touch(src / "desktop.ini", b"[.ShellClassInfo]")
    _touch(src / "Thumbs.db", b"x")
    _touch(src / "~$draft.docx", b"x")
    _touch(src / "NTUSER.DAT", b"x")
    _touch(src / "$RECYCLE.BIN" / "x.txt", b"x")
    _touch(src / "System Volume Information" / "x", b"x")
    _touch(src / "AppData" / "Local" / "x", b"x")
    engine = CopyEngine(use_default_xattr=False)
    files = [rel for kind, _p, rel, _i in engine.walk(src) if kind == "file"]
    assert files == ["letter.txt"]


def test_walk_never_follows_a_legacy_junction_silently(tmp_path: Path) -> None:
    src = tmp_path / "profile"
    _touch(src / "real.txt", b"x")
    target = tmp_path / "target"
    _touch(target / "loop.txt", b"must not be copied")
    link = src / "My Documents"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    engine = CopyEngine(use_default_xattr=False)
    events = list(engine.walk(src))
    files = [rel for kind, _p, rel, _i in events if kind == "file"]
    skips = [(rel, why) for kind, _p, rel, why in events if kind == "skip"]
    assert files == ["real.txt"]
    assert skips == []  # a *legacy* junction name is skipped quietly, not reported


def test_walk_reports_a_non_legacy_link_as_skipped(tmp_path: Path) -> None:
    src = tmp_path / "profile"
    target = tmp_path / "target"
    target.mkdir()
    link = src / "MyLink"
    src.mkdir()
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    engine = CopyEngine(use_default_xattr=False)
    skips = [(rel, why) for kind, _p, rel, why in engine.walk(src) if kind == "skip"]
    assert skips == [("MyLink", REASON_LINK)]


def test_walk_reports_ntfs3g_cloud_placeholder_symlink(tmp_path: Path) -> None:
    src = tmp_path / "OneDrive"
    src.mkdir()
    link = src / "big.mkv"
    try:
        os.symlink("unsupported reparse tag 0x9000001a", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    engine = CopyEngine(use_default_xattr=False, driver="ntfs-3g")
    skips = [(rel, why) for kind, _p, rel, why in engine.walk(src) if kind == "skip"]
    assert skips == [("big.mkv", REASON_PLACEHOLDER)]


def test_walk_skips_and_reports_denylisted_files(tmp_path: Path) -> None:
    src = tmp_path / "profile"
    _touch(src / "Documents" / "keep.txt", b"x")
    _touch(src / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Login Data", b"secret")
    # AppData is skipped wholesale by name anyway; use a path the walker still descends into
    engine = CopyEngine(use_default_xattr=False)
    files = [rel for kind, _p, rel, _i in engine.walk(src) if kind == "file"]
    assert files == ["Documents/keep.txt"]


def test_walk_root_itself_denylisted_yields_one_skip(tmp_path: Path) -> None:
    root = tmp_path / "system32" / "config" / "SAM"
    root.mkdir(parents=True)
    engine = CopyEngine(use_default_xattr=False)
    events = list(engine.walk(root))
    assert len(events) == 1 and events[0][0] == "skip"


def test_scan_counts_files_and_bytes(tmp_path: Path) -> None:
    src = tmp_path / "Documents"
    _touch(src / "a.txt", b"12345")
    _touch(src / "sub" / "b.txt", b"1234567890")
    engine = CopyEngine(use_default_xattr=False)
    res = engine.scan(src)
    assert res.files == 2 and res.bytes == 15


# --------------------------------------------------------------------------- #
# copy_tree / copy_file: real copies, conflicts, journal resume, disk-full
# --------------------------------------------------------------------------- #
def test_copy_tree_preserves_mtime_and_verifies_size(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    f = src / "a.txt"
    _touch(f, b"hello world")
    os.utime(f, (1000000000, 1000000000))
    engine = CopyEngine(use_default_xattr=False)
    stats = engine.copy_tree(src, dest, item="documents")
    assert stats.files == 1 and stats.bytes == 11 and not stats.errors
    out = dest / "a.txt"
    assert out.read_bytes() == b"hello world"
    assert int(out.stat().st_mtime) == 1000000000


def test_copy_tree_identical_file_is_skipped_not_recopied(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    f = src / "a.txt"
    _touch(f, b"hello")
    os.utime(f, (1000000000, 1000000000))
    dest.mkdir()
    out = dest / "a.txt"
    out.write_bytes(b"hello")
    os.utime(out, (1000000000, 1000000000))
    engine = CopyEngine(use_default_xattr=False)
    stats = engine.copy_tree(src, dest, item="documents")
    assert stats.identical == 1 and stats.files == 0


def test_copy_tree_conflicting_file_keeps_both(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _touch(src / "a.txt", b"new content")
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"old content, different")
    engine = CopyEngine(use_default_xattr=False)
    stats = engine.copy_tree(src, dest, item="documents")
    assert stats.renamed == 1 and stats.files == 1
    assert (dest / "a.txt").read_bytes() == b"old content, different"
    assert (dest / "a (from Windows).txt").read_bytes() == b"new content"


def test_copy_tree_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _touch(src / "a.txt", b"data")
    engine = CopyEngine(use_default_xattr=False, dry_run=True)
    stats = engine.copy_tree(src, dest, item="documents")
    assert stats.files == 1
    assert not dest.exists()


def test_copy_file_denylisted_source_is_skipped_not_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "system32" / "config" / "SAM"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    opened = []
    monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open")))
    engine = CopyEngine(use_default_xattr=False)
    stats = engine.copy_file(src, tmp_path / "out", item="x")
    assert stats.skipped and stats.files == 0


def test_journal_resume_skips_already_copied_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    f = src / "a.txt"
    _touch(f, b"hello")
    os.utime(f, (1000000000, 1000000000))
    journal_path = tmp_path / "journal.jsonl"
    journal = Journal(journal_path)
    engine = CopyEngine(use_default_xattr=False, journal=journal)
    stats1 = engine.copy_tree(src, dest, item="documents")
    assert stats1.files == 1
    journal.close()
    if not sys.platform.startswith("win"):
        assert (journal_path.stat().st_mode & 0o777) == 0o600

    # a fresh engine, re-reading the same journal file, must not touch the filesystem again
    journal2 = Journal(journal_path)
    engine2 = CopyEngine(use_default_xattr=False, journal=journal2)
    stats2 = engine2.copy_tree(src, dest, item="documents")
    assert stats2.identical == 1 and stats2.files == 0


# --------------------------------------------------------------------------- #
# open_private(): the journal/plan/report files are 0600 (independent of the umask), their
# parent directory 0700 (sec-transfer:state-dir-file-permissions)
# --------------------------------------------------------------------------- #
def test_open_private_creates_a_0600_file_in_a_0700_directory(tmp_path: Path) -> None:
    path = tmp_path / "state" / "20260926-103000-ab12" / "plan.json"
    with open_private(path, "w") as fh:
        fh.write("hello")
    assert path.read_text(encoding="utf-8") == "hello"
    if not sys.platform.startswith("win"):
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_open_private_append_mode_does_not_truncate(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    with open_private(path, "a") as fh:
        fh.write("one\n")
    with open_private(path, "a") as fh:
        fh.write("two\n")
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_open_private_ignores_a_permissive_umask(tmp_path: Path) -> None:
    """Regression test: a plain ``open(path, 'w')`` would be world-readable (0644) under Ubuntu's
    default umask (022); ``open_private`` must not depend on the umask at all."""
    if sys.platform.startswith("win") or not hasattr(os, "umask"):
        pytest.skip("POSIX file-mode semantics only")
    old = os.umask(0o022)
    try:
        path = tmp_path / "report.json"
        with open_private(path, "wb") as fh:
            fh.write(b"{}")
        assert (path.stat().st_mode & 0o777) == 0o600
    finally:
        os.umask(old)


def test_copy_tree_raises_disk_full_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _touch(src / "a.txt", b"data")

    def bad_mkdir(self, *a, **k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(Path, "mkdir", bad_mkdir)
    engine = CopyEngine(use_default_xattr=False)
    with pytest.raises(DiskFullError):
        engine.copy_tree(src, dest, item="documents")


def test_emit_callback_receives_progress_events(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _touch(src / "a.txt", b"data")
    events = []
    engine = CopyEngine(use_default_xattr=False, emit=events.append, total_bytes=4)
    engine.copy_tree(src, dest, item="documents")
    kinds = [e["event"] for e in events]
    assert "file" in kinds and "progress" in kinds


# --------------------------------------------------------------------------- #
# copy_to_scratch / write_generated
# --------------------------------------------------------------------------- #
def test_copy_to_scratch_streams_through_the_secrets_gate(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    dest = tmp_path / "out" / "a.txt"
    n = copy_to_scratch(src, dest)
    assert n == 5 and dest.read_bytes() == b"hello"


def test_write_generated_identical_written_renamed(tmp_path: Path) -> None:
    dest = tmp_path / "Bookmarks.html"
    status, final = write_generated(dest, b"<html>1</html>")
    assert status == "written" and final == dest
    status2, final2 = write_generated(dest, b"<html>1</html>")
    assert status2 == "identical" and final2 == dest
    status3, final3 = write_generated(dest, b"<html>2</html>")
    assert status3 == "renamed" and final3.name == "Bookmarks (from Windows).html"


def test_write_generated_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    dest = tmp_path / "x.html"
    status, final = write_generated(dest, b"data", dry_run=True)
    assert status == "written" and not dest.exists()
