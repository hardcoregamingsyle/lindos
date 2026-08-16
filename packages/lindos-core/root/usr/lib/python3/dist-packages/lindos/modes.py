"""Lindos modes (SPEC §3, §4.3): Everyday / Gaming / Work / Creator / Lite.

Mode definitions live in ``/usr/share/lindos/modes/<id>/mode.json`` (+ optional
``panel.tar.bz2`` or ``panel/`` dir, ``apply-user.sh``, ``apply-system.sh``).

:func:`apply_mode` performs the *user* part (config, panel profile, docklike pins,
compositor, apply-user.sh) and delegates the *privileged* part to the polkit helper action
``apply-mode`` with the plan produced by :func:`build_system_plan`.  It never raises for a
missing tool: every step is recorded in :class:`ApplyResult` as ``(name, ok, message)`` and
skipped steps carry a message starting with ``"skipped:"``.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as lconfig
from . import helper as lhelper
from . import paths

log = logging.getLogger("lindos.modes")

MODE_IDS: List[str] = ["everyday", "gaming", "work", "creator", "lite"]
GOVERNORS = ("schedutil", "performance", "powersave")
COMPOSITORS = ("picom", "xfwm", "none")
PLAN_SCHEMA = 1

LogFn = Callable[[str], None]


# --- data model --------------------------------------------------------------------------
@dataclass
class Mode:
    """One entry of ``/usr/share/lindos/modes/<id>/mode.json``."""

    id: str
    name: str
    description: str = ""
    icon: str = ""
    packages: List[str] = field(default_factory=list)
    flatpaks: List[str] = field(default_factory=list)
    services_disable: List[str] = field(default_factory=list)
    services_enable: List[str] = field(default_factory=list)
    sysctl: Dict[str, str] = field(default_factory=dict)
    governor: str = "schedutil"
    pins: List[str] = field(default_factory=list)
    compositor: str = "picom"
    zram_percent: int = 50
    path: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)  # non-SPEC keys (ram_hint, tagline …)

    _LIST_KEYS = ("packages", "flatpaks", "services_disable", "services_enable", "pins")
    _KNOWN = ("id", "name", "description", "icon", "packages", "flatpaks", "services_disable",
              "services_enable", "sysctl", "governor", "pins", "compositor", "zram_percent")

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> "Mode":
        """Build a Mode from a decoded ``mode.json`` object (validating types)."""
        if not isinstance(data, dict):
            raise ValueError("mode.json must contain a JSON object")
        mode_id = data.get("id")
        if not isinstance(mode_id, str) or not re.match(r"^[a-z][a-z0-9-]*$", mode_id):
            raise ValueError(f"invalid mode id {mode_id!r}")
        kwargs: Dict[str, Any] = {"id": mode_id, "name": str(data.get("name") or mode_id.title())}
        kwargs["description"] = str(data.get("description") or "")
        kwargs["icon"] = str(data.get("icon") or "")
        for key in cls._LIST_KEYS:
            value = data.get(key, [])
            if value is None:
                value = []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"'{key}' must be a list of strings in {path or mode_id}")
            kwargs[key] = [v.strip() for v in value if v.strip()]
        sysctl = data.get("sysctl", {}) or {}
        if not isinstance(sysctl, dict):
            raise ValueError(f"'sysctl' must be an object in {path or mode_id}")
        kwargs["sysctl"] = {str(k): ("1" if v is True else "0" if v is False else str(v)) for k, v in sysctl.items()}
        governor = str(data.get("governor") or "schedutil")
        if governor not in GOVERNORS:
            raise ValueError(f"'governor' must be one of {GOVERNORS} in {path or mode_id}")
        kwargs["governor"] = governor
        compositor = str(data.get("compositor") or "picom")
        if compositor not in COMPOSITORS:
            raise ValueError(f"'compositor' must be one of {COMPOSITORS} in {path or mode_id}")
        kwargs["compositor"] = compositor
        zram = data.get("zram_percent", 50)
        if isinstance(zram, bool) or not isinstance(zram, int) or not 0 <= zram <= 200:
            raise ValueError(f"'zram_percent' must be an integer 0..200 in {path or mode_id}")
        kwargs["zram_percent"] = zram
        kwargs["path"] = path
        kwargs["extra"] = {k: v for k, v in data.items() if k not in cls._KNOWN}
        return cls(**kwargs)

    @classmethod
    def from_file(cls, mode_json: str) -> "Mode":
        with open(mode_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data, path=os.path.dirname(os.path.abspath(mode_json)))

    def to_dict(self) -> Dict[str, Any]:
        """JSON-able dict (SPEC keys + ``path`` + extras)."""
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        data.update({k: v for k, v in extra.items() if k not in data})
        return data

    # -- optional companion files ---------------------------------------------------------
    def _companion(self, *parts: str) -> Optional[str]:
        if not self.path:
            return None
        candidate = os.path.join(self.path, *parts)
        return candidate if os.path.isfile(candidate) else None

    def panel_profile(self) -> Optional[str]:
        """``panel.tar.bz2`` (xfce4-panel-profiles tarball) if shipped."""
        return self._companion("panel.tar.bz2")

    def panel_xml(self) -> Optional[str]:
        """``panel/xfce4-panel.xml`` fallback if shipped."""
        return self._companion("panel", "xfce4-panel.xml")

    def apply_user_script(self) -> Optional[str]:
        return self._companion("apply-user.sh")

    def apply_system_script(self) -> Optional[str]:
        return self._companion("apply-system.sh")

    @property
    def ram_hint(self) -> str:
        return str(self.extra.get("ram_hint", ""))

    @property
    def tagline(self) -> str:
        return str(self.extra.get("tagline", ""))


@dataclass
class ApplyResult:
    """Outcome of :func:`apply_mode`.

    ``steps`` holds ``(name, ok, message)`` for every step attempted.  A step that could not
    run because a tool/file is missing is recorded with ``ok=False`` and a message starting
    with ``"skipped:"``; such steps do **not** flip the overall ``ok`` flag.  Any other failed
    step does.
    """

    ok: bool = True
    steps: List[Tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, message: str = "") -> None:
        self.steps.append((name, bool(ok), message))
        if not ok and not message.startswith("skipped"):
            self.ok = False

    @property
    def failed(self) -> List[Tuple[str, bool, str]]:
        return [s for s in self.steps if not s[1] and not s[2].startswith("skipped")]

    @property
    def skipped(self) -> List[Tuple[str, bool, str]]:
        return [s for s in self.steps if not s[1] and s[2].startswith("skipped")]

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "steps": [{"name": n, "ok": o, "message": m} for n, o, m in self.steps]}

    def summary(self) -> str:
        lines = []
        for name, ok, message in self.steps:
            mark = "ok  " if ok else ("skip" if message.startswith("skipped") else "FAIL")
            lines.append(f"[{mark}] {name}: {message}" if message else f"[{mark}] {name}")
        lines.append("result: " + ("ok" if self.ok else "failed"))
        return "\n".join(lines)


# --- loading -----------------------------------------------------------------------------
def load_modes(modes_dir: Optional[str] = None) -> Dict[str, Mode]:
    """Load every ``<modes_dir>/<id>/mode.json``.  Invalid entries are logged and skipped."""
    base = modes_dir or paths.modes_dir()
    result: Dict[str, Mode] = {}
    try:
        entries = sorted(os.listdir(base))
    except OSError as exc:
        log.warning("modes directory %s unavailable: %s", base, exc)
        return result
    for entry in entries:
        mode_json = os.path.join(base, entry, "mode.json")
        if not os.path.isfile(mode_json):
            continue
        try:
            mode = Mode.from_file(mode_json)
        except (OSError, ValueError) as exc:
            log.warning("ignoring %s: %s", mode_json, exc)
            continue
        if mode.id != entry:
            log.warning("%s: id %r does not match directory name; using directory name", mode_json, mode.id)
            mode.id = entry
        result[mode.id] = mode
    ordered: Dict[str, Mode] = {}
    for mid in MODE_IDS:
        if mid in result:
            ordered[mid] = result[mid]
    for mid, mode in result.items():
        ordered.setdefault(mid, mode)
    return ordered


def get_mode(mode_id: str, modes_dir: Optional[str] = None) -> Mode:
    """Return the Mode for *mode_id* or raise ``KeyError``."""
    modes = load_modes(modes_dir)
    if mode_id not in modes:
        raise KeyError(f"unknown mode {mode_id!r} (available: {', '.join(modes) or 'none'})")
    return modes[mode_id]


def current_mode() -> str:
    """The effective mode id (user override → system default → everyday)."""
    return lconfig.effective_mode()


def build_system_plan(mode: Mode, *, set_system_default: bool = False, offline: bool = False) -> Dict[str, Any]:
    """JSON-able plan for the helper action ``apply-mode``."""
    return {
        "schema": PLAN_SCHEMA,
        "mode": mode.id,
        "packages": list(mode.packages),
        "flatpaks": list(mode.flatpaks),
        "services_disable": list(mode.services_disable),
        "services_enable": list(mode.services_enable),
        "sysctl": dict(mode.sysctl),
        "governor": mode.governor,
        "zram_percent": int(mode.zram_percent),
        "compositor": mode.compositor,
        "apply_system": mode.apply_system_script(),
        "set_system_default": bool(set_system_default),
        "offline": bool(offline),
    }


# --- process helpers ---------------------------------------------------------------------
def _run(cmd: List[str], timeout: float = 120, env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Run *cmd*; return ``(ok, combined output)``.  Never raises."""
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False)
    except FileNotFoundError:
        return False, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]}: timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return False, f"{cmd[0]}: {exc}"
    text = (proc.stdout or "").strip()
    return proc.returncode == 0, text


