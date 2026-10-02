"""The installer flow wired into the image: build/chroot/79-installer-flow.sh (SPEC §2, §8).

The flow (CONTINUATION.md, docs/BUILDING.md "Installer flow"): the live session is only the installer, the
installer does everything heavy in ONE Ubiquity target-config hook, the first boot of the new system only asks
for the account.  packages/lindos-installer ships the scripts; this hook (runs in the squashfs chroot after
78-installer-brand.sh and before 80-cleanup.sh) puts them where Ubiquity looks:

  * /usr/lib/ubiquity/target-config/50lindos-install - Ubiquity runs executable files WITHOUT a '.' in the
    name; git on Windows loses exec bits, so the hook is COPIED with 'install -m 0755' under the dot-less name;
  * /usr/lib/ubiquity/dm-scripts/install/50lindos-noblank - ubiquity-dm runs the executable, dot-less files of
    that directory once the installer's own X server is up: the 'Install Lindos' (only-ubiquity) session has no
    desktop, so nothing else keeps X's screensaver/DPMS from blanking the display after ten minutes;
  * the image's debconf database gets oem-config/enable, ubiquity/success_command, ... (lindos.seed);
  * an audit of what Ubiquity will run is logged.

Nothing here needs Linux, root or a real image: structure and wiring are checked from the sources, and the hook
is run for real with bash against a *fake root* (LINDOS_INSTALLER_ROOT) with fake debconf tools.  What this cannot
show - that Ubiquity really picks the hook up and runs it - needs a QEMU install (see the hook's header).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK = REPO_ROOT / "build" / "chroot" / "79-installer-flow.sh"
HOOK_DIR = HOOK.parent
BUILD_ISO = REPO_ROOT / "build" / "build-iso.sh"
CONFIG_ENV = REPO_ROOT / "build" / "config.env"
PKG = REPO_ROOT / "packages" / "lindos-installer" / "root"
PKG_LIBEXEC = PKG / "usr" / "libexec" / "lindos" / "installer"
PKG_SHARE = PKG / "usr" / "share" / "lindos" / "installer"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")
posix_only = pytest.mark.skipif(os.name == "nt", reason="exec bits are not tracked on Windows (install -m 0755 sets them on the build host)")


def _text(path: Path) -> str:
    assert path.is_file(), path
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- structure
def test_hook_exists_with_house_style() -> None:
    lines = _text(HOOK).splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "set -Eeuo pipefail" in lines
    assert any('/lib.sh"' in ln and ln.lstrip().startswith(".") for ln in lines), "must source lib.sh"
    assert b"\r" not in HOOK.read_bytes()
    text = _text(HOOK)
    assert "hook_begin" in text and "hook_end" in text


def test_hook_runs_after_the_installer_branding_and_before_cleanup() -> None:
    """build-iso.sh runs hooks matching [0-9][0-9]-*.sh in sorted order."""
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    assert HOOK.name in names and re.fullmatch(r"[0-9][0-9]-.*\.sh", HOOK.name)
    i = names.index(HOOK.name)
    before, after = names[:i], names[i + 1:]
    assert before[-1] == "78-installer-brand.sh", "79 runs right after the installer branding: %s" % before
    assert after[0] == "80-cleanup.sh", "and right before the cleanup: %s" % after
    for pkg_hook in ("00-repos.sh", "10-debloat.sh", "20-base.sh", "30-lindos-debs.sh", "40-theme.sh", "50-tune.sh",
                     "60-compat.sh", "70-gaming.sh", "75-vm.sh", "77-mint-sweep.sh"):
        assert pkg_hook in before, "%s must run before the installer flow is wired (nothing may reinstall over it)" % pkg_hook


def test_build_iso_stages_every_hook_by_glob() -> None:
    assert 'cp -f "${BUILD_DIR}/chroot/"*.sh' in _text(BUILD_ISO), "no hook list to maintain"


def _find_shellcheck() -> Optional[str]:
    found = shutil.which("shellcheck")
    if found:
        return found
    import sysconfig
    for scheme in (None, "nt_user", "posix_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        for name in ("shellcheck", "shellcheck.exe"):
            cand = os.path.join(d or "", name)
            if d and os.path.isfile(cand):
                return cand
    return None


@needs_bash
def test_hook_and_installer_scripts_parse_and_are_shellcheck_clean() -> None:
    assert BASH is not None
    targets = [HOOK, *sorted(PKG_LIBEXEC.glob("*.sh"))]
    for path in targets:
        res = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, check=False)
        assert res.returncode == 0, "%s: %s" % (path.name, res.stderr)
    sc = _find_shellcheck()
    if sc is None:
        pytest.skip("shellcheck not installed (tests/run.sh lints these files when it is)")
    res = subprocess.run([sc, "-S", "warning", "-e", "SC1090,SC1091", "-x", *map(str, targets)], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=180, check=False, cwd=str(REPO_ROOT))
    assert res.returncode == 0, res.stdout + res.stderr


def test_hook_refuses_to_edit_a_build_host_and_dies_instead_of_warning() -> None:
    t = _text(HOOK)
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t
    assert t.count("die ") >= 6, "an ISO without the hook silently installs the old way: the hook must fail the build"


def test_the_deployed_name_is_dotless_and_is_a_ubiquity_target_config_name() -> None:
    m = re.search(r'^HOOK_NAME="([^"]+)"$', _text(HOOK), re.M)
    assert m, "HOOK_NAME must be spelled out"
    name = m.group(1)
    assert "." not in name, "Ubiquity skips target-config entries with a '.' in their name"
    assert re.fullmatch(r"[0-9]{2}[A-Za-z0-9_-]+", name)
    assert 'HOOK_DIR="${ROOT}/usr/lib/ubiquity/target-config"' in _text(HOOK)


def test_the_ubiquity_dm_hook_name_and_directory_follow_ubiquity_dms_rules() -> None:
    """bin/ubiquity-dm run_hooks: every entry of dm-scripts/install WITHOUT a '.', exec'd directly (no shell)."""
    t = _text(HOOK)
    m = re.search(r'^DM_NAME="([^"]+)"$', t, re.M)
    assert m, "DM_NAME must be spelled out"
    assert "." not in m.group(1), "ubiquity-dm skips entries with a '.' in their name"
    assert re.fullmatch(r"[0-9]{2}[A-Za-z0-9_-]+", m.group(1))
    assert 'DM_DIR="${ROOT}/usr/lib/ubiquity/dm-scripts/install"' in t
    assert 'DM_HOOK="${DM_DIR}/${DM_NAME}"' in t
    # it is deployed, in the step list, next to the target-config hook
    assert re.search(r"^step_hook\nstep_dm_hook\nstep_seed\n", t, re.M)


