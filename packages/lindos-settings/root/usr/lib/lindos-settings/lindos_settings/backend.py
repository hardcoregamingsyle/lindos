"""Adapter between the Settings UI and the outside world.

Everything that touches lindos-core (``lindos.*``), external CLIs (``lindos-run``,
``lindos-proton``, ``lindos-drivers``, ``lindos-game``, ``lindos-tune``, ``lindos-compat``,
``lindos-mode``, ``lindos-compositor``, ``xfconf-query``, ``xrandr`` …) or the filesystem lives
here, so the pages stay thin and the model stays pure.

Rules:
* stdlib only, no GTK, nothing executed at import time (importable on Windows);
* every lindos-core import is lazy and guarded — when lindos-core is missing the app still
  starts and every operation degrades to an honest fallback (logged) or returns "unavailable";
* never ``shell=True``.
"""

from __future__ import annotations

import glob
import importlib
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from . import model

log = logging.getLogger("lindos.settings.backend")

DEFAULT_TIMEOUT = 20

# SPEC §4.2 defaults (used only when lindos-core is unavailable)
CONFIG_DEFAULTS: dict[str, Any] = {
    "mode": "everyday",
    "browser": "chrome",
    "theme": "dark",
    "accent": "#60CDFF",
    "wallpaper": "/usr/share/backgrounds/lindos/aurora-dark.svg",
    "setup_done": False,
    "gamemode_auto": True,
    "mangohud": False,
    "telemetry": False,
    "schema": 1,
}


@dataclass
class CmdResult:
    code: int
    out: str = ""
    err: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out or "") + (("\n" + self.err) if self.err else "")


@dataclass
class HelperResult:
    ok: bool
    out: str = ""
    err: str = ""
    code: int = 1


@dataclass
class ApplyResult:
    ok: bool
    steps: list[tuple[str, bool, str]] = field(default_factory=list)


def _home() -> str:
    return os.environ.get("LINDOS_HOME") or os.path.expanduser("~")


def _sys_root() -> str:
    return os.environ.get("LINDOS_ROOT", "").rstrip("/\\")


def _clean_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


