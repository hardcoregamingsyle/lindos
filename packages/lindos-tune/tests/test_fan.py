"""``lindos-tune fan`` — ``sensors -j`` parsing, hwmon fallback, controllers, profiles."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import fan
from lindos_tune.common import CmdResult

SENSORS_JSON = {
    "coretemp-isa-0000": {
        "Adapter": "ISA adapter",
        "Package id 0": {"temp1_input": 47.0, "temp1_max": 100.0, "temp1_crit": 100.0, "temp1_crit_alarm": 0.0},
        "Core 0": {"temp2_input": 45.0, "temp2_max": 100.0, "temp2_crit": 100.0},
        "Core 1": {"temp3_input": 46.5, "temp3_max": 100.0, "temp3_crit": 100.0},
    },
    "nct6798-isa-0290": {
        "Adapter": "ISA adapter",
        "fan1": {"fan1_input": 0.0, "fan1_min": 0.0, "fan1_alarm": 0.0},
        "fan2": {"fan2_input": 812.0, "fan2_min": 0.0},
        "SYSTIN": {"temp1_input": 34.0, "temp1_max": 80.0, "temp1_max_hyst": 75.0},
    },
    "amdgpu-pci-0300": {
        "Adapter": "PCI adapter",
        "fan1": {"fan1_input": 1230.0, "fan1_min": 0.0, "fan1_max": 3300.0},
        "edge": {"temp1_input": 52.0, "temp1_crit": 100.0, "temp1_crit_hyst": -273.15},
        "junction": {"temp2_input": 55.0, "temp2_crit": 110.0},
        "PPT": {"power1_average": 22.0, "power1_cap": 130.0},
    },
    "acpitz-acpi-0": {"Adapter": "ACPI interface", "temp1": {"temp1_input": 27.8, "temp1_crit": 105.0}},
}


def test_parse_sensors_json() -> None:
    parsed = fan.parse_sensors_json(SENSORS_JSON)
    fans = parsed["fans"]
    temps = parsed["temps"]
    assert [(f["chip"], f["label"], f["rpm"]) for f in fans] == [
        ("amdgpu-pci-0300", "fan1", 1230), ("nct6798-isa-0290", "fan1", 0), ("nct6798-isa-0290", "fan2", 812)]
    assert fans[0]["min"] == 0.0
    by_label = {(t["chip"], t["label"]): t for t in temps}
    assert by_label[("coretemp-isa-0000", "Package id 0")]["celsius"] == 47.0
    assert by_label[("coretemp-isa-0000", "Package id 0")]["high"] == 100.0
    assert by_label[("coretemp-isa-0000", "Core 1")]["celsius"] == 46.5
    assert by_label[("amdgpu-pci-0300", "junction")]["crit"] == 110.0
    assert by_label[("nct6798-isa-0290", "SYSTIN")]["crit"] is None
    assert by_label[("acpitz-acpi-0", "temp1")]["celsius"] == 27.8
    assert len(temps) == 7
    # power readings are not temperatures/fans
    assert not any(t["label"] == "PPT" for t in temps)


def test_parse_sensors_text_tolerates_noise() -> None:
    text = "WARNING: something\n" + json.dumps(SENSORS_JSON) + "\n"
    parsed = fan.parse_sensors_text(text)
    assert len(parsed["fans"]) == 3
    assert fan.parse_sensors_text("") == {"fans": [], "temps": []}
    assert fan.parse_sensors_text("not json at all") == {"fans": [], "temps": []}
    assert fan.parse_sensors_json(["nope"]) == {"fans": [], "temps": []}


def test_list_fans_via_sensors_runner(make_ctx, recorder) -> None:
    recorder.add(["sensors", "-j"], CmdResult(True, 0, json.dumps(SENSORS_JSON)))
    ctx = make_ctx(tools=("sensors",))
    data = fan.list_fans(ctx)
    assert data["source"] == "sensors"
    assert len(data["fans"]) == 3 and len(data["temps"]) == 7
    assert data["controllable"] is False and data["hint"] and "nbfc" in data["hint"]
    assert data["controllers"]["nbfc"]["available"] is False
    assert data["controllers"]["fancontrol"]["configured"] is False
    table = fan.render_table(data)
    assert "amdgpu-pci-0300" in table and "1230 RPM" in table and "47.0 °C" in table
    assert "no controllable fans detected" in table
    assert "install-nbfc.sh" in table


def test_hwmon_fallback(staging: Path, make_ctx) -> None:
    tl.write(staging, "/sys/class/hwmon/hwmon2/name", "thinkpad\n")
    tl.write(staging, "/sys/class/hwmon/hwmon2/fan1_input", "2870\n")
    tl.write(staging, "/sys/class/hwmon/hwmon2/fan1_label", "CPU fan\n")
    tl.write(staging, "/sys/class/hwmon/hwmon2/temp1_input", "51000\n")
    tl.write(staging, "/sys/class/hwmon/hwmon2/temp1_crit", "100000\n")
    tl.write(staging, "/sys/class/hwmon/hwmon3/name", "nvme\n")
    tl.write(staging, "/sys/class/hwmon/hwmon3/temp1_input", "38850\n")
    ctx = make_ctx(tools=())
    data = fan.list_fans(ctx)
    assert data["source"] == "hwmon"
    assert data["fans"] == [{"chip": "thinkpad", "label": "CPU fan", "rpm": 2870, "min": None}]
    temps = {(t["chip"], t["label"]): t for t in data["temps"]}
    assert temps[("thinkpad", "temp1")]["celsius"] == 51.0 and temps[("thinkpad", "temp1")]["crit"] == 100.0
    assert temps[("nvme", "temp1")]["celsius"] == 38.9
    assert "thinkpad" in fan.render_table(data)


def test_fancontrol_and_thinkpad_detection(staging: Path, make_ctx) -> None:
    tl.write(staging, fan.FANCONTROL_CONF, "INTERVAL=10\n")
    tl.write(staging, fan.THINKPAD_FAN, "status:\t\tenabled\nspeed:\t\t2870\nlevel:\t\tauto\ncommands:\tlevel <level> (<level> is 0-7, auto, disengaged, full-speed)\n")
    ctx = make_ctx(tools=())
    data = fan.list_fans(ctx)
    ctrl = data["controllers"]
    assert ctrl["fancontrol"]["configured"] is True
    assert ctrl["thinkpad_acpi"] == {"available": True, "status": "enabled", "level": "auto", "writable": True}
    assert data["controllable"] is True and data["hint"] is None
    table = fan.render_table(data)
    assert "fancontrol: configured" in table and "thinkpad_acpi: level auto" in table and "lindos-tune fan set" in table


@pytest.mark.parametrize("profile,argv", [
    ("auto", ["nbfc", "set", "-a"]),
    ("quiet", ["nbfc", "set", "-s", "30"]),
    ("balanced", ["nbfc", "set", "-s", "55"]),
    ("max", ["nbfc", "set", "-s", "100"]),
    ("silent", ["nbfc", "set", "-s", "30"]),   # alias
])
def test_set_profile_nbfc(make_ctx, recorder, profile: str, argv) -> None:
    ctx = make_ctx(tools=("nbfc",), nbfc=True)
    report = fan.set_profile(ctx, profile)
    assert report.ok, report.render()
    assert recorder.find(*argv), recorder.calls
    assert "nbfc" in report.steps[-1].detail


def test_set_profile_nbfc_failure_gives_config_hint(make_ctx, recorder) -> None:
    recorder.add(["nbfc", "set"], CmdResult(False, 1, "Error: no config selected — service not running"))
    ctx = make_ctx(tools=("nbfc",), nbfc=True)
    report = fan.set_profile(ctx, "max")
    assert not report.ok
    assert "nbfc config -r" in report.steps[-1].detail


def test_set_profile_dry_run_and_unknown(make_ctx, recorder) -> None:
    ctx = make_ctx(dry_run=True, nbfc=True)
    report = fan.set_profile(ctx, "quiet")
    assert report.ok and "would run: nbfc set -s 30" in report.steps[-1].detail
    assert not recorder.calls
    report = fan.set_profile(ctx, "turbo")
    assert not report.ok and "unknown profile" in report.steps[-1].detail
    assert fan.normalize_profile("Balanced") == "balanced" and fan.normalize_profile("x") is None


def test_set_profile_thinkpad(staging: Path, make_ctx) -> None:
    path = tl.write(staging, fan.THINKPAD_FAN, "status:\t\tenabled\nlevel:\t\tauto\ncommands:\tlevel <level>\n")
    ctx = make_ctx(tools=(), nbfc=False)
    report = fan.set_profile(ctx, "balanced")
    assert report.ok, report.render()
    assert path.read_text(encoding="utf-8") == "level 4\n"
    assert "thinkpad_acpi" in report.steps[-1].detail


def test_set_profile_without_controllers_is_honest(make_ctx, recorder) -> None:
    ctx = make_ctx(tools=(), nbfc=False)
    report = fan.set_profile(ctx, "max")
    assert report.ok  # skipped, not failed
    step = report.steps[-1]
    assert step.skipped and "no controllable fans detected" in step.detail and "nbfc-linux" in step.detail
    assert not recorder.calls
