"""lindos_compat.formats.detect: synthetic PE/NE/LE/MZ headers, containers and text formats.

Every fixture is generated here (SPEC-WINDOWS §33): no real Windows or Store binaries.
Malformed/truncated input must never make detect() raise.
"""
from __future__ import annotations

import io
import json
import os
import random
import struct
import sys
import types
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import pytest

import lindos_compat
from lindos_compat import formats

APPHOST_SIGNATURE = bytes((
    0x8B, 0x12, 0x02, 0xB9, 0x6A, 0x61, 0x20, 0x38, 0x72, 0x7B, 0x93, 0x02, 0x14, 0xD7, 0xA0, 0x32,
    0x13, 0xF5, 0xB9, 0xE6, 0xEF, 0xAE, 0x33, 0x18, 0xEE, 0x3B, 0x2D, 0xCE, 0x24, 0xB3, 0x6A, 0xAE,
))


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def make_pe(*, machine: int = 0x14C, chars: int = 0x0102, magic: int = 0x10B, subsystem: int = 2,
            clr: Optional[Tuple[int, int]] = None, nrva: int = 16, e_lfanew: int = 0x80, size: int = 0x400,
            sections: Sequence[Tuple[int, int, int, int]] = (), opt_size: Optional[int] = None) -> bytearray:
    """A minimal PE image: DOS header, PE signature, COFF + optional header, optional sections."""
    if opt_size is None:
        opt_size = 224 if magic == 0x10B else 240
    need = e_lfanew + 24 + max(opt_size, 240) + 40 * len(sections) + 16
    buf = bytearray(max(size, need))
    buf[0:2] = b"MZ"
    struct.pack_into("<H", buf, 0x18, 0x40)
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 4] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buf, e_lfanew + 4, machine, len(sections), 0, 0, 0, opt_size, chars)
    opt = e_lfanew + 24
    struct.pack_into("<H", buf, opt, magic)
    struct.pack_into("<H", buf, opt + 68, subsystem)
    nrva_off, dd = (92, 96) if magic == 0x10B else (108, 112)
    struct.pack_into("<I", buf, opt + nrva_off, nrva)
    if clr is not None:
        struct.pack_into("<II", buf, opt + dd + 14 * 8, clr[0], clr[1])
    base = opt + opt_size
    for i, (vaddr, vsize, rawptr, rawsize) in enumerate(sections):
        struct.pack_into("<8sIIII", buf, base + i * 40, b".text", vsize, vaddr, rawsize, rawptr)
    return buf


def make_dotnet_pe(version: bytes = b"v4.0.30319", *, dll: bool = False, flags: int = 1) -> bytearray:
    """PE32 with a CLR header and a metadata root ("BSJB" + version) mapped through one section."""
    chars = 0x2102 if dll else 0x0102
    buf = make_pe(chars=chars, clr=(0x2000, 0x48), sections=[(0x2000, 0x200, 0x400, 0x200)], size=0x600)
    struct.pack_into("<IHHIII", buf, 0x400, 0x48, 2, 5, 0x2050, 0x100, flags)
    md = 0x450
    vlen = (len(version) + 1 + 3) // 4 * 4
    struct.pack_into("<IHHII", buf, md, 0x424A5342, 1, 1, 0, vlen)
    buf[md + 16:md + 16 + len(version)] = version
    return buf


def make_ne(*, exetyp: int = 2, flags: int = 0x0302, e_lfanew: int = 0x80) -> bytearray:
    buf = bytearray(0x200)
    buf[0:2] = b"MZ"
    struct.pack_into("<H", buf, 0x18, 0x40)
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 2] = b"NE"
    struct.pack_into("<H", buf, e_lfanew + 0x0C, flags)
    buf[e_lfanew + 0x36] = exetyp
    return buf


def make_le(*, sig: bytes = b"LE", ostype: int = 1, flags: int = 0x0, e_lfanew: int = 0x80) -> bytearray:
    buf = bytearray(0x200)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 4] = sig + b"\x00\x00"
    struct.pack_into("<H", buf, e_lfanew + 0x0A, ostype)
    struct.pack_into("<I", buf, e_lfanew + 0x10, flags)
    return buf


def make_dos(size: int = 0x200) -> bytearray:
    buf = bytearray(size)
    buf[0:2] = b"MZ"
    struct.pack_into("<HH", buf, 2, size % 512, (size + 511) // 512)
    struct.pack_into("<H", buf, 0x08, 2)
    struct.pack_into("<I", buf, 0x3C, 0xB4090E1F)  # code bytes, not a real e_lfanew
    return buf


def make_ole(clsid: Optional[bytes], *, shift: int = 9, first_dir: int = 0) -> bytes:
    sector = 1 << shift
    buf = bytearray(sector * (first_dir + 2))
    buf[0:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<H", buf, 0x1E, shift)
    struct.pack_into("<I", buf, 0x30, first_dir)
    entry = (first_dir + 1) * sector
    name = "Root Entry".encode("utf-16-le")
    buf[entry:entry + len(name)] = name
    buf[entry + 0x42] = 5
    if clsid is not None:
        buf[entry + 0x50:entry + 0x60] = clsid
    return bytes(buf)


MSI_CLSID = bytes.fromhex("84100c0000000000c000000000000046")
MSP_CLSID = bytes.fromhex("86100c0000000000c000000000000046")
MST_CLSID = bytes.fromhex("82100c0000000000c000000000000046")


def make_iso(idents: Iterable[bytes]) -> bytes:
    buf = bytearray(0x8000)
    for ident in idents:
        buf += b"\x00" + ident + b"\x01" + b"\x00" * (0x800 - 7)
    return bytes(buf)


def make_zip(names: Iterable[str]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for n in names:
            zf.writestr(n, b"<x/>")
    return bio.getvalue()


def make_pif(program: str = "KEEN.EXE") -> bytes:
    buf = bytearray(0x171 + 0x200)
    buf[0x24:0x24 + len(program)] = program.encode("ascii")
    buf[0x171:0x171 + 15] = b"MICROSOFT PIFEX"
    return bytes(buf)


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(data))
    return p


def det_of(tmp: Path, name: str, data: bytes) -> formats.Detection:
    return formats.detect(write(tmp, name, data))


@pytest.fixture()
def no_msix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the zip fallback (W-B's msix module is absent)."""
    monkeypatch.delattr(lindos_compat, "msix", raising=False)
    monkeypatch.setitem(sys.modules, "lindos_compat.msix", None)


def install_fake_msix(monkeypatch: pytest.MonkeyPatch, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType("lindos_compat.msix")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, "lindos_compat.msix", mod)
    monkeypatch.setattr(lindos_compat, "msix", mod, raising=False)
    return mod


# --------------------------------------------------------------------------- #
# PE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("machine,magic,subsystem,reason", [
    (0x14C, 0x10B, 2, "PE32 x86 GUI"),
    (0x14C, 0x10B, 3, "PE32 x86 console"),
    (0x8664, 0x20B, 2, "PE32+ x64 GUI"),
    (0x8664, 0x20B, 3, "PE32+ x64 console"),
])
def test_pe_exe(tmp_path: Path, machine: int, magic: int, subsystem: int, reason: str):
    d = det_of(tmp_path, "app.exe", make_pe(machine=machine, magic=magic, subsystem=subsystem))
    assert d.format.id == "exe" and d.reason == reason
    assert d.details["machine"] == machine and d.details["subsystem"] == subsystem
    assert d.details["bits"] == (64 if magic == 0x20B else 32)
    assert d.details["is_dll"] is False and d.details["clr"] is False and d.details["apphost"] is False
    json.dumps(d.as_dict())


def test_pe_subsystem_unknown_still_runs(tmp_path: Path):
    assert det_of(tmp_path, "a.exe", make_pe(subsystem=0)).format.id == "exe"


def test_pe_dll_cpl_scr(tmp_path: Path):
    dll = det_of(tmp_path, "lib.dll", make_pe(chars=0x2102))
    assert dll.format.id == "dll" and dll.details["is_dll"] and "DLL" in dll.reason
    exe_named_dll = det_of(tmp_path, "odd.exe", make_pe(chars=0x2102))
    assert exe_named_dll.format.id == "dll"  # content wins over the suffix
    cpl = det_of(tmp_path, "desk.cpl", make_pe(machine=0x8664, magic=0x20B, chars=0x2102))
    assert cpl.format.id == "cpl"
    arm_cpl = det_of(tmp_path, "arm.cpl", make_pe(machine=0xAA64, magic=0x20B, chars=0x2102))
    assert arm_cpl.format.id == "dll"
    scr = det_of(tmp_path, "Bubbles.SCR", make_pe())
    assert scr.format.id == "scr"
    assert det_of(tmp_path, "x.ocx", make_pe(chars=0x2102)).format.id == "dll"


@pytest.mark.parametrize("machine,name", [(0xAA64, "arm64"), (0xA641, "arm64ec"), (0xA64E, "arm64x"),
                                          (0x01C4, "armnt"), (0x01C2, "thumb"), (0x01C0, "arm")])
def test_pe_arm(tmp_path: Path, machine: int, name: str):
    d = det_of(tmp_path, "arm.exe", make_pe(machine=machine, magic=0x20B))
    assert d.format.id == "arm-exe" and d.details["machine_name"] == name
    arm_dll = det_of(tmp_path, "arm.dll", make_pe(machine=machine, magic=0x20B, chars=0x2102))
    assert arm_dll.format.id == "dll"


@pytest.mark.parametrize("machine,cpu", [(0x0200, "Itanium"), (0x0166, "MIPS"), (0x0184, "Alpha"),
                                         (0x01F0, "PowerPC"), (0x5064, "RISC-V"), (0x0000, "unknown processor"),
                                         (0x1234, "0x1234")])
def test_pe_other_cpus_are_explained(tmp_path: Path, machine: int, cpu: str):
    d = det_of(tmp_path, "old.exe", make_pe(machine=machine))
    assert d.format is formats.UNKNOWN_FORMAT
    assert cpu in d.reason and "x64" in d.details["hint"]


@pytest.mark.parametrize("subsystem", [10, 11, 12, 13])
def test_pe_efi(tmp_path: Path, subsystem: int):
    d = det_of(tmp_path, "bootx64.efi", make_pe(machine=0x8664, magic=0x20B, subsystem=subsystem))
    assert d.format.id == "dll" and d.details["efi"] is True and "UEFI" in d.reason


def test_pe_native_driver_and_boot_app(tmp_path: Path):
    sys_ = det_of(tmp_path, "e1000.sys", make_pe(machine=0x8664, magic=0x20B, subsystem=1))
    assert sys_.format.id == "dll" and sys_.details["driver"] is True
    native_exe = det_of(tmp_path, "autochk.exe", make_pe(subsystem=1))
    assert native_exe.format.id == "dll"
    boot = det_of(tmp_path, "winload.exe", make_pe(machine=0x8664, magic=0x20B, subsystem=16))
    assert boot.format.id == "dll" and boot.details["boot_application"]


@pytest.mark.parametrize("subsystem,needle", [(5, "OS/2"), (7, "POSIX"), (9, "Windows CE"), (14, "Xbox")])
def test_pe_odd_subsystems(tmp_path: Path, subsystem: int, needle: str):
    d = det_of(tmp_path, "x.exe", make_pe(subsystem=subsystem))
    assert d.format is formats.UNKNOWN_FORMAT and needle in d.reason


def test_pe_dotnet_framework(tmp_path: Path):
    d = det_of(tmp_path, "tool.exe", make_dotnet_pe())
    assert d.format.id == "dotnet-exe" and d.details["clr"] is True
    assert d.details["clr_version"] == "v4.0.30319"
    assert d.reason.endswith("(.NET)")
    old = det_of(tmp_path, "old.exe", make_dotnet_pe(b"v2.0.50727", flags=3))
    assert old.details["clr_version"] == "v2.0.50727" and old.details["clr_32bit_required"] is True
    lib = det_of(tmp_path, "lib.dll", make_dotnet_pe(dll=True))
    assert lib.format.id == "dll" and "(.NET)" in lib.reason


def test_pe_clr_directory_needs_enough_rva_entries(tmp_path: Path):
    d = det_of(tmp_path, "a.exe", make_pe(clr=(0x2000, 0x48), nrva=14))
    assert d.format.id == "exe" and d.details["clr"] is False
    empty = det_of(tmp_path, "b.exe", make_pe(clr=(0x2000, 0)))
    assert empty.format.id == "exe"


def test_pe_clr_without_metadata_still_dotnet(tmp_path: Path):
    d = det_of(tmp_path, "c.exe", make_pe(clr=(0x9000, 0x48)))
    assert d.format.id == "dotnet-exe" and "clr_version" not in d.details


def test_apphost_runtimeconfig(tmp_path: Path):
    write(tmp_path, "App.runtimeconfig.json", json.dumps({"runtimeOptions": {"tfm": "net8.0", "framework": {
        "name": "Microsoft.WindowsDesktop.App", "version": "8.0.0"}}}).encode("utf-8-sig"))
    d = det_of(tmp_path, "App.exe", make_pe(machine=0x8664, magic=0x20B))
    assert d.format.id == "dotnet-exe" and d.details["apphost"] is True
    assert d.details["dotnet_frameworks"] == ["Microsoft.WindowsDesktop.App 8.0.0"]
    assert d.details["dotnet_self_contained"] is False
    assert "apphost" in d.reason


def test_apphost_case_insensitive_and_self_contained(tmp_path: Path):
    write(tmp_path, "TOOL.RuntimeConfig.JSON", json.dumps({"runtimeOptions": {"includedFrameworks": [
        {"name": "Microsoft.NETCore.App", "version": "9.0.1"}]}}).encode())
    d = det_of(tmp_path, "tool.exe", make_pe(machine=0x8664, magic=0x20B))
    assert d.format.id == "dotnet-exe" and d.details["dotnet_self_contained"] is True


def test_apphost_bad_runtimeconfig_still_apphost(tmp_path: Path):
    write(tmp_path, "x.runtimeconfig.json", b"{not json")
    d = det_of(tmp_path, "x.exe", make_pe())
    assert d.format.id == "dotnet-exe" and d.details["apphost"] is True


def test_apphost_sibling_managed_dll(tmp_path: Path):
    write(tmp_path, "Game.dll", make_dotnet_pe(dll=True))
    assert det_of(tmp_path, "Game.exe", make_pe()).format.id == "dotnet-exe"
    write(tmp_path, "Native.dll", make_pe(chars=0x2102))
    assert det_of(tmp_path, "Native.exe", make_pe()).format.id == "exe"


@pytest.mark.parametrize("offset,single", [(0, False), (0x12345, True)])
def test_apphost_bundle_marker(tmp_path: Path, offset: int, single: bool):
    buf = make_pe(machine=0x8664, magic=0x20B, size=0x3000)
    pos = 0x2000
    struct.pack_into("<q", buf, pos, offset)
    buf[pos + 8:pos + 8 + 32] = APPHOST_SIGNATURE
    d = det_of(tmp_path, "single.exe", buf)
    assert d.format.id == "dotnet-exe" and d.details["dotnet_single_file"] is single


def test_apphost_only_checked_for_exe_suffix(tmp_path: Path):
    write(tmp_path, "saver.runtimeconfig.json", b"{}")
    assert det_of(tmp_path, "saver.scr", make_pe()).format.id == "scr"


def test_pe_header_beyond_head(tmp_path: Path):
    e = formats.HEAD_BYTES + 0x1000
    d = det_of(tmp_path, "far.exe", make_pe(machine=0x8664, magic=0x20B, e_lfanew=e, size=e + 0x400))
    assert d.format.id == "exe" and d.details["e_lfanew"] == e


def test_pe_damaged(tmp_path: Path):
    bad_magic = det_of(tmp_path, "a.exe", make_pe(magic=0x107))
    assert bad_magic.format is formats.UNKNOWN_FORMAT and "optional header" in bad_magic.reason
    buf = make_pe()
    cut = det_of(tmp_path, "b.exe", buf[:0x80 + 10])
    assert cut.format is formats.UNKNOWN_FORMAT and "cut off" in cut.reason
    no_opt = det_of(tmp_path, "c.exe", make_pe(opt_size=0))
    assert no_opt.format is formats.UNKNOWN_FORMAT


# --------------------------------------------------------------------------- #
# NE / LE / LX / DOS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exetyp", [2, 4])
def test_ne_windows(tmp_path: Path, exetyp: int):
    d = det_of(tmp_path, "sol.exe", make_ne(exetyp=exetyp))
    assert d.format.id == "win16-exe" and d.details["ne_exetyp"] == exetyp
    assert d.reason.startswith("NE Windows")


def test_ne_library(tmp_path: Path):
    d = det_of(tmp_path, "commdlg.dll", make_ne(flags=0x8301))
    assert d.format.id == "dll" and d.details["is_dll"] is True
    exe_suffix = det_of(tmp_path, "lib.exe", make_ne(flags=0x8000, exetyp=2))
    assert exe_suffix.format.id == "dll"


@pytest.mark.parametrize("exetyp,os_name", [(1, "OS/2"), (0, "unknown"), (3, "European MS-DOS 4"), (5, "BOSS"),
                                            (9, "type 9")])
def test_ne_other_targets_run_dos_part(tmp_path: Path, exetyp: int, os_name: str):
    d = det_of(tmp_path, "bound.exe", make_ne(exetyp=exetyp))
    assert d.format.id == "dos-exe" and d.details["ne_os"] == os_name
    assert "DOS part" in d.reason and d.details["partial_note"]


def test_ne_truncated(tmp_path: Path):
    buf = make_ne()[:0x80 + 0x20]
    d = det_of(tmp_path, "t.exe", buf)
    assert d.format.id == "dos-exe"


def test_le_lx(tmp_path: Path):
    prog = det_of(tmp_path, "doom.exe", make_le())
    assert prog.format.id == "dos-exe" and prog.details["new_exe"] == "LE"
    assert "DOS extender" in prog.reason and "VxD" in prog.details["partial_note"]
    lx = det_of(tmp_path, "pmode.exe", make_le(sig=b"LX"))
    assert lx.format.id == "dos-exe" and lx.details["new_exe"] == "LX"
    vxd = det_of(tmp_path, "vmm.vxd", make_le(ostype=4, flags=0x28000))
    assert vxd.format.id == "dll" and vxd.details["driver"] is True
    pdd = det_of(tmp_path, "x.sys", make_le(flags=0x20000))
    assert pdd.format.id == "dll"
    lib = det_of(tmp_path, "x.dll", make_le(sig=b"LX", flags=0x8000))
    assert lib.format.id == "dll" and lib.details["is_dll"] is True


def test_plain_dos(tmp_path: Path):
    d = det_of(tmp_path, "GAME.EXE", make_dos())
    assert d.format.id == "dos-exe" and d.reason == "MZ DOS program"
    zm = bytearray(make_dos())
    zm[0:2] = b"ZM"
    assert det_of(tmp_path, "old.exe", zm).format.id == "dos-exe"
    small = det_of(tmp_path, "tiny.exe", b"MZ" + b"\x00" * 40)
    assert small.format.id == "dos-exe"
    assert det_of(tmp_path, "cmd.com", make_dos()).format.id == "dos-exe"  # MZ .com is an .exe inside


@pytest.mark.parametrize("e_lfanew", [0, 2, 0xFFFFFFFF, 0x7FFFFFF0, 0x1FC])
def test_mz_bogus_e_lfanew(tmp_path: Path, e_lfanew: int):
    buf = make_dos()
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    assert det_of(tmp_path, "x.exe", buf).format.id == "dos-exe"


def test_mz_too_short_is_damaged(tmp_path: Path):
    d = det_of(tmp_path, "cut.exe", b"MZ\x90\x00")
    assert d.format is formats.UNKNOWN_FORMAT and "cut off" in d.reason


# --------------------------------------------------------------------------- #
# .com / .pif
# --------------------------------------------------------------------------- #
def test_com_without_mz(tmp_path: Path):
    d = det_of(tmp_path, "HELLO.COM", b"\xb4\x09\xba\x09\x01\xcd\x21\xcd\x20Hi$")
    assert d.format.id == "dos-com" and "no MZ header" in d.reason
    big = det_of(tmp_path, "big.com", b"\x90" * (formats.MAX_COM_SIZE + 1))
    assert big.format is formats.UNKNOWN_FORMAT and "too big" in big.reason
    exact = det_of(tmp_path, "max.com", b"\x90" * formats.MAX_COM_SIZE)
    assert exact.format.id == "dos-com"


def test_pif(tmp_path: Path):
    d = det_of(tmp_path, "keen.pif", make_pif())
    assert d.format.id == "dos-com" and d.details["pif"]["program"] == "KEEN.EXE"
    raw = det_of(tmp_path, "raw.pif", b"\xb4\x4c\xcd\x21")
    assert raw.format.id == "dos-com" and "pif" not in raw.details
    mz = det_of(tmp_path, "trick.pif", make_pe())
    assert mz.format.id == "exe"


# --------------------------------------------------------------------------- #
# OLE (msi / msp / mst)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clsid,name,fid", [
    (MSI_CLSID, "app.msi", "msi"), (MSP_CLSID, "fix.msp", "msp"), (MST_CLSID, "t.mst", "mst"),
    (MSP_CLSID, "misnamed.msi", "msp"), (MST_CLSID, "noext", "mst"), (MSI_CLSID, "setup.bin", "msi"),
])
def test_ole_by_clsid(tmp_path: Path, clsid: bytes, name: str, fid: str):
    d = det_of(tmp_path, name, make_ole(clsid))
    assert d.format.id == fid and d.details["container"] == "ole"


