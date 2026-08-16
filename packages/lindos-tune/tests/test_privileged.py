"""Op payloads → lindos-core helper actions, escalation refusal, and the argparse CLI
(``lindos_tune.cli.main`` + the ``/usr/bin/lindos-tune`` launcher)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import cli, common, privileged
from lindos_tune.privileged import EscalationError, Request


# --- mapping ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("request_,action,payload", [
    (Request.apply("gaming", offline=True), "apply-tune", {"mode": "gaming", "offline": True}),
    (Request.apply("lite"), "apply-tune", {"mode": "lite", "offline": False}),
    (Request.zram(75), "set-zram", {"percent": 75}),
    (Request.governor("performance"), "set-governor", {"governor": "performance"}),
    (Request.services(disable=["bluetooth"]), "set-services", {"enable": [], "disable": ["bluetooth.service"], "mask": []}),
    (Request.services(enable=["earlyoom.service"], disable=["cups-browsed"]), "set-services",
     {"enable": ["earlyoom.service"], "disable": ["cups-browsed.service"], "mask": []}),
    (Request.fan("quiet"), "set-fan-profile", {"profile": "quiet"}),
    (Request.fan("silent"), "set-fan-profile", {"profile": "quiet"}),
    (Request.power("performance"), "set-governor", {"governor": "performance"}),
    (Request.power("balanced"), "set-governor", {"governor": "schedutil"}),
    (Request.power("power-saver"), "set-governor", {"governor": "powersave"}),
])
def test_helper_call_mapping(staging: Path, request_: Request, action: str, payload: dict) -> None:
    assert privileged.helper_call(request_) == (action, payload)
    op_payload = request_.to_payload()
    assert op_payload["op"] in privileged.OPS
    assert json.loads(request_.to_json()) == op_payload


@pytest.mark.skipif(not tl.core_available(), reason="lindos-core not importable")
@pytest.mark.parametrize("request_", [
    Request.apply("gaming", offline=True), Request.zram(75), Request.governor("performance"),
    Request.services(disable=["bluetooth"], enable=["earlyoom"]), Request.fan("quiet"), Request.power("balanced"),
])
def test_mapped_payloads_pass_core_validation(staging: Path, request_: Request) -> None:
    from lindos import helper as core_helper

    action, payload = privileged.helper_call(request_)
    assert action in core_helper.ACTIONS
    validated = core_helper.validate_payload(action, payload)
    for key, value in payload.items():
        assert validated.get(key) == value, (key, validated)


@pytest.mark.parametrize("request_,match", [
    (Request("apply", {"mode": "turbo"}), "unknown mode"),
    (Request("zram", {"percent": 500}), "between 0 and 200"),
    (Request("zram", {"percent": "lots"}), "integer"),
    (Request("governor", {"governor": "warp"}), "unknown governor"),
    (Request("services", {}), "nothing to enable"),
    (Request("services", {"disable": ["NetworkManager"]}), "whitelist"),
    (Request("fan", {"profile": "turbo"}), "unknown profile"),
    (Request("power", {"profile": "eco"}), "unknown profile"),
    (Request("reboot", {}), "unknown op"),
])
def test_helper_call_rejects_bad_requests(staging: Path, request_: Request, match: str) -> None:
    with pytest.raises(EscalationError, match=match):
        privileged.helper_call(request_)


def test_escalate_disabled_by_env(staging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(privileged.NO_ESCALATE_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["lindos-tune", "zram", "50"])
    result = privileged.escalate(Request.zram(50))
    assert not result.ok and result.code == common.EXIT_ERROR
    assert "sudo lindos-tune zram 50" in result.err
    assert result.action == "set-zram" and result.payload == {"percent": 50}
    bad = privileged.escalate(Request("zram", {"percent": 999}))
    assert bad.code == common.EXIT_USAGE and not bad.ok
    assert "needs root" in privileged.describe(Request.zram(50)) and "set-zram" in privileged.describe(Request.zram(50))


@pytest.mark.skipif(not tl.core_available(), reason="lindos-core not importable")
def test_escalate_dry_run_helper(staging: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With LINDOS_HELPER_DRYRUN=1 lindos-core runs the helper unprivileged and prints the plan."""
    monkeypatch.delenv(privileged.NO_ESCALATE_ENV, raising=False)
    monkeypatch.setenv("LINDOS_HELPER_DRYRUN", "1")
    helper = tl.CORE_LIB.parent.parent.parent / "libexec" / "lindos" / "lindos-helper"
    if not helper.is_file():
        pytest.skip("lindos-helper script not present")
    monkeypatch.setenv("LINDOS_HELPER", str(helper))
    lines: list = []
    result = privileged.escalate(Request.zram(60), log=lines.append)
    assert result.ok, (result.out, result.err)
    assert result.action == "set-zram"
    joined = "\n".join(lines) + result.out
    assert "dry-run" in joined and "lindos-tune zram 60" in joined


