"""Desktop feedback when ``lindos-run`` is started without a terminal (file manager, Start Menu).

Uses ``zenity`` (preferred) or ``yad`` for a pulsating progress window, error/explanation
dialogs, Windows-style Yes/No questions and a C:\\ drive chooser, and ``notify-send`` for
the "installed" toast.  Everything is optional and silently degrades to plain logging
(:meth:`Feedback.question` / :meth:`Feedback.choose` then return ``None`` so the caller can
ask on the terminal instead).  Dialog text is never interpreted as markup: file names and
registry keys may contain ``<`` or ``&``.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
from typing import Callable, List, Optional, Sequence, Tuple

from . import get_logger

__all__ = ["Feedback", "gui_wanted", "TITLE"]

log = get_logger("lindos-compat.gui")

TITLE = "Lindos - Windows program"


def gui_wanted(env: Optional[dict] = None, isatty: Optional[Callable[[], bool]] = None) -> bool:
    """True when we were launched without a terminal but with a display."""
    env = os.environ if env is None else env
    if isatty is None:
        isatty = lambda: bool(sys.stdout.isatty())  # noqa: E731
    if os.environ.get("LINDOS_NO_GUI"):
        return False
    try:
        tty = isatty()
    except (ValueError, OSError):
        tty = False
    return (not tty) and bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


class Feedback:
    """Progress + error dialogs (zenity/yad) and notifications; no-ops when disabled."""

    def __init__(self, enabled: bool, *, which: Callable[[str], Optional[str]] = shutil.which,
                 popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
                 run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> None:
        self.enabled = enabled
        self._which = which
        self._popen = popen
        self._run = run
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self.tool: Optional[str] = None
        if enabled:
            if which("zenity"):
                self.tool = "zenity"
            elif which("yad"):
                self.tool = "yad"

    # -- progress ---------------------------------------------------------
    def progress(self, text: str) -> None:
        """Show (or update) a pulsating progress dialog."""
        if not self.enabled or not self.tool:
            log.info("%s", text)
            return
        if self._proc is None or self._proc.poll() is not None:
            exe = self._which(self.tool)
            if not exe:
                return
            if self.tool == "zenity":
                argv = [exe, "--progress", "--pulsate", "--no-cancel", "--auto-close", "--auto-kill",
                        f"--title={TITLE}", f"--text={text}", "--width=380"]
            else:
                argv = [exe, "--progress", "--pulsate", "--no-buttons", "--auto-close", "--auto-kill",
                        f"--title={TITLE}", f"--text={text}", "--width=380", "--center", "--undecorated"]
            try:
                self._proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None
                return
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(f"# {text}\n".encode("utf-8", "replace"))
            self._proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            self._proc = None

    def close_progress(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"100\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except OSError:
                pass

    # -- dialogs ----------------------------------------------------------
    @property
    def interactive(self) -> bool:
        """True when dialogs can actually be shown (GUI mode and zenity/yad present)."""
        return bool(self.enabled and self.tool and self._which(self.tool))

    def _text_arg(self, text: str) -> str:
        # zenity gets --no-markup; yad always parses Pango markup, so escape for it
        return f"--text={text if self.tool == 'zenity' else html.escape(text, quote=False)}"

    def _dialog(self, kind: str, text: str) -> None:
        if not self.enabled or not self.tool:
            (log.error if kind == "error" else log.info)("%s", text)
            return
        exe = self._which(self.tool)
        if not exe:
            return
        flag = {"error": "--error", "info": "--info", "warning": "--warning"}.get(kind, "--info")
        argv = [exe, flag, f"--title={TITLE}", self._text_arg(text), "--width=460"]
        if self.tool == "zenity":
            argv.append("--no-markup")
            if len(text) < 90:
                argv.append("--no-wrap")
        else:
            argv += ["--center", "--button=OK:0"]
        try:
            self._run(argv, timeout=600, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    def error(self, text: str) -> None:
        self.close_progress()
        self._dialog("error", text)

    def info(self, text: str) -> None:
        self.close_progress()
        self._dialog("info", text)

    def warning(self, text: str) -> None:
        self._dialog("warning", text)

    def explain(self, text: str) -> None:
        """Why a file cannot be opened (``lindos-run`` exit code 3): a warning dialog."""
        self.close_progress()
        self._dialog("warning", text)

    def question(self, text: str, *, ok_label: str = "Yes", cancel_label: str = "No",
                 title: str = TITLE) -> Optional[bool]:
        """Windows-style Yes/No question.  ``None`` when no dialog can be shown."""
        if not self.interactive:
            return None
        self.close_progress()
        exe = self._which(str(self.tool))
        assert exe is not None
        if self.tool == "zenity":
            argv = [exe, "--question", f"--title={title}", self._text_arg(text), "--no-markup", "--width=520",
                    f"--ok-label={ok_label}", f"--cancel-label={cancel_label}"]
        else:
            argv = [exe, f"--title={title}", self._text_arg(text), "--width=520", "--center",
                    "--image=dialog-question", f"--button={cancel_label}:1", f"--button={ok_label}:0"]
        try:
            proc = self._run(argv, timeout=3600, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return getattr(proc, "returncode", 1) == 0

    def choose(self, text: str, rows: Sequence[Tuple[str, str]], *, column: str = "C:\\ drive",
               detail_column: str = "Programs", ok_label: str = "OK", title: str = TITLE) -> Optional[str]:
        """Pick one of ``rows`` (``(value, description)``); returns the value, "" on cancel,
        ``None`` when no dialog can be shown."""
        if not self.interactive or not rows:
            return None
        self.close_progress()
        exe = self._which(str(self.tool))
        assert exe is not None
        cells: List[str] = []
        for value, detail in rows:
            # list cells are plain arguments: never let one look like an option
            cells += [value.lstrip("-") or value, (detail or " ").lstrip("-") or " "]
        label = f"--text={html.escape(text, quote=False)}"  # list labels are markup in zenity and yad
        if self.tool == "zenity":
            argv = [exe, "--list", f"--title={title}", label, f"--column={column}",
                    f"--column={detail_column}", "--print-column=1", "--width=560", "--height=380",
                    f"--ok-label={ok_label}", *cells]
        else:
            argv = [exe, "--list", f"--title={title}", label, f"--column={column}",
                    f"--column={detail_column}", "--print-column=1", "--width=560", "--height=380", "--center",
                    *cells]
        try:
            proc = self._run(argv, timeout=3600, check=False, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return ""
        out = (getattr(proc, "stdout", "") or "").strip().splitlines()
        choice = out[0].strip().rstrip("|") if out else ""
        valid = {value for value, _detail in rows}
        return choice if choice in valid else ""

    # -- notifications ----------------------------------------------------
    def notify(self, summary: str, body: str = "", icon: str = "lindos-exe") -> bool:
        exe = self._which("notify-send")
        if not exe or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            log.info("%s - %s", summary, body)
            return False
        try:
            self._run([exe, "-a", "Lindos", "-i", icon, summary, body], timeout=15, check=False,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