def test_ole_4k_sectors_and_suffix_fallback(tmp_path: Path):
    assert det_of(tmp_path, "big.msi", make_ole(MSI_CLSID, shift=12, first_dir=1)).format.id == "msi"
    assert det_of(tmp_path, "p.msp", make_ole(None)).format.id == "msp"
    assert det_of(tmp_path, "p.MST", make_ole(b"\x01" * 16)).format.id == "mst"
    doc = det_of(tmp_path, "letter.doc", make_ole(None))
    assert doc.format is formats.UNKNOWN_FORMAT and "Office" in doc.reason


def test_ole_bogus_directory(tmp_path: Path):
    for shift, first in ((7, 0), (9, 0xFFFFFFFE), (9, 0x7FFFFFF)):
        data = bytearray(make_ole(MSI_CLSID))
        struct.pack_into("<H", data, 0x1E, shift)
        struct.pack_into("<I", data, 0x30, first)
        d = det_of(tmp_path, "x.msi", data)
        assert d.format.id == "msi"  # suffix fallback, no crash


# --------------------------------------------------------------------------- #
# ZIP / MSIX family
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("names,fid", [
    (["AppxManifest.xml", "AppxBlockMap.xml", "App.exe"], "msix"),
    (["AppxMetadata/AppxBundleManifest.xml", "App_x64.msix"], "msix-bundle"),
    (["App_1.0.0.0_x64.msixbundle", "App.appxsym"], "msix-upload"),
    (["sub/App.appx"], "msix-upload"),
])
def test_zip_msix_fallback(tmp_path: Path, no_msix: None, names: Sequence[str], fid: str):
    d = det_of(tmp_path, "pkg.appx", make_zip(names))
    assert d.format.id == fid and d.details["container"] == "zip"


