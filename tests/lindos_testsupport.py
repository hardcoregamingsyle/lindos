"""Lindos test-support pytest plugin (rootdir-independent).

Loaded two ways:

* ``tests/run.sh`` exports ``PYTHONPATH=<repo>/tests`` and passes
  ``-p lindos_testsupport`` to pytest, so it is active for *every* test tree
  (``tests/``, ``packages/*/tests``, ``build/tests``) regardless of rootdir.
* ``tests/conftest.py`` imports it, so plain ``pytest tests`` works too.

What it does (SPEC §12):

1. Adds the in-repo library directories to ``sys.path`` so the pure-Python
   modules can be imported straight from the source tree without installing
   the .debs:

   * ``packages/lindos-core/root/usr/lib/python3/dist-packages``  (``import lindos``)
   * ``packages/lindos-setup/root/usr/lib/lindos-setup``          (``import lindos_setup``)
   * ``packages/lindos-settings/root/usr/lib/lindos-settings``    (``import lindos_settings``)
   * ``packages/lindos-compat/root/usr/lib/lindos-compat``        (``import lindos_compat``)
   * ``packages/lindos-tune/root/usr/lib/lindos-tune``            (``import lindos_tune``)
   * any other ``packages/*/root/usr/lib/lindos-*`` directory that exists.

2. Installs a permissive stub ``gi`` module **only if the real PyGObject is
   missing** (Windows/macOS developer machines, bare CI interpreters), so that
   UI modules doing ``gi.require_version("Gtk", "3.0")`` and
   ``from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf`` can be
   imported and their pure-logic parts unit-tested.  ``Gtk.Window`` & friends
   are real (dynamically created) classes so they can be subclassed;
   attribute access on them returns more permissive dummies; enum-like
   class attributes (``Gtk.Orientation.VERTICAL``) are cached so equality by
   identity works.

Everything here is idempotent; importing twice is harmless.
"""
from __future__ import annotations

import glob
import importlib
import os
import sys
import types
from typing import Any, Dict, List

__all__ = [
    "REPO_ROOT",
    "LIB_DIRS",
    "setup_paths",
    "gi_is_stub",
    "install_gi_stub",
    "install",
]

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
_PKGS = os.path.join(REPO_ROOT, "packages")

#: SPEC §12 + lindos-tune.  Order matters: first entry wins on name clashes.
LIB_DIRS: List[str] = [
    os.path.join(_PKGS, "lindos-core", "root", "usr", "lib", "python3", "dist-packages"),
    os.path.join(_PKGS, "lindos-setup", "root", "usr", "lib", "lindos-setup"),
    os.path.join(_PKGS, "lindos-settings", "root", "usr", "lib", "lindos-settings"),
    os.path.join(_PKGS, "lindos-compat", "root", "usr", "lib", "lindos-compat"),
    os.path.join(_PKGS, "lindos-tune", "root", "usr", "lib", "lindos-tune"),
]

_STUB_MARKER = "__lindos_stub__"


# --------------------------------------------------------------------------- #
# sys.path
# --------------------------------------------------------------------------- #
def _extra_lib_dirs() -> List[str]:
    """Any packages/*/root/usr/lib/lindos-* directory not already listed."""
    found: List[str] = []
    for path in sorted(glob.glob(os.path.join(_PKGS, "*", "root", "usr", "lib", "lindos-*"))):
        if os.path.isdir(path) and path not in LIB_DIRS:
            found.append(path)
    return found


def setup_paths() -> List[str]:
    """Insert every existing library dir at the front of ``sys.path``.

    Returns the list of directories that are now on ``sys.path`` (existing
    ones only).  Missing directories are silently skipped so the plugin works
    while packages are still being written.
    """
    added: List[str] = []
    # Insert in reverse so the final order equals LIB_DIRS order.
    for path in reversed(LIB_DIRS + _extra_lib_dirs()):
        if os.path.isdir(path):
            norm = os.path.normpath(path)
            if norm in sys.path:
                sys.path.remove(norm)
            sys.path.insert(0, norm)
            added.append(norm)
    added.reverse()
    return added


