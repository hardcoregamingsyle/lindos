"""Pure planning logic for the Lindos first-boot setup (OOBE).

This module deliberately imports **no** GTK (``gi``) and **no** ``lindos``
modules at import time so it can be unit-tested on any OS.  It turns the
user's :class:`Selections` into a :class:`Plan` -- an ordered list of
:class:`Step` objects -- and provides a :class:`Runner` that executes those
steps through injectable executors (real ones live in
``lindos_setup.core``; tests and ``--dry-run`` inject fakes / printers).

Plan JSON shape (``Plan.to_json()``)::

    {
      "schema": 1,
      "selections": {...Selections...},
      "notes": ["human readable remarks"],
      "steps": [
        {"id": "write-config", "title": "...", "kind": "user",
         "action": "write-config", "payload": {...}},
        ...
      ]
    }

Step kinds:

* ``user``   -- runs as the logged-in user (config, theme, wallpaper, ...)
* ``system`` -- needs privileges; one step == one helper call
  (``write-system-config``, ``apply-mode`` with ``install: false``).

The plan is **install-free**: the Lindos installer already installed the browser, drivers, apps and
updates (its record is ``/var/lib/lindos/install-state.json``), so the wizard only saves choices.
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union

log = logging.getLogger("lindos-setup.plan")

SCHEMA = 1

# --- Constants mirroring SPEC §2 / §3 / §4 (values must stay identical) ------
MODE_IDS: List[str] = ["everyday", "gaming", "work", "creator", "lite"]
BROWSER_IDS: List[str] = ["edge", "chrome", "firefox"]
THEMES: List[str] = ["dark", "light"]
TASKBAR_ALIGNMENTS: List[str] = ["center", "left"]
DEFAULT_ACCENT = "#60CDFF"
WALLPAPER_DIR = "/usr/share/backgrounds/lindos"
DEFAULT_WALLPAPER = WALLPAPER_DIR + "/aurora-dark.svg"
LIGHT_WALLPAPER = WALLPAPER_DIR + "/aurora-light.svg"
WALLPAPER_NAMES: List[str] = [
    "aurora-dark.svg", "aurora-light.svg", "bloom-blue.svg", "mist-purple.svg", "nightfall.svg",
]
APPS_JSON_REL = "usr/share/lindos/setup/apps.json"   # reference catalog; the wizard no longer installs from it
ACCENTS_JSON_REL = "usr/share/lindos/setup/accents.json"

KIND_USER = "user"
KIND_SYSTEM = "system"
STEP_KINDS = (KIND_USER, KIND_SYSTEM)

# user-side actions (executed in-process through lindos-core APIs)
ACT_WRITE_CONFIG = "write-config"
ACT_SET_THEME = "set-theme"
ACT_SET_ACCENT = "set-accent"
ACT_SET_WALLPAPER = "set-wallpaper"
ACT_SET_TASKBAR = "set-taskbar-alignment"
ACT_SET_DEFAULT_BROWSER = "set-default-browser"
# privileged actions (helper action names from SPEC §4.6, one call each)
ACT_WRITE_SYSTEM_CONFIG = "write-system-config"
ACT_APPLY_MODE = "apply-mode"

SYSTEM_ACTION_ORDER: List[str] = [ACT_WRITE_SYSTEM_CONFIG, ACT_APPLY_MODE]

APP_KINDS = ("apt", "flatpak", "script")
SCRIPT_ACTIONS = ("install-compat", "install-gaming")   # helper actions the reference catalog may name

# install-state step ids (lindos.installstate.STEPS) with the friendly names the Done page shows
INSTALL_STEP_ORDER: Tuple[str, ...] = (
    "updates", "drivers", "browser", "compat", "gaming", "mode_extras", "flatpaks")
INSTALL_STEP_NAMES: Dict[str, str] = {
    "updates": "System updates",
    "drivers": "Drivers and firmware",
    "browser": "Google Chrome",
    "compat": "Windows app support (Wine + Proton)",
    "gaming": "Gaming launchers",
    "mode_extras": "Apps for the Modes",
    "flatpaks": "Flatpak apps (Prism, Sober, Heroic, Bottles)",
}
# browser states shown on the Browser page (see core.browser_state)
BROWSER_INSTALLED = "installed"
BROWSER_PENDING = "pending"          # not on this PC yet; Lindos adds it once it is online
BROWSER_UNAVAILABLE = "unavailable"  # not installed and nothing is going to install it: not offered

# Selections.transfer["source_type"] (SPEC-WINDOWS §29.3: "partition" | "bundle"; "" = no source
# chosen / skipped).  Nothing is ever copied during OOBE -- see the `transfer` page and DonePage.
TRANSFER_SOURCE_TYPES: List[str] = ["", "partition", "bundle"]

FALLBACK_ACCENTS: List[Dict[str, str]] = [
    {"id": "aurora-blue", "name": "Aurora Blue", "hex": "#60CDFF"},
    {"id": "classic-blue", "name": "Classic Blue", "hex": "#0067C0"},
    {"id": "meadow-green", "name": "Meadow Green", "hex": "#6CCB5F"},
    {"id": "violet", "name": "Violet", "hex": "#B4A0FF"},
    {"id": "rose", "name": "Rose", "hex": "#FF99A4"},
    {"id": "sunset-orange", "name": "Sunset Orange", "hex": "#FF9E5A"},
    {"id": "teal", "name": "Teal", "hex": "#4CC2C2"},
    {"id": "gold", "name": "Gold", "hex": "#F2C94C"},
]


# --- Data file lookup ---------------------------------------------------------
def _package_root() -> str:
    """Return the ``root/`` directory this package tree lives in.

    ``plan.py`` is installed at ``<root>/usr/lib/lindos-setup/lindos_setup/plan.py``;
    walking four levels up yields ``<root>`` (``/`` on an installed system,
    ``packages/lindos-setup/root`` inside the repository).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = here
    for _ in range(4):
        root = os.path.dirname(root)
    return root


