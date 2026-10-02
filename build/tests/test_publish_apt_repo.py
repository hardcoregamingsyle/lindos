"""build/publish-apt-repo.sh: syntax, argument parsing, and hermetic end-to-end runs (SPEC-UPDATE.md §36.5,
docs/RELEASING.md).

Never touches a real apt/gpg installation: ``apt-ftparchive``, ``gpg`` and ``gpgv`` are fake PATH stubs
(closer to "no network, no real apt" than shelling out to whatever the test host has), driven against
fixture ``.deb``-shaped files. What is tested is the script's own logic - key modes, the pinned-fingerprint
refusal, ``--release``, the private key ring for the secret, retention, Valid-Until - never real OpenPGP.
Skipped cleanly when bash is unavailable (bare Windows CI, like every other build/tests file).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                      # build/tests -> repo root
SCRIPT = REPO_ROOT / "build" / "publish-apt-repo.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

PIN = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
SECRET_KEY_TEXT = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nTOP-SECRET-SUBKEY-MATERIAL\n-----END PGP PRIVATE KEY BLOCK-----"
PASSPHRASE = "correct-horse-battery-staple"

FAKE_APT_FTPARCHIVE = """#!/bin/bash
[ -z "${FAKE_LOG_DIR:-}" ] || printf '%s\\n' "$*" >> "${FAKE_LOG_DIR}/apt-ftparchive.log"
case "$1" in
    packages)
        for f in *.deb; do
            [ -e "$f" ] || continue
            printf 'Package: fake\\nFilename: %s\\n\\n' "$f"
        done
        ;;
    -o|release)
        echo "Origin: Lindos"
        echo "Codename: Aurora"
        ;;
esac
"""

#: a fake gpg that only models what the script asks of it: whether a secret key exists (after a "generate" or an
#: "import", or FAKE_SECRET_KEYS), its fingerprint (FAKE_FPR), and "--output PATH" writing a recognisable file.
#: Every call is logged to FAKE_GPG_LOG; a --passphrase-file's content is logged on its own line so a test can
#: prove the passphrase never appears in an argument list.
FAKE_GPG = """#!/bin/bash
home="${GNUPGHOME:-/nonexistent-gnupghome}"
if [ -n "${FAKE_GPG_LOG:-}" ]; then
    printf 'ARGS %s | GNUPGHOME=%s\\n' "$*" "${GNUPGHOME:-}" >> "${FAKE_GPG_LOG}"
    prev=""
    for a in "$@"; do
        if [ "${prev}" = "--passphrase-file" ] && [ -f "${a}" ]; then printf 'PASSFILE %s\\n' "$(cat "${a}")" >> "${FAKE_GPG_LOG}"; fi
        prev="${a}"
    done
fi
case " $* " in
    *" --show-keys "*)
        [ -z "${FAKE_KEYRING_UNREADABLE:-}" ] || exit 2
        i=0
        while [ "${i}" -lt "${FAKE_KEYRING_PUBS:-1}" ]; do
            echo "pub:-:255:22:AAAA:1::::::scESC:::::"
            echo "fpr:::::::::${FAKE_KEYRING_FPR:-${FAKE_FPR:-0123456789ABCDEF0123456789ABCDEF01234567}}:"
            i=$((i + 1))
        done
        exit 0
        ;;
    *" --list-secret-keys "*)
        if [ -f "${home}/.fake-key-generated" ] || [ -n "${FAKE_SECRET_KEYS:-}" ]; then
            echo "sec:u:255:22:FAKEKEYID12345678:1700000000:::u:::scSC:::+::ed25519:::0:"
            [ -n "${FAKE_NO_FPR:-}" ] || echo "fpr:::::::::${FAKE_FPR:-0123456789ABCDEF0123456789ABCDEF01234567}:"
        fi
        exit 0
        ;;
    *" --quick-generate-key "*)
        : "${GNUPGHOME:?GNUPGHOME must be set for --quick-generate-key}"
        mkdir -p "${GNUPGHOME}"
        touch "${GNUPGHOME}/.fake-key-generated"
        exit 0
        ;;
    *" --import "*)
        cat > "${home}/imported-key.txt"
        [ -z "${FAKE_IMPORT_FAIL:-}" ] || exit 2
        touch "${home}/.fake-key-generated"
        exit 0
        ;;
esac
out=""
prev=""
for a in "$@"; do
    if [ "${prev}" = "--output" ]; then out="${a}"; fi
    prev="${a}"
done
if [ -n "${out}" ]; then
    printf 'FAKE-GPG-SIGNED-OUTPUT\\nargs: %s\\n' "$*" > "${out}"
