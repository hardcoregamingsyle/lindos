"""lindos.installstate: the record of what the installer did and what is still pending, its command
line (used by shell scripts) and ``lindos-config install-state``.  Hermetic: LINDOS_ROOT/--root only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import pytest

from lindos import installstate as ist
from lindos import paths

PKG_ROOT = Path(__file__).resolve().parent.parent
PYLIB = PKG_ROOT / "root" / "usr" / "lib" / "python3" / "dist-packages"


def _state_file(root: Path) -> Path:
    return root / "var" / "lib" / "lindos" / "install-state.json"


# --- reading is tolerant ---------------------------------------------------------------------------
def test_missing_file_is_an_empty_state(core_env) -> None:
    state = ist.load()
    assert state == {"schema": 1, "updated": "", "online": None, "steps": {}}
    assert ist.status("browser") == "" and ist.pending() == [] and ist.is_terminal("browser") is False


@pytest.mark.parametrize("content", [
    "", "not json", "[]", "null", "42", '"text"', '{"steps": []}', '{"steps": "x"}',
    '{"steps": {"browser": "done"}}', '{"steps": {"browser": {"status": "bogus"}}}',
    '{"steps": {"browser": {"detail": "no status"}}}', '{"steps": {"browser": null}}',
])
def test_corrupt_or_odd_files_never_raise(core_env, content: str) -> None:
    path = _state_file(core_env["root"])
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    state = ist.load()
    assert state["steps"] == {} and state["schema"] == 1 and state["online"] is None
    assert ist.status("browser") == "" and ist.pending() == []
    # and a later mark() repairs the file
    ist.mark("browser", "done")
    assert json.loads(path.read_text(encoding="utf-8"))["steps"]["browser"]["status"] == "done"


def test_load_keeps_good_entries_and_drops_bad_fields(core_env) -> None:
    path = _state_file(core_env["root"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": 1, "updated": "2026-09-29T10:00:00Z", "online": "maybe",
        "steps": {"browser": {"status": "done", "detail": 5, "time": None},
                  "drivers": {"status": "pending", "detail": "offline", "time": "2026-09-29T10:00:01Z"},
                  "flatpaks": {"status": "weird"}}}), encoding="utf-8")
    state = ist.load()
    assert state["updated"] == "2026-09-29T10:00:00Z" and state["online"] is None
    assert state["steps"]["browser"] == {"status": "done", "detail": "", "time": ""}
    assert state["steps"]["drivers"]["detail"] == "offline"
    assert "flatpaks" not in state["steps"]


# --- mark ---------------------------------------------------------------------------------------------
def test_mark_writes_the_documented_schema_atomically(core_env) -> None:
    ist.mark("browser", "done", "google-chrome-stable 141.0")
    path = _state_file(core_env["root"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "updated", "online", "steps"}
    assert data["schema"] == 1 and data["online"] is None
    entry = data["steps"]["browser"]
    assert set(entry) == {"status", "detail", "time"}
    assert entry["status"] == "done" and entry["detail"] == "google-chrome-stable 141.0"
    for stamp in (data["updated"], entry["time"]):
        assert len(stamp) == 20 and stamp.endswith("Z") and stamp[10] == "T", stamp
    assert not list(path.parent.glob(".lindos-*")), "no temp file may be left behind"
    assert path.stat().st_size > 0 and "\r" not in path.read_text(encoding="utf-8")


def test_mark_validates_step_and_status(core_env) -> None:
    with pytest.raises(ist.StateError):
        ist.mark("chrome", "done")                       # not a step id
    with pytest.raises(ist.StateError):
        ist.mark("browser", "finished")                  # not a status
    with pytest.raises(ValueError):                      # StateError is a ValueError
        ist.mark("", "done")
    assert not _state_file(core_env["root"]).exists()   # nothing written for a rejected call


def test_every_documented_step_and_status_is_accepted(core_env) -> None:
    assert ist.STEPS == ("updates", "drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks")
    assert ist.STATUSES == ("done", "pending", "skipped", "failed")
    for step in ist.STEPS:
        for status in ist.STATUSES:
            ist.mark(step, status)
            assert ist.status(step) == status


def test_mark_preserves_other_steps_and_unknown_ones(core_env) -> None:
    path = _state_file(core_env["root"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "updated": "x", "online": True, "steps": {
        "future_step": {"status": "pending", "detail": "from a newer installer", "time": "t"}}}), encoding="utf-8")
    ist.mark("updates", "done", "14 packages")
    ist.mark("drivers", "failed", "dkms build failed")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["online"] is True                                    # untouched
    assert set(data["steps"]) == {"future_step", "updates", "drivers"}
    assert data["steps"]["future_step"]["detail"] == "from a newer installer"
    assert ist.pending() == ["drivers", "future_step"]               # known ids first, then the rest


def test_detail_is_one_short_line(core_env) -> None:
    ist.mark("gaming", "failed", "line one\nline two\r\n\tthird   " + "x" * 500)
    detail = ist.load()["steps"]["gaming"]["detail"]
    assert "\n" not in detail and "\r" not in detail and "\t" not in detail
    assert len(detail) == ist.DETAIL_MAX and detail.startswith("line one line two third x")
    ist.mark("gaming", "done")
    assert ist.load()["steps"]["gaming"]["detail"] == ""


def test_remarking_replaces_the_step_and_refreshes_the_time(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter(["2026-09-29T10:00:00Z", "2026-09-29T10:00:00Z", "2026-09-29T10:05:00Z", "2026-09-29T10:05:00Z"])
    monkeypatch.setattr(ist, "now_iso", lambda: next(times))
    ist.mark("browser", "pending", "offline")
    first = ist.load()
    ist.mark("browser", "done", "installed")
    second = ist.load()
    assert first["steps"]["browser"]["status"] == "pending" and first["updated"] == "2026-09-29T10:00:00Z"
    assert second["steps"]["browser"] == {"status": "done", "detail": "installed", "time": "2026-09-29T10:05:00Z"}
    assert second["updated"] == "2026-09-29T10:05:00Z"


# --- status / pending / terminal -----------------------------------------------------------------------
def test_pending_lists_pending_and_failed_in_canonical_order(core_env) -> None:
    ist.mark("flatpaks", "pending", "offline")
    ist.mark("updates", "done")
    ist.mark("drivers", "failed", "dkms")
    ist.mark("compat", "skipped", "user choice")
    ist.mark("browser", "pending")
    assert ist.pending() == ["drivers", "browser", "flatpaks"]
    assert [ist.is_terminal(s) for s in ("updates", "compat", "drivers", "browser", "gaming")] == \
        [True, True, False, False, False]
    assert ist.status("nothing-like-this") == ""


def test_explicit_root_is_a_prefix_and_beats_lindos_root(core_env, tmp_path: Path) -> None:
    target = tmp_path / "target"
    ist.mark("browser", "done", "in /target", root=str(target))
    assert (target / "var" / "lib" / "lindos" / "install-state.json").is_file()
    assert not _state_file(core_env["root"]).exists()                # LINDOS_ROOT's file was not touched
    assert ist.status("browser", root=str(target)) == "done" and ist.status("browser") == ""
    assert ist.state_path(str(target)) == os.path.normpath(str(target / "var" / "lib" / "lindos" / "install-state.json"))
    assert ist.state_path("") == paths.install_state() == ist.state_path(None)


def test_set_online_records_true_false_unknown(core_env) -> None:
    assert ist.set_online(True)["online"] is True and ist.load()["online"] is True
    assert ist.set_online(False)["online"] is False
    assert ist.set_online(None)["online"] is None and ist.load()["online"] is None
    with pytest.raises(ist.StateError):
        ist.set_online("yes")   # type: ignore[arg-type]
    ist.mark("browser", "done")
    ist.set_online(True)
    assert ist.status("browser") == "done"                           # steps survive


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is POSIX flock; Windows has no fcntl (best effort there)")
def test_concurrent_marks_do_not_lose_updates(core_env) -> None:
    """Two first-boot services (browser + drivers) write different steps at the same moment."""
    errors: list = []

    def worker(step: str) -> None:
        try:
            for _ in range(15):
                ist.mark(step, "pending", step)
                ist.mark(step, "done", step)
        except Exception as exc:  # pragma: no cover - would fail the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in ("browser", "drivers", "updates")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    steps = ist.load()["steps"]
    assert set(steps) == {"browser", "drivers", "updates"} and all(v["status"] == "done" for v in steps.values())


def _fake_fcntl(monkeypatch: pytest.MonkeyPatch, *, busy_times: int):
    """A stand-in for POSIX fcntl (absent on Windows): flock() is busy ``busy_times`` times first."""
    import types
    calls: list = []
    fake = types.ModuleType("fcntl")
    fake.LOCK_EX, fake.LOCK_NB = 2, 4           # type: ignore[attr-defined]

    def flock(fd: int, how: int) -> None:
        calls.append(how)
        if len([c for c in calls if c == 2 | 4]) <= busy_times:
            raise BlockingIOError("locked by someone else")

    fake.flock = flock                          # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    return calls


def test_lock_is_taken_retried_and_released(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_fcntl(monkeypatch, busy_times=2)
    ist.mark("browser", "done")
    assert calls.count(2 | 4) == 3                                   # busy, busy, then acquired
    assert ist.status("browser") == "done"
    assert _state_file(core_env["root"]).with_name("install-state.json.lock").exists()


def test_a_stuck_lock_holder_never_hangs_the_boot(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_fcntl(monkeypatch, busy_times=10 ** 9)            # never acquired
    monkeypatch.setattr(ist, "LOCK_WAIT_SECONDS", 0.2)
    ist.mark("drivers", "failed", "still recorded without the lock")
    assert len(calls) >= 2 and ist.status("drivers") == "failed"


def test_state_module_is_not_part_of_the_system_defaults() -> None:
    """install-state.json is its own file: /etc/lindos/system.json's defaults stay as they were."""
    from lindos import config as lconfig
    assert lconfig.SYSTEM_DEFAULTS == {"mode": "everyday", "browser": "chrome", "oem": False}
    assert "install_state" not in lconfig.DEFAULTS and "install-state" not in lconfig.DEFAULTS