def data_file_candidates(rel: str) -> List[str]:
    """Candidate absolute paths for a data file, most specific first."""
    rel = rel.lstrip("/")
    cands: List[str] = []
    lindos_root = os.environ.get("LINDOS_ROOT", "").strip()
    if lindos_root:
        cands.append(os.path.join(lindos_root, rel))
    cands.append(os.path.join(_package_root(), rel))
    cands.append("/" + rel)
    # de-duplicate, keep order
    seen = set()
    out: List[str] = []
    for c in cands:
        c = os.path.normpath(c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_data_file(rel: str) -> Optional[str]:
    """First existing candidate for ``rel`` (see :func:`data_file_candidates`)."""
    for cand in data_file_candidates(rel):
        if os.path.isfile(cand):
            return cand
    return None


# --- Selections ---------------------------------------------------------------
def _is_hex_colour(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


@dataclass
class Selections:
    """Everything the user chose in the wizard.  Defaults mirror SPEC §4.2."""

    mode: str = "everyday"
    browser: str = "chrome"
    theme: str = "dark"
    accent: str = DEFAULT_ACCENT
    wallpaper: str = DEFAULT_WALLPAPER
    taskbar_alignment: str = "center"
    location: bool = False
    crash_reports: bool = False
    # optional OOBE "transfer" page choice (SPEC-WINDOWS §32): never copies anything itself --
    # it only records what the Done page should hand to `lindos-transfer-gui --from <source>`.
    transfer: Dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "source_type": "", "source": ""})

    # -- validation / helpers -------------------------------------------------
    def validate(self) -> None:
        """Raise ``ValueError`` on anything outside the SPEC vocabularies."""
        if self.mode not in MODE_IDS:
            raise ValueError("unknown mode %r (expected one of %s)" % (self.mode, MODE_IDS))
        if self.browser not in BROWSER_IDS:
            raise ValueError("unknown browser %r (expected one of %s)" % (self.browser, BROWSER_IDS))
        if self.theme not in THEMES:
            raise ValueError("unknown theme %r (expected one of %s)" % (self.theme, THEMES))
        if not _is_hex_colour(self.accent):
            raise ValueError("accent must be '#RRGGBB', got %r" % (self.accent,))
        if self.taskbar_alignment not in TASKBAR_ALIGNMENTS:
            raise ValueError("unknown taskbar alignment %r" % (self.taskbar_alignment,))
        if not isinstance(self.wallpaper, str) or not self.wallpaper:
            raise ValueError("wallpaper must be a non-empty path")
        if not isinstance(self.location, bool) or not isinstance(self.crash_reports, bool):
            raise ValueError("location / crash_reports must be booleans")
        if not isinstance(self.transfer, dict):
            raise ValueError("transfer must be an object")
        if not isinstance(self.transfer.get("enabled", False), bool):
            raise ValueError("transfer['enabled'] must be a boolean")
        if self.transfer.get("source_type", "") not in TRANSFER_SOURCE_TYPES:
            raise ValueError("transfer['source_type'] must be one of %s" % TRANSFER_SOURCE_TYPES)
        if not isinstance(self.transfer.get("source", ""), str):
            raise ValueError("transfer['source'] must be a string")

    @property
    def dark(self) -> bool:
        return self.theme == "dark"

    def copy(self) -> "Selections":
        return Selections.from_dict(self.as_dict())

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["transfer"] = dict(self.transfer)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Selections":
        """Build from a dict, ignoring unknown keys (forward compatible).

        Old selection files still carry an ``apps`` list from the time the wizard installed apps;
        it is ignored like any other unknown key."""
        sel = cls()
        for key in ("mode", "browser", "theme", "accent", "wallpaper", "taskbar_alignment"):
            if key in data and data[key] is not None:
                setattr(sel, key, str(data[key]))
        for key in ("location", "crash_reports"):
            if key in data and data[key] is not None:
                setattr(sel, key, bool(data[key]))
        transfer = data.get("transfer")
        if isinstance(transfer, dict):
            sel.transfer = {
                "enabled": bool(transfer.get("enabled", False)),
                "source_type": str(transfer.get("source_type", "") or ""),
                "source": str(transfer.get("source", "") or ""),
            }
        return sel

    def set_theme(self, theme: str, *, follow_wallpaper: bool = True) -> None:
        """Change theme; swap the aurora wallpaper to the matching variant when
        the user has not picked a custom one."""
        if theme not in THEMES:
            raise ValueError("unknown theme %r" % (theme,))
        old = self.theme
        self.theme = theme
        if follow_wallpaper and old != theme:
            if theme == "light" and self.wallpaper == DEFAULT_WALLPAPER:
                self.wallpaper = LIGHT_WALLPAPER
            elif theme == "dark" and self.wallpaper == LIGHT_WALLPAPER:
                self.wallpaper = DEFAULT_WALLPAPER


