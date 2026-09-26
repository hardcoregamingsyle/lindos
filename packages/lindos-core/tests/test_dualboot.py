"""lindos.dualboot + the lindos-dualboot CLI + the dual-boot/Wi-Fi/binfmt lindos-helper actions
(SPEC-WINDOWS §27, §30.3, §30.4, §28.7).

Hermetic: no network, no Wine, no root, no real efibootmgr/grub-probe/nmcli/systemd-binfmt — every
external command is reached through an injectable ``run``/``which`` (module functions) or through
``LINDOS_HELPER_DRYRUN=1`` (the CLI/helper scripts), and every filesystem path goes through a
synthetic ``root``/``LINDOS_ROOT`` (never the real ``/sys``, ``/proc``, ``/boot`` or
``/etc/NetworkManager``).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from lindos import dualboot as ldualboot
from lindos import helper as lhelper

# paths of the package under test (computed here, not imported from conftest: under
# --import-mode=importlib "conftest" resolves to the repository-level tests/conftest.py)
_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = _PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
HELPER = LIBEXEC / "lindos-helper"


# --- loading lindos-helper (a script, no .py suffix) as a module, to unit-test its internals ----
def _load_bin(path: Path, modname: str) -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # never leave a __pycache__ inside root/usr/libexec (deb payload)
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


@pytest.fixture()
def helper_mod(core_env):
    """A fresh import of ``lindos-helper`` (env already points LINDOS_ROOT/HOME at scratch dirs)."""
    return _load_bin(HELPER, "lindos_core_helper_bin_dualboot_test")


class FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


# =================================================================================================
# lindos.dualboot — efibootmgr parsing
# =================================================================================================
EFIBOOTMGR_SAMPLE = (
    "BootCurrent: 0002\n"
    "Timeout: 0 seconds\n"
    "BootOrder: 0002,0000,0001,0003,0004\n"
    "Boot0000* Windows Boot Manager\tHD(1,GPT,AAAAAAAA-0000-0000-0000-000000000001,0x800,0x32000)"
    "/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
    "Boot0001  Windows Boot Manager\tHD(2,GPT,BBBBBBBB-0000-0000-0000-000000000002,0x800,0x32000)"
    "/File(\\EFI\\evil\\notwindows.efi)\n"
    "Boot0002* Lindos\tHD(3,GPT,CCCCCCCC-0000-0000-0000-000000000003,0x800,0x32000)"
    "/File(\\EFI\\lindos\\shimx64.efi)\n"
    "Boot0003  Windows Boot Manager\tHD(4,GPT,DDDDDDDD-0000-0000-0000-000000000004,0x800,0x32000)"
    "/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
    "Boot0004* shim\t.\\EFI\\ubuntu\\shimx64.efi\n"
)


def test_parse_efibootmgr_header_fields() -> None:
    parsed = ldualboot.parse_efibootmgr(EFIBOOTMGR_SAMPLE)
    assert parsed["bootcurrent"] == "0002"
    assert parsed["timeout"] == 0
    assert parsed["bootorder"] == ["0002", "0000", "0001", "0003", "0004"]
    assert parsed["bootnext"] is None
    assert len(parsed["entries"]) == 5


def test_parse_efibootmgr_windows_entries_several_stale_inactive_and_fake_rejected() -> None:
    """Several Windows entries (one active, one stale/inactive), a non-Windows active entry, and a
    label-only fake ("Windows Boot Manager" pointing at a different loader) that must be rejected."""
    parsed = ldualboot.parse_efibootmgr(EFIBOOTMGR_SAMPLE)
    wins = ldualboot.windows_entries(parsed)
    assert [w.num for w in wins] == ["0000", "0003"]
    assert wins[0].active is True and wins[0].partuuid == "AAAAAAAA-0000-0000-0000-000000000001"
    assert wins[1].active is False    # stale: not in the active boot order path any more
    # the fake: labelled "Windows Boot Manager" but its loader is not bootmgfw.efi
    fake = next(e for e in parsed["entries"] if e.num == "0001")
    assert fake.label == "Windows Boot Manager" and fake.is_windows is False
    # a real, non-Windows active entry (Lindos itself) must never be misclassified
    lindos_entry = next(e for e in parsed["entries"] if e.num == "0002")
    assert lindos_entry.is_windows is False


@pytest.mark.parametrize("loader, expected", [
    ("\\EFI\\Microsoft\\Boot\\bootmgfw.efi", True),
    ("\\efi\\microsoft\\boot\\BOOTMGFW.EFI", True),          # case-insensitive
    (".\\EFI\\Microsoft\\Boot\\bootmgfw.efi", True),          # shim-style leading '.'
    ("/EFI/Microsoft/Boot/bootmgfw.efi", True),              # forward slashes
    ("\\EFI\\Microsoft\\Boot\\bootmgr.efi", False),           # similar but wrong file
    ("\\EFI\\ubuntu\\shimx64.efi", False),
    ("", False),
])
def test_is_windows_loader(loader: str, expected: bool) -> None:
    assert ldualboot._is_windows_loader(loader) is expected


def test_parse_efibootmgr_tolerant_of_garbage() -> None:
    assert ldualboot.parse_efibootmgr("") == {"bootnext": None, "bootcurrent": None, "timeout": None,
                                              "bootorder": [], "entries": []}
    parsed = ldualboot.parse_efibootmgr("not efibootmgr output at all\n\xff\xfe garbage\n")
    assert parsed["entries"] == []


def test_parse_efibootmgr_entry_with_no_devpath_column() -> None:
    parsed = ldualboot.parse_efibootmgr("Boot0005* Weird entry with no device path\n")
    assert len(parsed["entries"]) == 1
    e = parsed["entries"][0]
    assert e.num == "0005" and e.loader == "" and e.partuuid == "" and e.is_windows is False


# =================================================================================================
# lindos.dualboot — GRUB / os-prober
# =================================================================================================
GRUB_CFG_SAMPLE = (
    "menuentry 'Windows Boot Manager (on /dev/sda1)' --class windows --class os "
    "$menuentry_id_option 'osprober-efi-ABCD-1234' {\n    insmod part_gpt\n}\n"
    "menuentry 'Windows 10 (loader) (on /dev/sda2)' --class windows --class os "
    "$menuentry_id_option 'osprober-chain-hd0,gpt2' {\n    insmod chain\n}\n"
    "menuentry 'Ubuntu' --class ubuntu --class gnu-linux --class gnu --class os "
    "$menuentry_id_option 'gnulinux-simple-abcd' {\n}\n"
)


def test_grub_windows_entries_extracts_only_windows_ids() -> None:
    rows = ldualboot.grub_windows_entries(GRUB_CFG_SAMPLE)
    assert rows == [
        {"id": "osprober-efi-ABCD-1234", "title": "Windows Boot Manager (on /dev/sda1)"},
        {"id": "osprober-chain-hd0,gpt2", "title": "Windows 10 (loader) (on /dev/sda2)"},
    ]


def test_grub_windows_entries_dedupes_and_is_tolerant() -> None:
    doubled = GRUB_CFG_SAMPLE + GRUB_CFG_SAMPLE
    rows = ldualboot.grub_windows_entries(doubled)
    assert len(rows) == 2
    assert ldualboot.grub_windows_entries("") == []
    assert ldualboot.grub_windows_entries("menuentry 'broken") == []


# =================================================================================================
# lindos.dualboot — firmware / Secure Boot / TPM
# =================================================================================================
@pytest.fixture()
def sys_root(tmp_path: Path) -> Path:
    (tmp_path / "sys" / "firmware" / "efi" / "efivars").mkdir(parents=True)
    (tmp_path / "sys" / "class" / "tpm" / "tpm0").mkdir(parents=True)
    (tmp_path / "boot" / "grub").mkdir(parents=True)
    return tmp_path


def _write_secureboot_efivar(root: Path, enabled: bool) -> None:
    path = root / "sys" / "firmware" / "efi" / "efivars" / \
        "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    path.write_bytes(bytes([0x06, 0x00, 0x00, 0x00, 1 if enabled else 0]))


def test_firmware_uefi_vs_bios(sys_root: Path, tmp_path: Path) -> None:
    assert ldualboot.firmware(root=str(sys_root)) == "uefi"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ldualboot.firmware(root=str(empty)) == "bios"
    assert ldualboot.firmware(root=None) in ("uefi", "bios")   # never raises on the real host either


def test_secure_boot_state_from_efivar(sys_root: Path) -> None:
    _write_secureboot_efivar(sys_root, enabled=True)
    assert ldualboot.secure_boot_state(root=str(sys_root)) == "enabled"
    _write_secureboot_efivar(sys_root, enabled=False)
    assert ldualboot.secure_boot_state(root=str(sys_root)) == "disabled"


def test_secure_boot_state_mokutil_fallback(sys_root: Path) -> None:
    # no efivar file at all: falls back to `mokutil --sb-state`
    calls: List[List[str]] = []

    def fake_which(name: str) -> Optional[str]:
        return "/usr/bin/mokutil" if name == "mokutil" else None

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return FakeProc("SecureBoot disabled\n")

    assert ldualboot.secure_boot_state(root=str(sys_root), which=fake_which, run=fake_run) == "disabled"
    assert calls == [["/usr/bin/mokutil", "--sb-state"]]


def test_secure_boot_state_unknown_when_nothing_available(sys_root: Path) -> None:
    assert ldualboot.secure_boot_state(root=str(sys_root), which=lambda n: None) == "unknown"


def test_secure_boot_state_never_raises_on_command_failure(sys_root: Path) -> None:
    def boom(cmd, **kw):
        raise OSError("no such tool")

    assert ldualboot.secure_boot_state(root=str(sys_root), which=lambda n: "/x/mokutil", run=boom) == "unknown"


def test_tpm_version(sys_root: Path, tmp_path: Path) -> None:
    (sys_root / "sys" / "class" / "tpm" / "tpm0" / "tpm_version_major").write_text("2\n", encoding="utf-8")
    assert ldualboot.tpm_version(root=str(sys_root)) == 2
    empty = tmp_path / "no-tpm"
    empty.mkdir()
    assert ldualboot.tpm_version(root=str(empty)) is None
    (sys_root / "sys" / "class" / "tpm" / "tpm0" / "tpm_version_major").write_text("nope", encoding="utf-8")
    assert ldualboot.tpm_version(root=str(sys_root)) is None


# =================================================================================================
# lindos.dualboot — grubenv_writable
# =================================================================================================
def test_grubenv_writable_missing_grub_dir(tmp_path: Path) -> None:
    assert ldualboot.grubenv_writable(root=str(tmp_path)) is False


def test_grubenv_writable_grub_probe_ext4_none(sys_root: Path) -> None:
    def run_ok(cmd, **kw):
        return FakeProc("ext4\n" if cmd[1] == "--target=fs" else "none\n")

    assert ldualboot.grubenv_writable(root=str(sys_root), run=run_ok) is True


@pytest.mark.parametrize("fs, abstraction, expected", [
    ("btrfs", "none", False),
    ("zfs", "none", False),
    ("ext4", "lvm", False),
    ("ext4", "diskfilter", False),
    ("ext4", "none", True),
    ("xfs", "", True),
])
def test_grubenv_writable_grub_probe_matrix(sys_root: Path, fs: str, abstraction: str, expected: bool) -> None:
    def run_probe(cmd, **kw):
        return FakeProc(fs if cmd[1] == "--target=fs" else abstraction)

    assert ldualboot.grubenv_writable(root=str(sys_root), run=run_probe) is expected


def test_grubenv_writable_grub_probe_missing_defaults_true(sys_root: Path) -> None:
    def missing(cmd, **kw):
        raise FileNotFoundError("no grub-probe")

    assert ldualboot.grubenv_writable(root=str(sys_root), run=missing) is True


def test_grubenv_writable_proc_mounts_fallback_btrfs(sys_root: Path) -> None:
    (sys_root / "proc").mkdir()
    (sys_root / "proc" / "mounts").write_text("/dev/sda2 /boot btrfs rw,relatime 0 0\n", encoding="utf-8")

    def missing(cmd, **kw):
        raise FileNotFoundError()

    assert ldualboot.grubenv_writable(root=str(sys_root), run=missing) is False


def test_grubenv_writable_proc_mounts_fallback_lvm_device(sys_root: Path) -> None:
    (sys_root / "proc").mkdir()
    (sys_root / "proc" / "mounts").write_text(
        "/dev/mapper/vg0-boot /boot ext4 rw,relatime 0 0\n", encoding="utf-8")

    def missing(cmd, **kw):
        raise FileNotFoundError()

    assert ldualboot.grubenv_writable(root=str(sys_root), run=missing) is False


def test_grubenv_writable_proc_mounts_fallback_ok(sys_root: Path) -> None:
    (sys_root / "proc").mkdir()
    (sys_root / "proc" / "mounts").write_text("/dev/sda2 /boot ext4 rw,relatime 0 0\n", encoding="utf-8")

    def missing(cmd, **kw):
        raise FileNotFoundError()

    assert ldualboot.grubenv_writable(root=str(sys_root), run=missing) is True


# =================================================================================================
# lindos.dualboot — status()
# =================================================================================================
def test_status_shape_and_bootnext_path(sys_root: Path) -> None:
    _write_secureboot_efivar(sys_root, enabled=True)
    (sys_root / "sys" / "class" / "tpm" / "tpm0" / "tpm_version_major").write_text("2", encoding="utf-8")

    def which(name: str) -> Optional[str]:
        return {"efibootmgr": "/usr/sbin/efibootmgr", "lsblk": "/usr/bin/lsblk"}.get(name)

    def run(cmd, **kw):
        if cmd[0] == "/usr/sbin/efibootmgr":
            return FakeProc(EFIBOOTMGR_SAMPLE)
        if cmd[0] == "/usr/bin/lsblk":
            return FakeProc(json.dumps({"blockdevices": [
                {"path": "/dev/sda1", "partuuid": "AAAAAAAA-0000-0000-0000-000000000001", "fstype": "BitLocker"},
                {"path": "/dev/sda4", "partuuid": "DDDDDDDD-0000-0000-0000-000000000004", "fstype": "ntfs"},
            ]}))
        return FakeProc("", 1)

    st = ldualboot.status(run=run, which=which, root=str(sys_root))
    assert set(st) == {"firmware", "secure_boot", "tpm", "windows_entries", "grub_windows_entries",
                       "can_reboot_to_windows", "method", "why", "lindos_kernel_signed", "bitlocker_hint"}
    assert st["firmware"] == "uefi" and st["secure_boot"] == "enabled" and st["tpm"] == 2
    assert st["can_reboot_to_windows"] is True and st["method"] == "bootnext" and st["why"] == ""
    # only the *active* Windows entry is offered (0003 is a stale/inactive Windows entry — status()
    # only lists what BootNext could actually use right now; parse_efibootmgr/windows_entries above
    # already cover seeing every Windows-loader entry regardless of active state).
    assert st["windows_entries"] == [
        {"num": "0000", "label": "Windows Boot Manager", "partuuid": "AAAAAAAA-0000-0000-0000-000000000001",
         "disk": "/dev/sda1"},
    ]
    assert st["bitlocker_hint"] is True    # the active entry's disk (sda1) reports FSTYPE=BitLocker
    assert st["lindos_kernel_signed"] is None
    assert json.dumps(st)   # the CLI's --json output must serialise cleanly


def test_status_falls_back_to_grub_reboot(sys_root: Path) -> None:
    (sys_root / "boot" / "grub" / "grub.cfg").write_text(GRUB_CFG_SAMPLE, encoding="utf-8")

    def which(name: str) -> Optional[str]:
        return None   # no efibootmgr, no lsblk

    def run(cmd, **kw):
        if cmd[0] == "grub-probe":
            return FakeProc("ext4" if cmd[1] == "--target=fs" else "none")
        return FakeProc("", 1)

    st = ldualboot.status(run=run, which=which, root=str(sys_root))
    assert st["method"] == "grub-reboot" and st["can_reboot_to_windows"] is True
    assert st["grub_windows_entries"][0]["id"] == "osprober-efi-ABCD-1234"


def test_status_grub_reboot_refused_when_grubenv_unwritable(sys_root: Path) -> None:
    (sys_root / "boot" / "grub" / "grub.cfg").write_text(GRUB_CFG_SAMPLE, encoding="utf-8")

    def run(cmd, **kw):
        if cmd[0] == "grub-probe":
            return FakeProc("btrfs" if cmd[1] == "--target=fs" else "none")
        return FakeProc("", 1)

    st = ldualboot.status(run=run, which=lambda n: None, root=str(sys_root))
    assert st["method"] is None and st["can_reboot_to_windows"] is False
    assert "btrfs" in st["why"] or "LVM" in st["why"] or "mdraid" in st["why"]


def test_status_nothing_found(sys_root: Path) -> None:
    st = ldualboot.status(run=lambda *a, **k: FakeProc("", 1), which=lambda n: None, root=str(sys_root))
    assert st["method"] is None and st["can_reboot_to_windows"] is False and st["why"]
    assert st["windows_entries"] == [] and st["grub_windows_entries"] == []


def test_status_lindos_kernel_signed_when_present(sys_root: Path) -> None:
    # real wire contract (packages/lindos-kernel/.../lindos_kernel/secureboot.py:
    # status()'s "any_lindos_kernel_signed" key, also asserted by
    # packages/lindos-kernel/tests/test_secureboot.py) -- NOT a "signed" key.
    def which(name: str) -> Optional[str]:
        return "/usr/bin/lindos-kernel" if name == "lindos-kernel" else None

    def run(cmd, **kw):
        if cmd[0] == "/usr/bin/lindos-kernel":
            return FakeProc(json.dumps({"any_lindos_kernel_signed": True}))
        return FakeProc("", 1)

    st = ldualboot.status(run=run, which=which, root=str(sys_root))
    assert st["lindos_kernel_signed"] is True


def test_status_lindos_kernel_signed_ignores_wrong_key(sys_root: Path) -> None:
    """Regression guard: a payload carrying only the old/wrong "signed" key (no
    "any_lindos_kernel_signed") must read back as unknown (None), never True -- so an accidental
    revert to the wrong key is caught instead of silently always reporting "unknown"."""
    def which(name: str) -> Optional[str]:
        return "/usr/bin/lindos-kernel" if name == "lindos-kernel" else None

    def run(cmd, **kw):
        if cmd[0] == "/usr/bin/lindos-kernel":
            return FakeProc(json.dumps({"signed": True}))
        return FakeProc("", 1)

    st = ldualboot.status(run=run, which=which, root=str(sys_root))
    assert st["lindos_kernel_signed"] is None


# =================================================================================================
# lindos.dualboot — reboot_payload()
# =================================================================================================
def test_reboot_payload_bootnext_default_and_explicit_entry() -> None:
    st = {"method": "bootnext", "windows_entries": [
        {"num": "0000", "label": "Windows Boot Manager"}, {"num": "0003", "label": "Windows Boot Manager"}]}
    assert ldualboot.reboot_payload(st) == {"method": "bootnext", "entry": "0000", "reboot": True}
    assert ldualboot.reboot_payload(st, entry="0003") == {"method": "bootnext", "entry": "0003", "reboot": True}
    assert ldualboot.reboot_payload(st, entry="0003")["entry"] == "0003"
    with pytest.raises(ValueError):
        ldualboot.reboot_payload(st, entry="ffff")


def test_reboot_payload_grub_reboot() -> None:
    st = {"method": "grub-reboot", "grub_windows_entries": [{"id": "osprober-efi-ABCD-1234", "title": "x"}]}
    assert ldualboot.reboot_payload(st) == {"method": "grub-reboot", "menuentry": "osprober-efi-ABCD-1234",
                                            "reboot": True}
    with pytest.raises(ValueError):
        ldualboot.reboot_payload(st, entry="osprober-efi-nope")


def test_reboot_payload_raises_with_why_when_impossible() -> None:
    with pytest.raises(ValueError, match="no Windows install here"):
        ldualboot.reboot_payload({"method": None, "why": "no Windows install here"})
    with pytest.raises(ValueError):
        ldualboot.reboot_payload({"method": "bootnext", "windows_entries": []})


# =================================================================================================
# lindos-dualboot CLI (subprocess, LINDOS_HELPER_DRYRUN=1 from core_env; real host has no
# efibootmgr/grub-probe, so `status` deterministically finds nothing to restart into)
# =================================================================================================
def test_cli_status_json_shape(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "status", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["can_reboot_to_windows"] is False and data["why"]
    assert data["windows_entries"] == []


def test_cli_status_text(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "status")
    assert proc.returncode == 0, proc.stderr
    assert "firmware:" in proc.stdout and "can restart into Windows: no" in proc.stdout


def test_cli_reboot_to_windows_nothing_found(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "reboot-to-windows", "--yes")
    assert proc.returncode == 3
    assert proc.stderr.strip()


def test_cli_firmware_setup_dry_run(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "firmware-setup", "--yes")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "BitLocker" in proc.stdout and "mbr2gpt" in proc.stdout
    assert "firmware-setup" in proc.stdout or "[dry-run]" in proc.stdout


def test_cli_firmware_setup_declined_without_yes(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "firmware-setup", input="n\n")
    assert proc.returncode == 0
    assert "cancelled" in proc.stdout


def test_cli_shortcut_writes_desktop_file(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot", "shortcut")
    assert proc.returncode == 0, proc.stderr
    dest = core_env["home"] / ".local" / "share" / "applications" / "lindos-restart-windows.desktop"
    assert dest.is_file()
    content = dest.read_text(encoding="utf-8")
    assert "Exec=lindos-dualboot reboot-to-windows --yes" in content
    assert "Type=Application" in content and "\r" not in content


def test_cli_help_with_no_command(core_env, run_cli) -> None:
    proc = run_cli("lindos-dualboot")
    assert proc.returncode == 2


# =================================================================================================
# lindos-helper: reboot-to-windows / firmware-setup / import-wifi / set-binfmt — dry-run command
# lists through the real CLI (subprocess, LINDOS_HELPER_DRYRUN=1)
# =================================================================================================
def _unquoted(text: str) -> str:
    return text.replace("'", "")


def test_helper_dry_run_reboot_to_windows_bootnext(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "reboot-to-windows", json.dumps({"method": "bootnext", "entry": "0001"}))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "would re-run efibootmgr" in out
    assert "efibootmgr --bootnext 0001" in out
    assert "systemctl reboot" in out


def test_helper_dry_run_reboot_to_windows_no_reboot_flag(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "reboot-to-windows",
                   json.dumps({"method": "bootnext", "entry": "0001", "reboot": False}))
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "efibootmgr --bootnext 0001" in out
    assert "systemctl reboot" not in out
    assert "not rebooting" in out


def test_helper_dry_run_reboot_to_windows_grub(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "reboot-to-windows",
                   json.dumps({"method": "grub-reboot", "menuentry": "osprober-efi-ABCD-1234"}))
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "grub-reboot osprober-efi-ABCD-1234" in out
    assert "systemctl reboot" in out


def test_helper_dry_run_firmware_setup(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "firmware-setup", json.dumps({"confirm": True}))
    assert proc.returncode == 0, proc.stderr
    assert "systemctl reboot --firmware-setup" in _unquoted(proc.stdout)


def test_helper_dry_run_set_binfmt_enable_and_disable(core_env, run_cli) -> None:
    on = run_cli("lindos-helper", "set-binfmt", json.dumps({"enabled": True}))
    assert on.returncode == 0, on.stderr
    out_on = _unquoted(on.stdout)
    assert "systemd-binfmt" in out_on and "lindos-pe.conf" in out_on
    assert "restart" not in out_on.lower()   # NEVER 'systemctl restart systemd-binfmt'

    off = run_cli("lindos-helper", "set-binfmt", json.dumps({"enabled": False}))
    assert off.returncode == 0, off.stderr
    out_off = _unquoted(off.stdout)
    assert "/dev/null" in out_off and "binfmt_misc/lindos-pe" in out_off
    assert "restart" not in out_off.lower()


def test_helper_dry_run_install_flatpaks_with_geforce_now_remote(core_env, run_cli) -> None:
    payload = {"flatpaks": ["com.nvidia.geforcenow"],
              "remote": {"name": "GeForceNOW",
                         "url": "https://international.download.nvidia.com/GFNLinux/flatpak/geforcenow.flatpakrepo"}}
    proc = run_cli("lindos-helper", "install-flatpaks", json.dumps(payload), env={"LINDOS_HELPER_ONLINE": "1", "LINDOS_FORCE_OFFLINE": "0"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "remote-add --if-not-exists --system GeForceNOW https://international.download.nvidia.com" in out
    assert "install -y --noninteractive --system -- GeForceNOW com.nvidia.geforcenow" in out


def test_helper_dry_run_import_wifi_via_stdin(core_env, run_cli) -> None:
    """Payload via stdin only ('-'): never on argv (a real pkexec call logs its argv)."""
    payload = {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "correcthorsebattery"}]}
    proc = run_cli("lindos-helper", "import-wifi", "-", input=json.dumps(payload))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = proc.stdout
    assert "lindos-" in out and ".nmconnection" in out
    assert "correcthorsebattery" not in out   # the psk itself is never echoed to the log/terminal
    assert "nmcli" in out or "would run" in out


def test_helper_rejects_bad_dualboot_payload(core_env, run_cli) -> None:
    assert run_cli("lindos-helper", "reboot-to-windows", '{"method":"bootnext","entry":"zzzz"}').returncode == 2
    assert run_cli("lindos-helper", "firmware-setup", "{}").returncode == 2
    assert run_cli("lindos-helper", "import-wifi", '{"networks":[]}').returncode == 2
    assert run_cli("lindos-helper", "set-binfmt", "{}").returncode == 2


# =================================================================================================
# lindos-helper internals: keyfile rendering, perms, and the root-side re-validation refusal
# (called in-process — these branches only run when the helper is NOT in dry-run mode, which the
# CLI can only reach as root; testing them through a real subprocess is therefore not possible)
# =================================================================================================
def test_wifi_safe_name_collision_resistant_and_hidden_fallback(helper_mod) -> None:
    a = helper_mod._wifi_safe_name("Home WiFi")
    b = helper_mod._wifi_safe_name("Home-WiFi")     # slugifies to the same ASCII text as (a)
    assert a != b                                    # hash suffix keeps them apart
    assert helper_mod._wifi_safe_name("   ").startswith("hidden-network-")


@pytest.mark.parametrize("net, expect_present, expect_absent", [
    ({"ssid": "HomeNet", "security": "open", "hidden": False, "agent_owned": False, "psk": None},
     ["ssid=HomeNet", "type=wifi", "autoconnect=true", "method=auto"],
     ["[wifi-security]", "hidden=true"]),
    ({"ssid": "HomeNet", "security": "wpa-psk", "hidden": True, "agent_owned": False, "psk": "correcthorsebattery"},
     ["key-mgmt=wpa-psk", "psk=correcthorsebattery", "hidden=true"], ["psk-flags=1"]),
    ({"ssid": "HomeNet", "security": "sae", "hidden": False, "agent_owned": True, "psk": None},
     ["key-mgmt=sae", "psk-flags=1"], ["psk="]),
])
def test_render_nmconnection(helper_mod, net: Dict[str, Any], expect_present: List[str], expect_absent: List[str]) -> None:
    content = helper_mod._render_nmconnection(net)
    assert content.startswith("[connection]\n")
    assert "uuid=" in content
    for token in expect_present:
        assert token in content, content
    for token in expect_absent:
        assert token not in content, content


def test_act_import_wifi_writes_root_only_perms(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file must be written 0600 (root:root is implicit — the helper only ever runs as root)."""
    ctx = helper_mod.Ctx(dry_run=False)
    captured: List[Dict[str, Any]] = []

    def fake_write_file(path, content, mode=0o644):
        captured.append({"path": path, "content": content, "mode": mode})
        return True

    monkeypatch.setattr(ctx, "write_file", fake_write_file)
    monkeypatch.setattr(ctx, "run", lambda cmd, **kw: (True, ""))
    code = helper_mod.act_import_wifi(ctx, {"networks": [
        {"ssid": "HomeNet", "security": "wpa-psk", "hidden": False, "agent_owned": False, "psk": "correcthorsebattery"},
    ]})
    assert code == helper_mod.EXIT_OK
    assert len(captured) == 1 and captured[0]["mode"] == 0o600
    assert captured[0]["path"].endswith(".nmconnection")
    assert "correcthorsebattery" in captured[0]["content"]


