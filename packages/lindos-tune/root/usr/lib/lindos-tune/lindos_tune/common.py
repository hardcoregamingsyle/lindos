"""Shared plumbing for :mod:`lindos_tune`.

* :func:`path` — resolve an absolute system path under ``$LINDOS_ROOT`` (tests, chroot staging).
* :func:`run` / :func:`which` — subprocess helpers that never raise and never use ``shell=True``.
* :func:`in_chroot` / :func:`systemd_running` — decide whether ``systemctl start/restart`` and
  live kernel writes are allowed.
* :class:`Step` / :class:`Report` — the ``(name, ok, detail)`` records every subcommand returns.
* :class:`Context` — everything a step needs (root, dry-run, offline, runner, ``which``…);
  tests build one with an injected runner so no real command is ever executed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("lindos.tune")

ROOT_ENV = "LINDOS_ROOT"
CHROOT_ENV = "LINDOS_CHROOT"

# --- files owned / written by lindos-tune ------------------------------------------------------
SYSCTL_BASE_CONF = "/etc/sysctl.d/70-lindos-base.conf"
SYSCTL_MODE_CONF = "/etc/sysctl.d/90-lindos-mode.conf"       # same path as lindos.paths.SYSCTL_MODE_CONF
ZRAM_GENERATOR_CONF = "/etc/systemd/zram-generator.conf"
ZRAMSWAP_DEFAULT = "/etc/default/zramswap"
EARLYOOM_DEFAULT = "/etc/default/earlyoom"
JOURNALD_DROPIN = "/etc/systemd/journald.conf.d/lindos.conf"
TMPFILES_CONF = "/etc/tmpfiles.d/lindos.conf"
TMPFILES_GOVERNOR = "/etc/tmpfiles.d/lindos-governor.conf"
PRESET_FILE = "/usr/lib/systemd/system-preset/90-lindos.preset"
TUNE_D_DIR = "/etc/lindos/tune.d"
TUNE_SHARE_DIR = "/usr/share/lindos/tune"
SERVICES_WHITELIST = "/usr/share/lindos/tune/services-whitelist.txt"
AUTOSTART_HIDE_LIST = "/usr/share/lindos/tune/autostart-hide.list"
RAM_BUDGET_JSON = "/usr/share/lindos/tune/ram-budget.json"
ANANICY_RULES_DIR = "/etc/ananicy.d/lindos"
XDG_AUTOSTART_DIR = "/etc/xdg/autostart"
STATE_DIR = "/var/lib/lindos-tune"
STATE_FILE = "/var/lib/lindos-tune/state.json"
SYSTEM_LOG_DIR = "/var/log/lindos"
TUNE_LOG = "/var/log/lindos/tune.log"
MODES_DIR = "/usr/share/lindos/modes"
SYSTEM_CONF = "/etc/lindos/system.json"
LINDOS_RELEASE = "/etc/lindos-release"
FSTAB = "/etc/fstab"
CPU_DIR = "/sys/devices/system/cpu"
INSTALL_NBFC_SCRIPT = "/usr/libexec/lindos/install-nbfc.sh"

MODE_IDS: Tuple[str, ...] = ("everyday", "gaming", "work", "creator", "lite")

# Idle RAM targets (SPEC §0.1 / §11) in MB, measured as ``free -m`` "used" after login.
TARGET_MIN_MB = 350
TARGET_MAX_MB = 500
LITE_TARGET_MIN_MB = 300
LITE_TARGET_MAX_MB = 380

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SCHED_EXT = 3   # 'lindos-tune sched set' on a kernel without CONFIG_SCHED_CLASS_EXT (SPEC-KERNEL §16)

UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]{1,128}$")


# --- paths --------------------------------------------------------------------------------------
def root() -> str:
    """The ``LINDOS_ROOT`` prefix ("" on a real system)."""
    return os.environ.get(ROOT_ENV, "") or ""


def path(system_path: str, base: Optional[str] = None) -> str:
    """Resolve absolute *system_path* under *base* (default: :func:`root`)."""
    prefix = root() if base is None else base
    if not prefix:
        return system_path
    _drive, tail = os.path.splitdrive(system_path)
    return os.path.normpath(os.path.join(prefix, tail.lstrip("/\\")))


def ensure_dir(directory: str, mode: int = 0o755) -> bool:
    try:
        os.makedirs(directory, mode=mode, exist_ok=True)
        return True
    except OSError as exc:
        log.debug("mkdir %s failed: %s", directory, exc)
        return False


# --- file helpers -------------------------------------------------------------------------------
def read_text(file_path: str) -> Optional[str]:
    """Return the file content or ``None`` when unreadable."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def read_first_line(file_path: str) -> str:
    text = read_text(file_path)
    return text.strip().splitlines()[0].strip() if text and text.strip() else ""


def read_json(file_path: str) -> Dict[str, Any]:
    text = read_text(file_path)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_text(file_path: str, content: str, mode: int = 0o644) -> bool:
    """Atomically write *content* (LF endings) to *file_path*, creating parents.  Never raises."""
    directory = os.path.dirname(file_path) or "."
    if not ensure_dir(directory):
        return False
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".lindos-tune.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, file_path)
        return True
    except OSError as exc:
        log.debug("write %s failed: %s", file_path, exc)
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False


def write_if_changed(file_path: str, content: str, mode: int = 0o644) -> Tuple[bool, bool]:
    """Write only when the content differs.  Returns ``(ok, changed)``."""
    current = read_text(file_path)
    if current == content:
        return True, False
    return write_text(file_path, content, mode), True


def write_json(file_path: str, data: Dict[str, Any], mode: int = 0o644) -> bool:
    return write_text(file_path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


_KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_kv(text: Optional[str]) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines (shell-style, quotes stripped, ``#`` comments ignored)."""
    out: Dict[str, str] = {}
    if not text:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def read_kv(file_path: str) -> Dict[str, str]:
    return parse_kv(read_text(file_path))


def sh_quote(value: str) -> str:
    """Quote *value* for a ``KEY=VALUE`` shell file (double quotes, escapes ``"``, ``$``, ``\\``, `` ` ``)."""
    if re.match(r"^[A-Za-z0-9_./:@%+,-]*$", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


# --- processes ----------------------------------------------------------------------------------
@dataclass
class CmdResult:
    """Outcome of :func:`run` (``out`` = stdout+stderr combined, stripped)."""

    ok: bool
    code: int
    out: str = ""

    def tail(self, n: int = 2) -> str:
        lines = [ln for ln in self.out.splitlines() if ln.strip()]
        return " | ".join(lines[-n:]) if lines else ""


Runner = Callable[[Sequence[str], float], CmdResult]


def which(name: str) -> Optional[str]:
    """``shutil.which`` that also looks in the sbin directories root usually has on PATH."""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "posix":
        for directory in ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/bin", "/bin"):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def run(cmd: Sequence[str], timeout: float = 60, env: Optional[Dict[str, str]] = None) -> CmdResult:
    """Run *cmd* (list, never a shell).  Never raises; ``code`` 127 = executable missing."""
    argv = list(cmd)
    if not argv:
        return CmdResult(False, 2, "empty command")
    exe = which(argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if not exe:
        return CmdResult(False, 127, f"{argv[0]}: not found")
    run_env = dict(os.environ)
    run_env.setdefault("LC_ALL", "C.UTF-8")
    if env:
        run_env.update(env)
    try:
        proc = subprocess.run([exe] + argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=run_env, check=False)
    except subprocess.TimeoutExpired:
        return CmdResult(False, 124, f"{argv[0]}: timed out after {timeout:g}s")
    except (OSError, ValueError) as exc:
        return CmdResult(False, 126, f"{argv[0]}: {exc}")
    return CmdResult(proc.returncode == 0, proc.returncode, (proc.stdout or "").strip())


def run_capture(cmd: Sequence[str], timeout: float = 60) -> Tuple[int, str, str]:
    """Like :func:`run` but keeps stdout and stderr apart (for JSON-emitting tools)."""
    argv = list(cmd)
    exe = which(argv[0]) if argv and not os.path.isabs(argv[0]) else (argv[0] if argv else None)
    if not exe:
        return 127, "", f"{argv[0] if argv else ''}: not found"
    env = dict(os.environ)
    env.setdefault("LC_ALL", "C.UTF-8")
    try:
        proc = subprocess.run([exe] + argv[1:], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out"
    except (OSError, ValueError) as exc:
        return 126, "", f"{argv[0]}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def is_linux() -> bool:
    return sys.platform.startswith("linux")


# --- chroot / systemd detection ------------------------------------------------------------------
def in_chroot() -> bool:
    """True when we are (probably) inside a chroot — e.g. the ISO build (SPEC §8).

    Order: ``LINDOS_CHROOT`` env → ``systemd-detect-virt --chroot`` → ``ischroot`` →
    ``/proc/1/root`` vs ``/`` comparison.  On non-Linux hosts the answer is ``True`` (nothing
    may be started there either).
    """
    flag = os.environ.get(CHROOT_ENV, "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    if not is_linux():
        return True
    tool = which("systemd-detect-virt")
    if tool:
        res = run([tool, "--chroot"], timeout=10)
        if res.code in (0, 1):
            return res.code == 0
    tool = which("ischroot")
    if tool:
        res = run([tool], timeout=10)
        if res.code in (0, 1):
            return res.code == 0
    try:
        st_root = os.stat("/")
        st_init = os.stat("/proc/1/root/.")
        return (st_root.st_dev, st_root.st_ino) != (st_init.st_dev, st_init.st_ino)
    except OSError:
        return False


def systemd_booted() -> bool:
    """True when ``/run/systemd/system`` exists (systemd manages this boot; may still be a chroot)."""
    return is_linux() and os.path.isdir("/run/systemd/system")


def systemd_running() -> bool:
    """True when systemd is PID 1 of *this* environment (booted with systemd and not a chroot)."""
    return systemd_booted() and not in_chroot()


# --- result types ---------------------------------------------------------------------------------
@dataclass
class Step:
    """One recorded action: ``ok`` = succeeded (or would succeed in dry-run); ``skipped`` = not
    applicable here (missing tool, chroot, offline) — skipped steps never fail a run."""

    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        return "ok" if self.ok else "FAIL"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "skipped": self.skipped}

    def to_tuple(self) -> Tuple[str, bool, str]:
        return (self.name, self.ok, ("skipped: " + self.detail) if self.skipped and not self.detail.startswith("skipped") else self.detail)


@dataclass
class Report:
    """Collected steps of one subcommand run."""

    title: str = ""
    steps: List[Step] = field(default_factory=list)
    dry_run: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> Step:
        step = Step(name, bool(ok), detail, skipped)
        self.steps.append(step)
        return step

    def skip(self, name: str, detail: str) -> Step:
        return self.add(name, False, detail, skipped=True)

    @property
    def ok(self) -> bool:
        return all(s.ok or s.skipped for s in self.steps)

    @property
    def failed(self) -> List[Step]:
        return [s for s in self.steps if not s.ok and not s.skipped]

    @property
    def skipped(self) -> List[Step]:
        return [s for s in self.steps if s.skipped]

    def to_dict(self) -> Dict[str, Any]:
        data = {"ok": self.ok, "dry_run": self.dry_run, "title": self.title,
                "steps": [s.to_dict() for s in self.steps]}
        data.update(self.extra)
        return data

    def render(self) -> str:
        lines: List[str] = []
        if self.title:
            lines.append(self.title + (" (dry run)" if self.dry_run else ""))
        width = max((len(s.name) for s in self.steps), default=4)
        for step in self.steps:
            lines.append(f"[{step.status:4}] {step.name.ljust(width)}  {step.detail}".rstrip())
        summary = "result: " + ("ok" if self.ok else f"{len(self.failed)} step(s) failed")
        if self.skipped:
            summary += f" ({len(self.skipped)} skipped)"
        lines.append(summary)
        return "\n".join(lines)


# --- context ---------------------------------------------------------------------------------------
class Context:
    """Environment handed to every step.

    ``root``      filesystem prefix ("" = live system).  With a non-empty root, ``systemctl``
                  gets ``--root=<root>`` (offline enable/disable/preset only) and nothing is
                  ever started, restarted or written to the kernel.
    ``dry_run``   record what would happen; touch nothing.
    ``offline``   skip anything that needs the network (nothing in lindos-tune does) and, like
                  a chroot, never start/restart units.
    ``live``      systemd is PID 1 here and root == "" and not offline → runtime actions allowed.
    ``runner``    ``runner(argv, timeout) -> CmdResult`` (tests inject a recorder).
    ``which``     ``which(name) -> path|None`` (tests inject a fake).
    """

    def __init__(self, *, root_dir: Optional[str] = None, dry_run: bool = False, offline: bool = False,
                 chroot: Optional[bool] = None, runner: Optional[Runner] = None,
                 which_fn: Optional[Callable[[str], Optional[str]]] = None,
                 log_fn: Optional[Callable[[str], None]] = None) -> None:
        self.root = root() if root_dir is None else root_dir
        self.dry_run = bool(dry_run)
        self.offline = bool(offline)
        self._chroot = chroot
        self.injected_runner = runner is not None
        self.runner: Runner = runner or (lambda argv, timeout: run(argv, timeout))
        self.which: Callable[[str], Optional[str]] = which_fn or which
        self.log: Callable[[str], None] = log_fn or (lambda _msg: None)
        self.commands: List[List[str]] = []
        self.overrides: Dict[str, Any] = {}   # test hooks, e.g. {"zram_backend": "zram-tools"}

    # -- environment ---------------------------------------------------------------------------
    @property
    def chroot(self) -> bool:
        if self._chroot is None:
            self._chroot = bool(self.root) or in_chroot()
        return self._chroot

    @property
    def commands_allowed(self) -> bool:
        """May we spawn real processes?  Always with an injected runner; on a live system
        (no ``LINDOS_ROOT``); never against a test/staging root without an injected runner
        (a real ``systemctl --root=…`` would still be harmless, but ``pgrep``/``sensors`` would
        describe the *host*, not the tree under test)."""
        return self.injected_runner or not self.root

    @property
    def live(self) -> bool:
        """May we start/stop/restart units, write sysfs and reload sysctl right now?"""
        if self.root or self.offline or self.dry_run:
            return False
        return not self.chroot and systemd_booted()

    @property
    def can_systemctl(self) -> bool:
        return self.which("systemctl") is not None

    def path(self, system_path: str) -> str:
        return path(system_path, self.root)

    def systemctl_args(self, *args: str) -> List[str]:
        argv = ["systemctl"]
        if self.root:
            argv.append(f"--root={self.root}")
        argv.extend(args)
        return argv

    # -- execution -----------------------------------------------------------------------------
    def run(self, argv: Sequence[str], timeout: float = 60) -> CmdResult:
        argv = list(argv)
        self.commands.append(argv)
        if self.dry_run:
            self.log("would run: " + " ".join(argv))
            return CmdResult(True, 0, "dry-run")
        self.log("run: " + " ".join(argv))
        return self.runner(argv, timeout)

    def systemctl(self, *args: str, timeout: float = 90) -> CmdResult:
        if not self.can_systemctl:
            return CmdResult(False, 127, "systemctl: not found")
        return self.run(self.systemctl_args(*args), timeout)

    def write(self, system_path: str, content: str, mode: int = 0o644) -> Tuple[bool, bool]:
        """Write (unless dry-run).  Returns ``(ok, changed)``."""
        target = self.path(system_path)
        if self.dry_run:
            current = read_text(target)
            return True, current != content
        return write_if_changed(target, content, mode)

    def exists(self, system_path: str) -> bool:
        return os.path.exists(self.path(system_path))

    def unit_exists(self, unit: str) -> bool:
        """Does a unit file exist in the usual systemd search path (under root)?"""
        for directory in ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system",
                          "/lib/systemd/system", "/usr/local/lib/systemd/system"):
            if os.path.exists(self.path(os.path.join(directory, unit))):
                return True
        if "@" in unit:  # templated instance → template file
            base, _, rest = unit.partition("@")
            suffix = rest[rest.rfind("."):] if "." in rest else ".service"
            for directory in ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"):
                if os.path.exists(self.path(os.path.join(directory, base + "@" + suffix))):
                    return True
        return False


def describe_cmd(argv: Iterable[str]) -> str:
    return " ".join(argv)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


__all__ = [
    "ROOT_ENV", "CHROOT_ENV", "SYSCTL_BASE_CONF", "SYSCTL_MODE_CONF", "ZRAM_GENERATOR_CONF",
    "ZRAMSWAP_DEFAULT", "EARLYOOM_DEFAULT", "JOURNALD_DROPIN", "TMPFILES_CONF", "TMPFILES_GOVERNOR",
    "PRESET_FILE", "TUNE_D_DIR", "TUNE_SHARE_DIR", "SERVICES_WHITELIST", "AUTOSTART_HIDE_LIST",
    "RAM_BUDGET_JSON", "ANANICY_RULES_DIR", "XDG_AUTOSTART_DIR", "STATE_DIR",
    "STATE_FILE", "SYSTEM_LOG_DIR", "TUNE_LOG", "MODES_DIR", "SYSTEM_CONF", "LINDOS_RELEASE", "FSTAB", "CPU_DIR",
    "INSTALL_NBFC_SCRIPT", "MODE_IDS", "TARGET_MIN_MB", "TARGET_MAX_MB", "LITE_TARGET_MIN_MB",
    "LITE_TARGET_MAX_MB", "EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "EXIT_NO_SCHED_EXT", "UNIT_RE",
    "root", "path", "ensure_dir", "read_text", "read_first_line", "read_json", "write_text",
    "write_if_changed", "write_json", "parse_kv", "read_kv", "sh_quote", "CmdResult", "Runner",
    "which", "run", "run_capture", "is_root", "is_linux", "in_chroot", "systemd_booted",
    "systemd_running", "Step", "Report", "Context", "describe_cmd", "clamp",
]
