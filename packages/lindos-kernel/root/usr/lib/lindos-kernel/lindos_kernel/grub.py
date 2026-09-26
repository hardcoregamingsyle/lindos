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
import shutil
import subprocess
from typing import Any, Callable, List, Optional

from . import grub_dropin_path, kernel_select_dropin_path

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
    """Reassemble the file, tidying blank lines around the managed region.

    A separator blank line is normalized to exactly one where the surrounding text already had
    one (one or more trailing/leading blank lines around the fence), but is never *introduced*
    where there was none. Every shipped drop-in's explanatory comment block runs right up
    against ``# >>> lindos >>>`` with no gap, so a no-op call (e.g. the postinst's
    ``apply-selection``, which fires on every kernel install) must reproduce that file
    byte-for-byte; synthesizing a blank line that was never there would make dpkg treat an
    untouched conffile as locally modified on the very next upgrade.
    """
    parts: List[str] = []
    # trailing blanks of `before` are dropped so we control spacing, but remember whether
    # there were any so we only re-add a separator when one already existed.
    top = list(before)
    top_had_gap = bool(top) and top[-1].strip() == ""
    while top and top[-1].strip() == "":
        top.pop()
    parts.extend(top)
    if block_text is not None:
        if parts and top_had_gap:
            parts.append("")
        parts.extend(block_text.rstrip("\n").splitlines())
    tail = list(after)
    tail_had_gap = bool(tail) and tail[0].strip() == ""
    while tail and tail[0].strip() == "":
        tail.pop(0)
    if tail:
        if parts and tail_had_gap:
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


# --------------------------------------------------------------------------------------------
# Secure-Boot-aware default-kernel selection (SPEC-WINDOWS §31.3)
#
# ``/etc/default/grub.d/51-lindos-kernel-select.cfg`` is a *second*, independent marker-fenced
# drop-in (same idempotent-fence idiom as the cmdline drop-in above, in its own file so the two
# concerns -- cmdline flags vs. which kernel boots by default -- stay separately readable and
# separately reversible).  It only ever sets ``GRUB_DEFAULT``.
#
# The policy (binding, SPEC-WINDOWS §27.5 + §31.3): the Lindos kernel is allowed to be the
# default boot entry only when Secure Boot is off, or the installed Lindos image is itself
# signed with the enrolled MOK.  Otherwise GRUB must keep booting a kernel shim/firmware will
# actually accept, so a user who turns Secure Boot on for a Windows game never finds their
# machine stuck at a Secure Boot violation screen with no obvious way back in.
# --------------------------------------------------------------------------------------------
SELECT_VAR = "GRUB_DEFAULT"
_SELECT_LINE_RE = re.compile(r'^\s*' + re.escape(SELECT_VAR) + r'="(?P<value>[^"]*)"\s*$')

RunFn = Callable[..., Any]
WhichFn = Callable[[str], Optional[str]]


def may_boot_lindos_by_default(secure_boot: Optional[bool], signed: Optional[bool]) -> bool:
    """The binding §31.3 rule: Secure Boot off, OR the image is signed.

    An unknown Secure Boot state (``None`` -- typically a BIOS/legacy machine, or nothing
    readable) is treated the same as "off": there is no Secure Boot chain to violate.  An
    unknown *signed* state only matters when Secure Boot is confirmed on, in which case it is
    treated as "not signed" (fail safe -- never assume a kernel is signed).
    """
    if secure_boot is not True:
        return True
    return bool(signed)


def render_kernel_select_block(value: str) -> str:
    """Render the fenced managed region setting ``GRUB_DEFAULT="<value>"``."""
    return f'{BEGIN}\n{SELECT_VAR}="{value}"\n{END}\n'


def current_kernel_select(path: Optional[str] = None) -> Optional[str]:
    """The ``GRUB_DEFAULT`` value currently in the managed region, or ``None``."""
    target = path or kernel_select_dropin_path()
    _before, block, _after = _split_block(read(target))
    if block is None:
        return None
    for line in block:
        match = _SELECT_LINE_RE.match(line)
        if match:
            return match.group("value")
    return None