def test_act_import_wifi_dry_run_never_touches_disk(core_env, helper_mod) -> None:
    ctx = helper_mod.Ctx(dry_run=True)
    code = helper_mod.act_import_wifi(ctx, {"networks": [{"ssid": "HomeNet", "security": "open",
                                                           "hidden": False, "agent_owned": False, "psk": None}]})
    assert code == helper_mod.EXIT_OK
    conn_dir = Path(helper_mod.lpaths.resolve(helper_mod.NM_SYSTEM_CONNECTIONS_DIR))
    assert not conn_dir.exists()   # dry-run: nothing written


def test_act_reboot_to_windows_refuses_entry_that_is_not_windows(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/efibootmgr" if name == "efibootmgr" else None)
    fake_text = ("Boot0001  Windows Boot Manager\tHD(2,GPT,DEAD-0002,0x800,0x32000)"
                "/File(\\EFI\\evil\\notwindows.efi)\n")
    monkeypatch.setattr(helper_mod, "_run_capture", lambda c, cmd, timeout=30: (True, fake_text))
    run_calls: List[List[str]] = []

    def fake_run(cmd, **kw):
        run_calls.append(list(cmd))
        return True, ""

    monkeypatch.setattr(ctx, "run", fake_run)
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "bootnext", "entry": "0001", "reboot": True})
    assert code == helper_mod.EXIT_ERROR
    assert run_calls == []   # refused before ever touching efibootmgr --bootnext or systemctl reboot


