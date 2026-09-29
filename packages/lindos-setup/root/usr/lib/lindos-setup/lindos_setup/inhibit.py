"""Keep the screen on, unlocked and the PC awake while the wizard runs (best effort).

A first-boot setup can sit on one page for a while.  With the stock power settings (blank/sleep
after 10 minutes, light-locker enabled) the screen would blank and *lock* half way through and ask
for yet another password, or the machine would suspend mid-setup.
While the wizard is open we therefore take D-Bus inhibits on the session bus, whichever of these
the running session offers:

* ``org.freedesktop.ScreenSaver.Inhibit``        (light-locker, xfce4-screensaver)
* ``org.freedesktop.PowerManagement.Inhibit``    (xfce4-power-manager)
* ``org.gnome.SessionManager.Inhibit``           (suspend + idle; xfce4-session and others)

The bus daemons tie an inhibit to our connection, so if the wizard crashes or is killed the
inhibits vanish with the process; nothing is written to any setting and there is nothing to restore
(no ``xset`` tricks).  Everything is guarded: no ``gi``, no session bus, or none of the services
never raises -- :meth:`IdleInhibitor.acquire` just reports that nothing could be inhibited.
"""
from __future__ import annotations

import atexit
import logging
import threading
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

log = logging.getLogger("lindos-setup.inhibit")

APP_NAME = "Lindos Setup"
REASON = "Setting up Lindos - please keep the PC on"
CALL_TIMEOUT_MS = 1500
#: org.gnome.SessionManager inhibit flags: 4 = suspend, 8 = idle
GNOME_INHIBIT_FLAGS = 4 | 8


class Target(NamedTuple):
    label: str
    bus_name: str
    object_path: str
    interface: str
    inhibit_method: str
    release_method: str
    signature: str          # "(ss)": app, reason  |  "(susu)": app, toplevel xid, reason, flags


TARGETS: Tuple[Target, ...] = (
    Target("screensaver", "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
           "org.freedesktop.ScreenSaver", "Inhibit", "UnInhibit", "(ss)"),
    Target("power", "org.freedesktop.PowerManagement", "/org/freedesktop/PowerManagement/Inhibit",
           "org.freedesktop.PowerManagement.Inhibit", "Inhibit", "UnInhibit", "(ss)"),
    Target("session", "org.gnome.SessionManager", "/org/gnome/SessionManager",
           "org.gnome.SessionManager", "Inhibit", "Uninhibit", "(susu)"),
)


def inhibit_args(target: Target, app_name: str, reason: str) -> tuple:
    """The Inhibit() call arguments for *target*."""
    if target.signature == "(susu)":
        return (app_name, 0, reason, GNOME_INHIBIT_FLAGS)
    return (app_name, reason)


class GioBus:
    """The session bus through Gio (imported lazily; ``gi`` may be missing or have no bus)."""

    def __init__(self, gio: Any, glib: Any, connection: Any) -> None:
        self._gio = gio
        self._glib = glib
        self._conn = connection

    @classmethod
    def connect(cls) -> Optional["GioBus"]:
        """A connected bus, or None (no PyGObject, no session bus, ...)."""
        try:
            import gi  # noqa: F401
            from gi.repository import Gio, GLib
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:  # ImportError, ValueError, GLib.Error, ...
            log.debug("no session bus for idle inhibit: %s", exc)
            return None
        if conn is None:
            return None
        return cls(Gio, GLib, conn)

    def _call(self, target: Target, method: str, signature: str, args: tuple) -> Any:
        return self._conn.call_sync(
            target.bus_name, target.object_path, target.interface, method,
            self._glib.Variant(signature, args), None, self._gio.DBusCallFlags.NO_AUTO_START,
            CALL_TIMEOUT_MS, None)

    def inhibit(self, target: Target, args: tuple) -> Optional[int]:
        """Take the inhibit; the cookie, or None when the service is not there / refused."""
        try:
            reply = self._call(target, target.inhibit_method, target.signature, args)
            cookie = reply.unpack()[0]
        except Exception as exc:
            log.debug("%s inhibit unavailable: %s", target.label, exc)
            return None
        if isinstance(cookie, bool) or not isinstance(cookie, int):
            return None
        return cookie

    def release(self, target: Target, cookie: int) -> bool:
        try:
            self._call(target, target.release_method, "(u)", (cookie,))
            return True
        except Exception as exc:
            log.debug("%s uninhibit failed: %s", target.label, exc)
            return False