def default_wallpaper_for(theme: str) -> str:
    return LIGHT_WALLPAPER if theme == "light" else DEFAULT_WALLPAPER


# --- Apps catalog (reference data) ---------------------------------------------
# ``apps.json`` still ships (helper item whitelists are checked against it in tests/), but the wizard
# no longer offers or installs apps: the installer does that.  Nothing in the OOBE reads these classes.
@dataclass
class AppEntry:
    """One optional app from ``apps.json``."""

    id: str
    name: str
    description: str = ""
    kind: str = "apt"                     # apt | flatpak | script
    packages: List[str] = field(default_factory=list)   # kind == apt
    flatpaks: List[str] = field(default_factory=list)   # kind == flatpak
    action: str = ""                      # kind == script: install-compat | install-gaming
    items: List[str] = field(default_factory=list)      # kind == script
    default_on_modes: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppEntry":
        if not isinstance(d, dict):
            raise ValueError("app entry must be an object")
        app_id = str(d.get("id", "")).strip()
        if not app_id:
            raise ValueError("app entry without id")
        kind = str(d.get("kind", "apt")).strip()
        if kind not in APP_KINDS:
            raise ValueError("app %r: unknown kind %r" % (app_id, kind))
        entry = cls(
            id=app_id,
            name=str(d.get("name", app_id)),
            description=str(d.get("description", "")),
            kind=kind,
            packages=[str(p) for p in d.get("packages", []) or []],
            flatpaks=[str(p) for p in d.get("flatpaks", []) or []],
            action=str(d.get("action", "") or ""),
            items=[str(p) for p in d.get("items", []) or []],
            default_on_modes=[str(m) for m in d.get("default_on_modes", []) or []],
        )
        if kind == "apt" and not entry.packages:
            raise ValueError("app %r: kind apt needs 'packages'" % app_id)
        if kind == "flatpak" and not entry.flatpaks:
            raise ValueError("app %r: kind flatpak needs 'flatpaks'" % app_id)
        if kind == "script":
            if entry.action not in SCRIPT_ACTIONS:
                raise ValueError("app %r: script action must be one of %s" % (app_id, SCRIPT_ACTIONS))
            if not entry.items:
                raise ValueError("app %r: kind script needs 'items'" % app_id)
        return entry

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def default_on(self, mode: str) -> bool:
        return mode in self.default_on_modes


