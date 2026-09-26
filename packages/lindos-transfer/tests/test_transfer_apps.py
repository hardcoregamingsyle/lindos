"""Tests for the installed-programs inventory, app-map matching and install executors
(SPEC-WINDOWS §29.9)."""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any, Dict

import pytest

from lindos_transfer import apps
from lindos_transfer.sources import open_source


# --------------------------------------------------------------------------- #
# the real app-map.json ships and is well-formed (>= 80 entries, every action valid)
# --------------------------------------------------------------------------- #
def test_real_app_map_has_at_least_80_valid_entries() -> None:
    app_map = apps.load_app_map()
    entries = app_map["apps"]
    assert len(entries) >= 80
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "app-map.json ids must be unique"
    for entry in entries:
        assert entry.get("match"), f"{entry['id']}: needs at least one match pattern"
        for act in entry["actions"]:
            apps.validate_action(act)  # must not raise


def test_real_app_map_maps_known_anticheat_titles_to_not_possible() -> None:
    app_map = apps.load_app_map()
    for name in ("VALORANT", "League of Legends", "Fortnite"):
        entry = apps.match_app_map_entry(app_map, name)
        assert entry is not None, name
        assert entry["actions"][0]["type"] == "not_possible"
        assert "lindos-game route" in entry["actions"][0]["note"]


def test_real_app_map_disambiguates_rust_game_from_rust_toolchain() -> None:
    app_map = apps.load_app_map()
    assert apps.match_app_map_entry(app_map, "Rust", "Facepunch") is not None
    assert apps.match_app_map_entry(app_map, "Rust", "") is None
    assert apps.match_app_map_entry(app_map, "Rust 1.75.0 (MSVC, x64)", "The Rust Project Developers") is None


def test_load_app_map_never_raises_on_a_missing_file(tmp_path: Path) -> None:
    m = apps.load_app_map(tmp_path / "nope.json")
    assert m == {"schema": 1, "apps": []}


def test_load_app_map_never_raises_on_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert apps.load_app_map(p) == {"schema": 1, "apps": []}


def test_load_app_map_rejects_wrong_schema(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": 2, "apps": []}))
    assert apps.load_app_map(p) == {"schema": 1, "apps": []}


# --------------------------------------------------------------------------- #
# match_app_map_entry(): patterns, publisher narrowing
# --------------------------------------------------------------------------- #
def _map(*entries):
    return {"schema": 1, "apps": list(entries)}


def test_match_first_entry_wins() -> None:
    m = _map({"id": "a", "match": ["Foo"], "actions": [{"type": "builtin", "id": "x", "label": "X"}]},
             {"id": "b", "match": ["Foo"], "actions": [{"type": "builtin", "id": "y", "label": "Y"}]})
    assert apps.match_app_map_entry(m, "Foobar")["id"] == "a"


def test_match_publisher_filter_only_applies_when_publisher_known() -> None:
    m = _map({"id": "a", "match": ["^X$"], "publisher": ["ACME"],
             "actions": [{"type": "builtin", "id": "x", "label": "X"}]})
    assert apps.match_app_map_entry(m, "X", "ACME") is not None
    assert apps.match_app_map_entry(m, "X", "Other") is None
    assert apps.match_app_map_entry(m, "X", "") is None


def test_match_bad_regex_is_skipped_not_fatal() -> None:
    m = _map({"id": "a", "match": ["(unclosed"], "actions": []})
    assert apps.match_app_map_entry(m, "(unclosed") is None


# --------------------------------------------------------------------------- #
# validate_action()
# --------------------------------------------------------------------------- #
def test_validate_action_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "nonsense", "id": "x", "label": "X"})


def test_validate_action_rejects_missing_id_or_label() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "builtin", "label": "X"})
    with pytest.raises(ValueError):
        apps.validate_action({"type": "builtin", "id": "x"})


def test_validate_action_apt_requires_packages() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "apt", "id": "vlc", "label": "VLC"})
    with pytest.raises(ValueError):
        apps.validate_action({"type": "apt", "id": "vlc", "label": "VLC", "packages": ["Bad Name!"]})
    apps.validate_action({"type": "apt", "id": "vlc", "label": "VLC", "packages": ["vlc"]})  # ok


def test_validate_action_flatpak_requires_reverse_dns_id() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "flatpak", "id": "notreversedns", "label": "X"})
    apps.validate_action({"type": "flatpak", "id": "com.example.App", "label": "X"})


def test_validate_action_browser_id_whitelist() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "browser", "id": "netscape", "label": "X"})
    apps.validate_action({"type": "browser", "id": "chrome", "label": "X"})


def test_validate_action_web_requires_https_url() -> None:
    with pytest.raises(ValueError):
        apps.validate_action({"type": "web", "id": "x", "label": "X", "url": "http://example.com"})
    apps.validate_action({"type": "web", "id": "x", "label": "X", "url": "https://example.com"})


