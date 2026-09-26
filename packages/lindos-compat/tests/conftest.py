"""pytest bootstrap for lindos-compat.

* Puts ``root/usr/lib/lindos-compat`` (``import lindos_compat``) and lindos-core's
  ``dist-packages`` (``import lindos``) on ``sys.path`` so the tests run straight from the
  source tree (mirrors the repository-level ``tests/conftest.py``).
* ``fake_core`` fixture: installs a small, deterministic stand-in for ``lindos.compat``
  (``analyze_exe``, ``ExeInfo``, ``slugify``, ``apps_db_load/save``, ``choose_runner``) so
  the tests do not depend on the real analyzer and never touch the real home directory.
* ``home`` fixture: ``LINDOS_HOME`` / ``LINDOS_ROOT`` → scratch directories (SPEC §4.1).

Everything works on Windows/macOS: no Wine, no Linux calls.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import types
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                        # packages/lindos-compat
ROOT = PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIB = ROOT / "usr" / "lib" / "lindos-compat"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
SHARE = ROOT / "usr" / "share" / "lindos"
RECIPES = SHARE / "recipes"
APPS = ROOT / "usr" / "share" / "applications"
DEBIAN = PKG_ROOT / "DEBIAN"
CORE_LIB = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"

for _p in (LIB, CORE_LIB):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from lindos_compat import lnk as _lnk  # noqa: E402  (needs the sys.path tweak above)


# --------------------------------------------------------------------------- #
# fake lindos.compat
# --------------------------------------------------------------------------- #
@dataclass
class FakeExeInfo:
    """Mirror of SPEC §4.8 ``ExeInfo``."""

    path: str
    name: str
    kind: str = "unknown"
    arch: str = "unknown"
    installer_type: Optional[str] = None
    product: str = ""
    company: str = ""
    sha256_prefix: str = ""
    markers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fake_slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "app"


def build_fake_core(home: Path) -> types.ModuleType:
    """A ``lindos.compat`` stand-in whose apps DB lives under ``home``."""
    mod = types.ModuleType("lindos.compat")
    db_path = home / ".local" / "share" / "lindos" / "apps.json"

    def analyze_exe(path: str) -> FakeExeInfo:
        p = Path(path)
        lower = p.name.lower()
        kind = "unknown"
        itype: Optional[str] = None
        if lower.endswith(".msi"):
            kind, itype = "msi", "msi"
        elif "setup" in lower or "install" in lower:
            kind, itype = "installer", "nsis"
        elif "game" in lower:
            kind = "game"
        elif lower.endswith(".exe"):
            kind = "app"
        arch = "x64" if "x64" in lower or "64" in lower else "unknown"
        product = getattr(mod, "PRODUCT_OVERRIDES", {}).get(p.name, "")
        return FakeExeInfo(path=str(p), name=p.name, kind=kind, arch=arch, installer_type=itype,
                           product=product, company="", sha256_prefix="deadbeefcafebabe")

    def apps_db_load() -> Dict[str, Any]:
        try:
            with open(db_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def apps_db_save(db: Dict[str, Any]) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = db_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2, sort_keys=True)
        os.replace(tmp, db_path)

    def choose_runner(info: Any, config: Any) -> str:
        kind = str(getattr(info, "kind", "unknown"))
        return "umu" if kind in ("game", "unknown") else "wine"

    mod.ExeInfo = FakeExeInfo  # type: ignore[attr-defined]
    mod.analyze_exe = analyze_exe  # type: ignore[attr-defined]
    mod.slugify = _fake_slugify  # type: ignore[attr-defined]
    mod.apps_db_load = apps_db_load  # type: ignore[attr-defined]
    mod.apps_db_save = apps_db_save  # type: ignore[attr-defined]
    mod.choose_runner = choose_runner  # type: ignore[attr-defined]
    mod.PRODUCT_OVERRIDES = {}  # type: ignore[attr-defined]
    mod.APPS_DB_PATH = db_path  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------- #
# fixtures
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
    monkeypatch.setenv("LINDOS_NO_GUI", "1")
    monkeypatch.delenv("PROTONPATH", raising=False)
    return h


@pytest.fixture()
def fake_core(home: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install the fake ``lindos.compat`` for the duration of a test."""
    try:
        import lindos  # noqa: F401  (real lindos-core package from the source tree)
    except ImportError:  # pragma: no cover - lindos-core missing from the checkout
        lindos = types.ModuleType("lindos")
        lindos.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "lindos", lindos)
    fake = build_fake_core(home)
    monkeypatch.setitem(sys.modules, "lindos.compat", fake)
    monkeypatch.setattr(sys.modules["lindos"], "compat", fake, raising=False)
    return fake


