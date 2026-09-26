"""build/kernel/build-kernel.sh: syntax + argument-parsing smoke (SPEC-KERNEL §15.5, §19)
and the --base-config ubuntu resolution logic (SPEC-WINDOWS §31.2).

Never triggers a real compile: only `bash -n`, the fast-exit argument paths (`--help`, unknown
option, invalid --base-config) and build/kernel/lib/base-config.sh's pure resolution functions
(sourced standalone, driven with a fake /boot directory and fake apt-cache/apt-get/dpkg-deb
scripts on PATH) are exercised.  Skipped cleanly when bash is unavailable (bare Windows CI).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                       # build/tests -> repo root
SCRIPT = REPO_ROOT / "build" / "kernel" / "build-kernel.sh"
BASE_CONFIG_LIB = REPO_ROOT / "build" / "kernel" / "lib" / "base-config.sh"
SBSIGN_HOOK = (REPO_ROOT / "packages" / "lindos-kernel" / "root" / "etc" / "kernel"
               / "postinst.d" / "zz-lindos-sbsign")
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


# --- --base-config (SPEC-WINDOWS §31.2) -----------------------------------------------------
def test_base_config_flag_documented_in_help() -> None:
    res = _run("--help")
    out = res.stdout + res.stderr
    assert "--base-config" in out
    assert "ubuntu" in out and "defconfig" in out


def test_base_config_default_is_ubuntu() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'BASE_CONFIG="ubuntu"' in text


def test_invalid_base_config_is_usage_error() -> None:
    # validated before the Linux-host guard, so this is cross-platform (fails fast on Windows too)
    res = _run("--base-config", "not-a-real-choice")
    assert res.returncode == 2
    out = res.stdout + res.stderr
    assert "--base-config" in out
    assert "ubuntu" in out


def test_base_config_path_mode_accepts_existing_file(tmp_path: Path) -> None:
    fake_cfg = tmp_path / "my-base.config"
    fake_cfg.write_text("CONFIG_FOO=y\n", encoding="utf-8")
    # still refused for another reason (not a Linux host, or missing deps) but NOT for
    # --base-config validation -- i.e. it must get past argument parsing.
    res = _run("--base-config", str(fake_cfg), "--skip-fetch", "--kdir", str(tmp_path))
    out = res.stdout + res.stderr
    assert "--base-config must be" not in out


def test_sources_base_config_lib() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "lib/base-config.sh" in text
    assert "apply_base_config" in text
    assert "blank_signing_keys" in text
    assert "SYSTEM_TRUSTED_KEYS" in text and "SYSTEM_REVOCATION_KEYS" in text


# --- pahole/dwarves (regression, correct-platform:F1) ---------------------------------------
# lindos.config sets CONFIG_DEBUG_INFO_BTF=y, which upstream Kconfig gates on
# `PAHOLE_VERSION >= 122`; without `pahole` (the `dwarves` package) on PATH, `make olddefconfig`
# silently clears that symbol back to 'n' -- no error, no warning -- so the resulting kernel
# would quietly ship without /sys/kernel/btf/vmlinux and every sched_ext BPF scheduler would
# silently fail its libbpf CO-RE relocations at load time, with zero build-time signal.
def test_check_deps_requires_pahole() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"check_deps\(\) \{(.*?)\n\}\n", text, re.DOTALL)
    assert match, "could not locate check_deps() body in build-kernel.sh"
    body = match.group(1)
    loop = re.search(r"for cmd in ([^;]+); do", body)
    assert loop, "could not locate the checked-command loop inside check_deps()"
    required_cmds = loop.group(1).split()
    assert "pahole" in required_cmds, (
        "check_deps() must verify `pahole` (from the `dwarves` package) is installed, or "
        "CONFIG_DEBUG_INFO_BTF=y silently degrades and sched_ext BPF schedulers break with no "
        "build-time signal (correct-platform:F1)"
    )


def test_ci_kernel_job_installs_dwarves() -> None:
    ci_yml = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_yml.is_file(), ci_yml
    text = ci_yml.read_text(encoding="utf-8")
    # isolate the 'kernel:' job block specifically (up to the next top-level job), not merely
    # anywhere in the file, so this doesn't pass by coincidence from an unrelated job.
    match = re.search(r"\n  kernel:\n.*?(?=\n  [A-Za-z_-]+:\n)", text, re.DOTALL)
    assert match, "could not locate the 'kernel' job in .github/workflows/ci.yml"
    kernel_job = match.group(0)
    assert "dwarves" in kernel_job, (
        "the 'kernel' CI job must install `dwarves` (provides pahole) alongside the other "
        "kernel build dependencies (correct-platform:F1)"
    )


# --- fetch_kernel() completeness marker (regression, correct-platform:F5) -------------------
# fetch_kernel() used to reuse ANY existing out/kernel-src/linux-<ver> directory purely
# because it existed, with no completeness check -- e.g. a tree left behind by an
# interrupted/cancelled `tar -xf` (a killed local run, or a CI cache restored from a run
# cancelled mid-extraction). It must instead detect the missing completeness marker, remove
# the stale tree, and attempt a fresh fetch rather than silently building against a partial one.
def _extract_fetch_kernel_source() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^FETCH_COMPLETE_MARKER=.*\n\nfetch_kernel\(\) \{\n.*?\n\}\n", text,
                       re.DOTALL | re.MULTILINE)
    assert match, "could not extract FETCH_COMPLETE_MARKER/fetch_kernel() from build-kernel.sh"
    return match.group(0)


def test_fetch_kernel_declares_a_completeness_marker() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "FETCH_COMPLETE_MARKER" in text
    # the marker must be written (only) after extraction succeeds, and checked before reuse
    assert re.search(r'touch "\$\{marker\}"', text)
    assert re.search(r'\[ -d "\$\{KDIR\}" \] && \[ -f "\$\{marker\}" \]', text)


def test_fetch_kernel_refuses_to_silently_reuse_incomplete_tree(tmp_path: Path) -> None:
    fetch_kernel_src = _extract_fetch_kernel_source()

    work = tmp_path / "out" / "kernel-src"
    kdir = work / "linux-1.2.3"
    kdir.mkdir(parents=True)
    (kdir / "some-partial-file").write_text("partial\n", encoding="utf-8")  # no marker written

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "curl", 'echo "curl: could not resolve host" >&2; exit 6')

    script = f"""