# --------------------------------------------------------------------------- #
# inventory() / plan_apps(): partition (registry) and bundle sources
# --------------------------------------------------------------------------- #
def test_inventory_partition_reads_uninstall_keys(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path, user="alice")
    src = open_source(root)
    from lindos_transfer.plan import make_context

    ctx = make_context(src, dest=str(tmp_path / "home"))
    items = apps.inventory(ctx)
    assert any(i["name"] == "VLC media player" for i in items)
    ctx.close()


def test_inventory_bundle_reads_apps_json(tmp_path: Path) -> None:
    bundle = tmp_path / "usb"
    (bundle / "lindos-transfer.json").parent.mkdir(parents=True, exist_ok=True)
    (bundle / "apps.json").write_text(json.dumps([
        {"name": "Discord", "version": "1.0", "publisher": "Discord Inc.", "install_location": "",
         "scope": "user", "arch": "x64"},
    ]))
    (bundle / "lindos-transfer.json").write_text(json.dumps({
        "schema": 1, "computer": "OLD", "user": {"name": "carol"}, "apps": "apps.json"}))
    src = open_source(bundle)
    from lindos_transfer.plan import make_context

    ctx = make_context(src, dest=str(tmp_path / "home"))
    items = apps.inventory(ctx)
    assert items == [{"name": "Discord", "version": "1.0", "publisher": "Discord Inc.",
                      "install_location": "", "scope": "user", "arch": "x64", "source": "bundle"}]
    ctx.close()


def test_inventory_falls_back_to_start_menu_names(tmp_path: Path, winbuild) -> None:
    empty_software = winbuild.software(users={}, with_uninstall_entries=False)
    root = winbuild.root(tmp_path, software=empty_software, user="alice")
    start = root / "Users" / "alice" / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    start.mkdir(parents=True)
    (start / "My Cool App.lnk").write_bytes(b"not a real lnk")
    (start / "Uninstall My Cool App.lnk").write_bytes(b"x")
    src = open_source(root)
    from lindos_transfer.plan import make_context

    ctx = make_context(src, dest=str(tmp_path / "home"))
    items = apps.inventory(ctx)
    assert [i["name"] for i in items] == ["My Cool App"]
    ctx.close()


def test_plan_apps_matches_and_deselects_unmatched(tmp_path: Path, winbuild) -> None:
    software = winbuild.software(users={"S-1-5-21-1-2-3-1001": "C:\\Users\\alice"}, steam_path=None)
    root = winbuild.root(tmp_path, software=software, user="alice")
    src = open_source(root)
    from lindos_transfer.plan import make_context

    ctx = make_context(src, dest=str(tmp_path / "home"))
    result = apps.plan_apps(ctx)
    assert len(result) == 1
    vlc = result[0]
    assert vlc["windows_name"] == "VLC media player"
    assert vlc["actions"][0]["type"] == "apt"
    assert vlc["chosen"] == 0 and vlc["selected"] is True
    assert vlc["winget_id"] == "VideoLAN.VLC"
    ctx.close()


# --------------------------------------------------------------------------- #
# actions_match() / verify_apps_against_source(): a loaded plan's actions are never trusted at
# face value -- only actions that still match a fresh app-map.json match against the plan's own
# live source survive (sec-transfer:install-apps-untrusted-plan).
# --------------------------------------------------------------------------- #
def _real_vlc_plan(tmp_path: Path, winbuild) -> Dict[str, Any]:
    software = winbuild.software(users={"S-1-5-21-1-2-3-1001": "C:\\Users\\alice"}, steam_path=None)
    root = winbuild.root(tmp_path, software=software, user="alice")
    src = open_source(root)
    from lindos_transfer.plan import make_context

    ctx = make_context(src, dest=str(tmp_path / "home"))
    try:
        plan_apps_result = apps.plan_apps(ctx)
    finally:
        ctx.close()
    assert plan_apps_result and plan_apps_result[0]["windows_name"] == "VLC media player"
    return {"schema": 1, "id": "20260926-103000-ab12", "created": "2026-09-26T10:30:00Z",
            "source": {"type": "partition", "root": str(root), "computer": "X", "windows": "Windows 11",
                      "hibernated": False, "driver": "ntfs-3g"},
            "user": "alice", "dest_home": str(tmp_path / "home"), "items": [],
            "apps": plan_apps_result, "options": {"firefox_passwords": False}, "skipped": [], "warnings": []}


def test_actions_match_compares_content_not_identity() -> None:
    apt = {"type": "apt", "id": "vlc", "label": "VLC media player", "packages": ["vlc"]}
    assert apps.actions_match(dict(apt), dict(apt)) is True
    assert apps.actions_match(apt, {**apt, "packages": ["vlc-evil"]}) is False
    assert apps.actions_match(apt, {**apt, "id": "not-vlc"}) is False
    assert apps.actions_match(apt, {**apt, "type": "flatpak"}) is False
    assert apps.actions_match(apt, "not a dict") is False