def test_act_reboot_to_windows_refuses_inactive_entry(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/efibootmgr" if name == "efibootmgr" else None)
    fake_text = ("Boot0003  Windows Boot Manager\tHD(4,GPT,DDDD-0004,0x800,0x32000)"
                "/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n")
    monkeypatch.setattr(helper_mod, "_run_capture", lambda c, cmd, timeout=30: (True, fake_text))
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "bootnext", "entry": "0003", "reboot": True})
    assert code == helper_mod.EXIT_ERROR


def test_act_reboot_to_windows_accepts_real_active_entry_and_reboots(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/efibootmgr" if name == "efibootmgr" else None)
    fake_text = ("Boot0000* Windows Boot Manager\tHD(1,GPT,AAAA-0001,0x800,0x32000)"
                "/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n")
    monkeypatch.setattr(helper_mod, "_run_capture", lambda c, cmd, timeout=30: (True, fake_text))
    run_calls: List[List[str]] = []

    def fake_run(cmd, **kw):
        run_calls.append(list(cmd))
        return True, ""

    monkeypatch.setattr(ctx, "run", fake_run)
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "bootnext", "entry": "0000", "reboot": True})
    assert code == helper_mod.EXIT_OK
    assert run_calls[0] == ["/usr/sbin/efibootmgr", "--bootnext", "0000"]
    assert run_calls[-1] == ["systemctl", "reboot"]


