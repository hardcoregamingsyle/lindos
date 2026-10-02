"""lindos.updatestate - the update engine's parsers, plan digest, state writer, reboot-required logic, history
parser and safe-cleanup planner (SPEC-UPDATE.md §39-§41).

Hermetic: no apt, no root, no network, no real clock or kernel. Every command goes through an injected
*runner*, every path through a scratch ``LINDOS_ROOT`` (``core_env``), every time through an argument.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from lindos import paths
from lindos import updatestate as us

UTC = dt.timezone.utc

# =================================================================================================
# apt-get -s dist-upgrade output
# =================================================================================================
SIM = """NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following NEW packages will be installed:
  linux-image-6.8.0-47-generic linux-modules-6.8.0-47-generic
The following packages will be REMOVED:
  libold1
The following packages have been kept back:
  gimp libreoffice-core
  something-else
The following packages will be upgraded:
  libc6 libssl3t64 lindos-core lindos-meta google-chrome-stable
5 upgraded, 2 newly installed, 1 to remove and 3 not upgraded.
Inst libc6 [2.39-0ubuntu8.3] (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates [amd64])
Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])
Inst lindos-meta [1.0.0] (1.0.1 Lindos:stable [all]) []
Inst google-chrome-stable [125.0] (126.0 Google LLC:stable [amd64])
Inst linux-image-6.8.0-47-generic (6.8.0-47.47 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst linux-modules-6.8.0-47-generic (6.8.0-47.47 Ubuntu:24.04/noble-updates [amd64])
Inst libfoo:i386 [1.0-1] (1.0-2 Ubuntu:24.04/noble-updates [i386])
Remv libold1 [1.0-1]
Purg libgone:amd64 [2.0]
Conf libc6 (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates [amd64])
Conf lindos-core (1.0.1 Lindos:stable [all])
"""


def test_parse_simulation_reads_upgrades_and_new_installs() -> None:
    plan = us.parse_simulation(SIM)
    by_name = {i.name: i for i in plan.items}
    assert len(plan.items) == 8
    libc = by_name["libc6"]
    assert (libc.package, libc.arch, libc.installed, libc.candidate) == ("libc6", "amd64", "2.39-0ubuntu8.3", "2.39-0ubuntu8.4")
    assert libc.origins == ["Ubuntu:24.04/noble-updates"] and not libc.is_new
    new_kernel = by_name["linux-image-6.8.0-47-generic"]
    assert new_kernel.installed is None and new_kernel.is_new and new_kernel.candidate == "6.8.0-47.47"
    assert plan.ok and plan.errors == []


def test_parse_simulation_multiple_origins_trailing_brackets_and_foreign_arch() -> None:
    plan = us.parse_simulation(SIM)
    by_name = {i.name: i for i in plan.items}
    assert by_name["libssl3t64"].origins == ["Ubuntu:24.04/noble-updates", "Ubuntu:24.04/noble-security"]
    assert by_name["libssl3t64"].security is True
    assert by_name["lindos-meta"].candidate == "1.0.1"                 # the trailing [] is not part of it
    assert by_name["google-chrome-stable"].origins == ["Google LLC:stable"]
    assert by_name["google-chrome-stable"].origin_label == "Apps"
    foreign = by_name["libfoo:i386"]
    assert (foreign.package, foreign.arch) == ("libfoo", "i386")


def test_parse_simulation_removals_kept_back_and_ignored_lines() -> None:
    plan = us.parse_simulation(SIM)
    assert [(r.package, r.version, r.purge) for r in plan.removals] == [("libold1", "1.0-1", False),
                                                                        ("libgone", "2.0", True)]
    assert plan.kept_back == ["gimp", "libreoffice-core", "something-else"]
    assert not any(i.name.startswith("Conf") for i in plan.items)          # Conf lines are not installs


def test_parse_simulation_errors_make_the_plan_not_ok() -> None:
    plan = us.parse_simulation("E: Unable to correct problems, you have held broken packages.\n")
    assert not plan.ok and "held broken packages" in plan.errors[0]


@pytest.mark.parametrize("text", ["", "\n", "garbage\nInst\nInst broken\nRemv\n", None])
def test_parse_simulation_tolerates_garbage(text) -> None:
    plan = us.parse_simulation(text or "")
    assert plan.items == [] and plan.removals == []


def test_parse_simulation_line_endings_do_not_matter() -> None:
    assert us.parse_simulation(SIM.replace("\n", "\r\n")).digest == us.parse_simulation(SIM).digest


def test_removal_names_strips_the_architecture_and_sorts() -> None:
    assert us.removal_names(SIM) == ["libgone", "libold1"]


# ---- print-uris --------------------------------------------------------------------------------
def test_parse_print_uris_sums_sizes_per_package() -> None:
    text = ("'http://archive.example/pool/libc6_2.39-0ubuntu8.4_amd64.deb' libc6_2.39-0ubuntu8.4_amd64.deb 2000000 SHA256:aa\n"
            "'http://archive.example/pool/libc6_2.39-0ubuntu8.4_i386.deb' libc6_2.39-0ubuntu8.4_i386.deb 1000 SHA256:bb\n"
            "'https://apt.example/stable/lindos-core_1.0.1_all.deb' lindos-core_1.0.1_all.deb 34567 SHA256:cc\n"
            "not a uri line\n")
    sizes = us.parse_print_uris(text)
    assert sizes == {"libc6": 2001000, "lindos-core": 34567}


def test_apply_sizes_marks_items_and_leaves_unknown_ones_alone() -> None:
    plan = us.parse_simulation(SIM)
    us.apply_sizes(plan, {"libc6": 5, "lindos-core": 7})
    by_name = {i.name: i.download_bytes for i in plan.items}
    assert by_name["libc6"] == 5 and by_name["lindos-core"] == 7 and by_name["lindos-meta"] is None


# =================================================================================================
# the plan digest
# =================================================================================================
def test_digest_format_and_stability() -> None:
    d = us.parse_simulation(SIM).digest
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", d)
    assert us.parse_simulation(SIM).digest == d


def test_digest_ignores_order_notes_conf_lines_and_sizes() -> None:
    lines = [ln for ln in SIM.splitlines() if ln.startswith(("Inst", "Remv", "Purg"))]
    reordered = "NOTE: whatever\n" + "\n".join(reversed(lines)) + "\nConf junk (1 x [y])\n"
    plan = us.parse_simulation(reordered)
    us.apply_sizes(plan, {"libc6": 99})
    assert plan.digest == us.parse_simulation(SIM).digest


def test_digest_changes_with_a_version_a_package_or_a_removal() -> None:
    base = us.parse_simulation(SIM).digest
    assert us.parse_simulation(SIM.replace("1.0.1 Lindos:stable [all])\nInst lindos-meta", "1.0.2 Lindos:stable [all])\nInst lindos-meta")).digest != base
    assert us.parse_simulation(SIM + "Inst extra (1 Ubuntu:24.04/noble [amd64])\n").digest != base
    assert us.parse_simulation(SIM.replace("Remv libold1 [1.0-1]\n", "")).digest != base


def test_digest_of_an_empty_plan_is_stable_and_valid() -> None:
    assert us.parse_simulation("").digest == us.parse_simulation("NOTE: x\n").digest
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", us.parse_simulation("").digest)


# =================================================================================================
# categories and neutral origin labels
# =================================================================================================
@pytest.mark.parametrize("package, origins, category, label, security", [
    ("libc6", ["Ubuntu:24.04/noble-updates"], "other", "Lindos base system", False),
    ("libssl3t64", ["Ubuntu:24.04/noble-updates", "Ubuntu:24.04/noble-security"], "security", "Security", True),
    ("linux-image-6.8.0-47-generic", ["Ubuntu:24.04/noble-security"], "drivers-kernel", "Security", True),
    ("linux-headers-6.8.0-47", ["Ubuntu:24.04/noble-updates"], "drivers-kernel", "Lindos base system", False),
    ("nvidia-driver-550", ["Ubuntu:24.04/noble-updates"], "drivers-kernel", "Lindos base system", False),
    ("intel-microcode", ["Ubuntu:24.04/noble-security"], "drivers-kernel", "Security", True),
    ("lindos-core", ["Lindos:stable"], "lindos", "Lindos", False),
    ("lindos-kernel", ["Lindos:stable"], "lindos", "Lindos", False),
    ("linux-image-6.14.0-lindos", ["Lindos:stable"], "drivers-kernel", "Lindos", False),
    ("google-chrome-stable", ["Google, Inc.:stable"], "apps", "Apps", False),
    ("microsoft-edge-stable", ["Microsoft:stable"], "apps", "Apps", False),
    ("steam-launcher", ["Valve:stable"], "apps", "Apps", False),
    ("firefox", ["linuxmint:zara/upstream"], "apps", "Lindos base system", False),
    ("mintupdate", ["Linux Mint:zara/upstream"], "other", "Lindos base system", False),
    ("thunar", ["Linux Mint:zara/upstream"], "other", "Lindos base system", False),
    ("something", ["Foo:bar/baz"], "other", "Other sources", False),
    ("nothing", [], "other", "Other sources", False),
])
def test_category_label_and_security_flag(package: str, origins: List[str], category: str, label: str,
                                          security: bool) -> None:
    assert us.categorize(package, origins) == category
    assert us.origin_label(origins) == label
    text = f"Inst {package} [1] (2 {', '.join(origins)} [amd64])".replace("(2  [amd64])", "(2 [amd64])")
    item = us.parse_simulation(text).items[0]
    assert item.security is security


def test_labels_never_name_the_base_distribution() -> None:
    every = [
        ["Ubuntu:24.04/noble-updates"], ["Ubuntu:24.04/noble-security"], ["linuxmint:zara/upstream"],
        ["Linux Mint:zara/import"], ["Debian:12/stable"], ["Debian-Security:12/stable-security"],
        ["Lindos:stable"], ["Google:stable"], ["Foo:bar"], [],
    ]
    for origins in every:
        label = us.origin_label(origins)
        assert label in us.ORIGIN_LABELS
        assert not re.search(r"mint|ubuntu|debian", label, re.I), (origins, label)
    for text in us.CATEGORY_TITLES.values():
        assert not re.search(r"mint|ubuntu|debian", text, re.I)


def test_origin_parts() -> None:
    assert us.origin_parts("Ubuntu:24.04/noble-updates") == ("Ubuntu", "24.04", "noble-updates")
    assert us.origin_parts("Lindos:stable") == ("Lindos", "", "stable")
    assert us.origin_parts("stable") == ("", "", "stable")


# =================================================================================================
# the state document and the atomic writer
# =================================================================================================
NOW = dt.datetime(2026, 9, 30, 8, 0, 3, tzinfo=UTC)


def _state(**overrides: Any) -> Dict[str, Any]:
    plan = us.parse_simulation(SIM)
    us.apply_sizes(plan, {"libc6": 1000, "lindos-core": 500})
    args: Dict[str, Any] = dict(
        now=NOW, refreshed_at="2026-09-30T08:00:03+00:00", refresh={"attempted": True, "ok": True, "message": ""},
        repo={"configured": True, "enabled": True, "url": "https://x/stable", "placeholder": False, "reachable": True},
        reboot_pending={"required": False, "packages": []}, reboot_reasons=[], booted_kernel="6.8.0-45-generic",
        held=["zzz"])
    args.update(overrides)
    return us.build_state(plan, **args)


def test_state_document_shape() -> None:
    state = _state()
    assert state["schema"] == 1 and state["refreshed_at"] == "2026-09-30T08:00:03+00:00"
    assert state["written_at"] == "2026-09-30T08:00:03+00:00"
    assert state["digest"] == us.parse_simulation(SIM).digest
    assert state["counts"]["total"] == 8
    assert state["counts"]["lindos"] == 2 and state["counts"]["security"] == 1
    assert state["counts"]["drivers-kernel"] == 2 and state["counts"]["apps"] == 1       # the kernel pair; chrome
    assert state["counts"]["other"] == 2                                                 # libc6, libfoo:i386
    assert state["download_bytes"] == 1500
    assert [g["id"] for g in state["groups"]] == ["lindos", "security", "drivers-kernel", "apps", "other"]
    assert state["removals"] == [{"name": "libold1", "version": "1.0-1", "purge": False},
                                 {"name": "libgone", "version": "2.0", "purge": True}]
    assert state["kept_back"] == ["gimp", "libreoffice-core", "something-else"]
    assert state["held"] == ["zzz"] and state["booted_kernel"] == "6.8.0-45-generic"
    assert state["plan_ok"] is True and state["plan_errors"] == []


def test_state_groups_have_titles_counts_and_items() -> None:
    state = _state()
    by_id = {g["id"]: g for g in state["groups"]}
    assert by_id["lindos"]["title"] == "Lindos" and by_id["lindos"]["count"] == 2
    assert by_id["lindos"]["download_bytes"] == 500
    item = next(i for i in by_id["lindos"]["items"] if i["name"] == "lindos-core")
    assert item["from"] == "1.0.0" and item["to"] == "1.0.1" and item["origin_label"] == "Lindos"
    assert by_id["security"]["items"][0]["name"] == "libssl3t64" and by_id["security"]["items"][0]["security"] is True
    assert "mint" not in json.dumps(state).lower()


def test_state_reboot_block() -> None:
    state = _state(reboot_pending={"required": True, "packages": ["linux-image-6.8.0-47-generic"]},
                   reboot_reasons=[{"kind": "reboot", "package": "linux-image-6.8.0-47-generic", "text": "new kernel"},
                                   {"kind": "relogin", "package": "lindos-desktop", "text": "sign out"}])
    reboot = state["reboot"]
    assert reboot["required"] is True and reboot["packages"] == ["linux-image-6.8.0-47-generic"]
    assert reboot["relogin"] == ["lindos-desktop"]
    assert reboot["would_require_reboot"] is True                     # libc6 and a kernel are in the plan
    assert "libc6" in reboot["would_require_reboot_packages"]


def test_state_is_json_serialisable() -> None:
    json.dumps(_state())


def test_write_json_atomic_writes_readable_json_and_makes_directories(core_env) -> None:
    target = paths.resolve("/var/lib/lindos/update-state.json")
    us.write_json_atomic(target, {"a": 1})
    assert json.loads(Path(target).read_text(encoding="utf-8")) == {"a": 1}
    assert Path(target).read_text(encoding="utf-8").endswith("\n")
    leftovers = [n for n in os.listdir(os.path.dirname(target)) if n.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_write_json_atomic_is_world_readable(core_env) -> None:
    target = paths.resolve("/var/lib/lindos/update-state.json")
    previous = os.umask(0o077)                                       # a strict umask must not make the state private
    try:
        us.write_json_atomic(target, {"a": 1})
    finally:
        os.umask(previous)
    assert os.stat(target).st_mode & 0o777 == 0o644


def test_write_json_atomic_never_leaves_a_half_written_file(core_env, monkeypatch) -> None:
    target = paths.resolve("/var/lib/lindos/update-state.json")
    us.write_json_atomic(target, {"version": "old"})

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        us.write_json_atomic(target, {"version": "new"})
    assert json.loads(Path(target).read_text(encoding="utf-8")) == {"version": "old"}      # the old file survived
    assert [n for n in os.listdir(os.path.dirname(target)) if n.endswith(".tmp")] == []


def test_write_json_atomic_cleans_up_when_the_data_cannot_be_serialised(core_env) -> None:
    target = paths.resolve("/var/lib/lindos/update-state.json")
    with pytest.raises(TypeError):
        us.write_json_atomic(target, {"bad": object()})
    assert not os.path.exists(target)
    assert [n for n in os.listdir(os.path.dirname(target)) if n.endswith(".tmp")] == []


def test_read_state_is_none_for_missing_or_broken_files(core_env) -> None:
    assert us.read_state() is None
    target = paths.resolve(us.STATE_PATH)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    Path(target).write_text("{ not json", encoding="utf-8")
    assert us.read_state() is None
    Path(target).write_text("[1, 2]", encoding="utf-8")
    assert us.read_state() is None
    Path(target).write_text('{"schema": 1}', encoding="utf-8")
    assert us.read_state() == {"schema": 1}


def test_state_age() -> None:
    state = {"refreshed_at": "2026-09-30T08:00:03+00:00"}
    assert us.state_age_seconds(state, now=NOW + dt.timedelta(hours=2)) == 7200
    assert us.state_age_seconds({"refreshed_at": None}) is None
    assert us.state_age_seconds({"refreshed_at": "junk"}) is None
    assert us.state_age_seconds({"refreshed_at": "2026-09-30T08:00:03"}, now=NOW) == 0     # naive = UTC


# =================================================================================================
# refresh_state (injected runner)
# =================================================================================================
URIS = "'https://apt.example/stable/lindos-core_1.0.1_all.deb' lindos-core_1.0.1_all.deb 34567 SHA256:aa\n"


class Runner:
    """Fake ``(argv, timeout) -> (rc, output)``: answers by the command's first words."""

    def __init__(self, sim: str = SIM, update: Tuple[int, str] = (0, ""), uris: str = URIS,
                 hold: str = "held-pkg\n") -> None:
        self.sim, self.update, self.uris, self.hold = sim, update, uris, hold
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float = 0) -> Tuple[int, str]:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd[:2] == ["apt-get", "update"]:
            return self.update
        if cmd[:4] == ["apt-get", "-q", "-s", "dist-upgrade"]:
            return 0, self.sim
        if "--print-uris" in cmd:
            return 0, self.uris
        if cmd[:2] == ["apt-mark", "showhold"]:
            return 0, self.hold
        return 1, "unexpected"

    def ran(self, *prefix: str) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


