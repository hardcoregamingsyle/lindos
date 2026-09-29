#!/usr/bin/env python3
"""Lindos boot menus: parse the GRUB entries and generate the BIOS (isolinux) menu from them.

The base ISO boots UEFI machines through /boot/grub/grub.cfg (build/overlay/boot/grub/grub.cfg) and BIOS
machines through ISOLINUX (/isolinux/live.cfg), whose entries pass ``username=mint hostname=mint`` and know
nothing of the Lindos flow.  This module keeps both in step: the GRUB file is the single source of truth and
``isolinux`` rewrites live.cfg so that its casper entries are the same ones (same kernel words, same
labels), while everything else in live.cfg (colours, title, hardware detection, boot from local drive,
memtest) stays as the base ISO has it.

Usage (build/build-iso.sh apply_overlay)::

    python3 build/lib/boot_menu.py isolinux --grub ISO/boot/grub/grub.cfg --live ISO/isolinux/live.cfg --write
    python3 build/lib/boot_menu.py entries  --grub ISO/boot/grub/grub.cfg        # print title | args

Standard library only; importable on any OS.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

KERNEL_DEFAULT = "/casper/vmlinuz"
INITRD_DEFAULT = "/casper/initrd.lz"


class Entry(NamedTuple):
    title: str
    kernel: str
    initrd: str
    args: List[str]        # kernel command line words, without the trailing "--"

    @property
    def is_installer(self) -> bool:
        return "only-ubiquity" in self.args


_ENTRY = re.compile(r'^\s*menuentry\s+"(?P<title>[^"]+)"[^{]*\{(?P<body>.*?)^\}', re.S | re.M)
_LINUX = re.compile(r"^\s*linux\s+(?P<kernel>\S+)\s+(?P<args>.*?)\s*$", re.M)
_INITRD = re.compile(r"^\s*initrd\s+(?P<initrd>\S+)", re.M)


def parse_grub_entries(text: str) -> List[Entry]:
    """The entries of a grub.cfg that boot a casper kernel (menuentries with a ``linux`` line)."""
    entries: List[Entry] = []
    for match in _ENTRY.finditer(text):
        body = match.group("body")
        linux = _LINUX.search(body)
        if not linux:
            continue        # "Boot from the first hard disk" and friends
        words = linux.group("args").split()
        if words and words[-1] == "--":
            words = words[:-1]
        initrd = _INITRD.search(body)
        entries.append(Entry(match.group("title"), linux.group("kernel"),
                             initrd.group("initrd") if initrd else INITRD_DEFAULT, words))
    return entries


def isolinux_words(entry: Entry) -> List[str]:
    """The entry's kernel words as ISOLINUX can pass them: no GRUB variables, no loopback hint."""
    out: List[str] = []
    for word in entry.args:
        if "${" in word or word.startswith("iso-scan/filename="):
            continue        # ${iso_path} belongs to loop-booting loaders; ISOLINUX boots the medium itself
        out.append(word)
    return out


def isolinux_label(entry: Entry, index: int) -> str:
    """A stable label: install, install-compat, try, try-compat, check."""
    lowered = entry.title.lower()
    if entry.is_installer:
        base = "install"
    elif "integrity" in lowered:
        base = "check"
    else:
        base = "try"
    if "compatibility" in lowered or "nomodeset" in entry.args:
        base += "-compat"
    return base


def render_labels(entries: List[Entry]) -> str:
    lines: List[str] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(entries):
        label = isolinux_label(entry, index)
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = "%s%d" % (label, seen[label])
        words = isolinux_words(entry)
        lines.append("label %s" % label)
        lines.append("\tmenu label %s" % entry.title)
        if index == 0:
            lines.append("\tmenu default")
        lines.append("\tkernel %s" % entry.kernel)
        lines.append("\tappend initrd=%s %s --" % (entry.initrd, " ".join(words)))
        lines.append("")
    return "\n".join(lines)


_LABEL_LINE = re.compile(r"^\s*label\s+(\S+)", re.I)


