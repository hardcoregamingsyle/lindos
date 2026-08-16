"""Runner selection, environment and command construction for ``lindos-run`` (SPEC §9).

Runners
-------
* ``umu``     -- umu-launcher + Proton-GE (DXVK/VKD3D built in): games and unknown programs
* ``wine``    -- plain Wine (staging): installers, office/creative/utility apps
* ``bottles`` -- Bottles (Flatpak com.usebottles.bottles) when a recipe asks for it, or on request

Everything here is pure computation except :func:`run_plan` and the small
"is X installed" probes, which take injectable ``which``/``run`` callables so the
logic is unit-testable on any OS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import CoreMissing, core, expand_user_path, get_logger, path_const, user_home
from .perf import GamescopeSpec, gamescope_wrap, perf_env
from .prefix import SHARED_SLUG, base_wine_env, find_wine, prefix_path, read_marker

__all__ = [
    "RUNNERS",
    "BOTTLES_APP_ID",
    "RunPlan",
    "choose_runner",
    "is_bottles_installed",
    "bottle_prefix_path",
    "find_proton",
    "proton_search_dirs",
    "umu_env",
    "wine_env",
    "build_command",
    "build_plan",
    "log_path_for",
    "load_prefix_settings",
    "run_plan",
    "gpu_is_nvidia",
    "ensure_bottle",
]

log = get_logger("lindos-compat.runner")

RUNNERS = ("umu", "wine", "bottles")
BOTTLES_APP_ID = "com.usebottles.bottles"
GAME_KINDS = ("game", "unknown")
WINE_KINDS = ("installer", "app", "msi")

_GE_PROTON_RE = re.compile(r"^GE-Proton(\d+)-(\d+)$")


@dataclass
class RunPlan:
    """Everything needed to launch a Windows program (argv/env/cwd) plus bookkeeping."""

    runner: str
    slug: str
    prefix: Path
    exe: Path
    kind: str
    argv: List[str]
    env: Dict[str, str]
    cwd: Optional[str]
    log_path: Path
    reason: str = ""
    gamemode: bool = False
    mangohud: bool = False
    args: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "runner": self.runner,
            "slug": self.slug,
            "prefix": str(self.prefix),
            "exe": str(self.exe),
            "kind": self.kind,
            "argv": list(self.argv),
            "env": dict(self.env),
            "cwd": self.cwd,
            "log": str(self.log_path),
            "reason": self.reason,
            "gamemode": self.gamemode,
            "mangohud": self.mangohud,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def is_bottles_installed(which: Callable[[str], Optional[str]] = shutil.which,
                         run: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
                         home: Optional[Path] = None) -> bool:
    """True when ``flatpak`` exists and Bottles (com.usebottles.bottles) is installed."""
    if not which("flatpak"):
        return False
    home = home or user_home()
    for exports in (
        home / ".local/share/flatpak/exports/bin" / BOTTLES_APP_ID,
        Path("/var/lib/flatpak/exports/bin") / BOTTLES_APP_ID,
    ):
        if exports.exists():
            return True
    if run is None:
        run = subprocess.run
    try:
        proc = run([str(which("flatpak")), "info", BOTTLES_APP_ID], capture_output=True, text=True,
                   timeout=20, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def bottle_prefix_path(name: str, home: Optional[Path] = None) -> Path:
    """Where the Bottles Flatpak keeps the bottle ``name`` (its C:\\ drive lives in ``drive_c`` below it)."""
    home = home or user_home()
    return home / ".var/app" / BOTTLES_APP_ID / "data/bottles/bottles" / name


def proton_search_dirs(home: Optional[Path] = None) -> List[Path]:
    home = home or user_home()
    return [
        home / ".local/share/umu/compatibilitytools",
        home / ".steam/root/compatibilitytools.d",
        home / ".local/share/Steam/compatibilitytools.d",
        home / ".steam/steam/compatibilitytools.d",
    ]


def _proton_version_key(name: str) -> Tuple[int, int]:
    m = _GE_PROTON_RE.match(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (-1, -1)


def find_proton(home: Optional[Path] = None) -> Optional[Path]:
    """Highest installed ``GE-Proton*`` directory (from lindos-proton / umu), or None."""
    best: Optional[Tuple[Tuple[int, int], Path]] = None
    for d in proton_search_dirs(home):
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith("GE-Proton"):
                continue
            if not (entry / "proton").exists():
                continue
            key = _proton_version_key(entry.name)
            if best is None or key > best[0]:
                best = (key, entry)
    return best[1] if best else None


def gpu_is_nvidia() -> bool:
    """Best-effort NVIDIA detection via lindos.hardware (guarded; False on any problem)."""
    try:
        from lindos import hardware  # type: ignore[import-not-found]

        info = hardware.gpu_info()
        vendors: List[str] = []
        if isinstance(info, dict):
            vendors.append(str(info.get("vendor", "")))
        elif isinstance(info, (list, tuple)):
            for g in info:
                vendors.append(str(g.get("vendor", "")) if isinstance(g, dict) else str(getattr(g, "vendor", "")))
        else:
            vendors.append(str(getattr(info, "vendor", "")))
        return any(v.lower() == "nvidia" for v in vendors)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Runner choice
# ---------------------------------------------------------------------------


def choose_runner(
    info: object,
    config: object,
    *,
    requested: Optional[str] = None,
    recipe: Optional[object] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    bottles_installed: Optional[bool] = None,
    prefix_runner: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(runner, reason)``.

    Rules (SPEC §9 / lindos.compat.choose_runner, made PATH-aware):
      * ``--runner`` wins when that runner is actually available;
      * a recipe asking for ``bottles`` wins when Bottles is installed;
      * a prefix already created by a runner keeps it (avoids mixing Wine/Proton layouts);
      * games & unknown → ``umu`` if ``umu-run`` is on PATH, else ``wine``;
      * installers, apps, .msi → ``wine`` (falls back to ``umu`` when Wine is missing).
    """
    kind = str(getattr(info, "kind", "unknown") or "unknown")
    have_umu = which("umu-run") is not None
    have_wine = find_wine(which) is not None
    if bottles_installed is None:
        bottles_installed = is_bottles_installed(which)

    def fallback(reason: str) -> Tuple[str, str]:
        if kind in GAME_KINDS:
            if have_umu:
                return "umu", reason + "; " + f"{kind} -> Proton (umu)"
            return "wine", reason + f"; {kind} -> Wine (umu-run not installed: lindos-compat install-umu)"
        if have_wine:
            return "wine", reason + f"; {kind} -> Wine"
        if have_umu:
            return "umu", reason + f"; {kind} -> Proton (umu) because Wine is not installed"
        return "wine", reason + f"; {kind} -> Wine (not installed! run: lindos-compat doctor)"

    if requested:
        req = requested.lower()
        if req not in RUNNERS:
            return fallback(f"unknown runner '{requested}' ignored")
        if req == "umu" and not have_umu:
            return fallback("umu-run is not installed (lindos-compat install-umu)")
        if req == "bottles" and not bottles_installed:
            return fallback("Bottles is not installed (lindos-compat install-bottles)")
        if req == "wine" and not have_wine:
            return fallback("Wine is not installed")
        return req, "requested with --runner"

    if prefix_runner in RUNNERS:
        if prefix_runner == "umu" and have_umu:
            return "umu", "prefix was created by Proton (umu)"
        if prefix_runner == "wine" and have_wine:
            return "wine", "prefix was created by Wine"
        if prefix_runner == "bottles" and bottles_installed:
            return "bottles", "prefix was created by Bottles"

    recipe_runner = str(getattr(recipe, "runner", "") or "") if recipe is not None else ""
    if recipe_runner == "bottles":
        if bottles_installed:
            return "bottles", "recipe prefers Bottles"
        return fallback("recipe prefers Bottles but it is not installed")
    if recipe_runner == "umu" and have_umu:
        return "umu", "recipe prefers Proton (umu)"
    if recipe_runner == "wine" and have_wine:
        return "wine", "recipe prefers Wine"

    # config: honour the shared lindos.compat rule as a hint (game/unknown -> umu, else wine)
    return fallback("auto")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def wine_env(prefix: Path, *, arch: str = "win64", dll_overrides: Optional[Dict[str, str]] = None,
             headless: bool = False, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = base_wine_env(prefix, arch=arch, dll_overrides=dll_overrides, headless=headless)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def umu_env(prefix: Path, slug: str, *, headless: bool = False, home: Optional[Path] = None,
            dll_overrides: Optional[Dict[str, str]] = None, extra: Optional[Dict[str, str]] = None,
            nvidia: Optional[bool] = None) -> Dict[str, str]:
    """Environment for umu-run (SPEC §9: GAMEID, PROTONPATH, STORE, DXVK_ASYNC, PROTON_*)."""
    overrides = {"winemenubuilder.exe": "d"}
    if headless:
        overrides.update({"mscoree": "d", "mshtml": "d"})
    if dll_overrides:
        overrides.update({k: v for k, v in dll_overrides.items() if k and v})
    env: Dict[str, str] = {
        "WINEPREFIX": str(prefix),
        "GAMEID": "umu-default" if slug == SHARED_SLUG else f"umu-{slug}",
        "STORE": "none",
        "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": ";".join(f"{k}={v}" for k, v in overrides.items()),
        "DXVK_ASYNC": "1",
    }
    user_proton = os.environ.get("PROTONPATH")
    if user_proton:
        env["PROTONPATH"] = user_proton
    else:
        proton = find_proton(home)
        # "GE-Proton" tells umu to use (and download when missing) the latest GE-Proton
        env["PROTONPATH"] = str(proton) if proton else "GE-Proton"
    if nvidia is None:
        nvidia = gpu_is_nvidia()
    if nvidia:
        env["PROTON_ENABLE_NVAPI"] = "1"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _file_kind(exe: Path, kind: str) -> str:
    suffix = exe.suffix.lower()
    if suffix == ".msi":
        return "msi"
    if suffix in (".bat", ".cmd"):
        return "bat"
    return kind


def build_command(
    runner: str,
    exe: Path,
    kind: str,
    *,
    args: Sequence[str] = (),
    which: Callable[[str], Optional[str]] = shutil.which,
    gamemode: bool = False,
    mangohud: bool = False,
    bottle: Optional[str] = None,
    gamescope: Optional[GamescopeSpec] = None,
) -> Tuple[List[str], List[str]]:
    """Return ``(argv, warnings)`` for the runner.

    ``.msi`` → ``msiexec /i``, ``.bat`` → ``cmd /c`` (under wine or umu).  When
    ``gamescope`` is enabled the whole command is wrapped in gamescope (outermost).
    """
    warnings: List[str] = []
    fkind = _file_kind(exe, kind)
    argv: List[str] = []
    if runner == "umu":
        umu = which("umu-run")
        if not umu:
            raise FileNotFoundError("umu-run not found")
        argv = [umu]
        if fkind == "msi":
            argv += ["msiexec", "/i", str(exe)]
        elif fkind == "bat":
            argv += ["cmd", "/c", str(exe)]
        else:
            argv += [str(exe)]
    elif runner == "bottles":
        flatpak = which("flatpak")
        if not flatpak:
            raise FileNotFoundError("flatpak not found")
        argv = [flatpak, "run", "--command=bottles-cli", BOTTLES_APP_ID, "run", "-b", bottle or "Lindos", "-e", str(exe)]
        if args:
            argv += ["-a", " ".join(args)]
            args = ()
    else:  # wine
        wine = find_wine(which)
        if not wine:
            raise FileNotFoundError("wine not found")
        argv = [wine]
        if fkind == "msi":
            argv += ["msiexec", "/i", str(exe)]
        elif fkind == "bat":
            argv += ["cmd", "/c", str(exe)]
        else:
            argv += [str(exe)]
    argv += [str(a) for a in args]

    if mangohud and runner != "umu":
        mh = which("mangohud")
        if mh:
            argv = [mh] + argv
        else:
            warnings.append("MangoHud requested but not installed (apt install mangohud)")
    if gamemode:
        gm = which("gamemoderun")
        if gm:
            argv = [gm] + argv
        else:
            warnings.append("Game Mode requested but gamemode is not installed (apt install gamemode)")
    argv, gs_warnings = gamescope_wrap(argv, gamescope, which=which)
    warnings.extend(gs_warnings)
    return argv, warnings


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def log_path_for(slug: str) -> Path:
    return expand_user_path(path_const("LOG_DIR")) / f"run-{slug}.log"


def load_prefix_settings(slug: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(env, dll_overrides) recorded for the prefix in APPS_DB (recipes / registered apps)."""
    env: Dict[str, str] = {}
    overrides: Dict[str, str] = {}
    try:
        db = core().apps_db_load()
    except CoreMissing:
        return env, overrides
    except Exception:  # noqa: BLE001
        return env, overrides
    if not isinstance(db, dict):
        return env, overrides
    prefix_str = str(prefix_path(slug))
    for rec in db.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("prefix") not in (slug, prefix_str):
            continue
        e = rec.get("env")
        if isinstance(e, dict):
            env.update({str(k): str(v) for k, v in e.items()})
        o = rec.get("dll_overrides")
        if isinstance(o, dict):
            overrides.update({str(k): str(v) for k, v in o.items()})
    return env, overrides


def build_plan(
    *,
    runner: str,
    slug: str,
    exe: Path,
    kind: str,
    config: object,
    args: Sequence[str] = (),
    gamemode_flag: bool = False,
    mangohud_flag: bool = False,
    which: Callable[[str], Optional[str]] = shutil.which,
    home: Optional[Path] = None,
    headless: bool = False,
    extra_env: Optional[Dict[str, str]] = None,
    dll_overrides: Optional[Dict[str, str]] = None,
    arch: str = "win64",
    reason: str = "",
    nvidia: Optional[bool] = None,
    environ: Optional[Mapping[str, str]] = None,
    dxvk_async: Optional[bool] = None,
    hdr: bool = False,
    ntsync: Optional[bool] = None,
    gamescope: Optional[GamescopeSpec] = None,
) -> RunPlan:
    """Assemble argv/env/cwd for a launch (no side effects).

    For the umu (Proton) runner the documented performance env (SPEC-KERNEL §17.1) is
    layered in: ntsync default, DXVK async and the NVIDIA-GPU flag are set only when the
    user has not already set them; ``--dxvk-async``/``--no-dxvk-async`` and a per-title
    profile's ``env`` win.  wine/app launches are untouched.
    """
    prefix = prefix_path(slug)
    environ = os.environ if environ is None else environ
    cfg_get = getattr(config, "get", None)
    gamemode_auto = bool(cfg_get("gamemode_auto", True)) if callable(cfg_get) else True
    mangohud_cfg = bool(cfg_get("mangohud", False)) if callable(cfg_get) else False
    gamemode = bool(gamemode_flag or (gamemode_auto and kind == "game"))
    mangohud = bool(mangohud_flag or mangohud_cfg)

    if runner == "umu":
        # base umu env (bakes DXVK_ASYNC=1); then the documented perf defaults, then the
        # per-title/recipe env, then an explicit --dxvk-async/--no-dxvk-async override.
        env = umu_env(prefix, slug, headless=headless, home=home, dll_overrides=dll_overrides, extra=None,
                      nvidia=nvidia)
        if "DXVK_ASYNC" in environ:
            env.pop("DXVK_ASYNC", None)  # let the user's own DXVK_ASYNC win at run time
        env.update(perf_env(kind=kind, runner=runner, environ=environ, dxvk_async=None, hdr=hdr, ntsync=ntsync))
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        if dxvk_async is True:
            env["DXVK_ASYNC"] = "1"
        elif dxvk_async is False:
            env["DXVK_ASYNC"] = "0"
        if mangohud:
            env["MANGOHUD"] = "1"
    elif runner == "bottles":
        env = {}
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
    else:
        env = wine_env(prefix, arch=arch, dll_overrides=dll_overrides, headless=headless, extra=extra_env)

    argv, warnings = build_command(runner, exe, kind, args=args, which=which, gamemode=gamemode,
                                   mangohud=mangohud, bottle=slug, gamescope=gamescope)
    cwd = str(exe.parent) if exe.parent and str(exe.parent) not in ("", ".") else None
    plan = RunPlan(runner=runner, slug=slug, prefix=prefix, exe=exe, kind=kind, argv=argv, env=env, cwd=cwd,
                   log_path=log_path_for(slug), reason=reason, gamemode=gamemode, mangohud=mangohud,
                   args=list(args), warnings=warnings)
    return plan


# ---------------------------------------------------------------------------
# Bottles helper
# ---------------------------------------------------------------------------


def ensure_bottle(name: str, *, environment: str = "application",
                  which: Callable[[str], Optional[str]] = shutil.which,
                  run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
                  log_file: Optional[Path] = None) -> bool:
    """Create the Bottles bottle ``name`` if it does not exist (needs internet on first use)."""
    flatpak = which("flatpak")
    if not flatpak:
        return False
    base = [flatpak, "run", "--command=bottles-cli", BOTTLES_APP_ID]
    try:
        proc = run(base + ["list", "bottles"], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("bottles-cli failed: %s", exc)
        return False
    listing = (proc.stdout or "") + (proc.stderr or "")
    for line in listing.splitlines():
        if line.strip().lstrip("-").strip() == name:
            return True
    log.info("Creating Bottles bottle '%s' (%s environment) - this downloads a runner the first time", name, environment)
    argv = base + ["new", "--bottle-name", name, "--environment", environment]
    try:
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"\n== bottles-cli new ({time.strftime('%Y-%m-%d %H:%M:%S')}) ==\n")
                fh.flush()
                proc = run(argv, stdout=fh, stderr=subprocess.STDOUT, timeout=3600, check=False)
        else:
            proc = run(argv, timeout=3600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("bottles-cli new failed: %s", exc)
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _write_header(fh, plan: RunPlan) -> None:  # type: ignore[no-untyped-def]
    fh.write(f"\n===== lindos-run {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.write(f"runner : {plan.runner} ({plan.reason})\n")
    fh.write(f"prefix : {plan.prefix}\n")
    fh.write(f"program: {plan.exe}\n")
    fh.write(f"cwd    : {plan.cwd}\n")
    fh.write("env    : " + " ".join(f"{k}={v}" for k, v in sorted(plan.env.items())) + "\n")
    fh.write("argv   : " + " ".join(plan.argv) + "\n")
    for w in plan.warnings:
        fh.write(f"warning: {w}\n")
    fh.write("-----\n")
    fh.flush()


def run_plan(plan: RunPlan, *, popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
             on_started: Optional[Callable[[int], None]] = None) -> int:
    """Launch the program, streaming its output to ``plan.log_path``.  Returns the exit code."""
    env = dict(os.environ)
    env.update(plan.env)
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plan.log_path, "a", encoding="utf-8", errors="replace") as fh:
        _write_header(fh, plan)
        try:
            proc = popen(plan.argv, env=env, cwd=plan.cwd, stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=subprocess.STDOUT)
        except OSError as exc:
            fh.write(f"failed to start: {exc}\n")
            log.error("Could not start the Windows program: %s", exc)
            return 127
        if on_started:
            try:
                on_started(int(getattr(proc, "pid", 0) or 0))
            except Exception:  # noqa: BLE001
                pass
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            rc = 130
        fh.write(f"----- exit code {rc} ({time.strftime('%Y-%m-%d %H:%M:%S')})\n")
    return int(rc)
