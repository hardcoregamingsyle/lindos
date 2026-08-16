"""lindos_kernel — the Lindos-tuned kernel package's pure-Python library (SPEC-KERNEL §15).

Sub-modules:

* :mod:`lindos_kernel.manifest` — load/validate ``/usr/share/lindos/kernel/manifest.json``.
* :mod:`lindos_kernel.kconfig`  — parse/validate the ``lindos.config`` kconfig fragment.
* :mod:`lindos_kernel.features`  — probe the *running* kernel for the Lindos perf features.
* :mod:`lindos_kernel.grub`      — read/patch the marker-fenced ``50-lindos.cfg`` cmdline drop-in.
* :mod:`lindos_kernel.build`     — thin wrapper around ``build/kernel/build-kernel.sh``.

Every module is importable on any OS: Linux-only calls (``/proc``, ``/sys``, ``/dev``,
``uname``) are guarded and honour the ``LINDOS_ROOT`` / ``LINDOS_HOME`` env overrides exactly
like ``lindos.paths`` in lindos-core, so the whole package is unit-testable on Windows against
a faked root tree.  Nothing here touches the filesystem at import time.
"""
from __future__ import annotations

import os

__version__ = "1.0.0"

ROOT_ENV = "LINDOS_ROOT"
HOME_ENV = "LINDOS_HOME"

# System locations owned by this package (canonical, unresolved).
SHARE_DIR = "/usr/share/lindos"
KERNEL_SHARE_DIR = "/usr/share/lindos/kernel"
MANIFEST_PATH = "/usr/share/lindos/kernel/manifest.json"
CONFIG_PATH = "/usr/share/lindos/kernel/lindos.config"
GRUB_DROPIN = "/etc/default/grub.d/50-lindos.cfg"


def root() -> str:
    """Return the ``LINDOS_ROOT`` prefix ("" when unset)."""
    return os.environ.get(ROOT_ENV, "") or ""


def home() -> str:
    """Return the effective home directory (``LINDOS_HOME`` or the real one)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return override
    return os.path.expanduser("~")


def resolve(path: str) -> str:
    """Apply ``LINDOS_HOME`` (``~`` paths) or ``LINDOS_ROOT`` (absolute paths).

    Mirrors ``lindos.paths.resolve`` so tests can redirect ``/proc``/``/sys``/``/boot``/
    ``/etc`` and the shipped data files under a scratch directory.
    """
    if path.startswith("~"):
        rest = path[1:].lstrip("/\\")
        return os.path.normpath(os.path.join(home(), rest)) if rest else home()
    prefix = root()
    if prefix and (path.startswith("/") or os.path.isabs(path)):
        _drive, tail = os.path.splitdrive(path)
        return os.path.normpath(os.path.join(prefix, tail.lstrip("/\\")))
    return path


def manifest_path() -> str:
    """Resolved path of the feature manifest."""
    return resolve(MANIFEST_PATH)


def config_path() -> str:
    """Resolved path of the shipped ``lindos.config`` kconfig fragment."""
    return resolve(CONFIG_PATH)


def grub_dropin_path() -> str:
    """Resolved path of the marker-fenced GRUB cmdline drop-in."""
    return resolve(GRUB_DROPIN)


def is_linux() -> bool:
    """True on a real Linux host (build/probe actions only run there)."""
    return os.name == "posix" and _uname_sysname() == "Linux"


def _uname_sysname() -> str:
    fn = getattr(os, "uname", None)
    if fn is None:
        return ""
    try:
        return fn().sysname
    except OSError:  # pragma: no cover - uname should not fail on posix
        return ""


__all__ = [
    "__version__", "ROOT_ENV", "HOME_ENV", "SHARE_DIR", "KERNEL_SHARE_DIR",
    "MANIFEST_PATH", "CONFIG_PATH", "GRUB_DROPIN",
    "root", "home", "resolve", "manifest_path", "config_path", "grub_dropin_path", "is_linux",
]
