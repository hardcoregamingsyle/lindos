"""Guarded bridge between the wizard and ``lindos-core`` (SPEC §4).

Every ``lindos.*`` import happens lazily inside a function and is wrapped, so
this module imports cleanly on Windows/macOS and the wizard still runs (with
honest fallbacks and log messages) when a core API is missing.  Nothing here
imports ``gi``.
"""
from __future__ import annotations

import glob
import importlib
import json
import logging
import os
import posixpath
import queue
import subprocess
import threading
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import plan as _plan
from .plan import (
    ACT_APPLY_MODE, ACT_SET_ACCENT, ACT_SET_DEFAULT_BROWSER, ACT_SET_TASKBAR, ACT_SET_THEME,
    ACT_SET_WALLPAPER, ACT_WRITE_CONFIG, ACT_WRITE_SYSTEM_CONFIG, BROWSER_INSTALLED,
    BROWSER_PENDING, BROWSER_UNAVAILABLE, Executor, LogFn, Step,
)

log = logging.getLogger("lindos-setup.core")

# ---------------------------------------------------------------------------
# lazy import helpers
# ---------------------------------------------------------------------------
_MISSING = object()
_module_cache: Dict[str, Any] = {}


def core_module(name: str) -> Optional[types.ModuleType]:
    """``import lindos.<name>`` guarded; cached; returns None when unavailable."""
    if name in _module_cache:
        mod = _module_cache[name]
        return None if mod is _MISSING else mod
    try:
        mod = importlib.import_module("lindos." + name)
    except Exception as exc:  # ImportError or anything raised at import time
        log.warning("lindos.%s unavailable: %s", name, exc)
        _module_cache[name] = _MISSING
        return None
    _module_cache[name] = mod
    return mod


def core_available() -> bool:
    return core_module("paths") is not None