class _FallbackConfig:
    """Minimal replacement for lindos.config.Config (used only if lindos-core is missing)."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, Any] = dict(CONFIG_DEFAULTS)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("config %s unreadable: %s", path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def as_dict(self) -> dict[str, Any]:
        return dict(self.data)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)


class Backend:
    """Facade used by every page.  One instance per application."""

    def __init__(self) -> None:
        self._mods: dict[str, Any] = {}
        self._missing: set[str] = set()
        self._config: Any = None
        self._config_mtime: Optional[float] = None
        self._lock = threading.Lock()
        self._flatpak_cache: Optional[tuple[float, set[str]]] = None

    # ------------------------------------------------------------------ lindos-core access
    def mod(self, name: str) -> Any:
        """Lazily import ``lindos.<name>``; None (logged once) if unavailable."""
        if name in self._mods:
            return self._mods[name]
        if name in self._missing:
            return None
        try:
            m = importlib.import_module(f"lindos.{name}")
        except Exception as exc:  # ImportError or anything raised at import
            self._missing.add(name)
            log.warning("lindos-core module lindos.%s unavailable: %s", name, exc)
            return None
        self._mods[name] = m
        return m

    @property
    def core_available(self) -> bool:
        return self.mod("config") is not None

    def _call(self, modname: str, fn: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
        m = self.mod(modname)
        if m is None or not hasattr(m, fn):
            return default
        try:
            return getattr(m, fn)(*args, **kwargs)
        except Exception as exc:
            log.warning("lindos.%s.%s failed: %s", modname, fn, exc)
            return default

    # ------------------------------------------------------------------ paths
    def path(self, name: str, default: str) -> str:
        p = self.mod("paths")
        value = getattr(p, name, None) if p else None
        if not isinstance(value, str) or not value:
            value = default
        if value.startswith("~"):
            return os.path.join(_home(), value[2:]) if value.startswith("~/") else _home()
        if value.startswith("/") and _sys_root() and not value.startswith(_sys_root()):
            return _sys_root() + value
        return value

    def prefixes_dir(self) -> str:
        return self.path("PREFIXES_DIR", "~/.local/share/lindos/prefixes")

    def recipes_dir(self) -> str:
        return self.path("RECIPES_DIR", model.RECIPES_DIR)

    def share_dir(self) -> str:
        return self.path("SHARE_DIR", "/usr/share/lindos")

    def state_dir(self) -> str:
        return self.path("STATE_DIR", "~/.local/share/lindos")

    def log_dir(self) -> str:
        return self.path("LOG_DIR", "~/.local/state/lindos")

    def user_conf_path(self) -> str:
        return self.path("USER_CONF", "~/.config/lindos/config.json")

    # ------------------------------------------------------------------ config
    def _config_file_mtime(self) -> Optional[float]:
        try:
            return os.stat(self.user_conf_path()).st_mtime
        except OSError:
            return None

    def _load_config(self) -> Any:
        cfg = self.mod("config")
        loaded: Any = None
        if cfg is not None and hasattr(cfg, "Config"):
            try:
                loaded = cfg.Config.load()
            except Exception as exc:
                log.warning("Config.load failed: %s — using fallback config", exc)
        if loaded is None:
            loaded = _FallbackConfig(self.user_conf_path())
        return loaded

    def config(self) -> Any:
        """The user config.  Re-read from disk whenever the file changed on disk (lindos-core's
        theme/mode functions write it too — never clobber their keys with a stale copy)."""
        with self._lock:
            mtime = self._config_file_mtime()
            if self._config is None or mtime != self._config_mtime:
                self._config = self._load_config()
                self._config_mtime = mtime
            return self._config

    def reload_config(self) -> None:
        with self._lock:
            self._config = None
            self._config_mtime = None

    def config_get(self, key: str, default: Any = None) -> Any:
        cfg = self.config()
        try:
            value = cfg.get(key, default)
        except TypeError:
            value = cfg.get(key)
        return default if value is None else value

    def config_set(self, key: str, value: Any) -> bool:
        with self._lock:
            cfg = self._load_config()  # read-modify-write against the current file
            try:
                cfg.set(key, value)
                cfg.save()
            except Exception as exc:
                log.error("config set %s failed: %s", key, exc)
                return False
            self._config = cfg
            self._config_mtime = self._config_file_mtime()
            return True

    def effective_mode(self) -> str:
        m = self._call("config", "effective_mode", default=None)
        if isinstance(m, str) and m:
            return m
        return str(self.config_get("mode", "everyday") or "everyday")

    def effective_browser(self) -> str:
        m = self._call("config", "effective_browser", default=None)
        return m if isinstance(m, str) and m else str(self.config_get("browser", "firefox"))

    # ------------------------------------------------------------------ browsers (SPEC §4.4)
    BROWSER_FALLBACK: tuple[tuple[str, str, str], ...] = (
        ("edge", "Microsoft Edge", "microsoft-edge.desktop"),
        ("chrome", "Google Chrome", "google-chrome.desktop"),
        ("firefox", "Mozilla Firefox", "firefox.desktop"),
    )

    def browsers(self) -> list[dict[str, Any]]:
        """Edge / Chrome / Firefox with ``installed`` / ``default`` flags (lindos.browsers, guarded).

        Edge and Chrome are never on the ISO (licence); they are downloaded from the vendor's apt
        repository by the helper action ``install-browser`` — Settings > Apps offers exactly that
        so an offline first boot can be finished later (SPEC §0.1, §6)."""
        rows = self._call("browsers", "list_browsers", default=None)
        current = self._call("browsers", "default_browser", default=None)
        if not isinstance(rows, list) or not rows:
            rows = [{"id": bid, "name": name, "desktop": desk, "installed": bool(self.which(bid) or self.which(desk.split(".desktop")[0])),
                     "on_iso": bid == "firefox",
                     "note": "Ships on the ISO" if bid == "firefox" else "Downloaded from the vendor's official apt repository (needs internet)"}
                    for bid, name, desk in self.BROWSER_FALLBACK]
        out: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            entry = dict(r)
            entry["default"] = bool(current) and current == entry["id"]
            out.append(entry)
        return out

    def install_browser(self, bid: str, set_default: bool = True) -> HelperResult:
        """Install *bid* (helper ``install-browser`` via lindos.browsers.install) and optionally
        make it the default (``xdg-settings``).  Falls back to the ``lindos-browser`` CLI."""
        lines: list[str] = []
        b = self.mod("browsers")
        if b is not None and hasattr(b, "install"):
            try:
                ok = bool(b.install(bid, log=lines.append))
            except Exception as exc:  # helper missing, bad id, …
                return HelperResult(False, "\n".join(lines), str(exc), 1)
            if ok and set_default:
                try:
                    if not b.set_default(bid):
                        lines.append(f"{bid}: installed, but could not make it the default browser")
                except Exception as exc:
                    lines.append(f"{bid}: set_default failed: {exc}")
            if ok:
                self.config_set("browser", bid)
            return HelperResult(ok, "\n".join(lines), "" if ok else "\n".join(lines[-3:]), 0 if ok else 1)
        if not self.which("lindos-browser"):
            return HelperResult(False, "", "lindos-core is not installed (lindos-browser missing)", 127)
        argv = ["lindos-browser", "install", bid] + (["--set-default"] if set_default else [])
        r = self.run(argv, timeout=1800)
        return HelperResult(r.ok, r.out, r.err, r.code)

    def set_default_browser(self, bid: str) -> bool:
        ok = self._call("browsers", "set_default", bid, default=None)
        if ok is None:
            if not self.which("lindos-browser"):
                return False
            ok = self.run(["lindos-browser", "set-default", bid], timeout=60).ok
        if ok:
            self.config_set("browser", bid)
        return bool(ok)

    # ------------------------------------------------------------------ processes
    @staticmethod
    def which(cmd: str) -> Optional[str]:
        return shutil.which(cmd)

    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT, env: Optional[dict[str, str]] = None, cwd: Optional[str] = None) -> CmdResult:
        argv_l = [str(a) for a in argv]
        try:
            cp = subprocess.run(  # noqa: S603 — argv list, never shell
                argv_l,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_clean_env(env),
                cwd=cwd,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            return CmdResult(cp.returncode, cp.stdout or "", cp.stderr or "", argv_l)
        except FileNotFoundError:
            return CmdResult(127, "", f"{argv_l[0] if argv_l else '?'}: not found", argv_l)
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", f"{' '.join(argv_l)}: timed out after {timeout:.0f}s", argv_l)
        except (OSError, ValueError) as exc:
            return CmdResult(1, "", str(exc), argv_l)

    def spawn(self, argv: Sequence[str], env: Optional[dict[str, str]] = None, cwd: Optional[str] = None) -> bool:
        """Launch a GUI tool detached (no terminal, does not block, survives us)."""
        argv_l = [str(a) for a in argv]
        if not argv_l:
            return False
        try:
            subprocess.Popen(  # noqa: S603
                argv_l,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(env),
                cwd=cwd,
            )
            log.info("spawned: %s", " ".join(argv_l))
            return True
        except (OSError, ValueError) as exc:
            log.error("spawn %s failed: %s", argv_l, exc)
            return False

    def stream(
        self,
        argv: Sequence[str],
        on_line: Callable[[str], None],
        on_done: Callable[[int], None],
        env: Optional[dict[str, str]] = None,
    ) -> threading.Thread:
        """Run ``argv`` in a worker thread, calling ``on_line`` for each output line and
        ``on_done(exit_code)`` at the end.  Callbacks run **in the worker thread** — the widget
        layer marshals them onto the GTK main loop."""
        argv_l = [str(a) for a in argv]

        def worker() -> None:
            code = 1
            try:
                proc = subprocess.Popen(  # noqa: S603
                    argv_l,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=_clean_env(env),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    on_line(line.rstrip("\n"))
                code = proc.wait()
            except FileNotFoundError:
                on_line(f"error: '{argv_l[0]}' is not installed")
                code = 127
            except (OSError, ValueError) as exc:
                on_line(f"error: {exc}")
                code = 1
            on_done(code)

        t = threading.Thread(target=worker, name=f"stream:{argv_l[0] if argv_l else '?'}", daemon=True)
        t.start()
        return t

    def open_path(self, path: str) -> bool:
        return self.spawn(["xdg-open", path])

    def open_url(self, url: str) -> bool:
        return self.spawn(["xdg-open", url])

    # ------------------------------------------------------------------ xfconf
    def xfconf_get(self, channel: str, prop: str) -> Optional[str]:
        r = self.run(["xfconf-query", "-c", channel, "-p", prop], timeout=5)
        return r.out.strip() if r.ok else None

    def xfconf_set(self, channel: str, prop: str, value: Any, vtype: str = "string") -> bool:
        val = str(value)
        if isinstance(value, bool):
            val = "true" if value else "false"
            vtype = "bool"
        r = self.run(["xfconf-query", "-c", channel, "-p", prop, "-s", val], timeout=5)
        if r.ok:
            return True
        r = self.run(["xfconf-query", "-c", channel, "-p", prop, "-n", "-t", vtype, "-s", val], timeout=5)
        if not r.ok:
            log.warning("xfconf set %s%s failed: %s", channel, prop, r.err.strip())
        return r.ok

    # ------------------------------------------------------------------ helper (privileged)
    def run_privileged(self, action: str, payload: Optional[dict[str, Any]] = None) -> HelperResult:
        payload = payload or {}
        h = self.mod("helper")
        if h is not None and hasattr(h, "run_privileged"):
            try:
                res = h.run_privileged(action, payload, log=log.info)
                return HelperResult(bool(getattr(res, "ok", False)), str(getattr(res, "out", "") or ""), str(getattr(res, "err", "") or ""), int(getattr(res, "code", 1) or 0))
            except Exception as exc:
                log.error("helper %s failed: %s", action, exc)
                return HelperResult(False, "", str(exc), 1)
        # Fallback: call the helper binary directly through pkexec / sudo -n
        helper = self.path("HELPER", "/usr/libexec/lindos/lindos-helper")
        if not os.path.exists(helper):
            return HelperResult(False, "", "lindos-core helper is not installed (/usr/libexec/lindos/lindos-helper)", 127)
        argv: list[str]
        if hasattr(os, "geteuid") and os.geteuid() == 0:  # type: ignore[attr-defined]
            argv = [helper, action, json.dumps(payload)]
        elif self.which("pkexec"):
            argv = ["pkexec", helper, action, json.dumps(payload)]
        elif self.which("sudo"):
            argv = ["sudo", "-n", helper, action, json.dumps(payload)]
        else:
            return HelperResult(False, "", "neither pkexec nor sudo is available", 127)
        r = self.run(argv, timeout=1800)
        return HelperResult(r.ok, r.out, r.err, r.code)

    def install_packages(self, packages: Sequence[str]) -> HelperResult:
        return self.run_privileged("install-packages", {"packages": [str(p) for p in packages]})

    # ------------------------------------------------------------------ theme
    def _theme(self, fn: str, *args: Any) -> Optional[bool]:
        """Call ``lindos.theme.<fn>(*args)``.  ``None`` when lindos.theme (or the function) is
        missing — the caller then applies its own fallback; ``False`` when the call raised."""
        m = self.mod("theme")
        if m is None or not hasattr(m, fn):
            return None
        try:
            return bool(getattr(m, fn)(*args))
        except Exception as exc:
            log.warning("lindos.theme.%s failed: %s", fn, exc)
            return False

    def is_dark(self) -> bool:
        v = self._call("theme", "is_dark", default=None)
        if isinstance(v, bool):
            return v
        name = self.xfconf_get("xsettings", "/Net/ThemeName") or ""
        if name:
            return "dark" in name.lower()
        return str(self.config_get("theme", "dark")).lower() != "light"

    def set_dark(self, dark: bool) -> bool:
        ok = self._theme("set_dark", bool(dark))
        if ok is None:
            log.warning("lindos.theme missing — setting theme via xfconf-query directly")
            gtk = "Lindos-Dark" if dark else "Lindos-Light"
            ok = self.xfconf_set("xsettings", "/Net/ThemeName", gtk)
            self.xfconf_set("xsettings", "/Net/IconThemeName", "Lindos")
            self.xfconf_set("xfwm4", "/general/theme", gtk)
            self.xfconf_set("xsettings", "/Gtk/CursorThemeName", "Fluent-dark-cursors" if dark else "Fluent-cursors")
        self.config_set("theme", "dark" if dark else "light")
        return bool(ok)

    def set_accent(self, hex_color: str) -> bool:
        ok = self._theme("set_accent", hex_color)
        self.config_set("accent", hex_color)
        if ok is None:
            log.warning("lindos.theme missing — accent stored in config only")
            return False
        return bool(ok)

    def list_wallpapers(self) -> list[str]:
        v = self._call("theme", "list_wallpapers", default=None)
        if isinstance(v, (list, tuple)) and v:
            return [str(p) for p in v]
        base = _sys_root() + "/usr/share/backgrounds/lindos"
        found: list[str] = []
        for ext in ("svg", "png", "jpg", "jpeg", "webp"):
            found.extend(sorted(glob.glob(os.path.join(base, f"*.{ext}"))))
        return found

    def set_wallpaper(self, path: str) -> bool:
        ok = self._theme("set_wallpaper", path)
        self.config_set("wallpaper", path)
        if ok is None:
            log.warning("lindos.theme missing — setting wallpaper via xfconf-query")
            r = self.run(["xfconf-query", "-c", "xfce4-desktop", "-l"], timeout=5)
            done = False
            for prop in r.out.split():
                if prop.endswith("/last-image"):
                    done = self.xfconf_set("xfce4-desktop", prop, path) or done
            return done
        return bool(ok)

    def set_font(self, name: str) -> bool:
        ok = self._theme("set_font", name)
        if ok is None:
            return self.xfconf_set("xsettings", "/Gtk/FontName", name)
        return bool(ok)

    def current_font(self) -> str:
        return self.xfconf_get("xsettings", "/Gtk/FontName") or "Selawik 10"

    def set_taskbar_alignment(self, where: str) -> bool:
        ok = self._theme("set_taskbar_alignment", where)
        if ok is None:
            log.warning("lindos.theme missing — cannot change taskbar alignment")
            return False
        if ok:
            self.config_set("taskbar_alignment", where)
        return bool(ok)

    def set_taskbar_position(self, where: str) -> bool:
        ok = self._theme("set_taskbar_position", where)
        if ok is None:
            log.warning("lindos.theme missing — cannot change taskbar position")
            return False
        if ok:
            self.config_set("taskbar_position", where)
        return bool(ok)

    # ------------------------------------------------------------------ gaming toggles
    def set_mangohud(self, on: bool) -> bool:
        """Store the ``mangohud`` config key and push it into the per-user MangoHud.conf.

        MangoHud only reads ``~/.config/MangoHud/MangoHud.conf``; ``lindos-mangohud sync``
        (lindos-gaming) applies the Lindos config key there (``no_display`` on/off).  Without
        lindos-gaming the key is still stored (lindos-run honours it for the ``mangohud`` wrapper).
        """
        ok = self.config_set("mangohud", bool(on))
        if ok and self.which("lindos-mangohud"):
            r = self.run(["lindos-mangohud", "sync"], timeout=20)
            if not r.ok:
                log.warning("lindos-mangohud sync failed: %s", (r.err or r.out).strip())
        return ok

    def taskbar_alignment(self) -> str:
        return str(self.config_get("taskbar_alignment", "center") or "center")

    def taskbar_position(self) -> str:
        return str(self.config_get("taskbar_position", "bottom") or "bottom")

    def cursor_theme(self) -> str:
        return self.xfconf_get("xsettings", "/Gtk/CursorThemeName") or ""

    def set_cursor_theme(self, name: str) -> bool:
        return self.xfconf_set("xsettings", "/Gtk/CursorThemeName", name)

    def cursor_themes(self) -> list[str]:
        names: set[str] = set()
        roots = [os.path.join(_home(), ".icons"), os.path.join(_home(), ".local/share/icons"), _sys_root() + "/usr/share/icons"]
        for root in roots:
            try:
                for entry in os.listdir(root):
                    if os.path.isdir(os.path.join(root, entry, "cursors")):
                        names.add(entry)
            except OSError:
                continue
        return sorted(names, key=str.lower)

    def accents(self) -> list[tuple[str, str]]:
        path = _sys_root() + model.ACCENTS_JSON
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return model.parse_accents(json.load(fh))
        except (OSError, ValueError) as exc:
            if os.path.exists(path):
                log.warning("accents.json unreadable: %s", exc)
            return list(model.DEFAULT_ACCENTS)

    # ------------------------------------------------------------------ modes
    def load_modes(self) -> dict[str, Any]:
        v = self._call("modes", "load_modes", default=None)
        if isinstance(v, dict) and v:
            return v
        # honest fallback: static names/descriptions only
        return {mid: {"id": mid, "name": model.MODE_NAMES[mid], "description": model.MODE_DESCRIPTIONS[mid], "icon": model.MODE_ICONS[mid]} for mid in model.MODE_IDS}

    @staticmethod
    def mode_as_dict(mode: Any) -> dict[str, Any]:
        if isinstance(mode, dict):
            d = dict(mode)
        else:
            d = {}
            for key in ("id", "name", "description", "icon", "packages", "flatpaks", "services_disable", "services_enable", "sysctl", "governor", "pins", "compositor", "zram_percent", "path"):
                d[key] = getattr(mode, key, None)
        for key in ("packages", "flatpaks", "services_disable", "services_enable", "pins"):
            if not isinstance(d.get(key), (list, tuple)):
                d[key] = []
        if not isinstance(d.get("sysctl"), dict):
            d["sysctl"] = {}
        return d

    def apply_mode(self, mode_id: str, log_cb: Callable[[str], None]) -> ApplyResult:
        m = self.mod("modes")
        if m is not None and hasattr(m, "apply_mode"):
            try:
                res = m.apply_mode(mode_id, log=log_cb)
                steps = [(str(s[0]), bool(s[1]), str(s[2]) if len(s) > 2 else "") for s in (getattr(res, "steps", None) or [])]
                ok = bool(getattr(res, "ok", False))
                self.reload_config()
                return ApplyResult(ok, steps)
            except Exception as exc:
                log_cb(f"apply_mode raised: {exc}")
                return ApplyResult(False, [("apply_mode", False, str(exc))])
        # fallback: lindos-mode CLI
        if self.which("lindos-mode"):
            log_cb("lindos-core python module missing; delegating to `lindos-mode set`")
            done = threading.Event()
            code_box: dict[str, int] = {"code": 1}

            def _done(code: int) -> None:
                code_box["code"] = code
                done.set()

            self.stream(["lindos-mode", "set", mode_id], log_cb, _done)
            done.wait()
            self.reload_config()
            return ApplyResult(code_box["code"] == 0, [("lindos-mode set " + mode_id, code_box["code"] == 0, f"exit {code_box['code']}")])
        log_cb("lindos-core is not installed: cannot apply a mode.")
        return ApplyResult(False, [("lindos-core", False, "not installed")])

    # ------------------------------------------------------------------ hardware
    def cpu_info(self) -> dict[str, Any]:
        info = self._call("hardware", "cpu_info", default=None)
        if not isinstance(info, dict):
            info = self._cpu_info_fallback()
        return model.normalize_cpu(info)

    @staticmethod
    def _cpu_info_fallback() -> dict[str, Any]:
        modelname = platform.processor() or ""
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        modelname = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        return {"model": modelname or "Unknown CPU", "threads": os.cpu_count() or 0, "arch": platform.machine()}

    def gpu_info(self) -> list[dict[str, Any]]:
        info = self._call("hardware", "gpu_info", default=None)
        gpus = model.normalize_gpus(info) if info is not None else []
        if not gpus:
            r = self.run(["lspci", "-nn"], timeout=5)
            for line in r.out.splitlines():
                low = line.lower()
                if "vga" in low or "3d controller" in low or "display controller" in low:
                    desc = line.split(":", 2)[-1].strip()
                    gpus.append({"vendor": model.guess_gpu_vendor(desc), "model": desc, "driver": ""})
        return gpus

    def ram_info(self) -> dict[str, Any]:
        info = self._call("hardware", "ram_info", default=None)
        if not isinstance(info, dict):
            info = self._meminfo()
        return model.normalize_ram(info)

    @staticmethod
    def _meminfo() -> dict[str, Any]:
        vals: dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    num = rest.strip().split()[0] if rest.strip() else "0"
                    vals[key] = int(num) // 1024
        except (OSError, ValueError):
            return {}
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable", 0)
        return {"total": total, "available": avail, "used": max(0, total - avail)}

    def ram_snapshot(self) -> dict[str, Any]:
        """``lindos.ram.snapshot()`` → {total, used, available, top}; when lindos.ram is missing,
        /proc/meminfo + the top RSS processes from /proc (Linux only)."""
        snap = self._call("ram", "snapshot", default=None)
        if not isinstance(snap, dict):
            snap = self._meminfo()
            snap["top"] = self._top_rss()
        return model.normalize_snapshot(snap)

    @staticmethod
    def _top_rss(limit: int = 8) -> list[tuple[str, float]]:
        procs: dict[str, float] = {}
        for status in glob.glob("/proc/[0-9]*/status"):
            name = ""
            rss_kb = 0
            try:
                with open(status, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("Name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("VmRSS:"):
                            rss_kb = int(line.split(":", 1)[1].strip().split()[0])
                            break
            except (OSError, ValueError, IndexError):
                continue
            if name and rss_kb:
                procs[name] = procs.get(name, 0.0) + rss_kb / 1024.0
        return sorted(((n, round(mb, 1)) for n, mb in procs.items()), key=lambda t: -t[1])[:limit]

    def battery_present(self) -> bool:
        v = self._call("hardware", "battery_present", default=None)
        if isinstance(v, bool):
            return v
        return bool(glob.glob("/sys/class/power_supply/BAT*"))

    def available_governors(self) -> list[str]:
        v = self._call("hardware", "available_governors", default=None)
        if isinstance(v, (list, tuple)) and v:
            return [str(g) for g in v]
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "r", encoding="utf-8") as fh:
                return model.parse_governor_list(fh.read())
        except OSError:
            return []

    def current_governor(self) -> str:
        v = self._call("hardware", "current_governor", default=None)
        if isinstance(v, str) and v:
            return v
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def set_governor(self, governor: str) -> HelperResult:
        h = self.mod("hardware")
        if h is not None and hasattr(h, "set_governor"):
            try:
                res = h.set_governor(governor)
                if isinstance(res, bool):
                    return HelperResult(res, "", "" if res else "set_governor returned False", 0 if res else 1)
                return HelperResult(bool(getattr(res, "ok", res)), str(getattr(res, "out", "")), str(getattr(res, "err", "")), int(getattr(res, "code", 0)))
            except Exception as exc:
                return HelperResult(False, "", str(exc), 1)
        return self.run_privileged("set-governor", {"governor": governor})

    def epp_available(self) -> list[str]:
        v = self._call("hardware", "available_epp", default=None)
        if isinstance(v, (list, tuple)) and v:
            return [str(e) for e in v]
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_available_preferences", "r", encoding="utf-8") as fh:
                return model.parse_governor_list(fh.read())
        except OSError:
            return []

    def epp_current(self) -> str:
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    # EPP note: SPEC §4.6 has no dedicated privileged action for the energy/performance
    # preference; the helper's `set-governor` writes a matching EPP (performance →
    # performance, schedutil → balance_performance, powersave → power).  The UI therefore shows
    # EPP read-only and lets the governor drive it.
    EPP_BY_GOVERNOR: dict[str, str] = {"performance": "performance", "schedutil": "balance_performance", "powersave": "power", "ondemand": "balance_performance", "conservative": "balance_power"}

    def fan_sensors(self) -> list[dict[str, Any]]:
        """Fans + temperatures: lindos.hardware (fan_sensors + temperatures) else ``sensors -j``."""
        readings: list[dict[str, Any]] = []
        hw = self.mod("hardware")
        if hw is not None and (hasattr(hw, "temperatures") or hasattr(hw, "fan_sensors")):
            for fn in ("temperatures", "fan_sensors"):
                v = self._call("hardware", fn, default=None)
                if isinstance(v, list):
                    readings.extend(model.normalize_fans(v))
            return readings
        r = self.run(["sensors", "-j"], timeout=8)
        if r.ok:
            try:
                return model.normalize_fans(json.loads(r.out))
            except ValueError:
                return []
        return []

    def fan_profiles(self) -> list[str]:
        r = self.run(["lindos-tune", "fan", "list"], timeout=15)
        profiles: list[str] = []
        if r.ok:
            for line in model.parse_lines(r.out):
                token = line.split()[0].strip("*:").strip()
                if token and token.lower() not in ("profiles", "available", "name"):
                    profiles.append(token)
        return profiles

    def set_fan_profile(self, profile: str) -> HelperResult:
        return self.run_privileged("set-fan-profile", {"profile": profile})

    def refresh_rates(self) -> list[dict[str, Any]]:
        v = self._call("hardware", "refresh_rates", default=None)
        rates = model.normalize_refresh_rates(v) if v else []
        if not rates:
            r = self.run(["xrandr"], timeout=8)
            if r.ok:
                rates = model.parse_xrandr(r.out)
        return rates

    def set_refresh_rate(self, output: str, rate: str) -> CmdResult:
        return self.run(["xrandr", "--output", output, "--rate", rate], timeout=15)

    def power_profile(self) -> str:
        r = self.run(["powerprofilesctl", "get"], timeout=5)
        return r.out.strip() if r.ok else ""

    def power_profiles(self) -> list[str]:
        r = self.run(["powerprofilesctl", "list"], timeout=5)
        if not r.ok:
            return []
        out: list[str] = []
        for line in r.out.splitlines():
            s = line.strip()
            if s.endswith(":") and not s.startswith(("CpuDriver", "PlatformDriver", "Degraded", "Driver")):
                out.append(s.lstrip("* ").rstrip(":"))
        return out or ["performance", "balanced", "power-saver"]

    def set_power_profile(self, profile: str) -> HelperResult:
        """`lindos-tune power <p>` (SPEC §11) — powerprofilesctl talks to power-profiles-daemon
        over D-Bus for the active session, so no privileged helper action is needed (and the
        helper's `apply-tune` payload has no power key)."""
        errors: list[str] = []
        if self.which("lindos-tune"):
            r = self.run(["lindos-tune", "power", profile], timeout=30)
            if r.ok:
                return HelperResult(True, r.out, r.err, 0)
            errors.append(r.err.strip() or r.out.strip() or f"lindos-tune exit {r.code}")
        if self.which("powerprofilesctl"):
            r = self.run(["powerprofilesctl", "set", profile], timeout=10)
            if r.ok:
                return HelperResult(True, r.out, r.err, 0)
            errors.append(r.err.strip() or f"powerprofilesctl exit {r.code}")
        else:
            errors.append("power-profiles-daemon (powerprofilesctl) is not installed")
        return HelperResult(False, "", "; ".join(e for e in errors if e) or "could not set the power profile", 1)

    def drivers_status(self) -> CmdResult:
        """`lindos-drivers status --json` (falls back to `--status`, then `detect --json`)."""
        if not self.which("lindos-drivers"):
            return CmdResult(127, "", "lindos-drivers (lindos-gaming) is not installed")
        r = self.run(["lindos-drivers", "status", "--json"], timeout=40)
        if r.ok and r.out.strip():
            return r
        r2 = self.run(["lindos-drivers", "--status"], timeout=40)
        if r2.ok and r2.out.strip():
            return r2
        r3 = self.run(["lindos-drivers", "detect", "--json"], timeout=30)
        return r3 if r3.ok else r

    def install_drivers(self, vendor: str, variant: str = "") -> HelperResult:
        """helper `install-drivers` with ``{"driver": nvidia-open|nvidia-proprietary|amd|intel}``
        (``{"args": []}`` = auto-detect) → runs `lindos-drivers install …` as root."""
        return self.run_privileged("install-drivers", model.driver_install_payload(vendor, variant))

    def trim_status(self) -> dict[str, str]:
        en = self.run(["systemctl", "is-enabled", "fstrim.timer"], timeout=5)
        ac = self.run(["systemctl", "is-active", "fstrim.timer"], timeout=5)
        return {"enabled": (en.out.strip() or en.err.strip() or "unknown"), "active": (ac.out.strip() or ac.err.strip() or "unknown")}

    def enable_trim(self) -> HelperResult:
        return self.run_privileged("set-services", {"enable": ["fstrim.timer"], "disable": []})

    def controllers(self) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        for dev in sorted(glob.glob("/dev/input/js*")):
            found.append({"device": dev, "name": self._js_name(dev)})
        for link in sorted(glob.glob("/dev/input/by-id/*-event-joystick")):
            name = os.path.basename(link).replace("-event-joystick", "").replace("usb-", "").replace("_", " ")
            found.append({"device": link, "name": name})
        return found

    @staticmethod
    def _js_name(dev: str) -> str:
        num = dev.rsplit("js", 1)[-1]
        try:
            with open(f"/sys/class/input/js{num}/device/name", "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return os.path.basename(dev)

    # ------------------------------------------------------------------ compositor
    def compositor_running(self) -> bool:
        if not self.which("lindos-compositor"):
            v = self.xfconf_get("xfwm4", "/general/use_compositing")
            return (v or "").strip().lower() == "true"
        r = self.run(["lindos-compositor", "status"], timeout=5)
        return model.compositor_state_from_output(r.code, r.out + r.err)

    def set_compositor(self, on: bool) -> bool:
        if self.which("lindos-compositor"):
            # explicit user request: `start --force` also works in Lite mode (compositor "none"),
            # where a plain `start` is skipped by design (lindos-compositor honours the mode)
            argv = ["lindos-compositor", "start", "--force"] if on else ["lindos-compositor", "stop"]
            return self.run(argv, timeout=15).ok
        log.warning("lindos-compositor missing — toggling xfwm4 compositing instead")
        return self.xfconf_set("xfwm4", "/general/use_compositing", bool(on))

    # ------------------------------------------------------------------ compat / gaming
    def apps_db_load(self) -> list[dict[str, Any]]:
        db = self._call("compat", "apps_db_load", default=None)
        if db is None:
            path = self.path("APPS_DB", "~/.local/share/lindos/apps.json")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    db = json.load(fh)
            except FileNotFoundError:
                db = {}
            except (OSError, ValueError) as exc:
                log.warning("apps db unreadable: %s", exc)
                db = {}
        return model.normalize_apps_db(db)

    def recipes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(glob.glob(os.path.join(self.recipes_dir(), "*.json"))):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = model.parse_recipe(json.load(fh), os.path.splitext(os.path.basename(path))[0])
            except (OSError, ValueError) as exc:
                log.warning("recipe %s unreadable: %s", path, exc)
                continue
            if rec:
                rec["path"] = path
                out.append(rec)
        return out

    def compat_matrix(self) -> list[dict[str, str]]:
        path = _sys_root() + model.COMPAT_MATRIX_JSON
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return model.parse_compat_matrix(json.load(fh))
        except (OSError, ValueError) as exc:
            log.warning("compat matrix unreadable (%s): %s", path, exc)
            return []

    def flatpak_apps(self) -> set[str]:
        now = time.monotonic()
        if self._flatpak_cache and now - self._flatpak_cache[0] < 30:
            return self._flatpak_cache[1]
        ids: set[str] = set()
        if self.which("flatpak"):
            r = self.run(["flatpak", "list", "--app", "--columns=application"], timeout=20)
            if r.ok:
                ids = {ln.strip() for ln in r.out.splitlines() if ln.strip() and "." in ln}
        self._flatpak_cache = (now, ids)
        return ids

    def launcher_states(self) -> list[dict[str, Any]]:
        fl = self.flatpak_apps()
        states: list[dict[str, Any]] = []
        for launcher in model.LAUNCHERS:
            installed = model.launcher_installed(launcher, self.which, fl)
            states.append({"launcher": launcher, "installed": installed, "run": model.launcher_run_argv(launcher, self.which, fl) if installed else None})
        return states

    def install_gaming(self, items: Sequence[str]) -> HelperResult:
        self._flatpak_cache = None
        return self.run_privileged("install-gaming", {"items": [str(i) for i in items]})

    def install_compat(self, items: Sequence[str] = ("wine", "umu")) -> HelperResult:
        """Windows app support: helper `install-compat` → /usr/libexec/lindos/install-compat.sh <items>
        (lindos-compat).  Default items = what the OOBE installs (Wine + Proton via umu)."""
        return self.run_privileged("install-compat", {"items": [str(i) for i in items]})

    def compat_status(self) -> dict[str, bool]:
        """Which Windows-app pieces are on PATH (cheap; the full check is `lindos-compat doctor`)."""
        return {
            "wine": bool(self.which("wine")),
            "umu": bool(self.which("umu-run")),
            "winetricks": bool(self.which("winetricks")),
            "lindos_run": bool(self.which("lindos-run")),
        }

    # ------------------------------------------------------------------ Windows file types / terminal .exe / winget
    def formats(self) -> list[dict[str, Any]]:
        """``lindos-compat formats --json`` (SPEC-WINDOWS §28.2): every call is defensive — a
        missing binary, non-zero exit or bad JSON all degrade to an empty list, never an
        exception, so the page can show a friendly "not available" message instead."""
        if not self.which("lindos-compat"):
            return []
        r = self.run(["lindos-compat", "formats", "--json"], timeout=20)
        if not r.ok or not r.out.strip():
            return []
        try:
            return model.parse_formats_table(json.loads(r.out))
        except ValueError as exc:
            log.warning("lindos-compat formats --json: bad output (%s)", exc)
            return []

    def binfmt_status(self) -> dict[str, Any]:
        """``lindos-compat binfmt status --json`` (SPEC-WINDOWS §28.7)."""
        if not self.which("lindos-compat"):
            return model.normalize_binfmt_status({"note": "lindos-compat is not installed"})
        r = self.run(["lindos-compat", "binfmt", "status", "--json"], timeout=15)
        if not r.ok or not r.out.strip():
            return model.normalize_binfmt_status({"note": (r.err or r.out).strip() or "could not read the status"})
        try:
            return model.normalize_binfmt_status(json.loads(r.out))
        except ValueError as exc:
            log.warning("lindos-compat binfmt status --json: bad output (%s)", exc)
            return model.normalize_binfmt_status({"note": "lindos-compat returned data Settings could not read"})

    def set_binfmt(self, enabled: bool) -> HelperResult:
        """``lindos-compat binfmt enable|disable --json`` -- the CLI itself talks to the
        privileged helper (action ``set-binfmt``) and may show its own polkit prompt; conflicts
        with any other registered handler are reported honestly, never silently overridden."""
        if not self.which("lindos-compat"):
            return HelperResult(False, "", "lindos-compat is not installed", 127)
        action = "enable" if enabled else "disable"
        r = self.run(["lindos-compat", "binfmt", action, "--json"], timeout=90)
        message = ""
        try:
            data = json.loads(r.out) if r.out.strip() else {}
            if isinstance(data, dict):
                message = str(data.get("message") or "")
        except ValueError:
            pass
        return HelperResult(r.ok, message or r.out, r.err, r.code)

    def winget_search(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        """``lindos-compat winget search <query> --json`` (SPEC-WINDOWS §28.10)."""
        if not query.strip() or not self.which("lindos-compat"):
            return []
        r = self.run(["lindos-compat", "winget", "search", query, "--limit", str(limit), "--json"], timeout=30)
        if not r.ok or not r.out.strip():
            return []
        try:
            return model.parse_winget_results(json.loads(r.out))
        except ValueError as exc:
            log.warning("lindos-compat winget search --json: bad output (%s)", exc)
            return []

    @staticmethod
    def winget_install_argv(package_id: str) -> list[str]:
        """argv for an :class:`OutputDialog` (SPEC-WINDOWS §32: "install button -> ... in a
        terminal or with progress"). ``--accept-package-agreements`` is required because a
        streamed log view cannot answer the CLI's interactive yes/no license prompt; the
        agreement text is still shown in the streamed output before the download starts."""
        return ["lindos-compat", "winget", "install", package_id, "--accept-package-agreements"]

    def transfer_gui_available(self) -> bool:
        return bool(self.which("lindos-transfer-gui"))

    def launch_transfer_gui(self) -> bool:
        return self.spawn(["lindos-transfer-gui"])

    # ------------------------------------------------------------------ play-anywhere (SPEC-WINDOWS §30)
    def not_possible_games(self) -> list[dict[str, str]]:
        """Titles from the compat matrix that need Windows (kernel anti-cheat with no Linux
        build) -- the "Games that need Windows" card."""
        return [g for g in self.compat_matrix() if g.get("status") == "not-possible"]

    def game_route(self, title: str) -> dict[str, Any]:
        """``lindos-game route <title> --json`` (SPEC-WINDOWS §30.2), fully defensive."""
        title = (title or "").strip()
        if not title:
            return model.normalize_game_route({})
        if not self.which("lindos-game"):
            return model.normalize_game_route(
                {"title": title, "notes": ["lindos-game (lindos-gaming) is not installed"]})
        r = self.run(["lindos-game", "route", title, "--json"], timeout=20)
        if not r.ok or not r.out.strip():
            return model.normalize_game_route(
                {"title": title, "notes": [(r.err or r.out).strip() or "could not look up routes for this game"]})
        try:
            return model.normalize_game_route(json.loads(r.out))
        except ValueError as exc:
            log.warning("lindos-game route --json: bad output (%s)", exc)
            return model.normalize_game_route(
                {"title": title, "notes": ["lindos-game returned data Settings could not read"]})

    def region(self) -> str:
        return str(self.config_get("region", "") or "")

    def set_region(self, region: str) -> bool:
        return self.config_set("region", region)

    def install_geforce_now(self) -> HelperResult:
        """``lindos-game cloud install geforce-now`` -- the official NVIDIA Flatpak through the
        helper's ``install-flatpaks`` action (SPEC-WINDOWS §30.2/§30.4); can take a while."""
        if not self.which("lindos-game"):
            return HelperResult(False, "", "lindos-game (lindos-gaming) is not installed", 127)
        r = self.run(["lindos-game", "cloud", "install", "geforce-now"], timeout=600)
        return HelperResult(r.ok, r.out, r.err, r.code)

    def dualboot_status(self) -> dict[str, Any]:
        """``lindos-dualboot status --json`` (SPEC-WINDOWS §30.3)."""
        if not self.which("lindos-dualboot"):
            return model.normalize_dualboot_status({"why": "lindos-dualboot (lindos-core) is not installed"})
        r = self.run(["lindos-dualboot", "status", "--json"], timeout=20)
        if not r.ok or not r.out.strip():
            return model.normalize_dualboot_status(
                {"why": (r.err or r.out).strip() or "could not read the dual-boot status"})
        try:
            return model.normalize_dualboot_status(json.loads(r.out))
        except ValueError as exc:
            log.warning("lindos-dualboot status --json: bad output (%s)", exc)
            return model.normalize_dualboot_status({"why": "lindos-dualboot returned data Settings could not read"})

    def reboot_to_windows(self, entry: str = "") -> HelperResult:
        """``lindos-dualboot reboot-to-windows --yes [--entry X]`` -- the CLI re-validates the
        chosen entry is really Windows Boot Manager (as root) before touching anything."""
        if not self.which("lindos-dualboot"):
            return HelperResult(False, "", "lindos-dualboot (lindos-core) is not installed", 127)
        argv = ["lindos-dualboot", "reboot-to-windows", "--yes"]
        if entry:
            argv += ["--entry", entry]
        r = self.run(argv, timeout=30)
        return HelperResult(r.ok, r.out, r.err, r.code)

    # ------------------------------------------------------------------ Lindos updates (SPEC-UPDATE §36/§37)
    def _update_cli_json(self, argv: Sequence[str], tool: str, timeout: float, normalize: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Run ``argv`` (a read-only ``--json`` query) and hand the parsed/failed result to
        *normalize* -- defensive against a missing binary, non-zero exit or bad JSON, exactly
        like :meth:`formats`/:meth:`binfmt_status` above: never an exception, always an honest
        dict the page can render."""
        if not self.which(str(argv[0])):
            return normalize({"_missing": True})
        r = self.run(list(argv), timeout=timeout)
        if not r.ok or not r.out.strip():
            return normalize({"_error": (r.err or r.out).strip() or f"{tool} exited {r.code}"})
        try:
            return normalize(json.loads(r.out))
        except ValueError as exc:
            log.warning("%s: bad output (%s)", " ".join(argv), exc)
            return normalize({"_error": f"{tool} returned data Settings could not read"})

    def update_check(self) -> dict[str, Any]:
        """``lindos-update check --json`` (SPEC-UPDATE §36.1/§36.2): every ``lindos-*``
        package's installed/available version, the booted/available kernel, and whether an apt
        repo is configured. Read-only, no root, safe to call as often as the UI wants."""
        return self._update_cli_json(["lindos-update", "check", "--json"], "lindos-update", 30, model.normalize_update_status)

    def update_kernel_status(self) -> dict[str, Any]:
        """``lindos-update kernel-status --json`` (SPEC-UPDATE §36.1) -- booted vs. installed vs.
        available kernel version for the Kernel card."""
        return self._update_cli_json(["lindos-update", "kernel-status", "--json"], "lindos-update", 20, model.normalize_kernel_status)

    def update_repo_status(self) -> dict[str, Any]:
        """``lindos-update repo status --json`` (SPEC-UPDATE §36.1) -- is ``LINDOS_APT_REPO_URL``
        configured and reachable."""
        return self._update_cli_json(["lindos-update", "repo", "status", "--json"], "lindos-update", 20, model.normalize_repo_status)

    def secureboot_status(self) -> dict[str, Any]:
        """``lindos-kernel secureboot status --json`` (SPEC-WINDOWS §31.3, shipped by
        lindos-kernel) -- Secure-Boot-signed indicator for the Kernel card."""
        return self._update_cli_json(["lindos-kernel", "secureboot", "status", "--json"], "lindos-kernel", 20, model.normalize_secureboot_status)

    def update_refresh(self) -> HelperResult:
        """"Check now": privileged helper action ``apt-get-update`` (SPEC-UPDATE §36.4) refreshes
        the apt cache; the caller re-runs :meth:`update_check` afterwards to read the new state.
        Same "run privileged" mechanism as :meth:`install_packages`/:meth:`install_drivers`."""
        return self.run_privileged("apt-get-update", {})

    def update_apply(self, packages: Sequence[str], allow_kernel: bool = False) -> HelperResult:
        """"Update now" / the kernel's "Apply now": privileged helper action ``system-upgrade``
        (SPEC-UPDATE §36.4) with an explicit, exact ``name=version`` list the caller computed
        from :meth:`update_check`'s own output (:func:`lindos_settings.model.lindos_update_payload`
        / :func:`~lindos_settings.model.kernel_update_payload`) -- never a bare apt-get upgrade."""
        payload: dict[str, Any] = {"packages": [str(p) for p in packages]}
        if allow_kernel:
            payload["allow_kernel"] = True
        return self.run_privileged("system-upgrade", payload)

    @staticmethod
    def sideload_argv(directory: str) -> list[str]:
        """``lindos-update sideload <DIR> --yes`` (SPEC-UPDATE §36.3) -- the CLI itself inspects
        every ``lindos-*.deb`` in *directory* and talks to the privileged helper
        (``install-local-debs``); ``--yes`` is required because the streamed
        :class:`~lindos_settings.widgets.OutputDialog` runs with stdin closed and cannot answer
        an interactive downgrade prompt."""
        return ["lindos-update", "sideload", str(directory), "--yes"]

    # ------------------------------------------------------------------ system info (about)
    @staticmethod
    def os_release() -> dict[str, str]:
        data: dict[str, str] = {}
        for path in (_sys_root() + "/etc/os-release", "/usr/lib/os-release"):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            data[k] = v.strip().strip('"')
                break
            except OSError:
                continue
        return data

    @staticmethod
    def lindos_release() -> str:
        try:
            with open(_sys_root() + "/etc/lindos-release", "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            osr = Backend.os_release()
            if osr.get("LINDOS_VERSION"):
                return f"Lindos {osr['LINDOS_VERSION']} ({osr.get('LINDOS_CODENAME', 'Aurora')})"
            return "Lindos (version file missing)"

    @staticmethod
    def kernel() -> str:
        try:
            u = os.uname()  # type: ignore[attr-defined]
            return f"{u.sysname} {u.release}"
        except AttributeError:
            return platform.platform()

    def xfce_version(self) -> str:
        r = self.run(["xfce4-session", "--version"], timeout=5)
        for line in (r.out + r.err).splitlines():
            if "xfce4-session" in line.lower():
                return line.strip()
        r = self.run(["xfce4-about", "--version"], timeout=5)
        first = (r.out + r.err).strip().splitlines()
        return first[0].strip() if first else "XFCE (version unknown)"

    @staticmethod
    def hostname() -> str:
        try:
            return socket.gethostname()
        except OSError:
            return "lindos"

    @staticmethod
    def uptime_seconds() -> float:
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            return 0.0

    @staticmethod
    def disk_usage(path: str = "/") -> Optional[tuple[int, int, int]]:
        try:
            du = shutil.disk_usage(path)
            return du.total, du.used, du.free
        except OSError:
            return None

    @staticmethod
    def username() -> str:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    @staticmethod
    def user_display_name() -> str:
        user = Backend.username()
        try:
            import pwd  # Linux only

            gecos = pwd.getpwnam(user).pw_gecos
        except (ImportError, KeyError, OSError):
            gecos = ""
        return model.display_name(user, gecos)

    @staticmethod
    def user_avatar_path() -> Optional[str]:
        for cand in (os.path.join(_home(), ".face"), os.path.join(_home(), ".face.icon")):
            if os.path.isfile(cand):
                return cand
        return None

    def install_date(self) -> str:
        for path in (_sys_root() + "/etc/lindos-release", _sys_root() + "/etc/machine-id", "/var/log/installer"):
            try:
                ts = os.stat(path).st_mtime
                return time.strftime("%Y-%m-%d", time.localtime(ts))
            except OSError:
                continue
        return "—"

    def desktop_session(self) -> str:
        return os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or ""

    # ------------------------------------------------------------------ misc
    def open_c_drive(self, prefix: str) -> bool:
        p = os.path.join(model.prefix_path(prefix, self.prefixes_dir()), "drive_c")
        if not os.path.isdir(p):
            log.warning("drive_c not found: %s", p)
            return False
        return self.open_path(p)

    def winecfg(self, slug: str, prefix: str) -> bool:
        if self.which("lindos-compat"):
            return self.spawn(["lindos-compat", "prefixes", "winecfg", slug])
        wp = model.prefix_path(prefix, self.prefixes_dir())
        if self.which("winecfg"):
            return self.spawn(["winecfg"], env={"WINEPREFIX": wp, "WINEDEBUG": "-all"})
        if self.which("wine"):
            return self.spawn(["wine", "winecfg"], env={"WINEPREFIX": wp, "WINEDEBUG": "-all"})
        return False

    def run_windows_app(self, exe: str, slug: str = "") -> bool:
        argv = ["lindos-run"]
        if slug:
            argv += ["--prefix", slug]
        argv.append(exe)
        return self.spawn(argv)


_BACKEND: Optional[Backend] = None


def get_backend() -> Backend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = Backend()
    return _BACKEND


__all__ = ["Backend", "CmdResult", "HelperResult", "ApplyResult", "get_backend", "CONFIG_DEFAULTS"]