class IdleInhibitor:
    """Holds screensaver/power inhibits between :meth:`acquire` and :meth:`release`.

    Both are idempotent and never raise.  ``connect`` (default :meth:`GioBus.connect`) returns an
    object with ``inhibit(target, args) -> cookie | None`` and ``release(target, cookie)``; tests
    inject a fake.  :meth:`acquire_async` does the D-Bus round trips on a worker thread so a slow
    or hung daemon can never delay the wizard window; :meth:`release` waits for it.
    """

    def __init__(self, connect: Optional[Callable[[], Any]] = None, *, app_name: str = APP_NAME,
                 reason: str = REASON, targets: Tuple[Target, ...] = TARGETS) -> None:
        self._connect = connect or GioBus.connect
        self._app_name = app_name
        self._reason = reason
        self._targets = targets
        self._bus: Any = None
        self._cookies: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._atexit = False

    @property
    def held(self) -> List[str]:
        """Labels of the inhibits currently taken (``screensaver``, ``power``, ``session``)."""
        return list(self._cookies)

    def acquire(self) -> List[str]:
        """Take every inhibit the session offers; returns :attr:`held`.  Never raises."""
        self._closed = False
        self._acquire()
        return self.held

    def acquire_async(self) -> None:
        """Like :meth:`acquire` but on a daemon thread."""
        self._closed = False
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._acquire, name="lindos-setup-inhibit", daemon=True)
        self._thread.start()

    def _acquire(self) -> None:
        try:
            with self._lock:
                if self._closed or self._cookies:
                    return
                try:
                    self._bus = self._connect()
                except Exception as exc:
                    log.debug("idle inhibit: cannot connect: %s", exc)
                    self._bus = None
                if self._bus is None:
                    log.info("idle inhibit: no session bus available; the screen may blank during setup")
                    return
                for target in self._targets:
                    if self._closed:
                        break
                    args = inhibit_args(target, self._app_name, self._reason)
                    try:
                        cookie = self._bus.inhibit(target, args)
                    except Exception as exc:
                        log.debug("idle inhibit: %s failed: %s", target.label, exc)
                        cookie = None
                    if cookie is not None:
                        self._cookies[target.label] = cookie
                if self._cookies:
                    log.info("idle inhibit taken: %s", ", ".join(self._cookies))
                    if not self._atexit:
                        atexit.register(self.release)
                        self._atexit = True
                else:
                    log.info("idle inhibit: no screensaver/power service answered; the screen may blank")
        except Exception as exc:  # belt and braces: the wizard must never fail because of this
            log.warning("idle inhibit failed: %s", exc)

    def release(self) -> None:
        """Give every inhibit back (also cancels one still being taken).  Never raises."""
        self._closed = True
        try:
            with self._lock:
                bus, cookies = self._bus, dict(self._cookies)
                self._cookies.clear()
                for target in self._targets:
                    cookie = cookies.get(target.label)
                    if cookie is None or bus is None:
                        continue
                    try:
                        bus.release(target, cookie)
                    except Exception as exc:
                        log.debug("idle inhibit: releasing %s failed: %s", target.label, exc)
                if cookies:
                    log.info("idle inhibit released: %s", ", ".join(cookies))
                self._bus = None
            if self._atexit:
                self._atexit = False
                try:
                    atexit.unregister(self.release)
                except Exception:  # pragma: no cover - defensive
                    pass
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("idle inhibit release failed: %s", exc)

    def __enter__(self) -> "IdleInhibitor":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


__all__ = ["APP_NAME", "REASON", "TARGETS", "Target", "GioBus", "IdleInhibitor", "inhibit_args"]
