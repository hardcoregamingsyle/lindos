"""Hermetic tests for build/qa/boot_test.py's pure logic (no real QEMU/xorriso/network).

Covers: serial-log parsing (the CHECK/INFO/FAILED_UNIT/DONE sentinel grammar the guest-side
``ci-boot-smoke-test.sh`` emits), the QEMU argv builder (systemd.run= path, KVM/TCG fallback,
screendump-friendly VGA choice), and the extraction helper against injected which/run callables
so it never shells out for real. Works on Windows and Linux.
"""
from __future__ import annotations

import json
import re
import shutil
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
LINDOS_DOCTOR_FAIL id=wine msg=wine not found (fix: apt install wine)
LINDOS_CHECK lindos-ram=FAIL rc=1
LINDOS_FAIL_LOG lindos-ram: Traceback (most recent call last):
LINDOS_FAIL_LOG lindos-ram: RuntimeError: /proc/meminfo unreadable
LINDOS_INFO failed_units=1
LINDOS_FAILED_UNIT some.service loaded failed failed Some Service
LINDOS_INFO is_system_running=degraded
LINDOS_DESKTOP_WATCH_STARTED
LINDOS_DESKTOP_READY
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
    assert report["doctor_fails"] == ["id=wine msg=wine not found (fix: apt install wine)"]
    assert report["fail_logs"] == [
        "lindos-ram: Traceback (most recent call last):",
        "lindos-ram: RuntimeError: /proc/meminfo unreadable",
    ]
    assert report["desktop_ready_line"] == "LINDOS_DESKTOP_READY"
    assert report["desktop_watch_started"] is True