def test_the_ubiquity_dm_hook_is_installed_with_mode_0755_and_checked_like_the_other_hook() -> None:
    t = _text(HOOK)
    body = t[t.index("step_dm_hook() {"):t.index("# 4. debconf selections")]
    assert 'install -m 0755 -o root -g root "${src}" "${DM_HOOK}"' in body and 'install -m 0755 "${src}" "${DM_HOOK}"' in body
    assert "install -d -m 0755" in body, "the hook runs as the unprivileged live user: its directories must be searchable"
    assert "ln -s" not in body
    assert "sed -i 's/\\r$//'" in body, "a CR in the shebang makes ubiquity-dm's exec fail silently"
    assert 'sh -n "${DM_HOOK}"' in body and '"#!/bin/sh"' in body
    assert body.count("die ") >= 8, "every way the hook could be skipped or hollow must fail the build"
    for setting in ("'xset'", "'s off'", "'s noblank'", "'-dpms'"):
        assert setting in body, "the deploy assertion must check %s" % setting
    assert "/usr/bin/xset" in body


def test_the_hook_is_installed_with_mode_0755_not_symlinked() -> None:
    t = _text(HOOK)
    assert "install -m 0755" in t, "git on Windows loses exec bits: the build sets them"
    assert "ln -s" not in t, "a symlink would need the exec bit of a file mkdeb.sh only marks for *.sh"
    assert "sed -i 's/\\r$//'" in t, "a CR in the shebang makes Ubiquity's exec fail silently"


def test_hook_touches_only_what_the_flow_needs() -> None:
    t = _text(HOOK)
    for forbidden in ("apt-get", "apt install", "partman", "grub-installer", "mkfs", "dpkg-divert", "sudo ", "curl ", "wget "):
        assert forbidden not in t, "%s does not belong in the wiring hook (the installer downloads, the build does not)" % forbidden
    assert "google-chrome-stable" in t and "microsoft-edge-stable" in t, "licence check: no browser binaries on the image"


