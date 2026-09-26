"""The four lindos-update helper actions -- apt-get-update / system-upgrade /
cleanup-old-packages / install-local-debs (SPEC-UPDATE.md §36.4).

Covers both layers independently, exactly like the rest of this codebase's helper-action tests
(see ``test_core.py``'s ``test_validate_payload_accepts``/``rejects``/``test_helper_dry_run_*``):

* ``lindos.helper.validate_payload`` -- the client-side schema/whitelist (fast, no subprocess).
* ``lindos-helper``'s dry-run execution (subprocess, ``LINDOS_HELPER_DRYRUN=1`` from ``core_env``)
  -- argv shape, offline refusal, kernel-gate refusal.
* ``act_install_local_debs``'s server-side re-verification of the *real* ``dpkg-deb --field``
  ``Package:`` value (never trusting the filename a client already validated) -- loaded as a
  module (like ``test_dualboot.py``'s ``helper_mod`` fixture) since exercising this branch for
  real needs a fake, non-dry-run ``Ctx`` with ``which``/``run``/``_run_capture`` all injected.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from lindos import helper as lhelper

_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = _PKG_ROOT / "root"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
HELPER = LIBEXEC / "lindos-helper"

#: pretend to be online inside the dry-run helper (core_env forces offline by default) -- matches
#: test_core.py's own ONLINE_ENV constant exactly.
ONLINE_ENV = {"LINDOS_HELPER_ONLINE": "1", "LINDOS_FORCE_OFFLINE": "0"}


def _unquoted(text: str) -> str:
    """The helper shell-quotes arguments with spaces/backslashes (Windows temp paths); strip quotes."""
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
    """A fresh import of ``lindos-helper`` (env already points LINDOS_ROOT/HOME at scratch dirs)."""
    return _load_bin(HELPER, "lindos_core_helper_bin_update_test")


# =================================================================================================
# lindos.helper.validate_payload -- client-side schema/whitelist
# =================================================================================================
@pytest.mark.parametrize("action, payload", [
    ("apt-get-update", {}),
    ("cleanup-old-packages", {}),
    ("system-upgrade", {"packages": ["lindos-core=1.0.1"]}),
    ("system-upgrade", {"packages": ["lindos-core=1.0.1", "lindos-tune=1:2.0-1~rc1"]}),
    ("system-upgrade", {"packages": ["linux-image-6.14.0-lindos=1.0.1"], "allow_kernel": True}),
    ("system-upgrade", {"packages": ["linux-headers-6.14.0-lindos=1.0.1"], "allow_kernel": True}),
])
def test_validate_payload_accepts_update_actions(action: str, payload: Dict[str, Any]) -> None:
    out = lhelper.validate_payload(action, payload)
    assert isinstance(out, dict) and json.dumps(out)


@pytest.mark.parametrize("action, payload", [
    ("system-upgrade", {"packages": []}),
    ("system-upgrade", {"packages": "lindos-core=1.0.1"}),                 # must be a list
    ("system-upgrade", {"packages": ["lindos-core"]}),                      # bare name, no '=version'
    ("system-upgrade", {"packages": ["lindos-core="]}),                     # empty version
    ("system-upgrade", {"packages": ["=1.0.1"]}),                           # empty name
    ("system-upgrade", {"packages": ["--allow-unauthenticated=1"]}),        # sec-core:F2-style guard
    ("system-upgrade", {"packages": ["lindos-core=1.0.1; rm -rf /"]}),
    # kernel-gate refusal (the whole point of this action's extra check):
    ("system-upgrade", {"packages": ["linux-image-6.14.0-lindos=1.0.1"]}),
    ("system-upgrade", {"packages": ["linux-headers-6.14.0-lindos=1.0.1"]}),
    ("system-upgrade", {"packages": ["linux-modules-6.14.0-lindos=1.0.1"]}),
    ("system-upgrade", {"packages": ["linux-image-6.14.0-lindos=1.0.1"], "allow_kernel": False}),
    ("system-upgrade", {"packages": ["linux-image-generic=6.8.0.41.41"], "allow_kernel": "yes"}),  # not a bool
])
def test_validate_payload_rejects_system_upgrade(action: str, payload: Dict[str, Any]) -> None:
    with pytest.raises(lhelper.PayloadError):
        lhelper.validate_payload(action, payload)


def test_validate_payload_install_local_debs_accepts(tmp_path: Path) -> None:
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    out = lhelper.validate_payload("install-local-debs", {"files": [str(deb)]})
    assert out["files"] == [str(deb)]


def test_validate_payload_install_local_debs_dedupes(tmp_path: Path) -> None:
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    out = lhelper.validate_payload("install-local-debs", {"files": [str(deb), str(deb)]})
    assert out["files"] == [str(deb)]


@pytest.mark.parametrize("build_payload", [
    lambda tmp_path: {"files": []},
    lambda tmp_path: {"files": "not-a-list"},
    lambda tmp_path: {"files": [""]},
    lambda tmp_path: {"files": ["relative/lindos-core_1.0.1_all.deb"]},          # not absolute
    lambda tmp_path: {"files": [str(tmp_path / "lindos-core_1.0.1_all.tar.gz")]},  # wrong extension
    lambda tmp_path: {"files": [str(tmp_path / "evil-package_1.0.1_all.deb")]},   # not lindos-*
    lambda tmp_path: {"files": [str(tmp_path / "lindos-core_1.0.1_all.deb")]},    # never created -> "exists" fails
])
def test_validate_payload_rejects_install_local_debs(tmp_path: Path, build_payload) -> None:
    payload = build_payload(tmp_path)
    # only create the file for cases that are expected to fail for a DIFFERENT reason than "missing"
    files = payload.get("files")
    if isinstance(files, list):
        for f in files:
            if f and os.path.isabs(f) and "evil-package" in os.path.basename(f):
                Path(f).write_text("fake\n", encoding="utf-8")
            elif f and os.path.isabs(f) and f.endswith(".tar.gz"):
                Path(f).write_text("fake\n", encoding="utf-8")
    with pytest.raises(lhelper.PayloadError):
        lhelper.validate_payload("install-local-debs", payload)


# =================================================================================================
# lindos-helper: dry-run execution (subprocess)
# =================================================================================================
def test_helper_dry_run_apt_get_update(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "apt-get-update", "{}", env=ONLINE_ENV)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "apt-get update -q" in _unquoted(proc.stdout)


def test_helper_dry_run_system_upgrade(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "system-upgrade", json.dumps({"packages": ["lindos-core=1.0.1"]}),
                   env=ONLINE_ENV)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "apt-get install --only-upgrade -y -q" in out
    assert "-- lindos-core=1.0.1" in out


def test_helper_system_upgrade_kernel_gate_refused_at_the_door(core_env, run_cli) -> None:
    """Refused by validate_payload before the action ever runs -- exit 2, not 1, and apt-get is
    never invoked (no '[dry-run] would run' line at all)."""
    proc = run_cli("lindos-helper", "system-upgrade",
                   json.dumps({"packages": ["linux-image-6.14.0-lindos=1.0.1"]}), env=ONLINE_ENV)
    assert proc.returncode == 2
    assert "kernel package" in (proc.stdout + proc.stderr)
    assert "would run" not in proc.stdout


def test_helper_dry_run_system_upgrade_with_kernel_allowed(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "system-upgrade",
                   json.dumps({"packages": ["linux-image-6.14.0-lindos=1.0.1"], "allow_kernel": True}),
                   env=ONLINE_ENV)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "linux-image-6.14.0-lindos=1.0.1" in _unquoted(proc.stdout)


def test_helper_dry_run_cleanup_old_packages(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "cleanup-old-packages", "{}")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "apt-get autoremove --purge -y -q" in _unquoted(proc.stdout)


def test_helper_dry_run_install_local_debs(core_env, run_cli, tmp_path: Path) -> None:
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    proc = run_cli("lindos-helper", "install-local-debs", json.dumps({"files": [str(deb)]}))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "would verify" in out
    assert "dpkg -i --" in out and str(deb) in out
    assert "apt-get -f install -y -q" in out


def test_helper_install_local_debs_rejects_missing_file(core_env, run_cli, tmp_path: Path) -> None:
    proc = run_cli("lindos-helper", "install-local-debs",
                   json.dumps({"files": [str(tmp_path / "lindos-core_1.0.1_all.deb")]}))
    assert proc.returncode == 2
    assert "does not exist" in (proc.stdout + proc.stderr)


def test_helper_install_local_debs_rejects_non_lindos_name(core_env, run_cli, tmp_path: Path) -> None:
    deb = tmp_path / "evil-package_1.0_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    proc = run_cli("lindos-helper", "install-local-debs", json.dumps({"files": [str(deb)]}))
    assert proc.returncode == 2
    assert "not look like a lindos-* package" in (proc.stdout + proc.stderr)


def test_helper_update_actions_appear_in_list(core_env, run_cli) -> None:
    lst = run_cli("lindos-helper", "--list")
    assert lst.returncode == 0
    actions = lst.stdout.split()
    for action in ("apt-get-update", "system-upgrade", "cleanup-old-packages", "install-local-debs"):
        assert action in actions


# =================================================================================================
# act_install_local_debs: server-side re-verification of the REAL dpkg-deb Package field
# (never trusting the filename a client-side check already looked at)
# =================================================================================================
def _make_ctx(helper_mod, monkeypatch: pytest.MonkeyPatch, *, field_value: str):
    """A non-dry-run Ctx with which()/_run_capture()/run() all faked: 'dpkg-deb' resolves, its
    '--field ... Package' output is *field_value*, and every other command (dpkg -i, apt-get -f
    install) is recorded instead of actually executed."""
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: f"/usr/bin/{name}")
    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **_kwargs: Any) -> Tuple[bool, str]:
        calls.append(list(cmd))
        return True, ""
    monkeypatch.setattr(ctx, "run", fake_run)

    def fake_run_capture(_ctx: Any, cmd: List[str], timeout: float = 30) -> Tuple[bool, str]:
        assert cmd[1] == "--field"
        return True, field_value + "\n"
    monkeypatch.setattr(helper_mod, "_run_capture", fake_run_capture)
    return ctx, calls


def test_act_install_local_debs_trusts_the_real_package_field_when_it_matches(
        helper_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    ctx, calls = _make_ctx(helper_mod, monkeypatch, field_value="lindos-core")
    code = helper_mod.act_install_local_debs(ctx, {"files": [str(deb)]})
    assert code == helper_mod.EXIT_OK
    assert any(c[:2] == ["dpkg", "-i"] for c in calls)
    assert any(c[:3] == ["apt-get", "-f", "install"] for c in calls)


def test_act_install_local_debs_refuses_when_real_package_field_is_not_lindos(
        helper_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The filename looks perfectly fine ('lindos-core_...deb') -- but the *actual* embedded
    control field says otherwise. The helper must never trust the filename alone."""
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    ctx, calls = _make_ctx(helper_mod, monkeypatch, field_value="not-lindos-at-all")
    code = helper_mod.act_install_local_debs(ctx, {"files": [str(deb)]})
    assert code == helper_mod.EXIT_ERROR
    assert not any(c[:2] == ["dpkg", "-i"] for c in calls)