# --------------------------------------------------------------------------- #
# gi stub
# --------------------------------------------------------------------------- #
class _DummyMeta(type):
    """Metaclass giving Dummy *classes* permissive class-level attributes.

    ``Gtk.Orientation.VERTICAL`` → cached Dummy instance (so two lookups are
    identical / equal); ``Gtk.Settings.get_default()`` → callable dummy;
    ``Gtk.Template.Child()`` → callable dummy.  Names starting with an
    underscore raise AttributeError so introspection tools (pytest, inspect,
    dataclasses, typing) behave normally.
    """

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # Always instantiate the *base* dummy: user subclasses may have
        # __init__ signatures that require arguments.
        value = _Dummy(_dummy_name=f"{cls.__name__}.{name}")
        setattr(cls, name, value)
        return value

    def __repr__(cls) -> str:
        return f"<gi-stub class {cls.__name__}>"


class _Dummy(metaclass=_DummyMeta):
    """A permissive object: accepts any constructor args, any attribute
    (returns another callable dummy), any call (returns a dummy — or, when
    used as a class decorator, the decorated class unchanged), basic
    operators, iteration (empty), context-manager protocol.
    """

    __lindos_stub__ = True

    def __init__(self, *args: Any, _dummy_name: str = "", **kwargs: Any) -> None:
        object.__setattr__(self, "_dummy_args", args)
        object.__setattr__(self, "_dummy_kwargs", kwargs)
        object.__setattr__(self, "_dummy_name", _dummy_name or type(self).__name__)
        object.__setattr__(self, "_dummy_props", {})

    # Subclasses may skip super().__init__(); never rely on it having run.
    def _dummy_state(self) -> tuple[str, dict]:
        d = object.__getattribute__(self, "__dict__")
        if "_dummy_props" not in d:
            d["_dummy_props"] = {}
        if "_dummy_name" not in d:
            d["_dummy_name"] = type(self).__name__
        return d["_dummy_name"], d["_dummy_props"]

    # -- attributes ---------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        my_name, props = self._dummy_state()
        if name in props:
            return props[name]
        value = _Dummy(_dummy_name=f"{my_name}.{name}")
        props[name] = value
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        # Real attribute assignment on instances (subclasses do this a lot).
        object.__setattr__(self, name, value)

    # -- calling ------------------------------------------------------------
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # ``@Gtk.Template(resource_path=...)`` style class decorators must
        # return the class untouched, otherwise the whole UI module breaks.
        if len(args) == 1 and not kwargs and isinstance(args[0], type):
            return args[0]
        return _Dummy(_dummy_name=f"{self._dummy_state()[0]}()")

    # -- protocol niceties --------------------------------------------------
    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return True

    def __int__(self) -> int:
        return 0

    def __float__(self) -> float:
        return 0.0

    def __index__(self) -> int:
        return 0

    def __getitem__(self, item: Any) -> Any:
        return _Dummy(_dummy_name=f"{self._dummy_state()[0]}[{item!r}]")

    def __setitem__(self, item: Any, value: Any) -> None:
        return None

    def __contains__(self, item: Any) -> bool:
        return False

    def __enter__(self) -> "_Dummy":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __or__(self, other: Any) -> "_Dummy":
        return self

    __ror__ = __and__ = __rand__ = __xor__ = __or__
    __add__ = __radd__ = __sub__ = __rsub__ = __or__
    __mul__ = __rmul__ = __truediv__ = __floordiv__ = __or__
    __lshift__ = __rshift__ = __or__

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: Any) -> bool:
        return self is other

    def __repr__(self) -> str:
        return f"<gi-stub {self._dummy_state()[0]}>"


def _make_dummy_class(qualname: str) -> type:
    """Create a fresh, subclassable Dummy class named after a GI symbol."""
    short = qualname.rsplit(".", 1)[-1]
    return _DummyMeta(short, (_Dummy,), {"__module__": qualname.rsplit(".", 1)[0], "__qualname__": short})


class _RepositoryModule(types.ModuleType):
    """``gi.repository.Gtk`` etc.: any attribute is a Dummy class (cached)."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        cls = _make_dummy_class(f"{self.__name__}.{name}")
        setattr(self, name, cls)
        return cls

    def __dir__(self) -> List[str]:
        return sorted(set(super().__dir__()) | {"Window", "Box", "Label", "Button"})


class _RepositoryPackage(types.ModuleType):
    """``gi.repository``: any attribute is a repository module (cached and
    registered in ``sys.modules`` so ``import gi.repository.Gtk`` also works)."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        full = f"{self.__name__}.{name}"
        mod = sys.modules.get(full)
        if mod is None:
            mod = _RepositoryModule(full)
            setattr(mod, _STUB_MARKER, True)
            mod.__file__ = f"<lindos gi stub {full}>"
            sys.modules[full] = mod
        setattr(self, name, mod)
        return mod


