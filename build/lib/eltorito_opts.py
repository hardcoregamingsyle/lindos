#!/usr/bin/env python3
"""eltorito_opts.py — turn ``xorriso -indev BASE.iso -report_el_torito as_mkisofs``
output into an argument list for ``xorriso -as mkisofs``.

Why
---
Linux Mint 22 / Ubuntu 24.04 ISOs are *hybrid* images: an ISO 9660 filesystem
plus an MBR (GRUB's ``boot_hybrid.img``), a protective GPT and an **appended
EFI System Partition** (partition 2) that is *not* a file inside the ISO tree.
Re-creating that layout by hand is fragile.  xorriso can *report* the exact
mkisofs-style options that reproduce the boot equipment of a loaded image::

    $ xorriso -indev linuxmint-22.2-xfce-64bit.iso -report_el_torito as_mkisofs
    -V 'Linux Mint 22.2 Xfce 64-bit'
    --modification-date='2025072813091400'
    --grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:'linuxmint-22.2-xfce-64bit.iso'
    --protective-msdos-label
    -partition_cyl_align off
    -partition_offset 16
    --mbr-force-bootable
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b --interval:local_fs:5919804d-5930075d::'linuxmint-22.2-xfce-64bit.iso'
    -appended_part_as_gpt
    -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7
    -c '/boot.catalog'
    -b '/boot/grub/i386-pc/eltorito.img'
    -no-emul-boot
    -boot-load-size 4
    -boot-info-table
    --grub2-boot-info
    -eltorito-alt-boot
    -e '--interval:appended_partition_2_start_1479951s_size_10272d:all::'
    -no-emul-boot
    -boot-load-size 10272

The ``--interval:local_fs:…:'FILE'`` tokens reference **byte ranges of the base
ISO file itself** (MBR template, EFI partition image).  As long as the base ISO
path is valid when we run ``xorriso -as mkisofs`` they can be reused verbatim,
which is exactly what build/build-iso.sh does (method 1).  This module:

* parses the report (one option per line, shell-quoted values),
* optionally rewrites the referenced ISO path (e.g. to an absolute path),
* strips ``-V`` and ``--modification-date=`` so the caller can set its own,
* prints the result as newline-separated tokens (``--format lines``, safe for
  ``mapfile -t``), a shell-quoted string, or JSON,
* can sanity-check that boot equipment was actually found (``--check``).

Pure stdlib, importable on any OS (tested with pytest on Windows).

CLI
---
    python3 build/lib/eltorito_opts.py --report out/work/eltorito-report.txt \
        --iso /abs/path/base.iso --format lines --check

Exit codes: 0 ok · 1 error · 2 usage · 3 no boot equipment in report
"""
from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from typing import Iterable, List, Optional, Sequence

__all__ = [
    "parse_report",
    "iso_refs",
    "rewrite_iso_path",
    "strip_option",
    "strip_volid",
    "strip_modification_date",
    "boot_paths",
    "catalog_path",
    "has_boot_equipment",
    "uses_isolinux",
    "as_mkisofs_args",
    "to_shell",
    "main",
]

LOG = logging.getLogger("eltorito_opts")

INTERVAL_PREFIX = "--interval:"
LOCAL_FS_PREFIX = "--interval:local_fs:"

# Options that take exactly one following argument (used by strip_option and
# boot_paths).  Everything else in the report is either a flag or a
# "--name=value" token.
_ONE_ARG_OPTIONS = {
    "-V", "-volid",
    "-c", "-b", "-e",
    "-boot-load-size",
    "-partition_offset", "-partition_cyl_align",
    "-iso_mbr_part_type",
    "-isohybrid-mbr", "--grub2-mbr",
    "-G", "-A", "-p", "-P", "-o",
}
# -append_partition takes three arguments: NUMBER TYPE SOURCE
_THREE_ARG_OPTIONS = {"-append_partition"}


class ReportError(ValueError):
    """Raised when the report cannot be parsed."""


