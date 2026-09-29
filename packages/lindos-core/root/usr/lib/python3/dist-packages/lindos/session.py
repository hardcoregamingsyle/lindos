"""Which kind of session is this?  Live USB, installer chroot or the temporary OEM account.

One source of truth for every Lindos component that must behave differently while the machine is
running from the install medium (no first-run wizard, no update toasts, no package installs):

* :func:`is_live_session` - the kernel command line has ``boot=casper`` or ``boot=live`` (the same
  test Mint's own ``xapp.os.is_live_session`` uses).  The command line file is ``/proc/cmdline``;
  tests point ``LINDOS_TEST_CMDLINE`` at a fake file instead (never set on a real system).
* :func:`is_installer_chroot` - ``LINDOS_INSTALLER=1``: the caller is a script running inside
  ``chroot /target`` for the installer.  ``/proc/cmdline`` still says ``boot=casper`` in there
  (proc is the live kernel's), so this explicit flag is the only reliable signal.
* :func:`is_oem_temp_user` - the login name is ``oem``, the temporary account of Ubiquity's OEM
  mode that exists between the installation and the end user's own first-boot wizard.

Shell twin of the first: ``/usr/libexec/lindos/is-live-session`` (exit 0 = live).  Systemd units use
``ConditionKernelCommandLine=!boot=casper``.  Standard library only; importable on any OS.
"""

from __future__ import annotations

import os
from typing import Optional

CMDLINE_PATH = "/proc/cmdline"
CMDLINE_ENV = "LINDOS_TEST_CMDLINE"
INSTALLER_ENV = "LINDOS_INSTALLER"
LIVE_WORDS = ("boot=casper", "boot=live")
OEM_USER = "oem"


def cmdline_path() -> str:
    """The file the kernel command line is read from (``LINDOS_TEST_CMDLINE`` or ``/proc/cmdline``)."""
    return os.environ.get(CMDLINE_ENV) or CMDLINE_PATH


def read_cmdline(path: Optional[str] = None) -> str:
    """The kernel command line as text; ``""`` when the file is missing or unreadable."""
    try:
        with open(path or cmdline_path(), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def is_live_session(cmdline: Optional[str] = None) -> bool:
    """True iff the command line has the word ``boot=casper`` or ``boot=live``.

    *cmdline* (text) overrides reading the file - handy for callers that already have it.  Words
    are matched exactly, so ``xboot=casper`` or ``boot=casper2`` do not count.
    """
    text = read_cmdline() if cmdline is None else cmdline
    return any(word in LIVE_WORDS for word in text.split())


def is_installer_chroot() -> bool:
    """True iff ``LINDOS_INSTALLER=1`` (a script inside the installer's ``chroot /target``)."""
    return os.environ.get(INSTALLER_ENV, "") == "1"


def _login_name() -> str:
    # lazy: getpass/pwd must not be needed just to import this module (Windows has no pwd)
    try:
        import getpass
        return getpass.getuser()
    except (ImportError, OSError, KeyError):
        return ""


def is_oem_temp_user(user: Optional[str] = None) -> bool:
    """True iff *user* (default: the current login name) is the temporary ``oem`` account."""
    name = _login_name() if user is None else user
    return (name or "").strip() == OEM_USER


__all__ = [
    "CMDLINE_PATH", "CMDLINE_ENV", "INSTALLER_ENV", "LIVE_WORDS", "OEM_USER",
    "cmdline_path", "read_cmdline", "is_live_session", "is_installer_chroot", "is_oem_temp_user",
]
