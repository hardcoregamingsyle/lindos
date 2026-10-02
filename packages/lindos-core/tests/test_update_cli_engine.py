"""The update-engine subcommands of ``lindos-update``: status, plan, apply (--all), history, check --refresh
(SPEC-UPDATE.md §36.1, §41).

The CLI is loaded as a module and driven in-process with a fake helper client (``lindos.helper.run_privileged``)
and a fake apt runner (``lindos.updatestate.default_runner``), so nothing here needs apt, root or a password -
and on a real Linux runner it still never touches the real apt.
"""
from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from lindos import helper as lhelper
from lindos import paths
from lindos import update as lupdate
from lindos import updatestate as lstate

BIN = Path(__file__).resolve().parent.parent / "root" / "usr" / "bin" / "lindos-update"

SIM = """Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])
Inst lindos-meta [1.0.0] (1.0.1 Lindos:stable [all])
Inst libc6 [2.39-0ubuntu8.3] (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates [amd64])
"""
SIM_KERNEL = SIM + "Inst linux-image-6.8.0-47-generic (6.8.0-47.47 Ubuntu:24.04/noble-updates [amd64])\n"
SIM_REMOVE = SIM + "Remv libold1 [1.0]\n"


def _load_cli(name: str) -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(name, str(BIN))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


class Apt:
    """Fake apt for ``lstate.default_runner``."""

    def __init__(self, sim: str = SIM, uris: str = "") -> None:
        self.sim, self.uris = sim, uris

    def __call__(self, argv: Sequence[str], timeout: float = 0) -> Tuple[int, str]:
        cmd = list(argv)
        if cmd[:4] == ["apt-get", "-q", "-s", "dist-upgrade"]:
            return 0, self.sim
        if "--print-uris" in cmd:
            return 0, self.uris
        return 1, "unexpected"


