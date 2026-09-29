"""Hermetic tests for build/qa/menu_test.py - boot the built ISO through its OWN boot loader (no QEMU, no KVM, no ISO).

What is proven here: the QEMU command line boots ONLY the ISO (no -kernel/-initrd/-append: the boot loader must do the
work), the monitor's ``info blockstats`` counter is read correctly, the run loop (fake QEMU process + fake clock) takes the
menu frames in their window, decides 'booted' from the bytes read from the medium and stops QEMU, and the verdicts name
what went wrong (a menu that waits, a dangling default, QEMU quitting).  What only a real run shows is the job itself
(.github/workflows/ci.yml, menu-test): whether SeaBIOS/OVMF load these menus and the thresholds fit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
QA_DIR = REPO / "build" / "qa"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import install_checks as ic  # noqa: E402
import menu_test as mt  # noqa: E402

MIB = 1 << 20


def _argv(tmp_path: Path, **kw):
    base = dict(iso=tmp_path / "x.iso", serial_log=tmp_path / "s.log", monitor_sock=tmp_path / "m.sock", ram_mb=4096, cpus=2)
    base.update(kw)
    return mt.build_argv(**base)


# ============================================================================================ the QEMU command line
def test_only_the_iso_can_boot_and_the_boot_loader_does_the_work(tmp_path):
    argv = _argv(tmp_path)
    assert argv[0] == "qemu-system-x86_64"
    for flag in ("-kernel", "-initrd", "-append", "-cdrom"):
        assert flag not in argv, flag
    drive = argv[argv.index("-drive") + 1]
    assert "media=cdrom" in drive and "readonly=on" in drive and "id=cd0" in drive and str(tmp_path / "x.iso") in drive
    assert "ide-cd,drive=cd0,bootindex=0" in [argv[i + 1] for i, a in enumerate(argv) if a == "-device"]
    assert argv.count("-drive") == 1, "no other disk the firmware could boot"
    assert argv[argv.index("-vga") + 1] == "std" and argv[argv.index("-display") + 1] == "none"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-accel"] == ["kvm", "tcg"]
    assert "-no-reboot" in argv and argv[argv.index("-m") + 1] == "4096"
    assert argv[argv.index("-monitor") + 1].startswith("unix:%s," % (tmp_path / "m.sock"))


def test_uefi_boots_through_ovmf_with_its_own_variable_store(tmp_path):
    with pytest.raises(ValueError, match="OVMF"):
        _argv(tmp_path, firmware="uefi")
    argv = _argv(tmp_path, firmware="uefi", ovmf=("/c.fd", "/v.fd"))
    pflash = [argv[i + 1] for i, a in enumerate(argv) if a == "-drive" and "pflash" in argv[i + 1]]
    assert len(pflash) == 2 and "readonly=on,file=/c.fd" in pflash[0] and pflash[1].endswith("file=/v.fd")
    assert "-kernel" not in argv
    assert not any("pflash" in a for a in _argv(tmp_path))                   # SeaBIOS: no firmware drives


def test_no_vm_detection_evasion_in_the_harness():
    """Honesty rules (SPEC 0.1 / SPEC-VM 20): the guest is an ordinary, honest QEMU machine."""
    text = (QA_DIR / "menu_test.py").read_text(encoding="utf-8").lower() + (QA_DIR / "menu_checks.py").read_text(encoding="utf-8").lower()
    for token in ("kvm=off", "hv-vendor-id", "hv_vendor_id", "-smbios", "acpitable", "hidden state"):
        assert token not in text, token


# ============================================================================================ the monitor's counter
@pytest.mark.parametrize("text, want", [
    ("cd0: rd_bytes=3407872 wr_bytes=0 rd_operations=1053 wr_operations=0 flush_operations=0 wr_total_time_ns=0", 3407872),
    ("cd0 (#block123): rd_bytes=250000000 wr_bytes=0\r\n(qemu) ", 250000000),
    ("a: rd_bytes=5 wr_bytes=0\nb: rd_bytes=900 wr_bytes=0\n", 900),                     # the largest: never double-counted
    ("(qemu) info blockstats\r\n(qemu) ", None),
    ("", None),
])
def test_parse_blockstats(text, want):
    assert mt.parse_blockstats(text) == want


# ============================================================================================ the run loop
class FakeProc:
    def __init__(self, exit_after=None):
        self.polls = 0
        self.exit_after = exit_after
        self.returncode = None
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def monotonic(self):
        return self.now


def _run(tmp_path, proc, reads, **kw):
    """run_menu_boot against a fake QEMU, a fake clock, fake screenshots and a scripted I/O counter (read at each poll)."""
    clock = Clock()
    taken: List[str] = []
    counter = iter(reads)

    def shoot(sock, png):
        taken.append(png.name)
        png.write_bytes(b"png")
        return True

    kw.setdefault("poll", 3.0)
    result = mt.run_menu_boot(["qemu"], monitor_sock=tmp_path / "m", shots_dir=tmp_path / "shots", popen=lambda argv: proc,
                              sleep=clock.sleep, monotonic=clock.monotonic, shoot=shoot, read_stats=lambda sock: next(counter, reads[-1]), **kw)
    return result, taken, clock


@pytest.fixture(autouse=True)
def _frames_have_content(monkeypatch):
    monkeypatch.setattr(mt.bt, "screenshot_has_content", lambda path: (True, "1024x768, 400 colours"))


def test_a_boot_that_reads_enough_of_the_medium_is_booted_and_stops_qemu(tmp_path):
    proc = FakeProc()
    reads = [0, 1 * MIB, 40 * MIB] + [230 * MIB] * 50
    result, taken, clock = _run(tmp_path, proc, reads, settle=10.0, min_read_bytes=200 * MIB)
    assert result["outcome"] == "booted" and result["read_bytes"] == 230 * MIB and result["stats_seen"]
    assert proc.terminated, "QEMU is stopped when the verdict is in"
    assert result["reached_at"] == 9 and result["seconds"] >= 19                # the counter passed the threshold at the 4th poll (9 s), then 10 s to draw
    assert taken[-1] == "boot-final.png" and result["final"]["has_content"] is True
    menu_frames = [n for n in taken if n.startswith("menu-")]
    assert menu_frames and len(menu_frames) == len(result["frames"])
    assert all(f["at"] <= 48 for f in result["frames"]), "menu frames are only taken in the menu window"


def test_a_menu_that_never_boots_times_out_with_a_small_counter(tmp_path):
    """A menu waiting for a key, a bare 'boot:' prompt, a missing kernel: a few MiB are read and nothing more."""
    result, taken, clock = _run(tmp_path, FakeProc(), [2 * MIB], timeout=60.0, min_read_bytes=200 * MIB)
    assert result["outcome"] == "timeout" and result["read_bytes"] == 2 * MIB and result["reached_at"] is None
    assert taken[-1] == "boot-final.png"
    findings = mt.judge_menu_boot(result, firmware="bios")
    bad = {f.name: f.detail for f in ic.failures(findings)}
    assert "menu-boot-bios-default-boot" in bad and "did not boot" in bad["menu-boot-bios-default-boot"] and "2 MiB" in bad["menu-boot-bios-default-boot"]


def test_qemu_quitting_by_itself_is_reported(tmp_path):
    result, taken, _ = _run(tmp_path, FakeProc(exit_after=4), [10 * MIB])
    assert result["outcome"] == "exited" and result["final"] is None
    assert "crashed or rebooted" in {f.name: f.detail for f in ic.failures(mt.judge_menu_boot(result, firmware="uefi"))}["menu-boot-uefi-default-boot"]


def test_without_an_io_counter_the_last_screenshot_is_all_there_is(tmp_path):
    proc = FakeProc()
    clock = Clock()

    def shoot(sock, png):
        png.write_bytes(b"png")
        return True

    result = mt.run_menu_boot(["qemu"], monitor_sock=tmp_path / "m", shots_dir=tmp_path / "shots", timeout=30.0, poll=5.0, popen=lambda a: proc,
                              sleep=clock.sleep, monotonic=clock.monotonic, shoot=shoot, read_stats=lambda s: None)
    assert result["outcome"] == "timeout" and result["stats_seen"] is False and result["read_bytes"] is None
    findings = mt.judge_menu_boot(result, firmware="bios")
    assert not ic.failures(findings) and [f.level for f in findings if f.name == "menu-boot-bios-default-boot"] == [ic.WARN]


# ============================================================================================ the verdicts
def _result(**kw):
    base = {"outcome": "booted", "seconds": 100, "read_bytes": 300 * MIB, "stats_seen": True, "reached_at": 60,
            "frames": [{"name": "menu-01.png", "at": 6, "has_content": True, "stats": "1024x768, 400 colours"}],
            "final": {"name": "boot-final.png", "at": 90, "has_content": True, "stats": "1024x768, 300 colours"}}
    base.update(kw)
    return base


def test_a_good_boot_passes_and_says_what_it_proves():
    findings = mt.judge_menu_boot(_result(), firmware="bios")
    assert not ic.failures(findings) and not ic.warnings(findings)
    assert {f.name for f in findings} == {"menu-boot-bios-menu", "menu-boot-bios-default-boot", "menu-boot-bios-screen"}
    boot = next(f for f in findings if f.name.endswith("default-boot"))
    assert "300 MiB" in boot.detail and "casper" in boot.detail


def test_a_blank_menu_and_a_blank_final_screen_are_warnings_never_failures():
    blank = {"name": "menu-01.png", "at": 6, "has_content": False, "stats": "1 colour"}
    findings = mt.judge_menu_boot(_result(frames=[blank], final=dict(blank, name="boot-final.png", at=90)), firmware="uefi")
    assert not ic.failures(findings)
    assert {f.name for f in ic.warnings(findings)} == {"menu-boot-uefi-menu", "menu-boot-uefi-screen"}


def test_unjudgeable_or_missing_menu_frames_are_notes():
    unknown = [{"name": "menu-01.png", "at": 6, "has_content": None, "stats": "Pillow not installed"}]
    assert [f.level for f in mt.judge_menu_boot(_result(frames=unknown), firmware="bios") if f.name.endswith("-menu")] == [ic.INFO]
    assert [f.level for f in mt.judge_menu_boot(_result(frames=[]), firmware="bios") if f.name.endswith("-menu")] == [ic.INFO]


# ============================================================================================ command line
def test_argument_defaults_and_choices():
    ns = mt.build_parser().parse_args(["--iso", "x", "--out-dir", "y"])
    assert (ns.firmware, ns.ram, ns.cpus, ns.timeout, ns.settle, ns.min_read_mb, ns.allow_tcg) == ("bios", 4096, 2, 420, 25, 200, False)
    assert mt.DEFAULT_MIN_READ_MB == 200
    with pytest.raises(SystemExit):
        mt.build_parser().parse_args(["--iso", "x", "--out-dir", "y", "--firmware", "ppc"])


def test_main_usage_errors(tmp_path, monkeypatch, capsys):
    assert mt.main(["--iso", str(tmp_path / "none-*.iso"), "--out-dir", str(tmp_path / "o")]) == 2
    assert "no ISO matched" in capsys.readouterr().err
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    monkeypatch.setattr(mt.shutil, "which", lambda n: None)
    assert mt.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o")]) == 2 and "qemu-system-x86_64 not found" in capsys.readouterr().err
    monkeypatch.setattr(mt.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(mt.os, "access", lambda *a, **k: False)
    assert mt.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o")]) == 2 and "/dev/kvm" in capsys.readouterr().err
    monkeypatch.setattr(mt.it, "find_ovmf", lambda: None)
    assert mt.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o"), "--allow-tcg", "--firmware", "uefi"]) == 2
    assert "OVMF" in capsys.readouterr().err


def test_main_runs_the_boot_writes_the_report_and_maps_the_verdict(tmp_path, monkeypatch, capsys):
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    seen = {}
    monkeypatch.setattr(mt.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(mt.os, "access", lambda *a, **k: True)
    ovmf_code, ovmf_vars = tmp_path / "CODE.fd", tmp_path / "VARS.fd"
    ovmf_code.write_bytes(b"c")
    ovmf_vars.write_bytes(b"v")
    monkeypatch.setattr(mt.it, "find_ovmf", lambda: (str(ovmf_code), str(ovmf_vars)))

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _result()

    monkeypatch.setattr(mt, "run_menu_boot", fake_run)
    out_dir = (tmp_path / "o").resolve()
    assert mt.main(["--iso", str(iso), "--out-dir", str(out_dir), "--firmware", "uefi", "--min-read-mb", "150"]) == 0
    assert seen["kw"]["min_read_bytes"] == 150 * MIB and seen["kw"]["shots_dir"] == out_dir / "uefi"
    # the firmware's variable store is a COPY in the out dir, never the system file
    pflash = [a for a in seen["argv"] if "pflash" in a and "unit=1" in a][0]
    assert str(out_dir / "OVMF_VARS-uefi.fd") in pflash and str(ovmf_vars) not in pflash
    report = (out_dir / "report-uefi.txt").read_text(encoding="utf-8")
    assert "===== PASS =====" in report and "300 MiB read from the medium" in report and "===== PASS =====" in capsys.readouterr().out
    monkeypatch.setattr(mt, "run_menu_boot", lambda argv, **kw: _result(outcome="timeout", read_bytes=3 * MIB, final=None))
    assert mt.main(["--iso", str(iso), "--out-dir", str(out_dir), "--firmware", "bios"]) == 1
    assert "===== FAIL =====" in (out_dir / "report-bios.txt").read_text(encoding="utf-8")


# ============================================================================================ ci.yml wiring
def _yaml():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def test_the_menu_test_job_boots_both_firmwares_and_never_fails_the_run():
    doc = _yaml()
    job = doc["jobs"]["menu-test"]
    assert job["needs"] == "iso" and job["continue-on-error"] is True
    cond = " ".join(str(job["if"]).split())
    for part in ("needs.iso.result == 'success'", "github.event_name == 'workflow_dispatch'", "inputs.build_iso", "inputs.menu_test"):
        assert part in cond
    inputs = (doc.get(True) or doc.get("on"))["workflow_dispatch"]["inputs"]
    assert inputs["menu_test"]["type"] == "boolean" and inputs["menu_test"]["default"] is True
    runs = [s for s in job["steps"] if "python3 build/qa/menu_test.py" in str(s.get("run", ""))]
    assert [("--firmware bios" in s["run"], "--firmware uefi" in s["run"]) for s in runs] == [(True, False), (False, True)]
    assert runs[1]["if"] == "always()", "a failed BIOS boot must not skip the UEFI one"
    text = CI_YML.read_text(encoding="utf-8")
    seg = text[text.index("  menu-test:"):]
    for tool in ("qemu-system-x86", "xorriso", "python3-pil", "ovmf", "/dev/kvm"):
        assert tool in seg, tool
    upload = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert upload and upload[0]["if"] == "always()" and upload[0]["with"]["name"] == "lindos-menu-test"
    assert "OVMF_VARS" not in upload[0]["with"]["path"] and "monitor" not in upload[0]["with"]["path"]


def test_the_menu_test_flags_used_by_the_workflow_exist():
    text = CI_YML.read_text(encoding="utf-8")
    seg = text[text.index("  menu-test:"):]
    src = (QA_DIR / "menu_test.py").read_text(encoding="utf-8")
    known = set(re.findall(r'add_argument\("--([a-z0-9-]+)"', src))
    used = set(re.findall(r"menu_test\.py[^\n]*", seg))
    flags = set(re.findall(r"--([a-z0-9-]+)", " ".join(used)))
    assert flags and flags <= known, flags - known


def test_no_job_depends_on_the_menu_or_offline_jobs():
    for name, job in _yaml()["jobs"].items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert "menu-test" not in needs and "install-test-offline" not in needs, name