def _spawn(cmd: List[str]) -> bool:
    """Start *cmd* detached (used to relaunch xfce4-panel).  Never raises."""
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, ValueError):
        return False


def _xfconf(args: List[str]) -> Tuple[bool, str]:
    tool = shutil.which("xfconf-query")
    if not tool:
        return False, "xfconf-query: not found"
    return _run([tool] + args, timeout=20)


def _tail(text: str, n: int = 3) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else ""


# --- user-side steps -----------------------------------------------------------------------
def _step_config(mode: Mode, result: ApplyResult, dry_run: bool, log_fn: LogFn) -> None:
    cfg = lconfig.Config.load()
    changes: Dict[str, Any] = {"mode": mode.id}
    if mode.id == "gaming":
        changes["gamemode_auto"] = True
    if dry_run:
        result.add("write-user-config", True, f"would set {changes} in {cfg.path}")
        return
    try:
        cfg.update(changes)
        cfg.save()
    except OSError as exc:
        result.add("write-user-config", False, f"cannot write {cfg.path}: {exc}")
        return
    log_fn(f"config: mode={mode.id} written to {cfg.path}")
    result.add("write-user-config", True, f"mode={mode.id} → {cfg.path}")


def _docklike_plugin_ids() -> List[int]:
    ok, out = _xfconf(["-c", "xfce4-panel", "-l", "-v"])
    if not ok:
        return []
    ids: List[int] = []
    for line in out.splitlines():
        m = re.match(r"^/plugins/plugin-(\d+)\s+docklike\s*$", line.strip())
        if m:
            ids.append(int(m.group(1)))
    return ids