fi
exit 0
"""

FAKE_GPGV = """#!/bin/bash
[ -z "${FAKE_LOG_DIR:-}" ] || printf 'gpgv %s\\n' "$*" >> "${FAKE_LOG_DIR}/gpgv.log"
exit "${FAKE_GPGV_RC:-0}"
"""


#: dpkg-deb -f FILE Package Version Architecture: from a sidecar <FAKE_FIELDS_DIR>/<file name>.fields when there is one
#: (a file whose content disagrees with its name), <file name>.unreadable (no readable control data), else from the
#: file name <package>_<version>_<arch>.deb
FAKE_DPKG_DEB = """#!/bin/bash
[ "$1" = "-f" ] || exit 2
f="$2"
side="${FAKE_FIELDS_DIR:-/nonexistent}/$(basename "${f}")"
[ ! -f "${side}.unreadable" ] || exit 2
if [ -f "${side}.fields" ]; then cat "${side}.fields"; exit 0; fi
b="$(basename "${f}" .deb)"
pkg="${b%%_*}"; rest="${b#*_}"; ver="${rest%%_*}"; arch="${rest#*_}"
printf 'Package: %s\\nVersion: %s\\nArchitecture: %s\\n' "${pkg}" "${ver}" "${arch}"
"""


def _write_fake_tool(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_bin(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_tool(fake_bin, "apt-ftparchive", FAKE_APT_FTPARCHIVE)
    _write_fake_tool(fake_bin, "gpg", FAKE_GPG)
    _write_fake_tool(fake_bin, "gpgv", FAKE_GPGV)
    _write_fake_tool(fake_bin, "dpkg-deb", FAKE_DPKG_DEB)
    return fake_bin


def _msys_path(value: str) -> str:
    """Convert a native Windows path ('C:\\Users\\...') to the MSYS form ('/c/Users/...') Git Bash's own runtime
    expects in argv - when this test's *Python* process launches bash.exe directly, MSYS's automatic argv path
    conversion never kicks in, so a raw ``str(tmp_path)`` would look relative to the script. Non-path arguments
    pass through unchanged."""
    if len(value) >= 2 and value[1] == ":" and (value[0].isalpha()):
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


class Env:
    """One hermetic run: fake tools on PATH, a log directory, and helpers to build the environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = _fake_bin(tmp_path)
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.gpg_log = self.logs / "gpg.log"
        self.repo = tmp_path / "repo"
        self.fields = tmp_path / "fields"          # sidecars for the fake dpkg-deb (see FAKE_DPKG_DEB)
        self.fields.mkdir(exist_ok=True)

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        for name in ("LINDOS_APT_SIGNING_KEY", "LINDOS_APT_SIGNING_KEY_PASSPHRASE", "GNUPGHOME", "FAKE_SECRET_KEYS",
                     "FAKE_FPR", "FAKE_NO_FPR", "FAKE_IMPORT_FAIL", "FAKE_GPGV_RC", "FAKE_KEYRING_PUBS",
                     "FAKE_KEYRING_FPR", "FAKE_KEYRING_UNREADABLE"):
            env.pop(name, None)
        env["PATH"] = _msys_path(str(self.bin)) + os.pathsep + env.get("PATH", "")
        env["FAKE_LOG_DIR"] = _msys_path(str(self.logs))
        env["FAKE_GPG_LOG"] = _msys_path(str(self.gpg_log))
        env["FAKE_FIELDS_DIR"] = _msys_path(str(self.fields))
        if extra:
            env.update(extra)
        return env

    def gpg_calls(self) -> List[str]:
        return self.gpg_log.read_text(encoding="utf-8").splitlines() if self.gpg_log.exists() else []

    def ftparchive_calls(self) -> List[str]:
        log = self.logs / "apt-ftparchive.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run(args, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, timeout: float = 300):
    assert BASH is not None
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    argv = [_msys_path(str(a)) for a in args]
    return subprocess.run([BASH, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False, cwd=str(cwd) if cwd else None, env=full_env)


def _publish(e: Env, args: List[str], extra_env: Optional[Dict[str, str]] = None, *, pin: str = "PLACEHOLDER",
             with_sources: bool = True, timeout: float = 300):
    """Run the script with hermetic defaults: no pin unless a test gives one (the committed pin file must never
    leak into these tests) and a fixture sources file."""
    full = list(args)
    if "--expect-fingerprint" not in full:
        full += ["--expect-fingerprint", pin]
    if with_sources and "--sources-file" not in full:
        src = e.tmp / "lindos.sources"
        if not src.exists():
            src.write_text("Enabled: yes\nTypes: deb\nURIs: https://apt.example.test/stable/\nSuites: ./\n", encoding="utf-8")
        full += ["--sources-file", str(src)]
    return _run(full, env=e.env(extra_env), timeout=timeout)


def _make_debs(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(f"fake deb bytes for {name}\n", encoding="utf-8")


def _gone(path: str) -> bool:
    """True when *path* (an MSYS-style path printed by the fake gpg) no longer exists."""
    assert BASH is not None
    return subprocess.run([BASH, "-c", '[ ! -e "$0" ]', path]).returncode == 0


# --- syntax / argument parsing -----------------------------------------------------------------
def test_script_exists_and_executable_shebang() -> None:
    assert SCRIPT.is_file(), SCRIPT
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/bin/bash"


def test_bash_syntax_ok() -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30, check=False)
    assert res.returncode == 0, res.stderr


def test_no_crlf_and_no_bare_sudo() -> None:
    raw = SCRIPT.read_bytes()
    assert b"\r\n" not in raw
    text = SCRIPT.read_text(encoding="utf-8")
    for line in text.splitlines():
        assert not line.strip().startswith("sudo "), line
    assert "set -Eeuo pipefail" in text


def test_help_exits_zero_and_documents_every_flag() -> None:
    res = _run(["--help"])
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    for flag in ("--in", "--previous", "--kernel-in", "--out", "--keep", "--key-id", "--gen-key", "--release",
                 "--expect-fingerprint", "--keyring", "--sources-file", "--valid-until-days",
                 "LINDOS_APT_SIGNING_KEY", "LINDOS_APT_SIGNING_KEY_PASSPHRASE"):
        assert flag in out, flag


def test_unknown_option_is_an_error() -> None:
    res = _run(["--frobnicate"])
    assert res.returncode != 0
    assert "unknown option" in (res.stdout + res.stderr)


def test_unexpected_positional_argument_is_an_error() -> None:
    res = _run(["surprise"])
    assert res.returncode != 0
    assert "unexpected argument" in (res.stdout + res.stderr)


@pytest.mark.parametrize("flag", ["--in", "--previous", "--out", "--keep", "--key-id", "--kernel-in", "--expect-fingerprint",
                                  "--keyring", "--sources-file", "--valid-until-days"])
def test_missing_value_for_flag_fails(flag: str) -> None:
    res = _run([flag])
    assert res.returncode != 0 and "needs an argument" in (res.stdout + res.stderr)


@pytest.mark.parametrize("flag", ["--keep", "--valid-until-days"])
def test_number_flags_reject_text(flag: str, tmp_path: Path) -> None:
    e = Env(tmp_path)
    _make_debs(tmp_path / "debs", "lindos-core_1.0.0_all.deb")
    res = _publish(e, ["--in", str(tmp_path / "debs"), "--out", str(e.repo), "--gen-key", flag, "many"])
    assert res.returncode != 0 and "needs a number" in (res.stdout + res.stderr)


def test_missing_in_directory_is_an_error(tmp_path: Path) -> None:
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(tmp_path / "does-not-exist"), "--out", str(e.repo)])
    assert res.returncode != 0
    assert "not found" in (res.stdout + res.stderr)


