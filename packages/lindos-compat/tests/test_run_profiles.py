"""lindos-run applies per-title profiles and honestly refuses not_possible titles (§17.3)."""
from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from lindos_compat import cli_run, runner

# The per-title starter set is shipped by lindos-gaming (single owner). lindos-compat
# supplies the loader (lindos_compat.profiles) and lindos-run, tested against that copy.
SHIPPED = (Path(__file__).resolve().parents[2] / "lindos-gaming" / "root" / "usr" / "share"
           / "lindos" / "gaming" / "profiles")


@pytest.fixture()
def shipped_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("LINDOS_PROFILES_DIR", str(SHIPPED))
    monkeypatch.setenv("LINDOS_USER_PROFILES_DIR", str(tmp_path / "no-user-profiles"))


def test_cli_applies_profile_in_plan(fake_core, home, shipped_profiles, tmp_path, capsys, monkeypatch, fake_which,
                                     make_pe):
    w = fake_which("umu-run", "wine")
    monkeypatch.setattr(cli_run, "choose_runner", functools.partial(runner.choose_runner, which=w, bottles_installed=False))
    monkeypatch.setattr(cli_run, "build_plan", functools.partial(runner.build_plan, which=w, home=home, nvidia=False))
    exe = tmp_path / "EldenRing.exe"
    exe.write_bytes(make_pe())
    rc = cli_run.main(["--dry-run", str(exe)])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["runner"] == "umu"          # profile forces the umu runner for an "app" exe
    assert plan["env"]["PROTONPATH"] == "GE-Proton"     # profile proton "GE-Proton-latest"
    assert plan["env"]["PROTON_USE_NTSYNC"] == "1"      # profile env
    assert plan["env"]["DXVK_ASYNC"] == "1"             # profile dxvk_async


def test_cli_refuses_not_possible_profile(fake_core, home, shipped_profiles, tmp_path, caplog, make_pe):
    import logging

    exe = tmp_path / "VALORANT.exe"
    exe.write_bytes(make_pe())
    # lindos-run's logger sets propagate=False, so caplog's root handler never
    # sees its records; attach the capture handler directly to that logger so
    # the assertion works the same on every host.
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="lindos-run"):
            rc = cli_run.main([str(exe)])
    finally:
        logger.removeHandler(caplog.handler)
    # not_possible = unsupported and explained (SPEC-WINDOWS §28.3 exit code 3), with the §30 route hint
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "cannot run on Lindos" in caplog.text and "Vanguard" in caplog.text
    assert "lindos-game route valorant" in caplog.text


def test_cli_info_shows_profile_and_perf_env(fake_core, home, shipped_profiles, tmp_path, capsys, monkeypatch,
                                             make_pe):
    monkeypatch.setattr(cli_run, "choose_runner", lambda *a, **k: ("umu", "forced for test"))
    exe = tmp_path / "Cyberpunk2077.exe"
    exe.write_bytes(make_pe())
    rc = cli_run.main(["--info", str(exe)])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["profile"]["id"] == "cyberpunk-2077"
    assert data["gamescope"]["fsr"] is True
    assert data["perf_env"]["DXVK_ASYNC"] == "1" and data["perf_env"]["PROTON_HIDE_NVIDIA_GPU"] == "0"


def test_cli_not_possible_shown_in_info(fake_core, home, shipped_profiles, tmp_path, capsys, monkeypatch, make_pe):
    monkeypatch.setattr(cli_run, "choose_runner", lambda *a, **k: ("umu", "forced for test"))
    exe = tmp_path / "VALORANT.exe"
    exe.write_bytes(make_pe())
    rc = cli_run.main(["--info", str(exe)])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["profile"]["status"] == "not_possible"
    assert data["route_hint"] == "lindos-game route valorant"
