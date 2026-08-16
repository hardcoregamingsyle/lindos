"""Compatibility recipes (``/usr/share/lindos/recipes/<id>.json``, SPEC §9).

A recipe describes how to prepare a Wine prefix for a well-known Windows program:
winetricks verbs, DLL overrides, registry keys, environment, post-commands, and --
most importantly -- an honest ``status`` (works / partial / broken) with notes and
native alternatives.

``apply_recipe`` creates the prefix, runs ``winetricks -q <verbs>``, applies the
DLL overrides and registry entries with ``wine reg add``, and records env/overrides
in the apps database so ``lindos-run --prefix <slug>`` picks them up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import CoreMissing, core, expand_user_path, get_logger, path_const
from .prefix import base_wine_env, ensure_prefix, find_wine, safe_slug, wine_tool

__all__ = [
    "STATUSES",
    "RUNNERS",
    "ARCHES",
    "REQUIRED_KEYS",
    "REG_TYPES",
    "Recipe",
    "ApplyResult",
    "recipes_dir",
    "load_recipe_file",
    "load_recipes",
    "get_recipe",
    "validate_recipe",
    "apply_recipe",
    "format_recipe",
]

log = get_logger("lindos-compat.recipes")

STATUSES = ("works", "partial", "broken")
RUNNERS = ("wine", "umu", "bottles")
ARCHES = ("win32", "win64")
REQUIRED_KEYS = (
    "id", "name", "vendor", "category", "status", "notes", "runner", "arch",
    "winetricks", "dll_overrides", "env", "post_cmds", "registry",
)
REG_TYPES = ("REG_SZ", "REG_DWORD", "REG_QWORD", "REG_BINARY", "REG_EXPAND_SZ", "REG_MULTI_SZ", "REG_NONE")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

STATUS_LABEL = {"works": "works", "partial": "partial (see notes)", "broken": "does NOT work"}
STATUS_MARK = {"works": "\u2713", "partial": "\u25d0", "broken": "\u2717"}


@dataclass
class Recipe:
    id: str
    name: str
    vendor: str = ""
    category: str = ""
    status: str = "partial"
    notes: str = ""
    runner: str = "wine"
    arch: str = "win64"
    winetricks: List[str] = field(default_factory=list)
    dll_overrides: Dict[str, str] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    post_cmds: List[List[str]] = field(default_factory=list)
    registry: List[Dict[str, str]] = field(default_factory=list)
    alternatives: List[Dict[str, str]] = field(default_factory=list)
    summary: str = ""
    homepage: str = ""
    installer_hint: str = ""
    path: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object], path: str = "") -> "Recipe":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["path"] = path
        # normalise post_cmds: allow list[str] items -> [str]
        cmds = []
        for c in kwargs.get("post_cmds", []) or []:
            if isinstance(c, str):
                cmds.append([c])
            elif isinstance(c, list):
                cmds.append([str(x) for x in c])
        kwargs["post_cmds"] = cmds
        return cls(**kwargs)  # type: ignore[arg-type]

    def as_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d.pop("path", None)
        return d


@dataclass
class ApplyResult:
    ok: bool
    recipe_id: str
    prefix_slug: str = ""
    prefix_path: str = ""
    steps: List[tuple] = field(default_factory=list)  # (name, ok, detail)
    message: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "recipe": self.recipe_id,
            "prefix": self.prefix_slug,
            "prefix_path": self.prefix_path,
            "steps": [list(s) for s in self.steps],
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------


def recipes_dir() -> Path:
    override = os.environ.get("LINDOS_RECIPES_DIR")
    if override:
        return Path(override)
    return Path(path_const("RECIPES_DIR"))


def validate_recipe(data: object, *, filename: Optional[str] = None) -> List[str]:
    """Return a list of problems (empty == valid)."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["recipe is not a JSON object"]
    for key in REQUIRED_KEYS:
        if key not in data:
            errors.append(f"missing key '{key}'")
    rid = data.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        errors.append("'id' must be a lowercase slug (a-z, 0-9, '-')")
    if filename and isinstance(rid, str) and Path(filename).stem != rid:
        errors.append(f"id '{rid}' does not match filename '{Path(filename).name}'")
    if data.get("status") not in STATUSES:
        errors.append(f"'status' must be one of {STATUSES}")
    if data.get("runner") not in RUNNERS:
        errors.append(f"'runner' must be one of {RUNNERS}")
    if data.get("arch") not in ARCHES:
        errors.append(f"'arch' must be one of {ARCHES}")
    if not isinstance(data.get("winetricks", []), list) or not all(isinstance(v, str) and v for v in data.get("winetricks", [])):
        errors.append("'winetricks' must be a list of non-empty strings")
    if not isinstance(data.get("dll_overrides", {}), dict):
        errors.append("'dll_overrides' must be an object")
    if not isinstance(data.get("env", {}), dict):
        errors.append("'env' must be an object")
    if not isinstance(data.get("post_cmds", []), list):
        errors.append("'post_cmds' must be a list")
    reg = data.get("registry", [])
    if not isinstance(reg, list):
        errors.append("'registry' must be a list")
    else:
        for i, entry in enumerate(reg):
            if not isinstance(entry, dict):
                errors.append(f"registry[{i}] is not an object")
                continue
            for k in ("key", "name", "type", "value"):
                if k not in entry:
                    errors.append(f"registry[{i}] missing '{k}'")
            if entry.get("type") not in REG_TYPES:
                errors.append(f"registry[{i}] type must be one of {REG_TYPES}")
    if not isinstance(data.get("notes", ""), str) or not str(data.get("notes", "")).strip():
        errors.append("'notes' must be a non-empty string (be honest with the user)")
    if data.get("status") == "broken":
        alts = data.get("alternatives")
        if not isinstance(alts, list) or not alts:
            errors.append("broken recipes must list 'alternatives'")
    alts = data.get("alternatives", [])
    if alts is not None and not isinstance(alts, list):
        errors.append("'alternatives' must be a list")
    elif isinstance(alts, list):
        for i, alt in enumerate(alts):
            if not isinstance(alt, dict) or "name" not in alt or "how" not in alt:
                errors.append(f"alternatives[{i}] must be an object with 'name' and 'how'")
    return errors