def test_verify_apps_against_source_keeps_a_genuine_action(tmp_path: Path, winbuild) -> None:
    plan = _real_vlc_plan(tmp_path, winbuild)
    verified, notes = apps.verify_apps_against_source(plan)
    assert notes == []
    assert len(verified) == 1 and verified[0]["windows_name"] == "VLC media player"


def test_verify_apps_against_source_drops_a_tampered_action(tmp_path: Path, winbuild) -> None:
    plan = _real_vlc_plan(tmp_path, winbuild)
    # Tamper with the chosen action's packages after the plan was made -- exactly what an edited
    # or foreign plan file would do (validate_action's shape check alone would not catch this).
    plan["apps"][0]["actions"][0]["packages"] = ["totally-not-vlc"]
    verified, notes = apps.verify_apps_against_source(plan)
    assert verified == []
    assert any("VLC media player" in n for n in notes)


def test_verify_apps_against_source_drops_an_app_no_longer_on_the_source(tmp_path: Path, winbuild) -> None:
    plan = _real_vlc_plan(tmp_path, winbuild)
    plan["apps"][0]["windows_name"] = "Some App That Was Never Installed"
    verified, notes = apps.verify_apps_against_source(plan)
    assert verified == []
    assert notes


def test_verify_apps_against_source_ignores_unselected_apps(tmp_path: Path, winbuild) -> None:
    plan = _real_vlc_plan(tmp_path, winbuild)
    plan["apps"][0]["selected"] = False
    verified, notes = apps.verify_apps_against_source(plan)
    assert verified == [] and notes == []


def test_verify_apps_against_source_refuses_when_the_source_is_gone(tmp_path: Path, winbuild) -> None:
    plan = _real_vlc_plan(tmp_path, winbuild)
    import shutil as _shutil

    _shutil.rmtree(plan["source"]["root"])
    with pytest.raises(apps.TransferError):
        apps.verify_apps_against_source(plan)


def test_verify_apps_against_source_refuses_with_no_source_root() -> None:
    with pytest.raises(apps.TransferError):
        apps.verify_apps_against_source({"source": {}, "apps": []})


# --------------------------------------------------------------------------- #
# blocked_game(): honest, degrades to None when the matrix is unavailable
# --------------------------------------------------------------------------- #
def test_blocked_game_reads_the_shared_compat_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = tmp_path / "compat-matrix.json"
    matrix.write_text(json.dumps({"entries": [
        {"game": "Valorant", "status": "not_possible", "reason": "Vanguard", "anticheat": "Vanguard"},
        {"game": "Portal 2", "status": "native", "reason": ""},
    ]}))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    # place it where system_path("/usr/share/lindos/compat-matrix.json") resolves under LINDOS_ROOT
    dest = tmp_path / "usr" / "share" / "lindos" / "compat-matrix.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(matrix.read_bytes())
    assert apps.blocked_game(None, name="Valorant") is not None
    assert apps.blocked_game(None, name="Portal 2") is None
    assert apps.blocked_game(None, name="Nonexistent Game") is None


def test_blocked_game_degrades_gracefully_without_a_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))  # no compat-matrix.json exists under here
    assert apps.blocked_game(None, name="Valorant") is None


def test_blocked_game_empty_name_is_never_blocked() -> None:
    assert apps.blocked_game(None, name="") is None


# --------------------------------------------------------------------------- #
# install_apps(): nothing installed without selection, correct executor dispatch
# --------------------------------------------------------------------------- #
def _fake_run_ok(argv, **kw):
    return types.SimpleNamespace(returncode=0, stdout=json.dumps({"message": "done"}), stderr="")


def test_install_apps_skips_unselected_apps(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [
        {"windows_name": "A", "selected": False, "chosen": 0,
         "actions": [{"type": "apt", "id": "a", "label": "A", "packages": ["a"]}]},
    ]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper)
    assert results == []
    assert fake_helper.calls == []


def test_install_apps_skips_apps_with_no_chosen_index(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [{"windows_name": "A", "selected": True, "chosen": None, "actions": []}]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper)
    assert results == []


def test_install_apps_apt_action_calls_helper(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [{"windows_name": "VLC", "selected": True, "chosen": 0,
                      "actions": [{"type": "apt", "id": "vlc", "label": "VLC", "packages": ["vlc"]}]}]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper)
    assert results[0]["status"] == "done"
    assert fake_helper.calls == [("install-packages", {"packages": ["vlc"]})]