def _split_blocks(text: str):
    """(header lines before the first label, [(label, block lines)...])."""
    header: List[str] = []
    blocks: List[List[str]] = []
    for line in text.splitlines():
        if _LABEL_LINE.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            header.append(line)
    return header, blocks


def _is_casper_block(block: List[str]) -> bool:
    joined = "\n".join(block)
    return "boot=casper" in joined or "/casper/vmlinuz" in joined


def rewrite_live_cfg(live_text: str, entries: List[Entry]) -> str:
    """live.cfg with every casper entry replaced by *entries*; other blocks and the header are kept."""
    if not entries:
        raise ValueError("no casper entries to generate the BIOS menu from")
    header, blocks = _split_blocks(live_text)
    kept = [b for b in blocks if not _is_casper_block(b)]
    out: List[str] = list(header)
    while out and not out[-1].strip():
        out.pop()
    if out:
        out.append("")
    out.append(render_labels(entries).rstrip("\n"))
    out.append("")
    for block in kept:
        while block and not block[-1].strip():
            block.pop()
        out.extend(block)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def casper_labels(live_text: str) -> List[str]:
    """The labels of the casper blocks of a live.cfg (what a rewrite replaces)."""
    _header, blocks = _split_blocks(live_text)
    labels: List[str] = []
    for block in blocks:
        if _is_casper_block(block):
            m = _LABEL_LINE.match(block[0])
            if m:
                labels.append(m.group(1))
    return labels


_LABEL_REF = re.compile(r"^(?P<lead>[ \t]*(?:default|ontimeout)[ \t]+)(?P<name>\S+)(?P<tail>[ \t]*)$", re.I | re.M)


def retarget_label_refs(text: str, old_labels: List[str], new_default: str) -> str:
    """Point ``default``/``ontimeout`` lines that named a replaced label at *new_default*.

    ISOLINUX configs of some bases say ``default live``; the rewritten menu has no ``live`` label any more
    and a dangling default drops the user to a bare ``boot:`` prompt.  Module names (``default vesamenu.c32``)
    are not labels and stay as they are.
    """
    old = set(old_labels)

    def _fix(m: "re.Match[str]") -> str:
        if m.group("name") in old:
            return "%s%s%s" % (m.group("lead"), new_default, m.group("tail"))
        return m.group(0)

    return _LABEL_REF.sub(_fix, text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("entries", help="print the casper entries of a grub.cfg")
    p.add_argument("--grub", required=True)
    p = sub.add_parser("isolinux", help="rewrite isolinux/live.cfg from grub.cfg")
    p.add_argument("--grub", required=True)
    p.add_argument("--live", required=True)
    p.add_argument("--write", action="store_true", help="write the file (default: print)")
    ns = ap.parse_args(argv)
    entries = parse_grub_entries(Path(ns.grub).read_text(encoding="utf-8"))
    if ns.cmd == "entries":
        for e in entries:
            print("%s | %s" % (e.title, " ".join(e.args)))
        return 0 if entries else 1
    live = Path(ns.live)
    if not live.is_file():
        print("boot_menu: %s does not exist - no BIOS menu to rewrite" % live, file=sys.stderr)
        return 0
    original = live.read_text(encoding="utf-8")
    try:
        text = rewrite_live_cfg(original, entries)
    except ValueError as exc:
        print("boot_menu: %s" % exc, file=sys.stderr)
        return 1
    if not ns.write:
        sys.stdout.write(text)
        return 0
    with open(live, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("boot_menu: %s rewritten (%d entries)" % (live, len(entries)))
    old_labels = casper_labels(original)
    new_default = isolinux_label(entries[0], 0)
    for sibling in sorted(live.parent.glob("*.cfg")):
        if sibling == live:
            continue
        before = sibling.read_text(encoding="utf-8", errors="replace")
        after = retarget_label_refs(before, old_labels, new_default)
        if after != before:
            with open(sibling, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(after)
            print("boot_menu: %s now defaults to the '%s' label" % (sibling.name, new_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
