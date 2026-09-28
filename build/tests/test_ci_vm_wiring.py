"""CI + build wiring for Addendum V (SPEC-VM §25, §20, §26).

Covers the pieces this repository's CI/build layer owns:
  * .github/workflows/ci.yml gains a `kernel` job (workflow_dispatch input
    build_kernel + on tags) that installs the kernel toolchain, runs
    build/kernel/build-kernel.sh and uploads the image/header .debs; and the
    `iso` job optionally downloads that artifact and still builds without it.
  * build/chroot/75-vm.sh installs the optional lindos-vm / lindos-winapps
    packages when present, idempotently, and ships NO spoofing (honesty §20).
  * packages/lindos-meta Recommends lindos-vm and lindos-winapps.

Stdlib-only text assertions so the test runs on the bare Windows pytest job;
one structural check uses PyYAML when it happens to be installed.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                       # build/tests -> repo root
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HOOK = REPO_ROOT / "build" / "chroot" / "75-vm.sh"
KERNEL_HOOK = REPO_ROOT / "build" / "chroot" / "35-kernel.sh"
META_CONTROL = REPO_ROOT / "packages" / "lindos-meta" / "DEBIAN" / "control"
BASH = shutil.which("bash")

# Anti-cheat / VM-detection evasion tokens that must NEVER appear anywhere in
# the wiring we own (SPEC-VM §20, §26 no-spoof assertion).
SPOOF_TOKENS = (
    "kvm=off",
    "hv-vendor-id",
    "hv_vendor_id",
    "smbios",
    "acpitable",
    "-acpitable",
    "hidden state",
    "hypervisor hiding",
    "spoof",
    "-M smm",  # not a spoof itself, but guards against copy-pasted evasion recipes
    "vm-detect",
)
# Tokens legitimately allowed as *negations* in comments (we assert absence of
# real usage, not of honest disclaimers), so scan only non-comment content.


def _text(path: Path) -> str:
    assert path.is_file(), path
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# ci.yml — kernel job
# --------------------------------------------------------------------------- #
def test_ci_yml_exists() -> None:
    assert CI_YML.is_file(), CI_YML


def test_kernel_job_present_and_triggered() -> None:
    t = _text(CI_YML)
    assert "\n  kernel:" in t, "no 'kernel' job"
    assert "build_kernel:" in t, "no build_kernel workflow_dispatch input"
    assert "inputs.build_kernel" in t, "kernel job not gated on build_kernel"
    assert "refs/tags/" in t, "kernel job not triggered on tags"
    assert 'tags: ["v*"]' in t or "tags:" in t, "push tags trigger missing"


def test_kernel_job_installs_toolchain() -> None:
    t = _text(CI_YML)
    # libdw-dev (provides dwarf.h) is required by scripts/gendwarfksyms on this kernel series --
    # missing it fails the build deep inside the compile, not at any earlier dependency check
    # (regression: run 36297620250, "gendwarfksyms.h: fatal error: dwarf.h: No such file or
    # directory"). Distinct from `dwarves` (provides `pahole`, checked separately elsewhere).
    for pkg in ("bc", "bison", "flex", "libssl-dev", "libelf-dev", "libdw-dev", "dpkg-dev"):
        assert pkg in t, f"kernel job does not install {pkg}"


def test_kernel_job_runs_build_script_with_version_and_jobs() -> None:
    t = _text(CI_YML)
    assert "build/kernel/build-kernel.sh" in t
    assert "--version" in t and "--jobs" in t
    assert "$(nproc)" in t, "kernel job must build with nproc jobs"
    # version resolved from the manifest (SPEC-VM §25 'from manifest')
    assert "manifest.json" in t and "recommended" in t


def test_kernel_job_caches_source_tree() -> None:
    t = _text(CI_YML)
    assert "actions/cache@" in t
    assert "out/kernel-src" in t, "kernel source tree is not cached"


def test_kernel_job_uploads_image_and_headers() -> None:
    t = _text(CI_YML)
    assert "lindos-kernel-debs" in t
    assert "out/kernel/linux-image-*.deb" in t
    assert "out/kernel/linux-headers-*.deb" in t


def test_kernel_job_excludes_debug_symbols_package() -> None:
    # Regression: `make bindeb-pkg` also produces linux-image-<ver>-dbg_*.deb (1.3+ GB for
    # 6.14.0-lindos, vs ~47 MB for the real image), which "linux-image-*.deb" above also
    # matches -- it made `gh run download -n lindos-kernel-debs` take 20+ minutes for nothing.
    # actions/upload-artifact@v4's path input supports "!"-prefixed exclude patterns.
    t = _text(CI_YML)
    m = re.search(r"name: lindos-kernel-debs\n(?:[ \t]*#.*\n)*[ \t]*path: \|\n((?:[ \t]+\S.*\n)+)", t)
    assert m, "could not find the lindos-kernel-debs upload-artifact 'path:' block"
    path_block = m.group(1)
    assert "!out/kernel/linux-image-*-dbg_*.deb" in path_block, (
        "lindos-kernel-debs upload must exclude the debug-symbols package"
    )


# --------------------------------------------------------------------------- #
# ci.yml — iso job optionally consumes the kernel artifact
# --------------------------------------------------------------------------- #
def test_iso_job_optionally_downloads_kernel_artifact() -> None:
    t = _text(CI_YML)
    assert "actions/download-artifact@" in t
    # must be optional: a skipped kernel job must not break the ISO
    assert "continue-on-error: true" in t
    # the ISO job must still run when the kernel job was skipped
    assert "always()" in t
    # and it stages the debs where 35-kernel.sh will find them
    assert "out/debs" in t


# --------------------------------------------------------------------------- #
# 35-kernel.sh chroot hook — find_kernel_debs() debug-symbols exclusion
# --------------------------------------------------------------------------- #
def _extract_function(script_text: str, name: str) -> str:
    """Pull one `name() { ... }` function body out of the real script by its source text (same
    technique as build/tests/test_boot_test_qa.py's helper of the same name), so the test
    exercises the exact shipped implementation instead of a re-typed copy."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?\n)^\}}\n", script_text, re.M | re.S)
    assert m, f"could not find function {name}() in {KERNEL_HOOK}"
    return f"{name}() {{\n{m.group(1)}}}\n"


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_find_kernel_debs_excludes_debug_symbols_package(tmp_path: Path) -> None:
    # Regression: linux-image-<ver>-dbg_*.deb (debug symbols, 1.3+ GB) matches the same
    # "*-lindos*"/"*lindos*" globs as the real linux-image-<ver>_*.deb -- `dpkg -i`'ing it into
    # the ISO chroot would bloat the ISO by well over a gigabyte for nothing.
    debs_dir = tmp_path / "debs"
    debs_dir.mkdir()
    for name in (
        "linux-image-6.14.0-lindos_6.14.0-2_amd64.deb",
        "linux-image-6.14.0-lindos-dbg_6.14.0-2_amd64.deb",
        "linux-headers-6.14.0-lindos_6.14.0-2_amd64.deb",
    ):
        (debs_dir / name).write_bytes(b"")
    script_text = KERNEL_HOOK.read_text(encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\nset -Eeuo pipefail\n"
        f'LINDOS_KERNEL_DEBS_DIR="{tmp_path / "nonexistent"}"\n'
        f'LINDOS_DEBS_DIR="{debs_dir}"\n'
        + _extract_function(script_text, "find_kernel_debs")
        + "\nfind_kernel_debs\n",
        encoding="utf-8",
    )
    res = subprocess.run([BASH, str(harness)], capture_output=True, text=True, timeout=30,
                        check=False)
    assert res.returncode == 0, res.stderr
    found = [Path(p).name for p in res.stdout.splitlines() if p]
    assert "linux-image-6.14.0-lindos_6.14.0-2_amd64.deb" in found
    assert "linux-headers-6.14.0-lindos_6.14.0-2_amd64.deb" in found
    assert "linux-image-6.14.0-lindos-dbg_6.14.0-2_amd64.deb" not in found, (
        "find_kernel_debs() must exclude the debug-symbols package: " + repr(found)
    )