# --------------------------------------------------------------------------- the wiring of the rest of the build
def test_the_package_is_installed_by_the_deb_hook_and_kept_off_the_installed_system() -> None:
    env = _text(CONFIG_ENV)
    order = re.search(r'^: "\$\{LINDOS_DEB_ORDER:=([^}]+)\}"', env, re.M).group(1).split()
    assert "lindos-installer" in order and order.index("lindos-installer") < order.index("lindos-meta")
    debs = _text(HOOK_DIR / "30-lindos-debs.sh")
    assert "lindos-installer" in debs
    build = _text(BUILD_ISO)
    assert "LIVE_ONLY_PACKAGES" in build and "filesystem.manifest-remove" in build
    assert re.search(r'^: "\$\{LIVE_ONLY_PACKAGES:=lindos-installer\}"', env, re.M)
    assert "export LIVE_ONLY_PACKAGES" in env or "LIVE_ONLY_PACKAGES REQUIRE_OEM_POOL" in env


def test_build_iso_verifies_oem_config_before_the_iso_is_built() -> None:
    build = _text(BUILD_ISO)
    main = build[build.index("\nmain() {"):]
    order = [main.index(step) for step in ("apply_overlay", "verify_oem_offline", "regen_md5", "build_iso")]
    assert order == sorted(order), "the pool check needs the finished tree and must come before the ISO is written"
    assert 'REQUIRE_OEM_POOL' in build and "verify_oem_pool.py" in build
    assert "OEM_DEBS_DIR" in build and "lindos/oem-debs" in build


def test_the_cleanup_keeps_deleting_the_apt_lists_and_says_why() -> None:
    """The installer hook refreshes the lists in the target; stale lists in the squashfs would only mislead."""
    cleanup = _text(HOOK_DIR / "80-cleanup.sh")
    assert "/var/lib/apt/lists" in cleanup
    assert "installer hook" in cleanup and "79-installer-flow.sh" in cleanup


# --------------------------------------------------------------------------- behaviour on a fake root
FAKE_DEBCONF_SET = """#!/bin/bash
# records what the hook feeds debconf-set-selections
cat >"${FAKE_DEBCONF_LOG}"
exit "${FAKE_DEBCONF_SET_RC:-0}"
"""

FAKE_DEBCONF_COMMUNICATE = """#!/bin/bash
cat >/dev/null
echo "0 ${FAKE_DEBCONF_ANSWER-/usr/libexec/lindos/installer/finalize.sh}"
"""

