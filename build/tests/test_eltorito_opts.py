"""Tests for build/lib/eltorito_opts.py (pure stdlib, runs on any OS).

The sample report below is what ``xorriso -indev linuxmint-22.2-xfce-64bit.iso
-report_el_torito as_mkisofs`` prints for a Mint 22 / Ubuntu 24.04 style hybrid
ISO (GRUB BIOS El Torito image + appended EFI System Partition as GPT
partition 2).  Informational header lines are included on purpose: they must
be ignored by the parser.
"""
from __future__ import annotations

import os
import shlex
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.normpath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import eltorito_opts as eo  # noqa: E402

MINT22_REPORT = """xorriso 1.5.6 : RockRidge filesystem manipulator, libburnia project.

Drive current: -indev 'linuxmint-22.2-xfce-64bit.iso'
Media current: stdio file, overwriteable
Media status : is written , is appendable
Boot record  : El Torito , MBR protective-msdos-label grub2-mbr cyl-align-off GPT
Media summary: 1 session, 1479935 data blocks, 2891m data, 41.2g free
Volume id    : 'Linux Mint 22.2 Xfce 64-bit'
-V 'Linux Mint 22.2 Xfce 64-bit'
--modification-date='2025072813091400'
--grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:'linuxmint-22.2-xfce-64bit.iso'
--protective-msdos-label
-partition_cyl_align off
-partition_offset 16
--mbr-force-bootable
-append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b --interval:local_fs:5919804d-5930075d::'linuxmint-22.2-xfce-64bit.iso'
-appended_part_as_gpt
-iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7
-c '/boot.catalog'
-b '/boot/grub/i386-pc/eltorito.img'
-no-emul-boot
-boot-load-size 4
-boot-info-table
--grub2-boot-info
-eltorito-alt-boot
-e '--interval:appended_partition_2_start_1479951s_size_10272d:all::'
-no-emul-boot
-boot-load-size 10272
"""

# Older Mint (≤ 21) style: ISOLINUX for BIOS + EFI image as a file in the tree.
MINT21_REPORT = """Drive current: -indev '/srv/iso/linuxmint-21.3-xfce-64bit.iso'
Volume id    : 'Linux Mint 21.3 Xfce 64-bit'
-V 'Linux Mint 21.3 Xfce 64-bit'
--modification-date='2024010810221200'
-isohybrid-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:'/srv/iso/linuxmint-21.3-xfce-64bit.iso'
-partition_cyl_align off
-partition_offset 0
-partition_hd_cyl 64
-partition_sec_hd 32
--mbr-force-bootable
-apm-block-size 2048
-iso_mbr_part_type 0x00
-c '/isolinux/boot.cat'
-b '/isolinux/isolinux.bin'
-no-emul-boot
-boot-load-size 4
-boot-info-table
-eltorito-alt-boot
-e '/boot/grub/efi.img'
-no-emul-boot
-boot-load-size 8000
-isohybrid-gpt-basdat
"""

NO_BOOT_REPORT = """Drive current: -indev 'data.iso'
Volume id    : 'DATA'
-V 'DATA'
--modification-date='2024010100000000'
"""


def test_parse_ignores_header_and_splits_quoted_values():
    toks = eo.parse_report(MINT22_REPORT)
    assert toks[0:2] == ["-V", "Linux Mint 22.2 Xfce 64-bit"]
    assert "--modification-date=2025072813091400" in toks
    assert "-c" in toks and toks[toks.index("-c") + 1] == "/boot.catalog"
    assert toks[toks.index("-b") + 1] == "/boot/grub/i386-pc/eltorito.img"
    # No header text leaked into the tokens.
    assert not any(t.startswith("Drive") or t.startswith("Media") for t in toks)
    # The last two tokens are the EFI boot-load-size.
    assert toks[-2:] == ["-boot-load-size", "10272"]


def test_iso_refs_and_rewrite():
    toks = eo.parse_report(MINT22_REPORT)
    refs = eo.iso_refs(toks)
    assert refs == ["linuxmint-22.2-xfce-64bit.iso", "linuxmint-22.2-xfce-64bit.iso"]

    new = eo.rewrite_iso_path(toks, "/srv/lindos/out/cache/my base.iso")
    assert eo.iso_refs(new) == ["/srv/lindos/out/cache/my base.iso"] * 2
    # Byte ranges and zeroizers are preserved.
    grub_mbr = new[new.index("--grub2-mbr") + 1]
    assert grub_mbr == "--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:/srv/lindos/out/cache/my base.iso"
    ap = new.index("-append_partition")
    assert new[ap + 1 : ap + 3] == ["2", "28732ac11ff8d211ba4b00a0c93ec93b"]
    assert new[ap + 3] == "--interval:local_fs:5919804d-5930075d::/srv/lindos/out/cache/my base.iso"
    # The appended_partition_2 interval (not local_fs) is untouched.
    assert "--interval:appended_partition_2_start_1479951s_size_10272d:all::" in new
    # Everything else identical.
    assert len(new) == len(toks)