def test_no_deb_files_found_is_an_error(tmp_path: Path) -> None:
    empty_in = tmp_path / "debs"
    empty_in.mkdir()
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(empty_in), "--out", str(e.repo)])
    assert res.returncode != 0
    assert "no .deb files found" in (res.stdout + res.stderr)


def test_no_signing_key_and_no_gen_key_is_an_error(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo)], {"GNUPGHOME": str(tmp_path / "no-such-gnupghome")})
    assert res.returncode != 0
    assert "no gpg signing key available" in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists()


# --- hermetic end-to-end run (fake apt-ftparchive + fake gpg) --------------------------------
def test_gen_key_run_produces_a_full_flat_repo(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb", "lindos-meta_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key"])
    assert res.returncode == 0, (res.stdout, res.stderr)

    for name in ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg",
                "lindos-archive-keyring.gpg", "README-CI-KEY.txt", "lindos.sources.example",
                "lindos-core_1.0.0_all.deb", "lindos-meta_1.0.0_all.deb"):
        assert (e.repo / name).is_file(), name
    assert not (e.repo / "lindos.list.example").exists()              # the one-line format is gone

    # signed with the fake gpg -- placeholder content, but must exist and reference gpg's real args
    assert "FAKE-GPG-SIGNED-OUTPUT" in (e.repo / "InRelease").read_text(encoding="utf-8")
    assert "--clearsign" in (e.repo / "InRelease").read_text(encoding="utf-8")
    assert "--detach-sign" in (e.repo / "Release.gpg").read_text(encoding="utf-8")

    # the ephemeral keyring's private material must never be left behind
    assert not (e.repo / ".gnupg-publish").exists()

    readme = (e.repo / "README-CI-KEY.txt").read_text(encoding="utf-8")
    assert "EPHEMERAL" in readme and "NEVER deployed" in readme and "not" in readme.lower()


