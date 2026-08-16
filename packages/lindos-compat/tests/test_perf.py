"""Perf-env selection (SPEC-KERNEL §17.1) and gamescope wrapping (§17.2)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lindos_compat import cli_run, perf, runner


# --------------------------------------------------------------------------- #
# ntsync probe (LINDOS_ROOT-aware)
# --------------------------------------------------------------------------- #
def _slash(p) -> str:
    return str(p).replace("\\", "/")


def test_ntsync_dev_path_and_probe(monkeypatch):
    monkeypatch.delenv("LINDOS_ROOT", raising=False)
    assert perf.ntsync_dev_path() == Path("/dev/ntsync")
    monkeypatch.setenv("LINDOS_ROOT", "/fake/root")
    assert _slash(perf.ntsync_dev_path()).endswith("/fake/root/dev/ntsync")
    seen = {}

    def fake_exists(p):
        seen["p"] = p
        return _slash(p).endswith("/dev/ntsync")

    assert perf.ntsync_available(exists=fake_exists) is True
    assert _slash(seen["p"]).endswith("/fake/root/dev/ntsync")
    assert perf.ntsync_available(root="/other", exists=lambda p: False) is False


# --------------------------------------------------------------------------- #
# perf_env: with and without /dev/ntsync, override handling
# --------------------------------------------------------------------------- #
def test_perf_env_ntsync_present_and_absent():
    with_dev = perf.perf_env(kind="game", runner="umu", environ={}, ntsync=True)
    assert with_dev["PROTON_USE_NTSYNC"] == "1"
    assert with_dev["DXVK_ASYNC"] == "1"
    assert with_dev["PROTON_HIDE_NVIDIA_GPU"] == "0"
    without = perf.perf_env(kind="game", runner="umu", environ={}, ntsync=False)
    assert "PROTON_USE_NTSYNC" not in without          # silent fall back to fsync
    assert without["DXVK_ASYNC"] == "1"


def test_perf_env_respects_user_override():
    # user already set the values -> perf_env leaves them alone
    env = {"DXVK_ASYNC": "0", "PROTON_USE_NTSYNC": "0", "PROTON_HIDE_NVIDIA_GPU": "1"}
    out = perf.perf_env(kind="game", runner="umu", environ=env, ntsync=True)
    assert "DXVK_ASYNC" not in out and "PROTON_USE_NTSYNC" not in out and "PROTON_HIDE_NVIDIA_GPU" not in out
    # explicit flags always produce a value
    assert perf.perf_env(kind="game", runner="umu", environ=env, ntsync=True, dxvk_async=True)["DXVK_ASYNC"] == "1"
    assert perf.perf_env(kind="game", runner="umu", environ=env, ntsync=True, dxvk_async=False)["DXVK_ASYNC"] == "0"
    # hdr adds DXVK_HDR only when asked and not already set
    assert perf.perf_env(kind="game", runner="umu", environ={}, ntsync=False, hdr=True)["DXVK_HDR"] == "1"


def test_perf_env_is_empty_for_wine_app():
    assert perf.perf_env(kind="app", runner="wine", environ={}, ntsync=True) == {}
    assert perf.perf_env(kind="installer", runner="wine", environ={}, ntsync=True) == {}


# --------------------------------------------------------------------------- #
# gamescope
# --------------------------------------------------------------------------- #
def test_parse_geometry():
    assert perf.parse_geometry("2560x1440") == (2560, 1440)
    assert perf.parse_geometry("1920X1080") == (1920, 1080)
    assert perf.parse_geometry("") == (None, None)
    assert perf.parse_geometry("garbage") == (None, None)


def test_gamescope_wrap_prefers_lindos_then_raw_then_degrades(fake_which):
    spec = perf.GamescopeSpec(enabled=True, width=2560, height=1440, hdr=True, fsr=True)
    base = ["/usr/bin/umu-run", "game.exe"]
    argv, warns = perf.gamescope_wrap(list(base), spec, which=fake_which("lindos-gamescope", "gamescope"))
    assert argv[0].endswith("lindos-gamescope") and not warns
    assert "--width" in argv and "2560" in argv and "--hdr" in argv and "--fsr" in argv
    assert argv[-2:] == base and "--" in argv
    # only raw gamescope present
    argv, warns = perf.gamescope_wrap(list(base), spec, which=fake_which("gamescope"))
    assert argv[0].endswith("gamescope") and "-W" in argv and "fsr" in argv and "--hdr-enabled" in argv
    assert argv[-2:] == base
    # neither -> degrade with a warning, command unchanged
    argv, warns = perf.gamescope_wrap(list(base), spec, which=fake_which())
    assert argv == base and warns and "gamescope" in warns[0]
    # disabled spec is a no-op
    argv, warns = perf.gamescope_wrap(list(base), perf.GamescopeSpec(enabled=False), which=fake_which("gamescope"))
    assert argv == base and not warns


def test_build_command_wraps_in_gamescope(fake_which):
    spec = perf.GamescopeSpec(enabled=True)
    argv, warns = runner.build_command("umu", Path("/g/g.exe"), "game",
                                       which=fake_which("umu-run", "gamescope"), gamescope=spec)
    assert argv[0].endswith("gamescope") and "--" in argv and argv[-1] == str(Path("/g/g.exe"))


# --------------------------------------------------------------------------- #
# build_plan: perf env with and without /dev/ntsync (the required acceptance test)
# --------------------------------------------------------------------------- #
def _umu_plan(home, fake_which, **kw):
    w = fake_which("umu-run")
    return runner.build_plan(runner="umu", slug="g", exe=Path("/g/g.exe"), kind="game", config={},
                             which=w, home=home, nvidia=False, **kw)


def test_build_plan_sets_ntsync_only_when_device_present(home, fake_which):
    root = Path(os.environ["LINDOS_ROOT"])
    plan = _umu_plan(home, fake_which, environ={})
    assert "PROTON_USE_NTSYNC" not in plan.env       # no /dev/ntsync in the fake root
    assert plan.env["DXVK_ASYNC"] == "1"
    assert plan.env["PROTON_HIDE_NVIDIA_GPU"] == "0"
    # now create the device node under LINDOS_ROOT
    (root / "dev").mkdir(parents=True, exist_ok=True)
    (root / "dev" / "ntsync").write_text("")
    plan = _umu_plan(home, fake_which, environ={})
    assert plan.env["PROTON_USE_NTSYNC"] == "1"


def test_build_plan_dxvk_override_and_flags(home, fake_which):
    # user override in the environment wins (no explicit flag)
    plan = _umu_plan(home, fake_which, environ={"DXVK_ASYNC": "0"})
    assert "DXVK_ASYNC" not in plan.env               # left to os.environ at run time
    # explicit --no-dxvk-async / --dxvk-async force the value regardless of environ
    assert _umu_plan(home, fake_which, environ={"DXVK_ASYNC": "0"}, dxvk_async=False).env["DXVK_ASYNC"] == "0"
    assert _umu_plan(home, fake_which, environ={}, dxvk_async=True).env["DXVK_ASYNC"] == "1"


def test_build_plan_wine_gets_no_perf_env(home, fake_which):
    w = fake_which("wine")
    plan = runner.build_plan(runner="wine", slug="tool", exe=Path("/t/t.exe"), kind="app", config={},
                             which=w, home=home, environ={})
    assert "PROTON_USE_NTSYNC" not in plan.env and "PROTON_HIDE_NVIDIA_GPU" not in plan.env
    assert "DXVK_ASYNC" not in plan.env


# --------------------------------------------------------------------------- #
# CLI flags
# --------------------------------------------------------------------------- #
def test_cli_perf_flags_parse():
    p = cli_run.build_parser()
    ns = p.parse_args(["game.exe", "--gamescope", "2560x1440", "--hdr", "--fsr", "--no-dxvk-async"])
    assert ns.gamescope == "2560x1440" and ns.hdr and ns.fsr and ns.dxvk_async is False
    ns = p.parse_args(["game.exe", "--gamescope"])
    assert ns.gamescope == "" and ns.dxvk_async is None
    ns = p.parse_args(["game.exe", "--dxvk-async"])
    assert ns.dxvk_async is True
    with pytest.raises(SystemExit):
        p.parse_args(["game.exe", "--dxvk-async", "--no-dxvk-async"])  # mutually exclusive


def test_gamescope_spec_combines_flags_and_profile():
    from lindos_compat.profiles import Profile
    prof = Profile(id="x", gamescope={"w": 1920, "h": 1080, "fsr": True})
    p = cli_run.build_parser()
    ns = p.parse_args(["game.exe"])
    spec = cli_run.gamescope_spec(ns, prof)
    assert spec.enabled and spec.width == 1920 and spec.fsr and not spec.hdr
    ns = p.parse_args(["game.exe", "--gamescope", "2560x1440", "--hdr"])
    spec = cli_run.gamescope_spec(ns, prof)
    assert spec.width == 2560 and spec.height == 1440 and spec.hdr and spec.fsr
    # no profile, no flag -> disabled
    assert not cli_run.gamescope_spec(p.parse_args(["game.exe"]), None).enabled