def _write_docklike_pins(plugin_id: int, pins: List[str]) -> str:
    rc_path = os.path.join(paths.panel_dir(), f"docklike-{plugin_id}.rc")
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    if os.path.isfile(rc_path):
        try:
            parser.read(rc_path, encoding="utf-8")
        except configparser.Error:
            parser = configparser.RawConfigParser()
            parser.optionxform = str  # type: ignore[assignment]
    if not parser.has_section("user"):
        parser.add_section("user")
    parser.set("user", "pinned", ";".join(pins) + (";" if pins else ""))
    os.makedirs(os.path.dirname(rc_path), exist_ok=True)
    with open(rc_path, "w", encoding="utf-8") as fh:
        parser.write(fh, space_around_delimiters=False)
    return rc_path


def _step_panel(mode: Mode, result: ApplyResult, dry_run: bool, log_fn: LogFn) -> None:
    tarball = mode.panel_profile()
    xml = mode.panel_xml()
    tool = shutil.which("xfce4-panel-profiles")
    tarball_failed = False
    if tarball and tool:
        if dry_run:
            result.add("panel-profile", True, f"would run: xfce4-panel-profiles load {tarball}")
            return
        ok, out = _run([tool, "load", tarball], timeout=90)
        if ok:
            log_fn(f"panel: loaded profile {tarball}")
            result.add("panel-profile", True, _tail(out) or f"loaded {tarball}")
            return
        tarball_failed = True
        log_fn(f"panel: xfce4-panel-profiles failed ({_tail(out)}); trying fallback")
        if not xml:
            result.add("panel-profile", False, f"xfce4-panel-profiles load failed: {_tail(out)}")
    elif tarball and not tool and not xml:
        result.add("panel-profile", False, "skipped: xfce4-panel-profiles not installed (panel.tar.bz2 present)")
        return
    if xml:
        dest_dir = paths.xfconf_dir()
        panel_src = os.path.dirname(xml)
        if dry_run:
            result.add("panel-profile", True, f"would copy {xml} → {dest_dir}/xfce4-panel.xml and restart the panel")
        else:
            panel_bin = shutil.which("xfce4-panel")
            pkill = shutil.which("pkill")
            if panel_bin:
                _run([panel_bin, "--quit"], timeout=15)
            if pkill:
                _run([pkill, "-x", "xfconfd"], timeout=10)
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copyfile(xml, os.path.join(dest_dir, "xfce4-panel.xml"))
                pdir = paths.panel_dir()
                os.makedirs(pdir, exist_ok=True)
                for entry in os.listdir(panel_src):
                    src = os.path.join(panel_src, entry)
                    if entry == "xfce4-panel.xml":
                        continue
                    dst = os.path.join(pdir, entry)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copyfile(src, dst)
            except OSError as exc:
                result.add("panel-profile", False, f"copy failed: {exc}")
                if panel_bin:
                    _spawn([panel_bin])
                return
            msg = f"copied {xml} → {dest_dir}"
            if panel_bin:
                _spawn([panel_bin])
                msg += " (panel restarted)"
            else:
                msg += " (xfce4-panel not found; not restarted)"
            log_fn(f"panel: {msg}")
            result.add("panel-profile", True, msg)
    elif not tarball_failed:
        result.add("panel-profile", False, f"skipped: no panel profile shipped for mode '{mode.id}'")

    # pins (docklike) — only needed when no tarball profile could be loaded
    if not mode.pins:
        return
    if dry_run:
        result.add("pins", True, f"would pin {len(mode.pins)} launchers in docklike")
        return
    if not shutil.which("xfconf-query"):
        result.add("pins", False, "skipped: xfconf-query not found")
        return
    ids = _docklike_plugin_ids()
    if not ids:
        result.add("pins", False, "skipped: no docklike plugin found in xfce4-panel")
        return
    try:
        rc = _write_docklike_pins(ids[0], mode.pins)
    except OSError as exc:
        result.add("pins", False, f"cannot write docklike rc: {exc}")
        return
    panel_bin = shutil.which("xfce4-panel")
    if panel_bin and not xml:  # xml path already restarted the panel
        _run([panel_bin, "-r"], timeout=15)
    result.add("pins", True, f"{len(mode.pins)} pins → {rc}")