def test_act_reboot_to_windows_grub_refuses_non_windows_menuentry(core_env, helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    grub_cfg = Path(helper_mod.lpaths.resolve(helper_mod.GRUB_CFG_PATH))
    grub_cfg.parent.mkdir(parents=True, exist_ok=True)
    grub_cfg.write_text(GRUB_CFG_SAMPLE, encoding="utf-8")
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/grub-reboot" if name == "grub-reboot" else None)
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "grub-reboot", "menuentry": "gnulinux-simple-abcd",
                                                  "reboot": True})
    assert code == helper_mod.EXIT_ERROR


def test_act_reboot_to_windows_grub_refuses_when_grubenv_unwritable(core_env, helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    grub_cfg = Path(helper_mod.lpaths.resolve(helper_mod.GRUB_CFG_PATH))
    grub_cfg.parent.mkdir(parents=True, exist_ok=True)
    grub_cfg.write_text(GRUB_CFG_SAMPLE, encoding="utf-8")
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/grub-reboot" if name == "grub-reboot" else None)
    monkeypatch.setattr(helper_mod.ldualboot, "grubenv_writable", lambda *a, **k: False)
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "grub-reboot", "menuentry": "osprober-efi-ABCD-1234",
                                                  "reboot": True})
    assert code == helper_mod.EXIT_ERROR


