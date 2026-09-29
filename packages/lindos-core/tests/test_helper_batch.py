"""The ``run-batch`` helper action: many whitelisted actions, ONE root process, ONE password prompt.

Three layers, like the other helper-action tests:

* ``lindos.helper.validate_payload("run-batch", ...)`` -- refusals and normalisation (no subprocess).
* ``lindos-helper`` itself: dry-run through a subprocess (``core_env`` sets ``LINDOS_HELPER_DRYRUN=1``)
  and, for failure isolation, ``act_run_batch`` loaded as a module with a failing/crashing handler.
* ``lindos.helper.run_privileged_batch`` -- the client wrapper, against the real dry-run helper and
  against tiny fake helpers that misbehave (crash, cancelled authentication, half a report).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import textwrap
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lindos import helper as lhelper

_PKG_ROOT = Path(__file__).resolve().parent.parent
HELPER = _PKG_ROOT / "root" / "usr" / "libexec" / "lindos" / "lindos-helper"

#: pretend to be online inside the dry-run helper (core_env forces offline by default)
ONLINE_ENV = {"LINDOS_HELPER_ONLINE": "1", "LINDOS_FORCE_OFFLINE": "0"}


def _unquoted(text: str) -> str:
    return text.replace("'", "")


def _load_bin(path: Path, modname: str) -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # never leave a __pycache__ inside root/usr/libexec (deb payload)
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


@pytest.fixture()
def helper_mod(core_env):
    return _load_bin(HELPER, "lindos_core_helper_bin_batch_test")


def _step(action: str, payload: Dict[str, Any], step_id: str = "") -> Dict[str, Any]:
    return {"id": step_id or action, "action": action, "payload": payload}


def _events(stdout: str) -> List[Dict[str, Any]]:
    return [ev for ev in (lhelper.parse_batch_line(ln) for ln in stdout.splitlines()) if ev]


# =================================================================================================
# validation
# =================================================================================================
def test_run_batch_is_whitelisted_and_listed(core_env, run_cli) -> None:
    assert "run-batch" in lhelper.ACTIONS
    assert "run-batch" in run_cli("lindos-helper", "--list").stdout.split()
    assert '"run-batch":' in HELPER.read_text(encoding="utf-8")      # a real handler exists


def test_validate_run_batch_normalises_every_step_with_its_own_validator() -> None:
    out = lhelper.validate_payload("run-batch", {"steps": [
        {"id": "cfg", "action": "write-system-config", "payload": {"mode": "gaming", "browser": "chrome", "x": 1}},
        {"action": "install-packages", "payload": {"packages": ["gimp", "gimp", " krita "]}},
        {"id": "gaming", "action": "install-gaming", "payload": {"items": ["steam"]}, "junk": True},
        {"action": "apt-get-update"},
    ], "ignored": 1})
    assert set(out) == {"steps"}
    assert out["steps"] == [
        {"id": "cfg", "action": "write-system-config", "payload": {"mode": "gaming", "browser": "chrome"}},
        {"id": "install-packages", "action": "install-packages", "payload": {"packages": ["gimp", "krita"]}},
        {"id": "gaming", "action": "install-gaming", "payload": {"items": ["steam"]}},
        {"id": "apt-get-update", "action": "apt-get-update", "payload": {}},
    ]
    assert json.loads(json.dumps(out)) == out


@pytest.mark.parametrize("payload, needle", [
    ({}, "'steps' must be a non-empty list"),
    ({"steps": []}, "'steps' must be a non-empty list"),
    ({"steps": "install-packages"}, "'steps' must be a non-empty list"),
    ({"steps": ["install-packages"]}, "steps[1] must be an object"),
    ({"steps": [{"payload": {}}]}, "unknown action"),
    ({"steps": [{"action": "rm-rf", "payload": {}}]}, "unknown action 'rm-rf'"),
    ({"steps": [{"action": 5}]}, "unknown action"),
    # no nesting
    ({"steps": [{"action": "run-batch", "payload": {"steps": [{"action": "apt-get-update"}]}}]},
     "not allowed inside run-batch"),
    # reboots and secrets never ride along in a generic batch
    ({"steps": [{"action": "reboot-to-windows", "payload": {"method": "bootnext", "entry": "0001"}}]},
     "not allowed inside run-batch"),
    ({"steps": [{"action": "firmware-setup", "payload": {"confirm": True}}]}, "not allowed inside run-batch"),
    ({"steps": [{"action": "import-wifi",
                 "payload": {"networks": [{"ssid": "x", "security": "wpa-psk", "psk": "hunter2hunter2"}]}}]},
     "not allowed inside run-batch"),
    # the per-action validator still applies: option-injection, bad enums, traversal
    ({"steps": [{"id": "pk", "action": "install-packages", "payload": {"packages": ["--allow-unauthenticated"]}}]},
     "steps[1] (pk): invalid entry in 'packages'"),
    ({"steps": [{"action": "install-browser", "payload": {"browser": "netscape"}}]}, "'browser' must be one of"),
    ({"steps": [{"action": "set-services", "payload": {"disable": ["sshd"]}}]}, "not in the whitelist"),
    ({"steps": [{"action": "install-packages", "payload": ["gimp"]}]}, "payload must be a JSON object"),
    ({"steps": [{"action": "apply-mode", "payload": {"mode": "everyday",
                                                      "apply_system": "/usr/share/lindos/modes/../../../../tmp/x/apply-system.sh"}}]},
     "'apply_system'"),
    # ids
    ({"steps": [{"id": "a b", "action": "apt-get-update"}]}, "'id' must be a short identifier"),
    ({"steps": [{"id": "x" * 65, "action": "apt-get-update"}]}, "'id' must be a short identifier"),
    ({"steps": [{"id": 3, "action": "apt-get-update"}]}, "'id' must be a short identifier"),
    ({"steps": [{"id": "same", "action": "apt-get-update"}, {"id": "same", "action": "cleanup-old-packages"}]},
     "duplicate step id"),
    ({"steps": [{"action": "apt-get-update"}, {"action": "apt-get-update"}]}, "duplicate step id"),
])
def test_validate_run_batch_refusals(payload: Dict[str, Any], needle: str) -> None:
    with pytest.raises(lhelper.PayloadError) as info:
        lhelper.validate_payload("run-batch", payload)
    assert needle in str(info.value)


def test_validate_run_batch_size_caps() -> None:
    at_limit = [{"id": f"s{i}", "action": "apt-get-update"} for i in range(lhelper.BATCH_MAX_STEPS)]
    assert len(lhelper.validate_payload("run-batch", {"steps": at_limit})["steps"]) == lhelper.BATCH_MAX_STEPS
    with pytest.raises(lhelper.PayloadError, match="at most 32 entries"):
        lhelper.validate_payload("run-batch", {"steps": at_limit + [{"id": "extra", "action": "apt-get-update"}]})
    huge = {"id": "big", "action": "install-packages",
            "payload": {"packages": [f"package-number-{i:06d}-padding-padding" for i in range(9000)]}}
    with pytest.raises(lhelper.PayloadError, match="too large"):
        lhelper.validate_payload("run-batch", {"steps": [huge]})
    with pytest.raises(lhelper.PayloadError, match="plain JSON"):
        lhelper.validate_payload("run-batch", {"steps": [{"action": "apt-get-update", "payload": {"x": object()}}]})


def test_run_batch_forbidden_set_covers_reboots_secrets_and_nesting() -> None:
    assert lhelper.BATCH_FORBIDDEN_ACTIONS == {"run-batch", "reboot-to-windows", "firmware-setup", "import-wifi"}
    assert lhelper.BATCH_FORBIDDEN_ACTIONS <= set(lhelper.ACTIONS)


# =================================================================================================
# the helper executable
# =================================================================================================
def test_helper_dry_run_batch_runs_every_step_in_order(core_env, run_cli) -> None:
    payload = {"steps": [
        _step("write-system-config", {"mode": "gaming", "browser": "chrome"}, "cfg"),
        _step("install-packages", {"packages": ["gimp"]}, "pk"),
        _step("install-compat", {"items": ["wine", "umu"]}, "compat"),
    ]}
    proc = run_cli("lindos-helper", "run-batch", "-", env=ONLINE_ENV, input=json.dumps(payload))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "system.json" in out and "install-compat.sh wine umu" in out
    assert "apt-get install -y -q" in out and "-- gimp" in out
    events = _events(proc.stdout)
    assert [(e["event"], e["id"]) for e in events] == [
        ("start", "cfg"), ("result", "cfg"), ("start", "pk"), ("result", "pk"),
        ("start", "compat"), ("result", "compat")]
    assert all(e["ok"] and e["code"] == 0 for e in events if e["event"] == "result")
    assert [e["index"] for e in events if e["event"] == "start"] == [1, 2, 3]
    # every marker is at column 0 and is the whole line (machine-readable, never mixed with output)
    for line in proc.stdout.splitlines():
        if lhelper.BATCH_MARKER.strip() in line:
            assert line.startswith(lhelper.BATCH_MARKER)
    log_text = (core_env["root"] / "var" / "log" / "lindos" / "helper.log").read_text(encoding="utf-8")
    assert "run-batch" in log_text


def test_helper_batch_refuses_bad_payloads_before_running_anything(core_env, run_cli) -> None:
    for steps, needle in (
        ([_step("run-batch", {"steps": [_step("apt-get-update", {})]})], "not allowed inside run-batch"),
        ([_step("install-packages", {"packages": ["gimp"]}), _step("nope", {})], "unknown action"),
        ([_step("install-packages", {"packages": ["gimp"]}), _step("set-zram", {"percent": 999})], "'percent'"),
        ([], "non-empty"),
    ):
        proc = run_cli("lindos-helper", "run-batch", json.dumps({"steps": steps}), env=ONLINE_ENV)
        assert proc.returncode == 2, (steps, proc.stdout)
        assert needle in proc.stderr
        assert "would run" not in proc.stdout and not _events(proc.stdout)   # nothing executed


def test_helper_batch_stdin_payload_is_size_capped(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "set-zram", "-", input='{"percent": 25}' + " " * (1024 * 1024 + 10))
    assert proc.returncode == 2 and "larger than" in proc.stderr
    ok = run_cli("lindos-helper", "set-zram", "-", input='{"percent": 25}')
    assert ok.returncode == 0


def test_helper_batch_updates_apt_lists_only_once(core_env, run_cli) -> None:
    payload = {"steps": [_step("install-packages", {"packages": ["gimp"]}, "a"),
                         _step("install-packages", {"packages": ["krita"]}, "b")]}
    proc = run_cli("lindos-helper", "run-batch", "-", env=ONLINE_ENV, input=json.dumps(payload))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("would run: apt-get update") == 1
    assert proc.stdout.count("would run: apt-get install") == 2
    # a single action is unchanged: it still updates first
    single = run_cli("lindos-helper", "install-packages", json.dumps({"packages": ["gimp"]}), env=ONLINE_ENV)
    assert single.stdout.count("would run: apt-get update") == 1


def _ctx_and_capture(helper_mod, capsys, steps: List[Dict[str, Any]]):
    ctx = helper_mod.Ctx(dry_run=True)
    code = helper_mod.act_run_batch(ctx, {"steps": steps})
    out = capsys.readouterr()
    return code, _events(out.out), out


def test_batch_isolates_failing_and_crashing_steps(helper_mod, monkeypatch, capsys) -> None:
    order: List[str] = []

    def failing(ctx, payload):
        order.append("failing")
        ctx.error("apt-get install failed: boom")
        ctx.error("second problem")
        return helper_mod.EXIT_ERROR

    def crashing(ctx, payload):
        order.append("crashing")
        raise RuntimeError("kaboom")

    def fine(ctx, payload):
        order.append("fine")
        return helper_mod.EXIT_OK

    monkeypatch.setitem(helper_mod.HANDLERS, "install-packages", failing)
    monkeypatch.setitem(helper_mod.HANDLERS, "install-flatpaks", crashing)
    monkeypatch.setitem(helper_mod.HANDLERS, "install-compat", fine)
    steps = [_step("install-packages", {"packages": ["gimp"]}),
             _step("install-flatpaks", {"flatpaks": ["org.gimp.GIMP"]}),
             _step("install-compat", {"items": ["wine"]})]
    code, events, out = _ctx_and_capture(helper_mod, capsys, steps)
    assert order == ["failing", "crashing", "fine"]           # a failure never stops the rest
    assert code == helper_mod.EXIT_ERROR
    results = {e["id"]: e for e in events if e["event"] == "result"}
    assert results["install-packages"]["ok"] is False and results["install-packages"]["message"] == "second problem"
    assert results["install-packages"]["code"] == helper_mod.EXIT_ERROR
    assert results["install-flatpaks"]["ok"] is False and "kaboom" in results["install-flatpaks"]["message"]
    assert results["install-compat"]["ok"] is True and results["install-compat"]["message"] == ""
    assert "run-batch finished with failed steps: install-packages, install-flatpaks" in out.err


def test_batch_reports_a_failed_step_without_error_lines_by_exit_code(helper_mod, monkeypatch, capsys) -> None:
    monkeypatch.setitem(helper_mod.HANDLERS, "set-zram", lambda ctx, payload: 7)
    code, events, _out = _ctx_and_capture(helper_mod, capsys, [_step("set-zram", {"percent": 10})])
    assert code == helper_mod.EXIT_ERROR
    res = [e for e in events if e["event"] == "result"][0]
    assert res["ok"] is False and res["code"] == 7 and res["message"] == "exit code 7"


def test_batch_revalidates_and_refuses_at_execution_time(helper_mod, monkeypatch, capsys) -> None:
    """act_run_batch does not trust its caller's normalisation: it runs the same validators again and
    refuses nesting/forbidden/unknown actions on its own (defence in depth)."""
    ran: List[str] = []
    monkeypatch.setitem(helper_mod.HANDLERS, "install-packages",
                        lambda ctx, payload: (ran.append("install-packages"), helper_mod.EXIT_OK)[1])
    steps = [
        {"id": "nested", "action": "run-batch", "payload": {"steps": []}},
        {"id": "reboot", "action": "reboot-to-windows", "payload": {"method": "bootnext", "entry": "0001"}},
        {"id": "ghost", "action": "no-such-action", "payload": {}},
        {"id": "evil", "action": "install-packages", "payload": {"packages": ["--allow-unauthenticated"]}},
        {"id": "ok", "action": "install-packages", "payload": {"packages": ["gimp"]}},
    ]
    code, events, _out = _ctx_and_capture(helper_mod, capsys, steps)
    assert code == helper_mod.EXIT_ERROR
    results = {e["id"]: e for e in events if e["event"] == "result"}
    for bad in ("nested", "reboot", "ghost", "evil"):
        assert results[bad]["ok"] is False and results[bad]["code"] == helper_mod.EXIT_USAGE, bad
    assert "not allowed inside run-batch" in results["nested"]["message"]
    assert "invalid payload for install-packages" in results["evil"]["message"]
    assert results["ok"]["ok"] is True
    assert ran == ["install-packages"]      # only the one valid step actually ran


def test_batch_interrupt_marks_remaining_steps_not_run(helper_mod, monkeypatch, capsys) -> None:
    def interrupted(ctx, payload):
        raise KeyboardInterrupt

    monkeypatch.setitem(helper_mod.HANDLERS, "install-packages", interrupted)
    steps = [_step("install-packages", {"packages": ["gimp"]}), _step("install-compat", {"items": ["wine"]})]
    code, events, _out = _ctx_and_capture(helper_mod, capsys, steps)
    assert code == helper_mod.EXIT_ERROR
    results = {e["id"]: e for e in events if e["event"] == "result"}
    assert results["install-packages"]["message"] == "interrupted"
    assert results["install-compat"]["ok"] is False and "not run" in results["install-compat"]["message"]


# =================================================================================================
# the client wrapper
# =================================================================================================
def test_parse_batch_line() -> None:
    line = lhelper.BATCH_MARKER + '{"event":"result","id":"a","ok":true}'
    assert lhelper.parse_batch_line(line) == {"event": "result", "id": "a", "ok": True}
    for junk in ("", "plain output", "  " + line, lhelper.BATCH_MARKER + "{not json",
                 lhelper.BATCH_MARKER + '["a"]', lhelper.BATCH_MARKER + '{"event":"other","id":"a"}',
                 lhelper.BATCH_MARKER + '{"event":"start"}'):
        assert lhelper.parse_batch_line(junk) is None, junk


def test_run_privileged_batch_end_to_end_with_the_dry_run_helper(core_env, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_HELPER_ONLINE", "1")
    monkeypatch.setenv("LINDOS_FORCE_OFFLINE", "0")
    steps = [("cfg", "write-system-config", {"mode": "lite", "browser": "firefox"}),
             {"id": "pk", "action": "install-packages", "payload": {"packages": ["gimp"]}},
             {"action": "install-gaming", "payload": {"items": ["steam"]}}]
    lines: List[str] = []
    seen: List[lhelper.BatchStepResult] = []
    res = lhelper.run_privileged_batch(steps, log=lines.append, on_step=seen.append)
    assert res.ok and res.code == 0, (res.out, res.err)
    assert [r.id for r in res.results] == ["cfg", "pk", "install-gaming"]
    assert [r.id for r in seen] == ["cfg", "pk", "install-gaming"]          # once per step, as they finish
    assert all(r.ok and r.code == 0 and r.message == "" for r in res.results)
    assert res.get("pk") is res.results[1] and res.get("nope") is None and res.failed == []
    assert any("system.json" in ln for ln in lines) and any("install-gaming.sh" in ln for ln in lines)
    assert not any(lhelper.BATCH_MARKER.strip() in ln for ln in lines)      # markers are not shown to the user
    assert "apt-get install" in res.get("pk").out and "install-gaming.sh" not in res.get("pk").out   # per-step output
    assert "install-gaming.sh" in res.get("install-gaming").out
    assert res.message == "ok" and res.results[0].to_helper_result().ok


def test_run_privileged_batch_isolates_invalid_steps_client_side(core_env) -> None:
    seen: List[str] = []
    res = lhelper.run_privileged_batch([
        {"id": "good", "action": "set-zram", "payload": {"percent": 40}},
        {"id": "evil", "action": "install-packages", "payload": {"packages": ["--allow-unauthenticated"]}},
        {"id": "nest", "action": "run-batch", "payload": {"steps": []}},
        {"id": "reboot", "action": "reboot-to-windows", "payload": {"method": "bootnext", "entry": "0001"}},
        {"id": "good", "action": "set-zram", "payload": {"percent": 50}},      # duplicate id
        "not a step",
        {"id": "after", "action": "set-governor", "payload": {"governor": "powersave"}},
    ], on_step=lambda r: seen.append(r.id))
    assert not res.ok
    by_id = {(i, r.id): r for i, r in enumerate(res.results)}
    assert [r.id for r in res.results] == ["good", "evil", "nest", "reboot", "good", "step-6", "after"]
    assert res.results[0].ok and res.results[6].ok                 # the valid ones ran despite the bad ones
    for index in (1, 2, 3, 4, 5):
        bad = res.results[index]
        assert not bad.ok and bad.code == lhelper.EXIT_USAGE and "invalid payload" in bad.message, by_id
    assert "duplicate step id" in res.results[4].message
    assert len(seen) == 7 and sorted(seen) == sorted(r.id for r in res.results)   # exactly once per submitted step


def test_run_privileged_batch_with_nothing_valid_never_spawns(core_env, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("must not start the helper")

    monkeypatch.setattr(lhelper.subprocess, "Popen", boom)
    res = lhelper.run_privileged_batch([{"id": "n", "action": "run-batch", "payload": {}}])
    assert not res.ok and res.code == lhelper.EXIT_USAGE and not res.results[0].ok
    empty = lhelper.run_privileged_batch([])
    assert empty.ok and empty.results == [] and empty.code == 0


def test_run_privileged_batch_helper_missing(core_env, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_HELPER", str(core_env["tmp"] / "no-such-helper"))
    seen: List[str] = []
    res = lhelper.run_privileged_batch([("a", "set-zram", {"percent": 1}), ("b", "set-zram", {"percent": 2})],
                                       on_step=lambda r: seen.append(r.id))
    assert not res.ok and res.code == 127 and seen == ["a", "b"]
    assert all(r.code == 127 and "helper not found" in r.message for r in res.results)


def _fake_helper(tmp: Path, body: str) -> Path:
    script = tmp / "fake-helper.py"
    script.write_text("import sys, json\n" + textwrap.dedent(body), encoding="utf-8")
    return script


def test_run_privileged_batch_cancelled_authentication_fails_every_step_once(core_env, monkeypatch) -> None:
    """pkexec exits 126 when the password dialog is cancelled: no step ran, each is reported failed exactly
    once, and nothing hangs or re-prompts."""
    script = _fake_helper(core_env["tmp"], "sys.stdin.read()\nsys.exit(126)\n")
    monkeypatch.setenv("LINDOS_HELPER", str(script))
    seen: List[str] = []
    res = lhelper.run_privileged_batch([("a", "set-zram", {"percent": 1}), ("b", "set-zram", {"percent": 2})],
                                       on_step=lambda r: seen.append(r.id))
    assert not res.ok and res.code == 126 and seen == ["a", "b"]
    assert [r.message for r in res.results] == ["authentication cancelled or not authorised"] * 2
    assert [r.code for r in res.results] == [126, 126]


def test_run_privileged_batch_large_payload_and_cancelled_authentication_still_reports_126(
        core_env, monkeypatch) -> None:
    """pkexec may exit (auth cancelled) without ever reading a payload that does not fit the pipe buffer:
    the write fails, but the real exit code must still be what the caller sees."""
    script = _fake_helper(core_env["tmp"], "sys.exit(126)\n")
    monkeypatch.setenv("LINDOS_HELPER", str(script))
    big = {"id": "big", "action": "install-packages",
           "payload": {"packages": [f"package-number-{i:05d}-padding" for i in range(6000)]}}
    assert len(json.dumps(big)) > 150_000
    seen: List[str] = []
    res = lhelper.run_privileged_batch([big, ("small", "set-zram", {"percent": 1})], on_step=lambda r: seen.append(r.id))
    assert not res.ok and res.code == 126 and seen == ["big", "small"]
    assert all(r.code == 126 and r.message == "authentication cancelled or not authorised" for r in res.results)


def test_run_privileged_batch_partial_report_then_crash(core_env, monkeypatch) -> None:
    marker = lhelper.BATCH_MARKER
    script = _fake_helper(core_env["tmp"], f"""
        steps = json.loads(sys.stdin.read())["steps"]
        first = steps[0]["id"]
        print({marker!r} + json.dumps({{"event": "start", "id": first}}), flush=True)
        print("doing the first thing", flush=True)
        print({marker!r} + json.dumps({{"event": "result", "id": first, "ok": True, "code": 0, "seconds": 0.5}}), flush=True)
        print({marker!r} + json.dumps({{"event": "result", "id": "not-a-step", "ok": True, "code": 0}}), flush=True)
        sys.stderr.write("error: the helper blew up\\n")
        sys.exit(1)
    """)
    monkeypatch.setenv("LINDOS_HELPER", str(script))
    seen: List[lhelper.BatchStepResult] = []
    lines: List[str] = []
    res = lhelper.run_privileged_batch([("a", "set-zram", {"percent": 1}), ("b", "set-zram", {"percent": 2})],
                                       log=lines.append, on_step=seen.append)
    assert not res.ok and res.code == 1
    assert [(r.id, r.ok) for r in seen] == [("a", True), ("b", False)]     # 'a' streamed live, 'b' synthesised
    assert res.results[0].seconds == 0.5 and res.results[0].out == "doing the first thing"
    assert "the helper blew up" in res.results[1].message
    assert "doing the first thing" in lines and not any(lhelper.BATCH_MARKER.strip() in ln for ln in lines)


def test_run_privileged_batch_survives_a_raising_callback(core_env) -> None:
    def bad_cb(_res):
        raise RuntimeError("UI blew up")

    res = lhelper.run_privileged_batch([("a", "set-zram", {"percent": 1}), ("b", "set-governor", {"governor": "powersave"})],
                                       log=lambda _l: 1 / 0, on_step=bad_cb)
    assert res.ok and [r.id for r in res.results] == ["a", "b"]


def test_run_privileged_batch_sends_the_payload_over_stdin_not_argv(core_env, monkeypatch) -> None:
    captured: Dict[str, Any] = {}
    real_popen = lhelper.subprocess.Popen

    def spy(cmd, *a, **k):
        captured["cmd"] = list(cmd)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(lhelper.subprocess, "Popen", spy)
    res = lhelper.run_privileged_batch([("a", "set-fan-profile", {"profile": "zz-sentinel-profile-zz"})])
    assert res.ok
    assert captured["cmd"][-2:] == ["run-batch", "-"]
    assert not any("zz-sentinel-profile-zz" in part for part in captured["cmd"])
    assert os.path.basename(captured["cmd"][1]) == "lindos-helper"
