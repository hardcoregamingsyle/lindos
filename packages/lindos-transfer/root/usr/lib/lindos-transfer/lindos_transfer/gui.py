"""``lindos-transfer-gui`` -- the GTK wizard for lindos-transfer (SPEC-WINDOWS Addendum W, §29.12).

Pages (a Windows-Easy-Transfer-style wizard): Welcome -> Source (detected partitions/bundles with
their notes; "Use a transfer folder..."; "Make a transfer USB for another PC...") -> User -> What
to bring (checkboxes + sizes; Firefox passwords opt-in) -> Apps (per-app choice from ``actions``)
-> Transfer (live progress) -> Done (report, "Open folder").

This module never imports the rest of ``lindos_transfer`` (that package is owned by a different
engineer and is being written concurrently).  Instead it drives the installed ``lindos-transfer``
command exactly as a user would from a terminal: every read uses ``--json`` (:class:`TransferCli`),
the long-running ``run``/``install-apps`` steps stream ``--json-progress`` lines
(:func:`parse_progress_line`), and the CLI's argv/JSON contract (SPEC-WINDOWS §29.2-29.7, §29.11) is
duplicated here as plain constants so this file has nothing to fall over if the sibling modules
change shape while both are being written.  ``TransferCli`` takes its ``run``/``popen``/``which``
callables as constructor arguments precisely so tests can replace them with fakes (SPEC-WINDOWS
hard rule: "mock them in your tests" for another owner's API) -- nothing here ever calls
``subprocess`` directly outside that class.

GTK is imported the way every other Lindos GUI does it (``packages/lindos-setup``,
``packages/lindos-settings``): a plain, unguarded ``import gi`` at module scope.  This keeps the
module importable in three situations without any special-casing here:

* a real desktop with PyGObject + GTK 3 installed;
* this repository's test suite, where ``tests/lindos_testsupport.py`` installs a permissive stub
  ``gi`` module whenever the real one is missing (Windows/macOS developer machines, bare CI
  interpreters) so the pure-logic pieces below can be unit-tested without a display;
* a Linux box with Python but no GTK typelibs at all, where the import raises -- the caller
  (``root/usr/bin/lindos-transfer-gui``) catches that and prints a plain-language error instead of
  a traceback, the same way ``lindos-setup/main.py`` and ``lindos-settings`` do.

Nothing at import time touches a display, spawns a process or reads a file: :func:`run_app` is the
only function that calls ``Gtk.init_check()``, and ``TransferCli`` only runs a command when one of
its methods is called.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

__version__ = "1.0.0"

log = logging.getLogger("lindos-transfer.gui")

__all__ = [
    "CATEGORIES", "FOLDER_CATEGORIES", "LABELS", "PAGE_IDS", "PAGE_TITLES",
    "format_size", "format_count",
    "CliError", "TransferCli", "parse_progress_line",
    "plan_items", "item_label", "set_item_selected", "set_category_selected", "selected_totals",
    "plan_apps", "set_app_selected", "set_app_chosen", "app_action_summary", "plan_has_selection",
    "ACTION_TYPE_TEXT", "action_type_text",
    "WizardState",
    "run_exit_ok",
    "TransferWizard", "run_app", "build_arg_parser", "main",
]

# ==================================================================================================
# The lindos-transfer CLI contract (SPEC-WINDOWS §29.2, §29.5, §29.6, §29.7).  Kept as plain data,
# not imported from ``lindos_transfer.plan``/``apps``, so this file has one source of truth for what
# it expects on the wire and never breaks merely because a sibling module's Python API changed.
# ==================================================================================================
DEFAULT_CLI = "lindos-transfer"

#: SPEC-WINDOWS §29.5 category ids, binding, in this order.
CATEGORIES: Tuple[str, ...] = (
    "desktop", "documents", "downloads", "music", "pictures", "videos", "saved-games", "favorites",
    "onedrive", "bookmarks", "firefox", "wallpaper", "fonts", "wifi", "apps", "steam-games",
)
FOLDER_CATEGORIES: Tuple[str, ...] = (
    "desktop", "documents", "downloads", "music", "pictures", "videos", "saved-games", "favorites",
    "onedrive",
)
LABELS: Dict[str, str] = {
    "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "music": "Music",
    "pictures": "Pictures", "videos": "Videos", "saved-games": "Saved games",
    "favorites": "Favorites (Internet Explorer / old Edge)", "onedrive": "OneDrive",
    "bookmarks": "Browser bookmarks", "firefox": "Firefox bookmarks and history",
    "wallpaper": "Desktop wallpaper", "fonts": "Your fonts", "wifi": "Wi-Fi networks",
    "apps": "List of installed apps", "steam-games": "Steam games",
}
#: Categories that are opt-in (unticked) the first time a plan is shown (SPEC-WINDOWS §27.3, §29.9:
#: nothing large or sensitive is selected without the user asking).
OPT_IN_CATEGORIES = frozenset({"steam-games"})

#: ``--json-progress`` event names (SPEC-WINDOWS §29.7, binding).
PROGRESS_EVENTS = frozenset({"start", "item", "file", "progress", "skip", "error", "done"})

#: Plain-language text for each SPEC-WINDOWS §29.9 app action ``type``.
ACTION_TYPE_TEXT: Dict[str, str] = {
    "builtin": "Already included in Lindos",
    "apt": "Installed from Lindos (a native package)",
    "flatpak": "Installed from Lindos (a Flatpak)",
    "browser": "Installed as a Linux browser",
    "launcher": "Installed as a game launcher",
    "recipe": "Installed with Lindos' Windows-app support",
    "winget": "Installed with winget, through Lindos' Windows-app support",
    "web": "Added as a shortcut to the website (no install needed)",
    "winapps": "Opened from your own licensed Windows over Lindos WinApps",
    "vm": "Opened in the Lindos Windows VM",
    "not_possible": "Cannot run on Linux (see the game routes in Lindos Settings > Gaming)",
}

#: Wizard page ids, in order (SPEC-WINDOWS §29.12).
PAGE_IDS: Tuple[str, ...] = ("welcome", "source", "user", "bring", "apps", "transfer", "done")
PAGE_TITLES: Dict[str, str] = {
    "welcome": "Bring your stuff from Windows",
    "source": "Where are your Windows files?",
    "user": "Whose files?",
    "bring": "What to bring",
    "apps": "Your apps",
    "transfer": "Transferring your files",
    "done": "Done",
}

RUN_TIMEOUT = 6 * 3600            # a big Steam/OneDrive copy can run for hours
JSON_TIMEOUT = 180.0              # sources/users/plan/apps/report are quick reads


def run_exit_ok(code: Optional[int]) -> bool:
    """Whether *code* -- the exit status of ``lindos-transfer run --json-progress`` -- means success.

    ``lindos-transfer``'s own exit-code contract (SPEC-WINDOWS §29.2) is ``0`` ok, ``1`` error,
    ``2`` usage, ``4`` nothing to transfer: unlike the Windows-side robocopy kit (where exit codes
    below 8 mean success), *any* non-zero code from this Linux CLI means something went wrong, so
    this is a plain ``code in (0, None)`` check, not a bitmask threshold.
    """
    return code is None or code == 0


# ==================================================================================================
# formatting (no Gtk; used by both the pure logic and the widgets below)
# ==================================================================================================
def format_size(num_bytes: Any) -> str:
    """``1536`` -> ``"1.5 KB"``, matching the plain language the Windows-side kit uses."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "0 bytes"
    n = max(0.0, n)
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:,.1f} {unit}" if unit == "GB" else f"{n / size:,.0f} {unit}"
    return f"{int(n):,} bytes"