set -Eeuo pipefail
REPO_ROOT={tmp_path.as_posix()!r}
log()  {{ printf '[build-kernel] %s\\n' "$*"; }}
warn() {{ printf '[build-kernel] warning: %s\\n' "$*" >&2; }}
die()  {{ printf '[build-kernel] fatal: %s\\n' "$*" >&2; exit "${{2:-1}}"; }}
verify_sha() {{ :; }}
{fetch_kernel_src}
fetch_kernel "1.2.3"
"""
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    res = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, check=False, env=env)
    out = res.stdout + res.stderr
    assert "reusing existing source tree" not in out, out
    assert "incomplete source tree" in out, out
    assert res.returncode != 0  # fake curl fails, so the (necessary) re-fetch attempt fails too
    assert not kdir.exists(), "the incomplete tree must be removed, never silently reused"


def test_fetch_kernel_reuses_tree_with_completeness_marker(tmp_path: Path) -> None:
    fetch_kernel_src = _extract_fetch_kernel_source()

    work = tmp_path / "out" / "kernel-src"
    kdir = work / "linux-1.2.3"
    kdir.mkdir(parents=True)
    (kdir / "Makefile").write_text("# fake complete tree\n", encoding="utf-8")
    (kdir / ".lindos-fetch-complete").touch()

    script = f"""
