"""lindos_transfer -- "Windows Easy Transfer" for Lindos (package ``lindos-transfer``, SPEC-WINDOWS §29).

Brings a Windows user's files and settings to Lindos from either

* the Windows partition of the same PC, mounted **strictly read-only**, or
* a *transfer folder* written on the old PC by the double-click Windows kit
  (``LindosTransfer.cmd`` + ``LindosTransfer.ps1``, manifest ``lindos-transfer.json``).

Honesty rules (SPEC-WINDOWS §27.3, binding): nothing is ever written to the Windows side,
secrets are never opened (:mod:`lindos_transfer.secrets` holds the enforced deny-list), cloud-only
OneDrive files are never downloaded, Wi-Fi passwords move only when the user exported them on
Windows and then go to the root helper on stdin, and BitLocker keys are typed by the user into
udisks/cryptsetup prompts -- Lindos never sees them.

Everything here is stdlib-only and importable on any OS; no Linux call happens at import time.

Modules
-------
secrets     the secrets deny-list + the only file-open path every reader uses
sources     partitions (lsblk), transfer folders, the :class:`~lindos_transfer.sources.Source` model
mounts      read-only udisks mounting, hibernation (Fast Startup) check, BitLocker guidance
regf        bounds-checked offline registry hive reader
winreg      the registry facts Lindos needs (users, folders, uninstall keys, control set, ...)
profiles    Windows users and their known folders
plan        the transfer plan (JSON schema 1)
copyengine  the read-only, resumable, placeholder-aware copier
browsers    Chromium-family and Firefox data (bookmarks, Firefox import profile)
bookmarks   bookmark models, Chromium/Firefox/Favorites readers, Netscape HTML writer
fonts       user-installed fonts only
wallpaper   the desktop picture and its fit mode
wifi        Wi-Fi profiles (SSID/security; passwords only from the kit's explicit export)
vdf         Valve KeyValues reader/writer
steam       Steam libraries and installed games
apps        installed-programs inventory, app-map matching and install executors
report      transfer report (JSON + a readable HTML page)
cli         ``lindos-transfer`` command line
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "__version__",
    "LIB_DIR",
    "SHARE_SUBDIR",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "EXIT_NOTHING",
    "TransferError",
    "core_paths",
    "system_path",
    "user_home",
    "share_dir",
    "state_dir",
    "get_logger",
]

__version__ = "1.0.0"

#: Where this package is installed on a Lindos system.
LIB_DIR = "/usr/lib/lindos-transfer"
#: Data directory (app-map.json, the Windows kit) below /usr/share/lindos.
SHARE_SUBDIR = "transfer"

# SPEC-WINDOWS §29.2 exit codes.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOTHING = 4


class TransferError(Exception):
    """A problem the user can understand (the message is shown as-is)."""


def core_paths() -> Optional[Any]:
    """Return ``lindos.paths`` from lindos-core, or ``None`` when it is not importable."""
    try:
        from lindos import paths as _paths  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return _paths


def system_path(path: str) -> str:
    """Apply ``LINDOS_ROOT`` to an absolute system path (``/usr/share/...``, ``/media``, ``/proc``...)."""
    mod = core_paths()
    if mod is not None:
        try:
            return str(mod.resolve(path))
        except Exception:  # noqa: BLE001 - never let a path helper break a transfer
            pass
    root = os.environ.get("LINDOS_ROOT", "")
    if root and path.startswith("/"):
        return os.path.normpath(os.path.join(root, path.lstrip("/")))
    return path


def user_home() -> Path:
    """The user's home directory, honouring ``LINDOS_HOME`` (SPEC §4.1)."""
    override = os.environ.get("LINDOS_HOME")
    if override:
        return Path(override)
    mod = core_paths()
    if mod is not None:
        try:
            return Path(mod.home())
        except Exception:  # noqa: BLE001
            pass
    return Path.home()


def share_dir() -> Path:
    """``/usr/share/lindos/transfer`` (LINDOS_ROOT-aware).

    Falls back to the copy inside a source checkout (``packages/lindos-transfer/root/...``) so the
    tools work straight from the repository.
    """
    installed = Path(system_path("/usr/share/lindos")) / SHARE_SUBDIR
    if installed.is_dir():
        return installed
    here = Path(__file__).resolve().parent            # .../root/usr/lib/lindos-transfer/lindos_transfer
    tree = here.parents[2] / "share" / "lindos" / SHARE_SUBDIR
    if tree.is_dir():
        return tree
    return installed


def state_dir() -> Path:
    """``~/.local/state/lindos/transfer`` (journals, plans, reports)."""
    mod = core_paths()
    if mod is not None:
        try:
            return Path(mod.log_dir()) / SHARE_SUBDIR
        except Exception:  # noqa: BLE001
            pass
    return user_home() / ".local" / "state" / "lindos" / SHARE_SUBDIR


def get_logger(name: str = "lindos-transfer", *, verbose: Optional[bool] = None) -> logging.Logger:
    """Return a configured logger (``LINDOS_DEBUG=1`` or *verbose* switches to DEBUG)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("lindos-transfer: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    if verbose is None:
        verbose = os.environ.get("LINDOS_DEBUG", "") not in ("", "0")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger
