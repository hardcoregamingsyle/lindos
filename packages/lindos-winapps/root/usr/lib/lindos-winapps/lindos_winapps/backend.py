"""Backend configuration and reachability (SPEC-VM §22).

The backend is a real Windows instance reachable over RDP:

* ``libvirt`` (default) - the ``lindos-vm`` domain ``RDPWindows``;
* ``podman`` (optional, advanced) - a ``dockur/windows`` container.

Config lives in ``~/.config/lindos/winapps/winapps.conf`` (shell-style ``KEY="value"``, the
format the winapps-org tooling uses).  ``setup`` writes it and NEVER prompts for or stores the
password: the file documents FreeRDP credential handling, the ``RDP_PASS`` environment variable
and an optional user-created ``rdp-pass`` file.  Lindos does not enter Windows/app credentials.

All Linux/RDP calls are injectable/guarded so the logic is testable on Windows/macOS.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import get_logger, winapps_conf_path, winapps_config_dir

__all__ = [
    "BackendConfig",
    "BACKENDS",
    "DEFAULT_VM_NAME",
    "DEFAULT_HOST",
    "RDP_PORT",
    "default_config",
    "parse_conf",
    "load_config",
    "render_conf",
    "write_config",
    "domain_running",
    "container_running",
    "rdp_port_open",
    "backend_reachable",
    "check",
]

log = get_logger("lindos-winapps.backend")

BACKENDS = ("libvirt", "podman")
DEFAULT_BACKEND = "libvirt"
DEFAULT_VM_NAME = "RDPWindows"          # the lindos-vm domain used by WinApps
DEFAULT_HOST = "127.0.0.1"
DEFAULT_FLAGS = "/cert:tofu /sound:sys:pulse /microphone +clipboard /dynamic-resolution"
RDP_PORT = 3389


@dataclass
class BackendConfig:
    """Parsed ``winapps.conf`` (no password is ever held here)."""

    backend: str = DEFAULT_BACKEND
    host: str = DEFAULT_HOST
    user: str = ""
    domain: str = ""
    vm_name: str = DEFAULT_VM_NAME
    flags: str = DEFAULT_FLAGS
    port: int = RDP_PORT
    extra: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "backend": self.backend, "host": self.host, "user": self.user,
            "domain": self.domain, "vm_name": self.vm_name, "flags": self.flags,
            "port": self.port,
        }


def default_config() -> BackendConfig:
    return BackendConfig()


def parse_conf(text: str) -> Dict[str, str]:
    """Parse shell-style ``KEY="value"`` lines (comments and blanks ignored)."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            hashpos = value.find("#")            # strip an inline comment
            if hashpos != -1:
                value = value[:hashpos]
            value = value.strip()
        if key:
            out[key] = value
    return out


def _config_from_map(data: Dict[str, str]) -> BackendConfig:
    known = {
        "BACKEND", "WAFLAVOR", "RDP_HOST", "RDP_IP", "RDP_USER", "RDP_DOMAIN",
        "VM_NAME", "RDP_FLAGS", "RDP_PORT",
    }
    backend = (data.get("BACKEND") or data.get("WAFLAVOR") or DEFAULT_BACKEND).strip().lower()
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    try:
        port = int(data.get("RDP_PORT") or RDP_PORT)
    except ValueError:
        port = RDP_PORT
    extra = {k: v for k, v in data.items() if k not in known and k != "RDP_PASS"}
    return BackendConfig(
        backend=backend,
        host=(data.get("RDP_HOST") or data.get("RDP_IP") or DEFAULT_HOST).strip(),
        user=(data.get("RDP_USER") or "").strip(),
        domain=(data.get("RDP_DOMAIN") or "").strip(),
        vm_name=(data.get("VM_NAME") or DEFAULT_VM_NAME).strip(),
        flags=(data.get("RDP_FLAGS") or DEFAULT_FLAGS).strip(),
        port=port,
        extra=extra,
    )


