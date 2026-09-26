"""Terminal ``./setup.exe``: the ``lindos-pe`` binfmt_misc entry — status, conflicts, commands.

SPEC-WINDOWS §28.7.  ``/usr/lib/binfmt.d/lindos-pe.conf`` holds one kernel rule (no flags)::

    :lindos-pe:M::MZ::/usr/libexec/lindos/lindos-binfmt:

so the kernel hands every executable file that starts with ``MZ`` to the ``lindos-binfmt``
wrapper, which re-checks the file (:func:`check_program`) and execs ``lindos-run``.

* **Enable** (helper action ``set-binfmt``, as root): remove the mask
  ``/etc/binfmt.d/lindos-pe.conf`` and run ``/usr/lib/systemd/systemd-binfmt
  /usr/lib/binfmt.d/lindos-pe.conf`` — that registers *only* this file (it replaces a
  same-named entry).  **Never** ``systemctl restart|stop systemd-binfmt``: it flushes every
  entry (qemu, wine …).
* **Disable**: write the mask symlink (``→ /dev/null``, persists across boots) and remove the
  live entry by writing ``-1`` to ``/proc/sys/fs/binfmt_misc/lindos-pe`` (absent entry = fine).
* **Conflicts**: any other *enabled* entry that also matches ``MZ`` at offset 0 (magic
  ``4d5a…``, honouring its mask) or claims the ``exe`` extension.  The kernel tries entries
  newest-first and binfmt-support registers after systemd-binfmt at boot, so the note says
  honestly which one probably wins.  Lindos never modifies other entries.  No ``.com``
  extension entry exists on purpose (it would hijack native Linux programs named ``*.com``).

:func:`status` and :func:`parse_proc_entry` only read files; ``root`` (default: ``LINDOS_ROOT``
or ``/``) lets tests supply a fake ``/proc`` + ``/etc`` + ``/usr`` tree.
``python3 -m lindos_compat.binfmt check FILE`` is the wrapper's fast re-check (exit 0 = run it,
3 = refused with a message on stderr); ``status --json`` prints :func:`status`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import get_logger

__all__ = [
    "BINFMT_NAME",
    "CONF_PATH",
    "MASK_PATH",
    "PROC_DIR",
    "INTERPRETER",
    "CONF_LINE",
    "SYSTEMD_BINFMT",
    "RUNNABLE_FORMATS",
    "status",
    "parse_proc_entry",
    "entry_matches_mz",
    "enable_commands",
    "disable_commands",
    "remove_runtime_entry",
    "check_program",
    "main",
]

log = get_logger("lindos-compat.binfmt")

BINFMT_NAME = "lindos-pe"
CONF_PATH = "/usr/lib/binfmt.d/lindos-pe.conf"
MASK_PATH = "/etc/binfmt.d/lindos-pe.conf"
PROC_DIR = "/proc/sys/fs/binfmt_misc"
INTERPRETER = "/usr/libexec/lindos/lindos-binfmt"
#: The single rule in CONF_PATH (type M, offset 0, magic "MZ", no mask, no flags).
CONF_LINE = f":{BINFMT_NAME}:M::MZ::{INTERPRETER}:"
SYSTEMD_BINFMT = "/usr/lib/systemd/systemd-binfmt"
#: Formats the wrapper may start from a terminal (other MZ images are refused with a message).
RUNNABLE_FORMATS = ("exe", "dotnet-exe", "win16-exe", "dos-exe", "scr")

_PROC_SKIP = ("register", "status")
_MZ = b"MZ"
_ENTRY_READ_LIMIT = 8192
_MAX_ENTRIES = 512


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _hex_bytes(text: str) -> Optional[bytes]:
    try:
        return bytes.fromhex("".join(text.split()))
    except ValueError:
        return None


def parse_proc_entry(text: str) -> Dict[str, object]:
    """Parse ``/proc/sys/fs/binfmt_misc/<name>``.

    Returns ``{"enabled", "interpreter", "flags", "flag_set", "type", "offset", "magic", "mask",
    "extension"}``.  Tolerant: unknown lines are ignored and unknown flag letters (e.g. the
    newer ``T``/``L``/``D``) are kept verbatim in ``flags``.
    """
    out: Dict[str, object] = {"enabled": False, "interpreter": "", "flags": "", "flag_set": [],
                              "type": "unknown", "offset": None, "magic": None, "mask": None, "extension": None}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low == "enabled":
            out["enabled"] = True
        elif low == "disabled":
            out["enabled"] = False
        elif low.startswith("interpreter "):
            out["interpreter"] = line.split(None, 1)[1].strip()
        elif low.startswith("flags:"):
            flags = line.split(":", 1)[1].strip()
            out["flags"] = flags
            out["flag_set"] = sorted({c for c in flags if c.isalpha()})
        elif low.startswith("offset "):
            try:
                out["offset"] = int(line.split(None, 1)[1].strip())
            except ValueError:
                out["offset"] = None
            out["type"] = "magic"
        elif low.startswith("magic "):
            out["magic"] = line.split(None, 1)[1].strip().lower()
            out["type"] = "magic"
        elif low.startswith("mask "):
            out["mask"] = line.split(None, 1)[1].strip().lower()
        elif low.startswith("extension "):
            out["extension"] = line.split(None, 1)[1].strip()
            out["type"] = "extension"
    return out


def entry_matches_mz(entry: Dict[str, object]) -> bool:
    """Would this (enabled or not) entry claim a Windows program?

    Magic entries: offset 0 and the (masked) magic agrees with ``MZ`` over its first bytes.
    Extension entries: the extension ``exe`` (the kernel compares case-sensitively).
    """
    if entry.get("type") == "extension":
        ext = str(entry.get("extension") or "").lstrip(".")
        return ext in ("exe", "EXE", "Exe")
    if entry.get("type") != "magic" or entry.get("offset") not in (0, None):
        return False
    magic = _hex_bytes(str(entry.get("magic") or ""))
    if not magic:
        return False
    mask_text = entry.get("mask")
    mask = _hex_bytes(str(mask_text)) if mask_text else None
    for i, want in enumerate(_MZ[:len(magic)]):
        m = mask[i] if mask is not None and i < len(mask) else 0xFF
        if (want & m) != (magic[i] & m):
            return False
    # a longer magic ("4d5a90...") matches only some Windows programs - still a competitor
    return True


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _root(root: Optional[Path]) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("LINDOS_ROOT")
    return Path(env) if env else Path("/")


def _under(root: Path, path: str) -> Path:
    return root / path.lstrip("/")


def _read_small(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_ENTRY_READ_LIMIT)
    except OSError:
        return None


def _is_masked(mask: Path) -> bool:
    try:
        if mask.is_symlink():
            return os.readlink(mask) == "/dev/null"
        if mask.is_file():
            return mask.stat().st_size == 0  # an empty override also disables the vendor rule
    except OSError:
        return False
    return False


def _conf_interpreter(conf: Path) -> Optional[str]:
    text = _read_small(conf)
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        fields = line.split(line[0])
        if len(fields) >= 7 and fields[1] == BINFMT_NAME:
            return fields[6]
    return None


def status(root: Optional[Path] = None) -> Dict[str, object]:
    """``{"registered","enabled","masked","conflicts":[{"name","interpreter"}],"interpreter","note"}``.

    Additional keys: ``available`` (binfmt_misc mounted), ``global_enabled``, ``installed``
    (the vendor .conf exists), ``binfmt_support`` (Debian's update-binfmts is installed).
    """
    base = _root(root)
    proc = _under(base, PROC_DIR)
    conf = _under(base, CONF_PATH)
    mask = _under(base, MASK_PATH)
    available = proc.is_dir() and (proc / "register").exists()
    global_text = _read_small(proc / "status") if available else None
    global_enabled = available and (global_text or "enabled").strip() == "enabled"
    installed = conf.is_file()
    masked = _is_masked(mask)
    binfmt_support = _under(base, "/usr/sbin/update-binfmts").exists()

    own: Optional[Dict[str, object]] = None
    conflicts: List[Dict[str, str]] = []
    if available:
        try:
            names = sorted(os.listdir(proc))[:_MAX_ENTRIES]
        except OSError:
            names = []
        for name in names:
            if name in _PROC_SKIP:
                continue
            text = _read_small(proc / name)
            if text is None:
                continue
            entry = parse_proc_entry(text)
            if name == BINFMT_NAME:
                own = entry
                continue
            if entry.get("enabled") and entry_matches_mz(entry):
                conflicts.append({"name": name, "interpreter": str(entry.get("interpreter") or ""),
                                  "managed_by": ("binfmt-support"
                                                 if _under(base, f"/var/lib/binfmts/{name}").exists() else "")})
    registered = own is not None
    enabled = bool(registered and own and own.get("enabled") and global_enabled)
    interpreter = str((own or {}).get("interpreter") or _conf_interpreter(conf) or INTERPRETER)
    note = _note(available=available, global_enabled=global_enabled, installed=installed, masked=masked,
                 registered=registered, enabled=enabled, conflicts=conflicts, binfmt_support=binfmt_support)
    return {"registered": registered, "enabled": enabled, "masked": masked, "conflicts": conflicts,
            "interpreter": interpreter, "note": note, "available": available, "global_enabled": global_enabled,
            "installed": installed, "binfmt_support": binfmt_support}


def _note(*, available: bool, global_enabled: bool, installed: bool, masked: bool, registered: bool,
          enabled: bool, conflicts: List[Dict[str, str]], binfmt_support: bool) -> str:
    if not available:
        return ("The kernel's binfmt_misc feature is not available (not mounted at /proc/sys/fs/binfmt_misc), "
                "so ./program.exe cannot start from a terminal. Double-clicking still works.")
    if not global_enabled:
        return "binfmt_misc is switched off system-wide, so ./program.exe cannot start from a terminal."
    if masked:
        return "Running .exe files from the terminal is turned off (turn it on: lindos-compat binfmt enable)."
    if not installed and not registered:
        return "The lindos-pe rule is not installed (reinstall the lindos-compat package)."
    if not registered:
        return "Turned on, but not active yet (restart, or run: lindos-compat binfmt enable)."
    if not enabled:
        return "The lindos-pe rule is loaded but disabled (turn it on: lindos-compat binfmt enable)."
    if conflicts:
        names = ", ".join(f"'{c['name']}'" for c in conflicts)
        text = (f"Another handler ({names}) also claims Windows programs. The kernel uses whichever was "
                "registered last, so ./program.exe may start that one instead of Lindos.")
        if any(c.get("managed_by") == "binfmt-support" for c in conflicts):
            text += " At start-up binfmt-support registers its handlers after Lindos, so they usually win."
        return text + " Lindos does not change other handlers."
    return "./program.exe in a terminal starts Windows programs through Lindos."


# ---------------------------------------------------------------------------
# commands (for the privileged helper action ``set-binfmt``)
# ---------------------------------------------------------------------------


def enable_commands() -> List[List[str]]:
    """Commands (run as root, in order) that turn the terminal ``.exe`` support on.  Idempotent."""
    return [["rm", "-f", MASK_PATH], [SYSTEMD_BINFMT, CONF_PATH]]


def disable_commands() -> List[List[str]]:
    """Commands (run as root, in order) that turn it off persistently and remove the live entry.

    The last one removes ``/proc/sys/fs/binfmt_misc/lindos-pe`` only when it exists (a missing
    entry is not an error); it is a fixed ``sh -c`` script without any user input.
    """
    entry = f"{PROC_DIR}/{BINFMT_NAME}"
    return [["mkdir", "-p", os.path.dirname(MASK_PATH)],
            ["ln", "-sf", "/dev/null", MASK_PATH],
            ["sh", "-c", f"[ ! -e {entry} ] || echo -1 > {entry}"]]


def remove_runtime_entry(root: Optional[Path] = None) -> bool:
    """Python equivalent of the last disable command: write ``-1`` to the live entry.

    Returns True when an entry was removed, False when there was none (ENOENT is ignored).
    Other errors (EPERM when not root) propagate as OSError.
    """
    entry = _under(_root(root), PROC_DIR) / BINFMT_NAME
    try:
        with open(entry, "w", encoding="ascii") as fh:
            fh.write("-1")
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------------------
# wrapper re-check
# ---------------------------------------------------------------------------


def check_program(path: Path) -> Tuple[bool, str]:
    """May the ``lindos-binfmt`` wrapper start ``path``?  ``(ok, message)``.

    The ``MZ`` rule also catches executable-bit DLLs, drivers, EFI files and Control Panel
    items (common on NTFS mounts); those are refused with the explanation lindos-run would give.
    """
    from . import formats  # lazy: keeps `status` usable even if formats cannot import

    det = formats.detect(Path(path))
    if det.format.id in RUNNABLE_FORMATS:
        return True, f"{det.format.label} ({det.reason})"
    plan = formats.plan_action(Path(path), det, wine_mode="unknown")
    message = plan.message if plan.handler == "explain" and plan.message else (
        f"“{Path(path).name}” is a {det.format.label.lower()} ({det.reason}), not a program that can be "
        "started from the terminal. Open it with: lindos-run <file>")
    return False, message


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m lindos_compat.binfmt",
                                     description="lindos-pe binfmt_misc status and the wrapper's file check.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="show whether ./program.exe works in a terminal")
    st.add_argument("--json", action="store_true")
    st.add_argument("--root", help=argparse.SUPPRESS)
    ck = sub.add_parser("check", help="exit 0 if FILE is a program the wrapper may start, else 3")
    ck.add_argument("file")
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if ns.cmd == "status":
        data = status(Path(ns.root) if ns.root else None)
        if ns.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            print(data["note"])
        return 0
    ok, message = check_program(Path(ns.file))
    if ok:
        return 0
    sys.stderr.write(message + "\n")
    return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
