"""Load and validate ``/usr/share/lindos/kernel/manifest.json`` (SPEC-KERNEL §15.1).

The manifest is the single source of truth for the recommended kernel series and the list of
Lindos performance features (each tied to a ``CONFIG_*`` symbol).  :func:`load` reads and
validates it; :func:`validate` schema-checks an already-parsed object.  Nothing here touches
the running kernel — that is :mod:`lindos_kernel.features`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import manifest_path

SCHEMA = 1
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")
FEATURE_ID_RE = re.compile(r"^[a-z0-9_]+$")
KCONFIG_RE = re.compile(r"^CONFIG_[A-Z0-9_]+$")


class ManifestError(ValueError):
    """Raised on a missing, unreadable, or schema-invalid manifest."""


@dataclass(frozen=True)
class Feature:
    """One entry of the manifest ``features`` array."""

    id: str
    kconfig: str
    why: str = ""
    since: Optional[str] = None
    dev: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "kconfig": self.kconfig, "why": self.why}
        if self.since is not None:
            out["since"] = self.since
        if self.dev is not None:
            out["dev"] = self.dev
        return out


@dataclass(frozen=True)
class Patch:
    """One entry of the manifest ``patches`` array (out-of-tree, optional)."""

    id: str
    optional: bool = True
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "optional": self.optional, "why": self.why}


@dataclass(frozen=True)
class Recommended:
    """The ``recommended`` object: which kernel to build/expect."""

    series: str
    min: str
    flavour: str = "lindos"
    localversion: str = "-lindos"

    def to_dict(self) -> Dict[str, Any]:
        return {"series": self.series, "min": self.min, "flavour": self.flavour,
                "localversion": self.localversion}


@dataclass(frozen=True)
class Manifest:
    """The whole validated manifest."""

    schema: int
    recommended: Recommended
    features: List[Feature] = field(default_factory=list)
    patches: List[Patch] = field(default_factory=list)

    def feature(self, feature_id: str) -> Optional[Feature]:
        for feat in self.features:
            if feat.id == feature_id:
                return feat
        return None

    def kconfig_keys(self) -> List[str]:
        return [feat.kconfig for feat in self.features]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "recommended": self.recommended.to_dict(),
            "features": [f.to_dict() for f in self.features],
            "patches": [p.to_dict() for p in self.patches],
        }


def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise ManifestError(message)


def validate(data: Any) -> Manifest:
    """Schema-check *data* (a parsed JSON object) and return a :class:`Manifest`.

    Raises :class:`ManifestError` on any problem.
    """
    _expect(isinstance(data, dict), "manifest must be a JSON object")
    _expect(data.get("schema") == SCHEMA, f"manifest schema must be {SCHEMA}")

    rec = data.get("recommended")
    _expect(isinstance(rec, dict), "'recommended' must be an object")
    for key in ("series", "min"):
        _expect(isinstance(rec.get(key), str) and bool(VERSION_RE.match(rec[key])),
                f"recommended.{key} must be a kernel version like 6.14")
    flavour = rec.get("flavour", "lindos")
    localversion = rec.get("localversion", "-lindos")
    _expect(isinstance(flavour, str) and bool(flavour), "recommended.flavour must be a string")
    _expect(isinstance(localversion, str) and localversion.startswith("-"),
            "recommended.localversion must start with '-'")
    recommended = Recommended(series=rec["series"], min=rec["min"], flavour=flavour,
                              localversion=localversion)

    raw_features = data.get("features", [])
    _expect(isinstance(raw_features, list) and raw_features, "'features' must be a non-empty list")
    features: List[Feature] = []
    seen_ids: set = set()
    for item in raw_features:
        _expect(isinstance(item, dict), "each feature must be an object")
        fid = item.get("id")
        kcfg = item.get("kconfig")
        _expect(isinstance(fid, str) and bool(FEATURE_ID_RE.match(fid)),
                f"feature id must match {FEATURE_ID_RE.pattern}: {fid!r}")
        _expect(fid not in seen_ids, f"duplicate feature id {fid!r}")
        seen_ids.add(fid)
        _expect(isinstance(kcfg, str) and bool(KCONFIG_RE.match(kcfg)),
                f"feature {fid}: kconfig must be a CONFIG_* symbol, got {kcfg!r}")
        since = item.get("since")
        _expect(since is None or (isinstance(since, str) and bool(VERSION_RE.match(since))),
                f"feature {fid}: since must be a version or absent")
        dev = item.get("dev")
        _expect(dev is None or (isinstance(dev, str) and dev.startswith("/")),
                f"feature {fid}: dev must be an absolute path or absent")
        why = item.get("why", "")
        _expect(isinstance(why, str), f"feature {fid}: why must be a string")
        features.append(Feature(id=fid, kconfig=kcfg, why=why, since=since, dev=dev))

    raw_patches = data.get("patches", [])
    _expect(isinstance(raw_patches, list), "'patches' must be a list")
    patches: List[Patch] = []
    for item in raw_patches:
        _expect(isinstance(item, dict), "each patch must be an object")
        pid = item.get("id")
        _expect(isinstance(pid, str) and bool(FEATURE_ID_RE.match(pid)),
                f"patch id must match {FEATURE_ID_RE.pattern}: {pid!r}")
        optional = item.get("optional", True)
        _expect(isinstance(optional, bool), f"patch {pid}: optional must be true/false")
        why = item.get("why", "")
        _expect(isinstance(why, str), f"patch {pid}: why must be a string")
        patches.append(Patch(id=pid, optional=optional, why=why))

    return Manifest(schema=SCHEMA, recommended=recommended, features=features, patches=patches)


def load(path: Optional[str] = None) -> Manifest:
    """Read, parse and validate the manifest.  *path* defaults to the shipped location
    (resolved through ``LINDOS_ROOT``)."""
    target = path or manifest_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {target}") from exc
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {target} is not valid JSON: {exc}") from exc
    return validate(data)


__all__ = [
    "SCHEMA", "ManifestError", "Feature", "Patch", "Recommended", "Manifest",
    "validate", "load",
]