def test_zip_fallback_edge_cases(tmp_path: Path, no_msix: None):
    both = det_of(tmp_path, "both.msix", make_zip(["AppxManifest.xml", "AppxMetadata/AppxBundleManifest.xml"]))
    assert both.format.id == "msix" and both.details["msix_kind"] == "unknown"
    plain = det_of(tmp_path, "photos.zip", make_zip(["a.jpg"]))
    assert plain.format is formats.UNKNOWN_FORMAT
    exe = det_of(tmp_path, "fake.exe", make_zip(["a.txt"]))
    assert exe.format is formats.UNKNOWN_FORMAT and ".exe" in exe.reason
    bundle_named_msix = det_of(tmp_path, "Rufus.appx", make_zip(["AppxMetadata/AppxBundleManifest.xml"]))
    assert bundle_named_msix.format.id == "msix-bundle"
    broken = det_of(tmp_path, "broken.msix", b"PK\x03\x04" + b"\x00" * 100)
    assert broken.format.id == "msix"


def test_zip_uses_msix_classify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = []

    def classify(path: Path) -> str:
        seen.append(Path(path).name)
        return "bundle"

    install_fake_msix(monkeypatch, classify=classify)
    d = det_of(tmp_path, "x.msix", make_zip(["AppxManifest.xml"]))
    assert d.format.id == "msix-bundle" and seen == ["x.msix"]