# --- module command line (used by the shell scripts) -----------------------------------------------------
def _cli(root: Optional[Path], *args: str, env_root: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYLIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LINDOS_ROOT", None)
    if env_root is not None:
        env["LINDOS_ROOT"] = str(env_root)
    cmd = [sys.executable, "-m", "lindos.installstate"]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd + list(args), capture_output=True, text=True, env=env, timeout=60)


def test_cli_mark_show_pending_status_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "target"
    assert _cli(target, "mark", "browser", "pending", "offline", "at", "install").returncode == 0
    assert _cli(target, "mark", "drivers", "done").returncode == 0
    assert _cli(target, "mark", "flatpaks", "failed", "timeout after 40 min").returncode == 0
    shown = _cli(target, "show")
    assert shown.returncode == 0
    data = json.loads(shown.stdout)
    assert data["steps"]["browser"]["detail"] == "offline at install"
    assert data["steps"]["flatpaks"]["detail"] == "timeout after 40 min"
    pend = _cli(target, "pending")
    assert pend.stdout.split() == ["browser", "flatpaks"] and "\r" not in pend.stdout
    for step, expected in (("browser", "pending"), ("drivers", "done"), ("updates", "")):
        out = _cli(target, "status", step)
        assert out.returncode == 0 and out.stdout == expected + "\n", (step, out.stdout)