def _refresh(runner: Runner, **kwargs: Any) -> Dict[str, Any]:
    args: Dict[str, Any] = dict(now=NOW, running_kernel="6.8.0-45-generic", boot_epoch=None, kernels=[])
    args.update(kwargs)
    return us.refresh_state(runner=runner, **args)


def test_refresh_updates_lists_simulates_and_writes_the_state_file(core_env) -> None:
    runner = Runner()
    state = _refresh(runner)
    assert runner.ran("apt-get", "update", "-q") and runner.ran("apt-get", "-q", "-s", "dist-upgrade")
    assert state["refresh"]["ok"] is True and state["refreshed_at"] == "2026-09-30T08:00:03+00:00"
    assert state["held"] == ["held-pkg"]
    lindos = next(g for g in state["groups"] if g["id"] == "lindos")
    assert lindos["download_bytes"] == 34567                          # from --print-uris
    on_disk = json.loads(Path(paths.resolve(us.STATE_PATH)).read_text(encoding="utf-8"))
    assert on_disk == state


def test_refresh_never_runs_anything_that_installs(core_env) -> None:
    runner = Runner()
    _refresh(runner)
    for cmd in runner.calls:
        joined = " ".join(cmd)
        assert "install" not in joined and "upgrade -y" not in joined
        if "dist-upgrade" in joined:
            assert "-s" in cmd or "--print-uris" in cmd, joined       # only ever a simulation / a URI listing