def test_zip_msix_classify_error_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def classify(path: Path) -> str:
        raise RuntimeError("hostile zip")

    install_fake_msix(monkeypatch, classify=classify)
    d = det_of(tmp_path, "x.msix", make_zip(["AppxManifest.xml"]))
    assert d.format.id == "msix"


def test_zip_entry_count_guard(tmp_path: Path, no_msix: None, monkeypatch: pytest.MonkeyPatch):
    data = bytearray(make_zip(["AppxManifest.xml"]))
    p = write(tmp_path, "many.appx", data)
    assert formats._zip_entry_count(p) == 1
    monkeypatch.setattr(formats, "_MAX_ZIP_ENTRIES", 0)
    d = formats.detect(p)
    assert d.details["msix_kind"] == "unknown" and d.format.id == "msix"
    assert formats._zip_entry_count(write(tmp_path, "noeocd.zip", b"PK\x03\x04garbage")) is None


def test_zip64_entry_count(tmp_path: Path):
    body = b"PK\x03\x04" + b"\x00" * 26
    z64_off = len(body)
    z64 = b"PK\x06\x06" + struct.pack("<QHHIIQQQQ", 44, 45, 45, 0, 0, 7, 300000, 0, 0)
    loc = b"PK\x06\x07" + struct.pack("<IQI", 0, z64_off, 1)
    eocd = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    p = write(tmp_path, "z64.zip", body + z64 + loc + eocd)
    assert formats._zip_entry_count(p) == 300000