def _ok(value: Any) -> bool:
    """Normalise return values of core APIs (None => ok, objects with .ok, bools)."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if hasattr(value, "ok"):
        return bool(getattr(value, "ok"))
    return bool(value)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def home_dir() -> str:
    """``LINDOS_HOME`` (tests) or the real home directory."""
    override = os.environ.get("LINDOS_HOME", "").strip()
    if override:
        return override
    return os.path.expanduser("~")


def user_path(path: str) -> str:
    """Expand a ``~/...`` path honouring ``LINDOS_HOME`` (SPEC §4.1)."""
    if path == "~":
        return home_dir()
    if path.startswith("~/") or path.startswith("~\\"):
        return os.path.join(home_dir(), path[2:])
    return os.path.expanduser(path)


def _core_path(attr: str, default: str) -> str:
    paths = core_module("paths")
    if paths is not None:
        value = getattr(paths, attr, None)
        if isinstance(value, str) and value:
            return value
    return default


def setup_done_path() -> str:
    return user_path(_core_path("SETUP_DONE", "~/.config/lindos/setup-done"))


def log_dir() -> str:
    return user_path(_core_path("LOG_DIR", "~/.local/state/lindos"))


def log_file() -> str:
    return os.path.join(log_dir(), "setup.log")


def user_conf_dir() -> str:
    return user_path(_core_path("USER_CONF_DIR", "~/.config/lindos"))


def setup_done_exists() -> bool:
    """SETUP_DONE marker present, or config says ``setup_done`` is true."""
    if os.path.exists(setup_done_path()):
        return True
    config = core_module("config")
    if config is not None:
        try:
            cfg = config.Config.load()
            return bool(cfg.get("setup_done", False))
        except Exception as exc:
            log.debug("Config.load failed while checking setup_done: %s", exc)
    return False


def in_xfce(environ: Optional[Dict[str, str]] = None) -> bool:
    """True when ``XDG_CURRENT_DESKTOP`` (or ``DESKTOP_SESSION``) names XFCE."""
    env = environ if environ is not None else os.environ
    current = env.get("XDG_CURRENT_DESKTOP", "")
    parts = [p.strip().lower() for p in current.split(":") if p.strip()]
    if "xfce" in parts:
        return True
    session = env.get("DESKTOP_SESSION", "").strip().lower()
    return session in ("xfce", "xfce4", "xubuntu", "lindos")


def mark_setup_done(*, dry_run: bool = False) -> bool:
    """Create the SETUP_DONE marker and set ``setup_done=True`` in the user config."""
    marker = setup_done_path()
    if dry_run:
        log.info("dry-run: would create %s and set config setup_done=true", marker)
        return True
    ok = True
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        log.info("wrote %s", marker)
    except OSError as exc:
        log.error("cannot write %s: %s", marker, exc)
        ok = False
    config = core_module("config")
    if config is not None:
        try:
            cfg = config.Config.load()
            cfg.set("setup_done", True)
            cfg.save()
        except Exception as exc:
            log.error("cannot persist setup_done in config: %s", exc)
            ok = False
    else:
        log.warning("lindos.config missing; setup_done recorded only via marker file")
    return ok


# ---------------------------------------------------------------------------
# read-only information for the pages
# ---------------------------------------------------------------------------
_FALLBACK_MODES: List[Dict[str, str]] = [
    {"id": "everyday", "name": "Everyday", "icon": "user-home",
     "description": "Balanced default for browsing, media and everyday work."},
    {"id": "gaming", "name": "Gaming", "icon": "applications-games",
     "description": "Max FPS: performance governor, Game Mode, launchers pinned."},
    {"id": "work", "name": "Work", "icon": "x-office-document",
     "description": "Productivity: office and mail pinned, balanced power, night light."},
    {"id": "creator", "name": "Creator", "icon": "applications-graphics",
     "description": "Creative apps and Adobe-era recipes through Wine/Bottles."},
    {"id": "lite", "name": "Lite", "icon": "battery-good",
     "description": "For 4 GB RAM or old PCs: no compositor, no animations, tiny tray."},
]

RAM_HINTS: Dict[str, str] = {
    "everyday": "Idle target 350–500 MB",
    "gaming": "Idle target 350–500 MB, more while gaming",
    "work": "Idle target 350–500 MB",
    "creator": "Idle target 350–500 MB",
    "lite": "Idle ≈ 300–380 MB (no compositor)",
}


def load_modes() -> Dict[str, Any]:
    """``lindos.modes.load_modes()`` in SPEC order; built-in fallback when missing."""
    modes = core_module("modes")
    result: Dict[str, Any] = {}
    if modes is not None:
        try:
            loaded = modes.load_modes()
            for mid in _plan.MODE_IDS:
                if mid in loaded:
                    result[mid] = loaded[mid]
            for mid, m in loaded.items():
                result.setdefault(mid, m)
        except Exception as exc:
            log.warning("lindos.modes.load_modes failed: %s", exc)
    if not result:
        log.warning("no mode definitions found; using built-in descriptions")
    for fb in _FALLBACK_MODES:
        if fb["id"] not in result:
            result[fb["id"]] = types.SimpleNamespace(
                id=fb["id"], name=fb["name"], description=fb["description"], icon=fb["icon"],
                packages=[], flatpaks=[], zram_percent=None, governor="", compositor="", pins=[])
    # keep SPEC order first
    ordered: Dict[str, Any] = {}
    for mid in _plan.MODE_IDS:
        if mid in result:
            ordered[mid] = result[mid]
    for mid, m in result.items():
        ordered.setdefault(mid, m)
    return ordered


_FALLBACK_BROWSERS: Dict[str, Dict[str, Any]] = {
    "edge": {"name": "Microsoft Edge", "package": "microsoft-edge-stable",
             "desktop": "microsoft-edge.desktop", "repo": "vendor", "key_url": None, "list": None},
    "chrome": {"name": "Google Chrome", "package": "google-chrome-stable",
               "desktop": "google-chrome.desktop", "repo": "vendor", "key_url": None, "list": None},
    "firefox": {"name": "Mozilla Firefox", "package": "firefox", "desktop": "firefox.desktop",
                "repo": None, "key_url": None, "list": None},
}


def browsers_table() -> Dict[str, Dict[str, Any]]:
    browsers = core_module("browsers")
    if browsers is not None:
        table = getattr(browsers, "BROWSERS", None)
        if isinstance(table, dict) and table:
            ordered: Dict[str, Dict[str, Any]] = {}
            for bid in _plan.BROWSER_IDS:
                if bid in table:
                    ordered[bid] = dict(table[bid])
            return ordered or {k: dict(v) for k, v in table.items()}
    return {k: dict(v) for k, v in _FALLBACK_BROWSERS.items()}


def browser_installed(bid: str) -> bool:
    browsers = core_module("browsers")
    if browsers is not None:
        try:
            return bool(browsers.is_installed(bid))
        except Exception as exc:
            log.debug("browsers.is_installed(%s) failed: %s", bid, exc)
    return False


# ---------------------------------------------------------------------------
# which kind of session is this?  (the wizard must never run in the live USB session
# or as the temporary OEM account: the installer and oem-config come first)
# ---------------------------------------------------------------------------
_LIVE_WORDS = ("boot=casper", "boot=live")


def is_live_session() -> bool:
    """True while running from the install medium (``boot=casper`` / ``boot=live`` on the kernel
    command line).  ``lindos.session`` is the source of truth; if lindos-core cannot be imported
    the same check is done here so the wizard still refuses to run in a live session."""
    session = core_module("session")
    if session is not None:
        try:
            return bool(session.is_live_session())
        except Exception as exc:
            log.warning("lindos.session.is_live_session failed: %s", exc)
    path = os.environ.get("LINDOS_TEST_CMDLINE") or "/proc/cmdline"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return any(word in _LIVE_WORDS for word in fh.read().split())
    except OSError:
        return False


def is_oem_temp_user() -> bool:
    """True for the temporary ``oem`` account of Ubiquity's OEM mode (the end user's own account
    does not exist yet, so the wizard would set up the wrong person)."""
    session = core_module("session")
    if session is not None:
        try:
            return bool(session.is_oem_temp_user())
        except Exception as exc:
            log.warning("lindos.session.is_oem_temp_user failed: %s", exc)
    try:
        import getpass
        return getpass.getuser().strip() == "oem"
    except (ImportError, OSError, KeyError):
        return False


# ---------------------------------------------------------------------------
# what the installer did (read-only; /var/lib/lindos/install-state.json)
# ---------------------------------------------------------------------------
def install_steps() -> Dict[str, str]:
    """``{step id: done|pending|skipped|failed}`` for every step the installer recorded.

    Empty when there is no record (an installation older than the installer flow, a dev machine)
    or lindos-core cannot be imported.  Never raises."""
    state = core_module("installstate")
    if state is None:
        return {}
    try:
        steps = state.load().get("steps", {})
    except Exception as exc:
        log.debug("lindos.installstate.load failed: %s", exc)
        return {}
    out: Dict[str, str] = {}
    if isinstance(steps, dict):
        for sid, entry in steps.items():
            status = entry.get("status") if isinstance(entry, dict) else None
            if isinstance(status, str) and status:
                out[str(sid)] = status
    return out


def browser_state(bid: str, steps: Optional[Dict[str, str]] = None) -> str:
    """``installed`` / ``pending`` / ``unavailable`` for one browser.

    * Firefox is on the ISO: always installed.
    * Anything really installed (dpkg / on PATH) is installed.
    * Chrome: the installer's ``browser`` step says ``done`` -> installed; ``skipped`` ->
      unavailable (the installer left it out on purpose); ``pending`` / ``failed`` / no record ->
      pending (the silent start-up retry adds it once the PC is online).
    * Edge is never installed by Lindos: only offered when it is already on this PC.
    """
    if bid == "firefox":
        return BROWSER_INSTALLED
    if browser_installed(bid):
        return BROWSER_INSTALLED
    if bid == "chrome":
        status = (steps if steps is not None else install_steps()).get("browser", "")
        if status == "done":
            return BROWSER_INSTALLED
        if status == "skipped":
            return BROWSER_UNAVAILABLE
        return BROWSER_PENDING
    return BROWSER_UNAVAILABLE


def browser_states(bids: Optional[List[str]] = None,
                   steps: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """:func:`browser_state` for every browser id (default: the browsers table)."""
    recorded = steps if steps is not None else install_steps()
    return {bid: browser_state(bid, recorded) for bid in (bids or list(browsers_table()))}


def ram_total_mb() -> Optional[int]:
    hardware = core_module("hardware")
    if hardware is not None:
        try:
            info = hardware.ram_info()
            total = info.get("total") if isinstance(info, dict) else getattr(info, "total", None)
            if total:
                return int(total)
        except Exception as exc:
            log.debug("hardware.ram_info failed: %s", exc)
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def list_wallpapers() -> List[str]:
    """Wallpaper paths: core API, else directory glob, else SPEC names."""
    theme = core_module("theme")
    if theme is not None:
        try:
            found = [str(p) for p in theme.list_wallpapers() or []]
            if found:
                return found
        except Exception as exc:
            log.debug("theme.list_wallpapers failed: %s", exc)
    root = os.environ.get("LINDOS_ROOT", "").strip()
    wall_dir = os.path.join(root, _plan.WALLPAPER_DIR.lstrip("/")) if root else _plan.WALLPAPER_DIR
    found = sorted(glob.glob(os.path.join(wall_dir, "*.svg")) + glob.glob(os.path.join(wall_dir, "*.png"))
                   + glob.glob(os.path.join(wall_dir, "*.jpg")))
    if found:
        # SPEC order first, then extras
        ordered = [os.path.join(wall_dir, n) for n in _plan.WALLPAPER_NAMES if os.path.join(wall_dir, n) in found]
        return ordered + [f for f in found if f not in ordered]
    return [posixpath.join(_plan.WALLPAPER_DIR, n) for n in _plan.WALLPAPER_NAMES]


def wallpaper_display_name(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# live preview (personalize page)
# ---------------------------------------------------------------------------
class LiveApplier:
    """Apply theme choices immediately while the wizard runs.

    In ``--dry-run`` nothing touches the system; calls are only logged.

    ``threaded=True`` (used by the GTK app) runs the ``lindos.theme`` calls
    on one background worker so the many ``xfconf-query`` subprocesses never
    freeze the UI; calls are serialised in order and coalesced per kind (a
    fast succession of accent clicks only applies the last colour).  The
    apply page calls :meth:`drain` before it starts writing config so no
    stale live-preview write can race the real plan.
    """

    def __init__(self, dry_run: bool = False, threaded: bool = False) -> None:
        self.dry_run = dry_run
        self.threaded = threaded
        self._queue: "queue.Queue[Optional[Tuple[str, str, tuple]]]" = queue.Queue()
        self._pending: Dict[str, Tuple[str, str, tuple]] = {}
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._worker: Optional[threading.Thread] = None

    # -- synchronous core -----------------------------------------------------
    def _call(self, what: str, fn_name: str, *args: Any) -> bool:
        if self.dry_run:
            log.info("dry-run: would %s %s", what, args)
            return True
        theme = core_module("theme")
        if theme is None:
            log.warning("cannot %s: lindos.theme unavailable", what)
            return False
        fn = getattr(theme, fn_name, None)
        if fn is None:
            log.warning("cannot %s: lindos.theme.%s missing", what, fn_name)
            return False
        try:
            return _ok(fn(*args))
        except Exception as exc:
            log.warning("%s failed: %s", what, exc)
            return False

    # -- threading ------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="lindos-setup-live", daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            what, fn_name, args = item
            with self._lock:
                latest = self._pending.get(fn_name)
                stale = latest is not None and latest is not item
            if not stale:
                self._call(what, fn_name, *args)
            self._queue.task_done()
            with self._lock:
                if self._pending.get(fn_name) is item:
                    del self._pending[fn_name]
                if not self._pending:
                    self._idle.set()

    def _submit(self, what: str, fn_name: str, *args: Any) -> bool:
        if not self.threaded:
            return self._call(what, fn_name, *args)
        item = (what, fn_name, tuple(args))
        with self._lock:
            self._pending[fn_name] = item
            self._idle.clear()
        self._queue.put(item)
        self._ensure_worker()
        return True

    def drain(self, timeout: float = 15.0) -> bool:
        """Wait until every queued live-preview call has finished (threaded mode)."""
        if not self.threaded:
            return True
        done = self._idle.wait(timeout)
        if not done:
            log.warning("live preview still busy after %.0fs; continuing", timeout)
        return done

    def close(self) -> None:
        if self.threaded and self._worker is not None and self._worker.is_alive():
            self._queue.put(None)

    # -- public API -----------------------------------------------------------
    def set_dark(self, dark: bool) -> bool:
        return self._submit("set dark theme", "set_dark", bool(dark))

    def set_accent(self, hex_colour: str) -> bool:
        return self._submit("set accent", "set_accent", hex_colour)

    def set_wallpaper(self, path: str) -> bool:
        return self._submit("set wallpaper", "set_wallpaper", path)

    def set_taskbar_alignment(self, alignment: str) -> bool:
        return self._submit("set taskbar alignment", "set_taskbar_alignment", alignment)


# ---------------------------------------------------------------------------
# real executors for the apply page
# ---------------------------------------------------------------------------
def _need(name: str) -> Any:
    mod = core_module(name)
    if mod is None:
        raise RuntimeError("lindos.%s is not available (is lindos-core installed?)" % name)
    return mod


def _exec_write_config(step: Step, logf: LogFn) -> Any:
    config = _need("config")
    cfg = config.Config.load()
    rejected: List[str] = []
    for key, value in step.payload.items():
        try:
            cfg.set(key, value)
        except Exception:
            try:
                cfg[key] = value
            except Exception as exc:  # unknown key rejected by lindos.config -> keep going
                log.warning("config key %r rejected: %s", key, exc)
                rejected.append(key)
    cfg.save()
    logf("config saved: %s" % ", ".join(
        "%s=%s" % (k, v) for k, v in step.payload.items() if k not in rejected))
    if rejected:
        return True, "keys not stored: %s" % ", ".join(rejected)
    return True


def _exec_set_theme(step: Step, logf: LogFn) -> Any:
    theme = _need("theme")
    return _ok(theme.set_dark(bool(step.payload.get("dark", True))))


def _exec_set_accent(step: Step, logf: LogFn) -> Any:
    theme = _need("theme")
    return _ok(theme.set_accent(str(step.payload["accent"])))


def _exec_set_wallpaper(step: Step, logf: LogFn) -> Any:
    theme = _need("theme")
    path = str(step.payload["path"])
    root = os.environ.get("LINDOS_ROOT", "").strip()
    probes = [path]
    if root:
        probes.insert(0, os.path.join(root, path.lstrip("/")))
    if not any(os.path.exists(p) for p in probes):
        return False, "wallpaper %s not found" % path
    return _ok(theme.set_wallpaper(path))


def _exec_set_taskbar(step: Step, logf: LogFn) -> Any:
    theme = _need("theme")
    return _ok(theme.set_taskbar_alignment(str(step.payload["alignment"])))


def _exec_apply_mode(step: Step, logf: LogFn) -> Any:
    modes = _need("modes")
    mode_id = str(step.payload["mode"])
    # install=False: configuration only - the installer already installed the Mode's packages
    result = modes.apply_mode(mode_id, log=logf, install=False)
    steps = getattr(result, "steps", None)
    if steps:
        for entry in steps:
            try:
                name, ok, msg = entry
            except (TypeError, ValueError):
                continue
            logf("  %s %s%s" % ("+" if ok else "!", name, (" - " + str(msg)) if msg else ""))
    ok = _ok(result)
    return ok, "" if ok else "mode apply reported problems (see log)"


def _exec_set_default_browser(step: Step, logf: LogFn) -> Any:
    browsers = _need("browsers")
    bid = str(step.payload["browser"])
    try:
        installed = bool(browsers.is_installed(bid))
    except Exception:
        installed = True
    if installed:
        return _ok(browsers.set_default(bid))
    if step.payload.get("pending") or browser_state(bid) == BROWSER_PENDING:
        # not a failure: the choice is stored (config + system.json) and it becomes the default
        # once the browser lands.  The personal default is left alone here on purpose (Firefox
        # until then; nothing is pointed at a browser that is not installed).  Two things make
        # the promise true, neither of which needs this wizard to be running any more:
        #   * the silent retry (lindos-browser-firstboot.service) writes /etc/xdg/mimeapps.list
        #     for GIO-based openers - but XFCE's own preferred web browser (exo-open, the browser
        #     key, the menu's web search) reads xfce4/helpers.rc, and the system-wide one of the
        #     base says Firefox, so
        #   * `lindos-browser sync-default` - an autostart entry of lindos-desktop, run as this
        #     user at every login and waiting for the retry - calls lindos.browsers.set_default
        #     once the chosen browser is installed (never over a browser picked in the meantime).
        return True, ("%s is not installed yet; it becomes the default browser when it is added "
                      "(Firefox until then)" % bid)
    return False, "%s is not installed; default browser unchanged" % bid


def _exec_helper(step: Step, logf: LogFn) -> Any:
    helper = _need("helper")
    result = helper.run_privileged(step.action, dict(step.payload), log=logf)
    out = (getattr(result, "out", "") or "").strip()
    err = (getattr(result, "err", "") or "").strip()
    if out:
        for line in out.splitlines()[-20:]:
            logf("  " + line)
    if err:
        for line in err.splitlines()[-20:]:
            logf("  ! " + line)
    ok = _ok(result)
    code = getattr(result, "code", None)
    return ok, "" if ok else "helper %s exited with %s" % (step.action, code)


class SystemBatch:
    """Every ``kind=system`` step of a plan, run through ONE ``lindos-helper`` process.

    The first system step that the :class:`~lindos_setup.plan.Runner` reaches triggers
    :meth:`run`, which sends all of the plan's privileged work to
    ``lindos.helper.run_privileged_batch`` -- a single ``pkexec``, i.e. a single password prompt.
    Later system steps only read the cached per-step outcomes, so ``Runner`` / ``Plan`` /
    ``Step`` semantics, per-step UI callbacks and failure isolation are unchanged.  The batch is
    configuration only (system defaults, and the Mode's tuning with ``install=False``: nothing is
    downloaded).  Work that needs no root stays in the executors and runs as the user after the
    batch: the rest of ``apply-mode`` (config, panel, compositor, ``apply-user.sh``).

    ``run_batch`` is injectable for tests; the default is ``lindos.helper.run_privileged_batch``.
    """

    def __init__(self, plan: _plan.Plan, run_batch: Optional[Callable[..., Any]] = None) -> None:
        self.plan = plan
        self._run_batch = run_batch
        self._lock = threading.Lock()
        self._started = False
        self.entries: List[Dict[str, Any]] = []
        self.outcomes: Dict[str, Tuple[bool, str]] = {}
        self.result: Any = None

    # -- building / running the batch ------------------------------------------
    def steps(self) -> List[Step]:
        return [s for s in self.plan.system_steps() if s.action in _plan.SYSTEM_ACTION_ORDER]

    def _prepare(self, step: Step, logf: LogFn) -> Optional[Dict[str, Any]]:
        """The batch entry for *step*, or None when it needs no helper (outcome recorded here)."""
        payload = dict(step.payload)
        if step.action == ACT_APPLY_MODE:
            modes = _need("modes")
            payload = modes.system_plan(str(step.payload["mode"]), install=False)
        return {"id": step.id, "action": step.action, "payload": payload}

    def run(self, logf: LogFn) -> None:
        """Send the whole plan's privileged work to the helper once (idempotent)."""
        with self._lock:
            if self._started:
                return
            self._started = True
            for step in self.steps():
                try:
                    entry = self._prepare(step, logf)
                except Exception as exc:  # one broken step must not sink the others
                    log.warning("cannot prepare %s: %s", step.id, exc)
                    self.outcomes[step.id] = (False, "%s: %s" % (type(exc).__name__, exc))
                    continue
                if entry is not None:
                    self.entries.append(entry)
            if not self.entries:
                return
            try:
                run_batch = self._run_batch or _need("helper").run_privileged_batch
                logf("Applying %d system change(s) with a single administrator prompt..." % len(self.entries))
                self.result = run_batch(list(self.entries), log=logf, on_step=self._on_step)
            except Exception as exc:
                log.error("privileged batch failed: %s", exc)
                for entry in self.entries:
                    self.outcomes.setdefault(entry["id"], (False, "%s: %s" % (type(exc).__name__, exc)))
                return
            for res in getattr(self.result, "results", None) or []:
                self.outcomes[res.id] = (bool(res.ok), str(res.message or ""))
            for entry in self.entries:
                self.outcomes.setdefault(entry["id"], (False, "the helper did not report this step"))

    def _on_step(self, res: Any) -> None:
        self.outcomes[res.id] = (bool(res.ok), str(res.message or ""))

    def outcome(self, step: Step) -> Tuple[bool, str]:
        return self.outcomes.get(step.id, (False, "no result was recorded for this step"))

    # -- executors (one per system action) --------------------------------------
    def exec_helper(self, step: Step, logf: LogFn) -> Any:
        self.run(logf)
        ok, msg = self.outcome(step)
        if ok:
            return True
        return False, ("helper %s failed: %s" % (step.action, msg)) if msg else "helper %s failed" % step.action

    def exec_apply_mode(self, step: Step, logf: LogFn) -> Any:
        self.run(logf)
        modes = _need("modes")
        mode_id = str(step.payload["mode"])
        result = modes.apply_mode(mode_id, log=logf, defer_system=True)   # the user half, as the user
        for entry in getattr(result, "steps", None) or []:
            try:
                name, ok, msg = entry
            except (TypeError, ValueError):
                continue
            logf("  %s %s%s" % ("+" if ok else "!", name, (" - " + str(msg)) if msg else ""))
        problems: List[str] = []
        if not _ok(result):
            problems.append("mode apply reported problems (see log)")
        sys_ok, sys_msg = self.outcome(step)
        if not sys_ok:
            problems.append("system part failed: %s" % (sys_msg or "no details"))
        return (not problems), "; ".join(problems)


