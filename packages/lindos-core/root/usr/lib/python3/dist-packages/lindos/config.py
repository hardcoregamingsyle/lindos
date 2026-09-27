"""User and system configuration (SPEC §3 storage, §4.2).

* User config: ``~/.config/lindos/config.json`` — :class:`Config` (dict-like, atomic save).
* System config: ``/etc/lindos/system.json`` — ``{"mode": …, "browser": …, "oem": …}``,
  written only by root (``lindos-helper write-system-config``).

The user mode/browser override the system one (:func:`effective_mode`,
:func:`effective_browser`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Iterator, Optional

from . import paths

log = logging.getLogger("lindos.config")

DEFAULTS: Dict[str, Any] = {
    "mode": "everyday",
    "browser": "chrome",
    "theme": "dark",
    "accent": "#60CDFF",
    "wallpaper": "/usr/share/backgrounds/lindos/aurora-dark.svg",
    "setup_done": False,
    "gamemode_auto": True,
    "mangohud": False,
    "telemetry": False,
    "schema": 1,
}

SYSTEM_DEFAULTS: Dict[str, Any] = {"mode": "everyday", "browser": "chrome", "oem": False}

VALID_MODES = ("everyday", "gaming", "work", "creator", "lite")
VALID_BROWSERS = ("edge", "chrome", "firefox")
VALID_THEMES = ("dark", "light")


# --- low level helpers -------------------------------------------------------------------
def read_json(path: str) -> Dict[str, Any]:
    """Read a JSON object from *path*; ``{}`` when missing, unreadable or not an object."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s does not contain a JSON object; ignoring", path)
        return {}
    return data


def atomic_write_json(path: str, data: Dict[str, Any], mode: int = 0o644) -> None:
    """Write *data* as pretty JSON to *path* atomically (temp file + ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lindos-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- user config -------------------------------------------------------------------------
class Config:
    """User configuration, dict-like.  ``Config.load()`` → ``.get()/.set()`` → ``.save()``."""

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[str] = None) -> None:
        self._data: Dict[str, Any] = dict(DEFAULTS)
        if data:
            self._data.update(data)
        self._path = path or paths.user_conf()

    # -- construction / persistence ---------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """Load ``~/.config/lindos/config.json`` merged over :data:`DEFAULTS`."""
        p = path or paths.user_conf()
        return cls(read_json(p), path=p)

    def save(self) -> None:
        """Atomically write the config file (creating parent directories)."""
        atomic_write_json(self._path, self._data)

    @property
    def path(self) -> str:
        return self._path

    @property
    def exists(self) -> bool:
        return os.path.isfile(self._path)

    # -- dict-like API --------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    def values(self):
        return self._data.values()

    def update(self, other: Dict[str, Any]) -> None:
        self._data.update(other)

    def setdefault(self, key: str, value: Any) -> Any:
        return self._data.setdefault(key, value)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"Config({self._path!r}, {self._data!r})"


def user_config_exists() -> bool:
    return os.path.isfile(paths.user_conf())


# --- system config -----------------------------------------------------------------------
def load_system(path: Optional[str] = None) -> Dict[str, Any]:
    """``/etc/lindos/system.json`` merged over :data:`SYSTEM_DEFAULTS`."""
    data = dict(SYSTEM_DEFAULTS)
    data.update(read_json(path or paths.system_conf()))
    return data


def save_system(data: Dict[str, Any], path: Optional[str] = None) -> None:
    """Atomically write the system config (needs root on a real system).

    Only the documented keys are written; unknown keys are preserved from the existing file
    so that OEM additions survive.
    """
    p = path or paths.system_conf()
    merged = load_system(p)
    merged.update(data)
    atomic_write_json(p, merged, mode=0o644)


# --- effective values --------------------------------------------------------------------
def _valid(value: Any, choices: tuple) -> Optional[str]:
    return value if isinstance(value, str) and value in choices else None


def effective_mode() -> str:
    """User override else system default else ``"everyday"``."""
    user = _valid(read_json(paths.user_conf()).get("mode"), VALID_MODES)
    if user:
        return user
    system = _valid(load_system().get("mode"), VALID_MODES)
    return system or "everyday"


def effective_browser() -> str:
    """User override else system default (``"chrome"``) else ``"firefox"``.

    The final ``"firefox"`` fallback only fires if ``/etc/lindos/system.json`` itself is
    missing/corrupt: Firefox is the one browser guaranteed to be on the ISO, so it is the safe
    answer when nothing else is known, even though the OOBE/system default is Chrome.
    """
    user = _valid(read_json(paths.user_conf()).get("browser"), VALID_BROWSERS)
    if user:
        return user
    system = _valid(load_system().get("browser"), VALID_BROWSERS)
    return system or "firefox"


def is_setup_done() -> bool:
    """True when the OOBE finished (marker file or ``setup_done`` in the user config)."""
    if os.path.exists(paths.setup_done()):
        return True
    return bool(read_json(paths.user_conf()).get("setup_done", False))


def parse_value(text: str) -> Any:
    """Turn a CLI string into a JSON-ish value: ``true``→True, ``5``→5, else the string."""
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


__all__ = [
    "DEFAULTS", "SYSTEM_DEFAULTS", "VALID_MODES", "VALID_BROWSERS", "VALID_THEMES",
    "Config", "read_json", "atomic_write_json", "user_config_exists",
    "load_system", "save_system", "effective_mode", "effective_browser", "is_setup_done",
    "parse_value",
]
