"""Shared helpers for the lindos-gaming test-suite (importable under any pytest import mode).

The gaming CLIs live in ``root/usr/bin`` without a ``.py`` suffix, so they are
loaded through ``importlib`` (:func:`load_bin`).  Nothing here needs GTK or
Linux: every module is importable on Windows/macOS (SPEC §4/§12).

Test modules import this file by name after putting their own directory on
``sys.path``::

    import os, sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from gaming_testlib import BIN, load_bin

(``from conftest import …`` is deliberately avoided: with
``--import-mode=importlib`` and the repository-level ``tests/`` on
``pythonpath`` the name ``conftest`` resolves to *tests/conftest.py*.)
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                   # packages/lindos-gaming
ROOT = PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SHARE = ROOT / "usr" / "share" / "lindos"
APPS = ROOT / "usr" / "share" / "applications"
ICONS = ROOT / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
ETC = ROOT / "etc"
DEBIAN = PKG_ROOT / "DEBIAN"

CORE_LIB = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"
if CORE_LIB.is_dir() and str(CORE_LIB) not in sys.path:
    sys.path.insert(0, str(CORE_LIB))

_MODULE_CACHE: Dict[str, types.ModuleType] = {}


def load_bin(name: str) -> types.ModuleType:
    """Import ``root/usr/bin/<name>`` (a python3 script without .py) as a module."""
    if name in _MODULE_CACHE:
        return _MODULE_CACHE[name]
    path = BIN / name
    if not path.is_file():
        raise FileNotFoundError(path)
    modname = "lindos_gaming_bin_" + name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    # never leave a __pycache__ inside root/usr/bin (it would end up in the .deb)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    _MODULE_CACHE[name] = module
    return module


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def symlinks_supported(base: Path) -> bool:
    """Windows without developer mode cannot create symlinks; tests degrade gracefully."""
    probe_target = base / "_symlink_target"
    probe_link = base / "_symlink_probe"
    try:
        probe_target.mkdir(exist_ok=True)
        os.symlink(str(probe_target), str(probe_link), target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            if probe_link.is_symlink():
                probe_link.unlink()
        except OSError:
            pass
    return True


def parse_desktop(path: Path) -> Dict[str, Dict[str, str]]:
    """Minimal .desktop parser: {group: {key: value}} (no locale handling needed here)."""
    groups: Dict[str, Dict[str, str]] = {}
    current: Dict[str, str] | None = None
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = groups.setdefault(line[1:-1], {})
            continue
        if current is None or "=" not in line:
            raise ValueError(f"{path}: line outside a group or without '=': {raw!r}")
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return groups


def parse_control(path: Path) -> Dict[str, str]:
    """Parse a DEBIAN/control file into {Field: value} (continuation lines joined)."""
    fields: Dict[str, str] = {}
    key = None
    for raw in read_text(path).splitlines():
        if not raw.strip():
            continue
        if raw[0] in " \t" and key is not None:
            fields[key] += "\n" + raw.strip()
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        fields[key] = value.strip()
    return fields


def split_deps(value: str) -> list:
    """'a (>= 1), b | c, d' -> ['a', 'b | c', 'd'] (versions stripped)."""
    out = []
    for part in value.replace("\n", " ").split(","):
        part = part.strip()
        if not part:
            continue
        alts = [alt.split("(")[0].strip() for alt in part.split("|")]
        out.append(" | ".join(alts))
    return out
