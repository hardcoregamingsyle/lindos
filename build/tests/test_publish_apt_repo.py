"""build/publish-apt-repo.sh: syntax, argument parsing, and a hermetic end-to-end run
(SPEC-UPDATE.md §36.5).

Never touches a real apt/gpg installation: `apt-ftparchive` and `gpg` are both fake PATH stubs
(closer to the spirit of "no network, no real apt" than shelling out to whatever happens to be
installed on the test host), driven against fixture `.deb`-shaped files. Skipped cleanly when
bash is unavailable (bare Windows CI, matching every other build/tests/test_*.py file here).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                      # build/tests -> repo root
SCRIPT = REPO_ROOT / "build" / "publish-apt-repo.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_APT_FTPARCHIVE = """#!/bin/bash
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

#: a minimal fake gpg: '--list-secret-keys' reports a key only after '--quick-generate-key' has
#: "run" (tracked via a marker file under $GNUPGHOME, exactly like a real keyring would persist
#: one), and any '--output PATH' invocation (export / clearsign / detach-sign) just writes a
#: recognisable placeholder to PATH -- this tests publish-apt-repo.sh's own orchestration logic,
#: never real OpenPGP cryptography.
FAKE_GPG = """#!/bin/bash
case " $* " in
    *" --list-secret-keys "*)
        marker="${GNUPGHOME:-/nonexistent-gnupghome}/.fake-key-generated"
        if [ -f "${marker}" ]; then
            echo "sec:u:255:22:FAKEKEYID12345678:1700000000:::u:::scSC:::+::ed25519:::0:"
        fi
        exit 0
        ;;
    *" --quick-generate-key "*)
        : "${GNUPGHOME:?GNUPGHOME must be set for --quick-generate-key}"
        mkdir -p "${GNUPGHOME}"
        touch "${GNUPGHOME}/.fake-key-generated"
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
    return fake_bin


def _msys_path(value: str) -> str:
    """Convert a native Windows path ('C:\\Users\\...') to the MSYS form ('/c/Users/...') Git
    Bash's own runtime expects in argv -- when this test's *Python* process (not an MSYS one)
    launches bash.exe directly, MSYS's automatic argv path conversion never kicks in, so a raw
    ``str(tmp_path)`` would reach the script's own ``case "${DIR}" in /*)`` absolute-path check
    still looking like 'C:\\...' (not starting with '/') and get wrongly treated as relative.
    Non-path arguments (flags, plain strings) pass through unchanged."""
    if len(value) >= 2 and value[1] == ":" and (value[0].isalpha()):
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _run(args, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
         timeout: float = 60) -> subprocess.CompletedProcess:
    assert BASH is not None
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    argv = [_msys_path(str(a)) for a in args]
    return subprocess.run([BASH, str(SCRIPT), *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False,
                          cwd=str(cwd) if cwd else None, env=full_env)


def _make_debs(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(f"fake deb bytes for {name}\n", encoding="utf-8")


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
    for flag in ("--in", "--kernel-in", "--out", "--key-id", "--gen-key"):
        assert flag in out, flag


def test_unknown_option_is_an_error() -> None:
    res = _run(["--frobnicate"])
    assert res.returncode != 0
    assert "unknown option" in (res.stdout + res.stderr)


def test_unexpected_positional_argument_is_an_error() -> None:
    res = _run(["surprise"])
    assert res.returncode != 0
    assert "unexpected argument" in (res.stdout + res.stderr)


def test_missing_value_for_flag_fails() -> None:
    res = _run(["--in"])
    assert res.returncode != 0


def test_missing_in_directory_is_an_error(tmp_path: Path) -> None:
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
    res = _run(["--in", str(tmp_path / "does-not-exist"), "--out", str(tmp_path / "repo")], env=env)
    assert res.returncode != 0
    assert "not found" in (res.stdout + res.stderr)


def test_no_deb_files_found_is_an_error(tmp_path: Path) -> None:
    empty_in = tmp_path / "debs"
    empty_in.mkdir()
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
    res = _run(["--in", str(empty_in), "--out", str(tmp_path / "repo")], env=env)
    assert res.returncode != 0
    assert "no .deb files found" in (res.stdout + res.stderr)


def test_no_signing_key_and_no_gen_key_is_an_error(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
          "GNUPGHOME": str(tmp_path / "no-such-gnupghome")}
    res = _run(["--in", str(debs), "--out", str(tmp_path / "repo")], env=env)
    assert res.returncode != 0
    assert "no gpg signing key available" in (res.stdout + res.stderr)


# --- hermetic end-to-end run (fake apt-ftparchive + fake gpg) --------------------------------
def test_gen_key_run_produces_a_full_flat_repo(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb", "lindos-meta_1.0.0_all.deb")
    repo = tmp_path / "repo"
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
          "LINDOS_APT_REPO_URL": "https://example.test/lindos-apt"}
    res = _run(["--in", str(debs), "--out", str(repo), "--gen-key"], env=env)
    assert res.returncode == 0, (res.stdout, res.stderr)

    for name in ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg",
                "lindos-archive-keyring.gpg", "README-CI-KEY.txt", "lindos.list.example",
                "lindos-core_1.0.0_all.deb", "lindos-meta_1.0.0_all.deb"):
        assert (repo / name).is_file(), name

    # signed with the fake gpg -- placeholder content, but must exist and reference gpg's real args
    assert "FAKE-GPG-SIGNED-OUTPUT" in (repo / "InRelease").read_text(encoding="utf-8")
    assert "--clearsign" in (repo / "InRelease").read_text(encoding="utf-8")
    assert "--detach-sign" in (repo / "Release.gpg").read_text(encoding="utf-8")

    sources_line = (repo / "lindos.list.example").read_text(encoding="utf-8").strip()
    assert sources_line == (
        "deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] https://example.test/lindos-apt ./"
    )

    # the ephemeral keyring's private material must never be left behind
    assert not (repo / ".gnupg-publish").exists()

    readme = (repo / "README-CI-KEY.txt").read_text(encoding="utf-8")
    assert "EPHEMERAL" in readme and "not" in readme.lower()


def test_existing_key_id_skips_generation_and_readme(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    repo = tmp_path / "repo"
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
    res = _run(["--in", str(debs), "--out", str(repo), "--key-id", "DEADBEEF12345678"], env=env)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "signing with an existing key" in (res.stdout + res.stderr)
    assert not (repo / "README-CI-KEY.txt").exists()
    assert (repo / "InRelease").is_file() and (repo / "Release.gpg").is_file()


def test_kernel_in_debs_are_included_when_present(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    kernel = tmp_path / "kernel"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    _make_debs(kernel, "linux-image-6.14.0-lindos_1.0.0_amd64.deb")
    repo = tmp_path / "repo"
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
    res = _run(["--in", str(debs), "--kernel-in", str(kernel), "--out", str(repo),
               "--key-id", "DEADBEEF12345678"], env=env)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert (repo / "linux-image-6.14.0-lindos_1.0.0_amd64.deb").is_file()
    assert "copied 2 .deb file(s)" in (res.stdout + res.stderr)


def test_missing_kernel_in_is_silently_skipped(tmp_path: Path) -> None:
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    repo = tmp_path / "repo"
    fake_bin = _fake_bin(tmp_path)
    env = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
    res = _run(["--in", str(debs), "--kernel-in", str(tmp_path / "no-kernel-here"),
               "--out", str(repo), "--key-id", "DEADBEEF12345678"], env=env)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "publishing without kernel .debs" in (res.stdout + res.stderr)


def test_default_placeholder_repo_url_used_when_unset(tmp_path: Path) -> None:
    """config.env's own placeholder (SPEC-UPDATE.md §35/§36.6) flows straight through when the
    caller never overrides LINDOS_APT_REPO_URL."""
    debs = tmp_path / "debs"
    _make_debs(debs, "lindos-core_1.0.0_all.deb")
    repo = tmp_path / "repo"
    fake_bin = _fake_bin(tmp_path)
    env = dict(os.environ)
    env.pop("LINDOS_APT_REPO_URL", None)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    res = _run(["--in", str(debs), "--out", str(repo), "--key-id", "DEADBEEF12345678"], env=env)
    assert res.returncode == 0, (res.stdout, res.stderr)
    sources_line = (repo / "lindos.list.example").read_text(encoding="utf-8").strip()
    assert "packages.lindos.dev" in sources_line