def test_refresh_offline_skips_apt_get_update_but_still_writes_the_state(core_env) -> None:
    runner = Runner()
    state = _refresh(runner, offline=True)
    assert not runner.ran("apt-get", "update")
    assert state["refresh"]["attempted"] is False and "offline" in state["refresh"]["message"]
    assert state["refreshed_at"] is None or isinstance(state["refreshed_at"], str)
    assert state["counts"]["total"] == 8                              # the plan comes from the lists apt already has


def test_refresh_no_update_flag(core_env) -> None:
    runner = Runner()
    _refresh(runner, do_update=False)
    assert not runner.ran("apt-get", "update")


def test_a_failed_refresh_keeps_the_last_good_refreshed_at(core_env) -> None:
    _refresh(Runner())
    later = NOW + dt.timedelta(days=1)
    state = us.refresh_state(runner=Runner(update=(100, "Err:1 https://x InRelease\nE: Failed to fetch\n")), now=later,
                             running_kernel="", boot_epoch=None, kernels=[])
    assert state["refresh"]["ok"] is False and "Failed to fetch" in state["refresh"]["message"]
    assert state["refreshed_at"] == "2026-09-30T08:00:03+00:00"       # not "now": the lists were not refreshed
    assert state["written_at"] == later.isoformat(timespec="seconds")


