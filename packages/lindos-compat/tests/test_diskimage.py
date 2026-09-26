"""lindos_compat.diskimage: read-only loop mount via udisksctl + autorun.inf (SPEC-WINDOWS §28.6).

Hermetic: ``which``/``run`` are fakes, never touching a real block device; mount points are
plain ``tmp_path`` directories.
"""
from __future__ import annotations

import os
import types
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from lindos_compat import diskimage as di


def which_of(*names: str) -> Callable[[str], Optional[str]]:
    table = {n: f"/usr/bin/{n}" for n in names}
    return table.get


class Runner:
    """Fake subprocess.run: answers keyed by "basename subcommand", records every call."""

    def __init__(self, answers: Optional[Dict[str, object]] = None) -> None:
        self.answers = answers or {}
        self.calls: List[List[str]] = []

    def __call__(self, argv, *a, **k):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        key = " ".join([os.path.basename(argv[0])] + argv[1:2])
        ans = self.answers.get(key, self.answers.get(os.path.basename(argv[0]), (0, "", "")))
        if isinstance(ans, BaseException):
            raise ans
        rc, out, err = ans  # type: ignore[misc]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err, args=argv)


# --------------------------------------------------------------------------- #
# parse_loop_setup_output / loop_setup
# --------------------------------------------------------------------------- #
def test_parse_loop_setup_output():
    assert di.parse_loop_setup_output("Mapped file /x/a.iso as /dev/loop13.\n") == "/dev/loop13"
    assert di.parse_loop_setup_output("garbage\nno device here") is None
    assert di.parse_loop_setup_output("") is None
    # the last "as /dev/loopN" line wins when udisksctl prints more than one
    text = "Mapped file /x/a as b.iso as /dev/loop3.\nsome other line as /dev/loop9.\n"
    assert di.parse_loop_setup_output(text) == "/dev/loop9"


