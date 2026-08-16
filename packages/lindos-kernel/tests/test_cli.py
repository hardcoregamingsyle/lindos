"""lindos-kernel CLI smoke tests: runs on Windows against a faked LINDOS_ROOT (SPEC-KERNEL §15.4)."""
from __future__ import annotations

import json


def _env(fake_root, **extra) -> dict:
    env = {"LINDOS_ROOT": str(fake_root["root"]), "LINDOS_KERNEL_RELEASE": "6.14.0-lindos"}
    env.update(extra)
    return env


def test_version(run_cli) -> None:
    res = run_cli("--version")
    assert res.returncode == 0
    assert "lindos-kernel" in res.stdout


def test_no_command_prints_help(run_cli) -> None:
    res = run_cli()
    assert res.returncode == 2
    assert "usage" in (res.stdout + res.stderr).lower()


def test_status_json(run_cli, fake_root) -> None:
    fake_root["touch"]("/dev/ntsync")
    fake_root["write"]("/boot/config-6.14.0-lindos", "CONFIG_HZ=1000\nCONFIG_PREEMPT=y\n")
    res = run_cli("status", "--json", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["release"] == "6.14.0-lindos"
    assert data["is_lindos"] is True
    assert data["features"]["ntsync"]["present"] is True


def test_features_json(run_cli, fake_root) -> None:
    res = run_cli("features", "--json", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["recommended"]["series"] == "6.14"
    ids = {row["id"] for row in data["features"]}
    assert "ntsync" in ids and "sched_ext" in ids


def test_cmdline_show_and_set(run_cli, fake_root, tmp_path) -> None:
    # plant a writable drop-in under LINDOS_ROOT
    fake_root["write"]("/etc/default/grub.d/50-lindos.cfg",
                       "# >>> lindos >>>\n"
                       'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT nowatchdog"\n'
                       "# <<< lindos <<<\n")
    show = run_cli("cmdline", "show", "--json", env=_env(fake_root))
    assert show.returncode == 0, show.stderr
    assert json.loads(show.stdout)["flags"] == ["nowatchdog"]

    res = run_cli("cmdline", "set", "quiet", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    assert "pkexec update-grub" in res.stdout  # prints the privileged follow-up, never runs it

    show2 = run_cli("cmdline", "show", "--json", env=_env(fake_root))
    assert "quiet" in json.loads(show2.stdout)["flags"]


def test_cmdline_preset_gaming(run_cli, fake_root) -> None:
    fake_root["write"]("/etc/default/grub.d/50-lindos.cfg",
                       "# >>> lindos >>>\n"
                       'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT"\n'
                       "# <<< lindos <<<\n")
    res = run_cli("cmdline", "--preset", "gaming", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    assert "mitigations=off" in res.stdout


def test_build_prints_plan_never_compiles(run_cli, fake_root) -> None:
    # Non-Linux: prints the plan and exits 0.  Linux: LINDOS_KERNEL_BUILD_DRYRUN keeps CI from
    # launching a real kernel compile.  Either way the build command must not hang or build.
    res = run_cli("build", "--jobs", "2", env=_env(fake_root, LINDOS_KERNEL_BUILD_DRYRUN="1"))
    assert res.returncode == 0, res.stderr
    assert "config fragment" in res.stdout
