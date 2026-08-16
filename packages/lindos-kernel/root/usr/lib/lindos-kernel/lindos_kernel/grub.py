"""Read and patch the marker-fenced GRUB cmdline drop-in (SPEC-KERNEL §15.3).

``/etc/default/grub.d/50-lindos.cfg`` appends only safe, reversible flags to
``GRUB_CMDLINE_LINUX_DEFAULT``.  All Lindos edits live between the fences::

    # >>> lindos >>>
    GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT <flags...>"
    # <<< lindos <<<

so :func:`set_flags` / :func:`reset` / :func:`remove_block` are idempotent — running any of
them twice yields byte-identical output.  Text outside the fences is preserved verbatim.  This
module never runs ``update-grub``; the CLI prints the privileged follow-up command instead.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

from . import grub_dropin_path

BEGIN = "# >>> lindos >>>"
END = "# <<< lindos <<<"
VAR = "GRUB_CMDLINE_LINUX_DEFAULT"

#: Conservative, always-safe flags (SPEC-KERNEL §15.3).
DEFAULT_FLAGS: List[str] = [
    "transparent_hugepage=madvise",
    "nowatchdog",
    "nvme_core.default_ps_max_latency_us=0",
]
#: Extra flags the *gaming* preset opts into (security/perf trade-off; documented).
GAMING_EXTRA_FLAGS: List[str] = ["mitigations=off"]

PRESETS = {
    "default": list(DEFAULT_FLAGS),
    "gaming": list(DEFAULT_FLAGS) + list(GAMING_EXTRA_FLAGS),
}

_FLAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:=+-]*$")
_APPEND_RE = re.compile(
    r'^\s*' + re.escape(VAR) + r'="\$' + re.escape(VAR) + r'\s*(?P<flags>[^"]*)"\s*$'
)


class GrubError(ValueError):
    """Raised on an invalid flag."""


def valid_flag(flag: str) -> bool:
    return bool(_FLAG_RE.match(flag))


def _check_flags(flags: List[str]) -> List[str]:
    cleaned: List[str] = []
    for flag in flags:
        flag = flag.strip()
        if not flag:
            continue
        if not valid_flag(flag):
            raise GrubError(f"unsafe or malformed cmdline flag: {flag!r}")
        if flag not in cleaned:
            cleaned.append(flag)
    return cleaned


def render_block(flags: List[str]) -> str:
    """Render the fenced managed region for *flags* (no surrounding blank lines)."""
    cleaned = _check_flags(flags)
    joined = (" " + " ".join(cleaned)) if cleaned else ""
    return f'{BEGIN}\n{VAR}="${VAR}{joined}"\n{END}\n'


def read(path: Optional[str] = None) -> str:
    """Return the drop-in's text ("" when the file does not exist)."""
    target = path or grub_dropin_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:  # pragma: no cover - unusual
        raise GrubError(f"cannot read {target}: {exc}") from exc


def _split_block(text: str) -> tuple:
    """Return ``(before, block_lines, after)`` around the first fenced region.

    ``block_lines`` is the list of lines strictly *between* the fences (markers excluded);
    it is ``None`` when there is no complete block.
    """
    lines = text.splitlines()
    start = end = -1
    for idx, line in enumerate(lines):
        if line.strip() == BEGIN and start == -1:
            start = idx
        elif line.strip() == END and start != -1:
            end = idx
            break
    if start == -1 or end == -1:
        return lines, None, []
    return lines[:start], lines[start + 1:end], lines[end + 1:]


def current_flags(path: Optional[str] = None, text: Optional[str] = None) -> List[str]:
    """Flags currently in the managed region ("" / missing block ⇒ empty list)."""
    body = read(path) if text is None else text
    _before, block, _after = _split_block(body)
    if block is None:
        return []
    for line in block:
        match = _APPEND_RE.match(line)
        if match:
            return [f for f in match.group("flags").split() if f]
    return []


def _compose(before: List[str], block_text: Optional[str], after: List[str]) -> str:
    """Reassemble the file, tidying blank lines around the managed region."""
    parts: List[str] = []
    # trailing blanks of `before` are dropped so we control spacing
    top = list(before)
    while top and top[-1].strip() == "":
        top.pop()
    parts.extend(top)
    if block_text is not None:
        if parts:
            parts.append("")
        parts.extend(block_text.rstrip("\n").splitlines())
    tail = list(after)
    while tail and tail[0].strip() == "":
        tail.pop(0)
    if tail:
        if parts:
            parts.append("")
        parts.extend(tail)
    return "\n".join(parts) + "\n"


def set_flags(flags: List[str], path: Optional[str] = None) -> str:
    """Write *flags* into the managed region idempotently; return the new file text."""
    target = path or grub_dropin_path()
    before, block, after = _split_block(read(target))
    if block is None:
        # No existing fenced region: append one after whatever is there.
        before = read(target).splitlines()
        after = []
    new_text = _compose(before, render_block(flags), after)
    _write(target, new_text)
    return new_text


def reset(path: Optional[str] = None) -> str:
    """Restore the conservative default flag set (SPEC-KERNEL §15.3)."""
    return set_flags(list(DEFAULT_FLAGS), path)


def preset(name: str, path: Optional[str] = None) -> str:
    """Apply a named preset (``default`` or ``gaming``)."""
    if name not in PRESETS:
        raise GrubError(f"unknown preset {name!r}; choose from {', '.join(sorted(PRESETS))}")
    return set_flags(list(PRESETS[name]), path)


def add_flags(flags: List[str], path: Optional[str] = None) -> str:
    """Merge *flags* into whatever is already in the managed region (idempotent)."""
    merged = current_flags(path) + list(flags)
    return set_flags(merged, path)


def remove_block(path: Optional[str] = None) -> str:
    """Strip the Lindos managed region entirely (idempotent); return the new file text."""
    target = path or grub_dropin_path()
    before, block, after = _split_block(read(target))
    if block is None:
        return read(target)
    new_text = _compose(before, None, after)
    _write(target, new_text)
    return new_text


def _write(target: str, text: str) -> None:
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.lindos.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, target)


def update_grub_command() -> List[str]:
    """The privileged follow-up the CLI must print (never executed here)."""
    return ["pkexec", "update-grub"]


__all__ = [
    "BEGIN", "END", "VAR", "DEFAULT_FLAGS", "GAMING_EXTRA_FLAGS", "PRESETS", "GrubError",
    "valid_flag", "render_block", "read", "current_flags", "set_flags", "reset", "preset",
    "add_flags", "remove_block", "update_grub_command",
]
