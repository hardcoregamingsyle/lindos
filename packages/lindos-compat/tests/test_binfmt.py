"""lindos_compat.binfmt: the ``lindos-pe`` binfmt_misc entry -- status, conflicts, the wrapper's
file re-check (SPEC-WINDOWS §28.7).

Hermetic: every test builds its own fake ``/proc`` + ``/etc`` + ``/usr`` tree under ``tmp_path``
and passes it as ``root=``; nothing ever touches the host's real binfmt_misc.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

import pytest

from lindos_compat import binfmt


# --------------------------------------------------------------------------- #
# tree builder
# --------------------------------------------------------------------------- #
class Tree:
    """A fake ``root=`` tree for :func:`binfmt.status`."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.proc = base / "proc/sys/fs/binfmt_misc"

    def with_binfmt_misc(self, *, global_enabled: bool = True) -> "Tree":
        self.proc.mkdir(parents=True)
        (self.proc / "register").write_text("", encoding="ascii")
        (self.proc / "status").write_text("enabled\n" if global_enabled else "disabled\n", encoding="ascii")
        return self

    def with_conf_installed(self) -> "Tree":
        conf = self.base / "usr/lib/binfmt.d/lindos-pe.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("# lindos-pe\n" + binfmt.CONF_LINE + "\n", encoding="ascii")
        return self

    def with_mask(self, *, empty_file: bool = True) -> "Tree":
        mask = self.base / "etc/binfmt.d/lindos-pe.conf"
        mask.parent.mkdir(parents=True, exist_ok=True)
        if empty_file:
            mask.write_text("", encoding="ascii")  # _is_masked also treats an empty override as masked
        else:
            try:
                mask.symlink_to("/dev/null")
            except (OSError, NotImplementedError):
                pytest.skip("symlinks not available")
        return self

    def with_entry(self, name: str, *, enabled: bool = True, interpreter: str = "/usr/bin/wine",
                  offset: Optional[int] = 0, magic: str = "4d5a", mask: Optional[str] = None,
                  extension: Optional[str] = None, extra_flags: str = "") -> "Tree":
        lines = ["enabled" if enabled else "disabled", f"interpreter {interpreter}", f"flags: {extra_flags}"]
        if extension is not None:
            lines.append(f"extension .{extension}")
        else:
            if offset is not None:
                lines.append(f"offset {offset}")
            lines.append(f"magic {magic}")
            if mask is not None:
                lines.append(f"mask {mask}")
        (self.proc / name).write_text("\n".join(lines) + "\n", encoding="ascii")
        return self

    def managed_by_binfmt_support(self, name: str) -> "Tree":
        managed = self.base / "var/lib/binfmts" / name
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text("", encoding="ascii")
        return self

    def with_binfmt_support_installed(self) -> "Tree":
        tool = self.base / "usr/sbin/update-binfmts"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("", encoding="ascii")
        return self


def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


# --------------------------------------------------------------------------- #
# parse_proc_entry
# --------------------------------------------------------------------------- #
def test_parse_proc_entry_magic():
    text = "enabled\ninterpreter /usr/libexec/lindos/lindos-binfmt\nflags: \noffset 0\nmagic 4d5a\n"
    got = binfmt.parse_proc_entry(text)
    assert got == {"enabled": True, "interpreter": "/usr/libexec/lindos/lindos-binfmt", "flags": "",
                  "flag_set": [], "type": "magic", "offset": 0, "magic": "4d5a", "mask": None, "extension": None}


def test_parse_proc_entry_disabled_with_mask():
    text = "disabled\ninterpreter /usr/bin/wine\nflags: POC\noffset 0\nmagic 4d5a0000\nmask ffff0000\n"
    got = binfmt.parse_proc_entry(text)
    assert got["enabled"] is False
    assert got["flag_set"] == ["C", "O", "P"]
    assert got["mask"] == "ffff0000"


def test_parse_proc_entry_extension():
    text = "enabled\ninterpreter /usr/bin/qemu-mips\nflags: OC\nextension .mips\n"
    got = binfmt.parse_proc_entry(text)
    assert got["type"] == "extension" and got["extension"] == ".mips"


def test_parse_proc_entry_unknown_flag_letters_tolerated():
    # upstream master documents new flags T/L/D; the parser must not choke on them
    text = "enabled\ninterpreter /usr/bin/x\nflags: TLD\noffset 0\nmagic aabb\n"
    got = binfmt.parse_proc_entry(text)
    assert got["flag_set"] == ["D", "L", "T"]


