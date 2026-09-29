"""Tests of the LIVE-SESSION part of build/qa/boot_test.py (hermetic; no QEMU).

The live session is only the installer now (docs/BUILDING.md "Installer flow"): it no longer runs the first-run
wizard, so a working desktop can no longer be told from the wizard's window in the screenshot.  It is proven by
the guest-side checks of packages/lindos-core/.../qa/ci-live-checks.sh (no wizard, the "Install Lindos" launcher,
panel/desktop running, no sleep, nothing installing) and by a screenshot that must not be blank.  These tests pin
how the harness parses and judges all of that; test_ci_guest_scripts.py runs the guest script itself.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
QA_DIR = HERE.parent / "qa"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import boot_test  # noqa: E402

LIVE_CHECKS_OK = "".join(f"LINDOS_CHECK {name}=OK\n" for name in boot_test.REQUIRED_LIVE_CHECKS)

GOOD_LIVE_SERIAL = (
    "LINDOS_SMOKE_START\nLINDOS_INFO uname=6.14.0-lindos\nLINDOS_CHECK python-import=OK\nLINDOS_SMOKE_DONE rc=0\n"
    "LINDOS_DESKTOP_WATCH_STARTED\nLINDOS_DESKTOP_READY\nLINDOS_LIVE_WATCH_STARTED\n" + LIVE_CHECKS_OK
    + "LINDOS_LIVE_CHECKS_DONE fails=0\n"
)


# ---- parsing -------------------------------------------------------------------------------------------------

def test_parse_report_reads_the_live_watcher_lines(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(
        GOOD_LIVE_SERIAL.replace("LINDOS_LIVE_CHECKS_DONE fails=0\n",
                                 "LINDOS_LIVE_DIAG ps: root 1 systemd\nLINDOS_LIVE_CHECKS_DONE fails=1\n"),
        encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["live_watch_started"] is True and report["live_checks_done"] is True
    assert report["live_fails"] == 1 and report["live_diag"] == ["ps: root 1 systemd"]
    for name in boot_test.REQUIRED_LIVE_CHECKS:
        assert report["checks"][name] == {"status": "OK", "rc": None}


def test_parse_report_without_a_live_watcher(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\nLINDOS_SMOKE_DONE rc=0\n", encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["live_watch_started"] is False and report["live_checks_done"] is False
    assert report["live_fails"] is None and report["live_diag"] == []


def test_live_checks_done_without_a_fail_count_still_counts(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_LIVE_CHECKS_DONE\n", encoding="utf-8")
    report = boot_test.parse_report(log)
    assert report["live_checks_done"] is True and report["live_fails"] is None


def test_tail_for_live_checks(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_LIVE_WATCH_STARTED\nLINDOS_LIVE_CHECKS_DONE fails=0\n", encoding="utf-8")
    assert boot_test.tail_for_live_checks(log, timeout=5) == "LINDOS_LIVE_CHECKS_DONE fails=0"
    log.write_text("LINDOS_LIVE_WATCH_STARTED\n", encoding="utf-8")
    assert boot_test.tail_for_live_checks(log, timeout=0.01) is None


# ---- the verdict ---------------------------------------------------------------------------------------------

def test_live_verdict_passes_only_when_every_required_check_is_ok_and_reported(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(GOOD_LIVE_SERIAL, encoding="utf-8")
    assert boot_test.live_verdict(boot_test.parse_report(log)) == []


def test_live_verdict_names_a_check_that_never_ran(tmp_path):
    """Silence must fail: a desktop that never came up prints nothing, and must not pass by saying nothing."""
    log = tmp_path / "serial.log"
    log.write_text("LINDOS_SMOKE_START\nLINDOS_SMOKE_DONE rc=0\nLINDOS_DESKTOP_READY_TIMEOUT\n", encoding="utf-8")
    problems = boot_test.live_verdict(boot_test.parse_report(log))
    assert all(f"live check {name} never reported" in problems for name in boot_test.REQUIRED_LIVE_CHECKS)
    assert any("watcher never started" in p for p in problems)


def test_live_verdict_reports_the_guests_reason_for_a_failed_check(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(
        GOOD_LIVE_SERIAL.replace("LINDOS_CHECK live-no-oobe=OK",
                                 "LINDOS_CHECK live-no-oobe=FAIL rc=1\nLINDOS_FAIL_LOG live-no-oobe: the first-run wizard runs"),
        encoding="utf-8")
    problems = boot_test.live_verdict(boot_test.parse_report(log))
    assert problems == ["live check live-no-oobe FAILED: the first-run wizard runs"]


def test_live_verdict_fails_a_watcher_that_started_but_never_finished(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(GOOD_LIVE_SERIAL.replace("LINDOS_LIVE_CHECKS_DONE fails=0\n", ""), encoding="utf-8")
    assert boot_test.live_verdict(boot_test.parse_report(log)) == [
        "the live-session watcher never printed LINDOS_LIVE_CHECKS_DONE (the desktop never came up, or the guest died first)"]


def test_the_required_live_checks_are_the_ones_the_guest_script_prints():
    script = (HERE.parent.parent / "packages" / "lindos-core" / "root" / "usr" / "libexec" / "lindos" / "qa"
              / "ci-live-checks.sh").read_text(encoding="utf-8")
    assert set(re.findall(r"verdict (live-[a-z-]+) ", script)) == set(boot_test.REQUIRED_LIVE_CHECKS)


# ---- the kernel command line ---------------------------------------------------------------------------------

def test_the_kernel_command_line_mirrors_the_try_lindos_boot_entry(tmp_path):
    """The live boot test must go through casper's preseed exactly like a real 'Try Lindos' boot."""
    sys.path.insert(0, str(HERE.parent / "lib"))
    import boot_menu

    grub = (HERE.parent / "overlay" / "boot" / "grub" / "grub.cfg").read_text(encoding="utf-8").replace("@PRESEED@", "")
    try_entry = next(e for e in boot_menu.parse_grub_entries(grub)
                     if e.title.startswith("Try Lindos") and "nomodeset" not in e.args)
    argv = boot_test.build_qemu_argv(vmlinuz=tmp_path / "v", initrd=tmp_path / "i", iso=tmp_path / "x.iso",
                                     serial_log=tmp_path / "s", monitor_sock=tmp_path / "m", ram_mb=4096, cpus=2)
    append = argv[argv.index("-append") + 1]
    words = append.split()
    for w in boot_menu.isolinux_words(try_entry):
        if w not in ("quiet", "splash"):
            assert w in words, w
    assert "only-ubiquity" not in words                       # the live session is the desktop, not the installer-only boot
    assert "username=liveuser hostname=lindos" in append      # pinned by lindos-desktop's test_rebrand as well