def test_act_reboot_to_windows_grub_accepts_real_windows_entry(core_env, helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    grub_cfg = Path(helper_mod.lpaths.resolve(helper_mod.GRUB_CFG_PATH))
    grub_cfg.parent.mkdir(parents=True, exist_ok=True)
    grub_cfg.write_text(GRUB_CFG_SAMPLE, encoding="utf-8")
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: "/usr/sbin/grub-reboot" if name == "grub-reboot" else None)
    monkeypatch.setattr(helper_mod.ldualboot, "grubenv_writable", lambda *a, **k: True)
    run_calls: List[List[str]] = []

    def fake_run(cmd, **kw):
        run_calls.append(list(cmd))
        return True, ""

    monkeypatch.setattr(ctx, "run", fake_run)
    code = helper_mod.act_reboot_to_windows(ctx, {"method": "grub-reboot", "menuentry": "osprober-efi-ABCD-1234",
                                                  "reboot": False})
    assert code == helper_mod.EXIT_OK
    assert run_calls == [["/usr/sbin/grub-reboot", "osprober-efi-ABCD-1234"]]   # no reboot: false


def test_act_set_binfmt_dry_run_never_touches_filesystem(helper_mod, core_env) -> None:
    ctx = helper_mod.Ctx(dry_run=True)
    assert helper_mod.act_set_binfmt(ctx, {"enabled": True}) == helper_mod.EXIT_OK
    assert helper_mod.act_set_binfmt(ctx, {"enabled": False}) == helper_mod.EXIT_OK
    mask = Path(helper_mod.lpaths.resolve(helper_mod.BINFMT_MASK_PATH))
    assert not mask.exists()