class Catalog:
    """Ordered collection of :class:`AppEntry`."""

    def __init__(self, apps: Optional[Iterable[AppEntry]] = None, source: str = "") -> None:
        self.apps: List[AppEntry] = list(apps or [])
        self.source = source
        ids = [a.id for a in self.apps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError("duplicate app ids in catalog: %s" % ", ".join(dupes))

    def __iter__(self) -> Iterator[AppEntry]:
        return iter(self.apps)

    def __len__(self) -> int:
        return len(self.apps)

    def __contains__(self, app_id: object) -> bool:
        return any(a.id == app_id for a in self.apps)

    def ids(self) -> List[str]:
        return [a.id for a in self.apps]

    def get(self, app_id: str) -> Optional[AppEntry]:
        for a in self.apps:
            if a.id == app_id:
                return a
        return None

    def default_ids(self, mode: str) -> List[str]:
        """App ids pre-checked for ``mode`` (catalog order)."""
        return [a.id for a in self.apps if a.default_on(mode)]

    def names(self, ids: Iterable[str]) -> List[str]:
        out: List[str] = []
        for i in ids:
            a = self.get(i)
            out.append(a.name if a else i)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: str = "") -> "Catalog":
        if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
            raise ValueError("apps catalog must be {'schema': 1, 'apps': [...]}")
        return cls([AppEntry.from_dict(d) for d in data["apps"]], source=source)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": SCHEMA, "apps": [a.to_dict() for a in self.apps]}


def load_catalog(path: Optional[str] = None) -> Catalog:
    """Load ``apps.json``.  Missing/invalid file -> empty catalog (logged)."""
    if path is None:
        path = find_data_file(APPS_JSON_REL)
    if not path or not os.path.isfile(path):
        log.warning("apps catalog not found (looked for %s)", ", ".join(data_file_candidates(APPS_JSON_REL)))
        return Catalog([], source="")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return Catalog.from_dict(data, source=path)
    except (OSError, ValueError) as exc:
        log.error("apps catalog %s unusable: %s", path, exc)
        return Catalog([], source=path)


def load_accents(path: Optional[str] = None) -> List[Dict[str, str]]:
    """Load ``accents.json`` -> list of {id,name,hex}. Falls back to the built-in 8."""
    if path is None:
        path = find_data_file(ACCENTS_JSON_REL)
    if not path or not os.path.isfile(path):
        log.warning("accents.json not found, using built-in accents")
        return [dict(a) for a in FALLBACK_ACCENTS]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        accents = data.get("accents") if isinstance(data, dict) else None
        out: List[Dict[str, str]] = []
        for a in accents or []:
            if not isinstance(a, dict):
                continue
            hexv = str(a.get("hex", "")).strip().upper()
            if not _is_hex_colour(hexv):
                log.warning("accents.json: skipping %r (bad hex)", a)
                continue
            out.append({"id": str(a.get("id", hexv)), "name": str(a.get("name", hexv)), "hex": hexv})
        if not out:
            raise ValueError("no valid accents")
        return out
    except (OSError, ValueError) as exc:
        log.error("accents.json %s unusable (%s); using built-in accents", path, exc)
        return [dict(a) for a in FALLBACK_ACCENTS]


# --- Steps & Plan -------------------------------------------------------------
@dataclass
class Step:
    id: str
    title: str
    kind: str                       # KIND_USER | KIND_SYSTEM
    action: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in STEP_KINDS:
            raise ValueError("step %r: kind must be one of %s" % (self.id, STEP_KINDS))
        if not self.id or not self.action:
            raise ValueError("step needs id and action")
        if self.payload is None:
            self.payload = {}

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "kind": self.kind,
                "action": self.action, "payload": json.loads(json.dumps(self.payload))}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        return cls(id=str(d["id"]), title=str(d.get("title", d["id"])), kind=str(d["kind"]),
                   action=str(d["action"]), payload=dict(d.get("payload") or {}))

    @property
    def is_system(self) -> bool:
        return self.kind == KIND_SYSTEM