def test_parse_proc_entry_blank_and_garbage_lines_ignored():
    text = "\n  \nenabled\ngarbage line with no colon\ninterpreter /usr/bin/x\n\noffset 0\nmagic 4d5a\n"
    got = binfmt.parse_proc_entry(text)
    assert got["enabled"] is True and got["interpreter"] == "/usr/bin/x"


def test_parse_proc_entry_bad_offset_is_none():
    text = "enabled\ninterpreter /usr/bin/x\noffset not-a-number\nmagic 4d5a\n"
    got = binfmt.parse_proc_entry(text)
    assert got["offset"] is None and got["type"] == "magic"


def test_parse_proc_entry_empty_text():
    got = binfmt.parse_proc_entry("")
    assert got["enabled"] is False and got["type"] == "unknown" and got["interpreter"] == ""


# --------------------------------------------------------------------------- #
# entry_matches_mz
# --------------------------------------------------------------------------- #
def test_entry_matches_mz_exact():
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 0, "magic": "4d5a"}) is True


def test_entry_matches_mz_reversed_bytes_do_not_match():
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 0, "magic": "5a4d"}) is False


def test_entry_matches_mz_wrong_offset():
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 4, "magic": "4d5a"}) is False


def test_entry_matches_mz_missing_offset_defaults_to_conflict():
    # a proc entry that (unusually) omits its "offset" line is still treated as a possible
    # offset-0 conflict rather than silently ignored
    assert binfmt.entry_matches_mz({"type": "magic", "offset": None, "magic": "4d5a"}) is True


def test_entry_matches_mz_mask_hides_extra_bytes():
    # the entry's magic requires more specific bytes than plain MZ, but the visible mask still
    # only compares the first two bytes against "MZ" -- still reported as a competitor
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 0, "magic": "4d5a9000", "mask": "ffffffff"}) is True


def test_entry_matches_mz_wildcard_mask_matches_anything():
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 0, "magic": "0000", "mask": "0000"}) is True


def test_entry_matches_mz_invalid_magic_hex():
    assert binfmt.entry_matches_mz({"type": "magic", "offset": 0, "magic": "not-hex"}) is False


def test_entry_matches_mz_extension_exe_case_variants():
    assert binfmt.entry_matches_mz({"type": "extension", "extension": ".exe"}) is True
    assert binfmt.entry_matches_mz({"type": "extension", "extension": "EXE"}) is True
    assert binfmt.entry_matches_mz({"type": "extension", "extension": ".com"}) is False


def test_entry_matches_mz_unknown_type():
    assert binfmt.entry_matches_mz({"type": "unknown"}) is False


# --------------------------------------------------------------------------- #
# status()
# --------------------------------------------------------------------------- #
def test_status_binfmt_misc_unavailable(tmp_path: Path):
    st = binfmt.status(tmp_path)  # no /proc/sys/fs/binfmt_misc at all
    assert st["available"] is False and st["registered"] is False and st["enabled"] is False
    assert "not available" in st["note"]