def test_parse_report_desktop_ready_timeout_sentinel(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(
        "LINDOS_SMOKE_START\nLINDOS_DESKTOP_READY_TIMEOUT\nLINDOS_SMOKE_DONE rc=0\n",
        encoding="utf-8",
    )
    report = boot_test.parse_report(log)
    assert report["desktop_ready_line"] == "LINDOS_DESKTOP_READY_TIMEOUT"


def test_parse_report_no_desktop_ready_line(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\nLINDOS_SMOKE_DONE rc=0\n", encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["desktop_ready_line"] is None
    assert report["desktop_watch_started"] is False
    assert report["doctor_fails"] == []
    assert report["fail_logs"] == []


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


def test_tail_for_desktop_ready_finds_ready_sentinel(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_DONE rc=0\n", encoding="utf-8")

    calls = {"n": 0}
    real_sleep = boot_test.time.sleep

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            with log.open("a", encoding="utf-8") as fh:
                fh.write("LINDOS_DESKTOP_READY\n")

    boot_test.time.sleep = fake_sleep
    try:
        line = boot_test.tail_for_desktop_ready(log, timeout=5)
    finally:
        boot_test.time.sleep = real_sleep
    assert line == "LINDOS_DESKTOP_READY"


def test_tail_for_desktop_ready_accepts_the_watchers_own_timeout_sentinel(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_DONE rc=0\nLINDOS_DESKTOP_READY_TIMEOUT\n", encoding="utf-8")
    line = boot_test.tail_for_desktop_ready(log, timeout=5)
    assert line == "LINDOS_DESKTOP_READY_TIMEOUT"


def test_tail_for_desktop_ready_times_out_when_watcher_never_ran_at_all(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_DONE rc=0\n", encoding="utf-8")
    line = boot_test.tail_for_desktop_ready(log, timeout=0.01)
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


# --------------------------------------------------------------------------- #
# ci-boot-smoke-test.sh's check() / check_compat_doctor() -- the guest-side
# helpers this same file's serial-log parser above consumes. Sourced from the
# real script (never re-typed) and driven with fake commands/fake JSON, so a
# real bash is required but no real lindos-*/QEMU tooling is.
#
# This exists because of a real, shipped bug: check() used
# `if "$@" ...; then ok; fi; local rc=$?` -- when that condition is false,
# POSIX defines the *if statement's own* exit status as 0 (no branch ran),
# which clobbers $? back to 0 right before it's read. Every failing check
# silently reported rc=0, which both hid genuine failures (default --ok "0"
# always "matched") and made every --ok'd check look like a failure (the
# real non-zero code could never match the whitelist). It only surfaced on
# real Linux (see CI-LOGS.md), never in bash -n/shellcheck.
# --------------------------------------------------------------------------- #

SMOKE_SCRIPT = (HERE.parent.parent / "packages" / "lindos-core" / "root" / "usr" / "libexec"
                / "lindos" / "qa" / "ci-boot-smoke-test.sh")
BASH = shutil.which("bash")


def _extract_function(script_text: str, name: str) -> str:
    """Pull one `name() { ... }` function body out of the real script by its source text, so
    the test exercises the exact shipped implementation instead of a re-typed copy. Relies on
    the script's own style: the closing brace is un-indented on its own line."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?\n)^\}}\n", script_text, re.M | re.S)
    assert m, f"could not find function {name}() in {SMOKE_SCRIPT}"
    return f"{name}() {{\n{m.group(1)}}}\n"


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_captures_real_exit_code_not_zero(tmp_path):
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\nRC=0\n"
        + _extract_function(script_text, "check")
        + "\n"
        "check truly-ok true\n"
        "check truly-fail bash -c 'exit 5'\n"
        "check allowed-3 --ok 3 bash -c 'exit 3'\n"
        "check allowed-3-but-got-7 --ok 3 bash -c 'exit 7'\n"
        'echo "FINAL_RC=${RC}"\n',
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "LINDOS_CHECK truly-ok=OK\n" in out
    # The regression: these must carry the command's REAL exit code, never rc=0.
    assert "LINDOS_CHECK truly-fail=FAIL rc=5" in out
    assert "LINDOS_CHECK allowed-3=OK rc=3" in out
    assert "LINDOS_CHECK allowed-3-but-got-7=FAIL rc=7" in out
    assert "FINAL_RC=1" in out


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_fail_prints_tail_of_its_own_log(tmp_path):
    """A failing check's own captured output never leaves the guest any other way -- the CI
    artifact only ever carries serial.log -- so check() must tail it straight to the console,
    prefixed so build_test.py's parser can tell it apart from everything else."""
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    harness = tmp_path / "harness.sh"
    noisy = tmp_path / "noisy.sh"
    noisy.write_text(
        "#!/bin/bash\nfor i in $(seq 1 15); do echo \"line ${i}\"; done\nexit 3\n", encoding="utf-8",
    )
    noisy.chmod(0o755)
    harness.write_text(
        "#!/bin/bash\nRC=0\n" + _extract_function(script_text, "check") + "\n"
        f'check noisy-fail "{noisy.as_posix()}"\n'
        'echo "FINAL_RC=${RC}"\n',
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "LINDOS_CHECK noisy-fail=FAIL rc=3" in out
    # Only the last 10 of the 15 lines (a tail, not the whole log), each correctly prefixed.
    assert "LINDOS_FAIL_LOG noisy-fail: line 6" in out
    assert "LINDOS_FAIL_LOG noisy-fail: line 15" in out
    assert "LINDOS_FAIL_LOG noisy-fail: line 1\n" not in out and "LINDOS_FAIL_LOG noisy-fail: line 5\n" not in out
    assert "FINAL_RC=1" in out


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_all_pass_gives_rc_zero(tmp_path):
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\nRC=0\n"
        + _extract_function(script_text, "check")
        + "\ncheck ok-one true\ncheck ok-two --ok 3 true\n"
        'echo "FINAL_RC=${RC}"\n',
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    assert "LINDOS_CHECK ok-one=OK\n" in res.stdout
    assert "LINDOS_CHECK ok-two=OK\n" in res.stdout
    assert "FINAL_RC=0" in res.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_compat_doctor_excuses_only_display(tmp_path):
    """lindos-compat doctor's 'required' DISPLAY check genuinely fails when this smoke test runs
    as an early-boot systemd.run= oneshot with no logged-in desktop session -- that's expected,
    not a bug, so check_compat_doctor() must still report OK. Any OTHER required check failing
    must still be a real FAIL."""
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    def doctor_report(extra_bad_required: bool) -> str:
        checks = [
            {"id": "core", "level": "required", "ok": True},
            {"id": "wine", "level": "required", "ok": True},
            {"id": "prefixes-dir", "level": "required", "ok": True},
            {"id": "display", "level": "required", "ok": False},
            {"id": "wine32", "level": "recommended", "ok": False},
        ]
        if extra_bad_required:
            checks.append({"id": "core", "level": "required", "ok": False,
                          "detail": "python module 'lindos' not found", "fix": "apt install lindos-core"})
        return json.dumps({"ok": False, "summary": {}, "checks": checks})

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_doctor = fake_bin / "lindos-compat"
    fake_doctor.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "doctor" ]; then cat "{tmp_path.as_posix()}/report.json"; exit 1; fi\n',
        encoding="utf-8",
    )
    fake_doctor.chmod(0o755)

    def run_it(extra_bad_required: bool) -> subprocess.CompletedProcess:
        (tmp_path / "report.json").write_text(doctor_report(extra_bad_required), encoding="utf-8")
        harness = tmp_path / "harness.sh"
        body = _extract_function(script_text, "check_compat_doctor")
        body = body.replace("/usr/bin/lindos-compat", str(fake_doctor.as_posix()))
        # The real script hard-codes /tmp/... (a real path on the real Linux boot target this
        # runs on). On a Windows dev host, git-bash's own /tmp alias isn't understood by the
        # native (non-MSYS) python3.exe the same way, so point the log at an ordinary absolute
        # path both sides agree on -- test portability only, the shipped script is untouched.
        body = body.replace("/tmp/lindos-smoke-lindos-compat-doctor.log",
                            (tmp_path / "doctor-check.log").as_posix())
        harness.write_text(
            "#!/bin/bash\nRC=0\n" + body + '\ncheck_compat_doctor\necho "FINAL_RC=${RC}"\n',
            encoding="utf-8",
        )
        return subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)

    only_display = run_it(extra_bad_required=False)
    assert only_display.returncode == 0, only_display.stderr
    assert "LINDOS_CHECK lindos-compat-doctor=OK rc=1" in only_display.stdout
    assert "FINAL_RC=0" in only_display.stdout

    core_also_broken = run_it(extra_bad_required=True)
    assert core_also_broken.returncode == 0, core_also_broken.stderr
    assert "LINDOS_CHECK lindos-compat-doctor=FAIL rc=1" in core_also_broken.stdout
    assert ("LINDOS_DOCTOR_FAIL id=core msg=python module 'lindos' not found "
            "(fix: apt install lindos-core)") in core_also_broken.stdout
    assert "FINAL_RC=1" in core_also_broken.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_compat_doctor_falls_back_to_log_tail_on_parse_error(tmp_path):
    """If the doctor JSON itself can't even be parsed (never seen in practice, but check_compat_
    doctor() must not go silent), it must still report SOMETHING actionable -- a raw log tail."""
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_doctor = fake_bin / "lindos-compat"
    fake_doctor.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "doctor" ]; then echo "not valid json at all"; exit 1; fi\n',
        encoding="utf-8",
    )
    fake_doctor.chmod(0o755)
    body = _extract_function(script_text, "check_compat_doctor")
    body = body.replace("/usr/bin/lindos-compat", str(fake_doctor.as_posix()))
    body = body.replace("/tmp/lindos-smoke-lindos-compat-doctor.log",
                        (tmp_path / "doctor-check.log").as_posix())
    body = body.replace("/tmp/lindos-smoke-lindos-compat-doctor.stderr.log",
                        (tmp_path / "doctor-check.stderr.log").as_posix())
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\nRC=0\n" + body + '\ncheck_compat_doctor\necho "FINAL_RC=${RC}"\n',
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    assert "LINDOS_CHECK lindos-compat-doctor=FAIL rc=1" in res.stdout
    assert "LINDOS_FAIL_LOG lindos-compat-doctor(stdout): not valid json at all" in res.stdout
    assert "FINAL_RC=1" in res.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_smoke_check_compat_doctor_never_lets_stderr_corrupt_the_json(tmp_path):
    """Regression for the exact bug seen in CI run 36286387867: a log record emitted to stderr
    during a real doctor run (logging.StreamHandler() defaults to stderr and flushes
    immediately) landed BEFORE doctor's own buffered stdout JSON when both were merged into one
    file with `2>&1`, so json.load saw the log line first and failed with "Expecting value: line
    1 column 1 (char 0)" even though the file's tail was well-formed JSON. stdout and stderr must
    be captured to separate files so the JSON stream is never corrupted by stderr timing."""
    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_doctor = fake_bin / "lindos-compat"
    # Simulates exactly the observed failure mode: a stderr line "arrives" (is written) before
    # the stdout JSON, as real unbuffered-stderr-vs-buffered-stdout timing would produce.
    fake_doctor.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "doctor" ]; then\n'
        '    echo "INFO: some incidental log message" >&2\n'
        '    echo \'{"ok": false, "summary": {}, "checks": '
        '[{"id": "wine", "level": "required", "ok": false, "detail": "wine not found", "fix": "apt install wine"}, '
        '{"id": "display", "level": "required", "ok": false}]}\'\n'
        "    exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_doctor.chmod(0o755)
    body = _extract_function(script_text, "check_compat_doctor")
    body = body.replace("/usr/bin/lindos-compat", str(fake_doctor.as_posix()))
    body = body.replace("/tmp/lindos-smoke-lindos-compat-doctor.log",
                        (tmp_path / "doctor-check.log").as_posix())
    body = body.replace("/tmp/lindos-smoke-lindos-compat-doctor.stderr.log",
                        (tmp_path / "doctor-check.stderr.log").as_posix())
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\nRC=0\n" + body + '\ncheck_compat_doctor\necho "FINAL_RC=${RC}"\n',
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    # Must parse correctly (no PARSE_ERROR) and report the real failing required check.
    assert "LINDOS_DOCTOR_PARSE_ERROR" not in res.stdout
    assert "LINDOS_DOCTOR_FAIL id=wine msg=wine not found (fix: apt install wine)" in res.stdout
    assert "LINDOS_CHECK lindos-compat-doctor=FAIL rc=1" in res.stdout
    assert "FINAL_RC=1" in res.stdout


