"""build/tools/fetch-previous-releases.sh: the older releases kept for rollback are trusted only when they verify.

The regression it closes: the release job used to download every ``*.deb`` attached to earlier GitHub Releases (mutable
assets that anyone with write access can add or replace, under any name) and sign all of them with the archive key.
The script now trusts a file only when the signed repository metadata published with that release vouches for its
exact bytes. Hermetic: ``gh`` and ``gpgv`` are fake PATH stubs (a signature is "good" when the file says so), the
hashes are real (``sha256sum``); the script is run as ``bash <script>``. Skipped where bash is unavailable.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = REPO / "build" / "tools" / "fetch-previous-releases.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

GOOD, BAD = "GOODSIG\n", "BADSIG\n"

FAKE_GH = """#!/bin/bash
if [ "$1 $2" = "release list" ]; then
    [ -z "${FAKE_GH_FAIL_LIST:-}" ] || exit 1
    cat "${FAKE_GH_TAGS}"
    exit 0
fi
if [ "$1 $2" = "release download" ]; then
    tag="$3"; shift 3
    pats=(); dir=""
    while [ $# -gt 0 ]; do
        case "$1" in --pattern) pats+=("$2"); shift 2 ;; --dir) dir="$2"; shift 2 ;; *) shift ;; esac
    done
    src="${FAKE_GH_ASSETS}/${tag}"
    [ -d "${src}" ] || exit 1
    mkdir -p "${dir}"
    for f in "${src}"/*; do
        [ -e "${f}" ] || continue
        b="$(basename "${f}")"
        for p in "${pats[@]}"; do
            case "${b}" in ${p}) cp "${f}" "${dir}/${b}"; break ;; esac
        done
    done
    exit 0
fi
exit 2
"""

FAKE_GPGV = """#!/bin/bash
printf 'gpgv %s\n' "$*" >> "${FAKE_GPGV_LOG}"
args=("$@"); n=${#args[@]}; sig="${args[$((n - 2))]}"      # gpgv --keyring KEYRING SIGNATURE SIGNED-FILE
grep -q GOODSIG "${sig}"
"""


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _tool(directory: Path, name: str, content: str) -> None:
    path = directory / name
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class World:
    """Fake GitHub: the release list and the assets of every release, plus the fake tools on PATH."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        _tool(self.bin, "gh", FAKE_GH)
        _tool(self.bin, "gpgv", FAKE_GPGV)
        self.assets = tmp / "assets"
        self.assets.mkdir()
        self.tags: List[str] = []
        self.keyring = tmp / "committed.gpg"
        self.keyring.write_bytes(b"\x99\x01key")
        self.out = tmp / "previous"
        self.gpgv_log = tmp / "gpgv.log"

    def release(self, tag: str, debs: Dict[str, bytes], *, signature: str = GOOD, listed: Optional[List[str]] = None,
                metadata: bool = True, packages_tamper: bool = False, extra: Optional[Dict[str, bytes]] = None,
                also_list: Optional[Dict[str, bytes]] = None) -> Path:
        """One release as published by release-repo: its debs plus Release, Release.gpg and Packages. *listed* limits
        which debs the Packages knows (default: all); *extra* are files added to the assets afterwards."""
        d = self.assets / tag
        d.mkdir(parents=True)
        for name, data in debs.items():
            (d / name).write_bytes(data)
        if metadata:
            stanzas = "".join(
                f"Package: {name.split('_')[0]}\nVersion: x\nFilename: ./{name}\nSize: {len(data)}\nMD5sum: 0\nSHA256: {_sha(data)}\n\n"
                for name, data in {**{n: d for n, d in debs.items() if listed is None or n in listed}, **(also_list or {})}.items())
            packages = stanzas.encode()
            (d / "Packages").write_bytes(packages + (b"# tampered after signing\n" if packages_tamper else b""))
            release = (f"Origin: Lindos\nSuite: stable\nMD5Sum:\n {'0' * 32} 1 Packages\nSHA1:\n {'0' * 40} 1 Packages\n"
                       f"SHA256:\n {_sha(packages)} {len(packages)} Packages\n {'0' * 64} 1 Packages.gz\nSHA512:\n {'0' * 128} 1 Packages\n")
            (d / "Release").write_text(release, encoding="utf-8")
            (d / "Release.gpg").write_text(signature, encoding="utf-8")
        for name, data in (extra or {}).items():
            (d / name).write_bytes(data)
        if tag not in self.tags:
            self.tags.append(tag)
        return d

    def run(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        assert BASH is not None
        tags_file = self.tmp / "tags.txt"
        tags_file.write_text("".join(f"{t}\n" for t in self.tags), encoding="utf-8", newline="\n")
        env = dict(os.environ)
        env["PATH"] = _msys(str(self.bin)) + os.pathsep + env.get("PATH", "")
        env.update({"FAKE_GH_TAGS": _msys(str(tags_file)), "FAKE_GH_ASSETS": _msys(str(self.assets)),
                    "FAKE_GPGV_LOG": _msys(str(self.gpgv_log))})
        env.pop("FAKE_GH_FAIL_LIST", None)
        env.update(extra_env or {})
        argv = list(args) if args else ["--tag", "v1.0.3", "--keyring", str(self.keyring), "--out", str(self.out)]
        return subprocess.run([BASH, str(SCRIPT), *[_msys(a) if os.sep in a else a for a in argv]], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env, timeout=120)

    def kept(self) -> List[str]:
        return sorted(p.name for p in self.out.glob("*")) if self.out.exists() else []


def _debs(version: str, *, marker: str = "") -> Dict[str, bytes]:
    return {f"lindos-core_{version}_all.deb": f"core {version} {marker}".encode(),
            f"lindos-meta_{version}_all.deb": f"meta {version} {marker}".encode()}


# --- the script itself ---------------------------------------------------------------------------------------------
def test_script_shape() -> None:
    raw = SCRIPT.read_bytes()
    text = raw.decode("utf-8")
    assert text.startswith("#!/bin/bash\n") and b"\r" not in raw and "set -Eeuo pipefail" in text
    assert not any(line.strip().startswith("sudo ") for line in text.splitlines())
    assert BASH is not None
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_help_documents_the_options_and_the_three_checks() -> None:
    proc = subprocess.run([BASH, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    for word in ("--tag", "--keyring", "--out", "--count", "--limit", "Release.gpg", "SHA256"):
        assert word in proc.stderr, word


@pytest.mark.parametrize("args", [[], ["--tag", "latest", "--keyring", "k"], ["--tag", "v1.0.0"], ["--tag", "v1.0.0", "--keyring", "/no/such/keyring"],
                                  ["--tag"], ["--frobnicate"]])
def test_usage_errors_stop_before_anything_is_fetched(tmp_path: Path, args: List[str]) -> None:
    w = World(tmp_path)
    w.release("v1.0.0", _debs("1.0.0"))
    proc = w.run(*args) if args else w.run("--tag", "")
    assert proc.returncode == 1
    assert w.kept() == []


def test_count_and_limit_must_be_numbers(tmp_path: Path) -> None:
    w = World(tmp_path)
    proc = w.run("--tag", "v1.0.3", "--keyring", str(w.keyring), "--out", str(w.out), "--count", "two")
    assert proc.returncode == 1 and "numbers" in proc.stderr


# --- what is kept --------------------------------------------------------------------------------------------------
def test_two_verified_releases_are_kept_and_the_signature_is_checked_against_the_committed_keyring(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"))
    w.release("v1.0.1", _debs("1.0.1"))
    w.release("v1.0.0", _debs("1.0.0"))                     # a third one: --count 2 stops before it
    proc = w.run()
    assert proc.returncode == 0, proc.stderr
    assert w.kept() == sorted(list(_debs("1.0.2")) + list(_debs("1.0.1")))
    assert (w.out / "lindos-core_1.0.2_all.deb").read_bytes() == b"core 1.0.2 "
    log = w.gpgv_log.read_text(encoding="utf-8")
    assert "committed.gpg" in log and "Release.gpg" in log and log.count("gpgv ") == 2


def test_the_first_release_has_nothing_to_keep_and_that_is_fine(tmp_path: Path) -> None:
    w = World(tmp_path)
    proc = w.run()
    assert proc.returncode == 0 and "no earlier release found" in proc.stdout
    assert w.out.is_dir() and w.kept() == []                # the directory exists: publish-apt-repo.sh --previous needs it


def test_the_current_release_and_newer_or_odd_tags_are_never_taken(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.3", _debs("1.0.3"))                     # the release being published
    w.release("v1.0.9", _debs("1.0.9"))                     # NEWER (an old tag run again): not a previous release
    w.release("latest", _debs("0.0.1"))
    w.release("v1.0", _debs("1.0.0"))
    w.release("v1.0.2", _debs("1.0.2"))
    proc = w.run()
    assert proc.returncode == 0
    assert w.kept() == sorted(_debs("1.0.2"))
    assert "not older" in proc.stderr and "not a release tag" in proc.stderr


def test_count_limits_how_many_releases_are_kept(tmp_path: Path) -> None:
    w = World(tmp_path)
    for v in ("1.0.2", "1.0.1", "1.0.0"):
        w.release(f"v{v}", _debs(v))
    proc = w.run("--tag", "v1.0.3", "--keyring", str(w.keyring), "--out", str(w.out), "--count", "1")
    assert proc.returncode == 0 and w.kept() == sorted(_debs("1.0.2"))


# --- what is never kept --------------------------------------------------------------------------------------------
def test_a_deb_that_the_signed_metadata_does_not_list_is_left_out(tmp_path: Path) -> None:
    """The planted-package attack: someone with write access uploads lindos-evil_9.9.9_all.deb to an old release."""
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), extra={"lindos-evil_9.9.9_all.deb": b"payload", "lindos-core_9.9.9_all.deb": b"payload"})
    proc = w.run()
    assert proc.returncode == 0
    assert w.kept() == sorted(_debs("1.0.2"))
    assert "lindos-evil_9.9.9_all.deb is not listed" in proc.stderr and "lindos-core_9.9.9_all.deb is not listed" in proc.stderr


def test_a_deb_replaced_after_publication_is_left_out(tmp_path: Path) -> None:
    """A same-named lindos-core_<version>_all.deb with other bytes: it is listed, but the hash is not the signed one."""
    w = World(tmp_path)
    d = w.release("v1.0.2", _debs("1.0.2"))
    (d / "lindos-core_1.0.2_all.deb").write_bytes(b"malicious replacement")
    proc = w.run()
    assert proc.returncode == 0
    assert w.kept() == ["lindos-meta_1.0.2_all.deb"]        # the honest sibling is still fine
    assert "lindos-core_1.0.2_all.deb does not match the hash" in proc.stderr


def test_a_release_whose_signature_does_not_verify_gives_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), signature=BAD)
    proc = w.run()
    assert proc.returncode == 0 and w.kept() == []
    assert "does not verify against the committed keyring" in proc.stderr


def test_metadata_swapped_after_signing_gives_nothing(tmp_path: Path) -> None:
    """Packages edited (to list a planted deb) after Release was signed: the signed hash of Packages no longer matches."""
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), packages_tamper=True)
    proc = w.run()
    assert proc.returncode == 0 and w.kept() == []
    assert "is not the one the signed Release lists" in proc.stderr


def test_a_release_without_signed_metadata_gives_nothing_and_the_next_older_one_is_used(tmp_path: Path) -> None:
    """Releases from before the metadata was attached, or an attacker deleting it, cannot be trusted; they are skipped
    without counting against --count."""
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), metadata=False)
    w.release("v1.0.1", _debs("1.0.1"))
    proc = w.run()
    assert proc.returncode == 0
    assert w.kept() == sorted(_debs("1.0.1"))
    assert "no signed repository metadata" in proc.stderr


def test_a_release_with_only_unlisted_debs_is_not_counted(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), listed=[], also_list={"lindos-other_1.0.2_all.deb": b"o"})   # signed, but vouches for none of its files
    w.release("v1.0.1", _debs("1.0.1"))
    proc = w.run()
    assert proc.returncode == 0 and w.kept() == sorted(_debs("1.0.1"))
    assert "no package could be verified" in proc.stderr


def test_a_download_that_fails_skips_that_release_only(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.tags += ["v1.0.2"]                                    # listed, but its assets cannot be fetched
    w.release("v1.0.1", _debs("1.0.1"))
    proc = w.run()
    assert proc.returncode == 0 and w.kept() == sorted(_debs("1.0.1"))
    assert "could not download" in proc.stderr


def test_a_failing_release_list_is_not_an_error_but_keeps_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"))
    proc = w.run(extra_env={"FAKE_GH_FAIL_LIST": "1"})
    assert proc.returncode == 0 and w.kept() == [] and "could not list the releases" in proc.stderr


def test_a_file_name_that_is_already_kept_is_never_overwritten(tmp_path: Path) -> None:
    w = World(tmp_path)
    same = {"lindos-core_1.0.1_all.deb": b"first"}
    w.release("v1.0.2", same)
    w.release("v1.0.1", {"lindos-core_1.0.1_all.deb": b"second", "lindos-meta_1.0.1_all.deb": b"meta"})
    proc = w.run()
    assert proc.returncode == 0
    assert (w.out / "lindos-core_1.0.1_all.deb").read_bytes() == b"first"
    assert "is already there" in proc.stderr


def test_nothing_unverified_ever_reaches_the_output_directory(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.release("v1.0.2", _debs("1.0.2"), extra={"lindos-x_1.0.0_all.deb": b"x", "notes.txt": b"n", "Release.evil": b"e"})
    w.release("v1.0.1", _debs("1.0.1"), signature=BAD)
    w.release("v1.0.0", _debs("1.0.0"), packages_tamper=True)
    proc = w.run()
    assert proc.returncode == 0
    assert w.kept() == sorted(_debs("1.0.2"))               # only the .debs of the one verified release; no metadata, no notes
