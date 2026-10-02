#!/usr/bin/env python3
"""build/qa/menu_checks.py - structural checks of the BOOT MENUS of a built Lindos ISO (xorriso only: no QEMU, no root).

Neither CI job boots the ISO through its own boot loader: build/qa/boot_test.py and build/qa/install_test.py hand
QEMU the kernel and initrd DIRECTLY, with words copied from grub.cfg, so a broken menu would ship green and show up
only on real hardware.  build-iso.sh guards the tree it builds (boot=casper, no placeholder, only-ubiquity on the
first linux line, no username=mint, kernel and initrd exist), but nothing looks at the FINISHED ISO.  This module
does, in seconds, with what xorriso can read:

  * /boot/grub/grub.cfg and /boot/grub/loopback.cfg (UEFI, loop-booted ISOs): the entries, the default (0 = the
    installer, a timeout), every entry boots casper as liveuser@lindos with the OEM answers (oem-config/enable and the
    finalize.sh success_command), only the two Install entries carry only-ubiquity, kernel and initrd exist on the
    medium, no unfilled @PLACEHOLDER@, grub.cfg and loopback.cfg agree, and the SCRIPT parses (``grub-script-check``
    when the tool is installed, else a built-in check of braces and if/fi, for/done);
  * /isolinux/*.cfg (BIOS): the casper labels are exactly what build/lib/boot_menu.py generates from the ISO's own
    grub.cfg (labels, titles, kernel, initrd, words - so "rewritten wrongly" cannot pass), one ``menu default`` on the
    install label, none of the base labels (live, compat, oem) left, no default/ontimeout naming a label that does not
    exist (that drops the user to a bare ``boot:`` prompt), every ``include`` and kernel/initrd path exists;
  * the boot catalogue (``xorriso -report_el_torito plain``): a BIOS and an UEFI boot image.

What this CANNOT show: that a firmware really loads these menus and boots the default entry - build/qa/menu_test.py
tries that under SeaBIOS/OVMF; that the appended EFI partition's own GRUB (a signed binary replayed from the base
ISO) finds /boot/grub/grub.cfg; how the menus LOOK.

Usage::

    python3 build/qa/menu_checks.py --iso 'out/lindos-*.iso' [--work-dir out/menu-checks]

Exit codes: 0 pass - 1 a check failed - 2 usage / environment error.  Standard library only (plus xorriso and,
optionally, grub-script-check on the machine that runs it).
"""

from __future__ import annotations

import argparse
import glob
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import boot_menu  # noqa: E402  (build/lib: the single source of truth of both boot menus)
import install_checks as ic  # noqa: E402  (Finding, ok/info/warn/fail)

SUCCESS_COMMAND = "ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh"
LABELS_EXPECTED: Tuple[str, ...] = ("install", "install-compat", "try", "try-compat", "check")
BASE_CASPER_LABELS: Tuple[str, ...] = ("live", "compat", "oem")     # the base ISO's own casper labels, replaced by the rewrite
GRUB_MENUS: Tuple[str, ...] = ("boot/grub/grub.cfg", "boot/grub/loopback.cfg")
LISTED_DIRS: Tuple[str, ...] = ("/casper", "/isolinux", "/boot/grub", "/EFI", "/.disk")
_PLACEHOLDER = re.compile(r"@[A-Z_][A-Z_]*@")
_MODULE_OR_FILE = re.compile(r"\.(c32|0|bin|img|com|bss)$", re.I)