def test_the_example_source_is_deb822_and_takes_its_address_from_the_conffile(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    src = tmp_path / "custom.sources"
    src.write_text("# c\nEnabled: no\nTypes: deb\nURIs: https://mirror.example.test/lindos/\nSuites: ./\n", encoding="utf-8")
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key", "--sources-file", str(src)])
    assert res.returncode == 0, res.stderr
    example = (e.repo / "lindos.sources.example").read_text(encoding="utf-8")
    assert "URIs: https://mirror.example.test/lindos/" in example
    assert "Types: deb" in example and "Suites: ./" in example
    assert "Signed-By: /usr/share/keyrings/lindos-archive-keyring.gpg" in example
    assert "Enabled" not in example.replace("# Example only", "")


def test_the_default_example_address_is_the_committed_one_and_a_reserved_name(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key"], with_sources=False)
    assert res.returncode == 0, res.stderr
    committed = (REPO_ROOT / "packages/lindos-archive-keyring/root/etc/apt/sources.list.d/lindos.sources").read_text(encoding="utf-8")
    url = re.search(r"^URIs:\s*(\S+)", committed, re.M).group(1)
    assert f"URIs: {url}" in (e.repo / "lindos.sources.example").read_text(encoding="utf-8")


def test_existing_key_id_skips_generation_and_readme(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "signing with existing key" in (res.stdout + res.stderr)
    assert not (e.repo / "README-CI-KEY.txt").exists()
    assert (e.repo / "InRelease").is_file() and (e.repo / "Release.gpg").is_file()


def test_kernel_in_debs_are_included_when_present(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    kernel = tmp_path / "kernel"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    _make_debs(kernel, "linux-image-6.14.0-lindos_1.0.0_amd64.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--kernel-in", str(kernel), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert (e.repo / "linux-image-6.14.0-lindos_1.0.0_amd64.deb").is_file()
    assert "2 .deb file(s)" in (res.stdout + res.stderr)


def test_missing_kernel_in_is_silently_skipped(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--kernel-in", str(tmp_path / "no-kernel-here"), "--out", str(e.repo),
                       "--key-id", "DEADBEEF12345678"])
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "publishing without kernel .debs" in (res.stdout + res.stderr)


# --- several releases + retention ------------------------------------------------------------------
def test_the_release_and_its_previous_releases_are_merged_and_only_the_newest_three_versions_are_kept(tmp_path: Path) -> None:
    now = tmp_path / "now"
    prev1 = tmp_path / "prev1"
    prev2 = tmp_path / "prev2"
    _make_debs(now, "lindos-core_1.0.10_all.deb", "lindos-meta_1.0.10_all.deb")
    _make_debs(prev1, "lindos-core_1.0.9_all.deb", "lindos-meta_1.0.9_all.deb")
    _make_debs(prev2, "lindos-core_1.0.2_all.deb", "lindos-core_1.0.1_all.deb", "lindos-core_1.0.0_all.deb",
               "lindos-meta_1.0.2_all.deb")
    kernel = tmp_path / "kernel"
    _make_debs(kernel, "linux-image-6.14.0-lindos_1.0_amd64.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(now), "--previous", str(prev1), "--previous", str(prev2), "--kernel-in", str(kernel),
                       "--out", str(e.repo), "--key-id", "DEADBEEF12345678", "--keep", "3"])
    assert res.returncode == 0, (res.stdout, res.stderr)
    left = sorted(p.name for p in e.repo.glob("*.deb"))
    # numeric, not textual, ordering: 1.0.10 is newer than 1.0.9; the oldest two of lindos-core are dropped
    assert left == ["lindos-core_1.0.10_all.deb", "lindos-core_1.0.2_all.deb", "lindos-core_1.0.9_all.deb",
                    "lindos-meta_1.0.10_all.deb", "lindos-meta_1.0.2_all.deb", "lindos-meta_1.0.9_all.deb",
                    "linux-image-6.14.0-lindos_1.0_amd64.deb"]
    assert "dropping old version lindos-core_1.0.0_all.deb" in (res.stdout + res.stderr)


def test_keep_zero_keeps_everything(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, *[f"lindos-core_1.0.{i}_all.deb" for i in range(6)])
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678", "--keep", "0"])
    assert res.returncode == 0 and len(list(e.repo.glob("*.deb"))) == 6


def test_the_default_keeps_three_releases(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, *[f"lindos-core_1.0.{i}_all.deb" for i in range(6)])
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode == 0
    assert sorted(p.name for p in e.repo.glob("*.deb")) == [f"lindos-core_1.0.{i}_all.deb" for i in (3, 4, 5)]


def test_the_same_version_for_two_architectures_counts_separately(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "foo_1.0_all.deb", "foo_1.0_amd64.deb", "foo_1.1_all.deb", "foo_1.1_amd64.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678", "--keep", "1"])
    assert res.returncode == 0
    assert sorted(p.name for p in e.repo.glob("*.deb")) == ["foo_1.1_all.deb", "foo_1.1_amd64.deb"]


def test_valid_until_is_off_by_default_and_a_flag_turns_it_on(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    assert _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"]).returncode == 0
    assert not any("Valid-Until" in c for c in e.ftparchive_calls())
    (tmp_path / "second").mkdir()
    e2 = Env(tmp_path / "second")
    res = _publish(e2, ["--in", str(debs), "--out", str(e2.repo), "--key-id", "DEADBEEF12345678", "--valid-until-days", "30"])
    assert res.returncode == 0, res.stderr
    assert any(re.search(r"Release::Valid-Until=\w{3}, \d{2} \w{3} \d{4} \d\d:\d\d:\d\d UTC", c) for c in e2.ftparchive_calls())


def test_release_metadata_is_sent_to_apt_ftparchive(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    assert _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"]).returncode == 0
    joined = "\n".join(e.ftparchive_calls())
    for field in ("Origin=Lindos", "Label=Lindos", "Suite=stable", "Codename=", "Architectures=amd64 all", "Description=Lindos"):
        assert field in joined, field


# --- the signing key: environment secret, private key ring ---------------------------------------
def _env_key_run(tmp_path: Path, *, passphrase: Optional[str] = None, extra: Optional[Dict[str, str]] = None,
                 args: Optional[List[str]] = None):
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    env = {"LINDOS_APT_SIGNING_KEY": SECRET_KEY_TEXT, "FAKE_FPR": PIN}
    if passphrase is not None:
        env["LINDOS_APT_SIGNING_KEY_PASSPHRASE"] = passphrase
    if extra:
        env.update(extra)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo)] + (args or []), env, pin=PIN)
    return e, res


def test_the_secret_key_is_imported_into_a_private_temporary_key_ring_and_signs(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path)
    assert res.returncode == 0, (res.stdout, res.stderr)
    calls = e.gpg_calls()
    imports = [c for c in calls if c.startswith("ARGS") and " --import" in c]
    assert len(imports) == 1
    homes = {c.rsplit("GNUPGHOME=", 1)[1] for c in calls if c.startswith("ARGS")}
    assert len(homes) == 1 and next(iter(homes))                         # every gpg call used the same private home
    home = next(iter(homes))
    assert _gone(home), "the private key ring was not removed"
    sign = [c for c in calls if "--clearsign" in c][0]
    assert f"--local-user {PIN}" in sign                                # signed with the primary fingerprint
    assert (e.repo / "InRelease").is_file() and not (e.repo / "README-CI-KEY.txt").exists()
    assert "signing with env key" in (res.stdout + res.stderr)


def test_the_secret_never_reaches_the_output_the_logs_or_an_argument_list(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, passphrase=PASSPHRASE)
    assert res.returncode == 0, (res.stdout, res.stderr)
    for text in (res.stdout, res.stderr):
        assert "TOP-SECRET" not in text and PASSPHRASE not in text
    for path in e.repo.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert b"TOP-SECRET" not in body and PASSPHRASE.encode() not in body, path
    for line in e.gpg_calls():
        if line.startswith("ARGS"):
            assert PASSPHRASE not in line and "TOP-SECRET" not in line     # not in argv (visible in /proc, in logs)


def test_the_passphrase_goes_through_a_file_in_the_private_key_ring_for_import_and_signing(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, passphrase=PASSPHRASE)
    assert res.returncode == 0, (res.stdout, res.stderr)
    calls = e.gpg_calls()
    importing = [c for c in calls if c.startswith("ARGS") and " --import" in c][0]
    signing = [c for c in calls if c.startswith("ARGS") and "--clearsign" in c][0]
    for call in (importing, signing):
        assert "--pinentry-mode loopback" in call and "--passphrase-file" in call
        assert "--passphrase " not in call and "--passphrase-fd" not in call
    assert f"PASSFILE {PASSPHRASE}" in calls


def test_without_a_passphrase_no_passphrase_handling_is_added(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path)
    assert res.returncode == 0
    joined = "\n".join(e.gpg_calls())
    assert "--passphrase-file" not in joined


def test_a_secret_that_cannot_be_imported_stops_the_publish_before_anything_is_signed(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, extra={"FAKE_IMPORT_FAIL": "1"})
    assert res.returncode != 0 and "could not import LINDOS_APT_SIGNING_KEY" in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists()


def test_the_secret_and_gen_key_cannot_be_combined_and_the_secret_wins_otherwise(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, args=["--gen-key"])
    assert res.returncode == 0                                            # the key from the secret is used ...
    assert not (e.repo / "README-CI-KEY.txt").exists()                    # ... and nothing ephemeral is generated
    assert not any("--quick-generate-key" in c for c in e.gpg_calls())


# --- the pinned fingerprint ------------------------------------------------------------------------
def test_the_signing_key_must_be_the_pinned_one(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, extra={"FAKE_FPR": OTHER})
    assert res.returncode != 0
    assert "REFUSING to publish" in (res.stdout + res.stderr) and OTHER in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists() and not (e.repo / "Release.gpg").exists()   # nothing was signed
    assert not any("--clearsign" in c for c in e.gpg_calls())


def test_a_matching_pin_is_logged_and_publishing_goes_ahead(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path)
    assert res.returncode == 0
    assert f"pinned Lindos archive key ({PIN})" in (res.stdout + res.stderr)


def test_a_pin_needs_a_readable_fingerprint(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path, extra={"FAKE_NO_FPR": "1"})
    assert res.returncode != 0 and not (e.repo / "InRelease").exists()


def test_the_pin_can_be_a_file_and_is_normalised(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    pin_file = tmp_path / "pin.txt"
    pin_file.write_text(" ".join(PIN[i:i + 4] for i in range(0, 40, 4)).lower() + "\n", encoding="utf-8")
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678",
                       "--expect-fingerprint", str(pin_file)], {"FAKE_SECRET_KEYS": "1", "FAKE_FPR": PIN})
    assert res.returncode == 0, res.stderr


