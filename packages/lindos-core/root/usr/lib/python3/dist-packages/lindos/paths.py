"""Filesystem locations shared by every Lindos component (SPEC §4.1).

The module-level constants are the canonical locations.  Because the test-suite runs on
Windows/macOS and because the ISO builder stages files inside a chroot, every location can
be redirected at *call time* through two environment variables:

* ``LINDOS_ROOT`` — prefix prepended to *system* paths
  (``/etc/lindos`` → ``$LINDOS_ROOT/etc/lindos``).
* ``LINDOS_HOME`` — replaces ``~`` for *user* paths (default: ``os.path.expanduser("~")``).

Always go through :func:`resolve` or the ``*()`` accessor functions (``system_conf()``,
``modes_dir()``, ``user_conf()`` …) instead of using the raw constants when you touch the
filesystem, otherwise the overrides are ignored.  Nothing in this module touches the
filesystem at import time.
"""

from __future__ import annotations

import os
from typing import Dict

# --- system locations -------------------------------------------------------------------
SYSTEM_CONF_DIR = "/etc/lindos"
SYSTEM_CONF = "/etc/lindos/system.json"
SHARE_DIR = "/usr/share/lindos"
MODES_DIR = "/usr/share/lindos/modes"
RECIPES_DIR = "/usr/share/lindos/recipes"          # compat recipes (yaml/json)
LIBEXEC_DIR = "/usr/libexec/lindos"
HELPER = "/usr/libexec/lindos/lindos-helper"
HELPER_LOG = "/var/log/lindos/helper.log"
SYSTEM_LOG_DIR = "/var/log/lindos"
WALLPAPERS_DIR = "/usr/share/backgrounds/lindos"
INSTALL_BROWSER_SCRIPT = "/usr/libexec/lindos/install-browser.sh"
INSTALL_COMPAT_SCRIPT = "/usr/libexec/lindos/install-compat.sh"
INSTALL_GAMING_SCRIPT = "/usr/libexec/lindos/install-gaming.sh"
SYSCTL_MODE_CONF = "/etc/sysctl.d/90-lindos-mode.conf"
APT_KEYRINGS_DIR = "/etc/apt/keyrings"

# --- user locations (``~`` is expanded at call time) ---------------------------------------
USER_CONF_DIR = "~/.config/lindos"
USER_CONF = "~/.config/lindos/config.json"
SETUP_DONE = "~/.config/lindos/setup-done"
STATE_DIR = "~/.local/share/lindos"
PREFIXES_DIR = "~/.local/share/lindos/prefixes"
APPS_DB = "~/.local/share/lindos/apps.json"
LOG_DIR = "~/.local/state/lindos"
XFCONF_DIR = "~/.config/xfce4/xfconf/xfce-perchannel-xml"
PANEL_DIR = "~/.config/xfce4/panel"
GTK3_DIR = "~/.config/gtk-3.0"
GTK4_DIR = "~/.config/gtk-4.0"
AUTOSTART_DIR = "~/.config/autostart"

ROOT_ENV = "LINDOS_ROOT"
HOME_ENV = "LINDOS_HOME"

_SYSTEM_NAMES = (
    "SYSTEM_CONF_DIR", "SYSTEM_CONF", "SHARE_DIR", "MODES_DIR", "RECIPES_DIR", "LIBEXEC_DIR",
    "HELPER", "HELPER_LOG", "SYSTEM_LOG_DIR", "WALLPAPERS_DIR", "INSTALL_BROWSER_SCRIPT",
    "INSTALL_COMPAT_SCRIPT", "INSTALL_GAMING_SCRIPT", "SYSCTL_MODE_CONF", "APT_KEYRINGS_DIR",
)
_USER_NAMES = (
    "USER_CONF_DIR", "USER_CONF", "SETUP_DONE", "STATE_DIR", "PREFIXES_DIR", "APPS_DB", "LOG_DIR",
    "XFCONF_DIR", "PANEL_DIR", "GTK3_DIR", "GTK4_DIR", "AUTOSTART_DIR",
)


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
    """Apply ``LINDOS_HOME`` (for ``~`` paths) or ``LINDOS_ROOT`` (for absolute paths).

    >>> os.environ["LINDOS_ROOT"] = "/tmp/r"; resolve("/etc/lindos")
    '/tmp/r/etc/lindos'
    """
    if path.startswith("~"):
        rest = path[1:].lstrip("/\\")
        return os.path.normpath(os.path.join(home(), rest)) if rest else home()
    prefix = root()
    if prefix and (path.startswith("/") or os.path.isabs(path)):
        drive, tail = os.path.splitdrive(path)
        return os.path.normpath(os.path.join(prefix, tail.lstrip("/\\")))
    return path