def make_real_executors(plan: Optional[_plan.Plan] = None) -> Dict[str, Executor]:
    """Executors that call the real lindos-core APIs (SPEC §13 call map).

    With *plan*, every privileged step shares one :class:`SystemBatch` (one helper run, one
    password prompt); without it each privileged step makes its own helper call (the original
    per-step behaviour, kept for callers that run a single step).  None of them installs anything.
    """
    execs = _make_step_executors()
    if plan is not None and plan.system_steps():
        batch = SystemBatch(plan)
        for action in _plan.SYSTEM_ACTION_ORDER:
            execs[action] = batch.exec_helper
        execs[ACT_APPLY_MODE] = batch.exec_apply_mode
    return execs


def _make_step_executors() -> Dict[str, Executor]:
    return {
        ACT_WRITE_CONFIG: _exec_write_config,
        ACT_SET_THEME: _exec_set_theme,
        ACT_SET_ACCENT: _exec_set_accent,
        ACT_SET_WALLPAPER: _exec_set_wallpaper,
        ACT_SET_TASKBAR: _exec_set_taskbar,
        ACT_SET_DEFAULT_BROWSER: _exec_set_default_browser,
        ACT_WRITE_SYSTEM_CONFIG: _exec_helper,
        ACT_APPLY_MODE: _exec_apply_mode,
    }


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
def headless_dry_run(logger: Optional[logging.Logger] = None,
                     write: Optional[Callable[[str], None]] = None) -> int:
    """No display / no GTK: print the default plan JSON and return 0."""
    import sys
    logger = logger or log
    emit = write if write is not None else (lambda s: (sys.stdout.write(s + "\n"), sys.stdout.flush()))
    plan = _plan.build_plan(_plan.Selections())
    emit(plan.to_json())
    logger.info("headless dry-run: printed default plan (%d steps)", len(plan))
    return 0