def load_config(path: Optional[Path] = None) -> BackendConfig:
    """Load ``winapps.conf`` (returns defaults when the file does not exist)."""
    src = Path(path) if path is not None else winapps_conf_path()
    try:
        text = src.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return default_config()
    return _config_from_map(parse_conf(text))


def render_conf(config: BackendConfig) -> str:
    """Render ``winapps.conf`` text.  The password is deliberately NOT written."""
    return "\n".join([
        "# Lindos WinApps configuration  (SPEC-VM §22)",
        "# Seamless Windows apps over RDP against your own licensed Windows.",
        "#",
        "# PASSWORD IS NOT STORED HERE.  Provide your Windows password at launch via ONE of:",
        "#   * FreeRDP's own credential handling (it prompts when no password is given), or",
        "#   * the RDP_PASS environment variable:   RDP_PASS='...' lindos-winapps run <id>, or",
        f"#   * a file you create:   {winapps_config_dir() / 'rdp-pass'}   (chmod 600).",
        "# Lindos never enters or stores your Windows/app credentials and never bypasses licensing.",
        "",
        f'BACKEND="{config.backend}"          # libvirt (lindos-vm, default) | podman',
        f'RDP_HOST="{config.host}"',
        f'RDP_PORT="{config.port}"',
        f'RDP_USER="{config.user}"           # your Windows username',
        f'RDP_DOMAIN="{config.domain}"',
        f'VM_NAME="{config.vm_name}"          # libvirt domain name (lindos-vm)',
        f'RDP_FLAGS="{config.flags}"',
        "",
    ])


def write_config(config: BackendConfig, path: Optional[Path] = None) -> Path:
    """Write ``winapps.conf`` (creating the directory).  Never writes a password."""
    target = Path(path) if path is not None else winapps_conf_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".conf.tmp")
    tmp.write_text(render_conf(config), encoding="utf-8")
    tmp.replace(target)
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    log.info("wrote %s", target)
    return target


def domain_running(vm_name: str, *, which: Callable[[str], Optional[str]] = shutil.which,
                   run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Optional[bool]:
    """True/False if a libvirt domain is running, or None when virsh is unavailable."""
    virsh = which("virsh")
    if not virsh:
        return None
    try:
        proc = run([virsh, "domstate", vm_name], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return False
    return (proc.stdout or "").strip().lower() == "running"


def container_running(name: str = "WinApps", *, which: Callable[[str], Optional[str]] = shutil.which,
                      run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Optional[bool]:
    """True/False if a podman container is running, or None when podman is unavailable."""
    podman = which("podman")
    if not podman:
        return None
    try:
        proc = run([podman, "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
                   capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return name in (proc.stdout or "")


def rdp_port_open(host: str, port: int = RDP_PORT, *, timeout: float = 3.0,
                  connect: Optional[Callable[[str, int, float], bool]] = None) -> bool:
    """True when a TCP connection to ``host:port`` succeeds (RDP reachable)."""
    if connect is not None:
        return connect(host, port, timeout)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def backend_reachable(config: BackendConfig, **kw: object) -> bool:
    """True when the RDP port is open (the definitive reachability signal)."""
    return rdp_port_open(config.host, config.port, **kw)  # type: ignore[arg-type]


def check(config: BackendConfig, *, which: Callable[[str], Optional[str]] = shutil.which,
          run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
          connect: Optional[Callable[[str, int, float], bool]] = None) -> Dict[str, object]:
    """Assemble a readiness report: FreeRDP present, backend up, RDP port open."""
    from .rdp import find_freerdp

    freerdp = find_freerdp(which)
    if config.backend == "podman":
        running = container_running(which=which, run=run)
    else:
        running = domain_running(config.vm_name, which=which, run=run)
    port_open = rdp_port_open(config.host, config.port, connect=connect) if connect else \
        rdp_port_open(config.host, config.port)
    reachable = bool(port_open)
    return {
        "backend": config.backend,
        "host": config.host,
        "port": config.port,
        "vm_name": config.vm_name,
        "freerdp": freerdp,
        "freerdp_present": bool(freerdp),
        "backend_running": running,
        "rdp_port_open": port_open,
        "reachable": reachable,
    }