def test_a_broken_dpkg_state_is_recorded_not_raised(core_env) -> None:
    state = _refresh(Runner(sim="E: dpkg was interrupted, you must manually run 'dpkg --configure -a'\n"))
    assert state["plan_ok"] is False and "dpkg was interrupted" in state["plan_errors"][0]
    assert state["groups"] == [] and state["counts"]["total"] == 0


def test_missing_apt_is_recorded_not_raised(core_env) -> None:
    def no_apt(argv: Sequence[str], timeout: float = 0) -> Tuple[int, str]:
        return 127, f"{argv[0]}: not found"
    state = us.refresh_state(runner=no_apt, now=NOW, running_kernel="", boot_epoch=None, kernels=[])
    assert state["plan_ok"] is False and state["refresh"]["ok"] is False


def test_refresh_reports_the_lindos_source_and_whether_it_answered(core_env) -> None:
    src = Path(paths.resolve("/etc/apt/sources.list.d/lindos.sources"))
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("Types: deb\nURIs: https://apt.example.test/stable/\nSuites: ./\n", encoding="utf-8")
    ok = _refresh(Runner(update=(0, "Hit:1 https://apt.example.test/stable ./ InRelease\n")))
    assert ok["repo"]["configured"] is True and ok["repo"]["url"] == "https://apt.example.test/stable"
    assert ok["repo"]["reachable"] is True
    bad = _refresh(Runner(update=(100, "Err:1 https://apt.example.test/stable ./ InRelease\n  Could not connect\n")))
    assert bad["repo"]["reachable"] is False
    quiet = _refresh(Runner(update=(0, "Hit:1 http://other.example/ubuntu noble InRelease\n")))
    assert quiet["repo"]["reachable"] is None                        # the update did not mention it: unknown
    src.write_text("Enabled: no\nTypes: deb\nURIs: https://apt.example.test/stable/\nSuites: ./\n", encoding="utf-8")
    off = _refresh(Runner())
    assert off["repo"]["configured"] is False and off["repo"]["enabled"] is False


