"""lindos_kernel.grub: fenced drop-in add/remove is idempotent (SPEC-KERNEL §15.3, §19)."""
from __future__ import annotations

import shutil
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


# --- byte-stability against the real shipped conffiles (regression, tests-hermetic:F2) -------
# `_compose()` used to unconditionally insert a blank-line separator before the fenced region,
# even when the source had none. Both shipped drop-ins have their explanatory comment block
# run right up against `# >>> lindos >>>` with no gap, so a no-op re-application (e.g. the
# postinst's `secureboot apply-selection`, which runs on every kernel install/upgrade) rewrote
# them with different bytes than what dpkg recorded at unpack time -- guaranteeing dpkg treats
# an untouched conffile as locally modified on the very next upgrade.
def test_shipped_cmdline_dropin_noop_is_byte_identical(shipped_dropin: Path, tmp_path: Path) -> None:
    copy = tmp_path / "50-lindos.cfg"
    shutil.copy2(str(shipped_dropin), str(copy))
    before = copy.read_bytes()
    g.set_flags(g.DEFAULT_FLAGS, str(copy))
    assert copy.read_bytes() == before


def test_compose_does_not_synthesize_gap_when_source_has_none(tmp_path: Path) -> None:
    path = tmp_path / "50-lindos.cfg"
    path.write_text(
        "# header, no blank line before the fence\n"
        f"{g.BEGIN}\n"
        'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT nowatchdog"\n'
        f"{g.END}\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    g.set_flags(["nowatchdog"], str(path))
    assert path.read_text(encoding="utf-8") == before


def test_compose_preserves_existing_gap(tmp_path: Path) -> None:
    # a file that already had a blank-line separator keeps exactly one; only *introducing* a
    # gap that never existed is the bug -- an existing one must still round-trip cleanly.
    path = tmp_path / "50-lindos.cfg"
    path.write_text(
        "# header, with a blank line before the fence\n"
        "\n"
        f"{g.BEGIN}\n"
        'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT nowatchdog"\n'
        f"{g.END}\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    g.set_flags(["nowatchdog"], str(path))
    assert path.read_text(encoding="utf-8") == before


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


# --- Secure-Boot-aware kernel selection (SPEC-WINDOWS §31.3) -------------------------------
def test_shipped_kernel_select_dropin_defaults_to_zero(pkg_root: Path) -> None:
    path = pkg_root / "root" / "etc" / "default" / "grub.d" / "51-lindos-kernel-select.cfg"
    assert g.current_kernel_select(str(path)) == "0"


@pytest.mark.parametrize(
    "secure_boot,signed,expected",
    [
        (False, False, True),
        (False, None, True),
        (None, None, True),
        (None, False, True),
        (True, True, True),
        (True, False, False),
        (True, None, False),
    ],
)
def test_may_boot_lindos_by_default_policy(secure_boot, signed, expected) -> None:
    assert g.may_boot_lindos_by_default(secure_boot, signed) is expected


def test_kernel_select_set_then_read_roundtrip(tmp_path: Path) -> None:
    path = str(tmp_path / "51-lindos-kernel-select.cfg")
    g.set_kernel_select("0", path)
    assert g.current_kernel_select(path) == "0"
    g.set_kernel_select("1", path)
    assert g.current_kernel_select(path) == "1"


def test_kernel_select_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "51-lindos-kernel-select.cfg")
    first = g.set_kernel_select("0", path)
    second = g.set_kernel_select("0", path)
    assert first == second


def test_shipped_kernel_select_dropin_noop_is_byte_identical(pkg_root: Path, tmp_path: Path) -> None:
    # regression, tests-hermetic:F2: postinst's `secureboot apply-selection` runs on every
    # kernel install/upgrade; a no-op decision (GRUB_DEFAULT="0", already shipped) must not
    # rewrite this conffile with different bytes (e.g. a synthesized blank line).
    src = pkg_root / "root" / "etc" / "default" / "grub.d" / "51-lindos-kernel-select.cfg"
    copy = tmp_path / "51-lindos-kernel-select.cfg"
    shutil.copy2(str(src), str(copy))
    before = copy.read_bytes()
    g.set_kernel_select("0", str(copy))
    assert copy.read_bytes() == before


def test_kernel_select_missing_block_reads_none(tmp_path: Path) -> None:
    path = tmp_path / "51-lindos-kernel-select.cfg"
    path.write_text("# nothing here\n", encoding="utf-8")
    assert g.current_kernel_select(str(path)) is None


def test_advanced_submenu_and_entry_ids() -> None:
    assert g.advanced_submenu_id("ABCD-1234") == "gnulinux-advanced-ABCD-1234"
    assert g.kernel_menu_entry_id("6.8.0-31-generic", "ABCD-1234") == \
        "gnulinux-6.8.0-31-generic-advanced-ABCD-1234"
    assert g.kernel_menu_entry_id("6.8.0-31-generic", "ABCD-1234", kind="recovery") == \
        "gnulinux-6.8.0-31-generic-recovery-ABCD-1234"


def test_select_default_target_allowed_is_zero() -> None:
    assert g.select_default_target(True, "6.14.0-lindos", "6.8.0-31-generic", "ABCD-1234") == "0"


def test_select_default_target_denied_with_full_info() -> None:
    value = g.select_default_target(False, "6.14.0-lindos", "6.8.0-31-generic", "ABCD-1234")
    assert value == "gnulinux-advanced-ABCD-1234>gnulinux-6.8.0-31-generic-advanced-ABCD-1234"


def test_select_default_target_denied_without_device_id_falls_back_to_advanced_menu() -> None:
    assert g.select_default_target(False, "6.14.0-lindos", "6.8.0-31-generic", None) == "1"
    assert g.select_default_target(False, "6.14.0-lindos", None, "ABCD-1234") == "1"


def test_boot_device_id_uses_grub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_which(name: str):
        return "/usr/sbin/grub-probe" if name == "grub-probe" else None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="ABCD-1234\n", stderr="")

    assert g.boot_device_id(run=fake_run, which=fake_which) == "ABCD-1234"
    assert calls[0][:2] == ["grub-probe", "--target=fs_uuid"]


def test_boot_device_id_none_without_grub_probe() -> None:
    assert g.boot_device_id(run=lambda *a, **k: None, which=lambda name: None) is None


def test_boot_device_id_falls_back_to_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "/boot":
            return SimpleNamespace(returncode=1, stdout="", stderr="no /boot fs\n")
        return SimpleNamespace(returncode=0, stdout="ROOT-UUID\n", stderr="")

    result = g.boot_device_id(run=fake_run, which=lambda name: "/usr/sbin/grub-probe")
    assert result == "ROOT-UUID"


def test_apply_kernel_selection_writes_and_returns_value(tmp_path: Path) -> None:
    path = str(tmp_path / "51-lindos-kernel-select.cfg")
    value = g.apply_kernel_selection(
        secure_boot=True, signed=False, lindos_version="6.14.0-lindos",
        fallback_version="6.8.0-31-generic",
        run=lambda *a, **k: None, which=lambda name: None,  # no grub-probe -> device_id None
        path=path,
    )
    assert value == "1"  # device_id unresolved -> conservative "open Advanced options"
    assert g.current_kernel_select(path) == "1"


def test_apply_kernel_selection_allows_lindos_when_secure_boot_off(tmp_path: Path) -> None:
    path = str(tmp_path / "51-lindos-kernel-select.cfg")
    value = g.apply_kernel_selection(
        secure_boot=False, signed=None, run=lambda *a, **k: None, which=lambda name: None,
        path=path,
    )
    assert value == "0"
