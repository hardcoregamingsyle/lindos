"""lindos_winapps -- seamless Windows apps over RDP (package ``lindos-winapps``, SPEC-VM §22).

The logic behind the ``lindos-winapps`` command.  Installed at ``/usr/lib/lindos-winapps``
and imported by the thin ``/usr/bin/lindos-winapps`` launcher.  Stdlib-only and importable on
any OS: no Linux/RDP call is made at import time, so the test-suite runs on Windows/macOS.

Modules
-------
apps      the app catalog (``apps.json``) + ``.desktop`` generation for ``install``/``remove``
backend   backend config (``winapps.conf``) + reachability of the Windows instance
rdp       FreeRDP RemoteApp command building and launch
cli       the argparse command-line interface (``setup``/``check``/``list``/``install``/``run``/``remove``)

The ``main`` entry point below is a thin wrapper that dispatches to :mod:`lindos_winapps.cli`
(kept in its own module so ``import lindos_winapps`` stays cheap), covering the
``__init__``/``apps``/``backend``/``rdp`` modules the spec requires plus ``cli``.

Honesty (SPEC-VM §20): a backend needs a *user-supplied, licensed* Windows and the licensed
app installed inside it.  ``setup`` NEVER prompts for or stores the password; Lindos does not
enter Windows/app credentials for the user.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

__all__ = [
    "__version__",
    "LIB_DIR",
    "CoreMissing",
    "core_paths",
    "user_home",
    "expand_user_path",
    "winapps_config_dir",
    "winapps_conf_path",
    "rdp_pass_file",
    "applications_dir",
    "catalog_path",
    "desktop_path",
    "get_logger",
    "main",
]

__version__ = "1.0.0"

#: Where this package is installed on a Lindos system.
LIB_DIR = "/usr/lib/lindos-winapps"

# Exit codes (SPEC-VM §22): 0 ok · 1 error · 2 usage · 3 backend unreachable.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


class CoreMissing(RuntimeError):
    """Raised when the ``lindos`` python package from lindos-core is not importable."""

    def __init__(self, what: str = "lindos.paths") -> None:
        super().__init__(
            f"The Lindos core module '{what}' is not installed (package lindos-core). "
            "Install it with: apt install lindos-core"
        )


def core_paths() -> Any:
    """Return the ``lindos.paths`` module or raise :class:`CoreMissing`."""
    try:
        from lindos import paths as _core_paths  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CoreMissing("lindos.paths") from exc
    return _core_paths


def user_home() -> Path:
    """The user's home directory, honouring ``LINDOS_HOME`` (SPEC §4.1)."""
    override = os.environ.get("LINDOS_HOME")
    if override:
        return Path(override)
    return Path.home()


def expand_user_path(path: str | os.PathLike[str]) -> Path:
    """Expand a leading ``~`` using :func:`user_home` (so ``LINDOS_HOME`` is honoured)."""
    text = str(path)
    if text == "~":
        return user_home()
    if text.startswith("~/") or text.startswith("~\\"):
        return user_home() / text[2:]
    return Path(os.path.expanduser(text))


def _root_prefix() -> str:
    return (os.environ.get("LINDOS_ROOT") or "").rstrip("/\\")


def winapps_config_dir() -> Path:
    """``~/.config/lindos/winapps`` (LINDOS_HOME-aware)."""
    return user_home() / ".config" / "lindos" / "winapps"


def winapps_conf_path() -> Path:
    """``~/.config/lindos/winapps/winapps.conf``."""
    return winapps_config_dir() / "winapps.conf"


def rdp_pass_file() -> Path:
    """Optional user-created password file (``~/.config/lindos/winapps/rdp-pass``).

    Lindos never writes this file; the user may create it (``chmod 600``) if they prefer a
    file to the ``RDP_PASS`` environment variable.  Documented in ``winapps.conf``.
    """
    return winapps_config_dir() / "rdp-pass"


def applications_dir() -> Path:
    """``~/.local/share/applications`` where per-app launchers are written."""
    return user_home() / ".local" / "share" / "applications"


def desktop_path(app_id: str) -> Path:
    """The generated launcher path for ``app_id`` (``lindos-winapp-<id>.desktop``)."""
    return applications_dir() / f"lindos-winapp-{app_id}.desktop"


def catalog_path() -> Path:
    """``/usr/share/lindos/winapps/apps.json`` (LINDOS_ROOT-aware).

    Uses ``lindos.paths`` when available so a faked tree is honoured; falls back to the SPEC
    value with ``LINDOS_ROOT`` applied when lindos-core is absent.
    """
    rel = "/usr/share/lindos/winapps/apps.json"
    try:
        mod = core_paths()
        resolver = getattr(mod, "resolve", None)
        if callable(resolver):
            return Path(str(resolver(rel)))
    except CoreMissing:
        pass
    root = _root_prefix()
    return Path(root + rel if root else rel)


def get_logger(name: str = "lindos-winapps", *, verbose: Optional[bool] = None) -> logging.Logger:
    """Return a configured logger.  ``LINDOS_DEBUG=1`` switches to DEBUG level."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    if verbose is None:
        verbose = os.environ.get("LINDOS_DEBUG", "") not in ("", "0")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def main(argv: Optional[List[str]] = None) -> int:
    """Argparse entry point for ``lindos-winapps`` (dispatches to :mod:`cli`)."""
    from .cli import run_cli  # local import keeps ``import lindos_winapps`` cheap
    return run_cli(argv)