def test_cli_root_default_is_lindos_root_and_root_flag_wins(tmp_path: Path) -> None:
    env_root = tmp_path / "envroot"
    flag_root = tmp_path / "flagroot"
    assert _cli(None, "mark", "updates", "done", env_root=env_root).returncode == 0
    assert _state_file(env_root).is_file()
    assert _cli(flag_root, "mark", "gaming", "done", env_root=env_root).returncode == 0
    assert _state_file(flag_root).is_file()
    assert json.loads(_state_file(env_root).read_text(encoding="utf-8"))["steps"].keys() == {"updates"}


def test_cli_online_and_errors(tmp_path: Path) -> None:
    root = tmp_path / "t"
    assert _cli(root, "online", "false").returncode == 0
    assert json.loads(_cli(root, "show").stdout)["online"] is False
    assert _cli(root, "online", "true").returncode == 0 and json.loads(_cli(root, "show").stdout)["online"] is True
    assert _cli(root, "online", "unknown").returncode == 0 and json.loads(_cli(root, "show").stdout)["online"] is None
    bad = _cli(root, "online", "sometimes")
    assert bad.returncode == 2 and "online" in bad.stderr
    bad_step = _cli(root, "mark", "chrome", "done")
    assert bad_step.returncode == 2 and "unknown install step" in bad_step.stderr
    bad_status = _cli(root, "mark", "browser", "great")
    assert bad_status.returncode == 2 and "unknown status" in bad_status.stderr
    assert _cli(root).returncode == 2                                # no command: usage
    assert _cli(root, "frobnicate").returncode == 2


def test_cli_unwritable_location_is_exit_1(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    proc = _cli(blocker, "mark", "browser", "done")
    assert proc.returncode == 1 and "cannot write" in proc.stderr


# --- lindos-config install-state ----------------------------------------------------------------------------
def test_lindos_config_install_state_json(core_env, run_cli) -> None:
    empty = run_cli("lindos-config", "install-state", "--json")
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == {"schema": 1, "updated": "", "online": None, "steps": {}}
    ist.mark("browser", "pending", "offline")
    ist.mark("updates", "done", "12 upgraded")
    ist.set_online(False)
    data = json.loads(run_cli("lindos-config", "install-state", "--json").stdout)
    assert data["online"] is False and data["steps"]["browser"]["status"] == "pending"
    assert data["steps"]["updates"]["detail"] == "12 upgraded"


def test_lindos_config_install_state_text(core_env, run_cli) -> None:
    none = run_cli("lindos-config", "install-state")
    assert none.returncode == 0 and "not written" in none.stdout and "pending: nothing" in none.stdout
    for step in ist.STEPS:
        assert step in none.stdout
    ist.mark("browser", "pending", "offline at install")
    ist.mark("drivers", "done")
    ist.mark("flatpaks", "failed", "timeout")
    ist.set_online(True)
    text = run_cli("lindos-config", "install-state").stdout
    assert "online: yes" in text
    assert "browser" in text and "pending" in text and "offline at install" in text
    assert "pending: browser, flatpaks" in text
    assert "install-state.json" in text


def test_lindos_config_paths_lists_the_state_file(core_env, run_cli) -> None:
    data = json.loads(run_cli("lindos-config", "paths", "--json").stdout)
    assert data["INSTALL_STATE"] == paths.install_state()
    assert data["INSTALL_STATE"].endswith("install-state.json")