@pytest.fixture()
def fake_which() -> Callable[..., Callable[[str], Optional[str]]]:
    """Factory: ``fake_which("wine", "umu-run")`` → a ``which`` that knows only those tools."""

    def factory(*names: str, prefix: str = "/usr/bin") -> Callable[[str], Optional[str]]:
        table = {n: f"{prefix}/{n}" for n in names}

        def which(name: str) -> Optional[str]:
            return table.get(name)

        return which

    return factory


class FakeRun:
    """Records every ``run(argv, ...)`` call and returns a CompletedProcess-like object.

    ``wineboot`` creates ``system.reg`` in ``WINEPREFIX`` (like the real thing);
    ``rc_for(argv)`` can be overridden to fail specific commands.
    """

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.envs: List[Dict[str, str]] = []
        self.rc_for: Callable[[List[str]], int] = lambda argv: 0

    def __call__(self, argv: List[str], *args: Any, **kwargs: Any) -> Any:
        argv = [str(a) for a in argv]
        env = kwargs.get("env") or {}
        self.calls.append(argv)
        self.envs.append(dict(env))
        if any(os.path.basename(a) == "wineboot" for a in argv[:2]):
            prefix = env.get("WINEPREFIX")
            if prefix:
                Path(prefix).mkdir(parents=True, exist_ok=True)
                (Path(prefix) / "system.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
                (Path(prefix) / "drive_c").mkdir(exist_ok=True)
        rc = self.rc_for(argv)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="", args=argv)

    def has(self, *tokens: str) -> bool:
        """True when one recorded call contains every token (matched exactly or by basename)."""

        def hit(argv: List[str], token: str) -> bool:
            return any(a == token or os.path.basename(a) == token for a in argv)

        return any(all(hit(c, t) for t in tokens) for c in self.calls)


@pytest.fixture()
def fake_run() -> FakeRun:
    return FakeRun()


@pytest.fixture(scope="session")
def repo_recipes_dir() -> Path:
    return RECIPES


# --------------------------------------------------------------------------- #
# .lnk builder (shared by test_lnk / test_runner)
# --------------------------------------------------------------------------- #
LNK_CLSID = bytes.fromhex("0114020000000000C000000000000046")


def _string_data(text: str, unicode: bool) -> bytes:
    if unicode:
        return struct.pack("<H", len(text)) + text.encode("utf-16-le")
    return struct.pack("<H", len(text)) + text.encode("cp1252")


def _build_lnk(
    *,
    local_base_path: Optional[str] = None,
    common_suffix: str = "",
    relative_path: Optional[str] = None,
    working_dir: Optional[str] = None,
    arguments: Optional[str] = None,
    icon_location: Optional[str] = None,
    description: Optional[str] = None,
    unicode: bool = False,
    unicode_link_info: bool = False,
    env_target: Optional[str] = None,
    with_id_list: bool = False,
    file_attributes: int = 0x20,
    icon_index: int = 0,
) -> bytes:
    """Assemble a .lnk from parts (MS-SHLLINK 2.1 header, 2.2 IDList, 2.3 LinkInfo, 2.4 StringData, 2.5 ExtraData)."""
    flags = 0
    if with_id_list:
        flags |= _lnk.HAS_LINK_TARGET_ID_LIST
    if local_base_path is not None:
        flags |= _lnk.HAS_LINK_INFO
    if description is not None:
        flags |= _lnk.HAS_NAME
    if relative_path is not None:
        flags |= _lnk.HAS_RELATIVE_PATH
    if working_dir is not None:
        flags |= _lnk.HAS_WORKING_DIR
    if arguments is not None:
        flags |= _lnk.HAS_ARGUMENTS
    if icon_location is not None:
        flags |= _lnk.HAS_ICON_LOCATION
    if unicode:
        flags |= _lnk.IS_UNICODE
    if env_target is not None:
        flags |= _lnk.HAS_EXP_STRING

    header = struct.pack("<I16sII", 0x4C, LNK_CLSID, flags, file_attributes)
    header += b"\x00" * 24  # Creation/Access/Write time
    header += struct.pack("<IiIHHII", 0, icon_index, 1, 0, 0, 0, 0)  # FileSize IconIndex ShowCommand HotKey Reserved
    assert len(header) == 0x4C
    data = header

    if with_id_list:
        # one bogus item id (size 4) + terminal id
        idlist = struct.pack("<H", 4) + b"\x31\x00" + struct.pack("<H", 0)
        data += struct.pack("<H", len(idlist)) + idlist

    if local_base_path is not None:
        volume_id = struct.pack("<IIII", 0x11, 3, 0x12345678, 0x10) + b"\x00"  # DRIVE_FIXED, empty label
        if unicode_link_info:
            header_size = 0x24
            vol_off = header_size
            base_off = 0  # not used: unicode offsets take over
            suffix_off = 0
            body = volume_id
            base_u_off = header_size + len(body)
            body += local_base_path.encode("utf-16-le") + b"\x00\x00"
            suffix_u_off = header_size + len(body)
            body += common_suffix.encode("utf-16-le") + b"\x00\x00"
            li = struct.pack("<IIIIIII", 0, header_size, _lnk.VOLUME_ID_AND_LOCAL_BASE_PATH, vol_off, base_off, 0,
                             suffix_off)
            li += struct.pack("<II", base_u_off, suffix_u_off)
            li += body
        else:
            header_size = 0x1C
            vol_off = header_size
            body = volume_id
            base_off = header_size + len(body)
            body += local_base_path.encode("cp1252") + b"\x00"
            suffix_off = header_size + len(body)
            body += common_suffix.encode("cp1252") + b"\x00"
            li = struct.pack("<IIIIIII", 0, header_size, _lnk.VOLUME_ID_AND_LOCAL_BASE_PATH, vol_off, base_off, 0,
                             suffix_off)
            li += body
        li = struct.pack("<I", len(li)) + li[4:]
        data += li

    for present, text in ((description is not None, description), (relative_path is not None, relative_path),
                          (working_dir is not None, working_dir), (arguments is not None, arguments),
                          (icon_location is not None, icon_location)):
        if present:
            data += _string_data(text or "", unicode)

    if env_target is not None:
        block = env_target.encode("cp1252").ljust(260, b"\x00") + env_target.encode("utf-16-le").ljust(520, b"\x00")
        data += struct.pack("<II", 8 + len(block), _lnk.ENVIRONMENT_PROPS_SIG) + block
    data += struct.pack("<I", 0)  # TerminalBlock
    return data