def get(name: str) -> str:
    """Resolve a constant by name, e.g. ``paths.get("MODES_DIR")``."""
    if name not in _SYSTEM_NAMES and name not in _USER_NAMES:
        raise KeyError(name)
    return resolve(globals()[name])


def all_paths() -> Dict[str, str]:
    """Every location, resolved — handy for ``lindos-config paths`` and debugging."""
    return {name: resolve(globals()[name]) for name in _SYSTEM_NAMES + _USER_NAMES}


def ensure_dir(path: str, mode: int = 0o755) -> str:
    """``mkdir -p`` (already resolved path).  Returns the path."""
    os.makedirs(path, mode=mode, exist_ok=True)
    return path


# --- accessor functions ------------------------------------------------------------------
def system_conf_dir() -> str:
    return resolve(SYSTEM_CONF_DIR)


def system_conf() -> str:
    return resolve(SYSTEM_CONF)


def share_dir() -> str:
    return resolve(SHARE_DIR)


def modes_dir() -> str:
    return resolve(MODES_DIR)


def recipes_dir() -> str:
    return resolve(RECIPES_DIR)


def libexec_dir() -> str:
    return resolve(LIBEXEC_DIR)


def helper() -> str:
    return resolve(HELPER)


def helper_log() -> str:
    return resolve(HELPER_LOG)


def system_log_dir() -> str:
    return resolve(SYSTEM_LOG_DIR)


def wallpapers_dir() -> str:
    return resolve(WALLPAPERS_DIR)


def sysctl_mode_conf() -> str:
    return resolve(SYSCTL_MODE_CONF)


def user_conf_dir() -> str:
    return resolve(USER_CONF_DIR)


def user_conf() -> str:
    return resolve(USER_CONF)


def setup_done() -> str:
    return resolve(SETUP_DONE)


def state_dir() -> str:
    return resolve(STATE_DIR)


def prefixes_dir() -> str:
    return resolve(PREFIXES_DIR)


def apps_db() -> str:
    return resolve(APPS_DB)


def log_dir() -> str:
    return resolve(LOG_DIR)


def xfconf_dir() -> str:
    return resolve(XFCONF_DIR)


def panel_dir() -> str:
    return resolve(PANEL_DIR)


def gtk3_dir() -> str:
    return resolve(GTK3_DIR)


def gtk4_dir() -> str:
    return resolve(GTK4_DIR)


def autostart_dir() -> str:
    return resolve(AUTOSTART_DIR)


def mode_dir(mode_id: str) -> str:
    """``/usr/share/lindos/modes/<id>`` (resolved)."""
    return os.path.join(modes_dir(), mode_id)


__all__ = [
    "SYSTEM_CONF_DIR", "SYSTEM_CONF", "SHARE_DIR", "MODES_DIR", "RECIPES_DIR", "LIBEXEC_DIR",
    "HELPER", "HELPER_LOG", "SYSTEM_LOG_DIR", "WALLPAPERS_DIR", "INSTALL_BROWSER_SCRIPT",
    "INSTALL_COMPAT_SCRIPT", "INSTALL_GAMING_SCRIPT", "SYSCTL_MODE_CONF", "APT_KEYRINGS_DIR",
    "USER_CONF_DIR", "USER_CONF", "SETUP_DONE", "STATE_DIR", "PREFIXES_DIR", "APPS_DB", "LOG_DIR",
    "XFCONF_DIR", "PANEL_DIR", "GTK3_DIR", "GTK4_DIR", "AUTOSTART_DIR", "ROOT_ENV", "HOME_ENV",
    "root", "home", "resolve", "get", "all_paths", "ensure_dir",
    "system_conf_dir", "system_conf", "share_dir", "modes_dir", "recipes_dir", "libexec_dir",
    "helper", "helper_log", "system_log_dir", "wallpapers_dir", "sysctl_mode_conf",
    "user_conf_dir", "user_conf", "setup_done", "state_dir", "prefixes_dir", "apps_db",
    "log_dir", "xfconf_dir", "panel_dir", "gtk3_dir", "gtk4_dir", "autostart_dir", "mode_dir",
]
