"""lindos_compat -- the Lindos Windows-app compatibility layer (package ``lindos-compat``).

This package holds the logic behind the ``lindos-run`` and ``lindos-compat`` command
line tools.  It is installed to ``/usr/lib/lindos-compat`` and imported by the thin
scripts in ``/usr/bin``.  Everything here is stdlib-only and importable on any OS: no
Linux call is made at import time, so the test-suite can run on Windows/macOS.

Shared pieces (PE analyzer, slugify, apps database) live in ``lindos-core``
(``lindos.compat``); this package imports them lazily through :func:`core` and never
duplicates them.

Modules
-------
lnk       minimal Windows Shell Link (.lnk) parser
prefix    Wine prefix management (per-app slug dirs under PREFIXES_DIR, casefold C:\\ drives)
runner    runner choice (umu / wine / bottles), environment + command building (Windows paths)
scan      post-install scan (Program Files, Start Menu) and .desktop creation
icons     icon extraction with wrestool/icotool
recipes   recipe loading/validation/apply (Photoshop, Office, ...)
doctor    ``lindos-compat doctor`` checks
gui       zenity/yad/notify-send feedback when launched without a terminal
installers ``install-umu`` (umu-launcher from GitHub) and ``install-bottles`` (Flatpak)
formats   Windows file-type registry, content classifier, per-format action plans (SPEC-WINDOWS §28.2)
dos       DOSBox launcher and Wine WoW64-mode probe for 16-bit programs (§28.5)
diskimage ISO/IMG read-only loop mount + autorun.inf (§28.6)
binfmt    binfmt_misc status for ``./setup.exe`` in a terminal (§28.7)
msix      MSIX/APPX/bundles/.appinstaller: classify, inspect, safe install (§28.4)
winget    winget index + manifests (``lindos-compat winget``, §28.10)
cli_run   ``lindos-run`` main (dispatches every Windows file type to its handler)
cli_compat ``lindos-compat`` main

The SPEC-WINDOWS modules are imported lazily by ``cli_run`` / ``doctor`` so that one
missing or broken module only disables its own file types.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "__version__",
    "LIB_DIR",
    "CoreMissing",
    "core",
    "core_paths",
    "core_config",
    "path_const",
    "expand_user_path",
    "user_home",
    "load_user_config",
    "get_logger",
]

__version__ = "1.0.0"

#: Where this package is installed on a Lindos system.
LIB_DIR = "/usr/lib/lindos-compat"

# ---------------------------------------------------------------------------
# lindos-core access (lazy, honest about absence)
# ---------------------------------------------------------------------------


class CoreMissing(RuntimeError):
    """Raised when the ``lindos`` python package from lindos-core is not importable."""

    def __init__(self, what: str = "lindos.compat") -> None:
        super().__init__(
            f"The Lindos core module '{what}' is not installed (package lindos-core). "
            "Install it with: apt install lindos-core"
        )


def core() -> Any:
    """Return the ``lindos.compat`` module (analyzer, slugify, apps db) or raise CoreMissing."""
    try:
        from lindos import compat as _core_compat  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CoreMissing("lindos.compat") from exc
    return _core_compat


def core_paths() -> Any:
    """Return the ``lindos.paths`` module or raise CoreMissing."""
    try:
        from lindos import paths as _core_paths  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise CoreMissing("lindos.paths") from exc
    return _core_paths


def core_config() -> Any:
    """Return the ``lindos.config`` module or raise CoreMissing."""
    try:
        from lindos import config as _core_config  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise CoreMissing("lindos.config") from exc
    return _core_config


# Fallback path constants: identical to SPEC §4.1.  They are used *only* when
# lindos-core is missing so that read-only commands (doctor, recipes list) still work.
_FALLBACK_PATHS = {
    "RECIPES_DIR": "/usr/share/lindos/recipes",
    "PREFIXES_DIR": "~/.local/share/lindos/prefixes",
    "APPS_DB": "~/.local/share/lindos/apps.json",
    "LOG_DIR": "~/.local/state/lindos",
    "STATE_DIR": "~/.local/share/lindos",
    "USER_CONF_DIR": "~/.config/lindos",
    "SHARE_DIR": "/usr/share/lindos",
}


def path_const(name: str) -> str:
    """Return a path constant from ``lindos.paths`` (SPEC §4.1) honouring LINDOS_ROOT/LINDOS_HOME.

    System paths (``/usr/share/lindos/...``) come back with ``LINDOS_ROOT`` applied;
    user paths (``~/...``) come back *resolved* against ``LINDOS_HOME`` (or the real
    home) when lindos-core provides ``paths.resolve``, otherwise still ``~``-prefixed
    (callers use :func:`expand_user_path`, which handles both forms).

    Falls back to the SPEC values when lindos-core is not importable.
    """
    try:
        mod = core_paths()
        value = getattr(mod, name)
        if isinstance(value, (str, os.PathLike)):
            resolver = getattr(mod, "resolve", None)
            if callable(resolver):
                try:
                    return str(resolver(str(value)))
                except Exception:  # noqa: BLE001 - never let a path helper break a launch
                    pass
            return str(value)
    except (CoreMissing, AttributeError):
        pass
    value = _FALLBACK_PATHS[name]
    root = os.environ.get("LINDOS_ROOT")
    if root and value.startswith("/"):
        return root.rstrip("/") + value
    return value


def user_home() -> Path:
    """The user's home directory, honouring ``LINDOS_HOME`` (SPEC §4.1)."""
    override = os.environ.get("LINDOS_HOME")
    if override:
        return Path(override)
    return Path.home()


def expand_user_path(path: str | os.PathLike[str]) -> Path:
    """Expand a leading ``~`` using :func:`user_home` (so LINDOS_HOME is honoured)."""
    text = str(path)
    if text == "~":
        return user_home()
    if text.startswith("~/") or text.startswith("~\\"):
        return user_home() / text[2:]
    return Path(os.path.expanduser(text))


def load_user_config() -> Any:
    """Load the user config (``lindos.config.Config``); return a dict of defaults if unavailable.

    The returned object always supports ``.get(key, default)``.
    """
    try:
        cfg_mod = core_config()
        return cfg_mod.Config.load()
    except Exception:  # noqa: BLE001 - config problems must never stop a launch
        return dict(_CONFIG_DEFAULTS)


# Mirror of SPEC §4.2 defaults (used only if lindos-core config cannot be loaded).
_CONFIG_DEFAULTS = {
    "mode": "everyday",
    "browser": "firefox",
    "theme": "dark",
    "accent": "#60CDFF",
    "setup_done": False,
    "gamemode_auto": True,
    "mangohud": False,
    "telemetry": False,
    "schema": 1,
}


def get_logger(name: str = "lindos-compat", *, verbose: Optional[bool] = None) -> logging.Logger:
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