set -Eeuo pipefail
REPO_ROOT={tmp_path.as_posix()!r}
log()  {{ printf '[build-kernel] %s\\n' "$*"; }}
warn() {{ printf '[build-kernel] warning: %s\\n' "$*" >&2; }}
die()  {{ printf '[build-kernel] fatal: %s\\n' "$*" >&2; exit "${{2:-1}}"; }}
verify_sha() {{ :; }}
{fetch_kernel_src}
fetch_kernel "1.2.3"
"""
    res = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, check=False)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "reusing existing source tree" in out
    assert kdir.exists()
    assert (kdir / "Makefile").read_text(encoding="utf-8") == "# fake complete tree\n"


def test_base_config_lib_exists_and_parses() -> None:
    assert BASE_CONFIG_LIB.is_file(), BASE_CONFIG_LIB
    res = subprocess.run([BASH, "-n", str(BASE_CONFIG_LIB)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60, check=False)
    assert res.returncode == 0, res.stderr


def _write_fake_tool(directory: Path, name: str, script_body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{script_body}\n", encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _source_and_call(function_and_args: str, extra_path: Optional[Path] = None,
                     timeout: float = 30) -> subprocess.CompletedProcess:
    assert BASH is not None
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    script = f'set -e\n. "{BASE_CONFIG_LIB.as_posix()}"\n{function_and_args}\n'
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False,
                          env=env)


def test_find_boot_generic_config_picks_newest(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config-6.8.0-31-generic").write_text("x", encoding="utf-8")
    (boot / "config-6.8.0-40-generic").write_text("x", encoding="utf-8")
    (boot / "config-6.8.0-9-generic").write_text("x", encoding="utf-8")
    res = _source_and_call(f'lindos_kernel_find_boot_generic_config "{boot.as_posix()}"')
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().endswith("config-6.8.0-40-generic")


def test_find_boot_generic_config_absent_returns_1(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    res = _source_and_call(f'lindos_kernel_find_boot_generic_config "{boot.as_posix()}"')
    assert res.returncode == 1
    assert res.stdout.strip() == ""


def test_resolve_modules_pkg_uses_apt_cache(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "apt-cache", """
if [ "$1" = "search" ]; then
    echo "linux-modules-6.8.0-31-generic - extra drivers"
    echo "linux-modules-6.8.0-40-generic - extra drivers"
    echo "linux-modules-generic - metapackage, should not match the strict pattern"
fi
""")
    res = _source_and_call("lindos_kernel_resolve_modules_pkg", extra_path=fake_bin)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "linux-modules-6.8.0-40-generic"


def test_resolve_modules_pkg_fails_without_apt_cache(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fakebin_empty"
    fake_bin.mkdir()
    res = _source_and_call("lindos_kernel_resolve_modules_pkg", extra_path=fake_bin)
    assert res.returncode != 0


def test_ubuntu_base_config_prefers_host_boot_dir(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config-6.8.0-31-generic").write_text("CONFIG_FOO=y\n", encoding="utf-8")
    work = tmp_path / "work"
    res = _source_and_call(
        f'lindos_kernel_ubuntu_base_config "{boot.as_posix()}" "{work.as_posix()}"'
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().endswith("config-6.8.0-31-generic")


def test_ubuntu_base_config_downloads_when_no_boot_config(tmp_path: Path) -> None:
    boot = tmp_path / "boot"       # empty: nothing already on the host
    boot.mkdir()
    work = tmp_path / "work"
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "apt-cache", """
if [ "$1" = "search" ]; then
    echo "linux-modules-6.8.0-40-generic - extra drivers"
fi
""")
    _write_fake_tool(fake_bin, "apt-get", """
if [ "$1" = "download" ]; then
    pkg="$2"
    touch "${pkg}_1.0_amd64.deb"
fi
""")
    _write_fake_tool(fake_bin, "dpkg-deb", """
if [ "$1" = "--fsys-tarfile" ]; then
    t="$(mktemp -d)"
    mkdir -p "$t/boot"
    echo "CONFIG_FAKE_UBUNTU=y" > "$t/boot/config-6.8.0-40-generic"
    (cd "$t" && tar -cf - ./boot/config-6.8.0-40-generic)
    rm -rf "$t"