@pytest.mark.parametrize("magic", [b"EXPH", b"EXSH", b"EXBH"])
def test_encrypted_store_packages(tmp_path: Path, magic: bytes):
    d = det_of(tmp_path, "Vendor.App_1.2.3.0_x64__8wekyb3d8bbwe.eappxbundle", magic + os.urandom(200))
    assert d.format.id == "msix-encrypted" and d.details["magic"] == magic.decode()
    assert det_of(tmp_path, "misnamed.msix", magic + b"\x00" * 64).format.id == "msix-encrypted"


# --------------------------------------------------------------------------- #
# CAB / ISO / lnk
# --------------------------------------------------------------------------- #
def test_cab_msu_installshield(tmp_path: Path):
    cab = b"MSCF\x00\x00\x00\x00" + b"\x00" * 64
    assert det_of(tmp_path, "drivers.cab", cab).format.id == "cab"
    assert det_of(tmp_path, "noext", cab).format.id == "cab"
    assert det_of(tmp_path, "windows10.0-kb5000000-x64.msu", cab).format.id == "msu"
    isc = det_of(tmp_path, "data1.cab", b"ISc(" + b"\x00" * 64)
    assert isc.format is formats.UNKNOWN_FORMAT and "setup.exe" in isc.details["hint"]
    fake_msu = det_of(tmp_path, "x.msu", b"not a cab")
    assert fake_msu.format.id == "msu"


