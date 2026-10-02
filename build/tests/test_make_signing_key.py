"""build/tools/make-signing-key.sh: the offline key ceremony helper (docs/RELEASING.md).

It prints the ceremony by default (running nothing), and with --generate does the gpg steps for you into a
directory OUTSIDE the repository. Hermetic: gpg is a fake on PATH that only records what it is asked to do and
creates the files the real one would write, so a test can prove what the script does - and never does:
it never stores a private key in the repository, never puts a passphrase on a command line, never sends anything.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = REPO / "build" / "tools" / "make-signing-key.sh"
BASH = shutil.which("bash")
FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
SUBFPR = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_GPG = f"""#!/bin/bash
printf '%s | GNUPGHOME=%s\\n' "$*" "${{GNUPGHOME:-}}" >> "${{FAKE_GPG_LOG:?}}"
case " $* " in
    *" --list-keys "*)
        echo "pub:u:255:22:AAAA:1700000000:::u:::scESC:::+::ed25519:::0:"
        echo "fpr:::::::::{FPR}:"
        echo "sub:u:255:22:BBBB:1700000000:1800000000::::s:::+:::::"
        echo "fpr:::::::::{SUBFPR}:"
        ;;
    *" --export "*) printf 'PUBLIC-KEYRING-BYTES' ;;
    *" --export-secret-subkeys "*) printf 'SECRET-SUBKEY-TEXT' ;;
    *" --gen-revoke "*)
        out=""; prev=""
        for a in "$@"; do [ "$prev" = "--output" ] && out="$a"; prev="$a"; done
        [ -z "$out" ] || printf 'REVOCATION-CERT' > "$out"
        ;;