def test_a_pin_that_is_not_forty_hex_digits_is_an_error(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key", "--expect-fingerprint", "abc123"])
    assert res.returncode != 0 and "not 40 hex digits" in (res.stdout + res.stderr)


def test_an_unpinned_real_key_is_allowed_for_a_private_test_repo_with_a_warning(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode == 0
    assert "NOT pinned" in (res.stdout + res.stderr)


def test_an_ephemeral_key_is_never_compared_with_the_pin(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key"], pin=PIN)
    assert res.returncode == 0                                            # it can never match: it is for tests only


# --- --release -----------------------------------------------------------------------------------------
def _release_run(tmp_path: Path, *, keyring: Optional[bytes] = b"\x99\x01\x0dreal", extra_env: Optional[Dict[str, str]] = None,
                 args: Optional[List[str]] = None, pin: str = PIN, secret: bool = True):
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    keyring_file = tmp_path / "committed.gpg"
    if keyring is not None:
        keyring_file.write_bytes(keyring)
    env: Dict[str, str] = {"FAKE_FPR": PIN}
    if secret:
        env["LINDOS_APT_SIGNING_KEY"] = SECRET_KEY_TEXT
    if extra_env:
        env.update(extra_env)
    res = _publish(e, ["--release", "--in", str(debs), "--out", str(e.repo), "--keyring", str(keyring_file)] + (args or []),
                   env, pin=pin)
    return e, res, keyring_file


def test_a_release_signs_pin_checks_and_verifies_against_the_committed_keyring(tmp_path: Path) -> None:
    e, res, keyring = _release_run(tmp_path)
    assert res.returncode == 0, (res.stdout, res.stderr)
    gpgv = (e.logs / "gpgv.log").read_text(encoding="utf-8")
    assert "InRelease" in gpgv and "Release.gpg" in gpgv
    assert "committed.gpg" in gpgv                                       # verified against the COMMITTED keyring
    assert (e.repo / "InRelease").is_file() and (e.repo / "lindos-archive-keyring.gpg").is_file()
    assert not (e.repo / "README-CI-KEY.txt").exists()


def test_a_release_needs_the_signing_key(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, secret=False)
    assert res.returncode != 0 and "LINDOS_APT_SIGNING_KEY" in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists()


def test_a_release_never_uses_an_ephemeral_key(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, args=["--gen-key"])
    assert res.returncode != 0 and "cannot be combined" in (res.stdout + res.stderr)
    assert not e.gpg_calls() or not any("--quick-generate-key" in c for c in e.gpg_calls())


def test_a_release_needs_a_pinned_fingerprint(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, pin="PLACEHOLDER")
    assert res.returncode != 0 and "needs a pinned fingerprint" in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists()


def test_a_release_signed_with_the_wrong_key_is_refused(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_FPR": OTHER})
    assert res.returncode != 0 and "REFUSING to publish" in (res.stdout + res.stderr)
    assert not (e.repo / "InRelease").exists()


def test_a_release_refuses_the_placeholder_keyring(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, keyring=b"LINDOS-PLACEHOLDER-KEYRING\n")
    assert res.returncode != 0 and "still the placeholder" in (res.stdout + res.stderr)


def test_a_release_needs_the_committed_keyring(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, keyring=None)
    assert res.returncode != 0 and "committed keyring is missing" in (res.stdout + res.stderr)


def test_a_signature_that_does_not_verify_fails_the_publish(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_GPGV_RC": "1"})
    assert res.returncode != 0 and "does not verify" in (res.stdout + res.stderr)


def test_a_test_build_also_verifies_against_the_keyring_it_just_exported(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    ok = _publish(e, ["--in", str(debs), "--out", str(e.repo), "--gen-key"])
    assert ok.returncode == 0
    assert "lindos-archive-keyring.gpg" in (e.logs / "gpgv.log").read_text(encoding="utf-8")
    (tmp_path / "second").mkdir()
    e2 = Env(tmp_path / "second")
    bad = _publish(e2, ["--in", str(debs), "--out", str(e2.repo), "--gen-key"], {"FAKE_GPGV_RC": "1"})
    assert bad.returncode != 0 and "does not verify" in (bad.stdout + bad.stderr)


def test_the_script_never_writes_a_private_key_into_the_output(tmp_path: Path) -> None:
    e, res = _env_key_run(tmp_path)
    assert res.returncode == 0
    names = sorted(p.name for p in e.repo.rglob("*") if p.is_file())
    assert not any("secret" in n.lower() or n.endswith(".asc") for n in names), names
    assert not (e.repo / ".gnupg-publish").exists()


# --- previous releases are untrusted input ------------------------------------------------------------------
# The regression: every '*.deb' fetched from earlier GitHub Release assets (mutable, unattested, any name) was signed
# with the archive key, and a later --in directory could overwrite a same-named file of the release being published.
def _release_args(e: Env, debs: Path, *more: str) -> List[str]:
    keyring = e.tmp / "committed.gpg"
    keyring.write_bytes(b"\x99\x01\x0dreal")
    return ["--release", "--in", str(debs), "--out", str(e.repo), "--keyring", str(keyring), *more]


def _release_publish(e: Env, debs: Path, *more: str, extra: Optional[Dict[str, str]] = None):
    env = {"LINDOS_APT_SIGNING_KEY": SECRET_KEY_TEXT, "FAKE_FPR": PIN}
    env.update(extra or {})
    return _publish(e, _release_args(e, debs, *more), env, pin=PIN)


def _previous_run(tmp_path: Path, previous: Dict[str, Optional[str]], *, current=("lindos-core_1.0.10_all.deb", "lindos-meta_1.0.10_all.deb"),
                  release: bool = False):
    """Publish *current* with *previous* (file name -> optional 'Package|Version|Architecture' the file really says)."""
    now, prev = tmp_path / "now", tmp_path / "prev"
    _make_debs(now, *current)
    _make_debs(prev, *previous)
    (tmp_path / "fields").mkdir(exist_ok=True)
    for name, says in previous.items():
        if says is not None:
            pkg, ver, arch = says.split("|")
            (tmp_path / "fields" / f"{name}.fields").write_text(f"Package: {pkg}\nVersion: {ver}\nArchitecture: {arch}\n", encoding="utf-8")
    e = Env(tmp_path)
    if release:
        res = _release_publish(e, now, "--previous", str(prev))
    else:
        res = _publish(e, ["--in", str(now), "--previous", str(prev), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    return e, res, now, prev


def _nothing_was_published(e: Env) -> None:
    assert not (e.repo / "InRelease").exists() and not (e.repo / "Release.gpg").exists()
    assert not any("--clearsign" in c for c in e.gpg_calls())


@pytest.mark.parametrize("release", [False, True])
def test_older_releases_of_the_same_packages_are_kept_for_rollback(tmp_path: Path, release: bool) -> None:
    e, res, _now, _prev = _previous_run(tmp_path, {"lindos-core_1.0.9_all.deb": None, "lindos-meta_1.0.9_all.deb": None,
                                                   "lindos-core_1.0.2_all.deb": None}, release=release)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert sorted(p.name for p in e.repo.glob("*.deb")) == [
        "lindos-core_1.0.10_all.deb", "lindos-core_1.0.2_all.deb", "lindos-core_1.0.9_all.deb",
        "lindos-meta_1.0.10_all.deb", "lindos-meta_1.0.9_all.deb"]


def test_a_previous_deb_can_never_replace_a_file_of_the_release_being_published(tmp_path: Path) -> None:
    """The exact attack: a planted lindos-core_<current version>_all.deb in an old release's assets."""
    e, res, now, _prev = _previous_run(tmp_path, {"lindos-core_1.0.10_all.deb": None})
    assert res.returncode != 0 and "the same file name twice" in (res.stdout + res.stderr)
    _nothing_was_published(e)
    assert "fake deb bytes" in (now / "lindos-core_1.0.10_all.deb").read_text(encoding="utf-8")   # the release is untouched


def test_the_same_file_name_from_two_directories_of_the_release_is_refused_not_overwritten(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _make_debs(a, "lindos-core_1.0.0_all.deb")
    _make_debs(b, "lindos-core_1.0.0_all.deb")
    (b / "lindos-core_1.0.0_all.deb").write_text("DIFFERENT BYTES\n", encoding="utf-8")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(a), "--in", str(b), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode != 0 and "the same file name twice" in (res.stdout + res.stderr)
    _nothing_was_published(e)


@pytest.mark.parametrize("previous, message", [
    ({"foo_0.9.0_all.deb": None}, "not a lindos-* package"),                                          # any name
    ({"lindos-evil_0.9.0_all.deb": None}, "not a package of the release being published"),            # a new name
    ({"lindos-core_9.9.9_all.deb": None}, "not older"),                                                # a higher version
    ({"lindos-core_1.0.10_all.deb": None}, "the same file name twice"),                                # the same file
    ({"lindos-core_1.0.9~rc1_all.deb": None}, "not a plain X.Y.Z"),
    ({"lindos-core_1.0_all.deb": None}, "not a plain X.Y.Z"),
])
@pytest.mark.parametrize("release", [False, True])
def test_a_previous_deb_that_is_not_an_older_file_of_the_release_is_refused(tmp_path: Path, previous, message: str, release: bool) -> None:
    e, res, _now, _prev = _previous_run(tmp_path, previous, release=release)
    assert res.returncode != 0 and message in (res.stdout + res.stderr), res.stdout + res.stderr
    _nothing_was_published(e)


def test_a_previous_deb_whose_content_disagrees_with_its_name_is_refused(tmp_path: Path) -> None:
    """A file called lindos-core_0.9.0 that really is lindos-evil 9.9.9 (the name passes every name check)."""
    e, res, _now, _prev = _previous_run(tmp_path, {"lindos-core_0.9.0_all.deb": "lindos-evil|9.9.9|all"})
    assert res.returncode != 0 and "does not match its content" in (res.stdout + res.stderr)
    _nothing_was_published(e)
    (tmp_path / "two").mkdir()
    e2, res2, _n, _p = _previous_run(tmp_path / "two", {"lindos-core_1.0.2_all.deb": "lindos-core|1.0.2|amd64"})
    assert res2.returncode != 0 and "does not match its content" in (res2.stdout + res2.stderr)


@pytest.mark.parametrize("says", ["lindos-core]|1.0.2|all", "lindos-$(id)|1.0.2|all", "Lindos-Core|1.0.2|all", "lindos-core|1.0.2;x|all",
                                  "lindos-core|1.0.2|a l l"])
def test_a_previous_deb_with_a_hostile_control_field_is_refused_before_it_is_used_as_a_key(tmp_path: Path, says: str) -> None:
    pkg, ver, arch = says.split("|")
    name = f"{pkg}_{ver}_{arch}.deb"
    e, res, _now, _prev = _previous_run(tmp_path, {name: None})
    out = res.stdout + res.stderr
    assert res.returncode != 0
    assert any(m in out for m in ("is not a Debian package name", "is not Debian version syntax", "is not an architecture name")), out
    _nothing_was_published(e)


def test_a_previous_deb_without_readable_control_data_is_refused(tmp_path: Path) -> None:
    now, prev = tmp_path / "now", tmp_path / "prev"
    _make_debs(now, "lindos-core_1.0.10_all.deb")
    _make_debs(prev, "lindos-core_1.0.2_all.deb")
    (tmp_path / "fields").mkdir(exist_ok=True)
    (tmp_path / "fields" / "lindos-core_1.0.2_all.deb.unreadable").write_text("", encoding="utf-8")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(now), "--previous", str(prev), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode != 0 and "cannot read its control data" in (res.stdout + res.stderr)
    _nothing_was_published(e)


def test_a_missing_previous_directory_is_an_error(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _publish(e, ["--in", str(debs), "--previous", str(tmp_path / "nope"), "--out", str(e.repo), "--key-id", "DEADBEEF12345678"])
    assert res.returncode != 0 and "--previous directory not found" in (res.stdout + res.stderr)


def test_an_empty_previous_directory_is_the_first_release(tmp_path: Path) -> None:
    e, res, _now, _prev = _previous_run(tmp_path, {})
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert sorted(p.name for p in e.repo.glob("*.deb")) == ["lindos-core_1.0.10_all.deb", "lindos-meta_1.0.10_all.deb"]


def test_the_kernel_debs_keep_their_own_names_and_cannot_collide(tmp_path: Path) -> None:
    now, kernel = tmp_path / "now", tmp_path / "kernel"
    _make_debs(now, "lindos-core_1.0.0_all.deb")
    _make_debs(kernel, "linux-image-6.14.0-lindos_1.0_amd64.deb")
    e = Env(tmp_path)
    res = _release_publish(e, now, "--kernel-in", str(kernel))
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert (e.repo / "linux-image-6.14.0-lindos_1.0_amd64.deb").is_file()


# --- --release: what is in the release itself -------------------------------------------------------------------
def test_a_release_refuses_a_package_that_is_not_a_lindos_package(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb", "stray_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _release_publish(e, debs)
    assert res.returncode != 0 and "not a lindos-* package" in (res.stdout + res.stderr)
    _nothing_was_published(e)


def test_a_release_is_one_version(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.1_all.deb", "lindos-meta_1.0.0_all.deb")
    e = Env(tmp_path)
    res = _release_publish(e, debs)
    assert res.returncode != 0 and "not one version" in (res.stdout + res.stderr)
    _nothing_was_published(e)


def test_a_release_refuses_a_file_named_one_thing_and_containing_another(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    (tmp_path / "fields").mkdir(exist_ok=True)
    (tmp_path / "fields" / "lindos-core_1.0.0_all.deb.fields").write_text("Package: lindos-core\nVersion: 1.0.0\nArchitecture: amd64\n", encoding="utf-8")
    e = Env(tmp_path)
    res = _release_publish(e, debs)
    assert res.returncode != 0 and "does not match its content" in (res.stdout + res.stderr)
    _nothing_was_published(e)


# --- --release: the committed keyring is exactly the pinned key ------------------------------------------------------
def test_a_second_primary_key_in_the_committed_keyring_stops_a_release(tmp_path: Path) -> None:
    """gpgv accepts a signature from ANY key in the keyring and apt trusts every key of it, so a key appended behind the
    pinned one would pass the signature check and be trusted by every installed system."""
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_KEYRING_PUBS": "2"})
    assert res.returncode != 0 and "2 primary keys" in (res.stdout + res.stderr)


def test_a_committed_keyring_whose_key_is_not_the_pin_stops_a_release(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_KEYRING_FPR": OTHER})
    assert res.returncode != 0 and "not the pinned one" in (res.stdout + res.stderr) and OTHER in (res.stdout + res.stderr)


def test_a_committed_keyring_gpg_cannot_read_stops_a_release(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_KEYRING_UNREADABLE": "1"})
    assert res.returncode != 0 and "cannot read the committed keyring" in (res.stdout + res.stderr)


def test_the_keyring_is_inspected_before_the_signature_is_verified_against_it(tmp_path: Path) -> None:
    e, res, _k = _release_run(tmp_path, extra_env={"FAKE_KEYRING_PUBS": "3"})
    assert res.returncode != 0
    assert not (e.logs / "gpgv.log").exists()                                           # gpgv never ran against the bad keyring


# --- a race that came and went with the machine's load -------------------------------------------------------------
def test_gpg_output_is_never_piped_into_an_awk_that_stops_reading_early() -> None:
    """``gpg ... | awk '... exit'`` under ``set -o pipefail`` fails whenever awk quits before gpg has written its last
    line (SIGPIPE): the publish died silently after importing the key, on some runs only. awk has to read to the end."""
    scripts = [SCRIPT, *sorted((REPO_ROOT / "build" / "tools").glob("*.sh")), REPO_ROOT / ".github" / "workflows" / "ci.yml"]
    offenders = []
    for path in scripts:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"gpg[^|\n]*\|\s*awk\b.*\bexit\b", line) and "exit found" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert offenders == []