fi
""")
    res = _source_and_call(
        f'lindos_kernel_ubuntu_base_config "{boot.as_posix()}" "{work.as_posix()}"',
        extra_path=fake_bin,
    )
    assert res.returncode == 0, res.stderr
    cfg_path = res.stdout.strip()
    assert cfg_path.endswith("config-6.8.0-40-generic")
    assert "CONFIG_FAKE_UBUNTU=y" in Path(cfg_path).read_text(encoding="utf-8")


def test_ubuntu_base_config_fails_clearly_without_any_tooling(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    work = tmp_path / "work"
    fake_bin = tmp_path / "fakebin_empty"
    fake_bin.mkdir()
    res = _source_and_call(
        f'lindos_kernel_ubuntu_base_config "{boot.as_posix()}" "{work.as_posix()}"',
        extra_path=fake_bin,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""
    assert "apt-cache" in res.stderr or "no linux-modules" in res.stderr


# --- zz-lindos-sbsign postinst.d hook (SPEC-WINDOWS §31.3) ----------------------------------
# Not picked up by tests/run.sh's generic file-discovery glob (no .sh extension, not under
# bin/libexec/sbin/DEBIAN/chroot -- see the cross-owner request in the review notes), so this
# package's own suite checks it directly.
def test_sbsign_hook_exists_and_parses() -> None:
    assert SBSIGN_HOOK.is_file(), SBSIGN_HOOK
    res = subprocess.run([BASH, "-n", str(SBSIGN_HOOK)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30, check=False)
    assert res.returncode == 0, res.stderr


def test_sbsign_hook_shebang_and_set_e() -> None:
    lines = SBSIGN_HOOK.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/sh"
    assert "set -e" in lines


def test_sbsign_hook_never_uses_sudo() -> None:
    text = SBSIGN_HOOK.read_text(encoding="utf-8")
    for line in text.splitlines():
        assert not line.strip().startswith("sudo "), line


def _run_sbsign_hook(version: str, image: Optional[str], env: Dict[str, str],
                     timeout: float = 30) -> subprocess.CompletedProcess:
    assert BASH is not None
    args = [BASH, str(SBSIGN_HOOK), version] + ([image] if image is not None else [])
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False, env=full_env)


def test_sbsign_hook_skips_non_lindos_versions(tmp_path: Path) -> None:
    res = _run_sbsign_hook("6.8.0-31-generic", None, env={"PATH": os.environ.get("PATH", "")})
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_sbsign_hook_reports_missing_image(tmp_path: Path) -> None:
    missing = tmp_path / "vmlinuz-6.14.0-lindos"
    res = _run_sbsign_hook("6.14.0-lindos", str(missing), env={"PATH": os.environ.get("PATH", "")})
    assert res.returncode == 0
    assert "not found" in res.stdout


def test_sbsign_hook_reports_missing_sbsign(tmp_path: Path) -> None:
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env={"PATH": str(empty_bin)})
    assert res.returncode == 0
    assert "sbsign is not installed" in res.stdout
    assert image.read_text(encoding="utf-8") == "fake kernel image\n"  # untouched


def test_sbsign_hook_reports_missing_mok(tmp_path: Path) -> None:
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "sbsign", "echo should-not-run; exit 1")
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env={"PATH": str(fake_bin)})
    assert res.returncode == 0
    assert "no Machine Owner Key found" in res.stdout
    assert "update-secureboot-policy --new-key" in res.stdout


def test_sbsign_hook_signs_when_everything_present(tmp_path: Path) -> None:
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    mok_dir = tmp_path / "mok"
    mok_dir.mkdir()
    (mok_dir / "MOK.priv").write_text("fake priv\n", encoding="utf-8")
    (mok_dir / "MOK.der").write_text("fake der\n", encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "openssl", """
if [ "$1" = "x509" ]; then
    out=""
    prev=""
    for a in "$@"; do
        if [ "$prev" = "-out" ]; then out="$a"; fi
        prev="$a"
    done
    echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$out"
fi
""")
    _write_fake_tool(fake_bin, "sbsign", """
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "--output" ]; then out="$a"; fi
    prev="$a"
done
echo "signed-kernel-bytes" > "$out"
""")
    real_path_bits = [p for p in (shutil.which("sh"), shutil.which("cat")) if p]
    real_dirs = os.pathsep.join(sorted({str(Path(p).parent) for p in real_path_bits}))
    env = {"PATH": str(fake_bin) + os.pathsep + real_dirs,
           "LINDOS_TEST_MOK_DIR": str(mok_dir)}
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env=env)
    assert res.returncode == 0, res.stderr
    assert "signed" in res.stdout
    assert image.read_text(encoding="utf-8") == "signed-kernel-bytes\n"


# --- reapplying GRUB kernel selection after a successful sign (regression, correct-platform:F2)
# Without this, reinstalling the kernel after enrolling a MOK would leave GRUB_DEFAULT pointing
# away from the now-signed Lindos kernel until the user ran `secureboot apply-selection` by
# hand, since only THIS per-kernel-image hook (not lindos-kernel's own package postinst) runs
# on a linux-image-* install/reinstall.
def test_sbsign_hook_reapplies_kernel_selection_after_successful_sign(tmp_path: Path) -> None:
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    mok_dir = tmp_path / "mok"
    mok_dir.mkdir()
    (mok_dir / "MOK.priv").write_text("fake priv\n", encoding="utf-8")
    (mok_dir / "MOK.der").write_text("fake der\n", encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "openssl", """
if [ "$1" = "x509" ]; then
    out=""
    prev=""
    for a in "$@"; do
        if [ "$prev" = "-out" ]; then out="$a"; fi
        prev="$a"
    done
    echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$out"
fi
""")
    _write_fake_tool(fake_bin, "sbsign", """
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "--output" ]; then out="$a"; fi
    prev="$a"