def test_loop_setup_missing_udisksctl(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00" * 64)
    with pytest.raises(di.DiskImageError, match="udisks2"):
        di.loop_setup(img, run=Runner(), which=which_of())


def test_loop_setup_success(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00" * 64)
    run = Runner({"udisksctl loop-setup": (0, f"Mapped file {img} as /dev/loop7.\n", "")})
    assert di.loop_setup(img, run=run, which=which_of("udisksctl")) == "/dev/loop7"
    assert run.calls == [["/usr/bin/udisksctl", "loop-setup", "-r", "-f", str(img.absolute())]]


def test_loop_setup_error_message(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00")
    err = ("Error setting up loop device for /x/win.iso: GDBus.Error:org.freedesktop.UDisks2.Error.Failed: "
           "Permission denied\n")
    run = Runner({"udisksctl loop-setup": (1, "", err)})
    with pytest.raises(di.DiskImageError, match="Permission denied"):
        di.loop_setup(img, run=run, which=which_of("udisksctl"))


def test_loop_setup_start_failure(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00")
    run = Runner({"udisksctl loop-setup": OSError("no exec")})
    with pytest.raises(di.DiskImageError, match="could not be started"):
        di.loop_setup(img, run=run, which=which_of("udisksctl"))


def test_loop_setup_falls_back_to_losetup(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00")
    run = Runner({"udisksctl loop-setup": (0, "unexpected output\n", ""),
                  "losetup": (0, "/dev/loop4: [2049]:123 (/x/win.iso)\n", "")})
    got = di.loop_setup(img, run=run, which=which_of("udisksctl", "losetup"))
    assert got == "/dev/loop4"


def test_loop_setup_nothing_works(tmp_path: Path):
    img = tmp_path / "win.iso"
    img.write_bytes(b"\x00")
    run = Runner({"udisksctl loop-setup": (0, "unexpected output\n", ""), "losetup": (0, "", "")})
    with pytest.raises(di.DiskImageError, match="could not tell"):
        di.loop_setup(img, run=run, which=which_of("udisksctl", "losetup"))


# --------------------------------------------------------------------------- #
# pick_mount_device
# --------------------------------------------------------------------------- #
def _lsblk_json(text: str) -> Runner:
    return Runner({"lsblk": (0, text, "")})


def test_pick_mount_device_no_lsblk():
    assert di.pick_mount_device("/dev/loop3", run=Runner(), which=which_of()) == ("/dev/loop3", None)


def test_pick_mount_device_lsblk_fails():
    run = Runner({"lsblk": (1, "", "no such device")})
    assert di.pick_mount_device("/dev/loop3", run=run, which=which_of("lsblk")) == ("/dev/loop3", None)


def test_pick_mount_device_whole_device_has_fstype():
    text = ('{"blockdevices":[{"name":"loop3","path":"/dev/loop3","fstype":"udf",'
           '"type":"loop","mountpoint":null,"label":"WIN11"}]}')
    got = di.pick_mount_device("/dev/loop3", run=_lsblk_json(text), which=which_of("lsblk"))
    assert got == ("/dev/loop3", None)


def test_pick_mount_device_whole_device_already_mounted():
    text = ('{"blockdevices":[{"name":"loop3","path":"/dev/loop3","fstype":"udf",'
           '"type":"loop","mountpoint":"/media/u/DISC"}]}')
    got = di.pick_mount_device("/dev/loop3", run=_lsblk_json(text), which=which_of("lsblk"))
    assert got == ("/dev/loop3", "/media/u/DISC")


def test_pick_mount_device_prefers_partition_with_fstype():
    text = ('{"blockdevices":[{"name":"loop3","path":"/dev/loop3","fstype":null,"type":"loop",'
           '"mountpoint":null,"children":['
           '{"name":"loop3p1","path":"/dev/loop3p1","fstype":"vfat","type":"part","mountpoint":null},'
           '{"name":"loop3p2","path":"/dev/loop3p2","fstype":"ntfs","type":"part","mountpoint":null}]}]}')
    got = di.pick_mount_device("/dev/loop3", run=_lsblk_json(text), which=which_of("lsblk"))
    assert got == ("/dev/loop3p1", None)  # vfat ranks before ntfs in _PREFERRED_FS


def test_pick_mount_device_no_fstype_anywhere():
    text = ('{"blockdevices":[{"name":"loop3","path":"/dev/loop3","fstype":null,"type":"loop",'
           '"mountpoint":null,"children":[{"name":"loop3p1","path":"/dev/loop3p1","fstype":null,'
           '"type":"part","mountpoint":null}]}]}')
    got = di.pick_mount_device("/dev/loop3", run=_lsblk_json(text), which=which_of("lsblk"))
    assert got == ("/dev/loop3", None)


def test_pick_mount_device_bad_json():
    run = Runner({"lsblk": (0, "not json at all", "")})
    assert di.pick_mount_device("/dev/loop3", run=run, which=which_of("lsblk")) == ("/dev/loop3", None)


# --------------------------------------------------------------------------- #
# parse_mount_output / mount_loop
# --------------------------------------------------------------------------- #
def test_parse_mount_output_basic():
    assert di.parse_mount_output("Mounted /dev/loop0 at /media/alice/DISC\n") == "/media/alice/DISC"


def test_parse_mount_output_trailing_period_is_stripped_when_path_absent():
    # older udisksctl ended the sentence with a period; the path itself never exists in the test env
    got = di.parse_mount_output("Mounted /dev/loop0 at /media/alice/DISC.\n")
    assert got == "/media/alice/DISC"


def test_parse_mount_output_already_mounted():
    err = ("Error mounting /dev/loop0: GDBus.Error:org.freedesktop.UDisks2.Error.AlreadyMounted: "
           "Device /dev/loop0 is already mounted at `/media/alice/DISC'.\n")
    assert di.parse_mount_output(err) == "/media/alice/DISC"


def test_parse_mount_output_none():
    assert di.parse_mount_output("nothing useful here") is None
    assert di.parse_mount_output("") is None


def test_mount_loop_already_mounted_skips_the_command(tmp_path: Path):
    text = ('{"blockdevices":[{"name":"loop3","path":"/dev/loop3","fstype":"udf","type":"loop",'
           f'"mountpoint":"{tmp_path.as_posix()}"}}]}}')
    run = Runner({"lsblk": (0, text, "")})
    got = di.mount_loop("/dev/loop3", run=run, which=which_of("lsblk", "udisksctl"))
    assert got == tmp_path
    assert not any(os.path.basename(c[0]) == "udisksctl" for c in run.calls)


def test_mount_loop_runs_udisksctl(tmp_path: Path):
    mount_dir = tmp_path / "DISC"
    run = Runner({"udisksctl mount": (0, f"Mounted /dev/loop3 at {mount_dir}\n", "")})
    got = di.mount_loop("/dev/loop3", run=run, which=which_of("udisksctl"))
    assert got == mount_dir
    assert run.calls == [["/usr/bin/udisksctl", "mount", "-b", "/dev/loop3"]]


def test_mount_loop_error(tmp_path: Path):
    run = Runner({"udisksctl mount": (1, "", "Error mounting /dev/loop3: not authorized\n")})
    with pytest.raises(di.DiskImageError, match="not authorized"):
        di.mount_loop("/dev/loop3", run=run, which=which_of("udisksctl"))


def test_mount_loop_unexpected_success_output():
    run = Runner({"udisksctl mount": (0, "", "")})
    with pytest.raises(di.DiskImageError, match="could not tell"):
        di.mount_loop("/dev/loop3", run=run, which=which_of("udisksctl"))


def test_mount_loop_missing_udisksctl():
    with pytest.raises(di.DiskImageError, match="udisks2"):
        di.mount_loop("/dev/loop3", run=Runner(), which=which_of())


# --------------------------------------------------------------------------- #
# base_loop_device / detach_command
# --------------------------------------------------------------------------- #
def test_base_loop_device():
    assert di.base_loop_device("/dev/loop3p1") == "/dev/loop3"
    assert di.base_loop_device("/dev/loop12p9") == "/dev/loop12"
    assert di.base_loop_device("/dev/loop3") == "/dev/loop3"
    assert di.base_loop_device("/dev/sda1") == "/dev/sda1"


def test_detach_command():
    assert di.detach_command("/dev/loop3p1") == [
        ["udisksctl", "unmount", "-b", "/dev/loop3p1"],
        ["udisksctl", "loop-delete", "-b", "/dev/loop3"],
    ]


# --------------------------------------------------------------------------- #
# find_autorun
# --------------------------------------------------------------------------- #
def test_find_autorun_ansi(tmp_path: Path):
    (tmp_path / "autorun.inf").write_bytes(b"[autorun]\r\nopen=SETUP.EXE\r\nlabel=My Disc\r\nicon=SETUP.EXE,0\r\n")
    got = di.find_autorun(tmp_path)
    assert got == {"open": "SETUP.EXE", "icon": "SETUP.EXE,0", "label": "My Disc", "shellexecute": ""}


def test_find_autorun_case_insensitive_filename(tmp_path: Path):
    (tmp_path / "AutoRun.INF").write_bytes(b"[AutoRun]\nopen=install.exe\n")
    assert di.find_autorun(tmp_path)["open"] == "install.exe"


def test_find_autorun_utf16le_bom(tmp_path: Path):
    text = "[AutoRun]\r\nopen=SETUP.EXE\r\nlabel=\u00dcber Disc\r\n"
    (tmp_path / "autorun.inf").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    got = di.find_autorun(tmp_path)
    assert got["open"] == "SETUP.EXE" and got["label"] == "\u00dcber Disc"


def test_find_autorun_utf8_bom(tmp_path: Path):
    (tmp_path / "autorun.inf").write_bytes(b"\xef\xbb\xbf[AutoRun]\r\nopen=SETUP.EXE\r\n")
    assert di.find_autorun(tmp_path)["open"] == "SETUP.EXE"


def test_find_autorun_amd64_section_wins(tmp_path: Path):
    (tmp_path / "autorun.inf").write_bytes(
        b"[AutoRun]\r\nopen=SETUP32.EXE\r\n[AutoRun.Amd64]\r\nopen=SETUP64.EXE\r\n")
    assert di.find_autorun(tmp_path)["open"] == "SETUP64.EXE"


def test_find_autorun_missing_file(tmp_path: Path):
    assert di.find_autorun(tmp_path) is None


def test_find_autorun_no_autorun_section(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text("[Content]\nMusicFiles=1\n", encoding="ascii")
    assert di.find_autorun(tmp_path) is None


def test_find_autorun_ignores_symlinked_inf(tmp_path: Path):
    real = tmp_path / "elsewhere" / "autorun.inf"
    real.parent.mkdir()
    real.write_text("[AutoRun]\nopen=SETUP.EXE\n", encoding="ascii")
    link = tmp_path / "disc" / "autorun.inf"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert di.find_autorun(link.parent) is None


def test_find_autorun_directory_named_autorun_inf_is_ignored(tmp_path: Path):
    (tmp_path / "autorun.inf").mkdir()
    assert di.find_autorun(tmp_path) is None


# --------------------------------------------------------------------------- #
# find_setup
# --------------------------------------------------------------------------- #
def test_find_setup_uses_autorun_open(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text("[AutoRun]\nopen=SETUP.EXE\n", encoding="ascii")
    (tmp_path / "SETUP.EXE").write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == tmp_path / "SETUP.EXE"


def test_find_setup_case_insensitive_nested_path(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text('[AutoRun]\nopen="Install\\Setup.exe"\n', encoding="ascii")
    nested = tmp_path / "install"
    nested.mkdir()
    target = nested / "setup.exe"
    target.write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == target


def test_find_setup_open_with_switches(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text("[AutoRun]\nopen=SETUP.EXE /silent\n", encoding="ascii")
    (tmp_path / "SETUP.EXE").write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == tmp_path / "SETUP.EXE"


def test_find_setup_drive_letter_is_stripped(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text(r"[AutoRun]\nopen=D:\SETUP.EXE".replace(r"\n", "\n"), encoding="ascii")
    (tmp_path / "SETUP.EXE").write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == tmp_path / "SETUP.EXE"


def test_find_setup_rejects_path_traversal(tmp_path: Path):
    outside = tmp_path.parent / "OUTSIDE.EXE"
    outside.write_bytes(b"MZ")
    disc = tmp_path / "disc"
    disc.mkdir()
    (disc / "autorun.inf").write_text(r"[AutoRun]\nopen=..\OUTSIDE.EXE".replace(r"\n", "\n"), encoding="ascii")
    assert di.find_setup(disc) is None


def test_find_setup_falls_back_to_top_level_setup_exe(tmp_path: Path):
    (tmp_path / "SETUP.EXE").write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == tmp_path / "SETUP.EXE"


def test_find_setup_falls_back_to_install_exe(tmp_path: Path):
    (tmp_path / "INSTALL.EXE").write_bytes(b"MZ")
    assert di.find_setup(tmp_path) == tmp_path / "INSTALL.EXE"


def test_find_setup_none_when_nothing_runnable(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("hello", encoding="ascii")
    assert di.find_setup(tmp_path) is None


def test_find_setup_refuses_non_runnable_open_target(tmp_path: Path):
    (tmp_path / "autorun.inf").write_text("[AutoRun]\nopen=readme.html\n", encoding="ascii")
    (tmp_path / "readme.html").write_text("<html></html>", encoding="ascii")
    (tmp_path / "SETUP.EXE").write_bytes(b"MZ")
    # the open= target is not a runnable suffix, so find_setup falls back to the top-level program
    assert di.find_setup(tmp_path) == tmp_path / "SETUP.EXE"


def test_find_setup_refuses_symlink_escaping_the_mount(tmp_path: Path):
    outside = tmp_path.parent / "OUTSIDE.EXE"
    outside.write_bytes(b"MZ")
    disc = tmp_path / "disc"
    disc.mkdir()
    (disc / "autorun.inf").write_text("[AutoRun]\nopen=SETUP.EXE\n", encoding="ascii")
    try:
        (disc / "SETUP.EXE").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert di.find_setup(disc) is None


# --------------------------------------------------------------------------- #
# open_image (the full flow)
# --------------------------------------------------------------------------- #
def test_open_image_full_flow(tmp_path: Path):
    img = tmp_path / "Windows11.iso"
    img.write_bytes(b"\x00" * 64)
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    (mount_dir / "autorun.inf").write_text("[AutoRun]\nopen=SETUP.EXE\nlabel=My Windows Disc\n", encoding="ascii")
    (mount_dir / "SETUP.EXE").write_bytes(b"MZ")

    run = Runner({
        "udisksctl loop-setup": (0, f"Mapped file {img} as /dev/loop7.\n", ""),
        "udisksctl mount": (0, f"Mounted /dev/loop7 at {mount_dir}\n", ""),
    })
    result = di.open_image(img, run=run, which=which_of("udisksctl"))
    assert result["device"] == "/dev/loop7"
    assert result["mount"] == str(mount_dir)
    assert result["label"] == "My Windows Disc"
    assert result["autorun"]["open"] == "SETUP.EXE"
    assert result["setup"] == str(mount_dir / "SETUP.EXE")
    assert result["detach"] == [["udisksctl", "unmount", "-b", "/dev/loop7"],
                                ["udisksctl", "loop-delete", "-b", "/dev/loop7"]]


def test_open_image_no_setup_program(tmp_path: Path):
    img = tmp_path / "Data.iso"
    img.write_bytes(b"\x00")
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    (mount_dir / "readme.txt").write_text("hi", encoding="ascii")
    run = Runner({
        "udisksctl loop-setup": (0, f"Mapped file {img} as /dev/loop2.\n", ""),
        "udisksctl mount": (0, f"Mounted /dev/loop2 at {mount_dir}\n", ""),
    })
    result = di.open_image(img, run=run, which=which_of("udisksctl"))
    assert result["setup"] is None
    assert result["autorun"] is None
    assert result["label"] == "mnt"  # falls back to the mount point's own name


def test_open_image_detaches_on_mount_failure(tmp_path: Path):
    img = tmp_path / "Bad.iso"
    img.write_bytes(b"\x00")
    run = Runner({
        "udisksctl loop-setup": (0, f"Mapped file {img} as /dev/loop9.\n", ""),
        "udisksctl mount": (1, "", "Error mounting /dev/loop9: not authorized\n"),
    })
    with pytest.raises(di.DiskImageError, match="not authorized"):
        di.open_image(img, run=run, which=which_of("udisksctl"))
    assert ["udisksctl", "loop-delete", "-b", "/dev/loop9"] in run.calls


def test_open_image_loop_setup_failure_propagates_without_mount_attempt(tmp_path: Path):
    img = tmp_path / "Bad.iso"
    img.write_bytes(b"\x00")
    run = Runner({"udisksctl loop-setup": (1, "", "Error setting up loop device: Permission denied\n")})
    with pytest.raises(di.DiskImageError, match="Permission denied"):
        di.open_image(img, run=run, which=which_of("udisksctl"))
    assert not any(c[1:2] == ["mount"] for c in run.calls)