class Helper:
    """Fake ``lindos.helper.run_privileged``: records calls, returns *result*."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.ok = ok

    def __call__(self, action: str, payload: Optional[Dict[str, Any]] = None, log=None, **_kw: Any):
        self.calls.append((action, dict(payload or {})))
        if log is not None:
            log(f"helper says: {action}")
        return lhelper.HelperResult(self.ok, f"{action} done\n", "" if self.ok else "boom", 0 if self.ok else 1)

    def actions(self) -> List[str]:
        return [a for a, _p in self.calls]


@pytest.fixture()
def cli(core_env, monkeypatch):
    module = _load_cli("lindos_update_cli_engine_test")
    helper = Helper()
    monkeypatch.setattr(lhelper, "run_privileged", helper)
    monkeypatch.setattr(lstate, "default_runner", Apt())
    monkeypatch.delenv(lhelper.DRYRUN_ENV, raising=False)
    monkeypatch.setenv("LINDOS_HELPER_DRYRUN", "1")           # belt and braces: never a real pkexec
    module.helper = helper
    return module


def _digest(sim: str) -> str:
    return lstate.parse_simulation(sim).digest


# =================================================================================================
# apply --all
# =================================================================================================
def test_apply_all_sends_the_digest_of_the_plan_it_showed(cli, capsys) -> None:
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 0
    assert cli.helper.calls == [("apt-full-upgrade", {"plan_digest": _digest(SIM)})]
    out = capsys.readouterr().out
    assert "3 update(s)" in out                                                  # the plan was printed before it ran
    assert "Lindos: 2" in out


def test_apply_refreshes_the_package_lists_first_unless_told_not_to(cli) -> None:
    assert cli.main(["apply", "--all", "--yes"]) == 0
    assert cli.helper.actions() == ["apt-get-update", "apt-full-upgrade"]
    cli.helper.calls.clear()
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 0
    assert cli.helper.actions() == ["apt-full-upgrade"]


def test_a_failed_refresh_is_reported_and_apply_carries_on_with_the_cache(cli, capsys) -> None:
    class RefreshFails(Helper):
        def __call__(self, action, payload=None, log=None, **kw):
            res = super().__call__(action, payload, log, **kw)
            if action == "apt-get-update":
                return lhelper.HelperResult(False, "", "no network", 1)
            return res
    helper = RefreshFails()
    cli.lhelper.run_privileged = helper
    assert cli.main(["apply", "--all", "--yes"]) == 0
    assert helper.actions() == ["apt-get-update", "apt-full-upgrade"]
    assert "could not refresh the package lists" in capsys.readouterr().err


def test_apply_all_needs_the_kernel_flag_when_the_plan_has_a_kernel(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lstate, "default_runner", Apt(SIM_KERNEL))
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 1
    assert cli.helper.calls == []
    assert "--include-kernel" in capsys.readouterr().err
    assert cli.main(["apply", "--all", "--yes", "--no-refresh", "--include-kernel"]) == 0
    assert cli.helper.calls == [("apt-full-upgrade", {"plan_digest": _digest(SIM_KERNEL), "allow_kernel": True})]


def test_apply_all_needs_the_removals_flag_when_the_plan_removes_packages(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lstate, "default_runner", Apt(SIM_REMOVE))
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 1
    assert cli.helper.calls == [] and "--allow-removals" in capsys.readouterr().err
    assert cli.main(["apply", "--all", "--yes", "--no-refresh", "--allow-removals"]) == 0
    assert cli.helper.calls[0][1]["allow_removals"] is True


def test_apply_all_json_keeps_stdout_pure(cli, capsys) -> None:
    assert cli.main(["apply", "--all", "--yes", "--no-refresh", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)                                  # the whole stdout is one JSON document
    assert data["status"] == "ok" and data["digest"] == _digest(SIM) and data["counts"]["lindos"] == 2
    assert "helper says" in captured.err and "helper says" not in captured.out


def test_apply_all_without_yes_asks_and_a_no_stops(cli, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["apply", "--all", "--no-refresh"]) == 0
    assert cli.helper.calls == [] and "cancelled" in capsys.readouterr().out


def test_apply_all_reports_a_helper_failure(cli, capsys) -> None:
    cli.lhelper.run_privileged = Helper(ok=False)
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 1
    assert "boom" in capsys.readouterr().err


def test_apply_all_nothing_to_do_and_apt_errors(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lstate, "default_runner", Apt("Reading package lists...\n"))
    assert cli.main(["apply", "--all", "--yes", "--no-refresh", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "nothing-to-do"
    monkeypatch.setattr(lstate, "default_runner", Apt("E: broken\n"))
    assert cli.main(["apply", "--all", "--yes", "--no-refresh"]) == 1
    assert cli.helper.calls == []


def test_dry_run_routes_the_helper_into_dry_run_mode(cli, monkeypatch) -> None:
    monkeypatch.delenv(lhelper.DRYRUN_ENV, raising=False)
    cli.main(["apply", "--all", "--yes", "--no-refresh", "--dry-run"])
    assert os.environ.get(lhelper.DRYRUN_ENV) == "1"


def test_apply_all_says_when_a_restart_is_needed_afterwards(cli, capsys) -> None:
    cli.main(["apply", "--all", "--yes", "--no-refresh"])
    assert "need a restart" in capsys.readouterr().out              # libc6 is in the plan


def test_plain_apply_still_sends_the_exact_lindos_list(cli, monkeypatch) -> None:
    status = lupdate.UpdateStatus(refreshed_at=None, lindos_updates=[lupdate.PackageUpdate("lindos-core", "1.0.0", "1.0.1", "lindos")],
                                  system_updates=[], kernel_available=None, booted_kernel="", booted_is_lindos_kernel=False,
                                  reboot_required=False, repo_configured=False, repo_reachable=None)
    monkeypatch.setattr(lupdate, "check", lambda **_k: status)
    assert cli.main(["apply", "--yes"]) == 0
    assert cli.helper.calls[0] == ("apt-get-update", {})             # refreshed first
    assert cli.helper.calls[1] == ("system-upgrade", {"packages": ["lindos-core=1.0.1"]})


# =================================================================================================
# check --refresh
# =================================================================================================
def test_check_refresh_refreshes_first_and_keeps_json_pure(cli, monkeypatch, capsys) -> None:
    status = lupdate.UpdateStatus(refreshed_at=None, lindos_updates=[], system_updates=[], kernel_available=None,
                                  booted_kernel="", booted_is_lindos_kernel=False, reboot_required=False,
                                  repo_configured=False, repo_reachable=None)
    monkeypatch.setattr(lupdate, "check", lambda **_k: status)
    assert cli.main(["check", "--refresh", "--json"]) == 3
    assert cli.helper.actions() == ["apt-get-update"]
    captured = capsys.readouterr()
    assert json.loads(captured.out)["repo_configured"] is False and "helper says" in captured.err
    cli.helper.calls.clear()
    cli.main(["check", "--json"])
    assert cli.helper.calls == []                                     # plain check never refreshes


# =================================================================================================
# plan
# =================================================================================================
def test_plan_prints_the_digest_and_the_groups(cli, capsys) -> None:
    assert cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert _digest(SIM) in out and "Lindos: 2" in out and "libc6: 2.39-0ubuntu8.3 -> 2.39-0ubuntu8.4" in out
    assert cli.helper.calls == []                                     # a plan is a simulation: no helper, no password


def test_plan_json_document(cli, capsys, monkeypatch) -> None:
    monkeypatch.setattr(lstate, "default_runner", Apt(SIM_KERNEL, uris="'https://x/lindos-core_1.0.1_all.deb' lindos-core_1.0.1_all.deb 1234 SHA256:aa\n"))
    assert cli.main(["plan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["digest"] == _digest(SIM_KERNEL) and data["includes_kernel"] is True
    assert data["counts"]["total"] == 4 and data["download_bytes"] == 1234
    assert [g["id"] for g in data["groups"]] == ["lindos", "drivers-kernel", "other"]


def test_plan_up_to_date_and_errors(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lstate, "default_runner", Apt(""))
    assert cli.main(["plan"]) == 3 and "up to date" in capsys.readouterr().out
    monkeypatch.setattr(lstate, "default_runner", Apt("E: no\n"))
    assert cli.main(["plan"]) == 1 and "cannot compute" in capsys.readouterr().err
    assert cli.main(["plan", "--json"]) == 1


# =================================================================================================
# status
# =================================================================================================
def _write_state(**changes: Any) -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    plan = lstate.parse_simulation(SIM_KERNEL)
    state = lstate.build_state(
        plan, now=now, refreshed_at=(now - dt.timedelta(hours=3)).isoformat(timespec="seconds"),
        refresh={"attempted": True, "ok": True, "message": ""},
        repo={"configured": False, "enabled": False, "url": None, "placeholder": True, "reachable": None},
        reboot_pending={"required": False, "packages": []}, reboot_reasons=[], booted_kernel="6.8.0-45-generic", held=[])
    state.update(changes)
    lstate.write_json_atomic(paths.resolve(lstate.STATE_PATH), state)
    return state


def test_status_without_a_state_file(cli, capsys) -> None:
    assert cli.main(["status"]) == 3
    assert "no update information yet" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["available"] is False


def test_status_summarises_the_state_file(cli, capsys) -> None:
    _write_state()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "updates available: 4" in out and "Lindos: 2" in out and "Drivers & kernel: 1" in out
    assert "a restart will be needed after installing" in out
    assert "out of date" not in out and "mint" not in out.lower()


def test_status_json_adds_availability_staleness_and_age(cli, capsys) -> None:
    state = _write_state()
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["available"] is True and data["stale"] is False and 3 * 3600 - 60 < data["age_seconds"] < 3 * 3600 + 3600
    assert data["digest"] == state["digest"] and data["groups"] == state["groups"]


def test_status_calls_an_old_state_out_of_date(cli, capsys) -> None:
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat(timespec="seconds")
    _write_state(refreshed_at=old)
    cli.main(["status"])
    assert "out of date" in capsys.readouterr().out
    cli.main(["status", "--json"])
    assert json.loads(capsys.readouterr().out)["stale"] is True


def test_status_reports_a_failed_refresh_a_pending_restart_and_removals(cli, capsys) -> None:
    _write_state(refresh={"attempted": True, "ok": False, "message": "Failed to fetch"},
                 reboot={"required": True, "packages": ["libc6"], "relogin": ["lindos-desktop"],
                         "reasons": [{"kind": "reboot", "package": "libc6", "text": "The core system libraries were updated"}],
                         "would_require_reboot": False, "would_require_reboot_packages": []},
                 removals=[{"name": "libold1", "version": "1", "purge": False}], kept_back=["gimp"])
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "last refresh failed: Failed to fetch" in out
    assert "restart required: The core system libraries were updated" in out
    assert "sign out and back in" in out and "would remove: 1" in out and "kept back: gimp" in out


def test_status_when_apt_could_not_compute_the_updates(cli, capsys) -> None:
    _write_state(plan_ok=False, plan_errors=["dpkg was interrupted"])
    cli.main(["status"])
    assert "could not work out the updates: dpkg was interrupted" in capsys.readouterr().out


def test_status_up_to_date(cli, capsys) -> None:
    _write_state(counts={"total": 0}, groups=[])
    cli.main(["status"])
    assert "up to date" in capsys.readouterr().out


# =================================================================================================
# history
# =================================================================================================
HISTORY = """Start-Date: 2026-09-29  08:12:03
Commandline: apt-get install --only-upgrade -y -q -- lindos-core=1.0.1
Upgrade: lindos-core:amd64 (1.0.0, 1.0.1), lindos-meta:all (1.0.0, 1.0.1), a:amd64 (1, 2), b:amd64 (1, 2), c:amd64 (1, 2)
End-Date: 2026-09-29  08:12:09

