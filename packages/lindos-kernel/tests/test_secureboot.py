"""lindos_kernel.secureboot: Secure Boot / MOK / kernel-signing status (SPEC-WINDOWS §31.3).

Everything is exercised with injected ``run``/``which``/``exists``/``glob_fn`` callables so no
real ``mokutil``/``sbverify``/UEFI variable is ever touched.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from lindos_kernel import secureboot as sb


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _which(available: Dict[str, str]):
    def _fn(name: str) -> Optional[str]:
        return available.get(name)
    return _fn


# --- firmware -------------------------------------------------------------------------------
def test_firmware_is_uefi_true_when_present() -> None:
    assert sb.firmware_is_uefi(exists=lambda p: p.endswith("/sys/firmware/efi")) is True


def test_firmware_is_uefi_false_when_absent() -> None:
    assert sb.firmware_is_uefi(exists=lambda p: False) is False


# --- secure boot state ------------------------------------------------------------------------
def test_secure_boot_enabled_from_efivar_bytes() -> None:
    # attrs (4 bytes) + state byte == 1 -> enabled
    data = b"\x06\x00\x00\x00\x01"
    assert sb.secure_boot_enabled(read_bytes=lambda p: data) is True


def test_secure_boot_disabled_from_efivar_bytes() -> None:
    data = b"\x06\x00\x00\x00\x00"
    assert sb.secure_boot_enabled(read_bytes=lambda p: data) is False


def test_secure_boot_falls_back_to_mokutil_when_efivar_unreadable() -> None:
    def _raise(path: str) -> bytes:
        raise OSError("no such efivar")

    def _run(cmd, **kwargs):
        assert cmd[0] == "mokutil"
        return _proc(stdout="SecureBoot enabled\n")

    result = sb.secure_boot_enabled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"}),
                                     read_bytes=_raise)
    assert result is True


def test_secure_boot_mokutil_disabled_text() -> None:
    def _raise(path: str) -> bytes:
        raise OSError("no such efivar")

    def _run(cmd, **kwargs):
        return _proc(stdout="SecureBoot disabled\n")

    result = sb.secure_boot_enabled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"}),
                                     read_bytes=_raise)
    assert result is False


def test_secure_boot_unknown_when_no_efivar_and_no_mokutil() -> None:
    def _raise(path: str) -> bytes:
        raise OSError("no such efivar")

    result = sb.secure_boot_enabled(run=lambda *a, **k: _proc(), which=_which({}), read_bytes=_raise)
    assert result is None


# --- MOK -------------------------------------------------------------------------------------
def test_mok_present_requires_both_files() -> None:
    have = {sb.resolve(sb.MOK_PRIV)}
    assert sb.mok_present(exists=lambda p: p in have) is False
    have.add(sb.resolve(sb.MOK_DER))
    assert sb.mok_present(exists=lambda p: p in have) is True


def test_mok_enrolled_unknown_without_mokutil() -> None:
    assert sb.mok_enrolled(which=_which({})) is None


def test_mok_enrolled_true_with_output() -> None:
    def _run(cmd, **kwargs):
        return _proc(stdout="[key 1]\nSHA1 Fingerprint: aa:bb\n")
    assert sb.mok_enrolled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"})) is True


def test_mok_enrolled_false_with_empty_output() -> None:
    def _run(cmd, **kwargs):
        return _proc(stdout="")
    assert sb.mok_enrolled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"})) is False


def test_mok_enrolled_false_with_no_keys_message() -> None:
    def _run(cmd, **kwargs):
        return _proc(stdout="No MOK keys enrolled\n")
    assert sb.mok_enrolled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"})) is False


def test_mok_enrolled_none_on_nonzero_exit() -> None:
    def _run(cmd, **kwargs):
        return _proc(returncode=1, stderr="mokutil: some error\n")
    assert sb.mok_enrolled(run=_run, which=_which({"mokutil": "/usr/bin/mokutil"})) is None


# --- installed kernels + signature ------------------------------------------------------------
def test_installed_lindos_kernels_filters_and_sorts() -> None:
    paths = ["/boot/vmlinuz-6.8.0-31-generic", "/boot/vmlinuz-6.14.0-lindos",
             "/boot/vmlinuz-6.13.0-lindos"]
    found = sb.installed_lindos_kernels(glob_fn=lambda pattern: paths)
    assert found == ["/boot/vmlinuz-6.13.0-lindos", "/boot/vmlinuz-6.14.0-lindos"]


def test_image_signed_unknown_without_sbverify() -> None:
    assert sb.image_signed("/boot/vmlinuz-6.14.0-lindos", which=_which({})) is None


def test_image_signed_true() -> None:
    def _run(cmd, **kwargs):
        assert cmd[:2] == ["sbverify", "--list"]
        return _proc(returncode=0, stdout="signature 1\nimage signature issuers:\n - /CN=Lindos\n")
    result = sb.image_signed("/boot/vmlinuz-6.14.0-lindos", run=_run,
                              which=_which({"sbverify": "/usr/bin/sbverify"}))
    assert result is True


def test_image_signed_false_no_signature_table() -> None:
    def _run(cmd, **kwargs):
        return _proc(returncode=1, stderr="No signature table present\n")
    result = sb.image_signed("/boot/vmlinuz-6.14.0-lindos", run=_run,
                              which=_which({"sbverify": "/usr/bin/sbverify"}))
    assert result is False


# --- full status blob --------------------------------------------------------------------------
def test_status_shape_uefi_secure_boot_on_unsigned() -> None:
    def _run(cmd, **kwargs):
        if cmd[0] == "mokutil" and "--sb-state" in cmd:
            return _proc(stdout="SecureBoot enabled\n")
        if cmd[0] == "mokutil":
            return _proc(stdout="")
        if cmd[0] == "sbverify":
            return _proc(returncode=1, stderr="No signature table present\n")
        raise AssertionError(cmd)

    def _raise_efivar(path: str) -> bytes:
        raise OSError("not readable in test")

    data = sb.status(
        run=_run,
        which=_which({"mokutil": "/usr/bin/mokutil", "sbverify": "/usr/bin/sbverify"}),
        exists=lambda p: p.endswith("/sys/firmware/efi"),
        glob_fn=lambda pattern: ["/boot/vmlinuz-6.14.0-lindos"],
        read_bytes=_raise_efivar,
    )
    assert data["firmware"] == "uefi"
    assert data["secure_boot"] is True  # via the injected mokutil fallback above
    assert data["mok"]["present"] is False
    assert data["kernels"] == [
        {"version": "6.14.0-lindos", "path": "/boot/vmlinuz-6.14.0-lindos", "signed": False}
    ]
    assert data["any_lindos_kernel_signed"] is False
    assert data["tools"]["sbsign"] is False
    assert data["tools"]["sbverify"] is True


def test_status_no_lindos_kernels_signed_is_none() -> None:
    data = sb.status(run=lambda *a, **k: _proc(), which=_which({}), exists=lambda p: False,
                      glob_fn=lambda pattern: [])
    assert data["kernels"] == []
    assert data["any_lindos_kernel_signed"] is None
    assert data["firmware"] == "bios"
    assert data["secure_boot"] is False
