"""How the update source gets onto an image and how versions stay in lock-step (SPEC-UPDATE.md, docs/RELEASING.md):

* the lindos-archive-keyring package is installed by ``build/chroot/30-lindos-debs.sh``, in the same apt transaction as
  lindos-core, and by no earlier hook (an early ``dpkg -i --force-depends`` left apt refusing every install of hooks
  10 and 20); the package needs nothing, so no order can leave apt with an unmet dependency. Hook 30 then checks the
  source, exercises an enabled one with ``apt-get update`` and, with ``LINDOS_APT_REPO_REQUIRE=1`` (release images),
  fails unless it ends up enabled. No key is fetched from the network;
* ``build/config.env``: the update variables, the deb order, and the VERSION file as the single version source;
* the source tree is internally in lock-step: one version everywhere and lindos-meta pinning exactly it.

Hermetic: the source-check section of hook 30 is extracted and run in bash with stubs, a fake ``apt-get`` and scratch
paths.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
HOOK = REPO / "build" / "chroot" / "00-repos.sh"
CONFIG = REPO / "build" / "config.env"
PACKAGES = REPO / "packages"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

HOOK_TEXT = HOOK.read_text(encoding="utf-8")


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


def _control(name: str) -> Dict[str, str]:
    text = (PACKAGES / name / "DEBIAN" / "control").read_text(encoding="utf-8")
    out: Dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and key:
            out[key] += "\n" + line
        elif ":" in line:
            key, _, value = line.partition(":")
            out[key] = value.strip()
    return out


# =================================================================================================
# the keyring is installed by 30-lindos-debs.sh and by nothing earlier
# =================================================================================================
CHROOT = REPO / "build" / "chroot"


def _code(path: Path) -> str:
    """A script without its comment lines (the comments explain the history and name what is forbidden)."""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))


def _hooks_before_30() -> List[Path]:
    return sorted(p for p in CHROOT.glob("[0-9][0-9]-*.sh") if int(p.name[:2]) < 30)


def test_no_hook_before_30_unpacks_a_lindos_package_or_forces_dependencies() -> None:
    """The regression: 00-repos.sh ran ``dpkg -i --force-depends`` on the keyring, whose Depends stayed unmet until
    hook 30; apt then refused every install and purge of hooks 10 and 20 and the image build died in hook 20."""
    hooks = _hooks_before_30()
    assert [h.name for h in hooks] == ["00-repos.sh", "10-debloat.sh", "20-base.sh"]
    for hook in hooks:
        code = _code(hook)
        assert "--force-depends" not in code and "--force-all" not in code, hook.name
        assert not re.search(r"\bdpkg\s+(-i|--install|--unpack)\b", code), hook.name
        assert "lindos-archive-keyring" not in code, hook.name


def test_hook_00_fetches_no_key_and_writes_no_lindos_source() -> None:
    code = _code(HOOK)
    assert "LINDOS_APT_REPO_URL" not in HOOK_TEXT and "LINDOS_APT_REPO_ENABLE" not in HOOK_TEXT
    assert "lindos.sources" not in code and "lindos.list" not in code and "lindos-archive-keyring" not in code
    assert "LINDOS_APT_REPO_REQUIRE" not in code                          # the release-image guard lives in hook 30 now
    m = re.search(r"^# 7b\..*?(?=^# -{20,}\n# 8\.)", HOOK_TEXT, re.M | re.S)
    assert m and "30-lindos-debs.sh" in m.group(0)                        # the why stays next to the place it was removed


def test_hook_00_keeps_its_other_repositories() -> None:
    for marker in ("winehq", "lindos-steam.list", "flathub", "KISAK_MESA", "apt_update --force"):
        assert marker in HOOK_TEXT, marker


def test_the_keyring_is_part_of_the_single_apt_transaction_of_hook_30() -> None:
    text = HOOK30.read_text(encoding="utf-8")
    default = re.search(r'LINDOS_DEB_ORDER:=([a-z0-9 _-]+)\}', text).group(1).split()
    assert default[:2] == ["lindos-archive-keyring", "lindos-core"] and default[-1] == "lindos-meta"
    assert 'apt-get "${APT_ARGS[@]}" install --no-install-recommends "${ordered[@]}"' in text
    # the keyring is checked only AFTER that transaction (and its dpkg fallback) has finished
    assert text.index('install --no-install-recommends "${ordered[@]}"') < text.index("The Lindos update source")


def test_the_keyring_needs_nothing_so_no_install_order_can_leave_apt_broken() -> None:
    fields = _control("lindos-archive-keyring")
    for field in ("Depends", "Pre-Depends"):
        assert field not in fields, field


# =================================================================================================
# 30-lindos-debs.sh: the source check after the install, run in bash with stubs
# =================================================================================================
HOOK30 = CHROOT / "30-lindos-debs.sh"
HOOK30_TEXT = HOOK30.read_text(encoding="utf-8")


def _source_section() -> str:
    m = re.search(r"^# The Lindos update source.*?(?=^# -{20,}\n# Optional: packages the default mode)", HOOK30_TEXT, re.M | re.S)
    assert m, "the Lindos update source section of 30-lindos-debs.sh was not found"
    return m.group(0)


class Section:
    """Runs that section in bash with stub log/warn/die/pkg_installed, a fake apt-get and scratch paths."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.sources = tmp / "lindos.sources"
        self.keyring = tmp / "lindos-archive-keyring.gpg"
        self.bin = tmp / "bin"
        self.calls = tmp / "apt-get.log"
        self.bin.mkdir()
        apt = self.bin / "apt-get"
        apt.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{_msys(str(self.calls))}"\n[ -z "${{FAKE_APT_FAIL:-}}" ] || exit 100\n',
                       encoding="utf-8", newline="\n")
        apt.chmod(0o755)

    def run(self, *, require: str = "0", installed: bool = True, source: Optional[str] = None, keyring: bool = True,
            apt_fails: bool = False) -> subprocess.CompletedProcess:
        assert BASH is not None
        if source is not None:
            self.sources.write_text(f"Enabled: {source}\nTypes: deb\nURIs: https://x.example/\n", encoding="utf-8")
        if keyring:
            self.keyring.write_bytes(b"\x99\x01key")
        script = (
            "set -Eeuo pipefail\n"
            'log() { printf "LOG: %s\\n" "$*"; }\n'
            'warn() { printf "WARN: %s\\n" "$*"; }\n'
            'die() { printf "DIE: %s\\n" "$*"; exit 1; }\n'
            f'pkg_installed() {{ [ "{1 if installed else 0}" = 1 ]; }}\n'
            "APT_ARGS=(-y)\n"
            'DEBS_DIR=/staged\n'
            f'export LINDOS_SOURCES_FILE="{_msys(str(self.sources))}"\n'
            f'export LINDOS_KEYRING_FILE="{_msys(str(self.keyring))}"\n'
            f'export LINDOS_APT_REPO_REQUIRE="{require}"\n'
            + _source_section())
        env = dict(os.environ)
        env["PATH"] = _msys(str(self.bin)) + os.pathsep + env.get("PATH", "")
        if apt_fails:
            env["FAKE_APT_FAIL"] = "1"
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)

    def apt_calls(self) -> List[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []


@needs_bash
def test_a_disabled_source_is_reported_and_apt_is_not_asked_anything(tmp_path: Path) -> None:
    s = Section(tmp_path)
    proc = s.run(source="no")
    assert proc.returncode == 0, proc.stdout
    assert "switched off" in proc.stdout and "DIE" not in proc.stdout
    assert s.apt_calls() == []


@needs_bash
def test_an_enabled_source_is_exercised_with_apt_get_update(tmp_path: Path) -> None:
    s = Section(tmp_path)
    proc = s.run(source="yes")
    assert proc.returncode == 0, proc.stdout
    assert "source enabled" in proc.stdout and "https://x.example/" in proc.stdout
    assert s.apt_calls() == ["-y update"]


@needs_bash
def test_an_enabled_source_without_its_keyring_stops_the_build(tmp_path: Path) -> None:
    s = Section(tmp_path)
    proc = s.run(source="yes", keyring=False)
    assert proc.returncode == 1 and "keyring" in proc.stdout and "missing" in proc.stdout
    assert s.apt_calls() == []


@needs_bash
def test_a_source_apt_rejects_is_a_warning_unless_a_release_image_requires_it(tmp_path: Path) -> None:
    proc = Section(tmp_path).run(source="yes", apt_fails=True)
    assert proc.returncode == 0 and "WARN" in proc.stdout and "DIE" not in proc.stdout
    (tmp_path / "r").mkdir()
    proc = Section(tmp_path / "r").run(source="yes", apt_fails=True, require="1")
    assert proc.returncode == 1 and "DIE: apt-get update failed with the Lindos source enabled" in proc.stdout


@needs_bash
def test_a_missing_package_is_a_warning_unless_a_release_image_requires_it(tmp_path: Path) -> None:
    proc = Section(tmp_path).run(installed=False)
    assert proc.returncode == 0 and "WARN" in proc.stdout and "no lindos-archive-keyring installed" in proc.stdout
    (tmp_path / "r").mkdir()
    proc = Section(tmp_path / "r").run(installed=False, require="1")
    assert proc.returncode == 1 and "DIE: LINDOS_APT_REPO_REQUIRE=1 but lindos-archive-keyring is not installed" in proc.stdout


@needs_bash
@pytest.mark.parametrize("value", ["no", "No", "false", "0", "OFF"])
def test_every_way_of_writing_disabled_counts_as_disabled(tmp_path: Path, value: str) -> None:
    proc = Section(tmp_path).run(require="1", source=value)
    assert proc.returncode == 1 and "not enabled" in proc.stdout


@needs_bash
def test_a_release_image_needs_an_enabled_source(tmp_path: Path) -> None:
    s = Section(tmp_path)
    ok = s.run(require="1", source="yes")
    assert ok.returncode == 0 and "source enabled" in ok.stdout
    (tmp_path / "gone").mkdir()
    gone = Section(tmp_path / "gone").run(require="1")                      # no source file at all
    assert gone.returncode == 1 and "not enabled" in gone.stdout
    (tmp_path / "off").mkdir()
    off = Section(tmp_path / "off").run(require="0")
    assert off.returncode == 0 and "switched off" in off.stdout


# =================================================================================================
# config.env
# =================================================================================================
def test_the_update_variables() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert re.search(r'^: "\$\{LINDOS_APT_REPO_REQUIRE:=0\}"', text, re.M)        # off unless it is a release image
    assert re.search(r"^export .*\bLINDOS_APT_REPO_REQUIRE\b", text, re.M)
    for gone in ("LINDOS_APT_REPO_URL", "LINDOS_APT_REPO_ENABLE", "packages.lindos.dev"):
        assert gone not in text, gone                                             # the address lives in the conffile only
    assert "lindos.sources" in text and "lindos-archive-keyring" in text


def test_the_keyring_package_is_in_the_deb_order_before_lindos_core() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    order = re.search(r'LINDOS_DEB_ORDER:=([a-z0-9 _-]+)\}', text).group(1).split()
    assert "lindos-archive-keyring" in order and order.index("lindos-archive-keyring") < order.index("lindos-core")
    assert order.index("lindos-meta") == len(order) - 1


def _source(script: str, tmp: Path, env_extra: Optional[Dict[str, str]] = None) -> str:
    env = dict(os.environ)
    env.pop("LINDOS_VERSION", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@needs_bash
def test_lindos_version_follows_the_version_file(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    shutil.copy(CONFIG, tmp_path / "build" / "config.env")
    (tmp_path / "VERSION").write_text("7.8.9\n", encoding="utf-8", newline="\n")
    cfg = _msys(str(tmp_path / "build" / "config.env"))
    assert _source(f'. "{cfg}"; echo "$LINDOS_VERSION $ISO_NAME"', tmp_path) == "7.8.9 lindos-7.8.9-xfce-64bit.iso"
    # the environment still wins, and a CRLF file does not leak a carriage return
    assert _source(f'. "{cfg}"; echo "$LINDOS_VERSION"', tmp_path, {"LINDOS_VERSION": "2.0.0"}) == "2.0.0"
    (tmp_path / "VERSION").write_bytes(b"7.8.9\r\n")
    assert _source(f'. "{cfg}"; printf "%s" "$LINDOS_VERSION" | od -An -c | tr -d " "', tmp_path) == "7.8.9"
    (tmp_path / "VERSION").unlink()
    assert _source(f'. "{cfg}"; echo "$LINDOS_VERSION"', tmp_path) == "1.0.0"       # the built-in default without the file


@needs_bash
def test_the_repository_version_file_and_config_agree() -> None:
    version = (REPO / "VERSION").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", version)
    cfg = _msys(str(CONFIG))
    assert _source(f'. "{cfg}"; echo "$LINDOS_VERSION"', REPO) == version.strip()


# =================================================================================================
# the source tree is in lock-step
# =================================================================================================
PKGS = sorted(p.name for p in PACKAGES.iterdir() if (p / "DEBIAN" / "control").is_file())


def test_every_package_carries_the_same_version() -> None:
    versions = {name: _control(name)["Version"] for name in PKGS}
    assert len(set(versions.values())) == 1, versions


def test_the_python_module_reports_the_same_version_as_the_packages() -> None:
    init = (PACKAGES / "lindos-core/root/usr/lib/python3/dist-packages/lindos/__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^__version__ = "([^"]+)"$', init, re.M).group(1) == _control("lindos-core")["Version"]


def test_lindos_meta_pins_exactly_that_version_for_every_component() -> None:
    version = _control("lindos-meta")["Version"]
    depends = _control("lindos-meta")["Depends"]
    pins = re.findall(r"(lindos-[a-z-]+) \(= ([^)]+)\)", depends)
    assert pins and {v for _n, v in pins} == {version}
    assert {"lindos-core", "lindos-archive-keyring"} <= {n for n, _v in pins}
    for name, _v in pins:
        assert (PACKAGES / name / "DEBIAN" / "control").is_file(), f"lindos-meta pins {name}, which is not a package"


def test_no_other_package_pins_an_exact_lindos_version() -> None:
    for name in PKGS:
        if name == "lindos-meta":
            continue
        for field in ("Depends", "Pre-Depends", "Recommends", "Suggests", "Breaks", "Conflicts"):
            assert not re.search(r"lindos-[a-z-]+ \(= ", _control(name).get(field, "")), (name, field)


def test_meta_depends_on_the_keyring_so_every_install_has_the_update_source() -> None:
    assert "lindos-archive-keyring (= " in _control("lindos-meta")["Depends"]


def test_the_version_file_is_the_release_version() -> None:
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert (REPO / "VERSION").read_bytes().count(b"\n") == 1 and b"\r" not in (REPO / "VERSION").read_bytes()
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog, "CHANGELOG.md has no section for the version in VERSION"
