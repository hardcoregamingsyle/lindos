"""lindos_setup.inhibit: screensaver/power inhibits held while the wizard runs.

Everything is exercised with fakes -- no PyGObject, no session bus -- and the "no gi" / "no bus"
paths prove the wizard can never fail because of this best-effort feature.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import types
from typing import Any, List, Optional, Tuple

import pytest

from lindos_setup import inhibit
from lindos_setup.inhibit import GioBus, IdleInhibitor, Target, TARGETS, inhibit_args

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeBus:
    """Stands in for GioBus: records every call, hands out cookies, can refuse per target."""

    def __init__(self, refuse=(), raise_on=()) -> None:
        self.refuse = set(refuse)
        self.raise_on = set(raise_on)
        self.inhibited: List[Tuple[str, tuple]] = []
        self.released: List[Tuple[str, int]] = []
        self._next = 100

    def inhibit(self, target: Target, args: tuple) -> Optional[int]:
        if target.label in self.raise_on:
            raise RuntimeError("bus exploded")
        if target.label in self.refuse:
            return None
        self.inhibited.append((target.label, args))
        self._next += 1
        return self._next

    def release(self, target: Target, cookie: int) -> bool:
        self.released.append((target.label, cookie))
        return True


def test_acquire_takes_every_inhibit_and_release_gives_them_back():
    bus = FakeBus()
    inh = IdleInhibitor(lambda: bus)
    assert inh.acquire() == ["screensaver", "power", "session"] and inh.held == ["screensaver", "power", "session"]
    assert [label for label, _ in bus.inhibited] == ["screensaver", "power", "session"]
    args = dict(bus.inhibited)
    assert args["screensaver"] == (inhibit.APP_NAME, inhibit.REASON)
    assert args["power"] == (inhibit.APP_NAME, inhibit.REASON)
    assert args["session"] == (inhibit.APP_NAME, 0, inhibit.REASON, 4 | 8)        # suspend + idle
    inh.release()
    assert bus.released == [("screensaver", 101), ("power", 102), ("session", 103)]
    assert inh.held == []


def test_acquire_and_release_are_idempotent():
    bus = FakeBus()
    inh = IdleInhibitor(lambda: bus)
    inh.acquire()
    inh.acquire()
    assert len(bus.inhibited) == 3
    inh.release()
    inh.release()
    assert len(bus.released) == 3
    # usable again after a release
    inh.acquire()
    assert len(bus.inhibited) == 6 and inh.held == ["screensaver", "power", "session"]
    inh.release()


def test_only_the_services_that_answer_are_held():
    bus = FakeBus(refuse={"power"}, raise_on={"session"})
    inh = IdleInhibitor(lambda: bus)
    assert inh.acquire() == ["screensaver"]
    inh.release()
    assert bus.released == [("screensaver", 101)]


def test_no_service_answering_is_a_quiet_no_op(caplog):
    caplog.set_level(logging.INFO, logger="lindos-setup.inhibit")
    bus = FakeBus(refuse={"screensaver", "power", "session"})
    inh = IdleInhibitor(lambda: bus)
    assert inh.acquire() == [] and inh.held == []
    inh.release()
    assert bus.released == []
    assert any("no screensaver/power service answered" in r.message for r in caplog.records)


def test_no_session_bus_or_a_failing_connect_never_raises():
    assert IdleInhibitor(lambda: None).acquire() == []

    def boom():
        raise OSError("no bus")

    inh = IdleInhibitor(boom)
    assert inh.acquire() == []
    inh.release()
    with IdleInhibitor(lambda: None) as ctx:
        assert ctx.held == []


def test_a_bus_object_that_misbehaves_never_raises():
    class Bad:
        def inhibit(self, target, args):
            return 5

        def release(self, target, cookie):
            raise RuntimeError("cannot release")

    inh = IdleInhibitor(lambda: Bad())
    assert len(inh.acquire()) == 3
    inh.release()                    # the failing release is swallowed
    assert inh.held == []


def test_context_manager_releases_on_exit_even_after_an_exception():
    bus = FakeBus()
    with pytest.raises(ValueError):
        with IdleInhibitor(lambda: bus) as inh:
            assert inh.held
            raise ValueError("wizard crashed")
    assert len(bus.released) == 3


def test_acquire_async_then_release_waits_for_and_cleans_up_the_worker():
    gate = threading.Event()
    entered = threading.Event()

    class SlowBus(FakeBus):
        def inhibit(self, target, args):
            entered.set()
            gate.wait(5)
            return super().inhibit(target, args)

    bus = SlowBus()
    inh = IdleInhibitor(lambda: bus)
    inh.acquire_async()
    assert entered.wait(5)                      # the worker is mid-way, holding the first D-Bus call
    threading.Timer(0.05, gate.set).start()
    inh.release()                               # must wait for the worker, then leave nothing held
    assert inh.held == []
    assert len(bus.released) == len(bus.inhibited) >= 1
    assert {label for label, _ in bus.released} == {label for label, _ in bus.inhibited}


def test_release_before_the_async_worker_starts_means_nothing_is_taken():
    bus = FakeBus()
    inh = IdleInhibitor(lambda: bus)
    inh._closed = False
    inh.release()                               # closes the inhibitor
    inh._acquire()                              # a late worker must notice and do nothing
    assert bus.inhibited == [] and inh.held == []


def test_release_is_registered_with_atexit_only_while_held(monkeypatch):
    registered: List[Any] = []
    monkeypatch.setattr(inhibit.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(inhibit.atexit, "unregister", lambda fn: registered.remove(fn))
    inh = IdleInhibitor(lambda: FakeBus())
    inh.acquire()
    inh.acquire()
    assert registered == [inh.release]
    inh.release()
    assert registered == []
    none_held = IdleInhibitor(lambda: None)
    none_held.acquire()
    assert registered == []


def test_inhibit_args_shapes():
    by_label = {t.label: t for t in TARGETS}
    assert inhibit_args(by_label["screensaver"], "app", "why") == ("app", "why")
    assert inhibit_args(by_label["session"], "app", "why") == ("app", 0, "why", 12)
    assert {t.bus_name for t in TARGETS} == {"org.freedesktop.ScreenSaver", "org.freedesktop.PowerManagement",
                                             "org.gnome.SessionManager"}
    assert by_label["session"].release_method == "Uninhibit" and by_label["power"].release_method == "UnInhibit"


# --- no gi -----------------------------------------------------------------------------------
def test_without_gi_everything_degrades_to_a_no_op(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)                 # `import gi` -> ImportError
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    assert GioBus.connect() is None
    inh = IdleInhibitor()                                        # default connect = GioBus.connect
    assert inh.acquire() == []
    inh.acquire_async()
    inh.release()
    assert inh.held == []


def test_module_import_needs_no_gi():
    with open(inhibit.__file__, encoding="utf-8") as fh:
        src = fh.read()
    top_level = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("gi" == ln.split()[1].split(".")[0] for ln in top_level)


# --- the Gio side, against a fake Gio ---------------------------------------------------------
class FakeGLib:
    class Variant:
        def __init__(self, signature, args):
            self.signature, self.args = signature, args


class FakeGio:
    class DBusCallFlags:
        NO_AUTO_START = 16

    class BusType:
        SESSION = "session"

    def __init__(self, conn=None, fail_connect=False):
        self._conn, self._fail = conn, fail_connect

    def bus_get_sync(self, bus_type, cancellable):
        if self._fail:
            raise RuntimeError("Could not connect: No such file or directory")
        assert bus_type == "session"
        return self._conn


class FakeReply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeConn:
    def __init__(self, reply=(42,), error=None):
        self.reply, self.error, self.calls = reply, error, []

    def call_sync(self, bus_name, path, iface, method, params, reply_type, flags, timeout, cancellable):
        self.calls.append((bus_name, path, iface, method, params.signature, params.args, flags, timeout))
        if self.error:
            raise self.error
        return FakeReply(self.reply)


def test_giobus_builds_the_right_dbus_calls():
    conn = FakeConn(reply=(42,))
    bus = GioBus(FakeGio(conn), FakeGLib, conn)
    by_label = {t.label: t for t in TARGETS}
    cookie = bus.inhibit(by_label["screensaver"], ("Lindos Setup", "why"))
    assert cookie == 42
    assert conn.calls[-1] == ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                              "org.freedesktop.ScreenSaver", "Inhibit", "(ss)", ("Lindos Setup", "why"),
                              FakeGio.DBusCallFlags.NO_AUTO_START, inhibit.CALL_TIMEOUT_MS)
    assert bus.inhibit(by_label["session"], ("Lindos Setup", 0, "why", 12)) == 42
    assert conn.calls[-1][:6] == ("org.gnome.SessionManager", "/org/gnome/SessionManager",
                                  "org.gnome.SessionManager", "Inhibit", "(susu)", ("Lindos Setup", 0, "why", 12))
    assert bus.release(by_label["power"], 42) is True
    assert conn.calls[-1][:6] == ("org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
                                  "org.freedesktop.PowerManagement.Inhibit", "UnInhibit", "(u)", (42,))


def test_giobus_reports_missing_services_and_junk_replies_as_none():
    by_label = {t.label: t for t in TARGETS}
    err = FakeConn(error=RuntimeError("The name org.freedesktop.ScreenSaver was not provided by any .service files"))
    bus = GioBus(FakeGio(err), FakeGLib, err)
    assert bus.inhibit(by_label["screensaver"], ("a", "b")) is None
    assert bus.release(by_label["screensaver"], 1) is False
    for junk in ((True,), ("not-an-int",), (), None):
        conn = FakeConn(reply=junk)
        assert GioBus(FakeGio(conn), FakeGLib, conn).inhibit(by_label["power"], ("a", "b")) is None


def test_giobus_connect_uses_the_session_bus_and_survives_failures(monkeypatch):
    def install(gio):
        gi = types.ModuleType("gi")
        repo = types.ModuleType("gi.repository")
        repo.Gio, repo.GLib = gio, FakeGLib
        gi.repository = repo
        monkeypatch.setitem(sys.modules, "gi", gi)
        monkeypatch.setitem(sys.modules, "gi.repository", repo)

    conn = FakeConn()
    install(FakeGio(conn))
    bus = GioBus.connect()
    assert isinstance(bus, GioBus)
    inh = IdleInhibitor(GioBus.connect)
    assert inh.acquire() == ["screensaver", "power", "session"]
    inh.release()
    assert [c[3] for c in conn.calls] == ["Inhibit", "Inhibit", "Inhibit", "UnInhibit", "UnInhibit", "Uninhibit"]
    install(FakeGio(fail_connect=True))                       # no session bus (e.g. a console login)
    assert GioBus.connect() is None and IdleInhibitor().acquire() == []
    install(FakeGio(None))
    assert GioBus.connect() is None


# --- app.py wiring ------------------------------------------------------------------------------
def _ensure_gi() -> None:
    try:
        import gi  # noqa: F401
        return
    except ImportError:
        pass
    tests_dir = os.path.normpath(os.path.join(HERE, "..", "..", "..", "tests"))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        import lindos_testsupport  # noqa: F401
    except ImportError:
        pytest.skip("neither PyGObject nor the repo gi stub is available")


def test_window_destroy_releases_the_inhibitor(monkeypatch):
    _ensure_gi()
    try:
        from lindos_setup import app
    except (ImportError, ValueError):
        pytest.skip("GTK is not usable here")
    monkeypatch.setattr(app.Gtk, "main_quit", lambda *a, **k: None, raising=False)
    bus = FakeBus()
    inh = IdleInhibitor(lambda: bus)
    inh.acquire()
    fake_window = types.SimpleNamespace(inhibitor=inh)
    app.SetupWindow._on_destroy(fake_window)
    assert len(bus.released) == 3 and inh.held == []
    app.SetupWindow._on_destroy(types.SimpleNamespace(inhibitor=None))      # no inhibitor: still fine


def test_context_executors_factory_passes_the_plan_to_make_real_executors(monkeypatch, tmp_path):
    _ensure_gi()
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    try:
        from lindos_setup import app
    except (ImportError, ValueError):
        pytest.skip("GTK is not usable here")
    from lindos_setup.plan import Selections, build_plan
    seen = {}
    monkeypatch.setattr(app.core, "make_real_executors", lambda plan=None: seen.setdefault("plan", plan) or {})
    ctx = app.build_context(dry_run=False, first_run=True, logger=logging.getLogger("t-inhibit"), online=True)
    try:
        plan = build_plan(Selections(), ctx.catalog, online=True)
        ctx.executors_factory(plan)
    finally:
        ctx.live.close()
    assert seen["plan"] is plan