# ---- the screenshot must be a picture of something -----------------------------------------------------------

def _png(path: Path, pixel) -> Path:
    Image = pytest.importorskip("PIL.Image")
    img = Image.new("RGB", (640, 480))
    img.putdata([pixel(x, y) for y in range(480) for x in range(640)])
    img.save(path)
    return path


def _busy(x, y):
    return (x * 255 // 640, y * 255 // 480, (x + y) % 256)


def test_screenshot_content_check_rejects_black_and_single_colour_frames(tmp_path):
    black = _png(tmp_path / "black.png", lambda x, y: (0, 0, 0))
    grey = _png(tmp_path / "grey.png", lambda x, y: (40, 40, 40))
    for path in (black, grey):
        verdict, stats = boot_test.screenshot_has_content(path)
        assert verdict is False and "1 colours" in stats


def test_screenshot_content_check_rejects_a_mostly_black_placeholder(tmp_path):
    """QEMU's 'Guest has not initialized the display' picture: black with a little text - few colours."""
    def px(x, y):
        return (255, 255, 255) if (20 <= y < 30 and 20 <= x < 200 and (x // 3) % 2 == 0) else (0, 0, 0)

    verdict, _ = boot_test.screenshot_has_content(_png(tmp_path / "placeholder.png", px))
    assert verdict is False


def test_screenshot_content_check_accepts_a_desktop_like_picture(tmp_path):
    def px(x, y):
        if y > 440:                                                   # a panel
            return (30 + x % 40, 30 + (x // 3) % 40, 60)
        return _busy(x, y)                                            # a wallpaper with a gradient and detail

    verdict, stats = boot_test.screenshot_has_content(_png(tmp_path / "desktop.png", px))
    assert verdict is True and "640x480" in stats


def test_screenshot_content_check_cannot_judge_a_missing_file(tmp_path):
    pytest.importorskip("PIL")
    verdict, why = boot_test.screenshot_has_content(tmp_path / "absent.png")
    assert verdict is None and "could not read" in why


def test_screenshot_content_check_without_pillow(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert boot_test.screenshot_has_content(tmp_path / "x.png") == (None, "Pillow not installed")


class FakeMonitor:
    instances: list = []

    def __init__(self, sock_path, **kw):
        self.commands: list = []
        self.closed = False
        FakeMonitor.instances.append(self)

    def command(self, cmd, **kw):
        self.commands.append(cmd)
        if cmd.startswith("screendump "):
            Image = pytest.importorskip("PIL.Image")
            Image.new("RGB", (64, 48), (10, 20, 30)).save(cmd.split(" ", 1)[1], format="PPM")
        return b""

    def close(self):
        self.closed = True


@pytest.mark.parametrize("quit_after, expect_quit", [(True, True), (False, False)])
def test_take_screenshot_quits_qemu_only_when_asked(tmp_path, monkeypatch, quit_after, expect_quit):
    """The boot test ends QEMU with its screenshot; the install test takes progress pictures of a running install."""
    pytest.importorskip("PIL")
    FakeMonitor.instances = []
    monkeypatch.setattr(boot_test, "MonitorClient", FakeMonitor)
    monkeypatch.setattr(boot_test.time, "sleep", lambda s: None)
    out = tmp_path / "shot.png"
    assert boot_test.take_screenshot(tmp_path / "sock", out, quit_after=quit_after) is True
    mon = FakeMonitor.instances[0]
    assert out.exists() and not out.with_suffix(".ppm").exists() and mon.closed
    assert ("quit" in mon.commands) is expect_quit


def test_take_screenshot_defaults_to_quitting(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    FakeMonitor.instances = []
    monkeypatch.setattr(boot_test, "MonitorClient", FakeMonitor)
    monkeypatch.setattr(boot_test.time, "sleep", lambda s: None)
    assert boot_test.take_screenshot(tmp_path / "sock", tmp_path / "s.png") is True
    assert "quit" in FakeMonitor.instances[0].commands


# ---- main(): the verdict, with a fake QEMU that "boots" by writing a serial log --------------------------------

class _FakeQemu:
    def __init__(self, serial: Path, text: str):
        serial.write_text(text, encoding="utf-8")

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _run_main(tmp_path, monkeypatch, capsys, serial_text, *, pixel, extra=()):
    pytest.importorskip("PIL")
    iso = tmp_path / "lindos-1.0.iso"
    iso.write_bytes(b"x")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(boot_test, "extract_casper", lambda iso_, dest: (dest / "vmlinuz", dest / "initrd"))
    monkeypatch.setattr(boot_test.time, "sleep", lambda s: None)

    def popen(argv):
        serial = Path(argv[argv.index("-serial") + 1].split(":", 1)[1])
        return _FakeQemu(serial, serial_text)

    monkeypatch.setattr(boot_test.subprocess, "Popen", popen)

    def shot(sock, png, quit_after=True):
        _png(png, pixel)
        return True

    monkeypatch.setattr(boot_test, "take_screenshot", shot)
    rc = boot_test.main(["--iso", str(iso), "--out-dir", str(out_dir), "--grace", "0", "--desktop-timeout", "1",
                         "--live-timeout", "1", *extra])
    return rc, capsys.readouterr().out


def test_main_passes_a_boot_with_a_working_live_desktop(tmp_path, monkeypatch, capsys):
    rc, out = _run_main(tmp_path, monkeypatch, capsys, GOOD_LIVE_SERIAL, pixel=_busy)
    assert rc == 0, out
    assert "[ok  ] live-no-oobe" in out and "live-session watcher: started=True done=True fails=0" in out
    assert "content check: ok" in out and "===== PASS =====" in out


def test_main_fails_when_the_live_desktop_never_reports(tmp_path, monkeypatch, capsys):
    silent = "LINDOS_SMOKE_START\nLINDOS_CHECK python-import=OK\nLINDOS_SMOKE_DONE rc=0\nLINDOS_DESKTOP_READY_TIMEOUT\n"
    rc, out = _run_main(tmp_path, monkeypatch, capsys, silent, pixel=_busy)
    assert rc == 1 and "FAIL: live check live-panel never reported" in out and "===== FAIL =====" in out


def test_main_fails_a_wizard_in_the_live_session(tmp_path, monkeypatch, capsys):
    bad = GOOD_LIVE_SERIAL.replace("LINDOS_CHECK live-no-oobe=OK",
                                   "LINDOS_CHECK live-no-oobe=FAIL rc=1\nLINDOS_FAIL_LOG live-no-oobe: wizard runs")
    rc, out = _run_main(tmp_path, monkeypatch, capsys, bad, pixel=_busy)
    assert rc == 1 and "FAIL: live check live-no-oobe FAILED: wizard runs" in out


def test_main_fails_a_blank_screenshot_unless_allowed(tmp_path, monkeypatch, capsys):
    rc, out = _run_main(tmp_path, monkeypatch, capsys, GOOD_LIVE_SERIAL, pixel=lambda x, y: (0, 0, 0))
    assert rc == 1 and "the screenshot is blank" in out and "content check: BLANK" in out
    rc, out = _run_main(tmp_path, monkeypatch, capsys, GOOD_LIVE_SERIAL, pixel=lambda x, y: (0, 0, 0),
                        extra=("--allow-blank-screenshot",))
    assert rc == 0


def test_main_can_skip_the_live_checks_for_an_older_iso(tmp_path, monkeypatch, capsys):
    old = "LINDOS_SMOKE_START\nLINDOS_CHECK python-import=OK\nLINDOS_SMOKE_DONE rc=0\nLINDOS_DESKTOP_READY\n"
    rc, out = _run_main(tmp_path, monkeypatch, capsys, old, pixel=_busy, extra=("--no-live-checks",))
    assert rc == 0, out