FAKE_DPKG_QUERY = """#!/bin/bash
# dpkg-query --admindir=... -W -f='${db:Status-Status}' PKG : "installed" only for FAKE_INSTALLED
for a in "$@"; do pkg="$a"; done
for p in ${FAKE_INSTALLED:-}; do
    if [ "$p" = "$pkg" ]; then printf 'installed'; exit 0; fi
done
exit 1
"""


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _put(path: Path, data: bytes, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _fake_image(tmp: Path, *, crlf: bool = False, package: bool = True, ubiquity: bool = True, xset: bool = True) -> Path:
    """A root with what the hook expects: Ubiquity's directory, xset and the lindos-installer package files."""
    root = tmp / "root"
    if xset:
        _put(root / "usr" / "bin" / "xset", b"#!/bin/sh\n", 0o755)
    if ubiquity:
        (root / "usr" / "lib" / "ubiquity" / "target-config").mkdir(parents=True)
        for stock in ("20xconfig", "30accessibility", "45jackd2"):
            _put(root / "usr" / "lib" / "ubiquity" / "target-config" / stock, b"#!/bin/sh\n", 0o755)
    if package:
        for src in sorted(PKG_LIBEXEC.glob("*.sh")):
            data = src.read_bytes()
            if crlf:
                data = data.replace(b"\n", b"\r\n")
            _put(root / "usr" / "libexec" / "lindos" / "installer" / src.name, data, 0o644)   # 0644: what git on Windows gives
        for src in sorted(PKG_SHARE.iterdir()):
            _put(root / "usr" / "share" / "lindos" / "installer" / src.name, src.read_bytes())
    return root


def _shims(tmp: Path) -> Path:
    bin_dir = tmp / "shims"
    bin_dir.mkdir()
    _put(bin_dir / "debconf-set-selections", FAKE_DEBCONF_SET.encode(), 0o755)
    _put(bin_dir / "debconf-communicate", FAKE_DEBCONF_COMMUNICATE.encode(), 0o755)
    _put(bin_dir / "dpkg-query", FAKE_DPKG_QUERY.encode(), 0o755)
    return bin_dir


def _run(tmp: Path, root: Path, **over: str) -> subprocess.CompletedProcess:
    assert BASH is not None
    shims = _shims(tmp) if not (tmp / "shims").exists() else tmp / "shims"
    env = dict(os.environ)
    for key in [k for k in env if k.startswith(("FAKE_", "LINDOS_"))]:
        del env[key]
    env.update({
        "PATH": _posix(shims) + os.pathsep + env.get("PATH", ""),
        "LINDOS_INSTALLER_ROOT": _posix(root),
        "LINDOS_DEBCONF_SET": _posix(shims / "debconf-set-selections"),
        "LINDOS_DEBCONF_COMMUNICATE": _posix(shims / "debconf-communicate"),
        "LINDOS_DPKG_QUERY": _posix(shims / "dpkg-query"),
        "LINDOS_STAGE_DIR": _posix(tmp / "no-such-stage"),
        "LINDOS_CONFIG_ENV": _posix(tmp / "no-such-config.env"),
        "FAKE_DEBCONF_LOG": _posix(tmp / "debconf.log"),
    })
    env.update(over)
    return subprocess.run([BASH, "-Eeuo", "pipefail", _posix(HOOK)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180, check=False, env=env)


def _snapshot(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


DEPLOYED = "usr/lib/ubiquity/target-config/50lindos-install"
DM_DEPLOYED = "usr/lib/ubiquity/dm-scripts/install/50lindos-noblank"


@needs_bash
def test_full_run_deploys_the_hook_and_bakes_the_selections(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root)
    assert res.returncode == 0, res.stderr
    deployed = root / DEPLOYED
    assert deployed.is_file() and "." not in deployed.name
    assert deployed.read_bytes() == (PKG_LIBEXEC / "target-config.sh").read_bytes(), "a plain copy of the package's script"
    assert deployed.read_bytes().startswith(b"#!/bin/bash\n")
    # the debconf database got the whole seed (the real hook feeds debconf-set-selections on stdin)
    fed = (tmp_path / "debconf.log").read_text(encoding="utf-8")
    assert fed == (PKG_SHARE / "lindos.seed").read_text(encoding="utf-8")
    assert "oem-config/enable boolean true" in fed and "ubiquity/success_command string" in fed
    assert "read back ubiquity/success_command" in res.stderr
    # the stock hooks of Ubiquity/casper are untouched, ours sits beside them
    listing = sorted(p.name for p in (root / "usr/lib/ubiquity/target-config").iterdir())
    assert listing == ["20xconfig", "30accessibility", "45jackd2", "50lindos-install"]
    # the audit logs what Ubiquity will find
    assert "target-config directory" in res.stderr and "50lindos-install" in res.stderr
    assert "installer flow wired" in res.stderr
    # ... and what ubiquity-dm will find: the hook that keeps the installer's X server from blanking
    dm = root / DM_DEPLOYED
    assert dm.is_file() and "." not in dm.name
    assert dm.read_bytes() == (PKG_LIBEXEC / "dm-noblank.sh").read_bytes(), "a plain copy of the package's script"
    assert dm.read_bytes().startswith(b"#!/bin/sh\n"), "ubiquity-dm execs it without a shell"
    assert sorted(p.name for p in dm.parent.iterdir()) == ["50lindos-noblank"]
    assert "ubiquity-dm's dm-scripts/install directory" in res.stderr and "50lindos-noblank" in res.stderr
    assert "xset s off, s noblank, -dpms" in res.stderr


@needs_bash
@posix_only
def test_the_deployed_hook_is_executable_even_when_the_package_file_is_not(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    assert not os.access(root / "usr/libexec/lindos/installer/target-config.sh", os.X_OK)
    assert _run(tmp_path, root).returncode == 0
    assert os.access(root / DEPLOYED, os.X_OK), "Ubiquity silently skips non-executable target-config entries"
    assert (root / DEPLOYED).stat().st_mode & 0o777 == 0o755
    # the ubiquity-dm hook: exec'd directly by ubiquity-dm as the unprivileged live user
    assert os.access(root / DM_DEPLOYED, os.X_OK), "ubiquity-dm cannot run a file without the exec bit"
    assert (root / DM_DEPLOYED).stat().st_mode & 0o777 == 0o755
    for d in ("usr/lib/ubiquity/dm-scripts", "usr/lib/ubiquity/dm-scripts/install"):
        assert (root / d).stat().st_mode & 0o777 == 0o755, d
    for name in ("target-config.sh", "lib.sh", "finalize.sh", "dm-noblank.sh"):
        assert os.access(root / "usr/libexec/lindos/installer" / name, os.X_OK), "finalize.sh is Ubiquity's success_command"


@needs_bash
def test_a_crlf_package_is_normalised_because_a_cr_in_the_shebang_kills_the_hook_silently(tmp_path: Path) -> None:
    root = _fake_image(tmp_path, crlf=True)
    res = _run(tmp_path, root)
    assert res.returncode == 0, res.stderr
    assert b"\r" not in (root / DEPLOYED).read_bytes()
    assert (root / DEPLOYED).read_bytes().startswith(b"#!/bin/bash\n")
    assert b"\r" not in (root / DM_DEPLOYED).read_bytes(), "ubiquity-dm's exec would fail on '#!/bin/sh<CR>' without a word"
    assert (root / DM_DEPLOYED).read_bytes().startswith(b"#!/bin/sh\n")


@needs_bash
def test_second_run_changes_nothing(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    assert _run(tmp_path, root).returncode == 0
    first = _snapshot(root)
    assert _run(tmp_path, root).returncode == 0
    assert _snapshot(root) == first


@needs_bash
@pytest.mark.parametrize("missing", ["target-config.sh", "lib.sh", "finalize.sh", "dm-noblank.sh"])
def test_a_missing_package_script_fails_the_build(tmp_path: Path, missing: str) -> None:
    root = _fake_image(tmp_path)
    (root / "usr/libexec/lindos/installer" / missing).unlink()
    res = _run(tmp_path, root)
    assert res.returncode != 0 and missing in res.stderr and "lindos-installer" in res.stderr
    assert not (root / DEPLOYED).exists(), "no half-wired image"
    assert not (root / DM_DEPLOYED).exists()


@needs_bash
@pytest.mark.parametrize("missing", ["lindos-installer.templates", "lindos.seed", "extras.json"])
def test_a_missing_package_data_file_fails_the_build(tmp_path: Path, missing: str) -> None:
    root = _fake_image(tmp_path)
    (root / "usr/share/lindos/installer" / missing).unlink()
    res = _run(tmp_path, root)
    assert res.returncode != 0 and missing in res.stderr


@needs_bash
def test_an_image_without_ubiquity_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path, ubiquity=False)
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "no Ubiquity installer" in res.stderr


@needs_bash
def test_a_syntax_error_in_the_hook_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = root / "usr/libexec/lindos/installer/target-config.sh"
    script.write_bytes(script.read_bytes() + b"\nif then fi (\n")
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "syntax error" in res.stderr


@needs_bash
def test_a_hook_with_errexit_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = root / "usr/libexec/lindos/installer/target-config.sh"
    script.write_bytes(script.read_bytes().replace(b"umask 022\n", b"set -e\numask 022\n", 1))
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "set -e" in res.stderr


@needs_bash
def test_a_hook_without_a_bash_shebang_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = root / "usr/libexec/lindos/installer/target-config.sh"
    script.write_bytes(script.read_bytes().replace(b"#!/bin/bash", b"#!/bin/sh", 1))
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "#!/bin/bash" in res.stderr


def _dm_script(root: Path) -> Path:
    return root / "usr/libexec/lindos/installer/dm-noblank.sh"


@needs_bash
def test_an_image_without_xset_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path, xset=False)
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "xset" in res.stderr and "x11-xserver-utils" in res.stderr


@needs_bash
def test_a_syntax_error_in_the_ubiquity_dm_hook_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = _dm_script(root)
    script.write_bytes(script.read_bytes() + b"\nif then fi (\n")
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "ubiquity-dm hook has a syntax error" in res.stderr


@needs_bash
def test_an_ubiquity_dm_hook_with_errexit_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = _dm_script(root)
    script.write_bytes(script.read_bytes().replace(b"\nXSET=", b"\nset -e\nXSET=", 1))
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "ubiquity-dm hook uses 'set -e'" in res.stderr


@needs_bash
def test_an_ubiquity_dm_hook_without_a_sh_shebang_fails_the_build(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    script = _dm_script(root)
    script.write_bytes(script.read_bytes().replace(b"#!/bin/sh", b"#!/bin/bash", 1))
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "#!/bin/sh" in res.stderr


@needs_bash
@pytest.mark.parametrize("setting", [b"s off", b"s noblank", b"-dpms"])
def test_an_ubiquity_dm_hook_that_does_not_switch_the_blanking_off_fails_the_build(tmp_path: Path, setting: bytes) -> None:
    """Deployed but hollow would look fine in a build log and blank after ten minutes on every install."""
    root = _fake_image(tmp_path)
    script = _dm_script(root)
    # only the code changes (the header comment still names the setting): the assertion must ignore comments
    text = script.read_bytes()
    code_line = b'for setting in "s off" "s noblank" "-dpms"; do'
    assert code_line in text
    script.write_bytes(text.replace(code_line, code_line.replace(b'"' + setting + b'"', b'"s nothing"')))
    res = _run(tmp_path, root)
    assert res.returncode != 0 and "does not use xset '%s'" % setting.decode() in res.stderr, res.stderr


@needs_bash
@pytest.mark.parametrize("browser", ["google-chrome-stable", "microsoft-edge-stable"])
def test_a_browser_on_the_image_fails_the_build_because_its_licence_forbids_shipping_it(tmp_path: Path, browser: str) -> None:
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root, FAKE_INSTALLED=browser)
    assert res.returncode != 0 and browser in res.stderr and "licence" in res.stderr
    assert not (root / DEPLOYED).exists()


@needs_bash
def test_without_debconf_tools_the_hook_warns_and_the_build_goes_on(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root, LINDOS_DEBCONF_SET="no-such-debconf-set-selections")
    assert res.returncode == 0 and "NOT baked" in res.stderr
    assert (root / DEPLOYED).is_file(), "the boot entries carry the essential answers, so the hook itself is still deployed"


@needs_bash
def test_a_failing_debconf_set_selections_warns_and_the_build_goes_on(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root, FAKE_DEBCONF_SET_RC="1")
    assert res.returncode == 0 and "reported a problem" in res.stderr


@needs_bash
def test_a_selection_that_does_not_read_back_is_reported(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root, FAKE_DEBCONF_ANSWER="something else")
    assert res.returncode == 0 and "did not read back" in res.stderr


@needs_bash
def test_oem_config_in_the_live_image_is_flagged(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    _put(root / "usr/lib/oem-config/oem-config.service", b"[Service]\n")
    res = _run(tmp_path, root)
    assert res.returncode == 0 and "oem-config is already installed in the live image" in res.stderr


@needs_bash
def test_the_hook_refuses_to_run_outside_the_build_chroot(tmp_path: Path) -> None:
    """Without the test seam and outside a chroot it must die before it writes anything."""
    assert BASH is not None
    # the hook's lib.sh resets PATH, so the "host" is played with exported shell functions: '/' has inode 2
    # (a real host) and ischroot says "not a chroot"
    script = "stat() { echo 2; }; ischroot() { return 1; }; export -f stat ischroot; exec bash -Eeuo pipefail '%s'" % _posix(HOOK)
    env = dict(os.environ)
    for key in [k for k in env if k.startswith(("FAKE_", "LINDOS_"))]:
        del env[key]
    env.update({"LINDOS_STAGE_DIR": _posix(tmp_path / "no-such-stage"),
                "LINDOS_CONFIG_ENV": _posix(tmp_path / "no-such-config.env")})
    res = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120, check=False, env=env)
    assert res.returncode != 0 and "refusing to edit the host" in res.stderr, res.stderr


# --------------------------------------------------------------------------- casper/filesystem.manifest-remove
def _casper_metadata_fn() -> str:
    text = _text(BUILD_ISO)
    start = text.index("\ncasper_metadata() {") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_casper_metadata(tmp: Path, manifest_remove: Optional[bytes], live_only: str = "lindos-installer") -> subprocess.CompletedProcess:
    assert BASH is not None
    iso, sq = tmp / "iso", tmp / "sq"
    (iso / "casper").mkdir(parents=True)
    sq.mkdir()
    if manifest_remove is not None:
        (iso / "casper" / "filesystem.manifest-remove").write_bytes(manifest_remove)
    script = (
        "set -Eeuo pipefail\n"
        "timer_start() { :; }; timer_end() { :; }; log() { echo \"log: $*\" >&2; }; warn() { echo \"warn: $*\" >&2; }\n"
        "human_size() { echo \"$1 bytes\"; }; sync_casper_kernel() { :; }\n"
        "chroot() { printf 'ubiquity\t24.04.3+mint18\n'; }\n"
        "ISO_DIR=%s; SQ=%s; LIVE_DIR=casper; LIVE_ONLY_PACKAGES=%r\n" % (_posix(iso), _posix(sq), live_only)
        + _casper_metadata_fn() + "casper_metadata\n")
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, check=False)


def _manifest_remove(tmp: Path) -> bytes:
    return (tmp / "iso" / "casper" / "filesystem.manifest-remove").read_bytes()


@needs_bash
def test_the_installer_package_is_added_to_the_removal_list_of_the_new_system(tmp_path: Path) -> None:
    res = _run_casper_metadata(tmp_path, b"ubiquity\nubiquity-casper\ngparted\n")
    assert res.returncode == 0, res.stderr
    assert _manifest_remove(tmp_path) == b"ubiquity\nubiquity-casper\ngparted\nlindos-installer\n", "the base's own list is kept"
    assert "lindos-installer added to filesystem.manifest-remove" in res.stderr


@needs_bash
def test_the_removal_list_gets_a_missing_final_newline_and_no_duplicates(tmp_path: Path) -> None:
    res = _run_casper_metadata(tmp_path, b"ubiquity\ngparted")            # no newline at the end
    assert res.returncode == 0, res.stderr
    assert _manifest_remove(tmp_path) == b"ubiquity\ngparted\nlindos-installer\n"
    again = _run_casper_metadata(tmp_path / "again", b"ubiquity\nlindos-installer\n")
    assert again.returncode == 0 and _manifest_remove(tmp_path / "again") == b"ubiquity\nlindos-installer\n", "never listed twice"


@needs_bash
def test_a_base_without_a_removal_list_is_warned_about_not_invented(tmp_path: Path) -> None:
    res = _run_casper_metadata(tmp_path, None)
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / "iso" / "casper" / "filesystem.manifest-remove").exists()
    assert "warn: base ISO has no filesystem.manifest-remove" in res.stderr


# --------------------------------------------------------------------------- the look of the installer session
SKIN_SRC = REPO_ROOT / "build" / "installer" / "themes" / "Lindos-Setup" / "gtk-3.0" / "gtk.css"
DROPIN = "etc/systemd/system/ubiquity.service.d/10-lindos.conf"
SKIN_DST = "usr/share/themes/Lindos-Setup/gtk-3.0/gtk.css"
IMPORT_LINE = '@import url("../../Lindos-Dark/gtk-3.0/gtk.css");'
ADWAITA_IMPORT = '@import url("resource:///org/gtk/libgtk/theme/Adwaita/gtk-contained-dark.css");'


def _with_skin(root: Path) -> None:
    _put(root / SKIN_DST, SKIN_SRC.read_bytes())


def _stage(tmp: Path) -> Path:
    stage = tmp / "stage"
    _put(stage / "themes" / "Lindos-Setup" / "gtk-3.0" / "gtk.css", SKIN_SRC.read_bytes())
    return stage


@needs_bash
def test_the_installer_session_gets_the_skin_through_a_drop_in_of_ubiquity_service(tmp_path: Path) -> None:
    """Only the launcher's Exec applied Lindos-Setup: the 'Install Lindos' entries (only-ubiquity) start ubiquity-dm from
    ubiquity.service, which never saw it.  ubiquity-dm passes its environment on to X, the window manager and the GTK
    program, so a variable of the unit is enough."""
    root = _fake_image(tmp_path)
    _with_skin(root)
    res = _run(tmp_path, root)
    assert res.returncode == 0, res.stderr
    text = (root / DROPIN).read_text(encoding="utf-8")
    assert text.splitlines()[-2:] == ["[Service]", "Environment=GTK_THEME=Lindos-Setup"], text
    assert "\r" not in text and text.startswith("# Written by build/chroot/79-installer-flow.sh")
    assert "deployed /%s" % DROPIN in res.stderr
    assert sorted(p.name for p in (root / "etc/systemd/system/ubiquity.service.d").iterdir()) == ["10-lindos.conf"]


@needs_bash
@posix_only
def test_the_drop_in_and_its_directory_are_world_readable(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    _with_skin(root)
    assert _run(tmp_path, root).returncode == 0
    assert (root / DROPIN).stat().st_mode & 0o777 == 0o644
    assert (root / "etc/systemd/system/ubiquity.service.d").stat().st_mode & 0o777 == 0o755


@needs_bash
def test_a_second_run_with_the_skin_changes_nothing(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    _with_skin(root)
    assert _run(tmp_path, root).returncode == 0
    first = _snapshot(root)
    assert _run(tmp_path, root).returncode == 0
    assert _snapshot(root) == first


@needs_bash
def test_without_a_skin_there_is_no_drop_in_and_the_build_goes_on(tmp_path: Path) -> None:
    """Cosmetic: the installer keeps the default look, the build says so and does not fail."""
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root)
    assert res.returncode == 0, res.stderr
    assert not (root / DROPIN).exists() and not (root / "etc/systemd/system/ubiquity.service.d").exists()
    assert "no Lindos-Setup skin" in res.stderr and "keep the default GTK theme" in res.stderr
    # a stale drop-in of an earlier build is removed: it would name a theme that is not there
    _put(root / DROPIN, b"[Service]\nEnvironment=GTK_THEME=Lindos-Setup\n")
    assert _run(tmp_path, root).returncode == 0
    assert not (root / DROPIN).exists()


@needs_bash
def test_a_skin_that_78_did_not_install_is_installed_from_the_staged_sources(tmp_path: Path) -> None:
    root = _fake_image(tmp_path)
    _put(root / "usr/share/themes/Lindos-Dark/gtk-3.0/gtk.css", b"/* base */\n")
    res = _run(tmp_path, root, LINDOS_INSTALLER_SRC=(_stage(tmp_path)).as_posix())
    assert res.returncode == 0, res.stderr
    assert (root / SKIN_DST).read_bytes() == SKIN_SRC.read_bytes(), "a plain copy: 78's own deployment is byte for byte the same"
    assert "installed the Lindos-Setup GTK skin" in res.stderr and (root / DROPIN).is_file()


@needs_bash
def test_without_lindos_dark_the_skin_is_rebased_on_gtks_dark_adwaita_so_the_installer_stays_dark(tmp_path: Path) -> None:
    """78 installs no skin when Lindos-Dark is missing (its @import would find nothing): then the installer would keep
    Ubiquity's light default although the drop-in asks for the skin.  Here the skin is installed on GTK's built-in dark."""
    root = _fake_image(tmp_path)
    res = _run(tmp_path, root, LINDOS_INSTALLER_SRC=(_stage(tmp_path)).as_posix())
    assert res.returncode == 0, res.stderr
    css = (root / SKIN_DST).read_text(encoding="utf-8")
    assert ADWAITA_IMPORT in css.splitlines() and IMPORT_LINE not in css.splitlines()
    assert css.replace(ADWAITA_IMPORT, IMPORT_LINE) == SKIN_SRC.read_text(encoding="utf-8"), "only the @import line differs"
    assert "no Lindos-Dark GTK theme" in res.stderr and "dark Adwaita" in res.stderr
    assert (root / DROPIN).is_file()


@needs_bash
def test_the_skin_source_carries_the_import_line_the_build_rebases() -> None:
    css = SKIN_SRC.read_text(encoding="utf-8")
    assert IMPORT_LINE in css.splitlines()
    first_rule = next(ln for ln in css.splitlines() if ln.startswith("@") or ln.endswith("{"))
    assert first_rule == IMPORT_LINE, "@import must come first in a GTK style sheet"


def test_the_skin_is_a_dark_fluent_palette_that_changes_no_geometry() -> None:
    css = SKIN_SRC.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert code.count("{") == code.count("}") and code.count("(") == code.count(")")
    assert b"\r" not in SKIN_SRC.read_bytes()
    for colour in ("#202020", "#2B2B2B", "#60CDFF"):
        assert colour.lower() in code.lower(), colour
    # the pages, the lists, the text fields, the buttons: nothing is left to Ubiquity's light default
    for selector in ("window,", ".view,", "entry,", "button {", "check,", "button.ubiquity-next", "progressbar progress", ".ubiquity-menubar"):
        assert selector in code, selector
    # window frames: metacity (ubiquity-dm's window manager here) reads the wm_* colours of the GTK theme
    for name in ("wm_title", "wm_bg_a", "wm_bg_b", "theme_bg_color", "theme_fg_color", "theme_selected_bg_color"):
        assert "@define-color %s " % name in code, name
    # colours and typography only: a size or a padding could change Ubiquity's layout
    for prop in ("padding", "margin", "min-width", "min-height", "width:", "height:", "font-size", "border-width"):
        assert prop not in code.replace("border-radius", ""), prop
    # a label takes the colour of what it sits in (black on the accent Continue button): no rule of its own
    assert not re.search(r"(^|\n)label\s*[,{]", code)
    # every rule that paints a dark background also says what colour the text is
    for block in re.findall(r"([^{}]+)\{([^{}]*)\}", code):
        selector, body = block
        if "background-color" in body and "color:" not in body.replace("background-color:", ""):
            assert any(w in selector for w in ("trough", "progress", "scrollbar", "separator", "infobar.", ":hover", "button:active",
                                               "button:disabled", "scrollbar slider")), "no text colour beside a background: %s" % selector.strip()


@needs_bash
def test_a_skin_drop_in_that_could_not_be_written_fails_the_build(tmp_path: Path) -> None:
    """The drop-in is checked after it is written, like the hook: a silently missing one means the light default."""
    root = _fake_image(tmp_path)
    _with_skin(root)
    script = HOOK.read_text(encoding="utf-8")
    assert 'grep -qx "Environment=GTK_THEME=${SKIN}" "${SKIN_DROPIN}" || die' in script