class Plan:
    """Ordered steps + the selections they were built from."""

    def __init__(self, steps: Optional[Iterable[Step]] = None,
                 selections: Optional[Selections] = None,
                 notes: Optional[Iterable[str]] = None) -> None:
        self.steps: List[Step] = list(steps or [])
        self.selections: Selections = selections if selections is not None else Selections()
        self.notes: List[str] = list(notes or [])
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids in plan")

    # -- container protocol ---------------------------------------------------
    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def get(self, step_id: str) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def actions(self) -> List[str]:
        return [s.action for s in self.steps]

    def user_steps(self) -> List[Step]:
        return [s for s in self.steps if s.kind == KIND_USER]

    def system_steps(self) -> List[Step]:
        return [s for s in self.steps if s.kind == KIND_SYSTEM]

    def system_payloads(self) -> List[Tuple[str, Dict[str, Any]]]:
        """``[(helper_action, payload), ...]`` -- one entry per privileged group."""
        return [(s.action, dict(s.payload)) for s in self.system_steps()]

    def has_action(self, action: str) -> bool:
        return any(s.action == action for s in self.steps)

    # -- (de)serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "selections": self.selections.as_dict(),
            "notes": list(self.notes),
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        if not isinstance(data, dict):
            raise ValueError("plan must be an object")
        schema = int(data.get("schema", SCHEMA))
        if schema != SCHEMA:
            raise ValueError("unsupported plan schema %s" % schema)
        steps = [Step.from_dict(s) for s in data.get("steps", []) or []]
        sel = Selections.from_dict(data.get("selections") or {})
        notes = [str(n) for n in data.get("notes", []) or []]
        return cls(steps, sel, notes)

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_selections(cls, selections: Selections, catalog: Optional[Catalog] = None,
                        *, online: bool = True,
                        browser_states: Optional[Dict[str, str]] = None) -> "Plan":
        return build_plan(selections, catalog, online=online, browser_states=browser_states)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Plan):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return "Plan(%d steps, mode=%s, browser=%s)" % (
            len(self.steps), self.selections.mode, self.selections.browser)


def build_plan(selections: Selections, catalog: Optional[Catalog] = None,
               *, online: bool = True,
               browser_states: Optional[Dict[str, str]] = None) -> Plan:
    """Turn selections into an ordered, **install-free** :class:`Plan`.

    Every step saves configuration; nothing is downloaded or installed (the installer did that).
    ``catalog`` and ``online`` are accepted only for callers written against the old signature
    and are ignored.  ``browser_states`` (``{browser id: installed|pending|unavailable}``, see
    ``core.browser_states``) lets the plan say so when the chosen browser is still *pending*: the
    choice is stored as the preferred browser and becomes the default once it has been added.
    """
    sel = selections.copy()
    sel.validate()
    notes: List[str] = []
    steps: List[Step] = []
    browser_pending = (browser_states or {}).get(sel.browser) == BROWSER_PENDING

    # ---- user side ---------------------------------------------------------
    steps.append(Step(
        id="write-config", title="Save your choices", kind=KIND_USER, action=ACT_WRITE_CONFIG,
        payload={
            "mode": sel.mode,
            "browser": sel.browser,
            "theme": sel.theme,
            "accent": sel.accent,
            "wallpaper": sel.wallpaper,
            "taskbar_alignment": sel.taskbar_alignment,
            "telemetry": bool(sel.crash_reports),
            "location_services": bool(sel.location),
        }))
    steps.append(Step(
        id="set-theme", title="Apply %s theme" % ("dark" if sel.dark else "light"),
        kind=KIND_USER, action=ACT_SET_THEME, payload={"dark": sel.dark, "theme": sel.theme}))
    steps.append(Step(
        id="set-accent", title="Apply accent colour", kind=KIND_USER, action=ACT_SET_ACCENT,
        payload={"accent": sel.accent}))
    steps.append(Step(
        id="set-wallpaper", title="Set wallpaper", kind=KIND_USER, action=ACT_SET_WALLPAPER,
        payload={"path": sel.wallpaper}))
    steps.append(Step(
        id="set-taskbar-alignment", title="Align taskbar (%s)" % sel.taskbar_alignment,
        kind=KIND_USER, action=ACT_SET_TASKBAR, payload={"alignment": sel.taskbar_alignment}))

    # ---- privileged side (one batched helper run; configuration only) -------
    steps.append(Step(
        id="write-system-config", title="Save system defaults", kind=KIND_SYSTEM,
        action=ACT_WRITE_SYSTEM_CONFIG, payload={"mode": sel.mode, "browser": sel.browser}))
    steps.append(Step(
        id="apply-mode", title="Apply %s mode" % sel.mode.capitalize(), kind=KIND_SYSTEM,
        action=ACT_APPLY_MODE, payload={"mode": sel.mode, "install": False}))

    # ---- final user side ---------------------------------------------------
    if browser_pending:
        name = INSTALL_STEP_NAMES["browser"] if sel.browser == "chrome" else sel.browser.capitalize()
        steps.append(Step(
            id="set-default-browser", title="Use %s as the default once it is added" % name,
            kind=KIND_USER, action=ACT_SET_DEFAULT_BROWSER,
            payload={"browser": sel.browser, "pending": True}))
        notes.append(
            "%s isn't on this PC yet. It will be added when you're online and then becomes your "
            "default browser; until then you keep using Firefox." % name)
    else:
        steps.append(Step(
            id="set-default-browser", title="Make %s the default browser" % sel.browser.capitalize(),
            kind=KIND_USER, action=ACT_SET_DEFAULT_BROWSER, payload={"browser": sel.browser}))

    return Plan(steps, sel, notes)