# --- CLI -------------------------------------------------------------------------------------------
def _run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_version_and_usage(staging: Path, capsys) -> None:
    code, out, _ = _run(["--version"], capsys)
    assert code == 0 and "lindos-tune 1.0.0" in out
    assert cli.main([]) == common.EXIT_USAGE
    assert cli.main(["bogus"]) == common.EXIT_USAGE
    assert cli.main(["apply"]) == common.EXIT_USAGE            # --mode required
    assert cli.main(["apply", "--mode", "turbo"]) == common.EXIT_USAGE
    assert cli.main(["zram", "abc"]) == common.EXIT_USAGE
    assert cli.main(["zram", "500"]) == common.EXIT_USAGE
    assert cli.main(["governor", "warp"]) == common.EXIT_USAGE
    assert cli.main(["fan", "set"]) == common.EXIT_USAGE
    assert cli.main(["fan", "set", "turbo"]) == common.EXIT_USAGE
    assert cli.main(["power", "eco"]) == common.EXIT_USAGE
    assert cli.main(["services", "disable"]) == common.EXIT_USAGE
    capsys.readouterr()


def test_cli_status_json_and_text(staging: Path, capsys) -> None:
    tl.write(staging, "/proc/meminfo", "MemTotal: 4096000 kB\nMemFree: 1000000 kB\nMemAvailable: 3600000 kB\n")
    code, out, _ = _run(["status", "--json"], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["ram"]["total"] == 4000 and data["ram"]["used"] == 485
    assert data["verdict"]["line"].startswith("Idle RAM 485 MB — target 350–500 MB (Lite: 300–380)")
    assert data["verdict"]["rating"] == "within target" and data["score"] == 100
    code, out, _ = _run(["status"], capsys)
    assert code == 0 and "Idle RAM 485 MB" in out and "score: 100/100" in out


def test_cli_apply_dry_run_and_staging_apply(staging: Path, capsys) -> None:
    code, out, _ = _run(["apply", "--mode", "gaming", "--system", "--dry-run", "--json"], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["ok"] and data["dry_run"] and data["mode"] == "gaming"
    assert not tl.read(staging, common.SYSCTL_MODE_CONF)
    # a LINDOS_ROOT staging tree needs no privileges → real writes into the tree
    code, out, _ = _run(["apply", "--mode", "gaming", "--system", "--offline"], capsys)
    assert code == 0 and "result: ok" in out
    assert "vm.max_map_count = 2147483642" in (tl.read(staging, common.SYSCTL_MODE_CONF) or "")
    assert json.loads(tl.read(staging, common.STATE_FILE) or "{}")["mode"] == "gaming"
    # implied --system note when the flag is omitted
    code, out, _ = _run(["apply", "--mode", "everyday"], capsys)
    assert code == 0 and "--system is implied" in out


def test_cli_services(staging: Path, capsys) -> None:
    code, out, err = _run(["services", "disable", "NetworkManager"], capsys)
    assert code == common.EXIT_ERROR and "not in the lindos-tune whitelist" in err
    tl.unit(staging, "bluetooth.service")
    code, out, _ = _run(["services", "disable", "bluetooth", "--dry-run"], capsys)
    # real `which` is used here: on Linux hosts systemctl exists ("would run"), elsewhere it is skipped
    assert code == 0 and "disable:bluetooth.service" in out
    assert "would run" in out or "systemctl not available" in out
    code, out, _ = _run(["services", "list", "--json"], capsys)
    assert code == 0 and json.loads(out)["whitelist"] == common.SERVICES_WHITELIST
    code, out, _ = _run(["services", "list"], capsys)
    assert code == 0 and "bluetooth.service" in out


def test_cli_zram_governor_power_fan_in_staging(staging: Path, capsys) -> None:
    tl.write(staging, "/usr/lib/systemd/system-generators/zram-generator", "")
    code, out, _ = _run(["zram", "80"], capsys)
    assert code == 0 and "ram * 0.80" in (tl.read(staging, common.ZRAM_GENERATOR_CONF) or "")
    code, out, _ = _run(["governor", "performance", "--json"], capsys)
    assert code == 0 and json.loads(out)["ok"]
    assert "performance" in (tl.read(staging, common.TMPFILES_GOVERNOR) or "")
    code, out, _ = _run(["power", "power-saver"], capsys)
    assert code == 0 and "powersave" in out
    code, out, _ = _run(["fan", "list"], capsys)
    assert code == 0 and "no controllable fans detected" in out
    code, out, _ = _run(["fan", "set", "quiet"], capsys)
    assert code == 0 and "install nbfc-linux" in out
    code, out, _ = _run(["fan", "list", "--json"], capsys)
    assert code == 0 and json.loads(out)["controllable"] is False


def test_cli_report(staging: Path, capsys, tmp_path: Path) -> None:
    code, out, _ = _run(["report"], capsys)
    assert code == 0 and out.startswith("# Lindos tune report")
    target = tmp_path / "r.md"
    code, out, _ = _run(["report", "-o", str(target)], capsys)
    assert code == 0 and target.read_text(encoding="utf-8").startswith("# Lindos tune report") and "written" in out


def test_cli_escalation_refused_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """No LINDOS_ROOT + not root → the CLI must escalate; with escalation disabled it fails cleanly."""
    if common.is_root():
        pytest.skip("running as root: nothing to escalate")
    monkeypatch.delenv("LINDOS_ROOT", raising=False)
    monkeypatch.setenv(privileged.NO_ESCALATE_ENV, "1")
    monkeypatch.setenv("LINDOS_CHROOT", "1")
    code, out, err = _run(["zram", "50"], capsys)
    assert code == common.EXIT_ERROR and "sudo lindos-tune" in out + err
    code, out, err = _run(["governor", "performance", "--json"], capsys)
    assert code == common.EXIT_ERROR
    data = json.loads(out)
    assert data["escalated"] and data["action"] == "set-governor" and data["op"] == {"op": "governor", "governor": "performance"}
    code, out, err = _run(["apply", "--mode", "lite", "--system"], capsys)
    assert code == common.EXIT_ERROR and "sudo lindos-tune" in out + err


# --- launcher ---------------------------------------------------------------------------------------
def test_bin_launcher_is_python_and_runs(staging: Path) -> None:
    text = tl.BIN.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/python3\n")
    assert "\r" not in text
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(tl.BIN), "--version"], capture_output=True, text=True, timeout=60, env=env, check=False)
    assert proc.returncode == 0 and "lindos-tune 1.0.0" in proc.stdout
    proc = subprocess.run([sys.executable, str(tl.BIN), "apply", "--mode", "work", "--dry-run", "--json"],
                          capture_output=True, text=True, timeout=120, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["mode"] == "work"


def test_all_modules_import_without_side_effects() -> None:
    for name in ("common", "status", "apply", "zram", "services", "governor", "fan", "power", "report", "privileged", "cli"):
        spec = importlib.util.find_spec(f"lindos_tune.{name}")
        assert spec is not None, name