@pytest.mark.parametrize("idents,name,udf,iso9660", [
    ([b"CD001", b"CD001"], "linux.iso", False, True),
    ([b"CD001", b"BEA01", b"NSR02", b"TEA01"], "Win11_24H2.iso", True, True),
    ([b"BEA01", b"NSR03", b"TEA01"], "udf-only.img", True, False),
    ([b"CD001"], "disc.bin", False, True),
])
def test_iso_udf(tmp_path: Path, idents, name: str, udf: bool, iso9660: bool):
    d = det_of(tmp_path, name, make_iso(idents))
    assert d.format.id == "iso"
    assert bool(d.details.get("udf")) is udf and bool(d.details.get("iso9660")) is iso9660


def test_iso_suffix_fallback(tmp_path: Path):
    raw = bytearray(0x200)
    raw[510:512] = b"\x55\xaa"
    assert det_of(tmp_path, "usb.img", raw).format.id == "iso"
    assert det_of(tmp_path, "odd.iso", b"\x00" * 100).format.id == "iso"
    bea_only = det_of(tmp_path, "x.bin", make_iso([b"BEA01", b"TEA01"]))
    assert bea_only.format is formats.UNKNOWN_FORMAT


def test_lnk_magic(tmp_path: Path, build_lnk):
    data = build_lnk(local_base_path="C:\\Program Files\\App\\app.exe")
    assert det_of(tmp_path, "App.lnk", data).format.id == "lnk"
    assert det_of(tmp_path, "App.lnk.bak", data).format.id == "lnk"
    assert det_of(tmp_path, "broken.lnk", b"garbage" * 20).format is formats.UNKNOWN_FORMAT


# --------------------------------------------------------------------------- #
# text formats
# --------------------------------------------------------------------------- #
def test_reg_by_content(tmp_path: Path):
    assert det_of(tmp_path, "tweak.txt", b"REGEDIT4\r\n\r\n[HKEY_CURRENT_USER\\x]\r\n").format.id == "reg"
    utf16 = b"\xff\xfe" + "Windows Registry Editor Version 5.00\r\n\r\n".encode("utf-16-le")
    d = det_of(tmp_path, "export", utf16)
    assert d.format.id == "reg" and d.reason == "registry file header"
    assert det_of(tmp_path, "bad.reg", b"[HKEY_CURRENT_USER\\x]\n").format.id == "reg"  # suffix; plan explains


def test_url_by_content(tmp_path: Path):
    assert det_of(tmp_path, "shortcut", b"[InternetShortcut]\r\nURL=https://x.org\r\n").format.id == "url"
    assert det_of(tmp_path, "x.url", b"[{000214A0-0000-0000-C000-000000000046}]\r\nProp3=19,2\r\n"
                                     b"[InternetShortcut]\r\nURL=https://x.org\r\n").format.id == "url"


@pytest.mark.parametrize("xml", [
    b'<?xml version="1.0" encoding="utf-8"?>\n<!-- c -->\n<AppInstaller xmlns="http://schemas.microsoft.com/appx/'
    b'appinstaller/2021" Version="1.0.0.0" Uri="https://x.org/a.appinstaller"><MainBundle/></AppInstaller>',
    b"\xef\xbb\xbf<AppInstaller Version='1.0.0.0'/>",
    b'<ai:AppInstaller xmlns:ai="http://schemas.microsoft.com/appx/appinstaller/2017/2"/>',
])
def test_appinstaller_by_content(tmp_path: Path, xml: bytes):
    assert det_of(tmp_path, "App.appinstaller", xml).format.id == "appinstaller"
    assert det_of(tmp_path, "download.xml", xml).format.id == "appinstaller"


def test_appinstaller_suffix_requires_xml(tmp_path: Path):
    d = det_of(tmp_path, "x.appinstaller", b"<html><body>404</body></html>")
    assert d.format is formats.UNKNOWN_FORMAT and "not a valid App Installer file" in d.reason


@pytest.mark.parametrize("name,fid", [
    ("run.bat", "bat"), ("RUN.CMD", "bat"), ("script.ps1", "ps1"), ("a.vbs", "vbs"), ("a.vbe", "vbs"),
    ("job.wsf", "vbs"), ("setup.inf", "inf"), ("app.application", "clickonce"), ("app.appref-ms", "clickonce"),
    ("game.msixvc", "msixvc"), ("x.url", "url"),
])
def test_suffix_fallbacks(tmp_path: Path, name: str, fid: str):
    d = det_of(tmp_path, name, b"some text content\r\n")
    assert d.format.id == fid and d.reason.startswith("suffix ")


@pytest.mark.parametrize("name", ["setup.exe", "x.dll", "a.scr", "b.cpl", "c.sys", "d.efi", "e.ocx",
                                  "p.msi", "q.msp", "r.mst", "s.lnk", "t.msix", "u.appxbundle", "v.eappx"])
def test_magic_required_suffixes(tmp_path: Path, name: str):
    d = det_of(tmp_path, name, b"<html>This is an error page, not a download</html>")
    assert d.format is formats.UNKNOWN_FORMAT
    assert "download it again" in d.details["hint"]


