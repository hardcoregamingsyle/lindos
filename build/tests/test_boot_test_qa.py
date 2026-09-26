"""Hermetic tests for build/qa/boot_test.py's pure logic (no real QEMU/xorriso/network).

Covers: serial-log parsing (the CHECK/INFO/FAILED_UNIT/DONE sentinel grammar the guest-side
``ci-boot-smoke-test.sh`` emits), the QEMU argv builder (systemd.run= path, KVM/TCG fallback,
screendump-friendly VGA choice), and the extraction helper against injected which/run callables
so it never shells out for real. Works on Windows and Linux.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
QA_DIR = HERE.parent / "qa"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import boot_test  # noqa: E402


# --------------------------------------------------------------------------- #
# serial-log parsing
# --------------------------------------------------------------------------- #

SAMPLE_LOG = """\
some kernel boot noise before init
LINDOS_SMOKE_START
LINDOS_INFO uname=6.8.0-31-lindos
LINDOS_INFO release=Lindos 1.0.0 (Aurora)
LINDOS_INFO os_release_id=linuxmint version=22.2 pretty=Lindos 1.0 (Aurora)
LINDOS_CHECK python-import=OK
LINDOS_CHECK lindos-mode=OK
LINDOS_CHECK lindos-compat-doctor=FAIL rc=1
LINDOS_INFO failed_units=1
LINDOS_FAILED_UNIT some.service loaded failed failed Some Service
LINDOS_INFO is_system_running=degraded
LINDOS_SMOKE_DONE rc=1
"""


def test_parse_report_full(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["booted"] is True
    assert report["smoke_rc"] == 1
    assert report["checks"]["python-import"] == {"status": "OK", "rc": None}
    assert report["checks"]["lindos-compat-doctor"] == {"status": "FAIL", "rc": "1"}
    assert report["info"]["uname"] == "6.8.0-31-lindos"
    assert report["info"]["is_system_running"] == "degraded"
    assert len(report["failed_units"]) == 1
    assert "some.service" in report["failed_units"][0]


def test_parse_report_all_ok(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(
        "LINDOS_SMOKE_START\n"
        "LINDOS_INFO uname=6.8.0-31-generic\n"
        "LINDOS_CHECK python-import=OK\n"
        "LINDOS_INFO failed_units=0\n"
        "LINDOS_SMOKE_DONE rc=0\n",
        encoding="utf-8",
    )
    report = boot_test.parse_report(log)
    assert report["smoke_rc"] == 0
    assert all(v["status"] == "OK" for v in report["checks"].values())
    assert report["failed_units"] == []


def test_parse_report_never_booted(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("Kernel panic - not syncing: VFS: Unable to mount root fs\n", encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["booted"] is False
    assert report["smoke_rc"] is None


def test_parse_report_missing_file(tmp_path):
    report = boot_test.parse_report(tmp_path / "does-not-exist.log")
    assert report["booted"] is False
    assert report["checks"] == {}
    assert report["info"] == {}


def test_tail_for_done_finds_sentinel_written_incrementally(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\n", encoding="utf-8")

    calls = {"n": 0}
    real_sleep = boot_test.time.sleep

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            with log.open("a", encoding="utf-8") as fh:
                fh.write("LINDOS_SMOKE_DONE rc=0\n")

    boot_test.time.sleep = fake_sleep
    try:
        line = boot_test.tail_for_done(log, timeout=5)
    finally:
        boot_test.time.sleep = real_sleep
    assert line == "LINDOS_SMOKE_DONE rc=0"


def test_tail_for_done_times_out(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\n", encoding="utf-8")
    line = boot_test.tail_for_done(log, timeout=0.01)
    assert line is None


# --------------------------------------------------------------------------- #
# QEMU argv
# --------------------------------------------------------------------------- #


def test_build_qemu_argv_shape(tmp_path):
    argv = boot_test.build_qemu_argv(
        vmlinuz=tmp_path / "vmlinuz", initrd=tmp_path / "initrd", iso=tmp_path / "lindos.iso",
        serial_log=tmp_path / "serial.log", monitor_sock=tmp_path / "mon.sock", ram_mb=4096, cpus=2,
    )
    assert argv[0] == "qemu-system-x86_64"
    assert "-kernel" in argv and str(tmp_path / "vmlinuz") in argv
    assert "-initrd" in argv and str(tmp_path / "initrd") in argv
    assert "-cdrom" in argv and str(tmp_path / "lindos.iso") in argv
    # KVM is tried first, TCG is the documented fallback -- never crash on a runner without /dev/kvm
    accel_idxs = [i for i, a in enumerate(argv) if a == "-accel"]
    accels = [argv[i + 1] for i in accel_idxs]
    assert accels == ["kvm", "tcg"]
    # -display none still needs a real VGA device for `screendump` to work
    assert "-display" in argv and argv[argv.index("-display") + 1] == "none"
    assert "-vga" in argv and argv[argv.index("-vga") + 1] == "std"
    append = argv[argv.index("-append") + 1]
    assert "boot=casper" in append
    assert f"systemd.run={boot_test.SMOKE_SCRIPT_PATH}" in append
    assert "console=ttyS0" in append
    # never a shell-quoted / spaced inline command -- a bare path only (kernel cmdline
    # quoting for systemd.run= is not something to rely on; see boot_test.py docstring)
    run_token = [t for t in append.split() if t.startswith("systemd.run=")][0]
    assert " " not in run_token


def test_build_qemu_argv_monitor_and_serial_paths(tmp_path):
    argv = boot_test.build_qemu_argv(
        vmlinuz=tmp_path / "v", initrd=tmp_path / "i", iso=tmp_path / "x.iso",
        serial_log=tmp_path / "s.log", monitor_sock=tmp_path / "m.sock", ram_mb=2048, cpus=1,
    )
    serial_arg = argv[argv.index("-serial") + 1]
    assert serial_arg == f"file:{tmp_path / 's.log'}"
    monitor_arg = argv[argv.index("-monitor") + 1]
    assert monitor_arg.startswith(f"unix:{tmp_path / 'm.sock'}")
    assert "server=on" in monitor_arg and "wait=off" in monitor_arg


# --------------------------------------------------------------------------- #
# extraction (injected which/run -- never shells out)
# --------------------------------------------------------------------------- #


def _fake_run_factory(*, initrd_name="/casper/initrd.lz"):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        dest = Path(argv[argv.index("-extract") + 2])
        src = argv[argv.index("-extract") + 1]
        if src == "/casper/vmlinuz":
            dest.write_bytes(b"fake-kernel-bytes")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if src == initrd_name:
            dest.write_bytes(b"fake-initrd-bytes")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    return fake_run, calls


def test_extract_casper_prefers_lz_initrd(tmp_path):
    fake_run, calls = _fake_run_factory(initrd_name="/casper/initrd.lz")
    vmlinuz, initrd = boot_test.extract_casper(
        tmp_path / "lindos.iso", tmp_path / "out",
        which=lambda name: "/usr/bin/xorriso" if name == "xorriso" else None,
        run=fake_run,
    )
    assert vmlinuz.read_bytes() == b"fake-kernel-bytes"
    assert initrd.read_bytes() == b"fake-initrd-bytes"
    # tried initrd.lz first and it succeeded -- must not have needed the plain-initrd fallback
    assert any(a[a.index("-extract") + 1] == "/casper/initrd.lz" for a in calls)


def test_extract_casper_falls_back_to_plain_initrd(tmp_path):
    fake_run, calls = _fake_run_factory(initrd_name="/casper/initrd")
    vmlinuz, initrd = boot_test.extract_casper(
        tmp_path / "lindos.iso", tmp_path / "out",
        which=lambda name: "/usr/bin/xorriso" if name == "xorriso" else None,
        run=fake_run,
    )
    assert initrd.read_bytes() == b"fake-initrd-bytes"
    sources = [a[a.index("-extract") + 1] for a in calls if "-extract" in a]
    assert "/casper/initrd.lz" in sources and "/casper/initrd" in sources


def test_extract_casper_requires_xorriso(tmp_path):
    with pytest.raises(RuntimeError, match="xorriso"):
        boot_test.extract_casper(tmp_path / "x.iso", tmp_path / "out", which=lambda _n: None,
                                 run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))


def test_extract_casper_raises_when_neither_initrd_found(tmp_path):
    def fake_run(argv, **kwargs):
        if argv[argv.index("-extract") + 1] == "/casper/vmlinuz":
            Path(argv[argv.index("-extract") + 2]).write_bytes(b"k")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    with pytest.raises(RuntimeError, match="initrd"):
        boot_test.extract_casper(tmp_path / "x.iso", tmp_path / "out",
                                 which=lambda _n: "/usr/bin/xorriso", run=fake_run)


def test_extract_casper_raises_on_empty_vmlinuz(tmp_path):
    def fake_run(argv, **kwargs):
        dest = Path(argv[argv.index("-extract") + 2])
        dest.touch()  # exists but empty
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(RuntimeError, match="vmlinuz"):
        boot_test.extract_casper(tmp_path / "x.iso", tmp_path / "out",
                                 which=lambda _n: "/usr/bin/xorriso", run=fake_run)


# --------------------------------------------------------------------------- #
# main() argument handling (usage errors only -- never launches real QEMU)
# --------------------------------------------------------------------------- #


def test_main_no_iso_match_is_usage_error(tmp_path, capsys):
    rc = boot_test.main(["--iso", str(tmp_path / "no-such-*.iso"), "--out-dir", str(tmp_path / "out")])
    assert rc == 2
    assert "no ISO matched" in capsys.readouterr().err
