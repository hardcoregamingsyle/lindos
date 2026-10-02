"""build/chroot/81-unrecognisable-gate.sh: the report of what still makes Lindos recognisable as Linux Mint.

The hook is run for real (bash) against a fake root (LINDOS_MINT_ROOT) with a fake dpkg-query first in PATH (lib.sh
seam LINDOS_HOOK_PATH_PREFIX).  It only reads.  Proven: every check reports what it should and nothing it should
not (hidden entries, allow-listed text, Lindos's own files, interim tools that stay on purpose), the report is
report-only by default and fails the build only with LINDOS_STRICT_UNRECOGNISABLE=1, and the deny list stays in step
with what 76-mint-purge.sh removes.  Needs bash (skipped otherwise).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK_DIR = REPO_ROOT / "build" / "chroot"
HOOK = HOOK_DIR / "81-unrecognisable-gate.sh"
PURGE = HOOK_DIR / "76-mint-purge.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _w(root: Path, rel: str, text: str = "x\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _entry(name: str, exec_: str = "true", extra: str = "") -> str:
    return "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n%s" % (name, exec_, extra)


class Gate:
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.proc = proc
        self.err = proc.stderr
        self.findings = re.findall(r"UNRECOGNISABLE-FINDING \[(\w+)\] (.*)", proc.stderr)
        self.notes = re.findall(r"UNRECOGNISABLE-NOTE \[(\w+)\] (.*)", proc.stderr)

    def cats(self) -> List[str]:
        return [c for c, _ in self.findings]

    def about(self, category: str) -> str:
        return "\n".join(t for c, t in self.findings if c == category)


def run_gate(tmp: Path, root: Path, *, packages: str = "libc6 thunar", strict: bool = False, hook: Path = HOOK) -> Gate:
    assert BASH is not None
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    listing = "".join("installed %s\n" % n for n in packages.split())
    (tmp / "pkgs.txt").write_bytes(listing.encode("utf-8"))
    dq = fake / "dpkg-query"
    dq.write_bytes(('#!/bin/sh\ncat "%s"\n' % _posix(tmp / "pkgs.txt")).encode("utf-8"))
    os.chmod(dq, 0o755)
    config = tmp / "config.env"
    config.write_bytes(b"# empty on purpose\n")
    env = dict(os.environ)
    env.update({"LINDOS_MINT_ROOT": _posix(root), "LINDOS_HOOK_PATH_PREFIX": _posix(fake), "LINDOS_STAGE_DIR": _posix(tmp / "nostage"),
                "LINDOS_CONFIG_ENV": _posix(config)})
    env.pop("LINDOS_STRICT_UNRECOGNISABLE", None)
    if strict:
        env["LINDOS_STRICT_UNRECOGNISABLE"] = "1"
    proc = subprocess.run([BASH, "-Eeuo", "pipefail", _posix(hook)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, check=False, env=env)
    return Gate(proc)


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------- structure
def test_hook_house_style_and_position() -> None:
    raw = HOOK.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw
    t = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in t and "hook_begin" in t and "hook_end" in t
    assert re.search(r'^\. "\$\(dirname "\$\(readlink -f "\$0"\)"\)/lib\.sh"$', t, flags=re.M)
    assert re.search(r"^\s*(sudo|pkexec)\s", t, flags=re.M) is None
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    i = names.index(HOOK.name)
    for h in ("76-mint-purge.sh", "77-mint-sweep.sh", "79-installer-flow.sh", "80-cleanup.sh"):
        assert h in names[:i], f"the gate must see the image after {h}"
    assert names[-1] == HOOK.name, "nothing runs after the gate"


def test_hook_only_reads() -> None:
    body = "\n".join(ln for ln in _text(HOOK).splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in (" rm ", " mv ", "sed -i", "apt-get", "apt-mark", "dpkg-divert", "> \"${", "tee "):
        assert forbidden not in body, forbidden


def test_strict_mode_is_opt_in_and_named_in_the_docs() -> None:
    t = _text(HOOK)
    assert ': "${LINDOS_STRICT_UNRECOGNISABLE:=0}"' in t
    building = _text(REPO_ROOT / "docs" / "BUILDING.md")
    assert "LINDOS_STRICT_UNRECOGNISABLE" in building and "81-unrecognisable-gate" in building


# --------------------------------------------------------------------------- behaviour
@needs_bash
def test_a_clean_image_has_no_findings(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _w(root, "usr/share/applications/lindos-store.desktop", _entry("Lindos Store", "mintinstall"))
    _w(root, "usr/share/applications/firefox.desktop", _entry("Firefox Web Browser", "firefox %u"))
    _w(root, "usr/share/applications/org.xfce.mousepad.desktop", _entry("Text Editor", "mousepad %F"))
    _w(root, "usr/share/applications/mintupdate.desktop", _entry("Lindos Updates", "mintupdate-launcher", "Icon=lindos-update\n"))
    _w(root, "etc/default/grub.d/50_linuxmint.cfg", "GRUB_DISTRIBUTOR=Ubuntu\n")
    _w(root, "etc/default/grub.d/60-lindos-distributor.cfg", 'GRUB_DISTRIBUTOR="Lindos"\n')
    _w(root, "usr/share/linuxmint/adjustments/99-lindos.preserve", "/usr/lib/firefox/distribution/distribution.ini\n")
    before = _snapshot(root)
    gate = run_gate(tmp_path, root, packages="libc6 thunar mintupdate mintinstall mintsystem linuxmint-keyring")
    assert gate.proc.returncode == 0, gate.err
    assert gate.findings == [], gate.err
    assert "UNRECOGNISABLE-GATE findings=0" in gate.err and "mode=report-only" in gate.err
    kinds = {c for c, _ in gate.notes}
    assert {"packages", "grub", "adjust"} <= kinds, gate.notes
    assert _snapshot(root) == before, "the gate never writes"


def _dirty_root(root: Path) -> None:
    _w(root, "usr/share/applications/xed.desktop", _entry("Text Editor", "xed %U", "Icon=xed\n"))
    _w(root, "usr/share/applications/pix.desktop", _entry("Pix", "pix", "NoDisplay=true\n"))                 # hidden: fine
    _w(root, "usr/share/applications/warp.desktop", _entry("Share", "warpinator"))                            # by its command
    _w(root, "usr/share/applications/thunar.desktop", _entry("Thunar File Manager", "thunar %F"))
    _w(root, "usr/share/applications/backup.desktop", _entry("Backup", "true", "Comment=Back up your Linux Mint home\n"))
    _w(root, "usr/share/applications/icon.desktop", _entry("Something", "true", "Icon=mintbackup\n"))
    _w(root, "usr/share/applications/onlykde.desktop", _entry("Kde only", "xed", "OnlyShowIn=KDE;\n"))     # not in XFCE: fine
    _w(root, "usr/share/applications/lindos-terminal.desktop", _entry("Terminal", "xfce4-terminal"))        # Lindos shim: fine
    _w(root, "etc/xdg/autostart/sticky.desktop", _entry("Sticky", "sticky --autostart"))
    _w(root, "etc/xdg/autostart/mintwelcome.desktop", _entry("Welcome", "mintwelcome", "Hidden=true\n"))
    _w(root, "usr/share/themes/Mint-Y/index.theme", "[Desktop Entry]\nName=Mint-Y\n")
    _w(root, "usr/share/themes/Yaru/index.theme", "[Desktop Entry]\nName=Yaru\n")
    _w(root, "usr/share/icons/Mint-Y-Sand/index.theme", "[Icon Theme]\nName=Mint-Y-Sand\n")
    _w(root, "usr/share/icons/Yaru/index.theme", "[Icon Theme]\nName=Yaru\n")
    _w(root, "usr/share/icons/Papirus/index.theme", "[Icon Theme]\nHidden=true\nName=Papirus\n")
    _w(root, "usr/local/bin/apt", "#!/bin/sh\necho This is the Linux Mint apt command\n")
    _w(root, "usr/local/bin/apt.lindos-orig", "#!/bin/sh\n")
    _w(root, "usr/local/bin/lindos-thing", "#!/bin/sh\n")
    _w(root, "usr/local/bin/other-tool", "#!/bin/sh\n")
    _w(root, "usr/bin/rtfm", "#!/bin/sh\n")
    _w(root, "etc/skel/.config/notes.txt", "Welcome to Linux Mint\n")
    _w(root, "etc/skel/.local/share/applications/webapp-OnlineChat4519.desktop", _entry("Matrix", "mintchat", "X-WebApp-URL=https://www.linuxmint.com/matrix.php\n"))
    _w(root, "etc/linuxmint/info", "DESCRIPTION=Linux Mint 22.2\n")                                          # allow-listed
    _w(root, "etc/apt/sources.list.d/mint.list", "deb http://packages.linuxmint.com zara main\n")            # allow-listed
    _w(root, "etc/os-release", 'ID=linuxmint\nNAME="Lindos"\n')                                            # allow-listed
    _w(root, "usr/share/lindos/legal/open-source-notices.txt", "Built on Linux Mint components\n")         # allow-listed
    _w(root, "etc/default/grub.d/50_linuxmint.cfg", "GRUB_DISTRIBUTOR=Ubuntu\n")
    _w(root, "etc/default/grub.d/49-lindos-distributor.cfg", 'GRUB_DISTRIBUTOR="Lindos"\n')
    (root / "usr/share/linuxmint/adjustments").mkdir(parents=True)


@needs_bash
def test_every_check_reports_what_it_should_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _dirty_root(root)
    gate = run_gate(tmp_path, root, packages="libc6 thunar mint-artwork xed mintupdate mintbackup")
    assert gate.proc.returncode == 0, "report-only by default: " + gate.err
    text = "\n".join("%s: %s" % f for f in gate.findings)
    # packages
    assert "installed: mint-artwork" in text and "installed: xed" in text and "installed: mintbackup" in text
    assert "installed: mintupdate" not in text and "installed: thunar" not in text
    # menu entries
    d = gate.about("desktop")
    for hit in ("/usr/share/applications/xed.desktop", "/usr/share/applications/warp.desktop",
                "/usr/share/applications/thunar.desktop", "/usr/share/applications/backup.desktop",
                "/usr/share/applications/icon.desktop", "/etc/xdg/autostart/sticky.desktop",
                "/etc/skel/.local/share/applications/webapp-OnlineChat4519.desktop"):
        assert hit in d, (hit, d)
    for fine in ("pix.desktop", "onlykde.desktop", "lindos-terminal.desktop", "mintwelcome.desktop"):
        assert fine not in d, fine
    # themes
    th = gate.about("themes")
    assert "/usr/share/themes/Mint-Y " in th + " " and "/usr/share/icons/Mint-Y-Sand" in th and "/usr/share/icons/Yaru" in th
    assert "Papirus" not in th and "/usr/share/themes/Yaru" not in th
    assert any("Papirus" in t and "hidden" in t for c, t in gate.notes if c == "themes")
    assert any("/usr/share/themes/Yaru" in t for c, t in gate.notes if c == "themes")
    # wrappers, text, grub, adjust, skel
    w = gate.about("wrapper")
    assert "/usr/local/bin/apt " in w + " " and "/usr/bin/rtfm" in w and "lindos-thing" not in w and "apt.lindos-orig" not in w
    assert any("other-tool" in t for c, t in gate.notes if c == "wrapper")
    tx = gate.about("text")
    assert "/etc/skel/.config/notes.txt" in tx
    for allowed in ("/etc/linuxmint/info", "/etc/apt/sources.list.d/mint.list", "/etc/os-release", "/usr/share/lindos/legal"):
        assert allowed not in tx, allowed
    g = gate.about("grub")
    assert "49-lindos-distributor.cfg sorts before 50_linuxmint.cfg" in g and "obsolete 49-lindos-distributor.cfg" in g
    assert "99-lindos.preserve" in gate.about("adjust")
    assert "Matrix web app" in gate.about("skel")
    assert "UNRECOGNISABLE-GATE findings=%d" % len(gate.findings) in gate.err


@needs_bash
def test_strict_mode_fails_the_build_only_when_asked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _dirty_root(root)
    report_only = run_gate(tmp_path, root, packages="libc6 xed")
    assert report_only.proc.returncode == 0 and report_only.findings
    strict_dir = tmp_path / "strict"
    strict_dir.mkdir()
    strict = run_gate(strict_dir, root, packages="libc6 xed", strict=True)
    assert strict.proc.returncode != 0
    assert "mode=strict" in strict.err and "LINDOS_STRICT_UNRECOGNISABLE=1" in strict.err
    clean_dir = tmp_path / "cleanrun"
    clean_dir.mkdir()
    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    assert run_gate(clean_dir, clean_root, strict=True).proc.returncode == 0, "strict with nothing to report passes"


@needs_bash
def test_a_grub_drop_in_that_sorts_after_mints_is_fine(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _w(root, "etc/default/grub.d/50_linuxmint.cfg", "GRUB_DISTRIBUTOR=Ubuntu\n")
    _w(root, "etc/default/grub.d/60-lindos-distributor.cfg", 'GRUB_DISTRIBUTOR="Lindos"\n')
    gate = run_gate(tmp_path, root)
    assert gate.findings == [] and any("sorts after" in t for c, t in gate.notes if c == "grub")
    (root / "etc/default/grub.d/60-lindos-distributor.cfg").unlink()
    again = tmp_path / "again"
    again.mkdir()
    assert "grub" in run_gate(again, root).cats(), "no Lindos drop-in at all leaves the base's title"


@needs_bash
def test_a_symlink_from_xdg_into_mint_artwork_is_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "usr" / "share" / "mint-artwork" / "xfce"
    target.mkdir(parents=True)
    (root / "etc" / "xdg").mkdir(parents=True)
    try:
        os.symlink(str(target), str(root / "etc" / "xdg" / "xdg-xfce"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    _w(root, "etc/xdg/xdg-other/xfce4/panel/x.rc", "x\n")
    gate = run_gate(tmp_path, root)
    assert "/etc/xdg/xdg-xfce resolves into mint-artwork" in gate.about("xdg")
    assert any("xdg-other" in t and "outranks" in t for c, t in gate.notes if c == "xdg")


@needs_bash
def test_an_empty_root_and_missing_dirs_are_fine(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    gate = run_gate(tmp_path, root, packages="")
    assert gate.proc.returncode == 0 and gate.findings == [], gate.err


# --------------------------------------------------------------------------- the deny list follows the purge
def _purge_groups() -> List[List[str]]:
    lines = _text(PURGE).replace("\\\n", " ").splitlines()
    groups = []
    for ln in lines:
        m = re.match(r"^\s*purge_group\s+(.*)$", ln)
        if m and not ln.lstrip().startswith("purge_group()"):
            groups.append(shlex.split(m.group(1)))
    return groups


def _gate_regex(var: str) -> "re.Pattern[str]":
    m = re.search(r"^%s='(.+)'$" % var, _text(HOOK), flags=re.M)
    assert m, var
    return re.compile(m.group(1))


def test_everything_76_purges_is_on_the_gate_deny_list_and_nothing_it_keeps_is() -> None:
    deny, interim = _gate_regex("DENY_PKG_RE"), _gate_regex("INTERIM_PKG_RE")
    groups = _purge_groups()
    assert len(groups) >= 15
    for tokens in groups:
        for tok in tokens[2:]:
            sample = tok.replace("*", "x") if "*" in tok else tok
            assert deny.match(sample), f"{tok} (group {tokens[0]}) is purged by 76 but not on the gate's deny list"
    keep = re.search(r"KEEP_SET=\((.*?)\n\)", _text(PURGE), flags=re.S)
    assert keep
    kept = [t for t in re.sub(r"#.*", "", keep.group(1)).split()]
    for name in kept:
        assert not deny.match(name), f"{name} is in the keep-set but the gate calls it a finding"
    for name in ("mintupdate", "mintinstall", "mintdrivers", "mintsources", "mintreport", "mintlocale-im", "mint-meta-codecs"):
        assert interim.match(name), name
        assert not deny.match(name), name


def test_the_deny_list_of_menu_entries_covers_the_apps_the_sweep_hides() -> None:
    import json
    rules = json.loads(_text(REPO_ROOT / "packages/lindos-desktop/root/usr/share/lindos/branding/base-sweep.json"))
    deny = _gate_regex("DENY_DESKTOP_RE")
    import fnmatch
    for name in ("xed", "xviewer", "xreader", "pix", "io.github.celluloid_player.Celluloid", "warpinator", "org.x.sticky", "thingy",
                 "thunar", "xfce4-terminal", "mintbackup", "mintwelcome"):
        assert deny.match(name.lower()) or re.match(deny.pattern, name, re.I), name
    hidden_globs = [g.lower() for g in rules["applications"]["hide_globs"]]
    for name in ("xed.desktop", "xviewer.desktop", "xreader.desktop", "pix.desktop", "thunar.desktop", "xfce4-terminal.desktop",
                 "warpinator.desktop", "org.x.sticky.desktop", "mintbackup.desktop", "mintstick.desktop"):
        assert any(fnmatch.fnmatchcase(name.lower(), g) for g in hidden_globs), f"{name} is on the gate deny list but the sweep does not hide it"
