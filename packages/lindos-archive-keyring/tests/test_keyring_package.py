"""lindos-archive-keyring: the Lindos apt source and public key, and the guard that a build can never ship a
fake trusted key (SPEC-UPDATE.md §36.5, docs/RELEASING.md).

The guard is ``build/tools/check-archive-keyring.sh`` (called by ``build/mkdeb.sh``). These tests

* check the committed tree (the URL lives in exactly one file; the source is switched off and the key is the
  placeholder until the owner supplies real ones - and if someone flips the source on with the placeholder key,
  a test here fails);
* run the guard against a matrix of fixture trees (fake ``gpg`` on PATH for the fingerprint comparison: exactly one
  primary key, subkeys allowed; the source file: one stanza, one https address, the shipped keyring, no trust overrides);
* run the maintainer script against a scratch root.

Hermetic: no real gpg, no network; the guard is run as ``bash <script>``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional, Sequence

import pytest

PKG = Path(__file__).resolve().parent.parent
REPO = PKG.parent.parent
ROOT = PKG / "root"
DEBIAN = PKG / "DEBIAN"
SOURCES = ROOT / "etc" / "apt" / "sources.list.d" / "lindos.sources"
KEYRING = ROOT / "usr" / "share" / "keyrings" / "lindos-archive-keyring.gpg"
FPR_FILE = ROOT / "usr" / "share" / "lindos" / "archive-key.fingerprint"
CHECK = REPO / "build" / "tools" / "check-archive-keyring.sh"
BASH = shutil.which("bash")
SH = shutil.which("sh")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

REAL_FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER_FPR = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
REAL_KEY_BYTES = b"\x99\x01\x0d\x04fake-public-key-bytes\x00\x01\x02"
PLACEHOLDER_KEY = b"LINDOS-PLACEHOLDER-KEYRING\nnot a key\n"


def _msys(value: str) -> str:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


# =================================================================================================
# the committed package
# =================================================================================================
def _control() -> Dict[str, str]:
    text = (DEBIAN / "control").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Za-z-]+): (.*)$", text, re.M))


def test_control_fields() -> None:
    fields = _control()
    assert fields["Package"] == "lindos-archive-keyring"
    assert fields["Architecture"] == "all" and fields["Maintainer"] == "Lindos Team <team@lindos.dev>"
    assert re.fullmatch(r"\d+\.\d+\.\d+", fields["Version"])
    # self-contained: an unmet Depends left behind by an early dpkg -i makes apt refuse every install and purge
    # that follows (the image build hit exactly that); it ships two files and a postinst that needs only sh
    for field in ("Depends", "Pre-Depends", "Recommends", "Suggests"):
        assert field not in fields, field


def test_the_maintainer_script_needs_nothing_but_the_shell() -> None:
    """Which is why the package can have no Depends: it has to run in a bare chroot before lindos-core exists."""
    text = (DEBIAN / "postinst").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for needle in ("python", "/usr/libexec", "/usr/bin", "lindos-update", "lindos-helper", "lindos-tune", "lindos-core"):
        assert needle not in code, needle


def test_the_only_conffile_is_the_source() -> None:
    assert (DEBIAN / "conffiles").read_text(encoding="utf-8").split() == ["/etc/apt/sources.list.d/lindos.sources"]
    assert SOURCES.is_file()


def _fields(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and ":" in raw:
            key, _, value = raw.partition(":")
            out.setdefault(key.strip().lower(), value.strip())
    return out


def test_the_source_is_a_switched_off_flat_deb822_source() -> None:
    f = _fields(SOURCES)
    assert f["enabled"] == "no"                                       # off until a real key AND server exist
    assert f["types"] == "deb" and f["suites"] == "./"                # flat repository: no Components
    assert "components" not in f
    assert f["signed-by"] == "/usr/share/keyrings/lindos-archive-keyring.gpg"
    assert f["uris"].startswith("https://") and f["uris"].endswith("/")


def test_the_address_is_a_reserved_name_that_can_never_resolve() -> None:
    host = re.sub(r"^https://([^/]+)/.*$", r"\1", _fields(SOURCES)["uris"])
    assert host.endswith(".invalid")                                  # RFC 2606: never a real host, never hijackable


def test_the_repository_address_lives_in_exactly_one_file() -> None:
    """The URL is written once (the conffile), so it can move by a package update; nothing else in the
    package - and no build script - repeats it."""
    address = _fields(SOURCES)["uris"]
    host = re.sub(r"^https://([^/]+)/.*$", r"\1", address)
    holders = []
    for path in (list(ROOT.rglob("*")) + list((REPO / "build").glob("*.sh")) + list((REPO / "build" / "chroot").glob("*.sh"))
                 + list((REPO / "build" / "tools").glob("*.sh"))):
        if path.is_file() and path.suffix != ".gpg":
            if host in path.read_text(encoding="utf-8", errors="replace"):
                holders.append(path.relative_to(REPO).as_posix())
    assert holders == [SOURCES.relative_to(REPO).as_posix()], holders
    assert "packages.lindos.dev" not in (REPO / "build" / "config.env").read_text(encoding="utf-8")


def test_no_placeholder_key_can_ship_enabled() -> None:
    """THE guard: if the committed source is switched on, the committed key must be a real binary keyring with a
    pinned 40-digit fingerprint and the address must not be a reserved name. Fails the moment someone enables the
    source with the placeholder still in place."""
    f = _fields(SOURCES)
    enabled = f.get("enabled", "yes").lower() not in ("no", "false", "0", "off")
    key = KEYRING.read_bytes()
    is_placeholder = key.startswith(b"LINDOS-PLACEHOLDER")
    fpr = FPR_FILE.read_text(encoding="utf-8").strip().upper()
    host = re.sub(r"^https?://([^/]+)/?.*$", r"\1", f["uris"])
    if enabled:
        assert not is_placeholder, "the source is enabled but the keyring is still the placeholder"
        assert key[:1] in (b"\x98", b"\x99", b"\x9a", b"\x9b", b"\xc6"), "the keyring is not a binary OpenPGP keyring"
        assert re.fullmatch(r"[0-9A-F]{40}", fpr), "the source is enabled but no fingerprint is pinned"
        assert not host.endswith((".invalid", ".example")) and host != "packages.lindos.dev"
    else:
        assert is_placeholder or re.fullmatch(r"[0-9A-F]{40}", fpr), "a real keyring needs its pinned fingerprint"


def test_the_placeholder_is_text_that_cannot_be_mistaken_for_a_key() -> None:
    key = KEYRING.read_bytes()
    if key.startswith(b"LINDOS-PLACEHOLDER"):
        assert b"\x00" not in key and key.isascii()
        assert key[:1] not in (b"\x98", b"\x99", b"\x9a", b"\x9b", b"\xc6")
        assert FPR_FILE.read_text(encoding="utf-8").strip() == "PLACEHOLDER"


def test_the_committed_tree_passes_its_own_guard() -> None:
    if BASH is None:
        pytest.skip("bash not available")
    proc = subprocess.run([BASH, str(CHECK), _msys(str(ROOT))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() in ("placeholder", "real")


def test_the_package_ships_nothing_but_its_files() -> None:
    files = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file())
    assert files == ["etc/apt/sources.list.d/lindos.sources", "usr/share/keyrings/lindos-archive-keyring.gpg",
                     "usr/share/lindos/archive-key.fingerprint"]


def test_the_binary_keyring_is_protected_from_line_ending_conversion() -> None:
    attrs = (PKG / ".gitattributes").read_text(encoding="utf-8")
    assert "*.gpg binary" in attrs


def test_maintainer_script_is_posix_sh_and_idempotent_by_design() -> None:
    text = (DEBIAN / "postinst").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and re.search(r"^set -e\b", text, re.M) and "\r" not in text
    assert text.rstrip().endswith("exit 0")


# =================================================================================================
# the guard against a matrix of fixture trees
# =================================================================================================
SHIPPED_KEYRING = "/usr/share/keyrings/lindos-archive-keyring.gpg"


class Tree:
    """A scratch copy of the package root with chosen key / fingerprint / source."""

    def __init__(self, tmp: Path, *, key: Optional[bytes] = PLACEHOLDER_KEY, fpr: Optional[str] = "PLACEHOLDER",
                 enabled: Optional[str] = "no", uri: str = "https://apt.example.test/stable/",
                 signed_by: Optional[str] = SHIPPED_KEYRING, extra: Sequence[str] = (),
                 raw_source: Optional[str] = None) -> None:
        self.root = tmp / "root"
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        (self.root / "usr/share/keyrings").mkdir(parents=True)
        (self.root / "usr/share/lindos").mkdir(parents=True)
        lines = ([] if enabled is None else [f"Enabled: {enabled}"]) + ["Types: deb", f"URIs: {uri}", "Suites: ./"]
        if signed_by is not None:
            lines.append(f"Signed-By: {signed_by}")
        lines.extend(extra)
        text = raw_source if raw_source is not None else "# c\n" + "\n".join(lines) + "\n"
        (self.root / "etc/apt/sources.list.d/lindos.sources").write_text(text, encoding="utf-8")
        if key is not None:
            (self.root / "usr/share/keyrings/lindos-archive-keyring.gpg").write_bytes(key)
        if fpr is not None:
            (self.root / "usr/share/lindos/archive-key.fingerprint").write_text(fpr + "\n", encoding="utf-8")
        self.bin = tmp / "bin"
        self.bin.mkdir()

    def fake_gpg(self, *fingerprints: str, subkey: bool = False) -> None:
        """A gpg whose --show-keys lists one primary key per fingerprint (each with a subkey when asked)."""
        lines = []
        for fingerprint in fingerprints:
            lines += ["pub:-:255:22:AAAA:1::::::scESC:::::", f"fpr:::::::::{fingerprint}:", "uid:-::::1::HASH::Lindos:"]
            if subkey:
                lines += ["sub:-:255:22:BBBB:1::::::s::", "fpr:::::::::1111111111111111111111111111111111111111:"]
        path = self.bin / "gpg"
        path.write_text("#!/bin/bash\n" + "".join(f'echo "{line}"\n' for line in lines), encoding="utf-8", newline="\n")
        os.chmod(path, 0o755)

    def run(self, extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        assert BASH is not None
        env = dict(os.environ)
        env["PATH"] = _msys(str(self.bin)) + os.pathsep + env.get("PATH", "")
        env.pop("LINDOS_GPG", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([BASH, str(CHECK), _msys(str(self.root))], capture_output=True, text=True, env=env)


@needs_bash
def test_placeholder_key_with_a_disabled_source_is_fine_and_reported_as_placeholder(tmp_path: Path) -> None:
    proc = Tree(tmp_path).run()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "placeholder"


@needs_bash
@pytest.mark.parametrize("enabled", ["yes", "true", "1", None])          # None: no Enabled field = apt's default = on
def test_placeholder_key_with_an_enabled_source_is_refused(tmp_path: Path, enabled: Optional[str]) -> None:
    proc = Tree(tmp_path, enabled=enabled).run()
    assert proc.returncode == 1
    assert "REFUSED" in proc.stderr and "placeholder" in proc.stderr
    assert proc.stdout.strip() == ""                                    # nothing is reported as fine


@needs_bash
def test_real_key_pinned_enabled_and_a_real_address_passes_when_gpg_agrees(tmp_path: Path) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes")
    t.fake_gpg(REAL_FPR)
    proc = t.run()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "real"


@needs_bash
def test_a_keyring_that_is_not_the_pinned_key_is_refused(tmp_path: Path) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes")
    t.fake_gpg(OTHER_FPR)
    proc = t.run()
    assert proc.returncode == 1 and "not the pinned one" in proc.stderr


@needs_bash
def test_the_pin_is_compared_case_and_space_insensitively(tmp_path: Path) -> None:
    spaced = " ".join(REAL_FPR[i:i + 4] for i in range(0, 40, 4)).lower()
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=spaced, enabled="yes")
    t.fake_gpg(REAL_FPR)
    assert t.run().returncode == 0


@needs_bash
@pytest.mark.parametrize("fpr", ["PLACEHOLDER", "1234", "not-a-fingerprint", "0123456789ABCDEF0123456789ABCDEF0123456G"])
def test_an_enabled_source_needs_a_real_pinned_fingerprint(tmp_path: Path, fpr: str) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=fpr, enabled="yes")
    t.fake_gpg(REAL_FPR)
    proc = t.run()
    assert proc.returncode == 1 and "pinned fingerprint" in proc.stderr


@needs_bash
@pytest.mark.parametrize("uri", ["https://apt.lindos.invalid/stable/", "https://packages.lindos.dev", "https://x.example/",
                                 "https://example.com/apt", "", "https://EXAMPLE.ORG/x"])
def test_an_enabled_source_may_not_point_at_a_placeholder_name(tmp_path: Path, uri: str) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes", uri=uri)
    t.fake_gpg(REAL_FPR)
    proc = t.run()
    assert proc.returncode == 1 and "placeholder name" in proc.stderr


@needs_bash
def test_a_localhost_address_is_a_real_address_for_the_e2e_job(tmp_path: Path) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes", uri="http://127.0.0.1:8099/stable/")
    t.fake_gpg(REAL_FPR)
    assert t.run().returncode == 0


@needs_bash
def test_a_real_key_without_a_pin_is_refused_even_when_disabled(tmp_path: Path) -> None:
    proc = Tree(tmp_path, key=REAL_KEY_BYTES, fpr="PLACEHOLDER", enabled="no").run()
    assert proc.returncode == 1 and "pinned fingerprint" in proc.stderr


@needs_bash
def test_a_real_key_disabled_with_its_pin_is_fine(tmp_path: Path) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="no")
    t.fake_gpg(REAL_FPR)
    proc = t.run()
    assert proc.returncode == 0 and proc.stdout.strip() == "real"


@needs_bash
@pytest.mark.parametrize("key, why", [
    (b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n", "armored"),
    (b"just some text", "invalid"),
    (None, "missing")])
@pytest.mark.parametrize("enabled", ["no", "yes"])
def test_anything_that_is_not_a_binary_keyring_or_the_placeholder_is_refused(tmp_path: Path, key, why: str, enabled: str) -> None:
    proc = Tree(tmp_path, key=key, fpr=REAL_FPR, enabled=enabled).run()
    assert proc.returncode == 1 and why in proc.stderr


@needs_bash
def test_a_missing_source_file_is_refused(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    (t.root / "etc/apt/sources.list.d/lindos.sources").unlink()
    assert t.run().returncode == 1


# --- what else is in the package root: an exact allow-list (review: another blob could still ship a trusted key) ---
#: files a commit could add next to the three expected ones; each is packed by mkdeb (it packs the whole root/) and would
#: be installed as root on every Lindos system
EXTRA_FILES = [
    "etc/apt/trusted.gpg.d/lindos-extra.gpg",              # trusted for EVERY apt source on the system
    "etc/apt/trusted.gpg.d/x.asc",
    "usr/share/keyrings/another-keyring.gpg",
    "etc/apt/sources.list.d/second.sources",               # a second source
    "etc/apt/sources.list.d/lindos.list",
    "etc/apt/apt.conf.d/99-no-checks",                     # e.g. Acquire::AllowInsecureRepositories
    "etc/apt/preferences.d/pin",
    "usr/share/lindos/archive-key.fingerprint.bak",        # next to an allowed name
    "usr/bin/helper",
    "opt/blob.bin",
    ".hidden",
]


@needs_bash
@pytest.mark.parametrize("extra", EXTRA_FILES)
@pytest.mark.parametrize("state", ["placeholder", "real"])
def test_any_file_that_is_not_on_the_allow_list_is_refused(tmp_path: Path, extra: str, state: str) -> None:
    """The guard used to inspect only the keyring, the source and the fingerprint; everything else in root/ shipped unseen."""
    if state == "real":
        tree = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes", uri="https://apt.example.test/stable/")
        tree.fake_gpg(REAL_FPR)
    else:
        tree = Tree(tmp_path)
    assert tree.run().returncode == 0, "the tree is fine before the extra file is added"
    path = tree.root / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x99\x01\x0dsomething nobody reviewed")
    proc = tree.run()
    assert proc.returncode == 1, (extra, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "", "nothing is reported as fine"
    assert "REFUSED" in proc.stderr and extra in proc.stderr and "allow-list" in proc.stderr, proc.stderr


@needs_bash
def test_every_extra_file_is_named_not_just_the_first(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    for extra in ("etc/apt/trusted.gpg.d/a.gpg", "etc/apt/sources.list.d/b.sources"):
        (tree.root / extra).parent.mkdir(parents=True, exist_ok=True)
        (tree.root / extra).write_text("x\n", encoding="utf-8")
    proc = tree.run()
    assert proc.returncode == 1
    assert "trusted.gpg.d/a.gpg" in proc.stderr and "sources.list.d/b.sources" in proc.stderr


@needs_bash
def test_empty_directories_ship_nothing_and_are_fine(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    (tree.root / "etc/apt/trusted.gpg.d").mkdir(parents=True)
    (tree.root / "usr/share/doc/lindos-archive-keyring").mkdir(parents=True)
    proc = tree.run()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "placeholder"


@needs_bash
def test_a_staging_copy_with_the_committed_debian_files_is_fine_but_not_with_more(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    for name in ("control", "conffiles", "postinst"):
        (tree.root / "DEBIAN").mkdir(exist_ok=True)
        (tree.root / "DEBIAN" / name).write_text("x\n", encoding="utf-8")
    assert tree.run().returncode == 0
    (tree.root / "DEBIAN" / "preinst").write_text("#!/bin/sh\n", encoding="utf-8")
    proc = tree.run()
    assert proc.returncode == 1 and "DEBIAN/preinst" in proc.stderr


@needs_bash
@pytest.mark.parametrize("name", ["etc/apt/sources.list.d/lindos.sources", "usr/share/keyrings/lindos-archive-keyring.gpg",
                                  "usr/share/lindos/archive-key.fingerprint"])
def test_an_allowed_name_that_is_a_symbolic_link_is_refused(tmp_path: Path, name: str) -> None:
    tree = Tree(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_bytes((tree.root / name).read_bytes())
    (tree.root / name).unlink()
    try:
        os.symlink(target, tree.root / name)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symbolic links")
    proc = tree.run()
    assert proc.returncode == 1 and "not a regular file" in proc.stderr and name in proc.stderr, proc.stderr


@needs_bash
def test_a_symbolic_link_to_a_directory_cannot_smuggle_a_tree_in(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.gpg").write_bytes(b"\x99\x01\x0devil")
    (tree.root / "etc/apt").mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, tree.root / "etc/apt/trusted.gpg.d", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symbolic links")
    proc = tree.run()
    assert proc.returncode == 1 and "trusted.gpg.d" in proc.stderr, proc.stderr


@needs_bash
def test_a_trailing_slash_on_the_package_root_changes_nothing(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    (tree.root / "etc/apt/trusted.gpg.d").mkdir(parents=True)
    (tree.root / "etc/apt/trusted.gpg.d/x.gpg").write_bytes(b"\x99\x01\x0dx")
    proc = subprocess.run([BASH, str(CHECK), _msys(str(tree.root)) + "/"], capture_output=True, text=True)
    assert proc.returncode == 1 and "etc/apt/trusted.gpg.d/x.gpg" in proc.stderr
    assert "root//" not in proc.stderr


def test_the_guard_documents_its_allow_list_in_its_header() -> None:
    text = CHECK.read_text(encoding="utf-8")
    assert "allow-list" in text and "ALLOWED_FILES" in text and "trusted.gpg.d" in text


@needs_bash
def test_usage_errors(tmp_path: Path) -> None:
    assert subprocess.run([BASH, str(CHECK)], capture_output=True, text=True).returncode == 2
    assert subprocess.run([BASH, str(CHECK), _msys(str(tmp_path / "nope"))], capture_output=True, text=True).returncode == 2
    assert subprocess.run([BASH, str(CHECK), "a", "b"], capture_output=True, text=True).returncode == 2


@needs_bash
def test_a_real_keyring_starting_with_other_valid_packet_tags_is_recognised(tmp_path: Path) -> None:
    for first in (b"\x98", b"\x9a", b"\x9b", b"\xc6"):
        sub = tmp_path / first.hex()
        sub.mkdir()
        t = Tree(sub, key=first + b"\x01\x02abc", fpr=REAL_FPR, enabled="no")
        t.fake_gpg(REAL_FPR)
        assert t.run().stdout.strip() == "real", first


# --- exactly one primary key, and gpg is required to prove it ------------------------------------------------------
@needs_bash
@pytest.mark.parametrize("enabled", ["no", "yes"])
def test_a_second_key_appended_behind_the_pinned_one_is_refused(tmp_path: Path, enabled: str) -> None:
    """The regression: only the first fingerprint was compared, so a second (attacker) key appended after the pinned
    one passed - and apt trusts every key of a Signed-By keyring."""
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled=enabled)
    t.fake_gpg(REAL_FPR, OTHER_FPR)
    proc = t.run()
    assert proc.returncode == 1 and proc.stdout.strip() == ""
    assert "2 primary keys" in proc.stderr and "exactly one" in proc.stderr


@needs_bash
def test_the_pinned_key_may_have_subkeys_but_never_a_second_primary(tmp_path: Path) -> None:
    ok = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes")
    ok.fake_gpg(REAL_FPR, subkey=True)                                   # the signing subkeys are part of the design
    assert ok.run().returncode == 0
    (tmp_path / "two").mkdir()
    bad = Tree(tmp_path / "two", key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes")
    bad.fake_gpg(OTHER_FPR, REAL_FPR, subkey=True)                       # pinned key second: still two keys, and the first is not it
    assert bad.run().returncode == 1


@needs_bash
def test_a_keyring_gpg_cannot_read_is_refused(tmp_path: Path) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled="yes")
    t.fake_gpg()                                                         # lists nothing
    proc = t.run()
    assert proc.returncode == 1 and "gpg could not read the keyring" in proc.stderr


@needs_bash
@pytest.mark.parametrize("enabled", ["no", "yes"])
def test_a_real_keyring_is_never_shipped_unchecked_when_gpg_is_missing(tmp_path: Path, enabled: str) -> None:
    t = Tree(tmp_path, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled=enabled)
    proc = t.run({"LINDOS_GPG": "no-such-gpg-anywhere"})
    assert proc.returncode == 1 and "gpg is not installed" in proc.stderr and proc.stdout.strip() == ""


@needs_bash
def test_the_placeholder_needs_no_gpg(tmp_path: Path) -> None:
    proc = Tree(tmp_path).run({"LINDOS_GPG": "no-such-gpg-anywhere"})
    assert proc.returncode == 0 and proc.stdout.strip() == "placeholder"


# --- the source file: one stanza, one https address, the shipped keyring, no trust overrides --------------------------
def _good_tree(tmp: Path, **kw) -> Tree:
    t = Tree(tmp, key=REAL_KEY_BYTES, fpr=REAL_FPR, enabled=kw.pop("enabled", "yes"), **kw)
    t.fake_gpg(REAL_FPR)
    return t


@needs_bash
def test_a_second_stanza_is_refused_however_harmless_it_looks(tmp_path: Path) -> None:
    clean = ("Enabled: yes\nTypes: deb\nURIs: https://apt.lindos.example.org/stable/\nSuites: ./\n"
             f"Signed-By: {SHIPPED_KEYRING}\n")
    second = "\nTypes: deb\nURIs: http://evil.test/\nSuites: ./\nTrusted: yes\nAllow-Insecure: yes\n"
    for name, raw in (("plain", clean + second), ("trailing-blank-lines-first", clean + "\n\n\n" + second)):
        sub = tmp_path / name
        sub.mkdir()
        t = _good_tree(sub, uri="https://apt.lindos.test/stable/", raw_source=raw)
        proc = t.run()
        assert proc.returncode == 1 and "2 stanzas" in proc.stderr, name


@needs_bash
def test_comments_and_blank_lines_around_the_stanza_are_not_stanzas(tmp_path: Path) -> None:
    raw = ("# one\n\n# two\nEnabled: no\n# inside\nTypes: deb\nURIs: https://apt.lindos.invalid/stable/\nSuites: ./\n"
           f"Signed-By: {SHIPPED_KEYRING}\n\n\n# trailing\n\n")
    proc = Tree(tmp_path, raw_source=raw).run()
    assert proc.returncode == 0, proc.stderr


@needs_bash
@pytest.mark.parametrize("line", ["Trusted: yes", "trusted: true", "TRUSTED: 1", "Allow-Insecure: yes", "Allow-Weak: yes",
                                  "Allow-Downgrade-To-Insecure: yes", "Trusted: maybe"])
@pytest.mark.parametrize("enabled", ["no", "yes"])
def test_a_trust_override_is_refused(tmp_path: Path, line: str, enabled: str) -> None:
    proc = _good_tree(tmp_path, enabled=enabled, extra=[line], uri="https://apt.lindos.test/stable/").run()
    assert proc.returncode == 1 and "REFUSED" in proc.stderr and line.split(":")[0].lower() in proc.stderr.lower()


@needs_bash
def test_an_explicit_no_for_a_trust_option_is_harmless(tmp_path: Path) -> None:
    t = _good_tree(tmp_path, extra=["Trusted: no", "Allow-Insecure: no"], uri="https://apt.lindos.test/stable/")
    assert t.run().returncode == 0


@needs_bash
@pytest.mark.parametrize("signed_by", [None, "/etc/apt/keyrings/other.gpg", "/usr/share/keyrings/lindos-archive-keyring.gpg.bak",
                                       "/usr/share/keyrings/lindos-archive-keyring.gpg /usr/share/keyrings/evil.gpg"])
def test_signed_by_must_be_exactly_the_shipped_keyring(tmp_path: Path, signed_by: Optional[str]) -> None:
    proc = _good_tree(tmp_path, signed_by=signed_by, uri="https://apt.lindos.test/stable/").run()
    assert proc.returncode == 1 and "Signed-By" in proc.stderr


@needs_bash
def test_an_inline_key_in_signed_by_is_refused(tmp_path: Path) -> None:
    raw = ("Enabled: yes\nTypes: deb\nURIs: https://apt.lindos.test/stable/\nSuites: ./\nSigned-By:\n"
           " -----BEGIN PGP PUBLIC KEY BLOCK-----\n abc\n -----END PGP PUBLIC KEY BLOCK-----\n")
    proc = _good_tree(tmp_path, raw_source=raw).run()
    assert proc.returncode == 1 and "Signed-By" in proc.stderr


@needs_bash
@pytest.mark.parametrize("uri", ["http://apt.lindos.test/stable/", "http://127.0.0.1.evil.test/", "ftp://apt.lindos.test/",
                                 "file:///srv/apt/", "copy:/srv/apt/", "https://apt.lindos.dev@evil.test/stable/",
                                 "https://user:pw@apt.lindos.test/stable/", "http://localhost@evil.test/"])
@pytest.mark.parametrize("enabled", ["no", "yes"])
def test_the_address_must_be_https_or_local_http_without_credentials(tmp_path: Path, uri: str, enabled: str) -> None:
    proc = _good_tree(tmp_path, enabled=enabled, uri=uri).run()
    assert proc.returncode == 1 and "REFUSED" in proc.stderr, proc.stderr


@needs_bash
@pytest.mark.parametrize("uri", ["https://apt.lindos.test/stable/", "http://127.0.0.1:8099/stable/", "http://localhost:8099/stable/",
                                 "http://LOCALHOST/stable/", "http://[::1]:8099/stable/", "https://apt.lindos.test:8443/stable/"])
def test_good_addresses_pass(tmp_path: Path, uri: str) -> None:
    proc = _good_tree(tmp_path, uri=uri).run()
    assert proc.returncode == 0, proc.stderr


@needs_bash
def test_two_addresses_are_refused(tmp_path: Path) -> None:
    proc = _good_tree(tmp_path, uri="https://apt.lindos.test/stable/ https://mirror.lindos.test/stable/").run()
    assert proc.returncode == 1 and "2 addresses" in proc.stderr


@needs_bash
def test_field_names_are_case_insensitive_like_apts_own(tmp_path: Path) -> None:
    raw = ("enabled: YES\ntypes: deb\nuris: https://apt.lindos.test/stable/\nsuites: ./\n"
           f"signed-by: {SHIPPED_KEYRING}\nTRUSTED: yes\n")
    proc = _good_tree(tmp_path, raw_source=raw).run()
    assert proc.returncode == 1 and "trusted" in proc.stderr.lower()


@needs_bash
def test_a_disabled_placeholder_source_with_the_placeholder_key_is_still_the_honest_default(tmp_path: Path) -> None:
    """The committed tree (all of the above must not break what ships today)."""
    proc = subprocess.run([BASH, str(CHECK), _msys(str(ROOT))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# =================================================================================================
# the maintainer script against a scratch root
# =================================================================================================
def _postinst(root: Path, *args: str) -> subprocess.CompletedProcess:
    assert SH is not None
    env = dict(os.environ, DPKG_ROOT=_msys(str(root)))
    return subprocess.run([SH, str(DEBIAN / "postinst"), *args], capture_output=True, text=True, env=env)


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_postinst_drops_the_old_one_line_source_and_its_key(tmp_path: Path) -> None:
    (tmp_path / "etc/apt/sources.list.d").mkdir(parents=True)
    (tmp_path / "etc/apt/keyrings").mkdir(parents=True)
    old = tmp_path / "etc/apt/sources.list.d/lindos.list"
    key = tmp_path / "etc/apt/keyrings/lindos-archive-keyring.gpg"
    old.write_text("deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] https://old.example/ ./\n", encoding="utf-8")
    key.write_bytes(b"old key")
    assert _postinst(tmp_path, "configure", "1.0.0").returncode == 0
    assert not old.exists() and not key.exists()


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_postinst_leaves_a_list_it_does_not_recognise_alone(tmp_path: Path) -> None:
    (tmp_path / "etc/apt/sources.list.d").mkdir(parents=True)
    mine = tmp_path / "etc/apt/sources.list.d/lindos.list"
    mine.write_text("deb https://my.own.mirror/lindos ./\n", encoding="utf-8")
    assert _postinst(tmp_path, "configure").returncode == 0
    assert mine.exists()


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_postinst_with_nothing_to_do_and_on_abort_actions(tmp_path: Path) -> None:
    for action in ("configure", "abort-upgrade", "abort-remove", "abort-deconfigure", "triggered"):
        assert _postinst(tmp_path, action).returncode == 0
