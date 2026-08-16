"""FreeRDP RemoteApp command building and launch (SPEC-VM §22).

``run <id>`` launches a single Windows program in its own window on the Lindos desktop using
FreeRDP's RemoteApp feature (``xfreerdp3 /app:program:...`` on FreeRDP 3, ``xfreerdp /app:...
/app-cmd: /app-name:`` on FreeRDP 2).

Credentials (SPEC-VM §20): the password is NEVER stored by Lindos.  At launch it is taken, in
order, from the ``RDP_PASS`` environment variable, then an optional user-created ``rdp-pass``
file; if neither is present no ``/p:`` is passed and FreeRDP prompts / uses its own credential
handling.  The password is never written to a log or shown in the redacted command line.

All subprocess calls are injectable so the logic is testable on Windows/macOS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import get_logger, rdp_pass_file

__all__ = [
    "FREERDP_BINARIES",
    "find_freerdp",
    "freerdp_generation",
    "password_source",
    "build_command",
    "redact_command",
    "launch",
]

log = get_logger("lindos-winapps.rdp")

#: Preferred order: FreeRDP 3 X11 client first, then FreeRDP 2's ``xfreerdp``.
FREERDP_BINARIES = ("xfreerdp3", "xfreerdp", "sdl-freerdp3")


def find_freerdp(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Return the path to a usable FreeRDP client, or None."""
    for name in FREERDP_BINARIES:
        found = which(name)
        if found:
            return found
    return None


def freerdp_generation(binary: str) -> int:
    """Guess the FreeRDP major version from the binary name (3 when it contains '3')."""
    return 3 if "3" in os.path.basename(binary) else 2


def password_source(env: Optional[dict] = None,
                    pass_file: Optional[Path] = None) -> Tuple[Optional[str], str]:
    """Return ``(password, source_label)``.

    Order: ``RDP_PASS`` env var, then the user-created ``rdp-pass`` file.  Returns
    ``(None, "prompt")`` when neither exists so FreeRDP handles credentials itself.  Lindos
    never writes either source.
    """
    environ = os.environ if env is None else env
    value = environ.get("RDP_PASS")
    if value:
        return value, "RDP_PASS environment variable"
    pf = Path(pass_file) if pass_file is not None else rdp_pass_file()
    try:
        text = pf.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        text = ""
    if text:
        return text, str(pf)
    return None, "prompt"


def _server(host: str, port: int) -> str:
    return f"{host}:{port}" if port and int(port) != 3389 else host


def build_command(app, config, binary: str, *, args: Optional[List[str]] = None,
                  password: Optional[str] = None, generation: Optional[int] = None) -> List[str]:
    """Build the ``xfreerdp`` argv for a RemoteApp launch.

    ``app`` needs ``.rdp_path``/``.name``; ``config`` needs ``.host``/``.port``/``.user``/
    ``.domain``/``.flags`` (a :class:`~lindos_winapps.backend.BackendConfig`).
    """
    gen = generation if generation is not None else freerdp_generation(binary)
    cmd: List[str] = [binary, f"/v:{_server(config.host, config.port)}"]
    if config.user:
        cmd.append(f"/u:{config.user}")
    if config.domain:
        cmd.append(f"/d:{config.domain}")
    if password:
        cmd.append(f"/p:{password}")
    for flag in str(config.flags or "").split():
        cmd.append(flag)
    cmd_args = " ".join(args) if args else ""
    if gen >= 3:
        spec = f"/app:program:{app.rdp_path}"
        if cmd_args:
            spec += f",cmd:{cmd_args}"
        spec += f",name:{app.name}"
        cmd.append(spec)
    else:
        cmd.append(f"/app:{app.rdp_path}")
        cmd.append(f"/app-name:{app.name}")
        if cmd_args:
            cmd.append(f"/app-cmd:{cmd_args}")
    return cmd


def redact_command(cmd: List[str]) -> List[str]:
    """A copy of ``cmd`` with any ``/p:`` password replaced by ``/p:******`` (for logging)."""
    out: List[str] = []
    for token in cmd:
        if token.startswith("/p:"):
            out.append("/p:******")
        else:
            out.append(token)
    return out


def launch(app, config, *, args: Optional[List[str]] = None,
           which: Callable[[str], Optional[str]] = shutil.which,
           run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
           env: Optional[dict] = None) -> int:
    """Launch ``app`` over RDP RemoteApp.  Returns the FreeRDP exit code (or 1/3 on setup errors)."""
    binary = find_freerdp(which)
    if not binary:
        log.error("no FreeRDP client found (install freerdp3-x11 or freerdp2-x11)")
        return 1
    password, source = password_source(env)
    if password:
        log.info("using password from %s", source)
    else:
        log.info("no password provided; FreeRDP will prompt (set RDP_PASS or create %s)", rdp_pass_file())
    cmd = build_command(app, config, binary, args=args, password=password)
    log.info("launching: %s", " ".join(redact_command(cmd)))
    try:
        proc = run(cmd, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("failed to launch FreeRDP: %s", exc)
        return 1
    return int(getattr(proc, "returncode", 0) or 0)
