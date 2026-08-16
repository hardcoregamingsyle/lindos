"""Shared helpers for the lindos-tune test-suite (imported by conftest.py and the tests).

Unique module name on purpose: several ``packages/*/tests`` directories are collected in one
pytest run (``--import-mode=importlib``), so ``import conftest`` would be ambiguous.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

_HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = Path(os.path.normpath(os.path.join(_HERE, "..")))
ROOT = PKG_ROOT / "root"
LIB = ROOT / "usr" / "lib" / "lindos-tune"
SHARE = ROOT / "usr" / "share" / "lindos" / "tune"
BIN = ROOT / "usr" / "bin" / "lindos-tune"
CORE_LIB = Path(os.path.normpath(os.path.join(PKG_ROOT, "..", "lindos-core", "root", "usr", "lib", "python3", "dist-packages")))

for _p in (str(LIB), str(CORE_LIB)):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from lindos_tune import common  # noqa: E402
from lindos_tune.common import CmdResult  # noqa: E402

#: files copied from the package tree into every staging root
SHIPPED_FILES = [
    "/usr/share/lindos/tune/services-whitelist.txt",
    "/usr/share/lindos/tune/autostart-hide.list",
    "/usr/share/lindos/tune/ram-budget.json",
    "/usr/share/lindos/tune/earlyoom.default",
    "/usr/lib/systemd/system-preset/90-lindos.preset",
    "/usr/lib/systemd/system/lindos-sensors-detect.service",
    "/etc/sysctl.d/70-lindos-base.conf",
    "/etc/systemd/journald.conf.d/lindos.conf",
    "/etc/tmpfiles.d/lindos.conf",
    "/etc/lindos/tune.d/everyday.conf",
    "/etc/lindos/tune.d/gaming.conf",
    "/etc/lindos/tune.d/work.conf",
    "/etc/lindos/tune.d/creator.conf",
    "/etc/lindos/tune.d/lite.conf",
    "/etc/ananicy.d/lindos/lindos.types",
    "/etc/ananicy.d/lindos/lindos-games.rules",
    "/etc/ananicy.d/lindos/lindos-browsers.rules",
    "/etc/ananicy.d/lindos/lindos-compilers.rules",
]

Matcher = Union[str, Sequence[str], Callable[[List[str]], bool]]


class Recorder:
    """Fake process runner: ``runner(argv, timeout) -> CmdResult``.

    ``responses`` is a list of ``(matcher, result)``; the first matcher that fits wins.  A
    matcher is a substring of the joined argv (``"is-enabled"``), an argv prefix list
    (``--root=…`` is ignored for the comparison), or a callable ``argv -> bool``.  ``result``
    may be a :class:`CmdResult` or a callable ``argv -> CmdResult``.  Unmatched commands succeed
    with empty output.
    """

    def __init__(self, responses: Optional[List[Tuple[Matcher, object]]] = None) -> None:
        self.calls: List[List[str]] = []
        self.responses: List[Tuple[Matcher, object]] = list(responses or [])

    def add(self, matcher: Matcher, result: object) -> "Recorder":
        self.responses.append((matcher, result))
        return self

    @staticmethod
    def _strip_root(argv: List[str]) -> List[str]:
        return [a for a in argv if not a.startswith("--root=")]

    def __call__(self, argv: Sequence[str], timeout: float = 0) -> CmdResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        plain = self._strip_root(argv)
        joined = " ".join(plain)
        for matcher, result in self.responses:
            if callable(matcher):
                hit = bool(matcher(plain))
            elif isinstance(matcher, str):
                hit = matcher in joined
            else:
                m = list(matcher)
                hit = plain[: len(m)] == m
            if hit:
                return result(plain) if callable(result) else result  # type: ignore[operator]
        return CmdResult(True, 0, "")

    def verbs(self, prog: str = "systemctl") -> List[str]:
        """The verbs used with *prog* (``--root=`` skipped)."""
        out: List[str] = []
        for argv in self.calls:
            plain = self._strip_root(argv)
            if plain and plain[0] == prog and len(plain) > 1:
                out.append(plain[1])
        return out

    def find(self, *prefix: str) -> List[List[str]]:
        return [argv for argv in self.calls if self._strip_root(argv)[: len(prefix)] == list(prefix)]


def write(root: Path, system_path: str, content: str, mode: int = 0o644) -> Path:
    """Write *content* to ``root/<system_path>`` (LF, parents created)."""
    target = Path(common.path(system_path, str(root)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    try:
        os.chmod(target, mode)
    except OSError:
        pass
    return target


def read(root: Path, system_path: str) -> Optional[str]:
    target = Path(common.path(system_path, str(root)))
    return target.read_text(encoding="utf-8") if target.exists() else None


def unit(root: Path, name: str, body: str = "[Unit]\nDescription=fake\n[Service]\nExecStart=/bin/true\n") -> Path:
    """Create a fake unit file so ``ctx.unit_exists(name)`` is true."""
    return write(root, f"/usr/lib/systemd/system/{name}", body)


def core_available() -> bool:
    try:
        import lindos.helper  # noqa: F401
    except Exception:
        return False
    return True


__all__ = ["PKG_ROOT", "ROOT", "LIB", "SHARE", "BIN", "CORE_LIB", "SHIPPED_FILES", "Recorder", "write", "read",
           "unit", "core_available"]
