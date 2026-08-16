"""``lindos-tune status`` — fake /proc, /sys/block/zram0, cpufreq and unit states in a tmp
LINDOS_ROOT → snapshot values, verdict line, JSON/text rendering; plus ``report``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import common, report as lreport, status
from lindos_tune.common import CmdResult

MEMINFO = """MemTotal:        7864320 kB
MemFree:         5242880 kB
MemAvailable:    7340032 kB
Buffers:          102400 kB
Cached:          1048576 kB
SwapCached:            0 kB
SReclaimable:     204800 kB
SwapTotal:       3932160 kB
SwapFree:        3670016 kB
"""

MI = 1024 * 1024


def _proc(root: Path, pid: int, name: str, rss_kb: int) -> None:
    tl.write(root, f"/proc/{pid}/status", f"Name:\t{name}\nUmask:\t0022\nState:\tS (sleeping)\nPid:\t{pid}\nVmRSS:\t{rss_kb} kB\nThreads:\t1\n")


@pytest.fixture()
def fake_system(staging: Path) -> Path:
    tl.write(staging, "/proc/meminfo", MEMINFO)
    tl.write(staging, "/proc/swaps", "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
                                    "/dev/zram0                              partition\t3932160\t\t262144\t\t100\n"
                                    "/swapfile                               file\t\t1048576\t\t0\t\t-2\n")
    tl.write(staging, "/proc/sys/kernel/osrelease", "6.8.0-45-generic\n")
    tl.write(staging, "/proc/cpuinfo", "processor\t: 0\nmodel name\t: Fake CPU 3000\n\nprocessor\t: 1\nmodel name\t: Fake CPU 3000\n")
    tl.write(staging, "/proc/self/status", "Name:\tpytest\nVmRSS:\t1 kB\n")  # not a digit dir → ignored
    _proc(staging, 1, "systemd", 12_000)
    _proc(staging, 900, "Xorg", 80_000)
    _proc(staging, 1200, "xfce4-panel", 45_000)
    _proc(staging, 1300, "firefox", 512_000)
    _proc(staging, 1301, "Isolated Web Co", 150_000)
    _proc(staging, 1400, "picom", 30_000)
    for pid in range(2000, 2012):
        _proc(staging, pid, f"daemon{pid}", 5_000 + pid)
    tl.write(staging, "/proc/3000/status", "Name:\tkthreadd\nState:\tS\n")  # no VmRSS → skipped
    tl.write(staging, "/sys/block/zram0/disksize", str(3840 * MI) + "\n")
    tl.write(staging, "/sys/block/zram0/mm_stat", f"{300 * MI} {100 * MI} {110 * MI} 0 {120 * MI} 5 0 0\n")
    tl.write(staging, "/sys/block/zram0/comp_algorithm", "lzo lzo-rle lz4 [zstd]\n")
    tl.write(staging, "/sys/block/sda/size", "1000\n")
    cpu = "/sys/devices/system/cpu/cpu0/cpufreq"
    tl.write(staging, f"{cpu}/scaling_governor", "powersave\n")
    tl.write(staging, f"{cpu}/scaling_available_governors", "performance powersave\n")
    tl.write(staging, f"{cpu}/scaling_driver", "intel_pstate\n")
    tl.write(staging, f"{cpu}/energy_performance_preference", "balance_performance\n")
    tl.write(staging, f"{cpu}/energy_performance_available_preferences", "default performance balance_performance balance_power power\n")
    tl.write(staging, "/etc/lindos/system.json", json.dumps({"mode": "lite", "browser": "firefox", "oem": False}))
    tl.write(staging, "/etc/lindos-release", "Lindos 1.0.0 (Aurora)\n")
    tl.write(staging, "/etc/os-release", 'NAME="Lindos"\nPRETTY_NAME="Lindos 1.0 (Aurora)"\nID=linuxmint\n')
    tl.write(staging, "/sys/class/power_supply/BAT0/type", "Battery\n")
    return staging


# --- parsers ----------------------------------------------------------------------------------------
def test_parse_meminfo_and_ram() -> None:
    mi = status.parse_meminfo(MEMINFO)
    assert mi["MemTotal"] == 7864320 and mi["SwapFree"] == 3670016
    ram = status.ram_from_meminfo(mi)
    assert ram["total"] == 7680
    assert ram["available"] == 7168
    assert ram["used"] == 512          # total - available, like free -m
    assert ram["swap_total"] == 3840 and ram["swap_used"] == 256
    assert ram["buff_cache"] == (102400 + 1048576 + 204800) // 1024


def test_parse_swaps_and_mm_stat() -> None:
    swaps = status.parse_swaps("Filename Type Size Used Priority\n/dev/zram0 partition 1024 512 100\nbad line\n")
    assert swaps == [{"device": "/dev/zram0", "type": "partition", "size_mb": 1, "used_mb": 0, "priority": 100}]
    mm = status.parse_mm_stat("1048576 524288 600000 0 700000 3 0 0")
    assert mm["orig_data_size"] == 1048576 and mm["compr_data_size"] == 524288 and mm["mem_used_total"] == 600000
    assert status.parse_comp_algorithm("lzo [zstd] lz4") == "zstd"
    assert status.parse_comp_algorithm("zstd") == "zstd"
    assert status.parse_comp_algorithm("") == ""


def test_parse_proc_status() -> None:
    st = status.parse_proc_status("Name:\tfoo\nVmRSS:\t  2048 kB\n")
    assert st == {"Name": "foo", "VmRSS": "2048 kB"}


# --- snapshot from the fake tree ---------------------------------------------------------------------
def test_snapshot_values(fake_system: Path, make_ctx, recorder) -> None:
    whitelist_units = ["bluetooth.service", "earlyoom.service", "cups.socket"]
    recorder.add(["systemctl", "is-enabled"], CmdResult(False, 1, "disabled\nenabled\nenabled\n"))
    recorder.add(["systemctl", "is-active"], CmdResult(False, 3, "inactive\nactive\nactive\n"))
    recorder.add(["pgrep", "-x", "picom"], CmdResult(True, 0, "1400\n"))
    recorder.add(["powerprofilesctl", "get"], CmdResult(True, 0, "balanced\n"))
    ctx = make_ctx(tools=("systemctl", "pgrep", "powerprofilesctl", "xfconf-query"))
    snap = status.snapshot(ctx, whitelist=whitelist_units)

    assert snap["mode"] == "lite"
    assert snap["kernel"] == "6.8.0-45-generic"
    ram = snap["ram"]
    assert ram["source"] == "/proc/meminfo" and ram["total"] == 7680 and ram["used"] == 512

    top = snap["top"]
    assert len(top) == 10
    assert [t["name"] for t in top[:4]] == ["firefox", "Isolated Web Co", "Xorg", "xfce4-panel"]
    assert top[0] == {"pid": 1300, "name": "firefox", "rss_mb": 500.0}
    assert all(top[i]["rss_mb"] >= top[i + 1]["rss_mb"] for i in range(9))
    assert "kthreadd" not in {t["name"] for t in top}

    z = snap["zram"]
    assert z["active"] is True and z["total_mb"] == 3840
    dev = z["devices"][0]
    assert dev["name"] == "zram0" and dev["disksize_mb"] == 3840 and dev["algorithm"] == "zstd"
    assert dev["orig_data_mb"] == 300.0 and dev["compr_data_mb"] == 100.0 and dev["mem_used_mb"] == 110.0
    assert dev["ratio"] == 3.0 and dev["swap_active"] and dev["swap_used_mb"] == 256 and dev["swap_priority"] == 100
    assert z["other_swaps"][0]["device"] == "/swapfile"

    gov = snap["governor"]
    assert gov["current"] == "powersave" and gov["driver"] == "intel_pstate" and gov["epp"] == "balance_performance"
    assert gov["available"] == ["performance", "powersave"]
    assert snap["power_profile"] == "balanced"
    assert snap["compositor"]["state"] == "picom" and snap["compositor"]["picom"] is True

    assert snap["units"]["bluetooth.service"] == {"enabled": "disabled", "active": "inactive"}
    assert snap["units_enabled"] == ["cups.socket", "earlyoom.service"]
    assert snap["units_active"] == ["cups.socket", "earlyoom.service"]

    v = snap["verdict"]
    assert v["line"].startswith("Idle RAM 512 MB — target 350–500 MB (Lite: 300–380)")
    assert v["target_min"] == 300 and v["target_max"] == 380 and v["rating"] == "slightly above target"
    assert 0 <= snap["score"] < 100
    json.dumps(snap)  # JSON-able


def test_verdict_and_score_bands() -> None:
    assert status.verdict(0)["rating"] == "unknown"
    assert status.verdict(300)["rating"] == "below target"
    assert status.verdict(420)["rating"] == "within target"
    assert status.verdict(600)["rating"] == "slightly above target"
    assert status.verdict(900)["rating"] == "above target"
    assert status.verdict(360, "lite")["rating"] == "within target"
    assert "for lite mode" in status.verdict(360, "lite")["line"]
    assert status.score(400) == 100 and status.score(1000) == 0 and 0 < status.score(600) < 100
    line = status.verdict(430)["line"]
    assert line.startswith("Idle RAM 430 MB — target 350–500 MB (Lite: 300–380)")


def test_render_text_and_json(fake_system: Path, make_ctx) -> None:
    ctx = make_ctx(tools=())
    text = status.run_status(ctx, as_json=False)
    assert "mode: lite" in text
    assert "used 512 MB / total 7680 MB" in text
    assert "zram0: 3840 MB zstd active" in text
    assert "governor powersave (intel_pstate), EPP balance_performance" in text
    assert "firefox (pid 1300)" in text
    assert "Idle RAM 512 MB — target 350–500 MB (Lite: 300–380)" in text
    assert "systemctl not reachable" in text
    data = json.loads(status.run_status(ctx, as_json=True))
    assert data["ram"]["used"] == 512 and data["mode"] == "lite"
    assert data["units"] == {u: {"enabled": "unknown", "active": "unknown"} for u in data["units"]}


def test_status_without_proc_is_graceful(staging: Path, make_ctx) -> None:
    ctx = make_ctx(tools=())
    snap = status.snapshot(ctx)
    assert snap["ram"]["total"] == 0 and snap["top"] == [] and snap["zram"]["devices"] == []
    assert snap["verdict"]["rating"] == "unknown" and snap["score"] == 0
    assert "n/a" in status.render_text(snap)


def test_current_mode_defaults(staging: Path) -> None:
    assert status.current_mode(str(staging)) == "everyday"
    tl.write(staging, common.SYSTEM_CONF, '{"mode": "gaming"}')
    assert status.current_mode(str(staging)) == "gaming"
    tl.write(staging, common.SYSTEM_CONF, '{"mode": "bogus"}')
    assert status.current_mode(str(staging)) == "everyday"


# --- report ------------------------------------------------------------------------------------------
def test_report_markdown(fake_system: Path, make_ctx, recorder) -> None:
    tl.write(fake_system, common.STATE_FILE, json.dumps({"mode": "lite", "applied_at": "2026-01-01T00:00:00+00:00", "zram_percent": 100}))
    tl.write(fake_system, common.SYSCTL_MODE_CONF, "# mode\nvm.max_map_count = 2147483642\n")
    tl.write(fake_system, common.EARLYOOM_DEFAULT, 'EARLYOOM_ARGS="-m 8 -s 100"\n')
    recorder.add(["lspci", "-nn"], CmdResult(True, 0, "00:02.0 VGA compatible controller [0300]: Intel Corporation UHD Graphics [8086:9bc4]\n"))
    ctx = make_ctx(tools=("lspci",))
    md = lreport.build(ctx)
    assert md.startswith("# Lindos tune report")
    for needle in ("| Lindos | Lindos 1.0.0 (Aurora) |", "| Kernel | 6.8.0-45-generic |", "| Mode | lite |",
                   "Fake CPU 3000 (2 threads)", "Intel Corporation UHD Graphics", "| Battery | yes (laptop) |",
                   "**Idle RAM 512 MB", "| zram0 | 3840 | zstd | active | 256 |", "| 1 | firefox | 1300 | 500.0 |",
                   "| governor | powersave |", "| EPP | balance_performance |", "## Tune state", "| zram_percent | 100 |",
                   "vm.max_map_count = 2147483642", "EARLYOOM_ARGS=\"-m 8 -s 100\"", "`/etc/lindos/tune.d/lite.conf`",
                   "ZRAM_PERCENT=100", "350–500 MB"):
        assert needle in md, needle
    assert "_absent_" in md  # tmpfiles governor conf not written in this tree
    out = fake_system / "report.md"
    text = lreport.run_report(ctx, output=str(out))
    assert out.read_text(encoding="utf-8") == text == md.replace(md.split("\n")[2], text.split("\n")[2])


def test_cpu_model_and_battery(fake_system: Path) -> None:
    assert lreport.cpu_model(str(fake_system)) == {"model": "Fake CPU 3000", "count": 2}
    assert lreport.battery_present(str(fake_system)) is True
    assert lreport.lindos_release(str(fake_system)) == "Lindos 1.0.0 (Aurora)"
