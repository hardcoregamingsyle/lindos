"""``lindos-tune power <performance|balanced|power-saver>`` (SPEC §11).

Back-ends in order: ``powerprofilesctl`` (power-profiles-daemon) → TLP (``tlp ac`` /
``tlp start`` / ``tlp bat``) → plain cpufreq governor (performance / schedutil / powersave)
through :mod:`lindos_tune.governor`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import common, governor
from .common import Context, Report

PROFILES = ("performance", "balanced", "power-saver")
GOVERNOR_FOR_PROFILE = {"performance": "performance", "balanced": "schedutil", "power-saver": "powersave"}
TLP_FOR_PROFILE = {"performance": "ac", "balanced": "start", "power-saver": "bat"}


def normalize(profile: str) -> Optional[str]:
    p = (profile or "").strip().lower().replace("_", "-")
    aliases = {"powersave": "power-saver", "power-save": "power-saver", "saver": "power-saver",
               "perf": "performance", "balance": "balanced"}
    p = aliases.get(p, p)
    return p if p in PROFILES else None


def backend(ctx: Context) -> str:
    """``ppd`` | ``tlp`` | ``governor``."""
    if governor.ppd_available(ctx):
        return "ppd"
    if ctx.overrides.get("tlp") is not None:
        return "tlp" if ctx.overrides["tlp"] else "governor"
    if not (ctx.root and not ctx.commands_allowed) and ctx.which("tlp"):
        return "tlp"
    return "governor"


def current(ctx: Context) -> Dict[str, Any]:
    """Current profile as far as we can tell (``{"backend", "profile", "governor"}``)."""
    be = backend(ctx)
    gov = governor.current(ctx)
    prof: Optional[str] = None
    if be == "ppd" and not ctx.dry_run and (ctx.commands_allowed or not ctx.root):
        res = ctx.run(["powerprofilesctl", "get"], timeout=10)
        if res.ok:
            prof = res.out.strip() or None
    if prof is None and gov:
        prof = {v: k for k, v in GOVERNOR_FOR_PROFILE.items()}.get(gov)
        if prof is None and gov in ("ondemand", "conservative"):
            prof = "balanced"
    return {"backend": be, "profile": prof, "governor": gov}


def list_profiles(ctx: Context) -> List[str]:
    if backend(ctx) == "ppd" and not ctx.dry_run and (ctx.commands_allowed or not ctx.root):
        res = ctx.run(["powerprofilesctl", "list"], timeout=10)
        found = [ln.strip().lstrip("* ").rstrip(":") for ln in res.out.splitlines()
                 if ln.strip().rstrip(":").lstrip("* ") in PROFILES]
        if found:
            return found
    return list(PROFILES)


def set_profile(ctx: Context, profile: str, report: Optional[Report] = None) -> Report:
    report = report or Report(title=f"power {profile}", dry_run=ctx.dry_run)
    prof = normalize(profile)
    if prof is None:
        report.add("power", False, f"unknown profile {profile!r} (choose from {', '.join(PROFILES)})")
        return report
    be = backend(ctx)
    if be == "ppd":
        if ctx.dry_run:
            report.add("power", True, f"would run: powerprofilesctl set {prof}")
            return report
        if not ctx.live and ctx.root:
            report.skip("power", "powerprofilesctl needs a live system (chroot/offline)")
            return report
        res = ctx.run(["powerprofilesctl", "set", prof], timeout=20)
        if res.ok:
            report.add("power", True, f"{prof} via power-profiles-daemon")
            return report
        report.add("power-ppd", False, f"powerprofilesctl set {prof} failed: {res.tail()} — trying fallback")
        be = "tlp" if (ctx.which("tlp") and not ctx.root) else "governor"
    if be == "tlp":
        arg = TLP_FOR_PROFILE[prof]
        if ctx.dry_run:
            report.add("power", True, f"would run: tlp {arg}")
            return report
        if not ctx.live and ctx.root:
            report.skip("power", "tlp needs a live system (chroot/offline)")
            return report
        res = ctx.run(["tlp", arg], timeout=60)
        if res.ok:
            report.add("power", True, f"{prof} via TLP (tlp {arg})")
        else:
            report.add("power", False, f"tlp {arg} failed: {res.tail()}" + ("" if common.is_root() else " (needs root)"))
        return report
    gov = GOVERNOR_FOR_PROFILE[prof]
    sub = governor.apply(ctx, gov, Report(dry_run=ctx.dry_run), prefer_ppd=False)
    for step in sub.steps:
        report.steps.append(step)
    if sub.ok:
        report.add("power", True, f"{prof} → cpufreq governor {gov} (no power-profiles-daemon/TLP installed)")
    else:
        report.add("power", False, f"governor fallback failed for {prof}")
    return report


__all__ = ["PROFILES", "GOVERNOR_FOR_PROFILE", "TLP_FOR_PROFILE", "normalize", "backend", "current",
           "list_profiles", "set_profile"]
