"""pytest bootstrap for lindos-transfer.

* Puts ``root/usr/lib/lindos-transfer`` (``import lindos_transfer``) and lindos-core's
  ``dist-packages`` (``import lindos``) on ``sys.path`` (mirrors the repository-level
  ``tests/conftest.py`` and the other packages' ``tests/conftest.py``).
* ``home``: ``LINDOS_HOME``/``LINDOS_ROOT`` point at scratch directories (SPEC §4.1) so every test
  is hermetic (no network, no real devices, no touching the machine this runs on).
* ``fake_run``/``fake_which``: injectable ``subprocess.run``/``shutil.which`` stand-ins.
* ``fake_helper``: a small stand-in for ``lindos.helper`` (records every call; never actually
  invokes pkexec/sudo) -- including the ``stdin_payload`` keyword and the ``import-wifi`` action
  that :mod:`lindos_transfer.wifi` uses ahead of lindos-core adding them (SPEC-WINDOWS §29.8).
* ``winbuild``: a small helper that assembles a synthetic Windows partition (``Windows/``,
  ``Users/<name>/NTUSER.DAT``, ``Windows/System32/config/{SOFTWARE,SYSTEM}``) out of real regf
  bytes built with :mod:`hive_builder`, for the modules that need a whole :class:`Source`.

Nothing here reads or writes outside ``tmp_path``; no real Windows drive, Wine or root privilege is
ever needed.
"""
from __future__ import annotations

import struct
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                          # packages/lindos-transfer
LIB = PKG_ROOT / "root" / "usr" / "lib" / "lindos-transfer"
CORE_LIB = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"

for _p in (LIB, CORE_LIB, HERE):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hive_builder import HiveBuilder, REG_DWORD, REG_SZ  # noqa: E402  (needs the sys.path tweak above)

