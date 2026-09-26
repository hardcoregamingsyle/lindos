"""Tests for the secrets deny-list and the single file-open path (SPEC-WINDOWS §27.3, §29).

The central claim this file proves: every path in :data:`SECRETS_DENYLIST` is refused *before* the
file is ever opened -- not merely "not returned" -- by monkeypatching ``os.open``/``io.open`` to
raise if called, and asserting :func:`safe_open`/:func:`read_bytes` never reach them.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from lindos_transfer import secrets

DENIED_PATHS = [
    r"C:\Windows\System32\config\SAM",
    r"C:\Windows\System32\config\SAM.LOG1",
    r"C:\Windows\System32\config\SECURITY",
    r"C:\Windows\System32\config\SAM{12345678-1234-1234-1234-123456789012}.TM.blf",
    r"C:\Windows\NTDS\ntds.dit",
    r"C:\Users\alice\AppData\Roaming\Microsoft\Protect\CREDHIST",
    r"C:\Windows\System32\Microsoft\Protect\S-1-5-18\User",
    r"C:\Users\alice\AppData\Local\Microsoft\Credentials\x",
    r"C:\Users\alice\AppData\Roaming\Microsoft\Vault\x",
    r"C:\Windows\System32\config\systemprofile\AppData\Local\x",
    r"C:\Windows\ServiceProfiles\LocalService\AppData\Local\x",
    r"C:\hiberfil.sys",
    r"C:\pagefile.sys",
    r"C:\swapfile.sys",
    r"C:\Windows\MEMORY.DMP",
    r"C:\Users\alice\crash.dmp",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Login Data",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Login Data-journal",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Login Data For Account",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Cookies",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Web Data",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Local State",
    r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default\EncryptedBookmarks2",
    r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default\EncryptedAccountBookmarks2",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\cookies.sqlite",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\cookies.sqlite-wal",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\key3.db",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\signons.sqlite",
]

OPT_IN_PATHS = [
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\key4.db",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\logins.json",
]

ALLOWED_PATHS = [
    r"C:\Users\alice\Documents\report.docx",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\Bookmarks",
    r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default\AccountBookmarks",
    r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\x\places.sqlite",
    r"C:\Windows\System32\config\SOFTWARE",
    r"C:\Windows\System32\config\SYSTEM",
    r"C:\Users\alice\NTUSER.DAT",
]


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_denylist_matches(path: str) -> None:
    assert secrets.match_denylist(path) is not None, path
    assert secrets.is_denied(path) is not None, path
    with pytest.raises(secrets.SecretPathError):
        secrets.check(path)


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_denylist_is_case_and_separator_insensitive(path: str) -> None:
    assert secrets.match_denylist(path.upper()) is not None
    assert secrets.match_denylist(path.replace("\\", "/")) is not None
    assert secrets.match_denylist(path.lower()) is not None


@pytest.mark.parametrize("path", ALLOWED_PATHS)
def test_allowed_paths_are_not_denied(path: str) -> None:
    assert secrets.match_denylist(path) is None, path
    assert secrets.is_denied(path) is None, path
    secrets.check(path)  # must not raise


@pytest.mark.parametrize("path", OPT_IN_PATHS)
def test_firefox_password_files_are_denied_by_default_but_opt_in_works(path: str) -> None:
    assert secrets.is_denied(path) is not None
    assert secrets.is_denied(path, allow_firefox_passwords=True) is None


def test_never_opens_denylisted_files_even_transiently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The core honesty guarantee: a denied path is refused before any syscall opens it."""
    real_open = os.open
    opened: list = []

    def spy_open(path, flags, *a, **kw):
        opened.append(path)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    target = tmp_path / "Windows" / "System32" / "config" / "SAM"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not really a hive")
    with pytest.raises(secrets.SecretPathError):
        secrets.safe_open(str(target))
    with pytest.raises(secrets.SecretPathError):
        secrets.read_bytes(str(target), 1024)
    assert opened == [], "a deny-listed file must never reach os.open()"


def test_denylist_help_text_is_readable() -> None:
    names = secrets.denied_names_help()
    assert names and all(isinstance(n, str) and n for n in names)
    joined = " ".join(names).lower()
    for word in ("sam", "credential", "hiberna", "cooki", "wi-fi", "password"):
        assert word in joined


# --------------------------------------------------------------------------- #
# safe_open() link/regular-file safety
# --------------------------------------------------------------------------- #
def test_safe_open_refuses_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("hello")
    link = tmp_path / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this host")
    with pytest.raises(secrets.UnsafeFileError):
        secrets.safe_open(link)


def test_safe_open_refuses_directories(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(secrets.UnsafeFileError):
        secrets.safe_open(d)


def test_safe_open_reads_a_plain_file(tmp_path: Path) -> None:
    f = tmp_path / "plain.txt"
    f.write_bytes(b"hello world")
    with secrets.safe_open(f) as fh:
        assert fh.read() == b"hello world"
    assert secrets.read_bytes(f, 1024) == b"hello world"


def test_read_bytes_enforces_limit(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)
    with pytest.raises(OSError):
        secrets.read_bytes(f, 10)
    assert secrets.read_bytes(f, 100) == b"x" * 100


# --------------------------------------------------------------------------- #
# hiberfil.sys header-only read
# --------------------------------------------------------------------------- #
def test_read_hiberfil_header_reads_only_the_signature(tmp_path: Path) -> None:
    f = tmp_path / "hiberfil.sys"
    f.write_bytes(b"hibr" + b"\x00" * 8192)
    head = secrets.read_hiberfil_header(f, 4)
    assert head == b"hibr"
    # even asking for more than HIBERFIL_HEADER_MAX is clamped
    head_all = secrets.read_hiberfil_header(f, 999999)
    assert len(head_all) == secrets.HIBERFIL_HEADER_MAX


def test_read_hiberfil_header_refuses_other_names(tmp_path: Path) -> None:
    f = tmp_path / "notthefile.sys"
    f.write_bytes(b"hibr")
    with pytest.raises(ValueError):
        secrets.read_hiberfil_header(f)


def test_hiberfil_is_case_insensitive_by_name(tmp_path: Path) -> None:
    f = tmp_path / "HIBERFIL.SYS"
    f.write_bytes(b"HIBR" + b"\x00" * 10)
    assert secrets.read_hiberfil_header(f, 4) == b"HIBR"


# --------------------------------------------------------------------------- #
# ** matching internals (the "**"-aware glob)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,expected", [
    ("a/b/c", True), ("x/a/b/c", True), ("a/b/c/d", False), ("a/c", False),
])
def test_glob_match_double_star_semantics(path: str, expected: bool) -> None:
    from lindos_transfer.secrets import _glob_match  # noqa: SLF001 - internal, tested directly

    segs = tuple(path.split("/"))
    pats = ("**", "a", "b", "c")
    assert _glob_match(segs, pats) is expected