def test_status_globally_disabled(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc(global_enabled=False).with_conf_installed().with_entry(binfmt.BINFMT_NAME)
    st = binfmt.status(tmp_path)
    assert st["available"] is True and st["global_enabled"] is False and st["enabled"] is False
    assert "switched off system-wide" in st["note"]


def test_status_masked(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME).with_mask()
    st = binfmt.status(tmp_path)
    assert st["masked"] is True
    assert "turned off" in st["note"].lower()


def test_status_masked_via_real_symlink(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME).with_mask(
        empty_file=False)
    st = binfmt.status(tmp_path)
    assert st["masked"] is True


def test_status_not_installed_at_all(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc()
    st = binfmt.status(tmp_path)
    assert st["installed"] is False and st["registered"] is False
    assert "reinstall" in st["note"]


def test_status_installed_but_not_registered_yet(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed()
    st = binfmt.status(tmp_path)
    assert st["installed"] is True and st["registered"] is False
    assert st["interpreter"] == binfmt.INTERPRETER  # read from the installed .conf via _conf_interpreter
    assert "not active yet" in st["note"]


def test_status_registered_but_disabled(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME, enabled=False)
    st = binfmt.status(tmp_path)
    assert st["registered"] is True and st["enabled"] is False
    assert "loaded but disabled" in st["note"]


def test_status_all_good_no_conflicts(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(
        binfmt.BINFMT_NAME, interpreter=binfmt.INTERPRETER)
    st = binfmt.status(tmp_path)
    assert st["registered"] is True and st["enabled"] is True and st["masked"] is False
    assert st["conflicts"] == []
    assert st["interpreter"] == binfmt.INTERPRETER
    assert "starts Windows programs through Lindos" in st["note"]


def test_status_reports_conflict_from_binfmt_support(tmp_path: Path):
    t = tree(tmp_path).with_binfmt_misc().with_conf_installed()
    t.with_entry(binfmt.BINFMT_NAME, interpreter=binfmt.INTERPRETER)
    t.with_entry("wine", interpreter="/usr/bin/wine")
    t.managed_by_binfmt_support("wine")
    st = binfmt.status(tmp_path)
    assert st["conflicts"] == [{"name": "wine", "interpreter": "/usr/bin/wine", "managed_by": "binfmt-support"}]
    assert "'wine'" in st["note"]
    assert "binfmt-support registers its handlers after Lindos" in st["note"]
    assert "does not change other handlers" in st["note"]


def test_status_conflict_not_managed_by_binfmt_support(tmp_path: Path):
    t = tree(tmp_path).with_binfmt_misc().with_conf_installed()
    t.with_entry(binfmt.BINFMT_NAME, interpreter=binfmt.INTERPRETER)
    t.with_entry("qemu-custom", interpreter="/usr/bin/qemu-custom")
    st = binfmt.status(tmp_path)
    assert st["conflicts"] == [{"name": "qemu-custom", "interpreter": "/usr/bin/qemu-custom", "managed_by": ""}]
    assert "binfmt-support registers" not in st["note"]


def test_status_disabled_conflict_is_not_reported(tmp_path: Path):
    t = tree(tmp_path).with_binfmt_misc().with_conf_installed()
    t.with_entry(binfmt.BINFMT_NAME, interpreter=binfmt.INTERPRETER)
    t.with_entry("wine", interpreter="/usr/bin/wine", enabled=False)
    st = binfmt.status(tmp_path)
    assert st["conflicts"] == []


def test_status_extension_conflict_is_reported(tmp_path: Path):
    t = tree(tmp_path).with_binfmt_misc().with_conf_installed()
    t.with_entry(binfmt.BINFMT_NAME, interpreter=binfmt.INTERPRETER)
    t.with_entry("legacy-exe", interpreter="/usr/bin/legacy", extension="exe")
    st = binfmt.status(tmp_path)
    assert [c["name"] for c in st["conflicts"]] == ["legacy-exe"]


def test_status_binfmt_support_installed_flag(tmp_path: Path):
    t = tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME)
    t.with_binfmt_support_installed()
    st = binfmt.status(tmp_path)
    assert st["binfmt_support"] is True


def test_status_binfmt_support_not_installed_flag(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME)
    st = binfmt.status(tmp_path)
    assert st["binfmt_support"] is False


def test_status_honours_lindos_root_env_when_root_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME,
                                                                       interpreter=binfmt.INTERPRETER)
    st = binfmt.status()  # root=None -> falls back to LINDOS_ROOT
    assert st["registered"] is True and st["enabled"] is True


def test_status_json_serialisable(tmp_path: Path):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME)
    st = binfmt.status(tmp_path)
    json.dumps(st)  # must not raise


# --------------------------------------------------------------------------- #
# enable_commands / disable_commands / remove_runtime_entry
# --------------------------------------------------------------------------- #
def test_enable_commands():
    assert binfmt.enable_commands() == [["rm", "-f", binfmt.MASK_PATH],
                                        [binfmt.SYSTEMD_BINFMT, binfmt.CONF_PATH]]


def test_disable_commands():
    cmds = binfmt.disable_commands()
    assert cmds[0] == ["mkdir", "-p", os.path.dirname(binfmt.MASK_PATH)]
    assert cmds[1] == ["ln", "-sf", "/dev/null", binfmt.MASK_PATH]
    assert cmds[2][0] == "sh" and cmds[2][1] == "-c"
    assert f"{binfmt.PROC_DIR}/{binfmt.BINFMT_NAME}" in cmds[2][2]
    assert "echo -1" in cmds[2][2]


def test_disable_commands_never_restarts_systemd_binfmt():
    # SPEC-WINDOWS §28.7 / the linux-plumbing research: this must NEVER appear anywhere here
    flat = " ".join(" ".join(c) for c in binfmt.enable_commands() + binfmt.disable_commands())
    assert "restart" not in flat and "stop" not in flat


def test_remove_runtime_entry_existing(tmp_path: Path):
    proc = tmp_path / "proc/sys/fs/binfmt_misc"
    proc.mkdir(parents=True)
    (proc / binfmt.BINFMT_NAME).write_text("enabled\n", encoding="ascii")
    assert binfmt.remove_runtime_entry(tmp_path) is True
    assert (proc / binfmt.BINFMT_NAME).read_text(encoding="ascii") == "-1"


def test_remove_runtime_entry_missing_is_false(tmp_path: Path):
    assert binfmt.remove_runtime_entry(tmp_path) is False


def test_remove_runtime_entry_other_errors_propagate(tmp_path: Path):
    proc = tmp_path / "proc/sys/fs/binfmt_misc"
    proc.mkdir(parents=True)
    (proc / binfmt.BINFMT_NAME).mkdir()  # a directory where a file is expected -> not FileNotFoundError
    with pytest.raises(OSError):
        binfmt.remove_runtime_entry(tmp_path)


# --------------------------------------------------------------------------- #
# check_program (uses formats.detect/plan_action lazily)
# --------------------------------------------------------------------------- #
def test_check_program_runnable_exe(tmp_path: Path, make_pe):
    exe = tmp_path / "GAME.EXE"
    exe.write_bytes(make_pe(machine=0x8664, subsystem=2))
    ok, message = binfmt.check_program(exe)
    assert ok is True
    assert "GUI" in message or "x64" in message


def test_check_program_refuses_dll(tmp_path: Path, make_pe):
    dll = tmp_path / "SOMETHING.DLL"
    dll.write_bytes(make_pe(machine=0x8664, dll=True))
    ok, message = binfmt.check_program(dll)
    assert ok is False
    assert "library" in message.lower() or "driver" in message.lower()


def test_check_program_refuses_arm_exe(tmp_path: Path, make_pe):
    exe = tmp_path / "ARMAPP.EXE"
    exe.write_bytes(make_pe(machine=0xAA64, subsystem=2))
    ok, message = binfmt.check_program(exe)
    assert ok is False
    assert "ARM" in message


def test_check_program_allows_dos_exe(tmp_path: Path):
    exe = tmp_path / "GAME.EXE"
    exe.write_bytes(b"MZ" + b"\x00" * 62 + b"\xff\xff\xff\xff")  # MZ with a bogus e_lfanew: falls back to dos-exe
    ok, _message = binfmt.check_program(exe)
    assert ok is True


def test_check_program_refuses_non_program(tmp_path: Path):
    txt = tmp_path / "readme.txt"
    txt.write_text("hello", encoding="ascii")
    ok, message = binfmt.check_program(txt)
    assert ok is False
    assert "readme.txt" in message


# --------------------------------------------------------------------------- #
# main() CLI
# --------------------------------------------------------------------------- #
def test_main_status_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME,
                                                                       interpreter=binfmt.INTERPRETER)
    rc = binfmt.main(["status", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "starts Windows programs through Lindos" in out


def test_main_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    tree(tmp_path).with_binfmt_misc().with_conf_installed().with_entry(binfmt.BINFMT_NAME,
                                                                       interpreter=binfmt.INTERPRETER)
    rc = binfmt.main(["status", "--json", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["registered"] is True and data["enabled"] is True


def test_main_check_ok(tmp_path: Path, make_pe):
    exe = tmp_path / "GAME.EXE"
    exe.write_bytes(make_pe(machine=0x8664, subsystem=2))
    assert binfmt.main(["check", str(exe)]) == 0


def test_main_check_refused(tmp_path: Path, make_pe, capsys: pytest.CaptureFixture[str]):
    dll = tmp_path / "SOMETHING.DLL"
    dll.write_bytes(make_pe(machine=0x8664, dll=True))
    rc = binfmt.main(["check", str(dll)])
    err = capsys.readouterr().err
    assert rc == 3
    assert "library" in err.lower() or "driver" in err.lower()


def test_main_requires_a_subcommand():
    with pytest.raises(SystemExit):
        binfmt.main([])


# --------------------------------------------------------------------------- #
# constants (a change here breaks the shipped .conf / postinst -- pin them)
# --------------------------------------------------------------------------- #
def test_constants_match_the_spec():
    assert binfmt.BINFMT_NAME == "lindos-pe"
    assert binfmt.CONF_PATH == "/usr/lib/binfmt.d/lindos-pe.conf"
    assert binfmt.MASK_PATH == "/etc/binfmt.d/lindos-pe.conf"
    assert binfmt.PROC_DIR == "/proc/sys/fs/binfmt_misc"
    assert binfmt.INTERPRETER == "/usr/libexec/lindos/lindos-binfmt"
    assert binfmt.CONF_LINE == ":lindos-pe:M::MZ::/usr/libexec/lindos/lindos-binfmt:"
    assert binfmt.SYSTEMD_BINFMT == "/usr/lib/systemd/systemd-binfmt"
    assert "exe" in binfmt.RUNNABLE_FORMATS and "dll" not in binfmt.RUNNABLE_FORMATS
