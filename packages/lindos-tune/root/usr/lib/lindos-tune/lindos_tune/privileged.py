"""Privilege escalation for ``lindos-tune`` — the helper is the single privileged entry (SPEC §4.6, §13).

When a subcommand needs root and ``euid != 0`` the CLI does **not** re-exec itself with sudo;
it describes the request as an *op payload* and hands it to lindos-core's
``lindos.helper.run_privileged`` (``pkexec /usr/libexec/lindos/lindos-helper``).  The helper
validates the payload against its whitelist and then calls ``lindos-tune`` back **as root**
(``lindos-tune apply --mode …``, ``lindos-tune zram N``, ``lindos-tune governor g``,
``lindos-tune fan set p``) or runs ``systemctl`` itself (``set-services``).

Op payload shape (what :func:`Request.to_payload` produces, logged and shown with ``--dry-run``)::

    {"op": "apply",    "mode": <id>, "offline": bool}
    {"op": "zram",     "percent": 0..200}
    {"op": "governor", "governor": "schedutil|performance|powersave|ondemand|conservative|userspace"}
    {"op": "services", "enable": [unit…], "disable": [unit…]}      # whitelisted units only
    {"op": "fan",      "profile": "auto|quiet|balanced|max"}
    {"op": "power",    "profile": "performance|balanced|power-saver"}
    {"op": "sched",    "profile": "scx_lavd|scx_bpfland|scx_flash|scx_rustland|none"}

Mapping onto lindos-core helper actions (``lindos.helper.ACTIONS`` / ``validate_payload``):

    ======== ==================== ============================================================
    op       helper action        payload sent
    ======== ==================== ============================================================
    apply    ``apply-tune``       ``{"mode": id, "offline": bool}``
    zram     ``set-zram``         ``{"percent": n}``
    governor ``set-governor``     ``{"governor": g}``
    services ``set-services``     ``{"enable": [...], "disable": [...], "mask": []}``
    fan      ``set-fan-profile``  ``{"profile": p}``
    power    ``set-governor``     ``{"governor": performance|schedutil|powersave}`` — only the
                                  cpufreq fallback needs root; ``powerprofilesctl`` works for the
                                  logged-in user directly (PPD's own polkit rule) and is run
                                  unprivileged by :mod:`lindos_tune.power`; TLP needs a root shell.
    sched    ``set-sched``        ``{"profile": scx_lavd|scx_bpfland|scx_flash|scx_rustland|none}``
    ======== ==================== ============================================================

The op layer exists so callers (Lindos Settings, tests, logs) see one stable vocabulary even
though lindos-core exposes several narrow actions.  Nothing here imports ``lindos`` at module
level — the import is lazy and guarded so the CLI works on a box without lindos-core (it then
tells the user to run the command as root).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import common

OPS = ("apply", "zram", "governor", "services", "fan", "power", "sched")

#: env var that forbids escalation (tests / CI): the CLI reports EXIT_ERROR with a hint instead.
NO_ESCALATE_ENV = "LINDOS_TUNE_NO_ESCALATE"

_GOVERNOR_FOR_POWER = {"performance": "performance", "balanced": "schedutil", "power-saver": "powersave"}


class EscalationError(RuntimeError):
    """The request cannot be handed to the helper (bad op, helper missing, escalation refused)."""


@dataclass
class Request:
    """One privileged request expressed in the ``op`` vocabulary."""

    op: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """The documented op payload (``{"op": ..., ...}``)."""
        payload: Dict[str, Any] = {"op": self.op}
        payload.update(self.params)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))

    # convenience constructors ------------------------------------------------------------
    @classmethod
    def apply(cls, mode: str, offline: bool = False) -> "Request":
        return cls("apply", {"mode": mode, "offline": bool(offline)})

    @classmethod
    def zram(cls, percent: int) -> "Request":
        return cls("zram", {"percent": int(percent)})

    @classmethod
    def governor(cls, governor: str) -> "Request":
        return cls("governor", {"governor": governor})

    @classmethod
    def services(cls, enable: Optional[List[str]] = None, disable: Optional[List[str]] = None) -> "Request":
        return cls("services", {"enable": list(enable or []), "disable": list(disable or [])})

    @classmethod
    def fan(cls, profile: str) -> "Request":
        return cls("fan", {"profile": profile})

    @classmethod
    def power(cls, profile: str) -> "Request":
        return cls("power", {"profile": profile})

    @classmethod
    def sched(cls, profile: str) -> "Request":
        return cls("sched", {"profile": profile})


@dataclass
class EscalationResult:
    """Outcome of :func:`escalate` (mirrors ``lindos.helper.HelperResult``)."""

    ok: bool
    out: str = ""
    err: str = ""
    code: int = 0
    action: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        text = (self.err or self.out or "").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ("ok" if self.ok else f"exit code {self.code}")


# --- mapping ---------------------------------------------------------------------------------------
def helper_call(request: Request) -> Tuple[str, Dict[str, Any]]:
    """Translate *request* into ``(helper_action, helper_payload)``.  Raises :class:`EscalationError`."""
    op = request.op
    p = request.params
    if op == "apply":
        mode = p.get("mode")
        if not isinstance(mode, str) or mode not in common.MODE_IDS:
            raise EscalationError(f"apply: unknown mode {mode!r}")
        return "apply-tune", {"mode": mode, "offline": bool(p.get("offline", False))}
    if op == "zram":
        try:
            percent = int(p.get("percent"))
        except (TypeError, ValueError):
            raise EscalationError("zram: 'percent' must be an integer") from None
        if not 0 <= percent <= 200:
            raise EscalationError("zram: 'percent' must be between 0 and 200")
        return "set-zram", {"percent": percent}
    if op == "governor":
        from . import governor as lgovernor

        gov = p.get("governor")
        if gov not in lgovernor.GOVERNORS:
            raise EscalationError(f"governor: unknown governor {gov!r}")
        return "set-governor", {"governor": gov}
    if op == "services":
        from . import services as lservices

        enable = [lservices.normalize_unit(u) for u in p.get("enable", []) or []]
        disable = [lservices.normalize_unit(u) for u in p.get("disable", []) or []]
        if not enable and not disable:
            raise EscalationError("services: nothing to enable or disable")
        wl = lservices.load_whitelist()
        for unit in enable + disable:
            if not lservices.is_whitelisted(unit, wl):
                raise EscalationError(f"services: unit '{unit}' is not in the lindos-tune whitelist")
        return "set-services", {"enable": enable, "disable": disable, "mask": []}
    if op == "fan":
        from . import fan as lfan

        prof = lfan.normalize_profile(str(p.get("profile", "")))
        if prof is None:
            raise EscalationError(f"fan: unknown profile {p.get('profile')!r}")
        return "set-fan-profile", {"profile": prof}
    if op == "power":
        from . import power as lpower

        prof = lpower.normalize(str(p.get("profile", "")))
        if prof is None:
            raise EscalationError(f"power: unknown profile {p.get('profile')!r}")
        return "set-governor", {"governor": _GOVERNOR_FOR_POWER[prof]}
    if op == "sched":
        from . import sched as lsched

        prof = p.get("profile")
        if prof not in lsched.SETTABLE_PROFILES:
            raise EscalationError(f"sched: unknown profile {prof!r} "
                                  f"(choose from {', '.join(lsched.SETTABLE_PROFILES)})")
        return "set-sched", {"profile": prof}
    raise EscalationError(f"unknown op {op!r} (choose from {', '.join(OPS)})")


def sudo_hint(argv: Optional[List[str]] = None) -> str:
    """The command line to run as root when escalation is impossible."""
    args = list(argv if argv is not None else sys.argv[1:])
    return "sudo lindos-tune " + " ".join(args) if args else "sudo lindos-tune …"


def escalation_allowed() -> bool:
    return os.environ.get(NO_ESCALATE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def _load_core_helper() -> Any:
    """``lindos.helper`` module or ``None`` when lindos-core is not importable."""
    try:
        from lindos import helper as core_helper  # type: ignore
    except Exception:  # ImportError or a broken lindos-core — treat the same
        return None
    return core_helper


# --- escalate --------------------------------------------------------------------------------------
def escalate(request: Request, *, log: Optional[Callable[[str], None]] = None,
             timeout: Optional[float] = None) -> EscalationResult:
    """Send *request* to the privileged helper.  Never raises; problems come back as a result
    with ``code`` 127 (helper unavailable), 126 (auth cancelled) or 2 (bad request)."""
    try:
        action, payload = helper_call(request)
    except EscalationError as exc:
        return EscalationResult(False, "", str(exc), common.EXIT_USAGE)
    if not escalation_allowed():
        return EscalationResult(False, "", f"privilege escalation disabled ({NO_ESCALATE_ENV}); run: {sudo_hint()}",
                                common.EXIT_ERROR, action, payload)
    core = _load_core_helper()
    if core is None:
        return EscalationResult(False, "", "lindos-core (python3 module 'lindos') is not installed — "
                                f"run as root instead: {sudo_hint()}", 127, action, payload)
    if log:
        log(f"escalating through lindos-helper: {action} {json.dumps(payload, sort_keys=True)} "
            f"(op payload {request.to_json()})")
    try:
        result = core.run_privileged(action, payload, log, timeout=timeout)
    except TypeError:  # older signature without timeout
        result = core.run_privileged(action, payload, log)
    except Exception as exc:  # pragma: no cover - defensive
        return EscalationResult(False, "", f"helper call failed: {exc}", common.EXIT_ERROR, action, payload)
    return EscalationResult(bool(getattr(result, "ok", False)), str(getattr(result, "out", "") or ""),
                            str(getattr(result, "err", "") or ""), int(getattr(result, "code", 1) or 0),
                            action, payload)


def describe(request: Request) -> str:
    """Human line for ``--dry-run`` / logs: what would be sent where."""
    try:
        action, payload = helper_call(request)
    except EscalationError as exc:
        return f"cannot escalate: {exc}"
    return (f"needs root → lindos-helper {action} {json.dumps(payload, sort_keys=True)}"
            f"  [op payload: {request.to_json()}]")


__all__ = ["OPS", "NO_ESCALATE_ENV", "EscalationError", "Request", "EscalationResult", "helper_call",
           "sudo_hint", "escalation_allowed", "escalate", "describe"]