def format_count(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def action_type_text(kind: Any) -> str:
    if not kind:
        return "Unknown"
    return ACTION_TYPE_TEXT.get(str(kind), str(kind))


# ==================================================================================================
# TransferCli -- subprocess wrapper around `lindos-transfer` (no other module talks to it)
# ==================================================================================================
class CliError(Exception):
    """``lindos-transfer`` exited non-zero, is not installed, or its output was not the JSON the
    §29.2/§29.7 contract promises.  ``message`` is always plain language, safe to show as-is."""

    def __init__(self, message: str, *, code: int = 1, argv: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.argv = list(argv)


RunFunc = Callable[..., "subprocess.CompletedProcess[str]"]
PopenFunc = Callable[..., "subprocess.Popen[str]"]
WhichFunc = Callable[[str], Optional[str]]


def _first_error_line(out: str, err: str) -> str:
    for text in (err, out):
        for line in (text or "").splitlines():
            line = line.strip()
            if line:
                return line
    return "lindos-transfer failed with no further message"


class TransferCli:
    """Everything the wizard needs from ``lindos-transfer`` (SPEC-WINDOWS §29.2), as plain method
    calls.  Every read command (``sources``, ``users``, ``plan``, ``apps``, ``report``) is invoked
    with ``--json`` and parsed; ``stream_run``/``stream_install_apps`` run the long copy/install
    steps and hand each ``--json-progress`` event to a callback so a caller (always a worker
    thread here -- see :meth:`TransferWizard._call_async`) can update the UI as it happens.

    ``run``/``popen``/``which`` are injected (defaulting to the real ``subprocess``/``shutil``
    ones) so every method here is testable without spawning a real process.
    """

    def __init__(self, exe: Optional[str] = None, *, run: RunFunc = subprocess.run,
                 popen: PopenFunc = subprocess.Popen, which: WhichFunc = shutil.which) -> None:
        self._which = which
        self.exe = exe or which(DEFAULT_CLI) or DEFAULT_CLI
        self._run = run
        self._popen = popen

    def available(self) -> bool:
        return bool(self._which(self.exe) or os.path.isabs(self.exe) and os.path.isfile(self.exe))

    # -- reads (--json) --------------------------------------------------------------------------
    def _json(self, args: Sequence[str], *, timeout: float = JSON_TIMEOUT) -> Any:
        argv = [self.exe, *args, "--json"]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise CliError(f"{self.exe} is not installed. Install the lindos-transfer package.",
                           code=127, argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError(f"{' '.join(argv)} took too long and was given up on.", code=124, argv=argv) from exc
        except OSError as exc:
            raise CliError(str(exc), argv=argv) from exc
        out, err, code = proc.stdout or "", proc.stderr or "", int(proc.returncode)
        if code != 0:
            raise CliError(_first_error_line(out, err), code=code, argv=argv)
        try:
            return json.loads(out)
        except ValueError as exc:
            raise CliError(f"{self.exe} produced output that was not valid JSON ({exc}).", code=code, argv=argv) from exc

    def sources(self) -> Dict[str, Any]:
        """``lindos-transfer sources --json`` -> ``{"partitions":[...], "bundles":[...]}``."""
        data = self._json(["sources"])
        if not isinstance(data, dict):
            raise CliError("lindos-transfer sources did not return the expected object.")
        data.setdefault("partitions", [])
        data.setdefault("bundles", [])
        return data

    def users(self, path: str) -> List[Dict[str, Any]]:
        """``lindos-transfer users --from <path> --json`` -> a list of user dicts.

        Accepts either the plain list or ``{"users": [...]}`` -- the exact envelope is not pinned
        down anywhere else yet, and either is easy to normalise here.
        """
        data = self._json(["users", "--from", path])
        if isinstance(data, dict):
            data = data.get("users", [])
        if not isinstance(data, list):
            raise CliError("lindos-transfer users did not return a list of users.")
        out: List[Dict[str, Any]] = []
        for entry in data:
            if isinstance(entry, dict):
                out.append(entry)
            elif isinstance(entry, str):
                out.append({"name": entry})
        return out

    def plan(self, *, path: str, user: Optional[str] = None, only: Optional[Sequence[str]] = None,
             exclude: Optional[Sequence[str]] = None, dest: Optional[str] = None,
             firefox_passwords: bool = False, timeout: float = 600.0) -> Dict[str, Any]:
        """``lindos-transfer plan --from <path> ... --json`` -> the schema-1 plan (SPEC-WINDOWS §29.6)."""
        args: List[str] = ["plan", "--from", path]
        if user:
            args += ["--user", user]
        if only:
            args += ["--only", ",".join(only)]
        if exclude:
            args += ["--exclude", ",".join(exclude)]
        if dest:
            args += ["--dest", dest]
        if firefox_passwords:
            args.append("--firefox-passwords")
        data = self._json(args, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise CliError("lindos-transfer plan did not return a valid plan.")
        data.setdefault("apps", [])
        data.setdefault("skipped", [])
        data.setdefault("warnings", [])
        return data

    def apps(self, *, path: str, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """``lindos-transfer apps --from <path> --json`` -> the app list (also embedded in a plan)."""
        args = ["apps", "--from", path]
        if user:
            args += ["--user", user]
        data = self._json(args)
        if isinstance(data, dict):
            data = data.get("apps", [])
        if not isinstance(data, list):
            raise CliError("lindos-transfer apps did not return a list of apps.")
        return data

    def report(self) -> Optional[Dict[str, Any]]:
        """``lindos-transfer report --json`` -> the last transfer's report, or ``None`` (exit 4:
        nothing to report yet)."""
        argv = [self.exe, "report", "--json"]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=JSON_TIMEOUT, check=False,
                             stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise CliError(f"{self.exe} is not installed.", code=127, argv=argv) from exc
        except OSError as exc:
            raise CliError(str(exc), argv=argv) from exc
        if proc.returncode == 4:
            return None
        if proc.returncode != 0:
            raise CliError(_first_error_line(proc.stdout or "", proc.stderr or ""), code=proc.returncode, argv=argv)
        try:
            data = json.loads(proc.stdout or "")
        except ValueError as exc:
            raise CliError(f"lindos-transfer report produced output that was not valid JSON ({exc}).", argv=argv) from exc
        return data if isinstance(data, dict) else None

    def make_usb_kit(self, directory: str, *, timeout: float = 120.0) -> None:
        """``lindos-transfer make-usb-kit <dir>`` -- copies the Windows-side kit there. Raises on failure."""
        argv = [self.exe, "make-usb-kit", directory]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                             stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise CliError(f"{self.exe} is not installed.", code=127, argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError("Making the transfer USB kit took too long and was given up on.", code=124, argv=argv) from exc
        except OSError as exc:
            raise CliError(str(exc), argv=argv) from exc
        if proc.returncode != 0:
            raise CliError(_first_error_line(proc.stdout or "", proc.stderr or ""), code=proc.returncode, argv=argv)

    # -- long-running, streamed (--json-progress) ------------------------------------------------
    def _stream(self, argv: List[str], *, on_event: Optional[Callable[[Dict[str, Any]], None]],
                on_line: Optional[Callable[[str], None]]) -> int:
        try:
            proc = self._popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               bufsize=1, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            if on_line is not None:
                on_line(f"error: {self.exe} is not installed.")
            return 127
        except OSError as exc:
            if on_line is not None:
                on_line(f"error: {exc}")
            return 1
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            for raw in stream:
                line = raw.rstrip("\n")
                event = parse_progress_line(line) if on_event is not None else None
                if event is not None:
                    on_event(event)
                elif on_line is not None:
                    on_line(line)
        code = proc.wait()
        return int(code) if code is not None else 1

    def stream_run(self, *, plan_path: str, dry_run: bool = False,
                   on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                   on_line: Optional[Callable[[str], None]] = None) -> int:
        """``lindos-transfer run --plan <plan_path> --yes --json-progress`` (SPEC-WINDOWS §29.7).

        Runs in the *calling* thread and blocks until the copy finishes -- always call this from a
        worker thread (see :meth:`TransferWizard._call_async`) and marshal the callbacks back onto
        the GTK main loop from there, never from inside this method.
        """
        argv = [self.exe, "run", "--plan", plan_path, "--yes", "--json-progress"]
        if dry_run:
            argv.append("--dry-run")
        return self._stream(argv, on_event=on_event, on_line=on_line)

    def stream_install_apps(self, *, plan_path: str, dry_run: bool = False,
                            on_line: Optional[Callable[[str], None]] = None) -> int:
        """``lindos-transfer install-apps --plan <plan_path> --yes`` -- plain log lines (SPEC-WINDOWS
        §29.2 does not define ``--json-progress`` for this command)."""
        argv = [self.exe, "install-apps", "--plan", plan_path, "--yes"]
        if dry_run:
            argv.append("--dry-run")
        return self._stream(argv, on_event=None, on_line=on_line)


def parse_progress_line(line: str) -> Optional[Dict[str, Any]]:
    """One line of ``--json-progress`` output -> its event dict, or ``None`` for anything that is
    not one (a blank line, a plain log line that slipped onto stdout, malformed JSON, JSON that is
    not an object, or an object whose ``event`` is not one of the SPEC-WINDOWS §29.7 names).
    """
    text = line.strip()
    if not text or text[0] != "{":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("event") not in PROGRESS_EVENTS:
        return None
    return data


# ==================================================================================================
# plan / apps helpers (pure dict manipulation -- what the "What to bring" and "Apps" pages need)
# ==================================================================================================
def plan_items(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(plan.get("items") or [])


def item_label(item: Dict[str, Any]) -> str:
    label = str(item.get("label") or LABELS.get(str(item.get("category")), item.get("category", "")))
    return label


def set_item_selected(plan: Dict[str, Any], item_id: str, selected: bool) -> bool:
    """Tick/untick one item by id.  Returns whether an item with that id was found."""
    for item in plan_items(plan):
        if item.get("id") == item_id:
            item["selected"] = bool(selected)
            return True
    return False


def set_category_selected(plan: Dict[str, Any], category: str, selected: bool) -> int:
    """Tick/untick every item of *category* (there can be more than one ``onedrive`` item).
    Returns how many items were changed."""
    changed = 0
    for item in plan_items(plan):
        if item.get("category") == category:
            item["selected"] = bool(selected)
            changed += 1
    return changed


def selected_totals(plan: Dict[str, Any]) -> Tuple[int, int]:
    """``(files, bytes)`` summed over every ticked item."""
    files = sum(int(it.get("files") or 0) for it in plan_items(plan) if it.get("selected"))
    total_bytes = sum(int(it.get("bytes") or 0) for it in plan_items(plan) if it.get("selected"))
    return files, total_bytes


def plan_apps(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(plan.get("apps") or [])


def set_app_selected(plan: Dict[str, Any], index: int, selected: bool) -> bool:
    apps = plan_apps(plan)
    if not 0 <= index < len(apps):
        return False
    plan["apps"][index]["selected"] = bool(selected)
    return True


def set_app_chosen(plan: Dict[str, Any], index: int, action_index: int) -> bool:
    apps = plan_apps(plan)
    if not 0 <= index < len(apps):
        return False
    actions = apps[index].get("actions") or []
    if not 0 <= action_index < len(actions):
        return False
    plan["apps"][index]["chosen"] = int(action_index)
    return True


def app_action_summary(app: Dict[str, Any]) -> str:
    """One line describing what will happen to this app, for the chosen action (or the first one)."""
    actions = app.get("actions") or []
    if not actions:
        return "No route known for this app yet."
    idx = app.get("chosen")
    idx = idx if isinstance(idx, int) and 0 <= idx < len(actions) else 0
    act = actions[idx]
    label = str(act.get("label") or "")
    kind = action_type_text(act.get("type"))
    return f"{label} - {kind}" if label else kind


def plan_has_selection(plan: Optional[Dict[str, Any]]) -> bool:
    if not plan:
        return False
    if any(it.get("selected") for it in plan_items(plan)):
        return True
    return any(app.get("selected") for app in plan_apps(plan))


# ==================================================================================================
# WizardState -- the page state machine (no Gtk; fully unit-testable)
# ==================================================================================================
@dataclass
class WizardState:
    """Everything collected while stepping through the wizard, plus the navigation rules.

    One instance lives for the whole run of the wizard; :class:`TransferWizard` owns it and never
    duplicates its logic in the widget callbacks -- every button asks *this* whether it may move.
    """

    index: int = 0
    sources: Optional[Dict[str, Any]] = None
    source_path: str = ""
    source_kind: str = ""              # "partition" | "bundle"
    source_note: str = ""
    users: List[Dict[str, Any]] = field(default_factory=list)
    user_name: str = ""
    firefox_passwords: bool = False
    plan: Optional[Dict[str, Any]] = None
    plan_path: str = ""
    transfer_running: bool = False
    transfer_done: bool = False
    transfer_failed: bool = False
    transfer_error: str = ""
    install_apps_after: bool = False
    report: Optional[Dict[str, Any]] = None

    # -- navigation ---------------------------------------------------------------------------
    @property
    def page(self) -> str:
        return PAGE_IDS[self.index]

    def can_go_back(self) -> bool:
        if self.index <= 0:
            return False
        if self.page == "transfer" and self.transfer_running:
            return False
        return True

    def can_go_next(self) -> bool:
        page = self.page
        if page == "welcome":
            return True
        if page == "source":
            return bool(self.source_path)
        if page == "user":
            return bool(self.user_name)
        if page == "bring":
            return self.plan is not None
        if page == "apps":
            return self.plan is not None
        if page == "transfer":
            return self.transfer_done
        return False       # "done": Finish, not Next

    def go_next(self) -> bool:
        """Advance one page.  Does not touch ``plan``/``users`` -- picking a *different* source or
        user is what invalidates them (see ``reset_for_new_source``, called by the page that
        notices the change), so simply revisiting a page you already answered reuses what was
        already fetched instead of re-running the CLI for nothing."""
        if not self.can_go_next() or self.index >= len(PAGE_IDS) - 1:
            return False
        self.index += 1
        return True

    def go_back(self) -> bool:
        if not self.can_go_back():
            return False
        self.index -= 1
        return True

    def reset_for_new_source(self) -> None:
        self.users = []
        self.user_name = ""
        self.plan = None
        self.plan_path = ""

    # -- selections -----------------------------------------------------------------------------
    def selected_totals(self) -> Tuple[int, int]:
        return selected_totals(self.plan) if self.plan else (0, 0)

    def selected_app_count(self) -> int:
        if not self.plan:
            return 0
        return sum(1 for a in plan_apps(self.plan) if a.get("selected"))


# ==================================================================================================
# small Gtk helpers (kept minimal and local: this package has no dependency on any other Lindos
# GUI package's widget helpers, so it stays installable on its own)
# ==================================================================================================
def _box(orientation: Gtk.Orientation, spacing: int = 8) -> Gtk.Box:
    b = Gtk.Box(orientation=orientation, spacing=spacing)
    return b


def vbox(spacing: int = 8) -> Gtk.Box:
    return _box(Gtk.Orientation.VERTICAL, spacing)


def hbox(spacing: int = 8) -> Gtk.Box:
    return _box(Gtk.Orientation.HORIZONTAL, spacing)


def heading(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.get_style_context().add_class("title-3")
    return label


def subtext(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_line_wrap(True)
    label.get_style_context().add_class("dim-label")
    return label


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.set_vexpand(True)
    sw.add(child)
    return sw


# ==================================================================================================
# TransferWizard -- the window
# ==================================================================================================
class TransferWizard(Gtk.Window):
    """The wizard window: a :class:`Gtk.Stack` of seven pages plus a Back/Next/Cancel footer.

    Every call to :class:`TransferCli` that can block (anything other than a page's own widget
    setup) runs in a worker thread started by :meth:`_call_async`; the thread never touches a
    widget directly -- it only ever calls ``GLib.idle_add`` with a plain callback, exactly like
    ``lindos_settings.backend.Backend.stream`` does it.
    """

    def __init__(self, cli: TransferCli, *, state: Optional[WizardState] = None) -> None:
        super().__init__(title="Lindos Transfer")
        self.cli = cli
        self.state = state or WizardState()
        self.exit_code = 0
        self._busy = False

        self.set_default_size(760, 560)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("lindos-transfer")
        self.connect("destroy", self._on_destroy)

        outer = vbox(0)
        self.add(outer)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_vexpand(True)
        self.stack.set_border_width(24)
        outer.pack_start(self.stack, True, True, 0)

        self._pages: Dict[str, Dict[str, Gtk.Widget]] = {}
        for page_id, builder in (
            ("welcome", self._build_welcome), ("source", self._build_source),
            ("user", self._build_user), ("bring", self._build_bring),
            ("apps", self._build_apps), ("transfer", self._build_transfer),
            ("done", self._build_done),
        ):
            widgets: Dict[str, Gtk.Widget] = {}
            page = builder(widgets)
            self._pages[page_id] = widgets
            self.stack.add_named(page, page_id)

        footer = hbox(12)
        footer.set_border_width(12)
        outer.pack_end(footer, False, False, 0)
        self.back_btn = Gtk.Button(label="Back")
        self.back_btn.connect("clicked", lambda _b: self._go_back())
        footer.pack_start(self.back_btn, False, False, 0)
        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0.0)
        footer.pack_start(self.status_label, True, True, 0)
        self.cancel_btn = Gtk.Button(label="Close")
        self.cancel_btn.connect("clicked", lambda _b: self.close())
        footer.pack_end(self.cancel_btn, False, False, 0)
        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.get_style_context().add_class("suggested-action")
        self.next_btn.connect("clicked", lambda _b: self._go_next())
        footer.pack_end(self.next_btn, False, False, 0)

        self.show_page(self.state.page)

    # ---------------------------------------------------------------- worker-thread plumbing
    def _call_async(self, work: Callable[[], Any], on_done: Callable[[Optional[Any], Optional[Exception]], None],
                    *, busy_message: str = "Working...") -> None:
        """Run *work* in a daemon thread; ``on_done(result, error)`` is called back on the GTK main
        loop (via ``GLib.idle_add``) with exactly one of the two set."""
        self._set_busy(True, busy_message)

        def runner() -> None:
            result: Optional[Any] = None
            error: Optional[Exception] = None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the user, never crashes the app
                error = exc

            def deliver() -> bool:
                self._set_busy(False)
                on_done(result, error)
                return False

            GLib.idle_add(deliver)

        threading.Thread(target=runner, name="lindos-transfer-gui-worker", daemon=True).start()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.next_btn.set_sensitive(not busy and self.state.can_go_next())
        self.back_btn.set_sensitive(not busy and self.state.can_go_back())
        self.status_label.set_label(message if busy else "")

    def _error_dialog(self, text: str) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text="Something went wrong")
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    # ---------------------------------------------------------------- page switching
    def show_page(self, page_id: str) -> None:
        self.stack.set_visible_child_name(page_id)
        self.set_title(f"Lindos Transfer - {PAGE_TITLES.get(page_id, page_id)}")
        self.next_btn.set_label("Finish" if page_id == "done" else "Next")
        self.next_btn.set_visible(page_id != "done")
        self.back_btn.set_sensitive(self.state.can_go_back())
        self.next_btn.set_sensitive(self.state.can_go_next())
        entered = getattr(self, f"_enter_{page_id}", None)
        if entered is not None:
            entered()

    def _go_next(self) -> None:
        if self._busy:
            return
        leaving = self.state.page
        if not self.state.go_next():
            if leaving == "done":
                self.close()
            return
        self.show_page(self.state.page)

    def _go_back(self) -> None:
        if self._busy or not self.state.go_back():
            return
        self.show_page(self.state.page)

    # ================================================================ Welcome
    def _build_welcome(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Bring your stuff from Windows"), False, False, 0)
        box.pack_start(subtext(
            "This copies your personal files, browser bookmarks, wallpaper, fonts, Wi-Fi networks "
            "and a list of your apps from a Windows PC into your Lindos account. Nothing on the "
            "Windows side is ever changed, moved or deleted, and things such as saved browser "
            "passwords, cookies and payment details never transfer (Windows keeps them encrypted "
            "for that PC only)."), False, False, 0)
        box.pack_start(subtext(
            "You can bring files from the Windows partition on this same PC (if there is one), or "
            "from a transfer folder made by the LindosTransfer kit on another PC's USB stick."),
            False, False, 0)
        return box

    # ================================================================ Source
    def _build_source(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Where are your Windows files?"), False, False, 0)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        widgets["list"] = list_box
        box.pack_start(scrolled(list_box), True, True, 0)

        buttons = hbox(8)
        browse_btn = Gtk.Button(label="Use a transfer folder...")
        browse_btn.connect("clicked", lambda _b: self._browse_for_bundle())
        buttons.pack_start(browse_btn, False, False, 0)
        usb_btn = Gtk.Button(label="Make a transfer USB for another PC...")
        usb_btn.connect("clicked", lambda _b: self._make_usb_kit())
        buttons.pack_start(usb_btn, False, False, 0)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self._load_sources())
        buttons.pack_end(refresh_btn, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        return box

    def _enter_source(self) -> None:
        self._load_sources()

    def _load_sources(self) -> None:
        self._call_async(self.cli.sources, self._on_sources_loaded, busy_message="Looking for Windows files...")

    def _on_sources_loaded(self, result: Optional[Any], error: Optional[Exception]) -> None:
        list_box: Gtk.ListBox = self._pages["source"]["list"]  # type: ignore[assignment]
        for child in list(list_box.get_children()):
            list_box.remove(child)
        if error is not None:
            list_box.add(Gtk.Label(label=f"Could not look for Windows files: {error}"))
            list_box.show_all()
            return
        self.state.sources = result
        partitions = (result or {}).get("partitions") or []
        bundles = (result or {}).get("bundles") or []
        group = None
        if not partitions and not bundles:
            list_box.add(Gtk.Label(label="No Windows partition or transfer folder was found automatically. "
                                         "Use 'Use a transfer folder...' below to browse to one."))
        for part in partitions:
            row, radio = self._source_row(
                title=f"{part.get('label') or part.get('device') or 'Windows partition'} "
                      f"({format_size(part.get('size'))})",
                subtitle=str(part.get("note") or part.get("mountpoint") or ""),
                enabled=bool(part.get("windows")) and not part.get("bitlocker"),
                group=group)
            group = group or radio
            mountpoint = str(part.get("mountpoint") or "")
            note = str(part.get("note") or "")
            radio.connect("toggled", self._on_source_picked, mountpoint, "partition", note)
            list_box.add(row)
        for bundle in bundles:
            row, radio = self._source_row(
                title=f"Transfer folder: {bundle.get('computer') or bundle.get('user') or 'USB drive'}",
                subtitle=str(bundle.get("path") or ""), enabled=True, group=group)
            group = group or radio
            path = str(bundle.get("path") or "")
            radio.connect("toggled", self._on_source_picked, path, "bundle", "")
            list_box.add(row)
        list_box.show_all()

    def _source_row(self, *, title: str, subtitle: str, enabled: bool,
                    group: Optional[Gtk.RadioButton]) -> Tuple[Gtk.ListBoxRow, Gtk.RadioButton]:
        row = Gtk.ListBoxRow()
        line = hbox(8)
        line.set_border_width(6)
        radio = Gtk.RadioButton.new_from_widget(group)
        radio.set_sensitive(enabled)
        line.pack_start(radio, False, False, 0)
        text_box = vbox(2)
        title_label = Gtk.Label(label=title)
        title_label.set_xalign(0.0)
        text_box.pack_start(title_label, False, False, 0)
        if subtitle:
            text_box.pack_start(subtext(subtitle), False, False, 0)
        line.pack_start(text_box, True, True, 0)
        row.add(line)
        return row, radio

    def _on_source_picked(self, radio: Gtk.RadioButton, path: str, kind: str, note: str) -> None:
        if not radio.get_active():
            return
        if path != self.state.source_path:
            self.state.reset_for_new_source()
        self.state.source_path = path
        self.state.source_kind = kind
        self.state.source_note = note
        self.next_btn.set_sensitive(self.state.can_go_next())

    def _browse_for_bundle(self) -> None:
        dlg = Gtk.FileChooserDialog(title="Choose a transfer folder", transient_for=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "_Use this folder", Gtk.ResponseType.OK)
        response = dlg.run()
        folder = dlg.get_filename() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if not folder:
            return
        if folder != self.state.source_path:
            self.state.reset_for_new_source()
        self.state.source_path = folder
        self.state.source_kind = "bundle"
        self.state.source_note = ""
        self.next_btn.set_sensitive(self.state.can_go_next())
        self._load_sources()   # redraw the list so the manual pick is reflected (no row is added)

    def _make_usb_kit(self) -> None:
        dlg = Gtk.FileChooserDialog(title="Choose where to put the transfer USB kit", transient_for=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "_Make kit here", Gtk.ResponseType.OK)
        response = dlg.run()
        folder = dlg.get_filename() if response == Gtk.ResponseType.OK else None
        dlg.destroy()
        if not folder:
            return

        def work() -> None:
            self.cli.make_usb_kit(folder)

        def done(_result: Optional[Any], error: Optional[Exception]) -> None:
            if error is not None:
                self._error_dialog(str(error))
                return
            info = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                     buttons=Gtk.ButtonsType.OK, text="Transfer USB kit ready")
            info.format_secondary_text(
                f"LindosTransfer.cmd, LindosTransfer.ps1 and README.txt were copied into:\n{folder}\n\n"
                "Copy that folder to a USB stick, plug it into the other Windows PC and double-click "
                "LindosTransfer.cmd there.")
            info.run()
            info.destroy()

        self._call_async(work, done, busy_message="Making the transfer USB kit...")

    # ================================================================ User
    def _build_user(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Whose files?"), False, False, 0)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        widgets["list"] = list_box
        box.pack_start(scrolled(list_box), True, True, 0)
        return box

    def _enter_user(self) -> None:
        if not self.state.source_path:
            return
        self._call_async(lambda: self.cli.users(self.state.source_path), self._on_users_loaded,
                         busy_message="Looking for Windows user accounts...")

    def _on_users_loaded(self, result: Optional[Any], error: Optional[Exception]) -> None:
        list_box: Gtk.ListBox = self._pages["user"]["list"]  # type: ignore[assignment]
        for child in list(list_box.get_children()):
            list_box.remove(child)
        if error is not None:
            list_box.add(Gtk.Label(label=f"Could not list Windows user accounts: {error}"))
            list_box.show_all()
            return
        self.state.users = result or []
        group = None
        for user in self.state.users:
            name = str(user.get("name") or "")
            subtitle = str(user.get("windows_profile") or "")
            row, radio = self._source_row(title=name or "(unnamed)", subtitle=subtitle, enabled=True, group=group)
            group = group or radio
            radio.connect("toggled", self._on_user_picked, name)
            list_box.add(row)
        if len(self.state.users) == 1:
            self.state.user_name = str(self.state.users[0].get("name") or "")
        list_box.show_all()
        self.next_btn.set_sensitive(self.state.can_go_next())

    def _on_user_picked(self, radio: Gtk.RadioButton, name: str) -> None:
        if not radio.get_active():
            return
        if name != self.state.user_name:
            self.state.plan = None
        self.state.user_name = name
        self.next_btn.set_sensitive(self.state.can_go_next())

    # ================================================================ Bring
    def _build_bring(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("What to bring"), False, False, 0)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        widgets["list"] = list_box
        box.pack_start(scrolled(list_box), True, True, 0)

        ff = Gtk.CheckButton(label="Include Firefox saved passwords (copied unchanged; still needs "
                                    "your Firefox Primary Password to read)")
        ff.connect("toggled", self._on_firefox_passwords_toggled)
        widgets["firefox"] = ff
        box.pack_start(ff, False, False, 0)

        totals = Gtk.Label(label="")
        totals.set_xalign(0.0)
        widgets["totals"] = totals
        box.pack_start(totals, False, False, 0)
        return box

    def _enter_bring(self) -> None:
        if self.state.plan is None:
            self._rebuild_plan()
        else:
            self._refresh_bring_list()

    def _rebuild_plan(self) -> None:
        self._call_async(
            lambda: self.cli.plan(path=self.state.source_path, user=self.state.user_name,
                                  firefox_passwords=self.state.firefox_passwords),
            self._on_plan_built, busy_message="Working out what can be transferred (this can take a minute)...")

    def _on_plan_built(self, result: Optional[Any], error: Optional[Exception]) -> None:
        if error is not None:
            self._error_dialog(str(error))
            self.next_btn.set_sensitive(self.state.can_go_next())
            return
        self.state.plan = result
        self._refresh_bring_list()
        self.next_btn.set_sensitive(self.state.can_go_next())

    def _refresh_bring_list(self) -> None:
        list_box: Gtk.ListBox = self._pages["bring"]["list"]  # type: ignore[assignment]
        for child in list(list_box.get_children()):
            list_box.remove(child)
        plan = self.state.plan or {}
        for item in plan_items(plan):
            row = Gtk.ListBoxRow()
            line = hbox(8)
            line.set_border_width(6)
            check = Gtk.CheckButton(label=f"{item_label(item)} - {format_count(item.get('files'))} files, "
                                          f"{format_size(item.get('bytes'))}")
            check.set_active(bool(item.get("selected")))
            item_id = str(item.get("id"))
            check.connect("toggled", self._on_item_toggled, item_id)
            line.pack_start(check, True, True, 0)
            row.add(line)
            list_box.add(row)
        for note in plan.get("warnings") or []:
            list_box.add(Gtk.Label(label=f"Note: {note}"))
        list_box.show_all()
        self._pages["bring"]["firefox"].set_active(self.state.firefox_passwords)  # type: ignore[union-attr]
        self._update_bring_totals()

    def _on_item_toggled(self, check: Gtk.CheckButton, item_id: str) -> None:
        if self.state.plan is not None:
            set_item_selected(self.state.plan, item_id, check.get_active())
            self._update_bring_totals()

    def _update_bring_totals(self) -> None:
        files, total_bytes = self.state.selected_totals()
        label: Gtk.Label = self._pages["bring"]["totals"]  # type: ignore[assignment]
        label.set_label(f"Selected: {format_count(files)} files, {format_size(total_bytes)}")

    def _on_firefox_passwords_toggled(self, check: Gtk.CheckButton) -> None:
        active = check.get_active()
        if active == self.state.firefox_passwords:
            return
        if active:
            confirm = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Include Firefox saved passwords?")
            confirm.format_secondary_text(
                "This copies the files Firefox keeps your saved passwords in, unchanged. Without your "
                "Firefox Primary Password, anyone with access to this Lindos account could read them. "
                "Firefox Sync is a safer way to move passwords.")
            answer = confirm.run()
            confirm.destroy()
            if answer != Gtk.ResponseType.YES:
                check.set_active(False)
                return
        self.state.firefox_passwords = active
        self._rebuild_plan()

    # ================================================================ Apps
    def _build_apps(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Your apps"), False, False, 0)
        box.pack_start(subtext(
            "Nothing is installed without you choosing it here. Programs themselves cannot be copied "
            "from Windows; Lindos offers the closest match it knows for each one."), False, False, 0)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        widgets["list"] = list_box
        box.pack_start(scrolled(list_box), True, True, 0)
        return box

    def _enter_apps(self) -> None:
        self._refresh_apps_list()

    def _refresh_apps_list(self) -> None:
        list_box: Gtk.ListBox = self._pages["apps"]["list"]  # type: ignore[assignment]
        for child in list(list_box.get_children()):
            list_box.remove(child)
        apps = plan_apps(self.state.plan or {})
        if not apps:
            list_box.add(Gtk.Label(label="No app list was collected for this transfer."))
        for index, app in enumerate(apps):
            row = Gtk.ListBoxRow()
            line = vbox(2)
            line.set_border_width(6)
            top = hbox(8)
            check = Gtk.CheckButton(label=str(app.get("windows_name") or "(unknown app)"))
            check.set_active(bool(app.get("selected")))
            check.connect("toggled", self._on_app_toggled, index)
            top.pack_start(check, True, True, 0)
            actions = app.get("actions") or []
            if len(actions) > 1:
                combo = Gtk.ComboBoxText()
                for act in actions:
                    combo.append_text(str(act.get("label") or act.get("type") or ""))
                chosen = app.get("chosen") if isinstance(app.get("chosen"), int) else 0
                combo.set_active(chosen if 0 <= chosen < len(actions) else 0)
                combo.connect("changed", self._on_app_action_changed, index)
                top.pack_start(combo, False, False, 0)
            line.pack_start(top, False, False, 0)
            line.pack_start(subtext(app_action_summary(app)), False, False, 0)
            row.add(line)
            list_box.add(row)
        list_box.show_all()

    def _on_app_toggled(self, check: Gtk.CheckButton, index: int) -> None:
        if self.state.plan is not None:
            set_app_selected(self.state.plan, index, check.get_active())

    def _on_app_action_changed(self, combo: Gtk.ComboBoxText, index: int) -> None:
        if self.state.plan is not None:
            set_app_chosen(self.state.plan, index, combo.get_active())
            self._refresh_apps_list()

    # ================================================================ Transfer
    def _build_transfer(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Transferring your files"), False, False, 0)
        progress = Gtk.ProgressBar()
        progress.set_show_text(True)
        widgets["progress"] = progress
        box.pack_start(progress, False, False, 0)
        current = Gtk.Label(label="")
        current.set_xalign(0.0)
        widgets["current"] = current
        box.pack_start(current, False, False, 0)
        log_view = Gtk.TextView()
        log_view.set_editable(False)
        log_view.set_monospace(True)
        widgets["log"] = log_view
        box.pack_start(scrolled(log_view), True, True, 0)
        return box

    def _enter_transfer(self) -> None:
        if self.state.transfer_running or self.state.transfer_done:
            return
        self._start_transfer()

    def _append_log(self, text: str) -> None:
        view: Gtk.TextView = self._pages["transfer"]["log"]  # type: ignore[assignment]
        buf = view.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, text + "\n")

    def _start_transfer(self) -> None:
        plan = self.state.plan
        if plan is None:
            self._append_log("Nothing to transfer: no plan was built.")
            self.state.transfer_done = True
            self.state.transfer_failed = True
            self.next_btn.set_sensitive(self.state.can_go_next())
            return
        if not plan_has_selection(plan):
            self._append_log("Nothing was selected, so there is nothing to copy.")
            self.state.transfer_done = True
            self.state.install_apps_after = False
            self.next_btn.set_sensitive(self.state.can_go_next())
            return
        state_dir = os.path.join(os.environ.get("LINDOS_HOME") or os.path.expanduser("~"),
                                 ".local", "state", "lindos", "transfer-gui")
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd, plan_path = tempfile.mkstemp(prefix="plan-", suffix=".json", dir=state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._append_log(f"error: could not write the plan file: {exc}")
            self.state.transfer_done = True
            self.state.transfer_failed = True
            self.next_btn.set_sensitive(self.state.can_go_next())
            return
        self.state.plan_path = plan_path
        self.state.transfer_running = True
        self.state.install_apps_after = self.state.selected_app_count() > 0
        self.next_btn.set_sensitive(False)
        self.back_btn.set_sensitive(False)
        progress: Gtk.ProgressBar = self._pages["transfer"]["progress"]  # type: ignore[assignment]
        progress.set_fraction(0.0)
        progress.set_text("Starting...")
        self._append_log(f"Copying {len(plan_items(plan))} folder(s)/item(s) into your Lindos account...")

        def worker() -> int:
            def on_event(event: Dict[str, Any]) -> None:
                GLib.idle_add(self._on_transfer_event, event)

            def on_line(line: str) -> None:
                GLib.idle_add(self._append_log, line)

            return self.cli.stream_run(plan_path=plan_path, on_event=on_event, on_line=on_line)

        def done(result: Optional[Any], error: Optional[Exception]) -> None:
            self._on_transfer_finished(result, error)

        self._call_async(worker, done, busy_message="Copying your files...")

    def _on_transfer_event(self, event: Dict[str, Any]) -> bool:
        progress: Gtk.ProgressBar = self._pages["transfer"]["progress"]  # type: ignore[assignment]
        current: Gtk.Label = self._pages["transfer"]["current"]  # type: ignore[assignment]
        kind = event.get("event")
        if kind == "item":
            current.set_label(str(event.get("message") or event.get("item") or ""))
            self._append_log(f"-> {event.get('message') or event.get('item')}")
        elif kind == "progress":
            total = float(event.get("total_bytes") or 0)
            done_bytes = float(event.get("done_bytes") or 0)
            if total > 0:
                progress.set_fraction(min(1.0, max(0.0, done_bytes / total)))
                progress.set_text(f"{format_size(done_bytes)} of {format_size(total)}")
        elif kind == "skip":
            self._append_log(f"   skipped: {event.get('path')} ({event.get('message')})")
        elif kind == "error":
            self._append_log(f"   problem: {event.get('path') or event.get('item')}: {event.get('message')}")
        elif kind == "done":
            progress.set_fraction(1.0)
            progress.set_text("Done")
            if event.get("message"):
                self._append_log(str(event["message"]))
        return False

    def _on_transfer_finished(self, result: Optional[Any], error: Optional[Exception]) -> None:
        self.state.transfer_running = False
        code = result if isinstance(result, int) else None
        if error is not None:
            self.state.transfer_failed = True
            self.state.transfer_error = str(error)
            self._append_log(f"error: {error}")
        elif not run_exit_ok(code):
            self.state.transfer_failed = True
            self.state.transfer_error = f"lindos-transfer run exited with code {code}"
            self._append_log(self.state.transfer_error)
        else:
            self._append_log("File transfer finished.")
        if self.state.install_apps_after and not self.state.transfer_failed:
            self._maybe_install_apps()
        else:
            self._finish_transfer_step()

    def _maybe_install_apps(self) -> None:
        count = self.state.selected_app_count()
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Install the apps you selected?")
        confirm.format_secondary_text(
            f"{count} app(s) were selected on the Apps page. Install them now? You can also do this "
            "later by running: lindos-transfer install-apps --plan <plan.json>")
        answer = confirm.run()
        confirm.destroy()
        if answer != Gtk.ResponseType.YES:
            self._finish_transfer_step()
            return
        self._append_log(f"Installing {count} app(s)...")

        def worker() -> int:
            def on_line(line: str) -> None:
                GLib.idle_add(self._append_log, line)

            return self.cli.stream_install_apps(plan_path=self.state.plan_path, on_line=on_line)

        def done(result: Optional[Any], error: Optional[Exception]) -> None:
            if error is not None:
                self._append_log(f"error installing apps: {error}")
            elif isinstance(result, int) and result != 0:
                self._append_log(f"install-apps exited with code {result}")
            else:
                self._append_log("Apps installed.")
            self._finish_transfer_step()

        self._call_async(worker, done, busy_message="Installing your apps...")

    def _finish_transfer_step(self) -> None:
        self.state.transfer_done = True
        self.back_btn.set_sensitive(self.state.can_go_back())
        self.next_btn.set_sensitive(self.state.can_go_next())
        self._call_async(self.cli.report, self._on_report_loaded, busy_message="Reading the report...")

    def _on_report_loaded(self, result: Optional[Any], error: Optional[Exception]) -> None:
        if error is None:
            self.state.report = result
        # A missing/unreadable report must never block reaching the Done page.

    # ================================================================ Done
    def _build_done(self, widgets: Dict[str, Gtk.Widget]) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(heading("Done"), False, False, 0)
        summary = Gtk.Label(label="")
        summary.set_xalign(0.0)
        summary.set_line_wrap(True)
        widgets["summary"] = summary
        box.pack_start(summary, False, False, 0)
        next_steps = Gtk.Label(label="")
        next_steps.set_xalign(0.0)
        next_steps.set_line_wrap(True)
        widgets["next_steps"] = next_steps
        box.pack_start(scrolled(next_steps), True, True, 0)
        open_btn = Gtk.Button(label="Open folder")
        open_btn.connect("clicked", lambda _b: self._open_destination())
        box.pack_start(open_btn, False, False, 0)
        return box

    def _enter_done(self) -> None:
        self.next_btn.set_visible(False)
        self.cancel_btn.set_label("Close")
        summary: Gtk.Label = self._pages["done"]["summary"]  # type: ignore[assignment]
        steps: Gtk.Label = self._pages["done"]["next_steps"]  # type: ignore[assignment]
        report = self.state.report
        if self.state.transfer_failed:
            summary.set_label(f"The transfer did not finish cleanly: {self.state.transfer_error}")
            steps.set_label("")
            return
        if not report:
            summary.set_label("The transfer finished. A detailed report was not available.")
            steps.set_label("")
            return
        totals = report.get("totals") or {}
        summary.set_label(
            f"{format_count(totals.get('files'))} files copied ({format_size(totals.get('bytes'))}), "
            f"{format_count(totals.get('skipped'))} skipped, {format_count(totals.get('errors'))} problems.")
        steps.set_label("\n".join(f"- {s}" for s in report.get("next_steps") or []) or "Nothing further to do.")

    def _open_destination(self) -> None:
        report = self.state.report or {}
        dest_home = str(report.get("dest_home") or "")
        candidate = os.path.join(dest_home, "Transferred from Windows") if dest_home else ""
        target = candidate if candidate and os.path.isdir(candidate) else dest_home
        if not target:
            return
        try:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("could not open %s: %s", target, exc)

    # ---------------------------------------------------------------- window lifecycle
    def _on_destroy(self, *_args: Any) -> None:
        Gtk.main_quit()


# ==================================================================================================
# entry point
# ==================================================================================================
def _gtk_init_ok() -> bool:
    try:
        res = Gtk.init_check()
    except TypeError:
        res = Gtk.init_check([sys.argv[0]])
    except Exception as exc:  # pragma: no cover - defensive
        log.error("Gtk.init_check failed: %s", exc)
        return False
    if isinstance(res, (tuple, list)):
        return bool(res[0])
    return bool(res)


def run_app(*, cli: Optional[TransferCli] = None, from_path: str = "", logger: Optional[logging.Logger] = None) -> int:
    """Create the wizard window and run the GTK main loop.  Returns the process exit code."""
    logger = logger or log
    if not _gtk_init_ok():
        sys.stderr.write("lindos-transfer-gui: cannot open a display (is DISPLAY/WAYLAND_DISPLAY set?)\n")
        return 1
    cli = cli or TransferCli()
    if not cli.available():
        sys.stderr.write(f"lindos-transfer-gui: {cli.exe} is not installed. Install the lindos-transfer package.\n")
        return 1
    state = WizardState()
    if from_path:
        state.source_path = from_path
        state.source_kind = "bundle" if os.path.isdir(from_path) and os.path.isfile(
            os.path.join(from_path, "lindos-transfer.json")) else "partition"
    win = TransferWizard(cli, state=state)
    win.show_all()
    win.present()
    logger.info("lindos-transfer-gui %s starting", __version__)
    Gtk.main()
    return win.exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lindos-transfer-gui",
                                description="Bring your files, bookmarks and app list from Windows (GTK wizard).")
    p.add_argument("--from", dest="from_path", metavar="PATH", default="",
                   help="start already pointed at this Windows partition or transfer folder")
    p.add_argument("--debug", action="store_true", help="verbose logging to stderr")
    p.add_argument("--version", action="version", version=f"lindos-transfer-gui {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return run_app(from_path=args.from_path)
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