def test_the_state_file_is_the_only_thing_refresh_writes(core_env) -> None:
    before = {p for p in Path(core_env["root"]).rglob("*") if p.is_file()}
    _refresh(Runner())
    after = {p for p in Path(core_env["root"]).rglob("*") if p.is_file()}
    new = {p.relative_to(core_env["root"]).as_posix() for p in after - before}
    assert new == {"var/lib/lindos/update-state.json"}


def test_main_refresh_command(core_env, monkeypatch, capsys) -> None:
    runner = Runner()
    monkeypatch.setattr(us, "default_runner", runner)
    assert us.main(["refresh", "--offline"]) == 0
    assert "update state written" in capsys.readouterr().out
    assert not runner.ran("apt-get", "update")
    assert us.read_state() is not None


def test_main_usage_errors(capsys) -> None:
    assert us.main([]) == 2
    assert us.main(["nope"]) == 2
    assert us.main(["refresh", "--frobnicate"]) == 2


def test_main_refresh_stays_out_of_the_way_of_a_running_upgrade(core_env, monkeypatch, capsys) -> None:
    """The check happens here too, after the queue behind other apt jobs (apt-serialise): the unit and the script
    looked long before, and an upgrade may have started while this run waited its turn."""
    runner = Runner()
    monkeypatch.setattr(us, "default_runner", runner)
    us.write_marker("apt-full-upgrade", digest="sha256:" + "0" * 64)
    assert us.main(["refresh"]) == 0
    assert "in progress" in capsys.readouterr().out
    assert runner.calls == [] and us.read_state() is None            # not a single apt command, no state written
    us.clear_marker()
    assert us.main(["refresh", "--offline"]) == 0
    assert us.read_state() is not None


# --- refreshed_at after a refresh the caller ran itself ------------------------------------------------------------
def test_lists_just_refreshed_stamps_refreshed_at_without_running_apt_get_update(core_env) -> None:
    """The helper's apt-get-update action runs the update itself and then rewrites the state with do_update=False;
    the stamp used to stay at its old value, so status kept calling a fresh refresh out of date."""
    _refresh(Runner())
    later = NOW + dt.timedelta(days=4)
    runner = Runner()
    state = us.refresh_state(runner=runner, now=later, do_update=False, lists_just_refreshed=True,
                             running_kernel="", boot_epoch=None, kernels=[])
    assert not runner.ran("apt-get", "update")                        # the caller did that
    assert state["refreshed_at"] == later.isoformat(timespec="seconds")
    assert state["refresh"]["ok"] is True and state["refresh"]["attempted"] is True
    assert us.state_age_seconds(state, now=later) == 0.0


def test_without_that_flag_a_state_only_rewrite_keeps_the_old_stamp(core_env) -> None:
    _refresh(Runner())
    later = NOW + dt.timedelta(days=4)
    state = us.refresh_state(runner=Runner(), now=later, do_update=False, running_kernel="", boot_epoch=None, kernels=[])
    assert state["refreshed_at"] == "2026-09-30T08:00:03+00:00" and state["refresh"]["attempted"] is False
    assert us.state_age_seconds(state, now=later) > us.STALE_AFTER_SECONDS      # the stale verdict this used to give


def test_offline_wins_over_lists_just_refreshed(core_env) -> None:
    state = us.refresh_state(runner=Runner(), now=NOW, offline=True, lists_just_refreshed=True, running_kernel="",
                             boot_epoch=None, kernels=[])
    assert state["refresh"]["attempted"] is False


# =================================================================================================
# reboot / re-login
# =================================================================================================
def _epoch(ts: str) -> float:
    return dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()


def _utc(value: dt.datetime) -> float:
    return value.replace(tzinfo=UTC).timestamp()


DPKG_LOG = """2026-09-29 20:00:00 upgrade libc6:amd64 2.39-0ubuntu8.3 2.39-0ubuntu8.4
2026-09-30 07:00:00 status installed libc6:amd64 2.39-0ubuntu8.4
2026-09-30 09:10:00 upgrade libc6:amd64 2.39-0ubuntu8.4 2.39-0ubuntu8.5
2026-09-30 09:11:00 install linux-image-6.8.0-47-generic:amd64 <none> 6.8.0-47.47
2026-09-30 09:12:00 upgrade systemd:amd64 255.4-1ubuntu8.4 255.4-1ubuntu8.5
2026-09-30 09:13:00 upgrade lindos-desktop:all 1.0.0 1.0.1
2026-09-30 09:14:00 upgrade coreutils:amd64 9.4-3ubuntu6 9.4-3ubuntu6.1
2026-09-30 09:15:00 upgrade libgtk-3-0t64:amd64 3.24.41-4ubuntu1.2 3.24.41-4ubuntu1.3
2026-09-30 09:16:00 remove linux-image-6.8.0-40-generic:amd64 6.8.0-40.40 <none>
garbage line
"""


def test_reboot_reasons_only_count_what_was_installed_after_boot() -> None:
    reasons = us.reboot_reasons_from_dpkg_log(DPKG_LOG, boot_epoch=_epoch("2026-09-30 08:00:00"), to_epoch=_utc)
    got = {r["package"]: r["kind"] for r in reasons}
    assert got == {"libc6": "reboot", "linux-image-6.8.0-47-generic": "reboot", "systemd": "reboot",
                   "libgtk-3-0t64": "reboot", "lindos-desktop": "relogin"}
    assert "coreutils" not in got and "linux-image-6.8.0-40-generic" not in got      # not on the list / a removal


def test_reboot_reasons_before_boot_do_not_count() -> None:
    assert us.reboot_reasons_from_dpkg_log(DPKG_LOG, boot_epoch=_epoch("2026-09-30 12:00:00"), to_epoch=_utc) == []


