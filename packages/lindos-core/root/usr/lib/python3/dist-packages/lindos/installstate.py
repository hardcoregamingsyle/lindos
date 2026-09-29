"""What the installer did - and what it could not do (``/var/lib/lindos/install-state.json``).

The Lindos installer (a Ubiquity target-config hook) does every heavy job while installing:
system updates, drivers, Chrome, Wine/gaming launchers, Flatpaks.  Anything it could not finish
(offline, timeout, failure) is recorded here so the installed system can retry silently and
Settings > Apps can show an "Install now" button.  Nothing here installs anything.

File format (schema 1)::

    {"schema": 1, "updated": "<ISO-8601 UTC>", "online": true | false | null,
     "steps": {"<step>": {"status": "done|pending|skipped|failed",
                          "detail": "<short text>", "time": "<ISO-8601 UTC>"}}}

Step ids: ``updates drivers browser compat gaming mode_extras flatpaks``.  ``done`` and
``skipped`` are terminal; ``pending`` and ``failed`` are what a retry picks up.

Command line (used by shell scripts; honours ``LINDOS_ROOT``)::

    python3 -m lindos.installstate [--root DIR] show
    python3 -m lindos.installstate [--root DIR] mark STEP STATUS [DETAIL]
    python3 -m lindos.installstate [--root DIR] pending
    python3 -m lindos.installstate [--root DIR] status STEP
    python3 -m lindos.installstate [--root DIR] online true|false|unknown

``--root DIR`` is a prefix (the installer passes ``--root /target``).  Exit codes: 0 ok, 1 the file
cannot be written, 2 unknown step/status or bad usage.  Reading is tolerant: a missing or corrupt
file is an empty state.  Standard library only; importable on any OS.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from . import config as lconfig
from . import paths

SCHEMA = 1
STEPS = ("updates", "drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks")
STATUSES = ("done", "pending", "skipped", "failed")
TERMINAL = ("done", "skipped")
RETRYABLE = ("pending", "failed")
DETAIL_MAX = 200
LOCK_WAIT_SECONDS = 10.0


class StateError(ValueError):
    """An unknown step id or status was given to :func:`mark`."""


# --- locations / time -------------------------------------------------------------------
def state_path(root: Optional[str] = None) -> str:
    """The state file: under *root* when given (``/target``), else ``LINDOS_ROOT``-aware."""
    if root:
        return os.path.normpath(os.path.join(str(root), paths.INSTALL_STATE.lstrip("/")))
    return paths.resolve(paths.INSTALL_STATE)


def now_iso() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- reading ------------------------------------------------------------------------------
def empty_state() -> Dict[str, Any]:
    return {"schema": SCHEMA, "updated": "", "online": None, "steps": {}}


def _clean(raw: Any) -> Dict[str, Any]:
    """Normalise a decoded file: keep only well-formed fields, drop what cannot be trusted."""
    state = empty_state()
    if not isinstance(raw, dict):
        return state
    if isinstance(raw.get("updated"), str):
        state["updated"] = raw["updated"]
    if isinstance(raw.get("online"), bool):
        state["online"] = raw["online"]
    steps = raw.get("steps")
    if isinstance(steps, dict):
        for sid, entry in steps.items():
            if not isinstance(sid, str) or not isinstance(entry, dict) or entry.get("status") not in STATUSES:
                continue
            detail = entry.get("detail")
            stamp = entry.get("time")
            state["steps"][sid] = {
                "status": entry["status"],
                "detail": detail if isinstance(detail, str) else "",
                "time": stamp if isinstance(stamp, str) else "",
            }
    return state


def load(root: Optional[str] = None) -> Dict[str, Any]:
    """The state as a dict; an empty state when the file is missing or corrupt.  Never raises."""
    return _clean(lconfig.read_json(state_path(root)))


def status(step: str, root: Optional[str] = None) -> str:
    """``done``/``pending``/``skipped``/``failed``, or ``""`` when nothing is recorded for *step*."""
    entry = load(root)["steps"].get(step)
    return entry["status"] if entry else ""


def pending(root: Optional[str] = None) -> List[str]:
    """Steps that are ``pending`` or ``failed`` (known ids in canonical order first)."""
    steps = load(root)["steps"]
    order = [s for s in STEPS if s in steps] + sorted(s for s in steps if s not in STEPS)
    return [s for s in order if steps[s]["status"] in RETRYABLE]


def is_terminal(step: str, root: Optional[str] = None) -> bool:
    """True when *step* is ``done`` or ``skipped`` (nothing left to retry)."""
    return status(step, root) in TERMINAL


# --- writing ------------------------------------------------------------------------------
@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """Best-effort exclusive lock so two first-boot services never lose each other's update.

    POSIX only (``fcntl``); waits at most :data:`LOCK_WAIT_SECONDS`, then proceeds unlocked - a
    stuck holder must never hang a boot.  The lock file stays next to the state file.
    """
    handle = None
    try:
        try:
            import fcntl  # POSIX only
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            handle = open(path + ".lock", "a")
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
        except (ImportError, OSError):
            pass
        yield
    finally:
        if handle is not None:
            try:
                handle.close()  # also releases the lock
            except OSError:
                pass


def _short(detail: Any) -> str:
    text = " ".join(str(detail or "").split())
    return text[:DETAIL_MAX]


def _write(state: Dict[str, Any], path: str) -> None:
    state["schema"] = SCHEMA
    state["updated"] = now_iso()
    lconfig.atomic_write_json(path, state)


def mark(step: str, status: str, detail: str = "", root: Optional[str] = None) -> Dict[str, Any]:
    """Record *status* for *step* (atomic write) and return the new state.

    Raises :class:`StateError` for an unknown step id or status and ``OSError`` when the file
    cannot be written.  *detail* is one short line (whitespace collapsed, cut at 200 chars).
    """
    if step not in STEPS:
        raise StateError(f"unknown install step {step!r} (known: {', '.join(STEPS)})")
    if status not in STATUSES:
        raise StateError(f"unknown status {status!r} (known: {', '.join(STATUSES)})")
    path = state_path(root)
    with _locked(path):
        state = load(root)
        state["steps"][step] = {"status": status, "detail": _short(detail), "time": now_iso()}
        _write(state, path)
    return state


def set_online(online: Optional[bool], root: Optional[str] = None) -> Dict[str, Any]:
    """Record whether the installer had a working internet connection (``None`` = unknown)."""
    if online is not None and not isinstance(online, bool):
        raise StateError("online must be true, false or None")
    path = state_path(root)
    with _locked(path):
        state = load(root)
        state["online"] = online
        _write(state, path)
    return state


# --- command line ---------------------------------------------------------------------------
def _parse_online(text: str) -> Optional[bool]:
    low = text.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    if low in ("unknown", "null", "none", ""):
        return None
    raise StateError(f"online must be true, false or unknown, not {text!r}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m lindos.installstate",
                                 description="Read or update the Lindos install-state record.")
    ap.add_argument("--root", default=None, metavar="DIR",
                    help="prefix for the state file (the installer uses /target); default: LINDOS_ROOT or /")
    sub = ap.add_subparsers(dest="command", metavar="command")
    sub.add_parser("show", help="print the state as JSON")
    p = sub.add_parser("mark", help="record a step outcome")
    p.add_argument("step", help="one of: " + " ".join(STEPS))
    p.add_argument("status", help="one of: " + " ".join(STATUSES))
    p.add_argument("detail", nargs="*", help="short free text")
    sub.add_parser("pending", help="print the pending/failed steps, one per line")
    p = sub.add_parser("status", help="print the status of one step (empty when unknown)")
    p.add_argument("step")
    p = sub.add_parser("online", help="record whether the installer was online")
    p.add_argument("value", help="true, false or unknown")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(newline="\n", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # no CRLF from a Windows text-mode stdout: shell callers compare exact words
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if not ns.command:
        parser.print_help()
        return 2
    root = ns.root or None
    try:
        if ns.command == "show":
            print(json.dumps(load(root), indent=2, sort_keys=True, ensure_ascii=False))
        elif ns.command == "mark":
            mark(ns.step, ns.status, " ".join(ns.detail), root)
        elif ns.command == "pending":
            for step in pending(root):
                print(step)
        elif ns.command == "status":
            print(status(ns.step, root))
        elif ns.command == "online":
            set_online(_parse_online(ns.value), root)
    except StateError as exc:
        sys.stderr.write(f"lindos.installstate: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"lindos.installstate: cannot write {state_path(root)}: {exc}\n")
        return 1
    return 0


__all__ = [
    "SCHEMA", "STEPS", "STATUSES", "TERMINAL", "RETRYABLE", "DETAIL_MAX", "StateError",
    "state_path", "now_iso", "empty_state", "load", "status", "pending", "is_terminal",
    "mark", "set_online", "build_parser", "main",
]


if __name__ == "__main__":
    sys.exit(main())