def _code_lines(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


# ============================================================================================
#  GRUB scripts: a built-in structure check (when grub-script-check is not installed)
# ============================================================================================
def script_structure_errors(text: str) -> List[str]:
    """Unbalanced braces, if/fi, for|while/done and unterminated quotes of a GRUB script - what grub-script-check
    reports first.  Deliberately small: quotes and comments are skipped, only whole words count (``${iso_path}`` is
    a variable, not a block), and only the first word of a statement is a keyword."""
    errors: List[str] = []
    braces: List[int] = []
    ifs: List[int] = []
    loops: List[int] = []
    quote = ""
    quote_line = 0
    for no, raw in enumerate(text.splitlines(), 1):
        buf: List[str] = []
        for ch in raw:
            if quote:
                if ch == quote:
                    quote = ""
                continue
            if ch in "\"'":
                quote, quote_line = ch, no
                buf.append("x")            # a quoted string is one word
                continue
            if ch == "#" and (not buf or buf[-1] in " \t;"):
                break
            buf.append(ch)
        line = "".join(buf)
        for statement in re.split(r";|&&|\|\|", line):
            words = statement.split()
            if not words:
                continue
            first = words[0]
            if first == "if":
                ifs.append(no)
            elif first == "fi":
                if not ifs:
                    errors.append("line %d: 'fi' without 'if'" % no)
                else:
                    ifs.pop()
            elif first in ("for", "while", "until"):
                loops.append(no)
            elif first == "done":
                if not loops:
                    errors.append("line %d: 'done' without a loop" % no)
                else:
                    loops.pop()
            for word in words:
                if word == "{" or (word.endswith("{") and word not in ("${",) and not word.endswith("${")):
                    braces.append(no)
                elif word == "}":
                    if not braces:
                        errors.append("line %d: '}' without '{'" % no)
                    else:
                        braces.pop()
    if quote:
        errors.append("line %d: unterminated %s quote" % (quote_line, "double" if quote == '"' else "single"))
    errors.extend("line %d: '{' is never closed" % n for n in braces)
    errors.extend("line %d: 'if' has no 'fi'" % n for n in ifs)
    errors.extend("line %d: loop has no 'done'" % n for n in loops)
    return errors


# ============================================================================================
#  grub.cfg / loopback.cfg
# ============================================================================================
def check_grub(name: str, text: str, exists: Callable[[str], bool], *, is_main: bool) -> List[ic.Finding]:
    """One GRUB menu file of the ISO (*is_main*: grub.cfg, which also owns ``set default`` and ``set timeout``)."""
    out: List[ic.Finding] = []
    tag = "menu-" + name.rsplit("/", 1)[-1].replace(".cfg", "")
    entries = boot_menu.parse_grub_entries(text)
    if not entries:
        return [ic.fail(tag, "%s has no menuentry with a linux line" % name)]
    left = sorted({m for ln in _code_lines(text) for m in _PLACEHOLDER.findall(ln)})
    if left:
        out.append(ic.fail(tag + "-placeholder", "%s still contains an unfilled placeholder: %s" % (name, " ".join(left))))
    # the entries
    linux_lines = re.findall(r"^\s*linux(?:efi)?\s.*$", text, re.M)
    if len(linux_lines) != len(entries) or not all(re.search(r"\s--\s*$", ln) for ln in linux_lines):
        out.append(ic.fail(tag + "-terminator", "every linux line of %s must end with ' --' (the words after it belong to init)" % name))
    problems: List[str] = []
    for e in entries:
        words = set(e.args)
        if "boot=casper" not in words:
            problems.append("%r has no boot=casper" % e.title)
        if "username=liveuser" not in words or "hostname=lindos" not in words:
            problems.append("%r does not boot as liveuser@lindos" % e.title)
        if any(w in ("username=mint", "hostname=mint") for w in words):
            problems.append("%r still says username=mint/hostname=mint" % e.title)
        if "oem-config/enable=true" not in words or SUCCESS_COMMAND not in words:
            problems.append("%r lacks oem-config/enable=true or the finalize.sh success_command (the OEM flow would not arm)" % e.title)
        if e.is_installer and ({"maybe-ubiquity", "automatic-ubiquity", "noninteractive"} & words):
            problems.append("%r uses an unattended installer mode" % e.title)
        for rel in (e.kernel, e.initrd):
            if not exists(rel):
                problems.append("%r boots %s which is not on the medium" % (e.title, rel))
        if re.search(r"(?i)\bmint\b", e.title):
            problems.append("the title %r still names the old brand" % e.title)
    if problems:
        out.append(ic.fail(tag + "-entries", "; ".join(problems[:8])))
    else:
        out.append(ic.ok(tag + "-entries", "%d entries, every one boots casper as liveuser@lindos with the OEM answers; kernel and "
                                           "initrd exist" % len(entries)))
    # the install entries and the default
    installers = [e for e in entries if e.is_installer]
    tries = [e for e in entries if not e.is_installer and "integrity-check" not in e.args]
    if not entries[0].is_installer or not entries[0].title.startswith("Install Lindos"):
        out.append(ic.fail(tag + "-install-first", "the first entry of %s is %r, not the installer: the default boot would not "
                                                   "install" % (name, entries[0].title)))
    elif len(installers) != 2 or "nomodeset" not in installers[1].args or "nomodeset" in installers[0].args:
        out.append(ic.fail(tag + "-install-entries", "expected the Install entry and its compatibility (nomodeset) variant, found: "
                           + "; ".join(e.title for e in installers)))
    else:
        out.append(ic.ok(tag + "-install-entries", "Install Lindos (+ compatibility mode) first, with only-ubiquity"))
    if not tries:
        out.append(ic.warn(tag + "-try", "%s offers no 'Try Lindos' entry" % name))
    if is_main:
        default = re.search(r'^\s*set\s+default\s*=\s*"?(\S+?)"?\s*$', text, re.M)
        timeout = re.search(r'^\s*set\s+timeout\s*=\s*"?(-?\d+)"?\s*$', text, re.M)
        if not default or default.group(1) != "0":
            out.append(ic.fail(tag + "-default", "grub.cfg does not select entry 0 (the installer) by default: set default=%s"
                               % (default.group(1) if default else "missing")))
        else:
            out.append(ic.ok(tag + "-default", "set default=0"))
        if not timeout or int(timeout.group(1)) <= 0:
            out.append(ic.warn(tag + "-timeout", "grub.cfg has no positive 'set timeout' (%s): the menu would boot at once or "
                                                 "wait for ever" % (timeout.group(1) if timeout else "missing")))
    # the script itself
    errors = script_structure_errors(text)
    if errors:
        out.append(ic.fail(tag + "-syntax", "%s does not parse as a GRUB script: %s" % (name, "; ".join(errors[:5]))))
    else:
        out.append(ic.ok(tag + "-syntax", "%s: braces and if/fi balance" % name))
    return out


def compare_grub_menus(grub_text: str, loopback_text: str) -> List[ic.Finding]:
    """loopback.cfg (Ventoy, grml, loop-booted ISOs) offers exactly grub.cfg's entries."""
    a = [(e.title, e.kernel, e.initrd, e.args) for e in boot_menu.parse_grub_entries(grub_text)]
    b = [(e.title, e.kernel, e.initrd, e.args) for e in boot_menu.parse_grub_entries(loopback_text)]
    if a == b:
        return [ic.ok("menu-grub-loopback", "loopback.cfg offers the same %d entries as grub.cfg" % len(a))]
    diff = [x[0] for x in a if x not in b] + [x[0] for x in b if x not in a]
    return [ic.fail("menu-grub-loopback", "grub.cfg and loopback.cfg differ (%s): a loop-booted ISO would install differently"
                    % ", ".join(sorted(set(diff))[:4] or ["order"]))]


# ============================================================================================
#  ISOLINUX (BIOS)
# ============================================================================================
class Label(NamedTuple):
    name: str
    title: str
    default: bool
    kernel: str
    append: str


class Syslinux(NamedTuple):
    top: Dict[str, str]           # default / ontimeout / onerror / ui / timeout (lower-case keys)
    includes: List[str]
    labels: List[Label]


def parse_syslinux(text: str) -> Syslinux:
    """The parts of an ISOLINUX/SYSLINUX config the checks need (labels with their menu title, kernel and append)."""
    top: Dict[str, str] = {}
    includes: List[str] = []
    labels: List[Label] = []
    cur: Optional[Dict[str, object]] = None

    def flush() -> None:
        if cur is not None:
            labels.append(Label(str(cur["name"]), str(cur["title"]), bool(cur["default"]), str(cur["kernel"]), str(cur["append"])))

    in_help = False
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower()
        if in_help:
            in_help = low != "endtext"
            continue
        if not line or line.startswith("#"):
            continue
        if low.startswith("text help") or low == "text":
            in_help = True
            continue
        m = re.match(r"(?i)^label\s+(\S+)", line)
        if m:
            flush()
            cur = {"name": m.group(1), "title": "", "default": False, "kernel": "", "append": ""}
            continue
        key, _, rest = line.partition(" ")
        key, rest = key.lower(), rest.strip()
        if cur is None:
            if key == "include":
                includes.append(rest)
            elif key in ("default", "ontimeout", "onerror", "ui", "timeout", "prompt"):
                top[key] = rest
            continue
        if key == "menu":
            sub, _, value = rest.partition(" ")
            if sub.lower() == "label":
                cur["title"] = value.strip()
            elif sub.lower() == "default":
                cur["default"] = True
        elif key in ("kernel", "linux", "com32", "localboot", "chain"):
            if key in ("kernel", "linux", "com32"):
                cur["kernel"] = rest.split()[0] if rest.split() else ""
        elif key == "append":
            cur["append"] = rest
    flush()
    return Syslinux(top, includes, labels)


def _append_parts(append: str) -> Tuple[str, List[str]]:
    """('/casper/initrd.lz', [words...]) of an ISOLINUX append line: the initrd= word apart, the trailing '--' dropped."""
    initrd = ""
    words: List[str] = []
    for w in append.split():
        if w.lower().startswith("initrd="):
            initrd = w.split("=", 1)[1]
        else:
            words.append(w)
    if words and words[-1] == "--":
        words = words[:-1]
    return initrd, words


def _is_casper(label: Label) -> bool:
    return "boot=casper" in label.append or "/casper/vmlinuz" in label.kernel


def check_isolinux(cfgs: Mapping[str, str], grub_text: str, exists: Callable[[str], bool]) -> List[ic.Finding]:
    """The BIOS menu of the ISO: *cfgs* = {'live.cfg': text, ...} of /isolinux; *grub_text* = the ISO's grub.cfg."""
    out: List[ic.Finding] = []
    parsed = {name: parse_syslinux(text) for name, text in cfgs.items()}
    labels: List[Tuple[str, Label]] = [(n, lb) for n, p in parsed.items() for lb in p.labels]
    casper = [(n, lb) for n, lb in labels if _is_casper(lb)]
    if not casper:
        return [ic.fail("menu-isolinux", "no casper label in /isolinux/*.cfg: the BIOS menu cannot boot Lindos")]
    left = sorted({m for n, t in cfgs.items() for ln in _code_lines(t) for m in _PLACEHOLDER.findall(ln)})
    if left:
        out.append(ic.fail("menu-isolinux-placeholder", "an unfilled placeholder in /isolinux: " + " ".join(left)))
    # 1. exactly what boot_menu.py generates from this ISO's own grub.cfg
    entries = boot_menu.parse_grub_entries(grub_text)
    expected = parse_syslinux(boot_menu.render_labels(entries)).labels if entries else []
    got = [lb for _n, lb in casper]
    if [(lb.name, lb.title, lb.kernel) for lb in got] != [(lb.name, lb.title, lb.kernel) for lb in expected]:
        out.append(ic.fail("menu-isolinux-labels", "the casper labels of /isolinux are %s, but grub.cfg's entries generate %s: the BIOS "
                           "menu was not rewritten from them (boot_menu.py did not run, or its result was changed afterwards)"
                           % (", ".join("%s (%s)" % (lb.name, lb.title) for lb in got),
                              ", ".join("%s (%s)" % (lb.name, lb.title) for lb in expected))))
    else:
        bad = []
        for have, want in zip(got, expected):
            if _append_parts(have.append) != _append_parts(want.append):
                bad.append(have.name)
        if bad:
            out.append(ic.fail("menu-isolinux-words", "the kernel words or initrd of label(s) %s differ from the GRUB entries they were "
                                                      "generated from" % ", ".join(bad)))
        else:
            out.append(ic.ok("menu-isolinux-labels", "the BIOS menu = the GRUB entries: %s" % ", ".join(lb.name for lb in got)))
    names = [lb.name for _n, lb in casper]
    old = [n for n in names if n.lower() in BASE_CASPER_LABELS]
    if old:
        out.append(ic.fail("menu-isolinux-base", "the base ISO's own casper label(s) are still offered: " + ", ".join(old)))
    for label in got:
        if re.search(r"username=mint|hostname=mint", label.append):
            out.append(ic.fail("menu-isolinux-user", "label %s still says username=mint/hostname=mint" % label.name))
    # 2. the default
    defaults = [lb.name for _n, lb in labels if lb.default]
    if defaults != ["install"]:
        out.append(ic.fail("menu-isolinux-default", "'menu default' is on %s, expected exactly the 'install' label"
                           % (", ".join(defaults) or "no label")))
    else:
        out.append(ic.ok("menu-isolinux-default", "the install label is the default"))
    # 3. no dangling default/ontimeout/onerror (a name that is not a label drops the user to a bare 'boot:' prompt)
    defined = {lb.name.lower() for _n, lb in labels}
    dangling = []
    for cfg, p in parsed.items():
        for key in ("default", "ontimeout"):
            value = p.top.get(key, "").split(" ")[0]
            if value and value.lower() not in defined and not _MODULE_OR_FILE.search(value):
                dangling.append("%s: %s %s" % (cfg, key, value))
    if dangling:
        out.append(ic.fail("menu-isolinux-dangling", "names no label (a bare 'boot:' prompt): " + "; ".join(dangling)))
    # 4. files
    missing: List[str] = []
    for cfg, p in parsed.items():
        for inc in p.includes:
            if not exists(inc if inc.startswith("/") else "/isolinux/" + inc):       # SYSLINUX: relative to the config's directory
                missing.append("%s includes %s" % (cfg, inc))
    for _n, lb in labels:
        initrd, _words = _append_parts(lb.append)
        for path in (lb.kernel, initrd):
            if path and path.startswith("/") and not exists(path):
                if _is_casper(lb):
                    missing.append("label %s boots %s" % (lb.name, path))
    if missing:
        out.append(ic.fail("menu-isolinux-files", "not on the medium: " + "; ".join(missing[:8])))
    else:
        out.append(ic.ok("menu-isolinux-files", "every include, kernel and initrd of the casper labels exists"))
    for _n, lb in labels:                    # a leftover brand in the visible text
        if re.search(r"(?i)\bmint\b", lb.title):
            out.append(ic.warn("menu-isolinux-brand", "label %s is titled %r" % (lb.name, lb.title)))
    return out


# ============================================================================================
#  the boot catalogue
# ============================================================================================
_EL_TORITO_IMG = re.compile(r"^El Torito boot img\s*:\s*\d+\s+(BIOS|UEFI|PPC|Mac)\b", re.M)


def check_boot_catalog(report: str) -> List[ic.Finding]:
    """``xorriso -indev ISO -report_el_torito plain``: a BIOS and an UEFI boot image are registered.

    The report format is xorriso's; when it shows no image lines at all the result is a warning (the format may
    differ), never a failure - only a report that lists images and lacks one of the two platforms fails."""
    platforms = _EL_TORITO_IMG.findall(report or "")
    if not platforms:
        return [ic.warn("menu-el-torito", "the boot catalogue could not be read from xorriso's report (no 'El Torito boot img' line)")]
    got = sorted(set(platforms))
    out: List[ic.Finding] = []
    for want, why in (("BIOS", "SeaBIOS/legacy machines would not boot the medium"), ("UEFI", "UEFI machines would not boot the medium")):
        if want not in got:
            out.append(ic.fail("menu-el-torito-" + want.lower(), "no %s boot image in the El Torito catalogue: %s" % (want, why)))
    if not out:
        out.append(ic.ok("menu-el-torito", "boot images: " + ", ".join(got)))
    return out


# ============================================================================================
#  the pure entry point: everything read from the ISO is passed in
# ============================================================================================
def check_menus(files: Mapping[str, str], listing: Set[str], *, grub_script_check: Optional[Mapping[str, Tuple[int, str]]] = None,
                el_torito: Optional[str] = None) -> List[ic.Finding]:
    """The structural checks of the boot menus.

    *files*: relative path ('boot/grub/grub.cfg', 'isolinux/live.cfg', ...) -> text, for every menu file that was on the
    ISO.  *listing*: every file path of the ISO ('/casper/vmlinuz', ...).  *grub_script_check*: {file: (rc, output)} of
    ``grub-script-check`` when it ran.  *el_torito*: xorriso's boot catalogue report (None = not read).
    """
    def exists(path: str) -> bool:
        return "/" + path.lstrip("/") in listing

    out: List[ic.Finding] = []
    grub = files.get("boot/grub/grub.cfg")
    if grub is None:
        out.append(ic.fail("menu-grub", "/boot/grub/grub.cfg is not on the ISO: UEFI machines have no menu"))
    else:
        out.extend(check_grub("boot/grub/grub.cfg", grub, exists, is_main=True))
    loop = files.get("boot/grub/loopback.cfg")
    if loop is None:
        out.append(ic.warn("menu-grub-loopback", "/boot/grub/loopback.cfg is not on the ISO: loop-booting it (Ventoy, grml) offers no Lindos menu"))
    else:
        out.extend(check_grub("boot/grub/loopback.cfg", loop, exists, is_main=False))
        if grub is not None:
            out.extend(compare_grub_menus(grub, loop))
    for name, (rc, text) in sorted((grub_script_check or {}).items()):
        tag = "menu-" + name.rsplit("/", 1)[-1].replace(".cfg", "") + "-script-check"
        if rc == 0:
            out.append(ic.ok(tag, "grub-script-check accepts /%s" % name))
        else:
            out.append(ic.fail(tag, "grub-script-check rejects /%s: %s" % (name, " ".join(text.split())[:300])))
    if grub_script_check is None:
        out.append(ic.info("menu-script-check", "grub-script-check is not installed: only the built-in structure check ran "
                                                "(apt-get install grub-common)"))
    iso_cfgs = {name.split("/", 1)[1]: text for name, text in files.items() if name.startswith("isolinux/") and name.endswith(".cfg")}
    if not iso_cfgs and not any(p.startswith("/isolinux/") for p in listing):
        out.append(ic.info("menu-isolinux", "the ISO has no /isolinux: no BIOS menu to check (UEFI only)"))
    elif not iso_cfgs:
        out.append(ic.fail("menu-isolinux", "/isolinux exists but none of its .cfg files could be read"))
    elif grub is not None:              # (without grub.cfg there is nothing to compare with; that is a failure above)
        out.extend(check_isolinux(iso_cfgs, grub, exists))
    if el_torito is not None:
        out.extend(check_boot_catalog(el_torito))
    return out


# ============================================================================================
#  reading the ISO (xorriso) and the command line
# ============================================================================================
def iso_listing(iso: Path, *, which=shutil.which, run=subprocess.run) -> Optional[Set[str]]:
    """Every regular file below the boot directories of the ISO ('/casper/vmlinuz', ...), or None when xorriso lists none.

    One xorriso run PER directory, in the command shape install_test.py's iso_find uses: ``-find`` takes every following
    word as one of its own tests until '--' or the end of the arguments, so a second ``-find`` in the same run is an
    unknown test and xorriso lists nothing at all (the first real install test: 'xorriso could not list the ISO's boot
    files').  A directory the ISO does not have (Mint 22 has no /isolinux) is an error of its own run and only leaves it
    empty.  The paths come back single-quoted, one per line."""
    xorriso = which("xorriso")
    if not xorriso:
        return None
    files: Set[str] = set()
    for top in LISTED_DIRS:
        proc = run([xorriso, "-indev", str(iso), "-find", top, "-type", "f"], capture_output=True, text=True)
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if len(line) >= 2 and line[0] == "'" and line[-1] == "'":
                line = line[1:-1]
            if line.startswith("/") and not line.startswith("/dev/"):
                files.add(line)
    return files or None


def iso_extract_text(iso: Path, iso_path: str, dest: Path, *, which=shutil.which, run=subprocess.run) -> Optional[str]:
    xorriso = which("xorriso")
    if not xorriso:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    proc = run([xorriso, "-osirrox", "on", "-indev", str(iso), "-extract", iso_path, str(dest)], capture_output=True, text=True)
    if getattr(proc, "returncode", 1) != 0 or not dest.exists():
        return None
    return dest.read_text(encoding="utf-8", errors="replace")


def iso_el_torito(iso: Path, *, which=shutil.which, run=subprocess.run) -> Optional[str]:
    xorriso = which("xorriso")
    if not xorriso:
        return None
    proc = run([xorriso, "-indev", str(iso), "-report_el_torito", "plain"], capture_output=True, text=True)
    return (proc.stdout or "") + (proc.stderr or "") if getattr(proc, "returncode", 1) == 0 else None


def run_grub_script_check(files: Mapping[str, Path], *, which=shutil.which, run=subprocess.run) -> Optional[Dict[str, Tuple[int, str]]]:
    """grub-script-check on each extracted GRUB menu; None when the tool is not installed."""
    tool = which("grub-script-check")
    if not tool:
        return None
    res: Dict[str, Tuple[int, str]] = {}
    for name, path in files.items():
        proc = run([tool, str(path)], capture_output=True, text=True)
        res[name] = (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    return res


def check_iso_menus(iso: Path, work: Path, *, which=shutil.which, run=subprocess.run) -> List[ic.Finding]:
    """Read the menus, the file list and the boot catalogue from *iso* with xorriso and run :func:`check_menus`."""
    if not which("xorriso"):
        return [ic.fail("menu-xorriso", "xorriso not found (apt-get install xorriso): the boot menus of the ISO cannot be read")]
    listing = iso_listing(iso, which=which, run=run)
    if listing is None:
        return [ic.fail("menu-listing", "xorriso could not list the ISO's boot files")]
    work.mkdir(parents=True, exist_ok=True)
    wanted = list(GRUB_MENUS) + sorted(p.lstrip("/") for p in listing if p.startswith("/isolinux/") and p.endswith(".cfg"))
    files: Dict[str, str] = {}
    paths: Dict[str, Path] = {}
    for rel in wanted:
        dest = work / rel.replace("/", "__")
        text = iso_extract_text(iso, "/" + rel, dest, which=which, run=run)
        if text is not None:
            files[rel] = text
            paths[rel] = dest
    checked = run_grub_script_check({k: v for k, v in paths.items() if k in GRUB_MENUS}, which=which, run=run)
    return check_menus(files, listing, grub_script_check=checked, el_torito=iso_el_torito(iso, which=which, run=run))


# ============================================================================================
#  command line
# ============================================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--iso", required=True, help="path to the built ISO (glob allowed)")
    ap.add_argument("--work-dir", default=None, help="where the extracted menu files go (default: a menu-checks folder next to the ISO)")
    ns = ap.parse_args(argv)
    matches = sorted(glob.glob(ns.iso))
    if not matches:
        print("menu_checks: no ISO matched %r" % ns.iso, file=sys.stderr)
        return 2
    iso = Path(matches[-1]).resolve()
    work = Path(ns.work_dir) if ns.work_dir else iso.parent / "menu-checks"
    if not shutil.which("xorriso"):
        print("menu_checks: xorriso not found (apt-get install xorriso)", file=sys.stderr)
        return 2
    findings = check_iso_menus(iso, work)
    print("===== boot menu checks: %s =====" % iso.name)
    for f in findings:
        print(f.line())
    bad = ic.failures(findings)
    print("===== %s (%d failed, %d warnings) =====" % ("FAIL" if bad else "PASS", len(bad), len(ic.warnings(findings))))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