def test_reboot_reasons_have_plain_text() -> None:
    reasons = us.reboot_reasons_from_dpkg_log(DPKG_LOG, boot_epoch=_epoch("2026-09-30 08:00:00"), to_epoch=_utc)
    text = {r["package"]: r["text"] for r in reasons}
    assert "kernel" in text["linux-image-6.8.0-47-generic"].lower()
    assert "libraries" in text["libc6"].lower()
    assert "sign out" in text["lindos-desktop"].lower()
    for r in reasons:
        assert not re.search(r"mint|ubuntu", r["text"], re.I)


@pytest.mark.parametrize("running, installed, expected", [
    ("6.8.0-45-generic", ["6.8.0-45-generic", "6.8.0-47-generic", "7.0.0-34-generic"], "7.0.0-34-generic"),
    ("6.8.0-45-generic", ["6.8.0-45-generic", "6.8.0-40-generic"], None),
    ("6.8.0-47-generic", ["6.8.0-47-generic"], None),
    ("6.14.0-lindos", ["6.14.0-lindos", "6.14.0-29-generic", "7.0.0-34-generic"], None),        # another flavour never nags
    ("6.8.0-45-generic", ["6.8.0-100-generic"], "6.8.0-100-generic"),                          # numeric, not text, order
    ("garbage", ["6.8.0-45-generic"], None),
    ("6.8.0-45-generic", [], None),
    ("6.8.0-45-generic", ["junk", "6.8.0-46-generic"], "6.8.0-46-generic"),
])
def test_newer_kernel_installed(running: str, installed: List[str], expected) -> None:
    assert us.newer_kernel_installed(running, installed) == expected


def test_compute_reboot_reasons_adds_the_newer_kernel_once() -> None:
    log = "2026-09-30 09:11:00 install linux-image-6.8.0-47-generic:amd64 <none> 6.8.0-47.47\n"
    both = us.compute_reboot_reasons(running_kernel="6.8.0-45-generic", boot_epoch=_epoch("2026-09-30 08:00:00"),
                                     dpkg_log_text=log, kernels=["6.8.0-45-generic", "6.8.0-47-generic"], to_epoch=_utc)
    assert [r["package"] for r in both] == ["linux-image-6.8.0-47-generic"]            # not counted twice
    only_boot = us.compute_reboot_reasons(running_kernel="6.8.0-45-generic", boot_epoch=_epoch("2026-09-30 12:00:00"),
                                          dpkg_log_text=log, kernels=["6.8.0-45-generic", "6.8.0-47-generic"], to_epoch=_utc)
    assert [r["package"] for r in only_boot] == ["linux-image-6.8.0-47-generic"]        # from /boot alone (log rotated away)
    assert "newer kernel" in only_boot[0]["text"].lower()


def test_compute_reboot_reasons_without_a_boot_time_uses_only_the_kernel_check() -> None:
    reasons = us.compute_reboot_reasons(running_kernel="6.8.0-45-generic", boot_epoch=None, dpkg_log_text=DPKG_LOG,
                                        kernels=["6.8.0-45-generic"], to_epoch=_utc)
    assert reasons == [] or all(r["package"].startswith("linux-image-") for r in reasons)


def test_write_reboot_required_writes_the_marker_and_merges_the_package_list(core_env) -> None:
    reasons = [{"kind": "reboot", "package": "libc6", "text": "x"}, {"kind": "relogin", "package": "lindos-desktop", "text": "y"}]
    assert us.write_reboot_required(reasons) is True
    marker = Path(paths.resolve(us.REBOOT_REQUIRED_PATH))
    assert marker.read_text(encoding="utf-8") == "*** System restart required ***\n"
    assert Path(paths.resolve(us.REBOOT_REQUIRED_PKGS_PATH)).read_text(encoding="utf-8") == "libc6\n"   # relogin is not a reboot
    us.write_reboot_required([{"kind": "reboot", "package": "systemd", "text": "z"}])
    assert Path(paths.resolve(us.REBOOT_REQUIRED_PKGS_PATH)).read_text(encoding="utf-8") == "libc6\nsystemd\n"
    assert us.read_reboot_state() == {"required": True, "packages": ["libc6", "systemd"]}


def test_write_reboot_required_never_removes_the_marker_and_writes_nothing_without_reasons(core_env) -> None:
    assert us.write_reboot_required([]) is False
    assert not os.path.exists(paths.resolve(us.REBOOT_REQUIRED_PATH))
    us.write_reboot_required([{"kind": "reboot", "package": "libc6", "text": "x"}])
    assert us.write_reboot_required([]) is True                       # still there: only the restart clears /run
    assert us.write_reboot_required([{"kind": "relogin", "package": "lindos-desktop", "text": "y"}]) is True


def test_reboot_hook_end_to_end_on_a_scratch_root(core_env) -> None:
    root = Path(core_env["root"])
    (root / "proc").mkdir()
    (root / "proc" / "stat").write_text("cpu 1 2 3\nbtime %d\n" % _epoch("2026-09-30 08:00:00"), encoding="utf-8")
    (root / "var" / "log").mkdir(parents=True)
    (root / "var" / "log" / "dpkg.log").write_text(DPKG_LOG, encoding="utf-8")
    (root / "boot").mkdir()
    (root / "boot" / "vmlinuz-6.8.0-45-generic").write_text("", encoding="utf-8")
    reasons = us.reboot_hook(running_kernel="6.8.0-45-generic", to_epoch=_utc)
    assert {r["package"] for r in reasons if r["kind"] == "reboot"} >= {"libc6", "systemd"}
    assert us.read_reboot_state()["required"] is True


