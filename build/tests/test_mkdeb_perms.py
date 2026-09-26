"""build/mkdeb.sh: is_exec_path() packaging-perms coverage (SPEC §1.1).

Regression test for the Secure-Boot signing hook (packages/lindos-kernel's
etc/kernel/postinst.d/zz-lindos-sbsign) being staged and shipped as 0644.

fix_perms() in build/mkdeb.sh chmod 0644's every staged file and then
re-chmods 0755 only the relative paths is_exec_path() matches. Debian/Ubuntu's
kernel-package postinst invokes every script in /etc/kernel/postinst.d/ (and
the sibling preinst.d/prerm.d/postrm.d directories) via run-parts, which
silently skips non-executable files — so a hook shipped as 0644 never runs on
a real install, with no error anywhere.

is_exec_path() is a shell function embedded in build/mkdeb.sh, which has no
"if run directly" guard (running the whole script performs a real package
build). Rather than source the whole file, this test extracts just the
is_exec_path() function body via a regex and evaluates it in a throwaway bash
subshell, then calls it with '&&'/'||' — matching how build/mkdeb.sh's own
fix_perms() consumes its return code.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                      # build/tests -> repo root
MKDEB = REPO_ROOT / "build" / "mkdeb.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

_FUNC_RE = re.compile(r"\nis_exec_path\(\)\s*\{.*?\n\}\n", re.DOTALL)


def _is_exec_path_func() -> str:
    text = MKDEB.read_text(encoding="utf-8")
    m = _FUNC_RE.search(text)
    assert m, "is_exec_path() function not found (or its shape changed) in build/mkdeb.sh"
    return m.group(0)


def _is_exec_path(rel: str) -> bool:
    assert BASH is not None
    func = _is_exec_path_func()
    script = f"{func}\nis_exec_path {shlex.quote(rel)}"
    res = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=10, check=False)
    assert res.returncode in (0, 1), (rel, res.returncode, res.stdout, res.stderr)
    return res.returncode == 0


# --- the confirmed finding: the Secure-Boot signing hook ---------------------------------

@pytest.mark.parametrize("rel", [
    "etc/kernel/postinst.d/zz-lindos-sbsign",
    "etc/kernel/postrm.d/some-hook",
    "etc/kernel/preinst.d/some-hook",
    "etc/kernel/prerm.d/some-hook",
])
def test_kernel_hook_dirs_are_executable(rel: str) -> None:
    assert _is_exec_path(rel), (
        f"{rel} must be treated as executable: Debian's kernel-package run-parts "
        "silently skips non-executable /etc/kernel/*.d/ hooks"
    )


def test_sbsign_hook_path_matches_shipped_file() -> None:
    # Keep this test honest against the real Addendum-W file, not just the pattern.
    shipped = (REPO_ROOT / "packages" / "lindos-kernel" / "root" / "etc" / "kernel"
               / "postinst.d" / "zz-lindos-sbsign")
    assert shipped.is_file(), shipped
    rel = str(shipped.relative_to(REPO_ROOT / "packages" / "lindos-kernel" / "root")).replace("\\", "/")
    assert rel == "etc/kernel/postinst.d/zz-lindos-sbsign"
    assert _is_exec_path(rel)


# --- unrelated etc/ paths must stay non-executable (no over-broad match) ----------------

@pytest.mark.parametrize("rel", [
    "etc/lindos/config.json",
    "etc/kernel-img.conf",
    "etc/kernel/README",
])
def test_unrelated_etc_paths_stay_non_executable(rel: str) -> None:
    assert not _is_exec_path(rel), rel


# --- existing exec-path rules must keep working (no regression from the new arm) --------

@pytest.mark.parametrize("rel", [
    "usr/bin/lindos-mode",
    "usr/sbin/lindos-thing",
    "usr/games/lindos-game",
    "usr/libexec/lindos/lindos-helper",
    "usr/lib/lindos-kernel/bin/tool",
    "some/script.sh",
    "DEBIAN/postinst",
    "DEBIAN/postrm",
    "etc/gamemode.d/lindos.ini",
    "etc/lindos/hooks.d/hook",
])
def test_existing_exec_rules_unaffected(rel: str) -> None:
    assert _is_exec_path(rel), rel


@pytest.mark.parametrize("rel", [
    "usr/lib/python3/dist-packages/lindos/helper.py",
    "usr/share/doc/README",
    "usr/share/applications/lindos-run.desktop",
])
def test_existing_non_exec_paths_unaffected(rel: str) -> None:
    assert not _is_exec_path(rel), rel
