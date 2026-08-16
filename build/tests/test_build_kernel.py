"""build/kernel/build-kernel.sh: syntax + argument-parsing smoke (SPEC-KERNEL §15.5, §19).

Never triggers a real compile: only `bash -n` and the fast-exit argument paths (`--help`,
unknown option) are exercised.  Skipped cleanly when bash is unavailable (bare Windows CI).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                       # build/tests -> repo root
SCRIPT = REPO_ROOT / "build" / "kernel" / "build-kernel.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")


def _run(*args: str) -> subprocess.CompletedProcess:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, check=False)


def test_script_exists() -> None:
    assert SCRIPT.is_file(), SCRIPT


def test_bash_syntax_ok() -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60, check=False)
    assert res.returncode == 0, res.stderr


def test_help_exits_zero_and_prints_usage() -> None:
    res = _run("--help")
    assert res.returncode == 0
    out = res.stdout + res.stderr
    assert "build-kernel.sh" in out
    assert "--version" in out and "--kdir" in out


def test_unknown_option_is_usage_error() -> None:
    res = _run("--frobnicate")
    assert res.returncode == 2
    assert "unknown option" in (res.stdout + res.stderr)


def test_missing_value_for_flag_fails() -> None:
    # `--version` with no argument must fail (set -u / parameter check), never build.
    res = _run("--version")
    assert res.returncode != 0


def test_no_shebang_regression() -> None:
    first = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/bash"


def test_has_linux_host_guard() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    # must refuse to run off Linux (SPEC-KERNEL §15.5) and never call sudo
    assert 'uname -s' in text
    assert "exit 2" in text or "die " in text
    # no line may actually *invoke* sudo (mentions in comments / advice strings are fine)
    for line in text.splitlines():
        assert not line.strip().startswith("sudo "), line


def test_reads_fragment_from_package_source() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "usr/share/lindos/kernel/lindos.config" in text
    assert "merge_config.sh" in text
    assert "bindeb-pkg" in text