def test_reboot_hook_command_never_fails(core_env, monkeypatch) -> None:
    def broken(**_kw: Any) -> None:
        raise RuntimeError("boom")
    monkeypatch.setattr(us, "reboot_hook", broken)
    assert us.main(["reboot-hook"]) == 0                              # a hook must never break apt


def test_boot_epoch_from_proc_stat() -> None:
    assert us.boot_epoch_now(proc_stat="cpu 1\nbtime 1700000000\nprocesses 5\n") == 1700000000.0
    assert us.boot_epoch_now(proc_stat="cpu 1\n") is None
    assert us.boot_epoch_now(proc_stat="btime nope\n") is None


# =================================================================================================
# in-progress marker
# =================================================================================================
def test_marker_roundtrip(core_env) -> None:
    assert not us.marker_present()
    us.write_marker("apt-full-upgrade", digest="sha256:" + "a" * 64, now=NOW)
    assert us.marker_present()
    data = json.loads(Path(us.marker_path()).read_text(encoding="utf-8"))
    assert data["action"] == "apt-full-upgrade" and data["digest"].startswith("sha256:")
    us.clear_marker()
    assert not us.marker_present()
    us.clear_marker()                                                # clearing twice is fine


# =================================================================================================
# checking a plan
# =================================================================================================
def _check(sim: str, **kw: Any) -> List[str]:
    plan = us.parse_simulation(sim)
    args: Dict[str, Any] = dict(expected_digest=plan.digest, allow_kernel=False, allow_removals=False,
                                running_kernel="6.8.0-45-generic")
    args.update(kw)
    return us.check_upgrade_plan(plan, **args)


def test_a_plain_plan_passes() -> None:
    assert _check("Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])\n") == []


def test_check_refuses_a_digest_mismatch_with_a_plain_message() -> None:
    problems = _check("Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])\n", expected_digest="sha256:" + "0" * 64)
    assert len(problems) == 1 and "not the one you were shown" in problems[0]


def test_check_refuses_kernel_removals_and_protected_removals() -> None:
    kernel = "Inst linux-image-6.8.0-47-generic (6.8.0-47.47 Ubuntu:24.04/noble-updates [amd64])\n"
    assert any("kernel" in p for p in _check(kernel))
    assert _check(kernel, allow_kernel=True) == []
    remove = "Remv libold1 [1.0]\n"
    assert any("removals" in p for p in _check(remove))
    assert _check(remove, allow_removals=True) == []
    for protected in ("lindos-desktop", "xfce4-panel", "lightdm", "network-manager", "plymouth-theme-x", "grub-pc",
                      "systemd", "libc6:i386", "linux-image-6.8.0-45-generic", "linux-modules-6.8.0-45-generic"):
        problems = _check(f"Remv {protected} [1.0]\n", allow_removals=True)
        assert any("protected" in p for p in problems), protected


def test_check_refuses_a_plan_apt_could_not_compute() -> None:
    plan = us.parse_simulation("E: broken\n")
    problems = us.check_upgrade_plan(plan, expected_digest=plan.digest, allow_kernel=True, allow_removals=True,
                                     running_kernel="")
    assert problems and "cannot compute" in problems[0]


# =================================================================================================
# the safe cleanup planner
# =================================================================================================
class CleanupRunner:
    def __init__(self, autoremove: str, purge: str = "") -> None:
        self.autoremove, self.purge = autoremove, purge
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float = 0) -> Tuple[int, str]:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd[:5] == ["apt-get", "-q", "-s", "autoremove", "--purge"]:
            return 0, self.autoremove
        if cmd[:5] == ["apt-get", "-q", "-s", "purge", "--"]:
            return 0, self.purge or "".join(f"Purg {n} [1]\n" for n in cmd[5:])
        return 1, ""


KERNELS = ["6.8.0-38-generic", "6.8.0-40-generic", "6.8.0-42-generic", "6.8.0-45-generic"]


def test_decide_cleanup_keeps_kernels_desktop_and_proves_the_rest() -> None:
    runner = CleanupRunner("Purg libold1 [1]\nPurg linux-image-6.8.0-38-generic [1]\nPurg linux-image-6.8.0-45-generic [1]\n"
                           "Purg linux-headers-6.8.0-42 [1]\nPurg xfce4-x [1]\nPurg gnome-thing [1]\n")
    decision = us.decide_cleanup(runner, running_kernel="6.8.0-45-generic", installed_kernels=KERNELS)
    assert sorted(decision.packages) == ["libold1", "linux-image-6.8.0-38-generic"]
    kept = {name for name, _why in decision.kept}
    assert kept == {"linux-image-6.8.0-45-generic", "linux-headers-6.8.0-42", "xfce4-x", "gnome-thing"}
    assert decision.refused == ""


def test_decide_cleanup_a_running_kernel_that_is_not_the_newest_stays() -> None:
    runner = CleanupRunner("Purg linux-image-6.8.0-38-generic [1]\nPurg linux-image-6.8.0-40-generic [1]\n")
    decision = us.decide_cleanup(runner, running_kernel="6.8.0-40-generic", installed_kernels=KERNELS)
    assert decision.packages == ["linux-image-6.8.0-38-generic"]
    assert ("linux-image-6.8.0-40-generic" in [k for k, _ in decision.kept])


def test_decide_cleanup_refuses_when_the_subset_drags_something_along() -> None:
    runner = CleanupRunner("Purg libold1 [1]\n", purge="Purg libold1 [1]\nPurg lightdm [1]\n")
    decision = us.decide_cleanup(runner, running_kernel="6.8.0-45-generic", installed_kernels=KERNELS)
    assert decision.packages == [] and "lightdm" in decision.refused


