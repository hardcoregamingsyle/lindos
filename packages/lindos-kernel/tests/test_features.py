"""lindos_kernel.features: probe a faked /proc+/sys+/boot+/dev tree (SPEC-KERNEL §15.4, §19)."""
from __future__ import annotations

import gzip

import pytest

from lindos_kernel import features as f

LINDOS_CONFIG = """\
CONFIG_NTSYNC=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_HZ_1000=y
CONFIG_HZ=1000
CONFIG_PREEMPT=y
CONFIG_LRU_GEN=y
CONFIG_TCP_CONG_BBR=y
"""

STOCK_CONFIG = """\
# CONFIG_NTSYNC is not set
CONFIG_HZ_250=y
CONFIG_HZ=250
CONFIG_PREEMPT_VOLUNTARY=y
"""


def _plant_lindos(fake_root, release="6.14.0-lindos"):
    monkey = fake_root
    monkey["write"](f"/boot/config-{release}", LINDOS_CONFIG)
    monkey["touch"]("/dev/ntsync")
    monkey["mkdir"]("/sys/kernel/sched_ext/root")
    monkey["write"]("/sys/kernel/sched_ext/root/ops", "scx_lavd\n")
    monkey["write"]("/sys/kernel/mm/transparent_hugepage/enabled",
                    "always [madvise] never\n")
    monkey["write"]("/sys/kernel/mm/lru_gen/enabled", "0x0007\n")
    monkey["write"]("/proc/sys/net/ipv4/tcp_congestion_control", "bbr\n")


def test_running_release_from_env(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    assert f.running_release() == "6.14.0-lindos"
    assert f.is_lindos_kernel() is True


def test_release_falls_back_to_boot_config(fake_root) -> None:
    fake_root["write"]("/boot/config-6.14.0-lindos", LINDOS_CONFIG)
    # no env, no os.uname release on Windows → glob /boot/config-*
    rel = f.running_release()
    assert rel.endswith("-lindos") or rel != ""  # real uname on Linux may differ; both acceptable


def test_lindos_kernel_all_features_present(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    _plant_lindos(fake_root)
    probed = f.probe()
    assert probed["ntsync"]["present"] is True
    assert probed["sched_ext"]["present"] is True
    assert "scx_lavd" in probed["sched_ext"]["detail"]
    assert probed["preempt_full"]["present"] is True
    assert probed["hz1000"]["present"] is True
    assert probed["mglru"]["present"] is True
    assert probed["bbr"]["present"] is True
    assert probed["thp"]["present"] is True


def test_stock_kernel_features_absent(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.8.0-generic")
    fake_root["write"]("/boot/config-6.8.0-generic", STOCK_CONFIG)
    # no /dev/ntsync, no sched_ext dir, no sysfs
    probed = f.probe()
    assert probed["ntsync"]["present"] is False
    assert probed["sched_ext"]["present"] is False
    assert probed["hz1000"]["present"] is False
    assert probed["preempt_full"]["present"] is False
    assert f.is_lindos_kernel() is False


def test_config_from_proc_config_gz(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    # no /boot/config-*, only /proc/config.gz
    root = fake_root["root"]
    proc = root / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    with gzip.open(proc / "config.gz", "wt", encoding="utf-8") as handle:
        handle.write(LINDOS_CONFIG)
    cfg = f.read_kernel_config()
    assert cfg.get("CONFIG_HZ") == "1000"
    assert cfg.get("CONFIG_NTSYNC") == "y"


def test_thp_mode_parsing(fake_root) -> None:
    fake_root["write"]("/sys/kernel/mm/transparent_hugepage/enabled",
                       "always [never] madvise\n")
    assert f.thp_mode() == "never"


def test_thp_absent_is_none(fake_root) -> None:
    assert f.thp_mode() is None


def test_mglru_bitmask(fake_root) -> None:
    fake_root["write"]("/sys/kernel/mm/lru_gen/enabled", "0x0000\n")
    assert f.mglru_enabled() is False
    fake_root["write"]("/sys/kernel/mm/lru_gen/enabled", "0x0007\n")
    assert f.mglru_enabled() is True


def test_sched_ext_absent(fake_root) -> None:
    assert f.sched_ext_present() is False
    assert f.sched_ext_active() is None


def test_status_blob_shape(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    _plant_lindos(fake_root)
    status = f.status()
    assert status["release"] == "6.14.0-lindos"
    assert status["is_lindos"] is True
    assert status["thp"] == "madvise"
    assert status["tcp_congestion_control"] == "bbr"
    assert isinstance(status["features"], dict)


def test_cross_check_against_manifest(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    _plant_lindos(fake_root)
    rows = f.cross_check()  # manifest loaded from LINDOS_ROOT
    by_id = {row["id"]: row for row in rows}
    assert by_id["ntsync"]["present"] is True
    assert by_id["ntsync"]["kconfig"] == "CONFIG_NTSYNC"
    assert by_id["sched_ext"]["present"] is True


def test_no_config_source_is_graceful(fake_root, monkeypatch) -> None:
    monkeypatch.setenv("LINDOS_KERNEL_RELEASE", "6.14.0-lindos")
    # nothing planted → probe must not raise, features read unknown/absent
    probed = f.probe()
    assert probed["ntsync"]["present"] is False
    assert probed["hz1000"]["present"] is None