def _step_compositor(mode: Mode, result: ApplyResult, dry_run: bool, log_fn: LogFn) -> None:
    want_picom = mode.compositor == "picom"
    want_xfwm = mode.compositor == "xfwm"
    action = "start" if want_picom else "stop"
    if dry_run:
        result.add("compositor", True, f"would run: lindos-compositor {action}"
                   + ("" if want_picom else f"; xfwm4 use_compositing={'true' if want_xfwm else 'false'}"))
        return
    notes: List[str] = []
    if not want_picom:
        ok, out = _xfconf(["-c", "xfwm4", "-p", "/general/use_compositing", "-n", "-t", "bool", "-s",
                           "true" if want_xfwm else "false"])
        notes.append(f"xfwm4 compositing {'on' if want_xfwm else 'off'}" if ok else f"xfconf: {_tail(out)}")
    tool = shutil.which("lindos-compositor")
    if not tool:
        result.add("compositor", False, "skipped: lindos-compositor not found" + (f" ({'; '.join(notes)})" if notes else ""))
        return
    ok, out = _run([tool, action], timeout=30)
    notes.insert(0, f"lindos-compositor {action}" + ("" if ok else f" failed: {_tail(out)}"))
    log_fn("compositor: " + "; ".join(notes))
    result.add("compositor", ok, "; ".join(notes))