@pytest.fixture()
def build_lnk() -> Callable[..., bytes]:
    """The .lnk builder as a fixture (importable-by-name is not guaranteed under importlib mode)."""
    return _build_lnk


# --------------------------------------------------------------------------- #
# synthetic PE / NE / MZ images (never vendored binaries)
# --------------------------------------------------------------------------- #
def _build_pe(*, machine: int = 0x8664, subsystem: int = 2, dll: bool = False, pad: int = 512) -> bytes:
    """A minimal, structurally valid PE image (MZ → e_lfanew → PE\\0\\0 → COFF → optional header)."""
    e_lfanew = 0x80
    buf = bytearray(b"MZ" + b"\x90\x00" + b"\x00" * (0x3C - 4))
    buf += struct.pack("<I", e_lfanew)
    buf += b"\x00" * (e_lfanew - len(buf))
    characteristics = 0x0022 | (0x2000 if dll else 0)
    pe32plus = machine in (0x8664, 0xAA64)
    opt_size = 240 if pe32plus else 224
    buf += b"PE\x00\x00"
    buf += struct.pack("<HHIIIHH", machine, 0, 0, 0, 0, opt_size, characteristics)
    opt = bytearray(struct.pack("<H", 0x20B if pe32plus else 0x10B))
    opt += b"\x00" * (68 - len(opt))
    opt += struct.pack("<H", subsystem)
    opt += b"\x00" * ((108 if pe32plus else 92) - len(opt))
    opt += struct.pack("<I", 16)                      # NumberOfRvaAndSizes
    opt += b"\x00" * (opt_size - len(opt))            # empty data directories (no CLR header)
    buf += opt
    buf += b"\x00" * pad
    return bytes(buf)


def _build_ne(*, exetyp: int = 2, library: bool = False) -> bytes:
    """A minimal NE (16-bit Windows) image: MZ stub + ``NE`` header with ne_flags/ne_exetyp."""
    e_lfanew = 0x80
    buf = bytearray(b"MZ" + b"\x00" * (0x3C - 2))
    buf += struct.pack("<I", e_lfanew)
    buf += b"\x00" * (e_lfanew - len(buf))
    ne = bytearray(b"NE" + b"\x00" * 0x3E)
    struct.pack_into("<H", ne, 0x0C, 0x8000 if library else 0x0002)
    ne[0x36] = exetyp
    buf += ne + b"\x00" * 256
    return bytes(buf)


@pytest.fixture()
def make_pe() -> Callable[..., bytes]:
    """``make_pe(machine=0x8664, subsystem=2, dll=False)`` → bytes of a synthetic PE image."""
    return _build_pe


@pytest.fixture()
def make_ne() -> Callable[..., bytes]:
    """``make_ne(exetyp=2, library=False)`` → bytes of a synthetic 16-bit NE image."""
    return _build_ne