def load_recipe_file(path: str | os.PathLike[str]) -> Recipe:
    p = Path(path)
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    problems = validate_recipe(data, filename=str(p))
    if problems:
        raise ValueError(f"{p.name}: " + "; ".join(problems))
    return Recipe.from_dict(data, path=str(p))


def load_recipes(directory: Optional[str | os.PathLike[str]] = None, *, strict: bool = False) -> Dict[str, Recipe]:
    """Load every ``*.json`` recipe; invalid files are logged and skipped unless ``strict``."""
    d = Path(directory) if directory else recipes_dir()
    out: Dict[str, Recipe] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            r = load_recipe_file(f)
        except (OSError, ValueError) as exc:
            if strict:
                raise
            log.warning("skipping invalid recipe %s: %s", f, exc)
            continue
        out[r.id] = r
    return out


def get_recipe(recipe_id: str, directory: Optional[str | os.PathLike[str]] = None) -> Optional[Recipe]:
    d = Path(directory) if directory else recipes_dir()
    f = d / f"{recipe_id}.json"
    if not f.is_file():
        return None
    try:
        return load_recipe_file(f)
    except (OSError, ValueError) as exc:
        log.error("recipe %s is invalid: %s", f, exc)
        return None


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def format_recipe(r: Recipe, *, verbose: bool = True) -> str:
    lines = [
        f"{r.name}  [{r.id}]",
        f"  Vendor:   {r.vendor or '-'}",
        f"  Category: {r.category or '-'}",
        f"  Status:   {STATUS_MARK.get(r.status, '?')} {STATUS_LABEL.get(r.status, r.status)}",
        f"  Runner:   {r.runner} ({r.arch})",
    ]
    if r.summary:
        lines.append(f"  Summary:  {r.summary}")
    if verbose:
        if r.winetricks:
            lines.append("  Winetricks: " + " ".join(r.winetricks))
        if r.dll_overrides:
            lines.append("  DLL overrides: " + ", ".join(f"{k}={v}" for k, v in r.dll_overrides.items()))
        if r.env:
            lines.append("  Environment: " + ", ".join(f"{k}={v}" for k, v in r.env.items()))
        if r.registry:
            lines.append("  Registry: " + "; ".join(f"{e.get('key')}\\{e.get('name')}={e.get('value')}" for e in r.registry))
        if r.installer_hint:
            lines.append(f"  Installer: {r.installer_hint}")
        lines.append("  Notes:")
        for para in r.notes.split("\n"):
            lines.append("    " + para)
        if r.alternatives:
            lines.append("  Alternatives:")
            for alt in r.alternatives:
                lines.append(f"    - {alt.get('name')}: {alt.get('how')}")
        if r.homepage:
            lines.append(f"  Homepage: {r.homepage}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _reg_add_argv(wine: Sequence[str], entry: Dict[str, str]) -> List[str]:
    argv = list(wine) + ["reg", "add", str(entry["key"]), "/v", str(entry["name"]), "/t", str(entry["type"]),
                         "/d", str(entry["value"]), "/f"]
    return argv


def apply_recipe(
    recipe: Recipe,
    *,
    prefix_slug: Optional[str] = None,
    fresh: bool = False,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    on_progress: Optional[Callable[[str], None]] = None,
    log_file: Optional[Path] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Prepare a prefix for ``recipe`` (see module docstring)."""
    slug = safe_slug(prefix_slug or recipe.id)
    result = ApplyResult(ok=False, recipe_id=recipe.id, prefix_slug=slug)

    if recipe.status == "broken":
        alts = "; ".join(f"{a.get('name')}: {a.get('how')}" for a in recipe.alternatives) or "none"
        result.message = (
            f"'{recipe.name}' does NOT work through Wine/Proton on Lindos (or any Linux) - refusing to set it up.\n"
            f"Why: {recipe.notes.splitlines()[0] if recipe.notes else 'see notes'}\n"
            f"Use instead: {alts}"
        )
        result.steps.append(("refuse", False, "recipe status is 'broken'"))
        return result

    if dry_run:
        result.steps.append(("plan", True, f"prefix {slug} ({recipe.arch}), runner {recipe.runner}, "
                                           f"winetricks {' '.join(recipe.winetricks) or '-'}"))
        result.ok = True
        result.message = "dry run"
        return result

    if log_file is None:
        log_file = expand_user_path(path_const("LOG_DIR")) / f"recipe-{recipe.id}.log"

    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    use_umu = recipe.runner == "umu" and bool(which("umu-run"))
    if recipe.runner == "bottles":
        log.info("Recipe prefers Bottles; preparing a plain Wine prefix instead (Bottles has no non-interactive "
                 "dependency installer). You can also recreate it in Bottles with the same winetricks verbs.")

    # 1. prefix
    state = ensure_prefix(slug, runner="wine", arch=recipe.arch, fresh=fresh, dll_overrides=recipe.dll_overrides,
                          headless=headless, which=which, run=run, on_progress=on_progress, log_file=log_file,
                          recipe_id=recipe.id)
    result.prefix_path = str(state.path)
    for w in state.warnings:
        result.steps.append(("prefix", False, w))
    if not use_umu and not state.initialized:
        result.steps.append(("prefix", False, "Wine could not create the C:\\ drive (is Wine installed? run: lindos-compat doctor)"))
        result.message = "prefix creation failed"
        return result
    result.steps.append(("prefix", True, f"{state.path} ({state.arch})" + (" created" if state.created else " reused")))

    env = dict(os.environ)
    if use_umu:
        from .runner import umu_env  # local import to avoid a cycle at import time
        env.update(umu_env(state.path, slug, headless=headless))
        wine_argv: List[str] = [str(which("umu-run"))]
    else:
        env.update(base_wine_env(state.path, arch=state.arch, dll_overrides=recipe.dll_overrides, headless=headless))
        wine = find_wine(which)
        if not wine:
            result.steps.append(("wine", False, "wine binary not found"))
            result.message = "Wine is not installed"
            return result
        wine_argv = [wine]
    for k, v in recipe.env.items():
        env[str(k)] = str(v)

    def _run(argv: List[str], step: str, timeout: int = 3600) -> bool:
        if on_progress:
            on_progress(f"{recipe.name}: {step}...")
        log.info("%s: %s", step, " ".join(argv))
        try:
            with open(log_file, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"\n== {step} ({time.strftime('%Y-%m-%d %H:%M:%S')}) ==\n$ {' '.join(argv)}\n")
                fh.flush()
                proc = run(argv, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            result.steps.append((step, False, str(exc)))
            return False
        ok = proc.returncode == 0
        result.steps.append((step, ok, "ok" if ok else f"exit code {proc.returncode} (log: {log_file})"))
        return ok

    # 2. winetricks
    if recipe.winetricks:
        wt = which("winetricks")
        if not wt:
            result.steps.append(("winetricks", False, "winetricks is not installed (apt install winetricks)"))
        else:
            argv = ([wine_argv[0], wt] if use_umu else [wt]) + ["-q"] + list(recipe.winetricks)
            if not use_umu:
                env.setdefault("WINE", wine_argv[0])
            _run(argv, "winetricks " + " ".join(recipe.winetricks), timeout=7200)

    # 3. DLL overrides (persist in the registry so plain `wine` picks them up too)
    for dll, mode in recipe.dll_overrides.items():
        _run(_reg_add_argv(wine_argv, {"key": "HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides", "name": dll,
                                       "type": "REG_SZ", "value": mode}), f"dll override {dll}={mode}", timeout=300)

    # 4. registry
    for entry in recipe.registry:
        _run(_reg_add_argv(wine_argv, entry), f"registry {entry.get('key')}\\{entry.get('name')}", timeout=300)

    # 5. post commands (argv lists; the token "wine" means the wine binary)
    for cmd in recipe.post_cmds:
        argv = []
        for tok in cmd:
            if tok == "wine":
                argv.extend(wine_argv)
            elif tok == "winetricks":
                argv.append(which("winetricks") or "winetricks")
            else:
                argv.append(os.path.expandvars(tok).replace("$WINEPREFIX", str(state.path)))
        _run(argv, "post command " + " ".join(cmd), timeout=3600)

    # 6. apps database entry: env + overrides for `lindos-run --prefix <slug>`
    try:
        c = core()
        db = c.apps_db_load()
        if not isinstance(db, dict):
            db = {}
        entry_key = f"recipe-{recipe.id}" if slug == recipe.id else f"recipe-{recipe.id}-{slug}"
        db[entry_key] = {
            "name": recipe.name,
            "exe": "",
            "prefix": slug,
            "runner": "umu" if use_umu else "wine",
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "recipe",
            "recipe": recipe.id,
            "status": recipe.status,
            "arch": state.arch,
            "env": dict(recipe.env),
            "dll_overrides": dict(recipe.dll_overrides),
        }
        c.apps_db_save(db)
        result.steps.append(("apps-db", True, entry_key))
    except CoreMissing as exc:
        result.steps.append(("apps-db", False, str(exc)))
    except Exception as exc:  # noqa: BLE001
        result.steps.append(("apps-db", False, f"could not save apps database: {exc}"))

    failed = [s for s in result.steps if not s[1]]
    result.ok = not failed
    result.message = (
        f"Prefix '{slug}' is ready for {recipe.name}. Next: run the program's installer with\n"
        f"  lindos-run --prefix {slug} /path/to/Setup.exe"
    ) if result.ok else f"{len(failed)} step(s) failed - see {log_file}"
    return result