# --------------------------------------------------------------------------- #
# 75-vm.sh chroot hook
# --------------------------------------------------------------------------- #
def test_hook_exists_and_shebang() -> None:
    lines = _text(HOOK).splitlines()
    assert lines[0] == "#!/bin/bash"
    assert any(line.strip() == "set -Eeuo pipefail" for line in lines)


def test_hook_syntax_ok() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this host")
    res = subprocess.run([bash, "-n", str(HOOK)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60, check=False)
    assert res.returncode == 0, res.stderr


def test_hook_installs_both_packages_idempotently() -> None:
    t = _text(HOOK)
    assert "lindos-vm" in t and "lindos-winapps" in t
    assert "pkg_installed" in t, "hook must be idempotent (skip already-installed)"
    # sources the shared hook library, like every other NN-*.sh hook
    assert "lib.sh" in t


def test_hook_never_calls_sudo() -> None:
    for line in _text(HOOK).splitlines():
        assert not line.strip().startswith("sudo "), line


def test_hook_survives_absent_packages() -> None:
    # the hook must log-and-continue, never `die`, when a package was not built
    t = _text(HOOK)
    assert "not built" in t or "skipped" in t


# --------------------------------------------------------------------------- #
# Honesty: no spoofing tokens anywhere in the wiring we own (SPEC-VM §20/§26)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [CI_YML, HOOK])
def test_no_spoof_tokens_in_noncomment_lines(path: Path) -> None:
    for raw in _text(path).splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue  # honest disclaimers in comments are allowed
        low = raw.lower()
        for tok in SPOOF_TOKENS:
            assert tok.lower() not in low, f"{path.name}: forbidden token {tok!r} in: {raw}"


# --------------------------------------------------------------------------- #
# lindos-meta Recommends
# --------------------------------------------------------------------------- #
def test_meta_recommends_vm_and_winapps() -> None:
    t = _text(META_CONTROL)
    rec = next((ln for ln in t.splitlines() if ln.startswith("Recommends:")), "")
    assert "lindos-vm" in rec, "lindos-meta must Recommend lindos-vm"
    assert "lindos-winapps" in rec, "lindos-meta must Recommend lindos-winapps"


# --------------------------------------------------------------------------- #
# Structural YAML check (only when PyYAML is present)
# --------------------------------------------------------------------------- #
def test_ci_yml_parses_as_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_text(CI_YML))
    jobs = doc["jobs"]
    for name in ("lint-test", "pytest-windows", "debs", "kernel", "iso"):
        assert name in jobs, f"missing job {name}"
    assert jobs["iso"]["needs"] == ["debs", "kernel"]
    assert jobs["kernel"]["needs"] == "lint-test"
