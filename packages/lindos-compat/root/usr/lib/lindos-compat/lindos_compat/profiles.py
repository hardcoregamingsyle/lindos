"""Per-title gaming profiles (SPEC-KERNEL §17.3).

A profile records how one Windows game should launch: its runner, Proton build,
extra environment, gamescope settings, DXVK/MangoHud toggles and an honest status.

Files: ``/usr/share/lindos/gaming/profiles/*.json`` (shipped), overridden per-id by
``~/.config/lindos/gaming/profiles/*.json``.  Resolution matches an executable file
name (case-insensitive) or a Steam appid.

Schema 1::

    { "schema": 1, "id": "...", "title": "...",
      "match": {"exe": ["game.exe"], "steam_appid": [1234]},
      "runner": "umu"|"wine", "proton": "GE-Proton-latest"|"<tag>",
      "env": {"KEY": "VALUE"},
      "gamescope": {"w": 2560, "h": 1440, "fsr": true, "hdr": false},
      "dxvk_async": true, "mangohud": false,
      "notes": "...", "status": "works"|"partial"|"native"|"not_possible" }

A ``not_possible`` profile only documents *why* and fakes nothing: it never claims a
runner or environment that would defeat an anti-cheat (SPEC-KERNEL §14).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import expand_user_path, get_logger, path_const
from .perf import GamescopeSpec

__all__ = [
    "SCHEMA",
    "STATUSES",
    "Profile",
    "system_profiles_dir",
    "user_profiles_dir",
    "load_profiles",
    "resolve_profile",
    "gamescope_from_profile",
]

log = get_logger("lindos-compat.profiles")

SCHEMA = 1
STATUSES = ("works", "partial", "native", "not_possible")


@dataclass
class Profile:
    id: str
    title: str = ""
    match_exe: List[str] = field(default_factory=list)
    match_appid: List[str] = field(default_factory=list)
    runner: str = ""
    proton: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    gamescope: Dict[str, object] = field(default_factory=dict)
    dxvk_async: Optional[bool] = None
    mangohud: Optional[bool] = None
    notes: str = ""
    status: str = "works"
    source: str = ""  # "system" | "user"

    @property
    def possible(self) -> bool:
        """False for a ``not_possible`` profile (anti-cheat titles etc.)."""
        return self.status != "not_possible"

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "match": {"exe": list(self.match_exe), "steam_appid": list(self.match_appid)},
            "runner": self.runner,
            "proton": self.proton,
            "env": dict(self.env),
            "gamescope": dict(self.gamescope),
            "dxvk_async": self.dxvk_async,
            "mangohud": self.mangohud,
            "notes": self.notes,
            "status": self.status,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------


def system_profiles_dir() -> Path:
    """``/usr/share/lindos/gaming/profiles`` (LINDOS_ROOT-aware)."""
    override = os.environ.get("LINDOS_PROFILES_DIR")
    if override:
        return Path(override)
    return Path(path_const("SHARE_DIR")) / "gaming" / "profiles"


def user_profiles_dir() -> Path:
    """``~/.config/lindos/gaming/profiles`` (LINDOS_HOME-aware)."""
    override = os.environ.get("LINDOS_USER_PROFILES_DIR")
    if override:
        return Path(override)
    return expand_user_path(path_const("USER_CONF_DIR")) / "gaming" / "profiles"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _as_str_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _as_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def profile_from_dict(data: Dict[str, object], *, source: str = "", fallback_id: str = "") -> Optional[Profile]:
    """Build a :class:`Profile` from parsed JSON (schema 1); ``None`` when unusable."""
    if not isinstance(data, dict):
        return None
    schema = data.get("schema", SCHEMA)
    try:
        if int(schema) != SCHEMA:
            log.warning("profile %s: unsupported schema %s (expected %s)", fallback_id or data.get("id"), schema, SCHEMA)
            return None
    except (TypeError, ValueError):
        return None
    pid = str(data.get("id") or fallback_id).strip()
    if not pid:
        return None
    match = data.get("match") if isinstance(data.get("match"), dict) else {}
    env_raw = data.get("env") if isinstance(data.get("env"), dict) else {}
    gs_raw = data.get("gamescope") if isinstance(data.get("gamescope"), dict) else {}
    status = str(data.get("status") or "works").strip().lower()
    if status not in STATUSES:
        status = "works"
    return Profile(
        id=pid,
        title=str(data.get("title") or pid),
        match_exe=[e.lower() for e in _as_str_list(match.get("exe"))],
        match_appid=_as_str_list(match.get("steam_appid")),
        runner=str(data.get("runner") or "").strip().lower(),
        proton=str(data.get("proton") or "").strip(),
        env={str(k): str(v) for k, v in env_raw.items()},
        gamescope=dict(gs_raw),
        dxvk_async=_as_bool(data.get("dxvk_async")),
        mangohud=_as_bool(data.get("mangohud")),
        notes=str(data.get("notes") or ""),
        status=status,
        source=source,
    )


def _load_dir(directory: Path, source: str, into: Dict[str, Profile]) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("cannot read profile %s: %s", path, exc)
            continue
        prof = profile_from_dict(data, source=source, fallback_id=path.stem)
        if prof is not None:
            into[prof.id] = prof  # later source (user) overrides by id


def load_profiles(system_dir: Optional[Path] = None, user_dir: Optional[Path] = None) -> Dict[str, Profile]:
    """All profiles by id; a user profile with the same id replaces the shipped one."""
    profiles: Dict[str, Profile] = {}
    _load_dir(system_dir or system_profiles_dir(), "system", profiles)
    _load_dir(user_dir or user_profiles_dir(), "user", profiles)
    return profiles


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def resolve_profile(
    *,
    exe: Optional[str] = None,
    appid: Optional[str] = None,
    profiles: Optional[Dict[str, Profile]] = None,
) -> Optional[Profile]:
    """Find the profile matching an executable name or a Steam appid (appid wins)."""
    if profiles is None:
        profiles = load_profiles()
    if not profiles:
        return None
    if appid:
        aid = str(appid).strip()
        for prof in profiles.values():
            if aid in prof.match_appid:
                return prof
    if exe:
        name = os.path.basename(str(exe)).lower()
        for prof in profiles.values():
            if name in prof.match_exe:
                return prof
    return None


def gamescope_from_profile(prof: Profile) -> GamescopeSpec:
    """A :class:`~lindos_compat.perf.GamescopeSpec` from a profile's ``gamescope`` block."""
    gs = prof.gamescope or {}

    def _int(key: str) -> Optional[int]:
        val = gs.get(key)
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    width = _int("w") if "w" in gs else _int("width")
    height = _int("h") if "h" in gs else _int("height")
    return GamescopeSpec(
        enabled=bool(gs),
        width=width,
        height=height,
        hdr=bool(gs.get("hdr", False)),
        fsr=bool(gs.get("fsr", False)),
    )
