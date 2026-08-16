"""lindos_kernel.grub: fenced drop-in add/remove is idempotent (SPEC-KERNEL §15.3, §19)."""
from __future__ import annotations

from pathlib import Path

import pytest

from lindos_kernel import grub as g


def test_shipped_dropin_parses(shipped_dropin: Path) -> None:
    flags = g.current_flags(str(shipped_dropin))
    assert flags == g.DEFAULT_FLAGS
    # must NOT ship mitigations=off by default (security trade-off)
    assert "mitigations=off" not in flags


def test_shipped_dropin_has_fences(shipped_dropin: Path) -> None:
    text = g.read(str(shipped_dropin))
    assert g.BEGIN in text and g.END in text
    # the mitigations opt-in is documented in a comment, not active
    assert "# " in text
    assert "mitigations=off" in text  # only inside the explanatory comment


def test_set_then_read_roundtrip(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    g.set_flags(["transparent_hugepage=madvise", "nowatchdog"], path)
    assert g.current_flags(path) == ["transparent_hugepage=madvise", "nowatchdog"]


def test_set_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    first = g.set_flags(g.DEFAULT_FLAGS, path)
    second = g.set_flags(g.DEFAULT_FLAGS, path)
    assert first == second  # byte-identical
    assert g.current_flags(path) == g.DEFAULT_FLAGS


def test_add_flags_dedupes(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    g.set_flags(["nowatchdog"], path)
    g.add_flags(["nowatchdog", "quiet"], path)
    assert g.current_flags(path) == ["nowatchdog", "quiet"]


def test_remove_block_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    g.set_flags(g.DEFAULT_FLAGS, path)
    g.remove_block(path)
    assert g.current_flags(path) == []
    text_after_first = g.read(path)
    g.remove_block(path)  # removing again does nothing
    assert g.read(path) == text_after_first
    assert g.BEGIN not in text_after_first


def test_remove_preserves_surrounding_text(tmp_path: Path) -> None:
    path = tmp_path / "50-lindos.cfg"
    path.write_text(
        "# a header comment\n"
        f"{g.BEGIN}\n"
        'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT nowatchdog"\n'
        f"{g.END}\n"
        "# a trailing comment\n",
        encoding="utf-8",
    )
    g.remove_block(str(path))
    text = path.read_text(encoding="utf-8")
    assert "# a header comment" in text
    assert "# a trailing comment" in text
    assert g.BEGIN not in text


def test_add_to_file_without_block(tmp_path: Path) -> None:
    path = tmp_path / "50-lindos.cfg"
    path.write_text("# just a comment, no fenced region\n", encoding="utf-8")
    g.set_flags(["nowatchdog"], str(path))
    text = path.read_text(encoding="utf-8")
    assert "# just a comment, no fenced region" in text
    assert g.current_flags(str(path)) == ["nowatchdog"]


def test_reset_restores_default(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    g.preset("gaming", path)
    assert "mitigations=off" in g.current_flags(path)
    g.reset(path)
    assert g.current_flags(path) == g.DEFAULT_FLAGS


def test_preset_gaming_adds_mitigations(tmp_path: Path) -> None:
    path = str(tmp_path / "50-lindos.cfg")
    g.preset("gaming", path)
    flags = g.current_flags(path)
    assert "mitigations=off" in flags
    assert set(g.DEFAULT_FLAGS) <= set(flags)


def test_unknown_preset_raises(tmp_path: Path) -> None:
    with pytest.raises(g.GrubError):
        g.preset("turbo", str(tmp_path / "50-lindos.cfg"))


def test_invalid_flag_rejected(tmp_path: Path) -> None:
    with pytest.raises(g.GrubError):
        g.set_flags(["bad flag with spaces"], str(tmp_path / "50-lindos.cfg"))


def test_default_dropin_path_uses_lindos_root(fake_root) -> None:
    path = g.grub_dropin_path()  # type: ignore[attr-defined]
    assert str(fake_root["root"]) in path
    assert path.endswith("50-lindos.cfg")


def test_update_grub_command_is_pkexec() -> None:
    assert g.update_grub_command() == ["pkexec", "update-grub"]