def which(cmd: str) -> Optional[str]:
    import shutil
    return shutil.which(cmd)


def transfer_sources(*, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """``lindos-transfer sources --json`` (SPEC-WINDOWS §29.3), fully guarded: a missing binary,
    a non-zero exit, a timeout or invalid JSON all degrade to an honest empty result with a
    ``note`` -- this must never raise and never block the wizard's UI thread (call it from a
    worker thread; see ``pages.TransferPage``).

    Returns ``{"partitions": [...], "bundles": [...], "available": bool, "note": str}``.
    """
    exe = which("lindos-transfer")
    if not exe:
        return {"partitions": [], "bundles": [], "available": False,
                "note": "The Transfer tool (lindos-transfer) is not installed."}
    try:
        proc = run([exe, "sources", "--json"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lindos-transfer sources failed: %s", exc)
        return {"partitions": [], "bundles": [], "available": False,
                "note": "lindos-transfer sources failed: %s" % exc}
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        log.warning("lindos-transfer sources exited %s: %s", getattr(proc, "returncode", "?"), err)
        return {"partitions": [], "bundles": [], "available": False,
                "note": err or "lindos-transfer sources reported an error"}
    try:
        data = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError as exc:
        log.warning("lindos-transfer sources returned invalid JSON: %s", exc)
        return {"partitions": [], "bundles": [], "available": False,
                "note": "lindos-transfer returned data Lindos Setup could not read (%s)" % exc}
    parts = data.get("partitions") if isinstance(data, dict) else None
    bundles = data.get("bundles") if isinstance(data, dict) else None
    return {
        "partitions": [p for p in (parts or []) if isinstance(p, dict)],
        "bundles": [b for b in (bundles or []) if isinstance(b, dict)],
        "available": True,
        "note": "",
    }


def launch_transfer_gui(source: str = "") -> bool:
    """Start ``lindos-transfer-gui [--from SOURCE]`` detached; False if it is not installed."""
    exe = which("lindos-transfer-gui")
    if not exe:
        log.warning("lindos-transfer-gui not found in PATH")
        return False
    argv = [exe] + (["--from", source] if source else [])
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        log.error("cannot start lindos-transfer-gui: %s", exc)
        return False


def launch_settings(page: Optional[str] = None) -> bool:
    """Start ``lindos-settings [page]`` detached; False if it is not installed."""
    exe = which("lindos-settings")
    if not exe:
        log.warning("lindos-settings not found in PATH")
        return False
    argv = [exe] + ([page] if page else [])
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        log.error("cannot start lindos-settings: %s", exc)
        return False


__all__ = [
    "core_module", "core_available", "home_dir", "user_path", "setup_done_path", "log_dir",
    "log_file", "setup_done_exists", "in_xfce", "mark_setup_done", "load_modes", "RAM_HINTS",
    "browsers_table", "browser_installed", "ram_total_mb", "list_wallpapers",
    "is_live_session", "is_oem_temp_user", "install_steps", "browser_state", "browser_states",
    "wallpaper_display_name", "LiveApplier", "SystemBatch", "make_real_executors", "launch_settings", "which",
    "headless_dry_run", "transfer_sources", "launch_transfer_gui",
]
