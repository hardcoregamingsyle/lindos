"""lindos_kernel.manifest: shipped manifest loads, schema is enforced (SPEC-KERNEL §15.1, §19)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lindos_kernel import manifest as m


def _shipped(shipped_manifest: Path) -> dict:
    with open(shipped_manifest, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_shipped_manifest_is_valid(shipped_manifest: Path) -> None:
    man = m.load(str(shipped_manifest))
    assert man.schema == 1
    assert man.recommended.series == "6.14"
    assert man.recommended.min == "6.14"
    assert man.recommended.localversion == "-lindos"


def test_shipped_manifest_headline_features(shipped_manifest: Path) -> None:
    man = m.load(str(shipped_manifest))
    ids = {feat.id for feat in man.features}
    assert {"ntsync", "sched_ext", "preempt_full", "hz1000", "mglru", "bbr"} <= ids
    ntsync = man.feature("ntsync")
    assert ntsync is not None
    assert ntsync.kconfig == "CONFIG_NTSYNC"
    assert ntsync.dev == "/dev/ntsync"
    assert ntsync.since == "6.14"
    assert man.feature("sched_ext").kconfig == "CONFIG_SCHED_CLASS_EXT"


def test_load_default_path_uses_lindos_root(fake_root) -> None:
    man = m.load()  # no arg → shipped location resolved through LINDOS_ROOT
    assert man.recommended.series == "6.14"


def test_roundtrip_to_dict(shipped_manifest: Path) -> None:
    man = m.load(str(shipped_manifest))
    again = m.validate(man.to_dict())
    assert again.to_dict() == man.to_dict()


def test_bad_schema_rejected(fake_root) -> None:
    data = _shipped(Path(fake_root["root"]) / "usr/share/lindos/kernel/manifest.json")
    data["schema"] = 2
    with pytest.raises(m.ManifestError):
        m.validate(data)


def test_missing_recommended_rejected(shipped_manifest: Path) -> None:
    data = _shipped(shipped_manifest)
    del data["recommended"]
    with pytest.raises(m.ManifestError):
        m.validate(data)


def test_bad_kconfig_symbol_rejected(shipped_manifest: Path) -> None:
    data = _shipped(shipped_manifest)
    data["features"][0]["kconfig"] = "ntsync"  # not a CONFIG_* symbol
    with pytest.raises(m.ManifestError):
        m.validate(data)


def test_duplicate_feature_id_rejected(shipped_manifest: Path) -> None:
    data = _shipped(shipped_manifest)
    data["features"].append(copy.deepcopy(data["features"][0]))
    with pytest.raises(m.ManifestError):
        m.validate(data)


def test_bad_version_rejected(shipped_manifest: Path) -> None:
    data = _shipped(shipped_manifest)
    data["recommended"]["series"] = "sixpointfourteen"
    with pytest.raises(m.ManifestError):
        m.validate(data)


def test_missing_file_raises() -> None:
    with pytest.raises(m.ManifestError):
        m.load("/nonexistent/does/not/exist/manifest.json")