def _build_gi_stub() -> types.ModuleType:
    gi = types.ModuleType("gi")
    setattr(gi, _STUB_MARKER, True)
    gi.__file__ = "<lindos gi stub>"
    gi.__path__ = []  # type: ignore[attr-defined]  # behave like a package
    gi.version_info = (3, 99, 0)  # type: ignore[attr-defined]
    gi.__version__ = "3.99.0-lindos-stub"  # type: ignore[attr-defined]

    def require_version(namespace: str, version: str) -> None:  # noqa: ARG001
        return None

    def require_versions(versions: Dict[str, str]) -> None:  # noqa: ARG001
        return None

    def require_foreign(namespace: str, symbol: str | None = None) -> None:  # noqa: ARG001
        return None

    def get_required_version(namespace: str) -> str | None:  # noqa: ARG001
        return None

    gi.require_version = require_version  # type: ignore[attr-defined]
    gi.require_versions = require_versions  # type: ignore[attr-defined]
    gi.require_foreign = require_foreign  # type: ignore[attr-defined]
    gi.get_required_version = get_required_version  # type: ignore[attr-defined]

    repo = _RepositoryPackage("gi.repository")
    setattr(repo, _STUB_MARKER, True)
    repo.__file__ = "<lindos gi stub gi.repository>"
    repo.__path__ = []  # type: ignore[attr-defined]
    gi.repository = repo  # type: ignore[attr-defined]

    # Pre-create the namespaces SPEC §12 names, plus a few common ones.
    for ns in ("Gtk", "Gdk", "GLib", "Gio", "GObject", "Pango", "PangoCairo",
               "GdkPixbuf", "Notify", "Wnck", "cairo", "Xfconf"):
        getattr(repo, ns)

    # gi.overrides / gi.pygtkcompat are occasionally probed by tooling.
    overrides = types.ModuleType("gi.overrides")
    setattr(overrides, _STUB_MARKER, True)
    gi.overrides = overrides  # type: ignore[attr-defined]
    return gi


def gi_is_stub() -> bool:
    """True when the ``gi`` in ``sys.modules`` is our stub."""
    mod = sys.modules.get("gi")
    return bool(mod is not None and getattr(mod, _STUB_MARKER, False))


def install_gi_stub(force: bool = False) -> bool:
    """Install the stub if the real ``gi`` cannot be imported.

    Returns True when the stub is (now) active, False when the real PyGObject
    is available and was left untouched.  ``force=True`` (tests only)
    replaces whatever is present.
    """
    if not force:
        if gi_is_stub():
            return True
        try:
            importlib.import_module("gi")
            return False
        except Exception:  # noqa: BLE001 - ImportError or broken installs
            pass
    stub = _build_gi_stub()
    # Purge any half-imported real gi so submodule imports resolve to the stub.
    for key in [k for k in sys.modules if k == "gi" or k.startswith("gi.")]:
        del sys.modules[key]
    sys.modules["gi"] = stub
    sys.modules["gi.repository"] = stub.repository  # type: ignore[attr-defined]
    sys.modules["gi.overrides"] = stub.overrides  # type: ignore[attr-defined]
    for ns in ("Gtk", "Gdk", "GLib", "Gio", "GObject", "Pango", "PangoCairo",
               "GdkPixbuf", "Notify", "Wnck", "cairo", "Xfconf"):
        getattr(stub.repository, ns)  # type: ignore[attr-defined]
    return True


def install() -> Dict[str, Any]:
    """Do everything: paths + gi stub.  Idempotent."""
    paths = setup_paths()
    stub = install_gi_stub()
    return {"paths": paths, "gi_stub": stub}


# Run at import time so both ``-p lindos_testsupport`` and a plain
# ``import lindos_testsupport`` (from tests/conftest.py) have the same effect.
_STATE = install()


# --------------------------------------------------------------------------- #
# pytest hooks (only used when loaded as a plugin)
# --------------------------------------------------------------------------- #
def pytest_configure(config: Any) -> None:  # pragma: no cover - trivial
    install()
    config.addinivalue_line("markers", "linux: test needs a Linux host (skipped elsewhere)")
    config.addinivalue_line("markers", "slow: slow test (network/heavy)")


def pytest_report_header(config: Any) -> List[str]:  # noqa: ARG001
    return [
        "lindos-testsupport: gi=%s, lib dirs on sys.path: %d"
        % ("stub" if gi_is_stub() else "real", len(_STATE["paths"]))
    ]