def test_install_apps_flatpak_and_browser_and_launcher(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [
        {"windows_name": "Discord", "selected": True, "chosen": 0,
         "actions": [{"type": "flatpak", "id": "com.discordapp.Discord", "label": "Discord"}]},
        {"windows_name": "Chrome", "selected": True, "chosen": 0,
         "actions": [{"type": "browser", "id": "chrome", "label": "Chrome"}]},
        {"windows_name": "Steam", "selected": True, "chosen": 0,
         "actions": [{"type": "launcher", "id": "steam", "label": "Steam"}]},
    ]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper)
    assert [r["status"] for r in results] == ["done", "done", "done"]
    assert ("install-flatpaks", {"flatpaks": ["com.discordapp.Discord"]}) in fake_helper.calls
    assert ("install-browser", {"browser": "chrome"}) in fake_helper.calls
    assert ("install-gaming", {"items": ["steam"]}) in fake_helper.calls


def test_install_apps_recipe_and_winget_call_lindos_compat(tmp_path: Path) -> None:
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"message": "ok"}), stderr="")

    plan = {"apps": [
        {"windows_name": "Notepad++", "selected": True, "chosen": 0,
         "actions": [{"type": "recipe", "id": "notepad-plus-plus", "label": "Notepad++"}]},
        {"windows_name": "Foo", "selected": True, "chosen": 0,
         "actions": [{"type": "winget", "id": "Foo.Foo", "label": "Foo"}]},
    ]}
    results = apps.install_apps(plan, home=tmp_path, run=run, which=lambda n: "/usr/bin/lindos-compat",
                                helper_mod=None)
    assert [r["status"] for r in results] == ["done", "done"]
    assert calls[0][:4] == ["lindos-compat", "recipes", "apply", "notepad-plus-plus"]
    assert calls[1][:4] == ["lindos-compat", "winget", "install", "Foo.Foo"]


def test_install_apps_recipe_fails_when_lindos_compat_missing(tmp_path: Path) -> None:
    plan = {"apps": [{"windows_name": "X", "selected": True, "chosen": 0,
                      "actions": [{"type": "recipe", "id": "x", "label": "X"}]}]}
    results = apps.install_apps(plan, home=tmp_path, which=lambda n: None, helper_mod=None)
    assert results[0]["status"] == "failed"
    assert "not installed" in results[0]["message"]


def test_install_apps_web_action_writes_a_desktop_shortcut(tmp_path: Path) -> None:
    plan = {"apps": [{"windows_name": "Notion", "selected": True, "chosen": 0,
                      "actions": [{"type": "web", "id": "notion-web", "label": "Notion",
                                  "url": "https://www.notion.so"}]}]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=None)
    assert results[0]["status"] == "done"
    shortcut = tmp_path / ".local" / "share" / "applications" / "lindos-webapp-notion-web.desktop"
    assert shortcut.is_file()
    text = shortcut.read_text()
    assert "Exec=xdg-open https://www.notion.so" in text


def test_install_apps_info_only_actions_never_touch_the_system(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [
        {"windows_name": "iTunes", "selected": True, "chosen": 0,
         "actions": [{"type": "vm", "id": "windows-vm", "label": "iTunes"}]},
        {"windows_name": "Valorant", "selected": True, "chosen": 0,
         "actions": [{"type": "not_possible", "id": "valorant", "label": "Valorant", "note": "blocked"}]},
    ]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper)
    assert [r["status"] for r in results] == ["done", "done"]
    assert fake_helper.calls == []
    assert results[1]["message"] == "blocked"


def test_install_apps_dry_run_never_calls_the_helper(tmp_path: Path, fake_helper) -> None:
    plan = {"apps": [{"windows_name": "VLC", "selected": True, "chosen": 0,
                      "actions": [{"type": "apt", "id": "vlc", "label": "VLC", "packages": ["vlc"]}]}]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper, dry_run=True)
    assert results[0]["status"] == "dry-run"
    assert fake_helper.calls == []


def test_install_apps_missing_lindos_core_reports_honestly(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, "_default_helper", lambda: None)
    plan = {"apps": [{"windows_name": "VLC", "selected": True, "chosen": 0,
                      "actions": [{"type": "apt", "id": "vlc", "label": "VLC", "packages": ["vlc"]}]}]}
    results = apps.install_apps(plan, home=tmp_path, helper_mod=None, which=lambda n: None)
    assert results[0]["status"] == "failed"
    assert "lindos-core" in results[0]["message"]


def test_install_apps_emits_progress_events(tmp_path: Path, fake_helper) -> None:
    events = []
    plan = {"apps": [{"windows_name": "VLC", "selected": True, "chosen": 0,
                      "actions": [{"type": "apt", "id": "vlc", "label": "VLC", "packages": ["vlc"]}]}]}
    apps.install_apps(plan, home=tmp_path, helper_mod=fake_helper, emit=events.append)
    kinds = [e["event"] for e in events]
    assert kinds == ["item", "item-done"]
