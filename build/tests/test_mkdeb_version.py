"""build/mkdeb.sh: lock-step version stamping and the archive-keyring guard (SPEC-UPDATE.md, docs/RELEASING.md).

Every lindos-* package of a release carries one version (the VERSION file, or --version / LINDOS_PKG_VERSION),
stamped into the STAGING copy only - the Version fields, every exact ``lindos-x (= old)`` pin in a relationship
field, ``lindos.__version__`` - so the source tree (and the tests that read it) never change.

Two layers, both hermetic:

* ``stamp_version`` extracted from mkdeb.sh and run against crafted staging trees (fast, edge cases);
* a real ``mkdeb.sh`` run with a fake ``dpkg-deb`` on PATH (the host has no dpkg): what the .deb would contain.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MKDEB = REPO / "build" / "mkdeb.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

_FUNC_RE = re.compile(r"\nstamp_version\(\)\s*\{.*?\n\}\n", re.DOTALL)


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _fields(control: str) -> Dict[str, str]:
    """Top-level fields of a control file (continuation lines joined with newlines)."""
    out: Dict[str, str] = {}
    key = ""
    for line in control.splitlines():
        if line[:1] in (" ", "\t") and key:
            out[key] += "\n" + line
        elif ":" in line:
            key, _, value = line.partition(":")
            out[key] = value.strip()
    return out


# =================================================================================================
# stamp_version, in isolation
# =================================================================================================
def _stamp(tmp: Path, control: str, version: str, *, init: Optional[str] = None) -> Dict[str, str]:
    stage = tmp / "stage"
    (stage / "DEBIAN").mkdir(parents=True)
    (stage / "DEBIAN" / "control").write_text(control, encoding="utf-8", newline="\n")
    if init is not None:
        d = stage / "usr" / "lib" / "python3" / "dist-packages" / "lindos"
        d.mkdir(parents=True)
        (d / "__init__.py").write_text(init, encoding="utf-8", newline="\n")
    func = _FUNC_RE.search(MKDEB.read_text(encoding="utf-8"))
    assert func, "stamp_version() not found in build/mkdeb.sh"
    script = f"set -Eeuo pipefail\n{func.group(0)}\nstamp_version {shlex.quote(_msys(str(stage)))} {shlex.quote(version)}\n"
    proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return {"control": (stage / "DEBIAN" / "control").read_text(encoding="utf-8"),
            "init": (stage / "usr/lib/python3/dist-packages/lindos/__init__.py").read_text(encoding="utf-8") if init is not None else ""}


META = """Package: lindos-meta
Version: 1.0.0
Architecture: all
Maintainer: Lindos Team <team@lindos.dev>
Depends: lindos-core (= 1.0.0), lindos-archive-keyring (= 1.0.0), lindos-desktop (= 1.0.0),
 lindos-setup (= 1.0.0), python3 (>= 3.10), foo (= 1.0.0)
Pre-Depends: lindos-tune (= 1.0.0)
Recommends: lindos-kernel, lindos-vm (>= 1.0.0), firefox (= 1.0.0) | lindos-x (= 1.0.0)
Suggests: lutris
Breaks: lindos-old (= 1.0.0)
Description: a meta package
 mentions lindos-core (= 1.0.0) in prose and Version: 1.0.0 too
 .
 Depends: lindos-gaming (= 1.0.0) is only text here