esac
exit 0
"""


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


class Run:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        gpg = self.bin / "gpg"
        gpg.write_text(FAKE_GPG, encoding="utf-8", newline="\n")
        gpg.chmod(gpg.stat().st_mode | stat.S_IEXEC)
        self.log = tmp / "gpg.log"

    def go(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = _msys(str(self.bin)) + os.pathsep + env.get("PATH", "")
        env["FAKE_GPG_LOG"] = _msys(str(self.log))
        if extra_env:
            env.update(extra_env)
        argv = [_msys(a) if (len(a) > 1 and a[1] == ":") else a for a in args]
        return subprocess.run([BASH, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=120)

    def gpg_calls(self) -> List[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


# --- static ----------------------------------------------------------------------------------------------
def test_script_follows_the_shell_rules() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text and "\r" not in text
    assert not any(line.strip().startswith("sudo ") for line in text.splitlines())
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_help() -> None:
    res = subprocess.run([BASH, str(SCRIPT), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0
    for word in ("--generate", "--out", "--uid", "--expire", "OUTSIDE this", "never"):
        assert word in res.stdout, word


def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    res = Run(tmp_path).go("--frobnicate")
    assert res.returncode == 2 and "unknown option" in res.stderr


# --- the printed ceremony ----------------------------------------------------------------------------------
def test_default_mode_only_prints_the_steps_and_runs_nothing(tmp_path: Path) -> None:
    r = Run(tmp_path)
    res = r.go()
    assert res.returncode == 0, res.stderr
    assert r.gpg_calls() == []                                          # not a single gpg call
    out = res.stdout
    assert out.count("gpg --quick-add-key") == 2 and "SPARE" in out
    for command in ("gpg --quick-generate-key", "ed25519 cert never", "gpg --quick-add-key", "ed25519 sign 2y",
                    "gpg --export ", "--export-secret-subkeys", "--gen-revoke",
                    "gh secret set LINDOS_APT_SIGNING_KEY --env apt-publish",
                    "gh secret set LINDOS_APT_SIGNING_KEY_PASSPHRASE --env apt-publish"):
        assert command in out, command
    # where the public material goes, and what has to be switched on together with it
    for path in ("packages/lindos-archive-keyring/root/usr/share/keyrings/lindos-archive-keyring.gpg",
                 "packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint",
                 "packages/lindos-archive-keyring/root/etc/apt/sources.list.d/lindos.sources"):
        assert path in out, path
    assert "Enabled: yes" in out and "docs/RELEASING.md" in out


def test_the_printed_steps_keep_the_private_key_out_of_the_repository(tmp_path: Path) -> None:
    out = Run(tmp_path).go().stdout
    assert "OFFLINE" in out.upper() and "NOT inside the Lindos repository" in out
    assert "pasted into a chat or committed" in out
    assert "lindos-signing-subkey.asc (shred -u)" in out
    # the only thing copied into the repository is public material
    copies = [line.strip() for line in out.splitlines() if line.strip().startswith("cp ")]
    assert copies and all("lindos-signing-subkey" not in c and "revoke" not in c for c in copies)
    assert "--passphrase" not in out                                    # nothing puts a passphrase on a command line


def test_uid_and_expiry_are_used_in_the_printed_commands(tmp_path: Path) -> None:
    out = Run(tmp_path).go("--uid", "Someone <a@b.example>", "--expire", "1y").stdout
    assert 'gpg --quick-generate-key "Someone <a@b.example>" ed25519 cert never' in out
    assert "ed25519 sign 1y" in out


# --- --generate ----------------------------------------------------------------------------------------
def test_generate_needs_an_output_directory(tmp_path: Path) -> None:
    r = Run(tmp_path)
    res = r.go("--generate")
    assert res.returncode == 2 and "--out" in res.stderr and r.gpg_calls() == []


def test_generate_refuses_an_output_directory_inside_the_repository(tmp_path: Path) -> None:
    r = Run(tmp_path)
    target = REPO / "out" / "keys-that-must-not-exist"
    res = r.go("--generate", "--out", str(target))
    assert res.returncode == 1 and "inside the repository" in res.stderr
    assert not target.exists()                                          # and nothing was created there
    assert r.gpg_calls() == []
    res2 = r.go("--generate", "--out", str(REPO / "packages" / "lindos-archive-keyring" / "keys"))
    assert res2.returncode == 1 and r.gpg_calls() == []
    assert not (REPO / "packages" / "lindos-archive-keyring" / "keys").exists()


def test_generate_refuses_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    r = Run(tmp_path)
    out = tmp_path / "keys"
    out.mkdir()
    (out / "old.txt").write_text("x", encoding="utf-8")
    res = r.go("--generate", "--out", str(out))
    assert res.returncode == 1 and "not empty" in res.stderr and r.gpg_calls() == []


def test_generate_creates_the_key_material_outside_the_repository(tmp_path: Path) -> None:
    r = Run(tmp_path)
    out = tmp_path / "keys"
    res = r.go("--generate", "--out", str(out), "--uid", "Test Archive <t@e.example>", "--expire", "1y")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert (out / "lindos-archive-keyring.gpg").read_text(encoding="utf-8") == "PUBLIC-KEYRING-BYTES"
    assert (out / "archive-key.fingerprint").read_text(encoding="utf-8").strip() == FPR
    assert (out / "lindos-signing-subkey.asc").read_text(encoding="utf-8") == "SECRET-SUBKEY-TEXT"
    assert (out / "revoke.asc").read_text(encoding="utf-8") == "REVOCATION-CERT"
    assert (out / "gnupg").is_dir()
    if os.name == "posix":
        for secret in ("lindos-signing-subkey.asc", "revoke.asc"):
            assert (out / secret).stat().st_mode & 0o777 == 0o600
        assert (out / "gnupg").stat().st_mode & 0o777 == 0o700
    assert FPR in res.stderr + res.stdout


def test_generate_runs_the_expected_gpg_steps_in_a_private_key_ring(tmp_path: Path) -> None:
    r = Run(tmp_path)
    out = tmp_path / "keys"
    assert r.go("--generate", "--out", str(out), "--expire", "18m").returncode == 0
    calls = r.gpg_calls()
    joined = "\n".join(calls)
    assert "--quick-generate-key" in calls[0] and "ed25519 cert never" in calls[0]
    adds = [c for c in calls if f"--quick-add-key {FPR} ed25519 sign 18m" in c]
    assert len(adds) == 2                                                               # the signing subkey and a SPARE
    exports = [c for c in calls if "--export-secret-subkeys" in c]
    assert len(exports) == 1 and SUBFPR + "!" in exports[0]                            # only ONE secret subkey leaves gnupg/
    assert any("--export-secret-subkeys" in c and SUBFPR + "!" in c for c in calls)     # exactly the subkey, not the primary
    assert any("--gen-revoke" in c and FPR in c for c in calls)
    homes = {c.rsplit("GNUPGHOME=", 1)[1] for c in calls}
    assert len(homes) == 1 and next(iter(homes)).endswith("/keys/gnupg")               # never the user's own key ring
    assert "--passphrase" not in joined and "--batch" not in joined                     # gpg asks; no secret on a command line


def test_generate_never_writes_anything_into_the_repository(tmp_path: Path) -> None:
    def snapshot() -> int:
        return sum(1 for _ in (REPO / "packages" / "lindos-archive-keyring").rglob("*"))
    before = snapshot()
    r = Run(tmp_path)
    assert r.go("--generate", "--out", str(tmp_path / "keys")).returncode == 0
    assert snapshot() == before
    assert (REPO / "packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint").read_text(encoding="utf-8").strip() != FPR
