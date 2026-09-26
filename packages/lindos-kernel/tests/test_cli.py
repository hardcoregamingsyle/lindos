"""lindos-kernel CLI smoke tests: runs on Windows against a faked LINDOS_ROOT (SPEC-KERNEL §15.4)."""
from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CLI_PATH = HERE.parent / "root" / "usr" / "bin" / "lindos-kernel"


@pytest.fixture(scope="module")
def cli_module():
    """Load the `/usr/bin/lindos-kernel` script (no .py suffix) as an importable module, so
    its private helpers (e.g. `_newest_kernel_version`) can be unit-tested directly without
    going through a subprocess. `main()` is only invoked under `__name__ == "__main__"`, so
    importing it this way never runs the CLI."""
    loader = SourceFileLoader("lindos_kernel_cli_under_test", str(CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)  # type: ignore[union-attr]
    return module


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


def test_secureboot_status_json(run_cli, fake_root) -> None:
    fake_root["write"]("/etc/default/grub.d/51-lindos-kernel-select.cfg",
                       "# >>> lindos >>>\nGRUB_DEFAULT=\"0\"\n# <<< lindos <<<\n")
    res = run_cli("secureboot", "status", "--json", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["firmware"] in ("uefi", "bios")
    assert "mok" in data and "tools" in data and "kernels" in data


def test_secureboot_status_human_readable(run_cli, fake_root) -> None:
    res = run_cli("secureboot", "status", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    assert "Firmware" in res.stdout
    assert "Secure Boot" in res.stdout


def test_secureboot_apply_selection(run_cli, fake_root) -> None:
    fake_root["write"]("/etc/default/grub.d/51-lindos-kernel-select.cfg",
                       "# >>> lindos >>>\nGRUB_DEFAULT=\"0\"\n# <<< lindos <<<\n")
    res = run_cli("secureboot", "apply-selection", "--json", env=_env(fake_root))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["grub_default"] == "0"  # no Secure Boot info in the fake root -> allowed


# --- _newest_kernel_version (regression, correct-platform:F3) ------------------------------
# A plain lexicographic string sort mis-orders real kernel version strings: sorted(...)[-1]
# picks '6.8.0-40-generic' over '6.11.0-9-generic' because '1' < '8' at the first differing
# character, so `secureboot apply-selection` could name a stale kernel as the GRUB fallback
# entry instead of the actually-newest one.
def test_newest_kernel_version_is_numeric_not_lexicographic(cli_module) -> None:
    images = [
        {"version": "6.8.0-40-generic", "lindos": False},
        {"version": "6.11.0-9-generic", "lindos": False},
    ]
    assert cli_module._newest_kernel_version(images, lindos=False) == "6.11.0-9-generic"


def test_newest_kernel_version_filters_by_lindos_flag(cli_module) -> None:
    images = [
        {"version": "6.8.0-40-generic", "lindos": False},
        {"version": "6.14.0-lindos", "lindos": True},
    ]
    assert cli_module._newest_kernel_version(images, lindos=True) == "6.14.0-lindos"
    assert cli_module._newest_kernel_version(images, lindos=False) == "6.8.0-40-generic"


def test_newest_kernel_version_empty_returns_none(cli_module) -> None:
    assert cli_module._newest_kernel_version([], lindos=False) is None
    assert cli_module._newest_kernel_version([{"version": "1.0", "lindos": True}], lindos=False) is None


def test_build_prints_plan_never_compiles(run_cli, fake_root) -> None:
    # Non-Linux: prints the plan and exits 0.  Linux: LINDOS_KERNEL_BUILD_DRYRUN keeps CI from
    # launching a real kernel compile.  Either way the build command must not hang or build.
    res = run_cli("build", "--jobs", "2", env=_env(fake_root, LINDOS_KERNEL_BUILD_DRYRUN="1"))
    assert res.returncode == 0, res.stderr
    assert "config fragment" in res.stdout


def test_build_default_base_config_is_ubuntu(run_cli, fake_root) -> None:
    res = run_cli("build", env=_env(fake_root, LINDOS_KERNEL_BUILD_DRYRUN="1"))
    assert res.returncode == 0, res.stderr
    assert "base config    : ubuntu" in res.stdout


def test_build_base_config_flag_passthrough(run_cli, fake_root) -> None:
    res = run_cli("build", "--base-config", "defconfig",
                  env=_env(fake_root, LINDOS_KERNEL_BUILD_DRYRUN="1"))
    assert res.returncode == 0, res.stderr
    assert "base config    : defconfig" in res.stdout