def test_act_install_local_debs_refuses_when_dpkg_deb_cannot_read_the_field(
        helper_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_text("fake\n", encoding="utf-8")
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: f"/usr/bin/{name}")
    calls: List[List[str]] = []
    monkeypatch.setattr(ctx, "run", lambda cmd, **_k: (calls.append(list(cmd)), (True, ""))[1])
    monkeypatch.setattr(helper_mod, "_run_capture", lambda _ctx, _cmd, timeout=30: (False, ""))
    code = helper_mod.act_install_local_debs(ctx, {"files": [str(deb)]})
    assert code == helper_mod.EXIT_ERROR
    assert not calls


def test_act_install_local_debs_empty_files_is_a_noop(helper_mod) -> None:
    ctx = helper_mod.Ctx(dry_run=True)
    assert helper_mod.act_install_local_debs(ctx, {"files": []}) == helper_mod.EXIT_OK


def test_act_system_upgrade_empty_packages_is_a_noop(helper_mod) -> None:
    ctx = helper_mod.Ctx(dry_run=True)
    assert helper_mod.act_system_upgrade(ctx, {"packages": []}) == helper_mod.EXIT_OK


# =================================================================================================
# offline refusal (act_apt_get_update / act_system_upgrade): only reachable in *real* (non-dry-run)
# mode -- ctx.online() is deliberately never consulted during a dry-run (same pattern as the
# pre-existing act_install_packages/act_install_flatpaks), and real mode needs real root, so this
# is exercised by calling the action directly against a faked Ctx instead of through the CLI.
# =================================================================================================
def test_act_apt_get_update_refuses_offline(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "online", lambda: False)
    calls: List[List[str]] = []
    monkeypatch.setattr(ctx, "run", lambda cmd, **_k: (calls.append(list(cmd)), (True, ""))[1])
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_ERROR
    assert not calls


def test_act_system_upgrade_refuses_offline(helper_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "online", lambda: False)
    calls: List[List[str]] = []
    monkeypatch.setattr(ctx, "run", lambda cmd, **_k: (calls.append(list(cmd)), (True, ""))[1])
    payload = {"packages": ["lindos-core=1.0.1"], "allow_kernel": False}
    assert helper_mod.act_system_upgrade(ctx, payload) == helper_mod.EXIT_ERROR
    assert not calls