def _step_user_script(mode: Mode, result: ApplyResult, dry_run: bool, log_fn: LogFn) -> None:
    script = mode.apply_user_script()
    if not script:
        result.add("apply-user-script", True, "none shipped")
        return
    if dry_run:
        result.add("apply-user-script", True, f"would run: bash {script}")
        return
    bash = shutil.which("bash")
    if not bash:
        result.add("apply-user-script", False, "skipped: bash not found")
        return
    env = dict(os.environ)
    env["LINDOS_MODE"] = mode.id
    env["HOME"] = paths.home()          # honours LINDOS_HOME (tests / staging)
    ok, out = _run([bash, script], timeout=300, env=env)
    log_fn(f"apply-user.sh: {'ok' if ok else 'failed'} {_tail(out)}")
    result.add("apply-user-script", ok, _tail(out) or ("ok" if ok else "failed"))


def _step_system(mode: Mode, result: ApplyResult, dry_run: bool, system: bool, log_fn: LogFn) -> None:
    plan = build_system_plan(mode, set_system_default=system)
    if dry_run:
        result.add("system", True, "would call helper apply-mode with plan: " + json.dumps(plan, sort_keys=True))
        return
    res = lhelper.run_privileged("apply-mode", plan, log=log_fn)
    if res.ok:
        result.add("system", True, res.message)
    elif res.code == 127:
        result.add("system", False, f"skipped: {res.message}")
    else:
        result.add("system", False, res.message)


# --- public entry point ------------------------------------------------------------------
def apply_mode(mode_id: str, *, system: bool = False, dry_run: bool = False,
               log: LogFn = print) -> ApplyResult:
    """Switch to *mode_id*.

    User side (always): write ``~/.config/lindos/config.json``, load the panel profile
    (``xfce4-panel-profiles load`` or the xml fallback), pin launchers, start/stop the
    compositor, run ``apply-user.sh``.  Privileged side: helper ``apply-mode`` with
    :func:`build_system_plan` (skipped when ``dry_run``).  ``system=True`` additionally makes
    the helper write ``/etc/lindos/system.json``.

    Never raises for missing tools; see :class:`ApplyResult`.
    """
    result = ApplyResult()
    log_fn: LogFn = log if callable(log) else (lambda _msg: None)
    try:
        mode = get_mode(mode_id)
    except KeyError as exc:
        result.add("load-mode", False, str(exc))
        return result
    result.add("load-mode", True, f"{mode.name} ({mode.path})")
    log_fn(f"applying mode '{mode.id}'{' (dry run)' if dry_run else ''}")

    for step in (_step_config, _step_panel, _step_compositor, _step_user_script):
        try:
            step(mode, result, dry_run, log_fn)
        except Exception as exc:  # defensive: a step must never abort the switch
            log.exception("step %s crashed", getattr(step, "__name__", step))
            result.add(getattr(step, "__name__", "step").replace("_step_", ""), False, f"error: {exc}")
    try:
        _step_system(mode, result, dry_run, system, log_fn)
    except Exception as exc:
        log.exception("system step crashed")
        result.add("system", False, f"error: {exc}")
    log_fn("done: " + ("ok" if result.ok else "finished with errors"))
    return result


__all__ = [
    "MODE_IDS", "GOVERNORS", "COMPOSITORS", "PLAN_SCHEMA", "Mode", "ApplyResult",
    "load_modes", "get_mode", "current_mode", "build_system_plan", "apply_mode",
]