done
echo "signed-kernel-bytes" > "$out"
""")
    calls_log = tmp_path / "calls.log"
    _write_fake_tool(fake_bin, "python3", "exit 0")
    _write_fake_tool(fake_bin, "update-grub", f'echo "update-grub" >> "{calls_log.as_posix()}"')
    fake_cli = tmp_path / "fake-lindos-kernel"
    _write_fake_tool(tmp_path, "fake-lindos-kernel", f'echo "cli:$*" >> "{calls_log.as_posix()}"')
    real_path_bits = [p for p in (shutil.which("sh"), shutil.which("cat")) if p]
    real_dirs = os.pathsep.join(sorted({str(Path(p).parent) for p in real_path_bits}))
    env = {"PATH": str(fake_bin) + os.pathsep + real_dirs,
           "LINDOS_TEST_MOK_DIR": str(mok_dir),
           "LINDOS_TEST_CLI": str(fake_cli)}
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env=env)
    assert res.returncode == 0, res.stderr
    assert "signed" in res.stdout
    assert calls_log.is_file(), "neither the CLI nor update-grub was invoked after a successful sign"
    log = calls_log.read_text(encoding="utf-8")
    assert "cli:secureboot apply-selection" in log
    assert "update-grub" in log


def test_sbsign_hook_skips_reapply_when_sign_fails(tmp_path: Path) -> None:
    # the failure path must NOT recompute the kernel selection or regenerate grub -- nothing
    # about the Secure Boot state changed when signing itself failed.
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    mok_dir = tmp_path / "mok"
    mok_dir.mkdir()
    (mok_dir / "MOK.priv").write_text("fake priv\n", encoding="utf-8")
    (mok_dir / "MOK.der").write_text("fake der\n", encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "openssl", 'echo "-----BEGIN CERTIFICATE-----" > "${@: -1}"')
    _write_fake_tool(fake_bin, "sbsign", 'echo "sbsign: some real signing error" >&2; exit 1')
    calls_log = tmp_path / "calls.log"
    _write_fake_tool(fake_bin, "python3", "exit 0")
    _write_fake_tool(fake_bin, "update-grub", f'echo "update-grub" >> "{calls_log.as_posix()}"')
    fake_cli = tmp_path / "fake-lindos-kernel"
    _write_fake_tool(tmp_path, "fake-lindos-kernel", f'echo "cli:$*" >> "{calls_log.as_posix()}"')
    real_path_bits = [p for p in (shutil.which("sh"), shutil.which("cat")) if p]
    real_dirs = os.pathsep.join(sorted({str(Path(p).parent) for p in real_path_bits}))
    env = {"PATH": str(fake_bin) + os.pathsep + real_dirs,
           "LINDOS_TEST_MOK_DIR": str(mok_dir),
           "LINDOS_TEST_CLI": str(fake_cli)}
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env=env)
    assert res.returncode == 0, res.stderr
    assert "sbsign failed" in res.stdout
    assert not calls_log.exists()


def test_sbsign_hook_reports_sbsign_failure_without_touching_image(tmp_path: Path) -> None:
    image = tmp_path / "vmlinuz-6.14.0-lindos"
    image.write_text("fake kernel image\n", encoding="utf-8")
    mok_dir = tmp_path / "mok"
    mok_dir.mkdir()
    (mok_dir / "MOK.priv").write_text("fake priv\n", encoding="utf-8")
    (mok_dir / "MOK.der").write_text("fake der\n", encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "openssl", 'echo "-----BEGIN CERTIFICATE-----" > "${@: -1}"')
    _write_fake_tool(fake_bin, "sbsign", 'echo "sbsign: some real signing error" >&2; exit 1')
    real_path_bits = [p for p in (shutil.which("sh"), shutil.which("cat")) if p]
    real_dirs = os.pathsep.join(sorted({str(Path(p).parent) for p in real_path_bits}))
    env = {"PATH": str(fake_bin) + os.pathsep + real_dirs,
           "LINDOS_TEST_MOK_DIR": str(mok_dir)}
    res = _run_sbsign_hook("6.14.0-lindos", str(image), env=env)
    assert res.returncode == 0, res.stderr
    assert "sbsign failed" in res.stdout
    assert image.read_text(encoding="utf-8") == "fake kernel image\n"  # untouched
    assert not (tmp_path / "vmlinuz-6.14.0-lindos.lindos-signed.tmp").exists()