"""


def test_stamp_rewrites_the_version_field_and_every_exact_lindos_pin(tmp_path: Path) -> None:
    out = _stamp(tmp_path, META, "1.2.3")["control"]
    f = _fields(out)
    assert f["Version"] == "1.2.3"
    assert f["Depends"].splitlines()[0] == ("lindos-core (= 1.2.3), lindos-archive-keyring (= 1.2.3), lindos-desktop (= 1.2.3),")
    assert "lindos-setup (= 1.2.3)" in f["Depends"]                  # the continuation line too
    assert f["Pre-Depends"] == "lindos-tune (= 1.2.3)"
    assert f["Breaks"] == "lindos-old (= 1.2.3)"
    assert "lindos-x (= 1.2.3)" in f["Recommends"]


def test_stamp_leaves_everything_else_alone(tmp_path: Path) -> None:
    out = _stamp(tmp_path, META, "1.2.3")["control"]
    f = _fields(out)
    assert "python3 (>= 3.10)" in f["Depends"]                        # not a lindos pin
    assert "foo (= 1.0.0)" in f["Depends"] and "firefox (= 1.0.0)" in f["Recommends"]   # other packages keep their pins
    assert "lindos-vm (>= 1.0.0)" in f["Recommends"]                  # a minimum is not an exact pin
    assert f["Suggests"] == "lutris"
    assert "mentions lindos-core (= 1.0.0) in prose and Version: 1.0.0 too" in out      # the description is prose
    assert " Depends: lindos-gaming (= 1.0.0) is only text here" in out
    assert out.count("\nVersion:") == 1


def test_stamp_keeps_line_structure(tmp_path: Path) -> None:
    out = _stamp(tmp_path, META, "1.2.3")["control"]
    assert len(out.splitlines()) == len(META.splitlines())
    assert out.endswith("\n") and "\r" not in out


@pytest.mark.parametrize("version", ["2.0.0", "1.0.1~rc1", "1.0.0+ci.7", "10.20.30", "3"])
def test_stamp_accepts_the_versions_mkdeb_accepts(tmp_path: Path, version: str) -> None:
    out = _stamp(tmp_path, META, version)["control"]
    assert _fields(out)["Version"] == version and f"lindos-core (= {version})" in out


def test_stamp_is_idempotent(tmp_path: Path) -> None:
    once = _stamp(tmp_path / "a", META, "1.2.3")["control"]
    (tmp_path / "b").mkdir()
    twice = _stamp(tmp_path / "b", once, "1.2.3")["control"]
    assert once == twice


def test_stamp_sets_the_python_module_version(tmp_path: Path) -> None:
    init = '"""doc"""\n\n__version__ = "1.0.0"\n__codename__ = "Aurora"\nVERSION = __version__\n'
    out = _stamp(tmp_path, META, "1.2.3", init=init)["init"]
    assert '__version__ = "1.2.3"' in out and '__codename__ = "Aurora"' in out and "VERSION = __version__" in out
    assert out.count("__version__ =") == 1


def test_a_package_without_pins_is_only_versioned(tmp_path: Path) -> None:
    plain = "Package: lindos-tune\nVersion: 1.0.0\nDepends: lindos-core (>= 1.0.0), python3\nDescription: x\n y\n"
    out = _stamp(tmp_path, plain, "1.2.3")["control"]
    assert _fields(out)["Version"] == "1.2.3" and "lindos-core (>= 1.0.0)" in out


# =================================================================================================
# a real mkdeb.sh run (fake dpkg-deb)
# =================================================================================================
FAKE_DPKG_DEB = """#!/bin/bash
# a stand-in for dpkg-deb: --build STAGE OUT copies the control file and a file list to OUT (a plain file)
case "$1" in
    --build)
        shift
        while [ "${1#-}" != "$1" ]; do shift; done
        stage="$1"; out="$2"
        { cat "$stage/DEBIAN/control"; echo "--- files"; (cd "$stage" && find . -type f | sort); } > "$out"
        ;;
    -I) cat "$2" ;;
    -f) grep -m1 "^$3:" "$2" | sed "s/^$3: *//" ;;
