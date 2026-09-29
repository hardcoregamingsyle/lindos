"""Regression guard: nothing Lindos ships may show the user "Linux Mint" by accident.

Lindos is a Linux Mint remaster and says so where it matters (licences, package descriptions, the About
page's "Based on" row).  Everything else the user can see - menu entries, autostarts, Settings text, CLI
output, boot menu, config files - must say Lindos.  This test scans the *repo-owned shipped data*
(packages/*/root, packages/*/DEBIAN, build/overlay) for the words "Mint" / "Linux Mint" / "LinuxMint" and
fails on anything that is not in an explicit, explained allow-list.

What is NOT a finding (so the list stays short and every entry means something):
  * comments (# // <!-- -->) and Python docstrings - they explain the base, users never see them;
  * names of Mint tools and packages: mintinstall, mintupdate, mint-artwork, ... (no word boundary after
    "mint", or "mint-" + package name) and the lowercase identifier "linuxmint" (ID=linuxmint, hostnames of
    apt repositories, /etc/linuxmint/info);
  * the colour "Mint Green".
Anything else needs an ALLOW entry (with the reason) or, while another engineer still owns the fix, a
KNOWN_LEFTOVERS entry.  ALLOW entries must keep matching something, so the list cannot rot.

Runs anywhere: pure stdlib, reads the repository only.
"""
from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("root", "DEBIAN")
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".gz", ".bz2", ".xz", ".zst", ".ttf", ".otf", ".woff", ".woff2", ".deb", ".mo"}

#: a visible "Mint": the word itself (Linux Mint, Mint's ...) or the capitalised identifier LinuxMint
FINDING = re.compile(r"(?i:\bmint\b)|\bLinuxMint\b")
#: ... unless it is a colour or the start of a Debian package name (mint-meta-xfce, mint-artwork, Mint-Y)
NOT_A_FINDING = re.compile(r"(?i)mint[- ]green\b|mint-[a-z0-9]")

# (path glob relative to the repo root, regex searched in the offending text, why it is fine)
ALLOW: List[Tuple[str, str, str]] = [
    ("packages/*/DEBIAN/control",
     r"remaster of Linux Mint|on top of Linux Mint XFCE|stock Mint/Ubuntu kernel|stock Mint 22 XFCE baseline",
     "package descriptions (apt show) say what Lindos is built on - attribution, like THIRD_PARTY.md"),
    ("packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/model.py", r"Linux Mint",
     "About's 'Based on' row states the base system (provenance); built from ID/VERSION_ID, never from the brand fields"),
    ("packages/lindos-tune/root/usr/share/lindos/tune/ram-budget.json", r"Mint",
     "the RAM budget compares Lindos with the stock base image it was measured against"),
    ("packages/lindos-desktop/root/usr/libexec/lindos/rebrand-base.py", r"(?i)mint",
     "the sweep script names the strings it removes and prints an audit of what still says Linux Mint"),
    ("packages/lindos-desktop/root/usr/libexec/lindos/apply-branding.sh", r"Mint sweep",
     "log wording for the build/upgrade-time sweep (root's log, not a user-facing screen)"),
]

# Findings somebody else still has to fix (owner named) - the test tolerates them but they are not hidden.
KNOWN_LEFTOVERS: List[Tuple[str, str, str]] = []


# --------------------------------------------------------------------------- scanning
def _shipped_files() -> Iterator[Path]:
    for pkg in sorted((REPO / "packages").iterdir()):
        for sub in SCAN_ROOTS:
            base = pkg / sub
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix not in SKIP_SUFFIXES:
                        yield p
    overlay = REPO / "build" / "overlay"
    if overlay.is_dir():
        for p in sorted(overlay.rglob("*")):
            if p.is_file():
                yield p


def _read(path: Path) -> Optional[str]:
    raw = path.read_bytes()
    if b"\0" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _python_strings(text: str) -> Iterator[Tuple[int, str]]:
    """String constants that are not docstrings (the strings a program can show)."""
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def _visible_lines(path: Path, text: str) -> Iterator[Tuple[int, str]]:
    first = text.split("\n", 1)[0]
    if path.suffix == ".py" or (first.startswith("#!") and "python" in first):
        try:
            yield from _python_strings(text)
            return
        except SyntaxError:
            pass
    if path.suffix in (".xml", ".svg", ".html", ".ui", ".policy"):
        text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    for no, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith(("#", "//")) and not stripped.startswith("#!"):
            continue
        yield no, line