# =================================================================================================
# honesty: no VM/anti-cheat evasion tokens on the reboot-to-Windows surface (SPEC-WINDOWS §27.1,
# §33) -- mirrors the FORBIDDEN_TOKENS scans of lindos-transfer/lindos-compat/lindos-gaming,
# extended to lindos-core's dualboot.py / lindos-dualboot / lindos-helper (the one place in the
# whole Addendum-W surface that actually reboots the machine into Windows for anti-cheat titles).
# =================================================================================================
DUALBOOT_PY = ROOT / "usr" / "lib" / "python3" / "dist-packages" / "lindos" / "dualboot.py"
HELPER_PY = ROOT / "usr" / "lib" / "python3" / "dist-packages" / "lindos" / "helper.py"
DUALBOOT_BIN = BIN / "lindos-dualboot"

#: same vocabulary as packages/lindos-transfer/tests/test_transfer_honesty.py:19 (which mirrors
#: lindos-vm/test_no_spoof.py and lindos-gaming/test_play_anywhere.py).
FORBIDDEN_TOKENS = ("spoof", "hwid", "attestation", "smbios", "vm-detect", "vmdetect", "kvm=off",
                   "hv-vendor-id", "acpitable", "hide_hypervisor", "hidden_hypervisor",
                   "fake_tpm", "user-agent spoof", "useragent spoof")

