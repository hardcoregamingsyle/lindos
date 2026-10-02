#!/usr/bin/env python3
"""merge-mimeapps.py - add Lindos's default applications to /etc/xdg/mimeapps.list.

lindos-desktop ships its defaults (Mousepad, Ristretto, Evince, VLC) as /usr/share/lindos/mimeapps-desktop.list.
Like lindos-compat's own merge, this only ADDS: a [Default Applications] key that is already set (by the
administrator, lindos-compat, Lindos Setup's browser choice) keeps its value, [Added Associations] values are
unioned, comments and unknown sections stay as they are.  Idempotent, atomic, stdlib only.  Exit status is 0
unless the command line is wrong: a postinst must never fail over a default application.

Usage: merge-mimeapps.py [--root DIR] [--src FILE] [--dst FILE] [--dry-run]
Env:   LINDOS_ROOT  prefix for the default --src / --dst paths (tests); --root wins.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

SRC = "/usr/share/lindos/mimeapps-desktop.list"
DST = "/etc/xdg/mimeapps.list"
HEAD = "__head__"

Entry = Tuple[Optional[str], str]   # (key, value) for KEY=VALUE lines, (None, raw line) for everything else


def parse(text: str) -> Tuple[List[str], Dict[str, List[Entry]]]:
    order: List[str] = []
    sections: Dict[str, List[Entry]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            current = m.group(1).strip()
            if current not in sections:
                order.append(current)
                sections[current] = []
            continue
        if current is None:
            current = HEAD
            order.insert(0, HEAD)
            sections[HEAD] = []
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            sections[current].append((key.strip(), value.strip()))
        else:
            sections[current].append((None, raw))
    return order, sections


def _append(entries: List[Entry], key: str, value: str) -> None:
    """Insert before the trailing blank lines of the section (keeps the file tidy)."""
    pos = len(entries)
    while pos > 0 and entries[pos - 1][0] is None and not entries[pos - 1][1].strip():
        pos -= 1
    entries.insert(pos, (key, value))


def merge(src_text: str, dst_text: str) -> str:
    """The new text of the destination (the destination text itself when nothing has to change)."""
    _s_order, src = parse(src_text)
    d_order, dst = parse(dst_text)
    changed = False

    def section(name: str) -> List[Entry]:
        nonlocal changed
        if name not in dst:
            d_order.append(name)
            dst[name] = []
            changed = True
        return dst[name]

    for key, value in src.get("Default Applications", []):
        if key is None:
            continue
        entries = section("Default Applications")
        seen = [i for i, (k, _v) in enumerate(entries) if k == key]
        if not seen:
            _append(entries, key, value)
            changed = True
        else:
            for i in reversed(seen[1:]):       # a duplicated key: the first one wins
                del entries[i]
                changed = True

    for key, value in src.get("Added Associations", []):
        if key is None:
            continue
        entries = section("Added Associations")
        seen = [i for i, (k, _v) in enumerate(entries) if k == key]
        if not seen:
            _append(entries, key, value)
            changed = True
            continue
        k, v = entries[seen[0]]
        have = [a for a in v.split(";") if a]
        missing = [a for a in value.split(";") if a and a not in have]
        if missing:
            entries[seen[0]] = (k, ";".join(have + missing) + ";")
            changed = True

    if not changed:
        return dst_text
    lines: List[str] = []
    for name in d_order:
        if name != HEAD:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[%s]" % name)
        for k, v in dst[name]:
            lines.append(v if k is None else "%s=%s" % (k, v))
    return "\n".join(lines).rstrip("\n") + "\n"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read().replace("\r\n", "\n")
    except OSError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="merge-mimeapps.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=None, help="prefix for the default paths (default: $LINDOS_ROOT or none)")
    ap.add_argument("--src", default=None, help="defaults to merge (default: %s)" % SRC)
    ap.add_argument("--dst", default=None, help="file to merge into (default: %s)" % DST)
    ap.add_argument("--dry-run", action="store_true", help="say whether the file would change, write nothing")
    args = ap.parse_args(argv)

    root = (args.root if args.root is not None else os.environ.get("LINDOS_ROOT", "")).rstrip("/\\")
    src_path = args.src or root + SRC
    dst_path = args.dst or root + DST
    try:
        src_text = _read(src_path)
        if src_text is None:
            sys.stderr.write("merge-mimeapps: %s not found (nothing to merge)\n" % src_path)
            return 0
        dst_text = _read(dst_path) or ""
        new = merge(src_text, dst_text)
        if new == dst_text:
            return 0
        if args.dry_run:
            sys.stderr.write("merge-mimeapps: would update %s\n" % dst_path)
            return 0
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".lindos-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst_path)
        sys.stderr.write("merge-mimeapps: updated %s\n" % dst_path)
    except Exception as exc:  # noqa: BLE001 - never fail a maintainer script
        sys.stderr.write("merge-mimeapps: WARNING: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
