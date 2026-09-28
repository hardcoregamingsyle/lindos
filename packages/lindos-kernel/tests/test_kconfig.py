"""lindos_kernel.kconfig: required keys, no duplicates, value rules (SPEC-KERNEL §15.2, §19)."""
from __future__ import annotations

from pathlib import Path

import pytest

from lindos_kernel import kconfig as k


# --- the shipped fragment -----------------------------------------------------------------
def test_shipped_fragment_is_valid(shipped_config: Path) -> None:
    cfg = k.validate(str(shipped_config), is_path=True, strict=True)
    assert cfg.is_valid()
    assert cfg.problems() == []


def test_shipped_fragment_has_every_required_key(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    present = set(cfg.keys())
    for key in k.REQUIRED_KEYS:
        assert key in present, key
    for group in k.REQUIRED_ANY:
        assert present.intersection(group), group


def test_shipped_fragment_required_values(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    assert cfg.get("CONFIG_HZ") == "1000"
    assert cfg.get("CONFIG_HZ_1000") == "y"
    assert cfg.get("CONFIG_LRU_GEN") == "y"
    assert cfg.get("CONFIG_LRU_GEN_ENABLED") == "y"
    assert cfg.get("CONFIG_TRANSPARENT_HUGEPAGE_MADVISE") == "y"


def test_shipped_fragment_no_duplicates(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    assert cfg.duplicates() == {}
    assert cfg.conflicts() == {}


# --- Addendum W (SPEC-WINDOWS §31.1): Windows-format keys ----------------------------------
def test_addendum_w_keys_are_required() -> None:
    # every Addendum-W key must be part of the general required-keys set
    for key in k.REQUIRED_KEYS_ADDENDUM_W:
        assert key in k.REQUIRED_KEYS, key


def test_addendum_w_keys_do_not_conflict_with_existing_keys() -> None:
    # the new §31.1 keys must not collide with any pre-existing (perf/Wine) required key
    pre_existing = set(k.REQUIRED_KEYS) - set(k.REQUIRED_KEYS_ADDENDUM_W)
    assert pre_existing.isdisjoint(k.REQUIRED_KEYS_ADDENDUM_W)


def test_shipped_fragment_has_every_addendum_w_key(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    present = set(cfg.keys())
    for key in k.REQUIRED_KEYS_ADDENDUM_W:
        assert key in present, key


def test_shipped_fragment_addendum_w_values(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    # module (built as a loadable driver, not built-in) for the heavier filesystem drivers
    assert cfg.get("CONFIG_NTFS3_FS") == "m"
    assert cfg.get("CONFIG_EXFAT_FS") == "m"
    assert cfg.get("CONFIG_DM_CRYPT") == "m"
    assert cfg.get("CONFIG_CRYPTO_USER_API_SKCIPHER") == "m"
    assert cfg.get("CONFIG_ISO9660_FS") == "m"
    assert cfg.get("CONFIG_UDF_FS") == "m"
    # built-in for the small always-needed bits
    assert cfg.get("CONFIG_NTFS3_LZX_XPRESS") == "y"
    assert cfg.get("CONFIG_NTFS3_FS_POSIX_ACL") == "y"
    assert cfg.get("CONFIG_UNICODE") == "y"
    assert cfg.get("CONFIG_BINFMT_MISC") == "y"
    assert cfg.get("CONFIG_EFIVAR_FS") == "y"
    assert cfg.get("CONFIG_BLK_DEV_LOOP") == "y"
    assert cfg.get("CONFIG_JOLIET") == "y"
    assert cfg.get("CONFIG_FUSE_FS") == "y"
    assert cfg.get("CONFIG_LDM_PARTITION") == "y"


def test_missing_addendum_w_key_is_reported_missing() -> None:
    base = {key: "y" for key in k.REQUIRED_KEYS if key != "CONFIG_NTFS3_FS"}
    base["CONFIG_HZ"] = "1000"
    text = "\n".join(f"{key}={val}" for key, val in base.items()) + "\nCONFIG_IOSCHED_BFQ=y\n"
    cfg = k.parse(text)
    assert "CONFIG_NTFS3_FS" in cfg.missing_required()


# --- display/GPU keys (CONTINUATION.md item 2 / boot-test run 36319809802) ------------------
def test_display_keys_are_required() -> None:
    # every display key must be part of the general required-keys set
    for key in k.REQUIRED_KEYS_DISPLAY:
        assert key in k.REQUIRED_KEYS, key


def test_display_keys_do_not_conflict_with_existing_keys() -> None:
    pre_existing = set(k.REQUIRED_KEYS) - set(k.REQUIRED_KEYS_DISPLAY)
    assert pre_existing.isdisjoint(k.REQUIRED_KEYS_DISPLAY)


def test_shipped_fragment_has_every_display_key(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    present = set(cfg.keys())
    for key in k.REQUIRED_KEYS_DISPLAY:
        assert key in present, key


def test_shipped_fragment_display_values(shipped_config: Path) -> None:
    cfg = k.parse_file(str(shipped_config))
    # display core: always built-in (KMS/fbcon must exist before any module loader could run)
    for key in ("CONFIG_DRM", "CONFIG_DRM_KMS_HELPER", "CONFIG_DRM_FBDEV_EMULATION",
                "CONFIG_FRAMEBUFFER_CONSOLE", "CONFIG_SYSFB_SIMPLEFB", "CONFIG_DRM_SIMPLEDRM"):
        assert cfg.get(key) == "y", key
    # real-hardware + QEMU/CI GPU drivers: modules (loaded on demand for whichever GPU is present)
    for key in ("CONFIG_DRM_I915", "CONFIG_DRM_XE", "CONFIG_DRM_AMDGPU", "CONFIG_DRM_RADEON",
                "CONFIG_DRM_NOUVEAU", "CONFIG_DRM_BOCHS", "CONFIG_DRM_VIRTIO_GPU", "CONFIG_DRM_QXL"):
        assert cfg.get(key) == "m", key


def test_missing_display_key_is_reported_missing() -> None:
    base = {key: "y" for key in k.REQUIRED_KEYS if key != "CONFIG_DRM_I915"}
    base["CONFIG_HZ"] = "1000"
    text = "\n".join(f"{key}={val}" for key, val in base.items()) + "\nCONFIG_IOSCHED_BFQ=y\n"
    cfg = k.parse(text)
    assert "CONFIG_DRM_I915" in cfg.missing_required()


def test_load_default_path_uses_lindos_root(fake_root) -> None:
    cfg = k.parse_file()  # resolved through LINDOS_ROOT
    assert cfg.get("CONFIG_NTSYNC") == "y"


# --- parsing rules ------------------------------------------------------------------------
def test_notset_line_parses_as_n() -> None:
    cfg = k.parse("# CONFIG_FOO is not set\nCONFIG_BAR=y\n")
    assert cfg.get("CONFIG_FOO") == "n"
    assert cfg.get("CONFIG_BAR") == "y"


def test_comment_and_blank_lines_ignored() -> None:
    cfg = k.parse("# a comment\n\n   \nCONFIG_BAR=m\n")
    assert cfg.keys() == ["CONFIG_BAR"]


def test_quoted_value_is_unquoted() -> None:
    cfg = k.parse('CONFIG_LOCALVERSION="-lindos"\n')
    assert cfg.get("CONFIG_LOCALVERSION") == "-lindos"


def test_malformed_line_raises() -> None:
    with pytest.raises(k.KConfigError):
        k.parse("this is not kconfig\n")


def test_empty_value_raises() -> None:
    with pytest.raises(k.KConfigError):
        k.parse("CONFIG_FOO=\n")


# --- duplicate / conflict detection -------------------------------------------------------
def test_same_value_duplicate_is_not_a_conflict() -> None:
    cfg = k.parse("CONFIG_FOO=y\nCONFIG_FOO=y\n")
    assert "CONFIG_FOO" in cfg.duplicates()
    assert cfg.conflicts() == {}


def test_conflicting_duplicate_detected() -> None:
    cfg = k.parse("CONFIG_HZ=1000\nCONFIG_HZ=250\n")
    conflicts = cfg.conflicts()
    assert conflicts.get("CONFIG_HZ") == ["1000", "250"]
    assert any("conflicting" in p for p in cfg.problems())


def test_strict_validate_raises_on_conflict() -> None:
    text = "\n".join(f"{key}=y" for key in k.REQUIRED_KEYS)
    text += "\nCONFIG_IOSCHED_BFQ=y\nCONFIG_HZ=250\n"  # HZ conflicts with the =y above
    with pytest.raises(k.KConfigError):
        k.validate(text, strict=True)


# --- missing-required detection -----------------------------------------------------------
def test_missing_required_key_reported() -> None:
    cfg = k.parse("CONFIG_NTSYNC=y\n")
    missing = cfg.missing_required()
    assert "CONFIG_SCHED_CLASS_EXT" in missing
    assert "CONFIG_IOSCHED_BFQ|CONFIG_MQ_IOSCHED_KYBER" in missing


def test_either_iosched_satisfies_group() -> None:
    base = {key: "y" for key in k.REQUIRED_KEYS}
    base["CONFIG_HZ"] = "1000"
    text = "\n".join(f"{key}={val}" for key, val in base.items()) + "\nCONFIG_MQ_IOSCHED_KYBER=y\n"
    cfg = k.parse(text)
    assert cfg.missing_required() == []


def test_wrong_required_value_reported() -> None:
    base = {key: "y" for key in k.REQUIRED_KEYS}
    base["CONFIG_HZ"] = "300"  # must be 1000
    text = "\n".join(f"{key}={val}" for key, val in base.items()) + "\nCONFIG_IOSCHED_BFQ=y\n"
    cfg = k.parse(text)
    assert "CONFIG_HZ" in cfg.wrong_values()
    assert not cfg.is_valid()