def _option_lines(text: str) -> Iterable[str]:
    """Yield the lines of *text* that carry mkisofs options.

    xorriso prints informational lines first ("Drive current:", "Media
    summary:", "Volume id    :" …) and NOTE/UPDATE lines on stderr.  Real
    option lines start with '-' (single or double dash).
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            yield line


def parse_report(text: str) -> List[str]:
    """Parse the ``-report_el_torito as_mkisofs`` output into argv tokens.

    Values are shell-quoted by xorriso (``-c '/boot.catalog'``); ``shlex``
    removes the quotes exactly as a POSIX shell would when the report is
    pasted on a command line, so the resulting tokens can be passed straight
    to ``subprocess`` / ``execvp`` without a shell.
    """
    tokens: List[str] = []
    for line in _option_lines(text):
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as exc:  # unbalanced quotes
            raise ReportError(f"cannot parse report line {line!r}: {exc}") from exc
        tokens.extend(parts)
    return tokens


def _split_interval(token: str) -> Optional[List[str]]:
    """Split ``--interval:FLAGS:RANGE:ZEROIZERS:SOURCE`` into its 5 fields.

    Returns None when *token* is not an interval spec.  SOURCE may contain
    colons (Windows-style or odd paths) — everything after the 4th colon is
    the source.
    """
    if not token.startswith(INTERVAL_PREFIX):
        return None
    fields = token.split(":", 4)
    if len(fields) != 5:
        return None
    return fields


def iso_refs(tokens: Sequence[str]) -> List[str]:
    """Return the ISO file paths referenced by ``--interval:local_fs`` tokens
    (usually all the same: the base ISO given to ``-indev``)."""
    refs: List[str] = []
    for tok in tokens:
        fields = _split_interval(tok)
        if fields and fields[1] == "local_fs" and fields[4]:
            refs.append(fields[4])
    return refs


def rewrite_iso_path(tokens: Sequence[str], new_path: str) -> List[str]:
    """Return a copy of *tokens* where every ``--interval:local_fs`` source
    path is replaced by *new_path* (byte ranges are kept)."""
    out: List[str] = []
    for tok in tokens:
        fields = _split_interval(tok)
        if fields and fields[1] == "local_fs":
            fields[4] = new_path
            out.append(":".join(fields))
        else:
            out.append(tok)
    return out


def strip_option(tokens: Sequence[str], name: str) -> List[str]:
    """Remove option *name* (and its argument(s)) from *tokens*.

    Handles ``-V value`` (one arg), ``-append_partition N T S`` (three args),
    flags and ``--name=value`` single tokens.
    """
    out: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == name:
            if name in _THREE_ARG_OPTIONS:
                i += 4
            elif name in _ONE_ARG_OPTIONS:
                i += 2
            else:
                i += 1
            continue
        if name.startswith("--") and tok.startswith(name + "="):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def strip_volid(tokens: Sequence[str]) -> List[str]:
    """Drop ``-V VALUE`` (and ``-volid``) so the caller can pass its own."""
    return strip_option(strip_option(tokens, "-V"), "-volid")


def strip_modification_date(tokens: Sequence[str]) -> List[str]:
    """Drop ``--modification-date=…`` so xorriso stamps the build time."""
    return strip_option(tokens, "--modification-date")


def _values_of(tokens: Sequence[str], name: str) -> List[str]:
    vals: List[str] = []
    for i, tok in enumerate(tokens):
        if tok == name and i + 1 < len(tokens):
            vals.append(tokens[i + 1])
    return vals


def boot_paths(tokens: Sequence[str]) -> List[str]:
    """ISO-tree paths of the boot images (``-b`` and non-interval ``-e``
    values).  build-iso.sh checks these files exist in the extracted tree."""
    paths: List[str] = []
    for name in ("-b", "-e"):
        for val in _values_of(tokens, name):
            if not val.startswith(INTERVAL_PREFIX):
                paths.append(val)
    return paths


def catalog_path(tokens: Sequence[str]) -> Optional[str]:
    """Path given to ``-c`` (the El Torito boot catalog, regenerated by xorriso)."""
    vals = _values_of(tokens, "-c")
    return vals[0] if vals else None


def has_boot_equipment(tokens: Sequence[str]) -> bool:
    """True when the report contains at least one boot image (``-b`` or ``-e``)."""
    return bool(_values_of(tokens, "-b") or _values_of(tokens, "-e"))


def uses_isolinux(tokens: Sequence[str]) -> bool:
    """True when the BIOS boot image is ISOLINUX (Mint ≤ 21 style)."""
    return any("isolinux" in p.lower() for p in _values_of(tokens, "-b")) or (
        "-isohybrid-mbr" in tokens
    )


def as_mkisofs_args(
    report_text: str,
    iso_path: Optional[str] = None,
    *,
    keep_volid: bool = False,
    keep_moddate: bool = False,
) -> List[str]:
    """One-shot helper: parse, rewrite the ISO path, strip -V/--modification-date."""
    tokens = parse_report(report_text)
    if iso_path:
        tokens = rewrite_iso_path(tokens, iso_path)
    if not keep_volid:
        tokens = strip_volid(tokens)
    if not keep_moddate:
        tokens = strip_modification_date(tokens)
    return tokens


def to_shell(tokens: Sequence[str]) -> str:
    """Shell-quoted single line (POSIX sh compatible)."""
    return " ".join(shlex.quote(t) for t in tokens)


def _read_report(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eltorito_opts",
        description=(
            "Convert 'xorriso -indev ISO -report_el_torito as_mkisofs' output "
            "into 'xorriso -as mkisofs' arguments."
        ),
    )
    p.add_argument("--report", required=True, metavar="FILE",
                   help="report file ('-' = stdin)")
    p.add_argument("--iso", metavar="PATH",
                   help="rewrite the --interval:local_fs source path to PATH "
                        "(use the absolute path of the base ISO)")
    p.add_argument("--keep-volid", action="store_true",
                   help="keep the reported -V (default: strip it)")
    p.add_argument("--keep-moddate", action="store_true",
                   help="keep --modification-date= (default: strip it)")
    p.add_argument("--format", choices=("lines", "shell", "json"), default="lines",
                   help="output format (default: lines — one token per line)")
    p.add_argument("--check", action="store_true",
                   help="exit 3 when the report carries no boot image (-b/-e)")
    p.add_argument("--print-boot-paths", action="store_true",
                   help="instead of options, print the ISO-tree boot image paths")
    p.add_argument("--print-iso-refs", action="store_true",
                   help="instead of options, print the referenced ISO file paths")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        text = _read_report(args.report)
    except OSError as exc:
        LOG.error("cannot read report %s: %s", args.report, exc)
        return 1
    try:
        tokens = as_mkisofs_args(
            text, args.iso, keep_volid=args.keep_volid, keep_moddate=args.keep_moddate
        )
    except ReportError as exc:
        LOG.error("%s", exc)
        return 1

    if args.check and not has_boot_equipment(tokens):
        LOG.error("no El Torito boot image (-b/-e) found in report %s", args.report)
        return 3

    if args.print_boot_paths:
        for pth in boot_paths(tokens):
            print(pth)
        return 0
    if args.print_iso_refs:
        for ref in sorted(set(iso_refs(tokens))):
            print(ref)
        return 0

    if args.format == "lines":
        for tok in tokens:
            print(tok)
    elif args.format == "shell":
        print(to_shell(tokens))
    else:
        print(json.dumps(tokens, indent=2))
    LOG.debug("%d tokens", len(tokens))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