# --------------------------------------------------------------------------- #
# start_desktop_watch()'s embedded watcher script (the guest-side detector for
# "the desktop is actually up", read by boot_test.py's tail_for_desktop_ready())
# --------------------------------------------------------------------------- #


def _extract_heredoc(script_text: str, delimiter: str) -> str:
    """Pull the literal body of a `cat > file <<'DELIM' ... DELIM` heredoc out of the real
    script, so the test exercises the exact shipped watcher instead of a re-typed copy."""
    m = re.search(rf"<<'{re.escape(delimiter)}'\n(.*?\n){re.escape(delimiter)}\n", script_text, re.S)
    assert m, f"could not find heredoc {delimiter!r} in {SMOKE_SCRIPT}"
    # The real watcher's `exec >/dev/console 2>&1` is meaningless (and /dev/console may not even
    # be writable) outside a real boot -- strip it so a plain subprocess capture works everywhere.
    return m.group(1).replace("exec >/dev/console 2>&1\n", "")


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_desktop_watch_prints_ready_once_running_and_a_session_process_exists(tmp_path):
    import os

    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    watcher = _extract_heredoc(script_text, "WATCH_EOF")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        '#!/bin/bash\nif [ "$1" = "is-system-running" ]; then echo running; exit 0; fi\n',
        encoding="utf-8",
    )
    (fake_bin / "systemctl").chmod(0o755)
    # Only lightdm "exists"; Xorg/Xwayland don't -- exercises the `||` chain, not just the first arm.
    (fake_bin / "pgrep").write_text(
        '#!/bin/bash\n[ "$2" = "lightdm" ] && exit 0\nexit 1\n', encoding="utf-8",
    )
    (fake_bin / "pgrep").chmod(0o755)
    watch_file = tmp_path / "watch.sh"
    watch_file.write_text(watcher, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    res = subprocess.run([BASH, str(watch_file)], capture_output=True, text=True, timeout=30,
                        check=False, env=env)
    assert res.returncode == 0, res.stderr
    assert "LINDOS_DESKTOP_WATCH_STARTED" in res.stdout
    assert "LINDOS_DESKTOP_READY" in res.stdout
    assert "LINDOS_DESKTOP_READY_TIMEOUT" not in res.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_desktop_watch_times_out_when_no_session_ever_appears(tmp_path):
    import os

    script_text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    watcher = _extract_heredoc(script_text, "WATCH_EOF")
    # Same logic, a much shorter bound/sleep so the test doesn't take 150 real seconds.
    watcher = watcher.replace("-lt 150", "-lt 2").replace("sleep 1", "sleep 0.01")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        '#!/bin/bash\nif [ "$1" = "is-system-running" ]; then echo starting; exit 1; fi\n',
        encoding="utf-8",
    )
    (fake_bin / "systemctl").chmod(0o755)
    (fake_bin / "pgrep").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    (fake_bin / "pgrep").chmod(0o755)
    watch_file = tmp_path / "watch.sh"
    watch_file.write_text(watcher, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    res = subprocess.run([BASH, str(watch_file)], capture_output=True, text=True, timeout=30,
                        check=False, env=env)
    assert res.returncode == 0, res.stderr
    assert "LINDOS_DESKTOP_WATCH_STARTED" in res.stdout
    assert "LINDOS_DESKTOP_READY_TIMEOUT" in res.stdout
