"""Performance environment for Proton/umu game launches + gamescope wrapping.

SPEC-KERNEL §17.1 / §17.2.  Two jobs, both pure and testable on any OS:

* :func:`perf_env` — the documented performance environment for a Proton/umu game
  launch, set **only when the value is not already in the environment** so a user
  override always wins:

    ``PROTON_USE_NTSYNC=1``   iff ``/dev/ntsync`` exists (``LINDOS_ROOT``-aware; when
                              it is absent Proton silently falls back to fsync);
    ``DXVK_ASYNC=1``          (``--dxvk-async``/``--no-dxvk-async`` force it on/off;
                              otherwise a user ``DXVK_ASYNC`` in the environment wins);
    ``PROTON_HIDE_NVIDIA_GPU=0``   (documented default; user override respected);
    ``DXVK_HDR=1``            only when ``--hdr`` was asked for.

  ``PROTON_ENABLE_WAYLAND`` is deliberately left untouched; ``WINEFSYNC``/``WINEESYNC``
  are handled by umu.  Nothing here changes a plain-Wine/app launch.

* :func:`gamescope_wrap` — wrap a command in gamescope (SPEC-KERNEL §17.2).  It prefers
  the ``lindos-gamescope`` helper (lindos-gaming; the documented wrapper), falls back to
  the raw ``gamescope`` binary, and degrades to the bare command with a warning when
  neither is installed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "NTSYNC_DEV",
    "PERF_KEYS",
    "GamescopeSpec",
    "ntsync_dev_path",
    "ntsync_available",
    "is_proton_launch",
    "perf_env",
    "parse_geometry",
    "gamescope_wrap",
]

#: Character device created by the ntsync kernel module (SPEC-KERNEL §15.1).
NTSYNC_DEV = "/dev/ntsync"

#: The documented performance keys this layer may set (order = display order).
PERF_KEYS = ("PROTON_USE_NTSYNC", "DXVK_ASYNC", "PROTON_HIDE_NVIDIA_GPU", "DXVK_HDR")

_GEOMETRY_RE = re.compile(r"^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$")


# ---------------------------------------------------------------------------
# ntsync probe (LINDOS_ROOT-aware, guarded)
# ---------------------------------------------------------------------------


def ntsync_dev_path(root: Optional[str] = None) -> Path:
    """Path of ``/dev/ntsync`` with ``LINDOS_ROOT`` applied (so tests can fake it)."""
    if root is None:
        root = os.environ.get("LINDOS_ROOT")
    if root:
        return Path(root.rstrip("/\\")) / "dev" / "ntsync"
    return Path(NTSYNC_DEV)


def ntsync_available(root: Optional[str] = None,
                     exists: Callable[[str], bool] = os.path.exists) -> bool:
    """True when the ntsync device node is present (Lindos kernel with ``CONFIG_NTSYNC``)."""
    try:
        return bool(exists(str(ntsync_dev_path(root))))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Perf environment
# ---------------------------------------------------------------------------


def is_proton_launch(kind: str, runner: str) -> bool:
    """Whether the documented perf env applies (Proton/umu, or a game — which routes to umu)."""
    return runner == "umu" or kind == "game"


def perf_env(
    *,
    kind: str,
    runner: str,
    environ: Optional[Mapping[str, str]] = None,
    dxvk_async: Optional[bool] = None,
    hdr: bool = False,
    ntsync: Optional[bool] = None,
    root: Optional[str] = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> Dict[str, str]:
    """Resolve the documented perf env for a launch (SPEC-KERNEL §17.1).

    Returns only the keys that should be *added*: a key already present in ``environ``
    is left out so the user's own value wins (``dxvk_async`` True/False is an explicit
    override and always produces a value).  Returns ``{}`` for wine/app launches.
    """
    if not is_proton_launch(kind, runner):
        return {}
    environ = os.environ if environ is None else environ
    out: Dict[str, str] = {}

    if ntsync is None:
        ntsync = ntsync_available(root, exists=exists)
    if ntsync and "PROTON_USE_NTSYNC" not in environ:
        out["PROTON_USE_NTSYNC"] = "1"
    # (ntsync absent -> add nothing; Proton falls back to fsync silently)

    if dxvk_async is True:
        out["DXVK_ASYNC"] = "1"
    elif dxvk_async is False:
        out["DXVK_ASYNC"] = "0"
    elif "DXVK_ASYNC" not in environ:
        out["DXVK_ASYNC"] = "1"

    if "PROTON_HIDE_NVIDIA_GPU" not in environ:
        out["PROTON_HIDE_NVIDIA_GPU"] = "0"

    if hdr and "DXVK_HDR" not in environ:
        out["DXVK_HDR"] = "1"

    return out


# ---------------------------------------------------------------------------
# gamescope
# ---------------------------------------------------------------------------


@dataclass
class GamescopeSpec:
    """A gamescope wrapping request (``--gamescope [WxH] --hdr --fsr`` or a profile block)."""

    enabled: bool = False
    width: Optional[int] = None
    height: Optional[int] = None
    hdr: bool = False
    fsr: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {"enabled": self.enabled, "width": self.width, "height": self.height,
                "hdr": self.hdr, "fsr": self.fsr}


def parse_geometry(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """``"1920x1080"`` -> ``(1920, 1080)``; empty/invalid -> ``(None, None)``."""
    if not text:
        return None, None
    m = _GEOMETRY_RE.match(str(text))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _lindos_gamescope_argv(tool: str, spec: GamescopeSpec) -> List[str]:
    argv = [tool]
    if spec.width:
        argv += ["--width", str(spec.width)]
    if spec.height:
        argv += ["--height", str(spec.height)]
    if spec.hdr:
        argv.append("--hdr")
    if spec.fsr:
        argv.append("--fsr")
    return argv


def _raw_gamescope_argv(tool: str, spec: GamescopeSpec) -> List[str]:
    argv = [tool]
    if spec.width:
        argv += ["-W", str(spec.width)]
    if spec.height:
        argv += ["-H", str(spec.height)]
    if spec.fsr:
        argv += ["-F", "fsr"]
    if spec.hdr:
        argv.append("--hdr-enabled")
    argv.append("-f")  # fullscreen: the game owns the gamescope surface
    return argv


def gamescope_wrap(
    argv: List[str],
    spec: Optional[GamescopeSpec],
    *,
    which: Callable[[str], Optional[str]],
) -> Tuple[List[str], List[str]]:
    """Return ``(argv, warnings)`` wrapping ``argv`` in gamescope when ``spec.enabled``.

    Prefers ``lindos-gamescope`` (lindos-gaming), then the raw ``gamescope`` binary; if
    neither exists it degrades to the bare command with a clear warning (SPEC-KERNEL §17.2).
    """
    warnings: List[str] = []
    if not spec or not spec.enabled:
        return argv, warnings
    helper = which("lindos-gamescope")
    if helper:
        return _lindos_gamescope_argv(helper, spec) + ["--"] + argv, warnings
    raw = which("gamescope")
    if raw:
        return _raw_gamescope_argv(raw, spec) + ["--"] + argv, warnings
    warnings.append("gamescope requested but not installed - running without it "
                    "(apt install gamescope, or install lindos-gaming)")
    return argv, warnings
