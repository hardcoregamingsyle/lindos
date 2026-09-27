"""Shipped data files: ram-budget.json, tune.d confs, ananicy rules, preset, DEBIAN metadata,
scripts (LF endings, shebangs), systemd unit — validity and SPEC agreement."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import apply, common, governor, services

ROOT = tl.ROOT
DEBIAN = tl.PKG_ROOT / "DEBIAN"


def _lf(path: Path) -> str:
    data = path.read_bytes()
    assert b"\r" not in data, f"{path} must use LF line endings"
    return data.decode("utf-8")


# --- ram-budget.json ---------------------------------------------------------------------------------
def test_ram_budget_json() -> None:
    data = json.loads(_lf(tl.SHARE / "ram-budget.json"))
    assert data["schema"] == 1 and data["unit"] == "MB"
    assert data["targets"]["default"] == {"min": 350, "max": 500, "modes": ["everyday", "gaming", "work", "creator"]}
    assert data["targets"]["lite"]["min"] == 300 and data["targets"]["lite"]["max"] == 380
    ids = [b["id"] for b in data["baselines"]]
    assert ids == ["stock-mint-22-xfce", "lindos-everyday", "lindos-lite"]
    stock = data["baselines"][0]
    assert (stock["min_mb"], stock["max_mb"]) == (600, 750) and stock["status"] == "estimate"
    assert "estimate" in data["honesty"].lower()
    measures = data["measures"]
    assert len(measures) >= 12
    seen = set()
    for m in measures:
        for key in ("id", "name", "kind", "modes", "saves_mb", "how", "explanation", "reversible"):
            assert key in m, (m.get("id"), key)
        assert m["id"] not in seen
        seen.add(m["id"])
        assert set(m["modes"]) <= set(common.MODE_IDS)
        lo, hi = m["saves_mb"]
        assert 0 <= lo <= hi
    by_id = {m["id"]: m for m in measures}
    assert by_id["no-picom-lite"]["saves_mb"] == [25, 35] and by_id["no-picom-lite"]["modes"] == ["lite"]
    assert by_id["bluetooth-off"]["saves_mb"] == [15, 25]
    assert by_id["zram"]["kind"] == "headroom"
    assert "avahi-kept" in by_id and by_id["cups-socket-activated"]["saves_mb"][0] >= 8


# --- tune.d ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", common.MODE_IDS)
def test_tune_d_conf_matches_mode_defaults(mode: str) -> None:
    text = _lf(ROOT / "etc" / "lindos" / "tune.d" / f"{mode}.conf")
    kv = common.parse_kv(text)
    d = apply.MODE_DEFAULTS[mode]
    assert int(kv["ZRAM_PERCENT"]) == d["zram_percent"]
    assert kv["GOVERNOR"] == d["governor"] and kv["GOVERNOR"] in governor.GOVERNORS
    assert int(kv["EARLYOOM_MIN"]) == d["earlyoom_min"]
    assert kv["COMPOSITOR"] == d["compositor"]
    assert (kv["ANANICY"].lower() in ("on", "true", "1", "yes")) == d["ananicy"]
    settings = apply.resolve_settings(mode, None, kv)
    assert settings["services_disable"] == d["services_disable"]
    assert "tune.d" in settings["sources"]


# --- ananicy ------------------------------------------------------------------------------------------
def test_ananicy_rules_are_json_lines_and_types_resolve() -> None:
    d = ROOT / "etc" / "ananicy.d" / "lindos"
    assert str(common.ANANICY_RULES_DIR) == "/etc/ananicy.d/lindos"
    types = {}
    for line in _lf(d / "lindos.types").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        assert set(obj) >= {"type", "nice", "ioclass", "ionice", "sched", "oom_score_adj"}
        assert -20 <= obj["nice"] <= 19 and 0 <= obj["ionice"] <= 7 and -1000 <= obj["oom_score_adj"] <= 1000
        assert obj["ioclass"] in ("none", "realtime", "best-effort", "idle") and obj["sched"] in ("other", "batch", "idle", "fifo", "rr")
        types[obj["type"]] = obj
    assert {"Lindos-Game", "Lindos-Launcher", "Lindos-Browser", "Lindos-Compiler"} <= set(types)
    assert types["Lindos-Game"]["nice"] < 0 < types["Lindos-Compiler"]["nice"] and types["Lindos-Browser"]["nice"] == 0
    rule_files = sorted(p.name for p in d.glob("*.rules"))
    assert rule_files == ["lindos-browsers.rules", "lindos-compilers.rules", "lindos-games.rules"]
    names = {}
    for rules in rule_files:
        for line in _lf(d / rules).splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            assert set(obj) == {"name", "type"}, (rules, line)
            assert obj["type"] in types, (rules, obj)
            assert obj["name"] not in names, f"duplicate rule {obj['name']} in {rules} and {names[obj['name']]}"
            names[obj["name"]] = rules
    assert names["steam"] == "lindos-games.rules" and names["firefox"] == "lindos-browsers.rules" and names["cc1"] == "lindos-compilers.rules"


# --- preset / whitelist / lists -------------------------------------------------------------------------
#: one-shot autodetect/first-boot units: not meant to be toggled by users through the generic
#: services wrapper (they are managed by their own systemd Condition*=/marker files instead).
_PRESET_NOT_WHITELISTED = {"lindos-sensors-detect.service", "lindos-driver-firstboot.service",
                           "lindos-browser-firstboot.service"}


def test_preset_units_are_valid_and_whitelisted_where_toggleable() -> None:
    entries = apply.parse_preset(_lf(ROOT / "usr/lib/systemd/system-preset/90-lindos.preset"))
    assert entries, "preset must have entries"
    wl = set(services.parse_whitelist(_lf(tl.SHARE / "services-whitelist.txt")))
    for verb, unit in entries:
        assert services.valid_unit_name(unit)
        if unit not in _PRESET_NOT_WHITELISTED:
            assert unit in wl, f"{unit} in preset but not toggleable via the whitelist"


def test_autostart_hide_list_entries_valid() -> None:
    entries = services.parse_hide_list(_lf(tl.SHARE / "autostart-hide.list"))
    assert len(entries) >= 4
    for name, cond in entries:
        assert re.match(r"^[A-Za-z0-9._@-]+$", name)
        assert cond is None or services.valid_unit_name(cond)


# --- DEBIAN --------------------------------------------------------------------------------------------
def test_debian_control() -> None:
    text = _lf(DEBIAN / "control")
    fields = dict(re.findall(r"^([A-Z][A-Za-z-]+): (.*)$", text, re.M))
    assert fields["Package"] == "lindos-tune" and fields["Version"] == "1.0.0" and fields["Architecture"] == "all"
    assert fields["Maintainer"] == "Lindos Team <team@lindos.dev>"
    depends = fields["Depends"]
    for dep in ("python3", "lindos-core", "systemd", "procps", "util-linux", "zram-tools | systemd-zram-generator", "earlyoom", "lm-sensors"):
        assert dep in depends, dep
    for rec in ("power-profiles-daemon", "ananicy-cpp", "fancontrol", "hdparm"):
        assert rec in fields["Recommends"], rec
    assert "Description:" in text and "350" in text and "estimates" in text


def test_debian_scripts_and_conffiles() -> None:
    for name in ("postinst", "postrm"):
        text = _lf(DEBIAN / name)
        assert text.startswith("#!/bin/sh\n") and "set -e" in text.splitlines()[0:8] or "set -e" in text
        assert "sudo" not in text
    postinst = _lf(DEBIAN / "postinst")
    assert "preset-all" not in postinst.replace("never preset-all", "")
    assert "sensors-detect" in postinst and "daemon-reload" in postinst and "sysctl --system" in postinst
    for line in _lf(DEBIAN / "conffiles").splitlines():
        if not line.strip():
            continue
        assert line.startswith("/etc/"), line
        assert (ROOT / line.lstrip("/")).is_file(), f"conffile {line} not shipped"
    # managed (regenerated) files must NOT be conffiles
    conffiles = _lf(DEBIAN / "conffiles").split()
    assert "/etc/sysctl.d/70-lindos-base.conf" not in conffiles
    assert "/etc/default/earlyoom" not in conffiles  # owned by the earlyoom package


def test_shell_scripts_style() -> None:
    for script in (ROOT / "usr/libexec/lindos/install-nbfc.sh", ROOT / "usr/libexec/lindos/sensors-detect-once.sh"):
        text = _lf(script)
        assert text.startswith("#!/bin/bash\n")
        assert "set -Eeuo pipefail" in text
        assert "log()" in text and "die()" in text
        assert re.search(r"^\s*sudo\b", text, re.M) is None
    nbfc = _lf(ROOT / "usr/libexec/lindos/install-nbfc.sh")
    assert "github.com/nbfc-linux/nbfc-linux" in nbfc and "linux-mint-22-nbfc-linux_%s_amd64.deb" in nbfc
    assert "sha256sum" in nbfc


def test_systemd_unit_and_tmpfiles() -> None:
    unit = _lf(ROOT / "usr/lib/systemd/system/lindos-sensors-detect.service")
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "ConditionPathExists=!/var/lib/lindos-tune/sensors-detect.done" in unit
    assert "ExecStart=/usr/libexec/lindos/sensors-detect-once.sh" in unit
    assert "Type=oneshot" in unit
    tmpfiles = _lf(ROOT / "etc/tmpfiles.d/lindos.conf")
    assert re.search(r"^d /var/log/lindos\s+0755 root root", tmpfiles, re.M)
    assert re.search(r"^d /var/lib/lindos-tune\s+0755 root root", tmpfiles, re.M)


def test_all_python_files_lf_and_no_shell_true() -> None:
    usage = re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True", re.S)
    for path in (ROOT / "usr/lib/lindos-tune/lindos_tune").glob("*.py"):
        text = _lf(path)
        assert not usage.search(text), path
    assert not usage.search(_lf(tl.BIN))