def test_elf_and_unknown(tmp_path: Path):
    elf = det_of(tmp_path, "tool.exe", b"\x7fELF\x02\x01\x01" + b"\x00" * 64)
    assert elf.format is formats.UNKNOWN_FORMAT and "Linux program" in elf.reason
    txt = det_of(tmp_path, "notes.txt", b"hello")
    assert txt.format is formats.UNKNOWN_FORMAT and ".txt" in txt.reason
    noext = det_of(tmp_path, "README", b"hello")
    assert "no suffix" in noext.reason


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #
def test_missing_empty_folder(tmp_path: Path):
    missing = formats.detect(tmp_path / "nope.exe")
    assert missing.format is formats.UNKNOWN_FORMAT and missing.reason == "file not found"
    assert missing.details["io_error"] is True
    empty = det_of(tmp_path, "empty.exe", b"")
    assert empty.format is formats.UNKNOWN_FORMAT and "empty" in empty.reason
    folder = tmp_path / "dir.exe"
    folder.mkdir()
    assert "folder" in formats.detect(folder).reason


def test_head_only_detection(tmp_path: Path):
    d = formats.detect(tmp_path / "does-not-exist.exe", head=bytes(make_pe(machine=0x8664, magic=0x20B)))
    assert d.format.id == "exe" and d.reason == "PE32+ x64 GUI"
    p = write(tmp_path, "real.exe", make_dos())
    d2 = formats.detect(p, head=bytes(make_pe()))  # the caller's head wins for the first bytes
    assert d2.format.id == "exe"
    assert formats.detect(tmp_path / "zip.msix", head=make_zip(["AppxManifest.xml"])).format.id == "msix"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_fifo_is_not_opened(tmp_path: Path):
    fifo = tmp_path / "pipe.exe"
    os.mkfifo(fifo)
    d = formats.detect(fifo)  # must not block
    assert d.format is formats.UNKNOWN_FORMAT and "regular" in d.reason


def test_symlink_followed(tmp_path: Path):
    target = write(tmp_path, "real.exe", make_pe())
    link = tmp_path / "link.exe"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert formats.detect(link).format.id == "exe"


@pytest.mark.parametrize("builder", [
    lambda: make_pe(machine=0x8664, magic=0x20B), lambda: make_dotnet_pe(), lambda: make_ne(), lambda: make_le(),
    lambda: bytearray(make_ole(MSI_CLSID)), lambda: bytearray(make_zip(["AppxManifest.xml"])),
    lambda: bytearray(make_iso([b"CD001"])), lambda: bytearray(make_pif()),
])
def test_every_truncation_never_raises(tmp_path: Path, builder, no_msix: None):
    data = bytes(builder())
    lengths = sorted(set(list(range(0, min(len(data), 400))) + list(range(400, len(data), max(1, len(data) // 60)))
                         + [len(data) - 1]))
    names = ("x.exe", "x.msi", "x.appx", "x.iso", "x.pif", "x.com")
    for i, n in enumerate(lengths):
        name = names[i % len(names)]
        d = formats.detect(tmp_path / ("gone" + name), head=data[:n])
        assert isinstance(d, formats.Detection) and d.format.handler in formats.HANDLERS
        if i % 5 == 0:
            d = det_of(tmp_path, name, data[:n])
            assert isinstance(d, formats.Detection) and d.format.handler in formats.HANDLERS


def test_random_bytes_never_raise(tmp_path: Path, no_msix: None):
    rng = random.Random(0x11D05)
    prefixes = [b"", b"MZ", b"PK\x03\x04", bytes.fromhex("d0cf11e0a1b11ae1"), b"MSCF", b"EXBH", b"\xff\xfe",
                b"REGEDIT4\n", b"L\x00\x00\x00"]
    for i in range(300):
        pre = prefixes[i % len(prefixes)]
        body = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 2048)))
        data = pre + body
        if pre == b"MZ" and len(data) >= 0x40:
            data = data[:0x3C] + struct.pack("<I", rng.choice([0x40, 0x80, len(data) - 2, 1 << 31])) + data[0x40:]
        name = rng.choice(["a.exe", "a.msi", "a.com", "a.reg", "a.url", "a.inf", "a.iso", "a.cab", "a", "a.msix"])
        d = det_of(tmp_path, name, data)
        assert isinstance(d, formats.Detection) and d.format.status in formats.STATUSES
        json.dumps(d.as_dict())


def test_detect_never_raises_on_internal_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **k):
        raise MemoryError("simulated")

    monkeypatch.setattr(formats, "_classify_mz", boom)
    d = det_of(tmp_path, "x.exe", make_pe())
    assert d.format is formats.UNKNOWN_FORMAT and "MemoryError" in d.reason
