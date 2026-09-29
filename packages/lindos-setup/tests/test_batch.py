"""One password prompt for the whole first-boot apply: every ``kind=system`` step of a plan goes
through a SINGLE ``lindos.helper.run_privileged_batch`` call; the user-side half of ``apply-mode``
still runs as the user afterwards; failures stay isolated per step.

The batch is configuration only: the wizard never installs anything (the installer did), so the
Mode's system plan is always built with ``install=False``.

A fake ``lindos`` package is injected (like ``test_core.py``) so the wizard side is exercised on any
OS; ``test_default_plan_through_the_real_helper_dry_run`` additionally drives the real
``lindos.helper`` client against the real ``lindos-helper`` script in dry-run mode.
"""
from __future__ import annotations

import os
import shutil
import sys
import types
from typing import Any, Dict, List

import pytest

from lindos_setup import core
from lindos_setup import plan as planmod
from lindos_setup.plan import Plan, Runner, Selections, Step, build_plan

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "lindos-core", "root"))
REAL_HELPER = os.path.join(CORE_ROOT, "usr", "libexec", "lindos", "lindos-helper")

SYSTEM_IDS = ["write-system-config", "apply-mode"]


@pytest.fixture(autouse=True)
def _clear_cache():
    core._module_cache.clear()
    yield
    core._module_cache.clear()


class _FakeConfig:
    store: Dict[str, Any] = {}

    @classmethod
    def load(cls):
        inst = cls()
        inst.data = dict(cls.store)
        return inst

    def set(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self):
        _FakeConfig.store = dict(self.data)