# --- Runner -------------------------------------------------------------------
LogFn = Callable[[str], None]
ExecutorResult = Union[bool, None, Tuple[bool, str]]
Executor = Callable[[Step, LogFn], ExecutorResult]


@dataclass
class StepResult:
    step_id: str
    ok: bool
    message: str = ""
    skipped: bool = False
    seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return not self.ok and not self.skipped


@dataclass
class RunResult:
    results: List[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing *failed* (skipped steps do not count as failure)."""
        return all(r.ok or r.skipped for r in self.results)

    @property
    def failed_ids(self) -> List[str]:
        return [r.step_id for r in self.results if r.failed]

    @property
    def skipped_ids(self) -> List[str]:
        return [r.step_id for r in self.results if r.skipped]

    @property
    def succeeded_ids(self) -> List[str]:
        return [r.step_id for r in self.results if r.ok and not r.skipped]

    def get(self, step_id: str) -> Optional[StepResult]:
        for r in self.results:
            if r.step_id == step_id:
                return r
        return None

    def summary(self) -> str:
        n_ok = len(self.succeeded_ids)
        n_skip = len(self.skipped_ids)
        n_fail = len(self.failed_ids)
        return "%d done, %d skipped, %d failed" % (n_ok, n_skip, n_fail)


def _normalise_result(value: ExecutorResult) -> Tuple[bool, str]:
    if value is None:
        return True, ""
    if isinstance(value, tuple):
        ok = bool(value[0])
        msg = str(value[1]) if len(value) > 1 and value[1] is not None else ""
        return ok, msg
    return bool(value), ""


class Runner:
    """Execute a :class:`Plan` step by step with injectable executors.

    ``executors`` maps ``step.action`` -> callable ``(step, log) -> bool | None |
    (bool, message)``.  Every step is isolated: exceptions are caught, logged
    and recorded as a failure, and the runner continues with the next step
    (unless ``stop_on_failure``).  Actions without an executor are recorded as
    *skipped* (never as failed) so a partial tool-set degrades gracefully.
    """

    def __init__(self, plan: Plan, executors: Dict[str, Executor], *,
                 log: Optional[LogFn] = None,
                 on_step_start: Optional[Callable[[int, int, Step], None]] = None,
                 on_step_done: Optional[Callable[[int, int, Step, StepResult], None]] = None,
                 stop_on_failure: bool = False) -> None:
        self.plan = plan
        self.executors = dict(executors)
        self._log: LogFn = log if log is not None else (lambda msg: log_module_default(msg))
        self.on_step_start = on_step_start
        self.on_step_done = on_step_done
        self.stop_on_failure = stop_on_failure
        self.result = RunResult()
        self._cancel = False

    def cancel(self) -> None:
        """Ask the runner to stop before the next step (thread-safe flag)."""
        self._cancel = True

    def log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:  # logging must never break the run
            pass

    def run_step(self, step: Step) -> StepResult:
        executor = self.executors.get(step.action)
        started = time.monotonic()
        if executor is None:
            res = StepResult(step.id, ok=False, skipped=True,
                             message="no executor for action %r" % step.action)
            self.log("[skip] %s: %s" % (step.title, res.message))
            return res
        try:
            ok, msg = _normalise_result(executor(step, self.log))
            res = StepResult(step.id, ok=ok, message=msg, seconds=time.monotonic() - started)
            if ok:
                self.log("[ok] %s%s" % (step.title, (" - " + msg) if msg else ""))
            else:
                self.log("[failed] %s%s" % (step.title, (" - " + msg) if msg else ""))
            return res
        except Exception as exc:  # failure isolation: never propagate
            tb = traceback.format_exc()
            log.error("step %s (%s) raised: %s\n%s", step.id, step.action, exc, tb)
            res = StepResult(step.id, ok=False, message="%s: %s" % (type(exc).__name__, exc),
                             seconds=time.monotonic() - started)
            self.log("[failed] %s - %s" % (step.title, res.message))
            return res

    def run(self) -> RunResult:
        self.result = RunResult()
        total = len(self.plan.steps)
        for index, step in enumerate(self.plan.steps):
            if self._cancel:
                res = StepResult(step.id, ok=False, skipped=True, message="cancelled")
                self.result.results.append(res)
                continue
            if self.on_step_start is not None:
                try:
                    self.on_step_start(index, total, step)
                except Exception:
                    log.debug("on_step_start callback failed", exc_info=True)
            res = self.run_step(step)
            self.result.results.append(res)
            if self.on_step_done is not None:
                try:
                    self.on_step_done(index, total, step, res)
                except Exception:
                    log.debug("on_step_done callback failed", exc_info=True)
            if res.failed and self.stop_on_failure:
                self.log("stopping after failure in %s" % step.id)
                break
        self.log("Setup steps finished: %s" % self.result.summary())
        return self.result


def log_module_default(msg: str) -> None:
    log.info("%s", msg)


def make_printing_executors(plan: Plan, write: Optional[Callable[[str], None]] = None,
                            prefix: str = "[dry-run]") -> Dict[str, Executor]:
    """Executors for ``--dry-run``: describe what *would* happen, do nothing.

    Lines go to ``write`` when given, otherwise to the runner's log function.
    """

    def _make(action: str) -> Executor:
        def _exec(step: Step, logf: LogFn) -> ExecutorResult:
            line = "%s %s %s %s" % (prefix, step.kind, step.action,
                                    json.dumps(step.payload, sort_keys=True, ensure_ascii=False))
            (write if write is not None else logf)(line)
            return True, "dry-run"
        return _exec

    return {action: _make(action) for action in sorted(set(plan.actions()))}


def make_recording_executors(actions: Iterable[str], record: List[Tuple[str, Dict[str, Any]]],
                             fail: Iterable[str] = (), raise_on: Iterable[str] = ()) -> Dict[str, Executor]:
    """Fake executors for tests: append ``(action, payload)`` to ``record``;
    return False for actions in ``fail``; raise for actions in ``raise_on``."""
    fail = set(fail)
    raise_on = set(raise_on)

    def _make(action: str) -> Executor:
        def _exec(step: Step, logf: LogFn) -> ExecutorResult:
            record.append((step.action, dict(step.payload)))
            if action in raise_on:
                raise RuntimeError("boom in %s" % action)
            if action in fail:
                return False, "simulated failure"
            return True
        return _exec

    return {a: _make(a) for a in actions}


def summarize(selections: Selections, mode_names: Optional[Dict[str, str]] = None,
              browser_names: Optional[Dict[str, str]] = None,
              accent_names: Optional[Dict[str, str]] = None,
              browser_states: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """Human-readable ``[(label, value), ...]`` rows for the summary page."""
    mode_names = mode_names or {}
    browser_names = browser_names or {}
    accent_names = accent_names or {}
    wallpaper = os.path.splitext(os.path.basename(selections.wallpaper))[0].replace("-", " ").title()
    accent_label = accent_names.get(selections.accent.upper(), selections.accent.upper())
    if accent_label != selections.accent.upper():
        accent_label = "%s (%s)" % (accent_label, selections.accent.upper())
    transfer = selections.transfer or {}
    if transfer.get("enabled"):
        transfer_label = "Yes — Transfer tool opens after setup finishes"
    else:
        transfer_label = "Not now"
    browser_label = browser_names.get(selections.browser, selections.browser.capitalize())
    if (browser_states or {}).get(selections.browser) == BROWSER_PENDING:
        browser_label += " — will be added when you're online (Firefox until then)"
    return [
        ("Mode", mode_names.get(selections.mode, selections.mode.capitalize())),
        ("Browser", browser_label),
        ("Theme", "Dark" if selections.dark else "Light"),
        ("Accent", accent_label),
        ("Wallpaper", wallpaper),
        ("Taskbar", selections.taskbar_alignment.capitalize()),
        ("Bring your files from Windows", transfer_label),
        ("Location services", "On" if selections.location else "Off"),
        ("Crash reports", "On" if selections.crash_reports else "Off"),
    ]


# --- what the installer did (read-only recap of install-state.json) ---------------
# Steps the installer retries silently in the background at start-up (lindos-*-firstboot).
_BACKGROUND_RETRY = ("browser", "drivers")

_RECAP_DONE = "installed while Lindos was installing"
_RECAP_DONE_DRIVERS = "set up while Lindos was installing"


def install_recap(steps: Optional[Dict[str, str]]) -> List[Tuple[str, str, str]]:
    """``[(step id, friendly name, sentence), ...]`` for every step the installer recorded.

    *steps* maps a step id to its status (``done``/``pending``/``failed``/``skipped``), see
    ``core.install_steps``.  Steps without a record are left out - nothing is claimed that the
    installer did not write down.  ``pending`` and ``failed`` are both phrased as "still waiting":
    the two steps that have a silent background retry say so, the others point to Settings.
    """
    rows: List[Tuple[str, str, str]] = []
    for step in INSTALL_STEP_ORDER:
        status = (steps or {}).get(step, "")
        if not status:
            continue
        name = INSTALL_STEP_NAMES[step]
        if status == "done":
            text = _RECAP_DONE_DRIVERS if step == "drivers" else _RECAP_DONE
        elif status == "skipped":
            text = "left out on purpose"
        elif step in _BACKGROUND_RETRY:
            text = "still waiting for an internet connection — Lindos adds it in the background"
        elif step == "updates":
            text = "still waiting — install them from the Update Manager when you're online"
        else:
            text = "still waiting — install it from Lindos Settings › Apps when you're online"
        rows.append((step, name, text))
    return rows


def pending_steps(steps: Optional[Dict[str, str]]) -> List[str]:
    """Recorded steps that are still ``pending`` or ``failed`` (canonical order)."""
    return [s for s in INSTALL_STEP_ORDER if (steps or {}).get(s) in ("pending", "failed")]


# Which install-state steps concern a Mode's own extras (its mode.json packages / Flatpaks / launchers).
def mode_extras_pending(mode: Any, mode_id: str, steps: Optional[Dict[str, str]]) -> bool:
    """True when something this Mode brings along is still waiting to be installed.

    *mode* is a ``lindos.modes.Mode`` (or anything with ``packages`` / ``flatpaks``); the Gaming
    Mode also owns the gaming launchers.  Switching Modes in the wizard is configuration only, so
    the Mode page tells the user plainly instead of pretending the extras are there.
    """
    waiting = set(pending_steps(steps))
    if getattr(mode, "packages", None) and "mode_extras" in waiting:
        return True
    if getattr(mode, "flatpaks", None) and "flatpaks" in waiting:
        return True
    return mode_id == "gaming" and "gaming" in waiting


__all__ = [
    "SCHEMA", "MODE_IDS", "BROWSER_IDS", "THEMES", "TASKBAR_ALIGNMENTS",
    "DEFAULT_ACCENT", "DEFAULT_WALLPAPER", "LIGHT_WALLPAPER", "WALLPAPER_DIR", "WALLPAPER_NAMES",
    "TRANSFER_SOURCE_TYPES", "INSTALL_STEP_ORDER", "INSTALL_STEP_NAMES",
    "BROWSER_INSTALLED", "BROWSER_PENDING", "BROWSER_UNAVAILABLE",
    "KIND_USER", "KIND_SYSTEM",
    "Selections", "AppEntry", "Catalog", "load_catalog", "load_accents", "find_data_file",
    "Step", "Plan", "build_plan", "StepResult", "RunResult", "Runner",
    "make_printing_executors", "make_recording_executors", "summarize", "default_wallpaper_for",
    "install_recap", "pending_steps", "mode_extras_pending",
]