REG_EXPAND_SZ = 2


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #
@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch ``LINDOS_HOME`` (and ``LINDOS_ROOT``); returns the home path."""
    h = tmp_path / "home"
    r = tmp_path / "root"
    h.mkdir()
    r.mkdir()
    monkeypatch.setenv("LINDOS_HOME", str(h))
    monkeypatch.setenv("LINDOS_ROOT", str(r))
    monkeypatch.delenv("LINDOS_DEBUG", raising=False)
    return h


# --------------------------------------------------------------------------- #
# run / which
# --------------------------------------------------------------------------- #
class FakeRun:
    """Records every ``run(argv, ...)`` call; ``rc_for``/``out_for`` customise the reply."""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.rc_for: Callable[[List[str]], int] = lambda argv: 0
        self.out_for: Callable[[List[str]], str] = lambda argv: ""
        self.err_for: Callable[[List[str]], str] = lambda argv: ""

    def __call__(self, argv: List[str], *args: Any, **kwargs: Any) -> Any:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        return types.SimpleNamespace(returncode=self.rc_for(argv), stdout=self.out_for(argv),
                                     stderr=self.err_for(argv), args=argv)

    def has(self, *tokens: str) -> bool:
        def hit(argv: List[str], token: str) -> bool:
            return any(a == token for a in argv)

        return any(all(hit(c, t) for t in tokens) for c in self.calls)


@pytest.fixture()
def fake_run() -> FakeRun:
    return FakeRun()


@pytest.fixture()
def fake_which() -> Callable[..., Callable[[str], Optional[str]]]:
    """Factory: ``fake_which("udisksctl", "lsblk")`` -> a ``which`` that knows only those tools."""

    def factory(*names: str, prefix: str = "/usr/bin") -> Callable[[str], Optional[str]]:
        table = {n: f"{prefix}/{n}" for n in names}
        return lambda name: table.get(name)

    return factory


# --------------------------------------------------------------------------- #
# fake lindos.helper
# --------------------------------------------------------------------------- #
class FakeHelperResult:
    def __init__(self, ok: bool = True, message: str = "ok") -> None:
        self.ok = ok
        self.out = message if ok else ""
        self.err = "" if ok else message
        self.code = 0 if ok else 1

    def __bool__(self) -> bool:
        return self.ok

    @property
    def message(self) -> str:
        return self.out if self.ok else self.err


class FakeHelper:
    """Stand-in for ``lindos.helper``: never runs pkexec/sudo, just records what would happen.

    Also exposes ``import-wifi`` (payload validated for shape only) ahead of lindos-core's own
    ``stdin_payload`` support (SPEC-WINDOWS §29.8 cross-owner note: W-G1 adds this to the real
    module) -- ``run_privileged`` here accepts the keyword so :mod:`lindos_transfer.wifi` can be
    tested against it without the real helper installed.
    """

    ACTIONS = ["install-packages", "install-flatpaks", "install-browser", "install-gaming", "import-wifi"]

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.result: Any = FakeHelperResult(True, "ok")
        self.result_for: Callable[[str, Dict[str, Any]], Any] = lambda action, payload: self.result

    def run_privileged(self, action: str, payload: Optional[Dict[str, Any]] = None, *,
                       stdin_payload: bool = False, log: Any = None, timeout: Any = None) -> Any:
        payload = dict(payload or {})
        self.calls.append((action, payload))
        return self.result_for(action, payload)

    def install_packages(self, packages: Any) -> Any:
        return self.run_privileged("install-packages", {"packages": list(packages)})

    def install_flatpaks(self, ids: Any) -> Any:
        return self.run_privileged("install-flatpaks", {"flatpaks": list(ids)})

    def install_browser(self, browser: str) -> Any:
        return self.run_privileged("install-browser", {"browser": browser})

    def install_gaming(self, items: Any) -> Any:
        return self.run_privileged("install-gaming", {"items": list(items)})


@pytest.fixture()
def fake_helper() -> FakeHelper:
    return FakeHelper()


# --------------------------------------------------------------------------- #
# synthetic Windows hives / partitions
# --------------------------------------------------------------------------- #
def _utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def build_software_hive(*, users: Optional[Dict[str, str]] = None, product: str = "Windows 11 Pro",
                        build: str = "26100", display_version: str = "24H2",
                        steam_path: Optional[str] = None, dirty: bool = False,
                        with_uninstall_entries: bool = True) -> bytes:
    """A minimal ``SOFTWARE`` hive: ``Microsoft\\Windows NT\\CurrentVersion`` (+ ProfileList),
    ``Microsoft\\Windows\\CurrentVersion\\Uninstall`` with a couple of programs (unless
    *with_uninstall_entries* is False, which omits the Uninstall key entirely)."""
    b = HiveBuilder(minor=5)
    profiles = []
    for i, (sid, path) in enumerate((users or {}).items()):
        profiles.append({"name": sid, "values": [("ProfileImagePath", REG_SZ, _utf16z(path))]})
    uninstall_entries = [
        {"name": "{GUID-1}", "values": [("DisplayName", REG_SZ, _utf16z("VLC media player")),
                                        ("DisplayVersion", REG_SZ, _utf16z("3.0.20")),
                                        ("Publisher", REG_SZ, _utf16z("VideoLAN")),
                                        ("EstimatedSize", REG_DWORD, (51200).to_bytes(4, "little"))]},
        {"name": "{GUID-2}", "values": [("DisplayName", REG_SZ, _utf16z("Update for Windows (KB5000001)")),
                                        ("SystemComponent", REG_DWORD, (0).to_bytes(4, "little"))]},
        {"name": "{GUID-3}", "values": [("SystemComponent", REG_DWORD, (1).to_bytes(4, "little"))]},
    ] if with_uninstall_entries else []
    if steam_path:
        valve = {"name": "Valve", "children": [
            {"name": "Steam", "values": [("InstallPath", REG_SZ, _utf16z(steam_path))]}]}
    else:
        valve = {"name": "Valve", "children": []}
    spec = {
        "name": "ROOT",
        "children": [
            {"name": "Microsoft", "children": [
                {"name": "Windows NT", "children": [
                    {"name": "CurrentVersion", "values": [
                        ("ProductName", REG_SZ, _utf16z(product)),
                        ("CurrentBuildNumber", REG_SZ, _utf16z(build)),
                        ("CurrentMajorVersionNumber", REG_DWORD, (10).to_bytes(4, "little")),
                        ("CurrentMinorVersionNumber", REG_DWORD, (0).to_bytes(4, "little")),
                        ("DisplayVersion", REG_SZ, _utf16z(display_version)),
                    ], "children": [
                        {"name": "ProfileList", "children": profiles},
                    ]},
                ]},
                {"name": "Windows", "children": [
                    {"name": "CurrentVersion", "children": [
                        {"name": "Uninstall", "children": uninstall_entries},
                    ]},
                ]},
                valve,
            ]},
        ],
    }
    root = b.add_key_tree(spec)
    return b.to_bytes(root_offset=root, seq_secondary=0 if dirty else 1)


def build_system_hive(*, computer: str = "DESKTOP-1", hiberboot: Optional[bool] = None,
                      mounted_devices: Optional[Dict[str, bytes]] = None) -> bytes:
    """A minimal ``SYSTEM`` hive: ``Select``, ``ControlSet001`` (ComputerName, HiberbootEnabled),
    ``MountedDevices``."""
    b = HiveBuilder(minor=5)
    power_values = []
    if hiberboot is not None:
        power_values.append(("HiberbootEnabled", REG_DWORD, (1 if hiberboot else 0).to_bytes(4, "little")))
    mounted = {"name": "MountedDevices",
              "values": [(f"\\DosDevices\\{letter}", 3, raw) for letter, raw in (mounted_devices or {}).items()]}
    spec = {
        "name": "ROOT",
        "children": [
            {"name": "Select", "values": [("Current", REG_DWORD, (1).to_bytes(4, "little"))]},
            {"name": "ControlSet001", "children": [
                {"name": "Control", "children": [
                    {"name": "ComputerName", "children": [
                        {"name": "ComputerName", "values": [("ComputerName", REG_SZ, _utf16z(computer))]},
                    ]},
                    {"name": "Session Manager", "children": [
                        {"name": "Power", "values": power_values},
                    ]},
                ]},
            ]},
            mounted,
        ],
    }
    root = b.add_key_tree(spec)
    return b.to_bytes(root_offset=root)


def build_ntuser_hive(*, shell_folders: Optional[Dict[str, str]] = None,
                     wallpaper: Optional[Dict[str, str]] = None) -> bytes:
    """A minimal ``NTUSER.DAT``: ``...\\Explorer\\User Shell Folders``, ``Control Panel\\Desktop``."""
    b = HiveBuilder(minor=5)
    folders = [(name, REG_EXPAND_SZ, _utf16z(path)) for name, path in (shell_folders or {}).items()]
    desktop_values = [(k, REG_SZ, _utf16z(v)) for k, v in (wallpaper or {}).items()]
    spec = {
        "name": "ROOT",
        "children": [
            {"name": "Software", "children": [
                {"name": "Microsoft", "children": [
                    {"name": "Windows", "children": [
                        {"name": "CurrentVersion", "children": [
                            {"name": "Explorer", "children": [
                                {"name": "User Shell Folders", "values": folders},
                            ]},
                        ]},
                    ]},
                ]},
            ]},
            {"name": "Control Panel", "children": [
                {"name": "Desktop", "values": desktop_values},
            ]},
        ],
    }
    root = b.add_key_tree(spec)
    return b.to_bytes(root_offset=root)


def make_windows_root(base: Path, *, user: str = "alice", sid: str = "S-1-5-21-1-2-3-1001",
                      software: Optional[bytes] = None, system: Optional[bytes] = None,
                      ntuser: Optional[bytes] = None, extra_files: Optional[Dict[str, bytes]] = None) -> Path:
    """A synthetic Windows partition root under *base*: ``Windows/System32/config/{SOFTWARE,SYSTEM}``
    and ``Users/<user>/NTUSER.DAT`` (real regf bytes unless overridden), ready for
    :func:`lindos_transfer.sources.open_source`."""
    root = base / "winroot"
    config = root / "Windows" / "System32" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "SOFTWARE").write_bytes(software if software is not None else
                                      build_software_hive(users={sid: f"C:\\Users\\{user}"}))
    (config / "SYSTEM").write_bytes(system if system is not None else build_system_hive())
    profile = root / "Users" / user
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "NTUSER.DAT").write_bytes(ntuser if ntuser is not None else build_ntuser_hive())
    for rel, data in (extra_files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.fixture()
def winbuild():
    """Namespace of the synthetic-hive helpers above (as a simple object with attributes)."""
    return types.SimpleNamespace(
        software=build_software_hive, system=build_system_hive, ntuser=build_ntuser_hive,
        root=make_windows_root, HiveBuilder=HiveBuilder,
    )