class Env:
    """The fake lindos world plus everything the tests want to observe/configure."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []          # ordered, across every fake module
        self.installed = {"firefox"}          # browsers considered installed
        self.fail_ids: Dict[str, str] = {}    # batch step id -> failure message
        self.batch_code = 0                   # helper exit code of the batch
        self.cancel = False                   # authentication cancelled: nothing runs
        self.system_plan_error = None
        self.batches: List[List[Dict[str, Any]]] = []
        self.singles: List[str] = []


def _install_fake_lindos(monkeypatch, tmp_path) -> Env:
    env = Env()
    calls = env.calls

    pkg = types.ModuleType("lindos")
    pkg.__path__ = []
    paths = types.ModuleType("lindos.paths")
    paths.SETUP_DONE = "~/.config/lindos/setup-done"
    paths.LOG_DIR = "~/.local/state/lindos"
    paths.USER_CONF_DIR = "~/.config/lindos"
    config = types.ModuleType("lindos.config")
    _FakeConfig.store = {}
    config.Config = _FakeConfig

    theme = types.ModuleType("lindos.theme")
    for name in ("set_dark", "set_accent", "set_wallpaper", "set_taskbar_alignment"):
        theme.__dict__[name] = (lambda n: (lambda *a: calls.append((n,) + a)))(name)

    modes = types.ModuleType("lindos.modes")

    def system_plan(mode_id, *, system=False, offline=False, install=True):
        calls.append(("system_plan", mode_id, install))
        if env.system_plan_error:
            raise env.system_plan_error
        return {"mode": mode_id, "offline": offline, "packages": ["thunderbird"], "flatpaks": [],
                "install": install}

    def apply_mode(mode_id, *, system=False, dry_run=False, log=print, defer_system=False, install=True):
        calls.append(("apply_mode(user)", mode_id, defer_system))
        assert defer_system is True, "the wizard must never let apply_mode ask for a password itself"
        log("fake user half of %s" % mode_id)
        return types.SimpleNamespace(ok=True, steps=[("write-user-config", True, "")])

    modes.system_plan = system_plan
    modes.apply_mode = apply_mode

    browsers = types.ModuleType("lindos.browsers")
    browsers.BROWSERS = {"edge": {}, "chrome": {}, "firefox": {}}
    browsers.install_preflight = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the wizard installs nothing"))
    browsers.install = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the wizard installs nothing"))
    browsers.is_installed = lambda bid: bid in env.installed
    browsers.set_default = lambda bid: (calls.append(("set_default", bid)), True)[1]

    helper = types.ModuleType("lindos.helper")

    def run_privileged(action, payload=None, log=None, **_kw):
        env.singles.append(action)
        calls.append(("run_privileged", action))
        return types.SimpleNamespace(ok=True, out="", err="", code=0)

    def run_privileged_batch(steps, log=None, on_step=None, timeout=None):
        steps = list(steps)
        env.batches.append(steps)
        calls.append(("batch", tuple(s["id"] for s in steps)))
        results = []
        for s in steps:
            if env.cancel:
                res = types.SimpleNamespace(id=s["id"], action=s["action"], ok=False, code=126,
                                            message="authentication cancelled or not authorised")
            elif s["id"] in env.fail_ids:
                res = types.SimpleNamespace(id=s["id"], action=s["action"], ok=False, code=1,
                                            message=env.fail_ids[s["id"]])
            else:
                if log:
                    log("[batch] ran %s" % s["id"])
                res = types.SimpleNamespace(id=s["id"], action=s["action"], ok=True, code=0, message="")
            results.append(res)
            if on_step:
                on_step(res)
        return types.SimpleNamespace(ok=all(r.ok for r in results), results=results, out="", err="",
                                     code=126 if env.cancel else env.batch_code)

    helper.run_privileged = run_privileged
    helper.run_privileged_batch = run_privileged_batch

    hardware = types.ModuleType("lindos.hardware")
    hardware.ram_info = lambda: {"total": 3900}
    for name, mod in (("lindos", pkg), ("lindos.paths", paths), ("lindos.config", config),
                      ("lindos.theme", theme), ("lindos.modes", modes), ("lindos.browsers", browsers),
                      ("lindos.helper", helper), ("lindos.hardware", hardware)):
        monkeypatch.setitem(sys.modules, name, mod)
    core._module_cache.clear()

    root = tmp_path / "root"
    wall = root / "usr" / "share" / "backgrounds" / "lindos"
    wall.mkdir(parents=True)
    (wall / "aurora-dark.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    return env


def _default_plan(**sel_kw) -> Plan:
    return build_plan(Selections(**sel_kw))


def _run(plan: Plan, logs=None):
    return Runner(plan, core.make_real_executors(plan), log=(logs.append if logs is not None else (lambda m: None))).run()


# ---------------------------------------------------------------------------------------------
# (b) exactly one batch, zero per-step helper calls, and never an install
# ---------------------------------------------------------------------------------------------
def test_default_plan_has_only_the_two_configuration_system_steps():
    plan = _default_plan()
    assert [s.id for s in plan.system_steps()] == SYSTEM_IDS


def test_default_plan_makes_exactly_one_batch_call_and_no_per_step_helper_calls(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    plan = _default_plan()
    logs: List[str] = []
    result = _run(plan, logs)
    assert result.ok, [(r.step_id, r.message) for r in result.results if not r.ok]
    assert len(env.batches) == 1, "every privileged step must share one helper run (one password prompt)"
    assert env.singles == [] and not [c for c in env.calls if c[0] == "run_privileged"]
    entries = env.batches[0]
    assert [e["id"] for e in entries] == SYSTEM_IDS
    assert [e["action"] for e in entries] == SYSTEM_IDS
    payloads = {e["id"]: e["payload"] for e in entries}
    assert payloads["write-system-config"] == {"mode": "everyday", "browser": "chrome"}
    # the Mode's plan still lists its packages, but tells the helper NOT to install them
    assert payloads["apply-mode"] == {"mode": "everyday", "offline": False, "packages": ["thunderbird"],
                                      "flatpaks": [], "install": False}
    assert ("system_plan", "everyday", False) in env.calls
    # every plan step still got its own result and callbacks-worthy outcome, in plan order
    assert [r.step_id for r in result.results] == [s.id for s in plan.steps]
    assert any("single administrator prompt" in line for line in logs)


def test_the_batch_never_carries_an_install_action(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    for mode in planmod.MODE_IDS:
        for browser in planmod.BROWSER_IDS:
            env.installed = {"firefox", "chrome", "edge"}
            _run(_default_plan(mode=mode, browser=browser))
    actions = {e["action"] for batch in env.batches for e in batch}
    assert actions == {"write-system-config", "apply-mode"}
    assert all(e["payload"].get("install") is False for batch in env.batches for e in batch
               if e["action"] == "apply-mode")


def test_batch_is_triggered_by_the_first_system_step_not_before(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    plan = _default_plan()
    done: List[tuple] = []
    runner = Runner(plan, core.make_real_executors(plan), log=lambda m: None,
                    on_step_done=lambda i, n, step, res: done.append((step.id, len(env.batches))))
    runner.run()
    # per-step callbacks are unchanged: one per plan step, in order; no batch until the first system step
    assert [sid for sid, _n in done] == [s.id for s in plan.steps]
    batches_after = dict(done)
    assert batches_after["set-wallpaper"] == 0 and batches_after["write-system-config"] == 1
    assert all(batches_after[sid] == 1 for sid in SYSTEM_IDS)       # never a second batch
    order = [c[0] for c in env.calls]
    assert order.index("set_dark") < order.index("batch")


def test_batch_runs_once_even_if_asked_twice(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    batch = core.SystemBatch(_default_plan())
    batch.run(lambda m: None)
    batch.run(lambda m: None)
    assert len(env.batches) == 1


# ---------------------------------------------------------------------------------------------
# (d) user-side work still runs, as the user, after the batch
# ---------------------------------------------------------------------------------------------
def test_user_side_steps_run_after_the_batch(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.installed.add("chrome")            # the installer put Chrome there
    result = _run(_default_plan())
    assert result.ok
    names = [c[0] for c in env.calls]
    batch_at = names.index("batch")
    apply_user_at = names.index("apply_mode(user)")
    set_default_at = names.index("set_default")
    assert batch_at < apply_user_at < set_default_at
    assert ("apply_mode(user)", "everyday", True) in env.calls
    assert ("set_default", "chrome") in env.calls


def test_a_failed_system_part_is_reported_on_apply_mode_but_its_user_half_still_ran(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.fail_ids = {"apply-mode": "apply-mode everyday finished with errors: lindos-tune"}
    result = _run(_default_plan())
    step = result.get("apply-mode")
    assert step.failed and "system part failed: apply-mode everyday finished with errors: lindos-tune" in step.message
    assert ("apply_mode(user)", "everyday", True) in env.calls
    assert result.failed_ids == ["apply-mode"]


# ---------------------------------------------------------------------------------------------
# failure isolation / cancelled authentication / prepare-time problems
# ---------------------------------------------------------------------------------------------
def test_one_failing_batch_step_does_not_affect_the_others(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.fail_ids = {"write-system-config": "cannot write /etc/lindos/system.json"}
    result = _run(_default_plan())
    assert "write-system-config" in result.failed_ids
    assert "helper write-system-config failed: cannot write /etc/lindos/system.json" in \
        result.get("write-system-config").message
    assert result.get("apply-mode").ok
    assert result.get("set-theme").ok
    assert len(env.batches) == 1


def test_cancelled_authentication_fails_the_system_steps_once_without_reprompting(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.cancel = True
    result = _run(_default_plan())
    assert len(env.batches) == 1 and env.singles == []          # one prompt, cancelled: never asked again
    assert set(result.failed_ids) >= set(SYSTEM_IDS)
    assert "authentication cancelled or not authorised" in result.get("write-system-config").message
    # user-side work is unaffected and the mode's user half still ran
    for step_id in ("write-config", "set-theme", "set-accent", "set-wallpaper", "set-taskbar-alignment"):
        assert result.get(step_id).ok, step_id
    assert ("apply_mode(user)", "everyday", True) in env.calls
    assert "system part failed" in result.get("apply-mode").message


def test_a_step_that_cannot_be_prepared_fails_alone(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.system_plan_error = KeyError("unknown mode 'everyday'")
    result = _run(_default_plan())
    assert "apply-mode" in result.failed_ids and "KeyError" in result.get("apply-mode").message
    assert [e["id"] for e in env.batches[0]] == ["write-system-config"]
    assert result.get("write-system-config").ok


def test_the_helper_module_raising_fails_every_privileged_step_not_the_wizard(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise OSError("pkexec vanished")

    sys.modules["lindos.helper"].run_privileged_batch = boom
    result = _run(_default_plan())
    assert set(result.failed_ids) >= set(SYSTEM_IDS)
    assert "pkexec vanished" in result.get("write-system-config").message
    assert result.get("set-theme").ok and env.singles == []


def test_helper_that_forgets_a_step_is_reported_not_hung(monkeypatch, tmp_path):
    _install_fake_lindos(monkeypatch, tmp_path)
    good = sys.modules["lindos.helper"].run_privileged_batch

    def forgetful(steps, log=None, on_step=None, timeout=None):
        return good(steps[:1], log=log, on_step=on_step)

    sys.modules["lindos.helper"].run_privileged_batch = forgetful
    result = _run(_default_plan())
    assert result.get("write-system-config").ok
    assert "the helper did not report this step" in result.get("apply-mode").message


# ---------------------------------------------------------------------------------------------
# the browser: nothing is installed or probed; a pending Chrome is a preference, not a failure
# ---------------------------------------------------------------------------------------------
def test_a_pending_browser_never_reaches_the_helper_and_does_not_fail_the_run(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    plan = build_plan(Selections(browser="chrome"), browser_states={"chrome": "pending"})
    result = _run(plan)
    assert result.ok, [(r.step_id, r.message) for r in result.results if not r.ok]
    assert [e["id"] for e in env.batches[0]] == SYSTEM_IDS
    assert env.batches[0][0]["payload"]["browser"] == "chrome"      # the silent retry reads system.json
    assert not [c for c in env.calls if c[0] == "set_default"], "the personal default is left alone"
    assert "not installed yet" in result.get("set-default-browser").message


def test_an_installed_browser_is_made_the_default(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    env.installed.add("edge")
    result = _run(_default_plan(browser="edge"))
    assert result.ok
    assert ("set_default", "edge") in env.calls


# ---------------------------------------------------------------------------------------------
# compatibility: no plan -> the original per-step executors; plan without system steps
# ---------------------------------------------------------------------------------------------
def test_without_a_plan_each_privileged_step_still_calls_the_helper_itself(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    execs = core.make_real_executors()
    step = Step("write-system-config", "Save", planmod.KIND_SYSTEM, planmod.ACT_WRITE_SYSTEM_CONFIG,
                {"mode": "gaming", "browser": "firefox"})
    assert execs[planmod.ACT_WRITE_SYSTEM_CONFIG](step, lambda m: None) == (True, "")
    assert env.singles == ["write-system-config"] and env.batches == []


def test_plan_without_system_steps_keeps_the_plain_executors(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    plan = Plan([Step("set-theme", "Theme", planmod.KIND_USER, planmod.ACT_SET_THEME, {"dark": True})],
                Selections())
    execs = core.make_real_executors(plan)
    assert set(execs) == set(core.make_real_executors())
    assert execs[planmod.ACT_APPLY_MODE] is not None and env.batches == []


def test_plan_subset_only_batches_the_system_steps_it_has(monkeypatch, tmp_path):
    env = _install_fake_lindos(monkeypatch, tmp_path)
    steps = [Step("write-system-config", "Save", planmod.KIND_SYSTEM, planmod.ACT_WRITE_SYSTEM_CONFIG,
                  {"mode": "work", "browser": "firefox"})]
    plan = Plan(steps, Selections())
    result = Runner(plan, core.make_real_executors(plan), log=lambda m: None).run()
    assert result.ok and [e["id"] for e in env.batches[0]] == ["write-system-config"]


def test_legacy_install_steps_in_an_old_plan_are_never_batched(monkeypatch, tmp_path):
    """A plan written before the installer flow may still name install-* steps: they have no
    executor and are not in the batch (a skipped step is not a failure, and nothing is installed)."""
    env = _install_fake_lindos(monkeypatch, tmp_path)
    plan = _default_plan()
    plan = Plan(list(plan.steps) + [Step("install-compat", "Wine", planmod.KIND_SYSTEM, "install-compat",
                                         {"items": ["wine"]})], plan.selections)
    result = _run(plan)
    assert result.ok and result.skipped_ids == ["install-compat"]
    assert "install-compat" not in [e["action"] for e in env.batches[0]]


# ---------------------------------------------------------------------------------------------
# the real client + the real dry-run helper
# ---------------------------------------------------------------------------------------------
def test_default_plan_through_the_real_helper_dry_run(monkeypatch, tmp_path):
    """Real lindos.helper + real lindos-helper script (dry-run): one subprocess for the whole plan, no
    per-step run_privileged, every system step reports back, and the helper is told not to install."""
    try:
        from lindos import browsers as rbrowsers
        from lindos import helper as rhelper
        from lindos import modes as rmodes
        from lindos import theme as rtheme
    except ImportError:      # pragma: no cover - lindos-core is always in the tree
        pytest.skip("lindos-core not importable")
    core._module_cache.clear()
    root = tmp_path / "root"
    shutil.copytree(os.path.join(CORE_ROOT, "usr", "share", "lindos", "modes"),
                    root / "usr" / "share" / "lindos" / "modes")
    wall = root / "usr" / "share" / "backgrounds" / "lindos"
    wall.mkdir(parents=True)
    (wall / "aurora-dark.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LINDOS_HELPER", REAL_HELPER)
    monkeypatch.setenv("LINDOS_HELPER_DRYRUN", "1")
    monkeypatch.setenv("LINDOS_HELPER_ONLINE", "1")
    monkeypatch.setenv("LINDOS_FORCE_OFFLINE", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

    seen: Dict[str, Any] = {"batches": 0, "singles": 0, "popen": 0, "user_apply": []}
    real_batch = rhelper.run_privileged_batch

    def spy_batch(*a, **k):
        seen["batches"] += 1
        return real_batch(*a, **k)

    def no_single(*_a, **_k):
        seen["singles"] += 1
        raise AssertionError("run_privileged must not be used per step")

    real_popen = rhelper.subprocess.Popen

    def spy_popen(*a, **k):
        seen["popen"] += 1
        return real_popen(*a, **k)

    monkeypatch.setattr(rhelper, "run_privileged_batch", spy_batch)
    monkeypatch.setattr(rhelper, "run_privileged", no_single)
    monkeypatch.setattr(rhelper.subprocess, "Popen", spy_popen)
    monkeypatch.setattr(rmodes, "apply_mode",
                        lambda mode_id, **kw: (seen["user_apply"].append((mode_id, kw.get("defer_system"))),
                                               types.SimpleNamespace(ok=True, steps=[]))[1])
    monkeypatch.setattr(rbrowsers, "is_installed", lambda bid: bid in ("firefox", "chrome"))
    monkeypatch.setattr(rbrowsers, "set_default", lambda bid: True)
    for name in ("set_dark", "set_accent", "set_wallpaper", "set_taskbar_alignment"):
        monkeypatch.setattr(rtheme, name, lambda *a, **k: True)

    logs: List[str] = []
    plan = _default_plan(mode="gaming")          # a Mode that has packages and Flatpaks of its own
    result = Runner(plan, core.make_real_executors(plan), log=logs.append).run()
    system_results = {sid: result.get(sid) for sid in SYSTEM_IDS}
    assert all(r is not None and r.ok for r in system_results.values()), \
        [(sid, r.message) for sid, r in system_results.items() if r is not None and not r.ok] + logs
    assert seen["batches"] == 1 and seen["singles"] == 0 and seen["popen"] == 1
    assert seen["user_apply"] == [("gaming", True)]
    text = "\n".join(logs)
    assert "install disabled: configuration only" in text
    for word in ("installing packages", "installing flatpak", "install-browser.sh", "install-compat.sh"):
        assert word not in text.replace("'", ""), word
    assert "system.json" in text
    assert "@@lindos-batch" not in text