Start-Date: 2026-09-30  09:00:00
Commandline: apt-get purge -y -q -- libold1
Remove: libold1:amd64 (1.0-1)
Error: dpkg returned an error code (1)
End-Date: 2026-09-30  09:00:05
"""


def _write_history() -> None:
    d = Path(paths.resolve("/var/log/apt"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.log").write_text(HISTORY, encoding="utf-8")


def test_history_lists_newest_first_and_flags_failures(cli, capsys) -> None:
    _write_history()
    assert cli.main(["history"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("2026-09-30 09:00:00") and "Remove 1 (libold1)" in lines[0] and lines[0].endswith("FAILED")
    assert lines[1].startswith("2026-09-29 08:12:03") and "Upgrade 5 (lindos-core, lindos-meta, a, ...)" in lines[1]


def test_history_limit_and_json(cli, capsys) -> None:
    _write_history()
    assert cli.main(["history", "--limit", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["entries"]) == 1 and data["entries"][0]["actions"]["Remove"][0]["name"] == "libold1"


def test_history_without_a_log(cli, capsys) -> None:
    assert cli.main(["history"]) == 3
    assert "no package history" in capsys.readouterr().out


# =================================================================================================
# cleanup and parser
# =================================================================================================
def test_cleanup_goes_through_the_helper_action(cli, capsys) -> None:
    assert cli.main(["cleanup", "--yes"]) == 0
    assert cli.helper.calls == [("cleanup-old-packages", {})]
    capsys.readouterr()
    assert cli.main(["cleanup", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"      # stdout is only the JSON document


def test_the_cli_names_no_other_update_tool(cli) -> None:
    text = BIN.read_text(encoding="utf-8").lower()
    assert "mintupdate" not in text and "update manager" not in text and "linux mint" not in text


def test_parser_knows_every_subcommand(cli) -> None:
    parser = cli.build_parser()
    for command in ("status", "check", "plan", "apply", "history", "cleanup", "kernel-status", "sideload", "repo"):
        assert command in parser.format_help()


# =================================================================================================
# the real script, in a subprocess (hermetic: only commands that never call apt)
# =================================================================================================
def test_subprocess_status_without_and_with_a_state_file(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "status")
    assert proc.returncode == 3 and "no update information yet" in proc.stdout
    _write_state()
    proc = run_cli("lindos-update", "status", "--json")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["counts"]["total"] == 4


def test_subprocess_history(core_env, run_cli) -> None:
    _write_history()
    proc = run_cli("lindos-update", "history", "--json")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert len(json.loads(proc.stdout)["entries"]) == 2