def set_kernel_select(value: str, path: Optional[str] = None) -> str:
    """Write *value* into the managed ``GRUB_DEFAULT`` region idempotently."""
    target = path or kernel_select_dropin_path()
    before, block, after = _split_block(read(target))
    if block is None:
        before = read(target).splitlines()
        after = []
    new_text = _compose(before, render_kernel_select_block(value), after)
    _write(target, new_text)
    return new_text


def boot_device_id(run: RunFn = subprocess.run, which: WhichFn = shutil.which) -> Optional[str]:
    """The filesystem-UUID-based device id GRUB's stock ``10_linux``/``30_os-prober`` templates
    key menu-entry ids on (``grub_get_device_id`` in ``grub-mkconfig_lib.in``), resolved with
    ``grub-probe --target=fs_uuid`` against ``/boot`` (falling back to ``/``).  ``None`` when
    ``grub-probe`` is unavailable or fails on both.
    """
    if not which("grub-probe"):
        return None
    for target in ("/boot", "/"):
        try:
            proc = run(["grub-probe", "--target=fs_uuid", target], capture_output=True,
                       text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(proc, "returncode", 1) == 0:
            out = (getattr(proc, "stdout", "") or "").strip()
            if out:
                return out
    return None


def advanced_submenu_id(device_id: str) -> str:
    """The id of the stock "Advanced options" submenu (``10_linux`` template)."""
    return f"gnulinux-advanced-{device_id}"


def kernel_menu_entry_id(version: str, device_id: str, kind: str = "advanced") -> str:
    """The id of one kernel's entry inside that submenu (``kind``: ``advanced``/``recovery``)."""
    return f"gnulinux-{version}-{kind}-{device_id}"


def select_default_target(select_lindos: bool, lindos_version: Optional[str] = None,
                          fallback_version: Optional[str] = None,
                          device_id: Optional[str] = None) -> str:
    """The ``GRUB_DEFAULT`` value that implements the §31.3 policy.

    * ``select_lindos`` True (safe): ``"0"`` -- GRUB's own normal behaviour (boot the newest
      installed kernel) is left alone; no override needed.
    * ``select_lindos`` False (Secure Boot on, image unsigned) *and* enough is known to name the
      exact fallback entry (``fallback_version`` + ``device_id``): the "Advanced options"
      submenu's specific entry for that kernel, so GRUB boots a kernel shim will actually run.
    * ``select_lindos`` False but the fallback entry cannot be named precisely (missing
      ``device_id``/``fallback_version`` -- e.g. ``grub-probe`` unavailable): ``"1"``, which at
      least opens the "Advanced options" submenu instead of the top-level (Lindos) entry, so the
      user lands on a menu of real choices rather than a Secure Boot violation.
    """
    if select_lindos:
        return "0"
    if fallback_version and device_id:
        return f"{advanced_submenu_id(device_id)}>{kernel_menu_entry_id(fallback_version, device_id)}"
    return "1"


def apply_kernel_selection(secure_boot: Optional[bool], signed: Optional[bool],
                           lindos_version: Optional[str] = None,
                           fallback_version: Optional[str] = None,
                           device_id: Optional[str] = None,
                           run: RunFn = subprocess.run, which: WhichFn = shutil.which,
                           path: Optional[str] = None) -> str:
    """Compute and persist the §31.3 selection; returns the ``GRUB_DEFAULT`` value written.

    ``device_id`` is resolved via :func:`boot_device_id` when not given.  Never calls
    ``update-grub`` itself -- callers print :func:`update_grub_command` afterwards, same as
    every other mutation in this module.
    """
    allow = may_boot_lindos_by_default(secure_boot, signed)
    resolved_device_id = device_id if device_id is not None else boot_device_id(run, which)
    value = select_default_target(allow, lindos_version, fallback_version, resolved_device_id)
    set_kernel_select(value, path)
    return value


__all__ = [
    "BEGIN", "END", "VAR", "DEFAULT_FLAGS", "GAMING_EXTRA_FLAGS", "PRESETS", "GrubError",
    "valid_flag", "render_block", "read", "current_flags", "set_flags", "reset", "preset",
    "add_flags", "remove_block", "update_grub_command",
    "SELECT_VAR", "may_boot_lindos_by_default", "render_kernel_select_block",
    "current_kernel_select", "set_kernel_select", "boot_device_id", "advanced_submenu_id",
    "kernel_menu_entry_id", "select_default_target", "apply_kernel_selection",
]