def _findings() -> List[Tuple[str, int, str]]:
    out: List[Tuple[str, int, str]] = []
    for path in _shipped_files():
        text = _read(path)
        if text is None:
            continue
        rel = path.relative_to(REPO).as_posix()
        for no, chunk in _visible_lines(path, text):
            for m in FINDING.finditer(chunk):
                if NOT_A_FINDING.match(chunk[m.start():m.start() + 24]):
                    continue
                out.append((rel, no, chunk.strip()[:160]))
                break
    return out


def _matches(table: List[Tuple[str, str, str]], rel: str, text: str) -> List[int]:
    return [i for i, (glob, rx, _why) in enumerate(table) if fnmatch.fnmatch(rel, glob) and re.search(rx, text)]


# --------------------------------------------------------------------------- the guard
def test_no_unexplained_mint_in_shipped_data() -> None:
    bad = []
    for rel, no, text in _findings():
        if _matches(ALLOW, rel, text) or _matches(KNOWN_LEFTOVERS, rel, text):
            continue
        bad.append(f"{rel}:{no}: {text}")
    assert not bad, (
        "user-visible Linux Mint text in shipped data - say Lindos, or add an ALLOW entry with the reason:\n  "
        + "\n  ".join(bad))


def test_allow_list_has_no_stale_entries() -> None:
    used = set()
    for rel, _no, text in _findings():
        used.update(_matches(ALLOW, rel, text))
    stale = [ALLOW[i][:2] for i in range(len(ALLOW)) if i not in used]
    assert not stale, "ALLOW entries that match nothing any more (delete them): %r" % stale


def test_every_allow_entry_states_a_reason() -> None:
    for table in (ALLOW, KNOWN_LEFTOVERS):
        for glob, rx, why in table:
            assert glob and rx and len(why) > 20, (glob, rx)
            re.compile(rx)


# --------------------------------------------------------------------------- menu entries are strict
def _desktop_entries() -> Iterator[Tuple[Path, str, str]]:
    for pkg in sorted((REPO / "packages").iterdir()):
        root = pkg / "root"
        if root.is_dir():
            for p in sorted(root.rglob("*.desktop")):
                for line in p.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"^(Name|GenericName|Comment|Keywords|Icon)(\[[^\]]*\])?=(.*)$", line)
                    if m:
                        yield p, m.group(1), m.group(3)


def test_no_shipped_menu_entry_shows_mint_in_its_name_comment_keywords_or_icon() -> None:
    bad = [f"{p.relative_to(REPO).as_posix()}: {k}={v}" for p, k, v in _desktop_entries()
           if re.search(r"mint", v, re.I) and not re.search(r"mint[- ]green", v, re.I)]
    assert not bad, "\n".join(bad)


def test_lindos_store_uses_a_lindos_icon_not_the_mint_one() -> None:
    entry = {}
    for p, k, v in _desktop_entries():
        if p.name == "lindos-store.desktop":
            entry[k] = v
    assert entry["Icon"] == "lindos-store" and entry["Name"] == "Lindos Store"


# --------------------------------------------------------------------------- the boot menu
def test_live_boot_entries_name_lindos_not_mint() -> None:
    for name in ("grub.cfg", "loopback.cfg"):
        text = (REPO / "build" / "overlay" / "boot" / "grub" / name).read_text(encoding="utf-8")
        entries = re.findall(r'^\s*menuentry\s+"([^"]+)"', text, flags=re.M)
        assert entries and all("mint" not in e.lower() for e in entries), (name, entries)
        assert "username=mint" not in text and "hostname=mint" not in text, name


# --------------------------------------------------------------------------- docs must not claim what is untrue
def test_docs_describe_the_current_installer_and_sweep() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "with the Mint installer" not in readme, "the installer is branded by build/chroot/78-installer-brand.sh"
    building = (REPO / "docs" / "BUILDING.md").read_text(encoding="utf-8")
    assert "77-mint-sweep.sh" in building and "Mint sweep" in building
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert "77-mint-sweep.sh" in spec and "Lindos Store" in spec
    assert "Name=Store, Exec=mintinstall" not in spec


@pytest.mark.parametrize("phrase", ["ID=linuxmint", "DISTRIB_ID", "/etc/linuxmint/info"])
def test_the_identity_decision_is_documented(phrase: str) -> None:
    """docs/BUILDING.md must say which Mint identity fields are kept on purpose and why."""
    building = (REPO / "docs" / "BUILDING.md").read_text(encoding="utf-8")
    assert phrase in building