esac
"""


def _fake_bin(tmp: Path) -> Path:
    b = tmp / "fakebin"
    b.mkdir()
    p = b / "dpkg-deb"
    p.write_text(FAKE_DPKG_DEB, encoding="utf-8", newline="\n")
    os.chmod(p, 0o755)
    return b


def _mkdeb(tmp: Path, *args: str, env_extra: Optional[Dict[str, str]] = None, packages_dir: Optional[Path] = None,
           bin_dir: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = _msys(str(bin_dir or _fake_bin(tmp))) + os.pathsep + env.get("PATH", "")
    env["WORK_DIR"] = _msys(str(tmp / "work"))
    env["DEBS_DIR"] = _msys(str(tmp / "debs"))
    env.pop("LINDOS_PKG_VERSION", None)
    if packages_dir is not None:
        env["LINDOS_PACKAGES_DIR"] = _msys(str(packages_dir))
    if env_extra:
        env.update(env_extra)
    argv = [_msys(a) if os.path.isabs(a) or (len(a) > 1 and a[1] == ":") else a for a in args]
    return subprocess.run([BASH, str(MKDEB), "--out", _msys(str(tmp / "debs")), "--keep", *argv],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=600)


def _deb(tmp: Path, name: str, version: str) -> str:
    path = tmp / "debs" / f"{name}_{version}_all.deb"
    assert path.is_file(), sorted(p.name for p in (tmp / "debs").glob("*"))
    return path.read_text(encoding="utf-8")


CORE_INIT = REPO / "packages/lindos-core/root/usr/lib/python3/dist-packages/lindos/__init__.py"
CORE_INIT_BEFORE = CORE_INIT.read_bytes()
SOURCE_CONTROL = {p.name: (p / "DEBIAN" / "control").read_bytes()
                  for p in (REPO / "packages").iterdir() if (p / "DEBIAN" / "control").is_file()}


def test_a_real_run_stamps_meta_core_and_the_keyring_in_lock_step(tmp_path: Path) -> None:
    proc = _mkdeb(tmp_path, "--version", "1.2.3", "lindos-meta", "lindos-archive-keyring", "lindos-core")
    assert proc.returncode == 0, proc.stderr[-3000:]
    meta = _deb(tmp_path, "lindos-meta", "1.2.3")
    fields = _fields(meta.split("--- files")[0])
    assert fields["Version"] == "1.2.3"
    pins = re.findall(r"(lindos-[a-z-]+) \(= ([^)]+)\)", fields["Depends"])
    names = [n for n, _v in pins]
    assert {"lindos-core", "lindos-archive-keyring", "lindos-desktop", "lindos-setup", "lindos-settings",
            "lindos-compat", "lindos-gaming", "lindos-tune", "lindos-transfer"} <= set(names)
    assert {v for _n, v in pins} == {"1.2.3"}                        # every pin moved with the version
    keyring = _deb(tmp_path, "lindos-archive-keyring", "1.2.3")
    assert _fields(keyring.split("--- files")[0])["Version"] == "1.2.3"
    core = _deb(tmp_path, "lindos-core", "1.2.3")
    assert _fields(core.split("--- files")[0])["Version"] == "1.2.3"
    # the python module reports the release version too (checked in the staging copy, which --keep left behind)
    init = (tmp_path / "work" / "deb-staging" / "lindos-core" / "usr/lib/python3/dist-packages/lindos/__init__.py")
    assert '__version__ = "1.2.3"' in init.read_text(encoding="utf-8")
    # ... and the SOURCE tree is never touched: the stamping happens in the staging copy only
    for name, before in SOURCE_CONTROL.items():
        assert (REPO / "packages" / name / "DEBIAN" / "control").read_bytes() == before, name
    assert CORE_INIT.read_bytes() == CORE_INIT_BEFORE


def test_the_version_comes_from_the_flag_then_the_environment_then_the_version_file(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    both = _mkdeb(tmp_path / "a", "--version", "3.0.0", "lindos-archive-keyring", env_extra={"LINDOS_PKG_VERSION": "2.0.0"})
    assert both.returncode == 0 and _deb(tmp_path / "a", "lindos-archive-keyring", "3.0.0")
    env_only = _mkdeb(tmp_path / "b", "lindos-archive-keyring", env_extra={"LINDOS_PKG_VERSION": "2.0.0"})
    assert env_only.returncode == 0 and _deb(tmp_path / "b", "lindos-archive-keyring", "2.0.0")
    default = _mkdeb(tmp_path / "c", "lindos-archive-keyring")
    file_version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert default.returncode == 0 and _deb(tmp_path / "c", "lindos-archive-keyring", file_version)


@pytest.mark.parametrize("bad", ["abc", "1.0.0-1", "v1.0.0", "1..0", "1.0 .0", "1.0.0;rm"])
def test_an_invalid_version_is_refused_before_anything_is_built(tmp_path: Path, bad: str) -> None:
    proc = _mkdeb(tmp_path, f"--version={bad}", "lindos-archive-keyring")
    assert proc.returncode == 2 and "invalid release version" in proc.stderr
    built = list((tmp_path / "debs").glob("*.deb")) if (tmp_path / "debs").exists() else []
    assert built == []


def test_version_is_documented_in_the_help() -> None:
    proc = subprocess.run([BASH, str(MKDEB), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    for word in ("--version", "LINDOS_PKG_VERSION", "VERSION", "lock-step", "LINDOS_PACKAGES_DIR"):
        assert word in proc.stdout


# =================================================================================================
# the keyring guard inside mkdeb
# =================================================================================================
def _keyring_packages(tmp: Path, *, enabled: str, key: bytes, fpr: str, uri: str) -> Path:
    pk = tmp / "packages"
    shutil.copytree(REPO / "packages" / "lindos-archive-keyring", pk / "lindos-archive-keyring", ignore=shutil.ignore_patterns("tests"))
    root = pk / "lindos-archive-keyring" / "root"
    (root / "usr/share/keyrings/lindos-archive-keyring.gpg").write_bytes(key)
    (root / "usr/share/lindos/archive-key.fingerprint").write_text(fpr + "\n", encoding="utf-8")
    (root / "etc/apt/sources.list.d/lindos.sources").write_text(
        f"Enabled: {enabled}\nTypes: deb\nURIs: {uri}\nSuites: ./\nSigned-By: /usr/share/keyrings/lindos-archive-keyring.gpg\n",
        encoding="utf-8", newline="\n")
    return pk


def test_the_committed_keyring_is_built_without_a_key_while_it_is_a_placeholder(tmp_path: Path) -> None:
    proc = _mkdeb(tmp_path, "lindos-archive-keyring")
    assert proc.returncode == 0, proc.stderr[-2000:]
    files = _deb(tmp_path, "lindos-archive-keyring", (REPO / "VERSION").read_text(encoding="utf-8").strip()).split("--- files")[1]
    assert "./etc/apt/sources.list.d/lindos.sources" in files
    assert "lindos-archive-keyring.gpg" not in files                  # a fake key is never shipped
    assert "WITHOUT a signing key" in proc.stderr


def test_a_switched_on_source_with_the_placeholder_key_cannot_be_built(tmp_path: Path) -> None:
    pk = _keyring_packages(tmp_path, enabled="yes", key=b"LINDOS-PLACEHOLDER-KEYRING\n", fpr="PLACEHOLDER",
                           uri="https://apt.example.test/stable/")
    proc = _mkdeb(tmp_path, "lindos-archive-keyring", packages_dir=pk)
    assert proc.returncode != 0
    assert "REFUSED" in proc.stderr and "refusing to build" in proc.stderr
    built = list((tmp_path / "debs").glob("*.deb")) if (tmp_path / "debs").exists() else []
    assert built == []


def test_a_switched_on_source_at_a_reserved_address_cannot_be_built(tmp_path: Path) -> None:
    pk = _keyring_packages(tmp_path, enabled="yes", key=b"\x99\x01\x0dkey\x00", fpr="0123456789ABCDEF0123456789ABCDEF01234567",
                           uri="https://apt.lindos.invalid/stable/")
    gpg_dir = tmp_path / "gpgbin"
    gpg_dir.mkdir()
    gpg = gpg_dir / "gpg"
    gpg.write_text('#!/bin/bash\necho "pub:-:255:22:AAAA:1::::::scESC:::::"\necho "fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:"\n',
                   encoding="utf-8", newline="\n")
    os.chmod(gpg, 0o755)
    fake = _fake_bin(tmp_path)
    proc = _mkdeb(tmp_path, "lindos-archive-keyring", packages_dir=pk, bin_dir=fake,
                  env_extra={"PATH": _msys(str(gpg_dir)) + os.pathsep + _msys(str(fake)) + os.pathsep + os.environ.get("PATH", "")})
    assert proc.returncode != 0 and "placeholder name" in proc.stderr


def test_a_real_pinned_key_with_a_real_address_ships_the_key(tmp_path: Path) -> None:
    fpr = "0123456789ABCDEF0123456789ABCDEF01234567"
    pk = _keyring_packages(tmp_path, enabled="yes", key=b"\x99\x01\x0dreal-key-bytes\x00", fpr=fpr,
                           uri="http://127.0.0.1:8099/stable/")
    gpg_dir = tmp_path / "gpgbin"
    gpg_dir.mkdir()
    gpg = gpg_dir / "gpg"
    gpg.write_text(f'#!/bin/bash\necho "pub:-:255:22:AAAA:1::::::scESC:::::"\necho "fpr:::::::::{fpr}:"\n', encoding="utf-8", newline="\n")
    os.chmod(gpg, 0o755)
    fake = _fake_bin(tmp_path)
    proc = _mkdeb(tmp_path, "--version", "1.0.7", "lindos-archive-keyring", packages_dir=pk, bin_dir=fake,
                  env_extra={"PATH": _msys(str(gpg_dir)) + os.pathsep + _msys(str(fake)) + os.pathsep + os.environ.get("PATH", "")})
    assert proc.returncode == 0, proc.stderr[-2000:]
    files = _deb(tmp_path, "lindos-archive-keyring", "1.0.7").split("--- files")[1]
    assert "./usr/share/keyrings/lindos-archive-keyring.gpg" in files
    assert "WITHOUT a signing key" not in proc.stderr