DUALBOOT_SURFACE = [DUALBOOT_PY, HELPER_PY, DUALBOOT_BIN, HELPER]

#: dualboot.py and lindos-dualboot each carry a couple of already-audited *honest denial*
#: sentences ("Lindos does not try to fake / spoof / attest ...") explaining why no kernel or VM
#: can satisfy hardware attestation, so it restarts into real Windows instead -- these exact
#: sentences are allowed; any OTHER occurrence of a forbidden token in these files still fails
#: the test, so a future edit that actually adds evasion code (not just repeating this sentence)
#: is caught.
_ALREADY_AUDITED_HONESTY_LINES: Dict[Path, tuple] = {
    DUALBOOT_PY: (
        "TPM/Secure-Boot attestation) that no Linux kernel or VM can ever",
    ),
    DUALBOOT_BIN: (
        "TPM/Secure-Boot hardware attestation). Lindos does not try to fake",
        "it never emulates, spoofs or hides from it.",
        "not an emulator or spoofer.",
    ),
}


@pytest.mark.parametrize("path", DUALBOOT_SURFACE, ids=lambda p: p.name)
def test_no_evasion_tokens_in_dualboot_surface(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    for marker in _ALREADY_AUDITED_HONESTY_LINES.get(path, ()):
        text = text.replace(marker, "")
    lowered = text.lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in lowered, f"HONESTY VIOLATION: {tok!r} found in {path.name}"