def test_decide_cleanup_refuses_more_than_the_limit() -> None:
    runner = CleanupRunner("".join(f"Purg libjunk{i} [1]\n" for i in range(us.CLEANUP_MAX_PACKAGES + 1)))
    decision = us.decide_cleanup(runner, running_kernel="6.8.0-45-generic", installed_kernels=KERNELS)
    assert decision.packages == [] and decision.refused
    assert len(runner.calls) == 1                                       # it never even asked apt to purge


def test_decide_cleanup_nothing_to_do_and_apt_failure() -> None:
    assert us.decide_cleanup(CleanupRunner(""), running_kernel="", installed_kernels=[]).nothing_to_do
    failing = lambda argv, timeout=0: (100, "E: locked")  # noqa: E731
    decision = us.decide_cleanup(failing, running_kernel="", installed_kernels=[])
    assert decision.refused and not decision.packages


def test_kernel_helpers() -> None:
    assert us.kernel_release_of("linux-image-6.8.0-45-generic") == "6.8.0-45-generic"
    assert us.kernel_release_of("linux-headers-6.8.0-45") == "6.8.0-45"
    assert us.kernel_release_of("linux-image-6.14.0-lindos") == "6.14.0-lindos"
    assert us.kernel_release_of("linux-image-generic") is None
    assert us.kernel_release_of("linux-firmware") is None
    assert us.kernel_base("6.8.0-45-generic") == "6.8.0-45" and us.kernel_base("6.14.0-lindos") == "6.14.0-lindos"
    assert us.running_kernel_packages("6.8.0-45-generic")[0] == "linux-image-6.8.0-45-generic"


# =================================================================================================
# apt history
# =================================================================================================
HISTORY = """Start-Date: 2026-09-28  10:00:00
Commandline: apt install gimp
Requested-By: hp (1000)
Install: gimp:amd64 (2.10.36-3build3), libgimp2.0:amd64 (2.10.36-3build3, automatic)
End-Date: 2026-09-28  10:00:30

Start-Date: 2026-09-29  08:12:03
Commandline: apt-get install --only-upgrade -y -q -- lindos-core=1.0.1
Requested-By: hp (1000)
Upgrade: lindos-core:amd64 (1.0.0, 1.0.1), lindos-meta:all (1.0.0, 1.0.1), libc6:amd64 (2.39-0ubuntu8.3, 2.39-0ubuntu8.4)
End-Date: 2026-09-29  08:12:09

Start-Date: 2026-09-30  09:00:00
Commandline: apt-get purge -y -q -- libold1
Remove: libold1:amd64 (1.0-1)
Purge: libgone:amd64 (2.0)
Error: Sub-process /usr/bin/dpkg returned an error code (1)
End-Date: 2026-09-30  09:00:05
"""


def test_parse_history() -> None:
    entries = us.parse_history(HISTORY)
    assert [e["start"] for e in entries] == ["2026-09-28 10:00:00", "2026-09-29 08:12:03", "2026-09-30 09:00:00"]
    first, second, third = entries
    assert first["commandline"] == "apt install gimp" and first["requested_by"] == "hp (1000)"
    assert first["actions"]["Install"] == [{"name": "gimp", "version": "2.10.36-3build3", "automatic": False},
                                           {"name": "libgimp2.0", "version": "2.10.36-3build3", "automatic": True}]
    assert second["actions"]["Upgrade"][0] == {"name": "lindos-core", "from": "1.0.0", "to": "1.0.1"}
    assert len(second["actions"]["Upgrade"]) == 3 and second["end"] == "2026-09-29 08:12:09"
    assert third["actions"]["Remove"][0]["name"] == "libold1" and third["actions"]["Purge"][0]["name"] == "libgone"
    assert "dpkg returned an error" in third["error"] and first["error"] is None


def test_parse_history_tolerates_garbage() -> None:
    assert us.parse_history("") == []
    assert us.parse_history("not a log\n\n???\n") == []
    assert us.parse_history("Commandline: orphan line without a start\n") == []


def test_read_history_merges_rotated_logs_newest_first(core_env) -> None:
    logdir = Path(paths.resolve("/var/log/apt"))
    logdir.mkdir(parents=True)
    (logdir / "history.log").write_text(HISTORY.split("\n\n", 1)[1], encoding="utf-8")           # the newer two
    (logdir / "history.log.1").write_text("Start-Date: 2026-09-20  10:00:00\nCommandline: apt update\nEnd-Date: 2026-09-20  10:00:01\n",
                                          encoding="utf-8")
    with gzip.open(logdir / "history.log.2.gz", "wt", encoding="utf-8") as fh:
        fh.write("Start-Date: 2026-09-10  10:00:00\nCommandline: apt full-upgrade\nUpgrade: a:amd64 (1, 2)\nEnd-Date: 2026-09-10  10:00:01\n")
    (logdir / "term.log").write_text("Log started\n", encoding="utf-8")                          # never read
    starts = [e["start"] for e in us.read_history(50)]
    assert starts == ["2026-09-30 09:00:00", "2026-09-29 08:12:03", "2026-09-20 10:00:00", "2026-09-10 10:00:00"]
    assert [e["start"] for e in us.read_history(2)] == ["2026-09-30 09:00:00", "2026-09-29 08:12:03"]
    assert us.read_history(0) == []


def test_read_history_without_logs_is_empty(core_env) -> None:
    assert us.read_history(10) == []


# =================================================================================================
# the module is importable and dependency-free
# =================================================================================================
def test_module_is_stdlib_only_and_lazy() -> None:
    src = (Path(us.__file__)).read_text(encoding="utf-8")
    assert "import gi" not in src and "from gi" not in src
    assert "shell=True" not in src                                    # nothing is ever run through a shell