def test_strip_volid_and_moddate():
    toks = eo.parse_report(MINT22_REPORT)
    stripped = eo.strip_modification_date(eo.strip_volid(toks))
    assert "-V" not in stripped
    assert "Linux Mint 22.2 Xfce 64-bit" not in stripped
    assert not any(t.startswith("--modification-date") for t in stripped)
    assert len(stripped) == len(toks) - 3
    # Everything after the stripped options is unchanged and in order.
    assert stripped[0] == "--grub2-mbr"
    assert stripped[-2:] == ["-boot-load-size", "10272"]


def test_as_mkisofs_args_one_shot():
    args = eo.as_mkisofs_args(MINT22_REPORT, "/abs/base.iso")
    assert args[0] == "--grub2-mbr"
    assert args[1] == "--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:/abs/base.iso"
    assert "-V" not in args
    keep = eo.as_mkisofs_args(MINT22_REPORT, None, keep_volid=True, keep_moddate=True)
    assert keep[0:2] == ["-V", "Linux Mint 22.2 Xfce 64-bit"]
    assert "--modification-date=2025072813091400" in keep


def test_boot_paths_catalog_and_flags():
    toks = eo.parse_report(MINT22_REPORT)
    assert eo.boot_paths(toks) == ["/boot/grub/i386-pc/eltorito.img"]
    assert eo.catalog_path(toks) == "/boot.catalog"
    assert eo.has_boot_equipment(toks)
    assert not eo.uses_isolinux(toks)

    old = eo.parse_report(MINT21_REPORT)
    assert eo.boot_paths(old) == ["/isolinux/isolinux.bin", "/boot/grub/efi.img"]
    assert eo.catalog_path(old) == "/isolinux/boot.cat"
    assert eo.uses_isolinux(old)
    assert eo.iso_refs(old) == ["/srv/iso/linuxmint-21.3-xfce-64bit.iso"]

    empty = eo.parse_report(NO_BOOT_REPORT)
    assert not eo.has_boot_equipment(empty)
    assert eo.boot_paths(empty) == []
    assert eo.catalog_path(empty) is None


def test_to_shell_round_trips_through_shlex():
    toks = eo.as_mkisofs_args(MINT22_REPORT, "/path with space/base.iso")
    line = eo.to_shell(toks)
    assert shlex.split(line) == toks
    # Quoting was actually applied to the token containing a space.
    assert "'--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:/path with space/base.iso'" in line


def test_paths_with_single_quotes_are_parsed():
    # xorriso quotes an embedded apostrophe the POSIX way: 'it'"'"'s.iso'
    report = "-V 'X'\n--grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:'/tmp/it'\"'\"'s.iso'\n-b '/boot/grub/i386-pc/eltorito.img'\n"
    toks = eo.parse_report(report)
    assert eo.iso_refs(toks) == ["/tmp/it's.iso"]


def test_unbalanced_quote_raises():
    with pytest.raises(eo.ReportError):
        eo.parse_report("-V 'oops\n")


def test_strip_option_three_arg():
    toks = eo.parse_report(MINT22_REPORT)
    no_ap = eo.strip_option(toks, "-append_partition")
    assert "-append_partition" not in no_ap
    assert "28732ac11ff8d211ba4b00a0c93ec93b" not in no_ap
    assert len(no_ap) == len(toks) - 4


def test_cli_lines_and_check(tmp_path, capsys):
    rep = tmp_path / "report.txt"
    rep.write_text(MINT22_REPORT, encoding="utf-8")
    rc = eo.main(["--report", str(rep), "--iso", "/abs/base.iso", "--check", "--format", "lines"])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "--grub2-mbr"
    assert out[1] == "--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:/abs/base.iso"
    assert "-V" not in out
    assert out[-1] == "10272"


def test_cli_shell_json_and_helpers(tmp_path, capsys):
    rep = tmp_path / "report.txt"
    rep.write_text(MINT22_REPORT, encoding="utf-8")

    assert eo.main(["--report", str(rep), "--format", "shell"]) == 0
    line = capsys.readouterr().out.strip()
    assert shlex.split(line)[0] == "--grub2-mbr"

    assert eo.main(["--report", str(rep), "--format", "json"]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data[0] == "--grub2-mbr"

    assert eo.main(["--report", str(rep), "--print-boot-paths"]) == 0
    assert capsys.readouterr().out.split() == ["/boot/grub/i386-pc/eltorito.img"]

    assert eo.main(["--report", str(rep), "--print-iso-refs"]) == 0
    assert capsys.readouterr().out.split() == ["linuxmint-22.2-xfce-64bit.iso"]


def test_cli_check_fails_without_boot_images(tmp_path):
    rep = tmp_path / "report.txt"
    rep.write_text(NO_BOOT_REPORT, encoding="utf-8")
    assert eo.main(["--report", str(rep), "--check"]) == 3
    # Without --check it still succeeds (caller decides).
    assert eo.main(["--report", str(rep)]) == 0


def test_cli_missing_report_file(tmp_path):
    assert eo.main(["--report", str(tmp_path / "nope.txt")]) == 1
