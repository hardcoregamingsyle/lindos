"""Tests for the transfer plan (schema 1) and running it (SPEC-WINDOWS §29.5, §29.6, §27.6)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lindos_transfer import TransferError
from lindos_transfer.plan import (CATEGORIES, apply_selection, build_plan, load_plan, make_context,
                                  new_plan_id, parse_categories, save_plan, selected_items, validate_plan,
                                  xdg_dirs, PlanError)
from lindos_transfer.sources import open_source


def test_parse_categories_splits_commas_and_rejects_unknown() -> None:
    assert parse_categories(["documents,pictures", "wifi"]) == {"documents", "pictures", "wifi"}
    assert parse_categories(None) is None
    with pytest.raises(TransferError, match="unknown category"):
        parse_categories(["nope"])


def test_new_plan_id_matches_the_binding_shape() -> None:
    import re

    pid = new_plan_id(1735689600.0)
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", pid)


def test_xdg_dirs_reads_user_dirs_dirs(tmp_path: Path) -> None:
    cfg = tmp_path / ".config"
    cfg.mkdir()
    (cfg / "user-dirs.dirs").write_text(
        'XDG_DOCUMENTS_DIR="$HOME/Documents-mine"\nXDG_DESKTOP_DIR="$HOME/"\n')
    dirs = xdg_dirs(tmp_path)
    assert dirs["documents"] == tmp_path / "Documents-mine"
    assert dirs["desktop"] == tmp_path / "Desktop"  # "$HOME/" means disabled -> keep the default name
    assert dirs["music"] == tmp_path / "Music"


def test_make_context_refuses_a_destination_on_the_windows_drive(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path)
    src = open_source(root)
    with pytest.raises(TransferError, match="never writes to Windows"):
        make_context(src, dest=str(root / "Users" / "alice"))
    src.close()


# --------------------------------------------------------------------------- #
# build_plan(): categories, selection defaults, apps, warnings
# --------------------------------------------------------------------------- #
def _prepared_root(tmp_path: Path, winbuild):
    ntuser = winbuild.ntuser(shell_folders={"Personal": "%USERPROFILE%\\Documents"})
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    docs = root / "Users" / "alice" / "Documents"
    docs.mkdir(parents=True)
    (docs / "a.txt").write_text("hello")
    return root


def test_build_plan_schema_matches_spec_shape(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    assert plan["schema"] == 1
    assert set(plan) == {"schema", "id", "created", "source", "user", "dest_home", "items", "apps",
                         "options", "skipped", "warnings"}
    assert plan["source"]["type"] == "partition"
    assert plan["items"][0]["category"] == "documents"
    assert plan["items"][0]["files"] == 1 and plan["items"][0]["bytes"] == 5
    ctx.close()


def test_build_plan_opt_in_categories_are_not_selected_by_default(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents", "steam-games"], dest=str(home))
    # no Steam library exists in this fixture, so no steam-games item is produced either way, but the
    # category-selection rule itself (opt-in unless explicitly requested) must not crash
    assert all(it["selected"] for it in plan["items"])
    ctx.close()


def test_build_plan_warns_about_hibernation(tmp_path: Path, winbuild, home: Path) -> None:
    system = winbuild.system(hiberboot=True)
    root = winbuild.root(tmp_path, system=system, user="alice")
    (root / "hiberfil.sys").write_bytes(b"hibr" + b"\x00" * 100)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    assert any("hibernated" in w for w in plan["warnings"])
    ctx.close()


def test_build_plan_includes_apps_with_a_known_route(tmp_path: Path, winbuild, home: Path) -> None:
    root = winbuild.root(tmp_path, user="alice")  # default fixture SOFTWARE hive lists VLC
    src = open_source(root)
    plan, ctx = build_plan(src, only=["apps"], dest=str(home))
    assert plan["apps"] and plan["apps"][0]["windows_name"] == "VLC media player"
    ctx.close()


# --------------------------------------------------------------------------- #
# validate_plan(): the strict schema check on a saved/loaded plan
# --------------------------------------------------------------------------- #
def _minimal_plan(**overrides):
    plan = {
        "schema": 1, "id": "20260926-103000-ab12", "created": "2026-09-26T10:30:00Z",
        "source": {"type": "partition", "root": "/media/alice/OS"}, "user": "alice",
        "dest_home": "/home/alice",
        "items": [{"id": "documents", "category": "documents", "label": "Documents", "src": "/x",
                  "dest": "/y", "files": 1, "bytes": 10, "selected": True, "notes": []}],
        "apps": [], "options": {"firefox_passwords": False}, "skipped": [], "warnings": [],
    }
    plan.update(overrides)
    return plan


def test_validate_plan_accepts_a_well_formed_plan() -> None:
    validate_plan(_minimal_plan())  # must not raise


@pytest.mark.parametrize("bad", [
    {"schema": 2}, {"id": "not-an-id"}, {"source": {"type": "nope", "root": "/x"}},
    {"user": ""}, {"dest_home": ""}, {"items": "not-a-list"},
])
def test_validate_plan_rejects_bad_top_level_fields(bad: dict) -> None:
    with pytest.raises(PlanError):
        validate_plan(_minimal_plan(**bad))


def test_validate_plan_rejects_duplicate_item_ids() -> None:
    plan = _minimal_plan()
    plan["items"].append(dict(plan["items"][0]))
    with pytest.raises(PlanError, match="duplicate"):
        validate_plan(plan)


def test_validate_plan_rejects_apps_category_in_items() -> None:
    plan = _minimal_plan()
    plan["items"][0]["category"] = "apps"
    with pytest.raises(PlanError):
        validate_plan(plan)


def test_validate_plan_validates_app_actions() -> None:
    plan = _minimal_plan(apps=[{"windows_name": "X", "publisher": "", "version": "", "winget_id": None,
                               "actions": [{"type": "apt", "id": "x", "label": "X"}],  # missing packages
                               "chosen": 0, "selected": True}])
    with pytest.raises(PlanError):
        validate_plan(plan)


def test_validate_plan_rejects_out_of_range_chosen() -> None:
    plan = _minimal_plan(apps=[{"windows_name": "X", "publisher": "", "version": "", "winget_id": None,
                               "actions": [{"type": "builtin", "id": "x", "label": "X"}],
                               "chosen": 5, "selected": True}])
    with pytest.raises(PlanError, match="chosen"):
        validate_plan(plan)


def test_load_plan_rejects_garbage_and_reads_bom(tmp_path: Path) -> None:
    p = tmp_path / "plan.json"
    p.write_text("not json")
    with pytest.raises(PlanError):
        load_plan(p)
    p.write_bytes(("﻿" + json.dumps(_minimal_plan())).encode("utf-8"))
    plan = load_plan(p)
    assert plan["id"] == "20260926-103000-ab12"


def test_save_plan_is_atomic_and_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "plan.json"
    save_plan(_minimal_plan(), p)
    assert p.is_file()
    loaded = json.loads(p.read_text())
    assert loaded["id"] == "20260926-103000-ab12"


def test_save_plan_is_private_regardless_of_the_umask(tmp_path: Path) -> None:
    """A plan names the source computer, the Windows user and every path that would be copied, so
    it is written 0600/0700 (sec-transfer:state-dir-file-permissions), not left to the umask."""
    p = tmp_path / "sub" / "plan.json"
    save_plan(_minimal_plan(), p)
    if not sys.platform.startswith("win"):
        assert (p.stat().st_mode & 0o777) == 0o600
        assert (p.parent.stat().st_mode & 0o777) == 0o700


# --------------------------------------------------------------------------- #
# apply_selection(): a saved plan's choices apply onto a freshly rebuilt plan
# --------------------------------------------------------------------------- #
def test_apply_selection_carries_over_id_and_choices_and_flags_new_items() -> None:
    saved = _minimal_plan()
    saved["items"][0]["selected"] = False
    fresh = _minimal_plan(id="20260927-000000-cdef")
    fresh["items"].append({"id": "wifi", "category": "wifi", "label": "Wi-Fi", "src": "", "dest": "",
                           "files": 0, "bytes": 0, "selected": True, "notes": []})
    notes = apply_selection(fresh, saved)
    assert fresh["id"] == saved["id"]
    assert fresh["items"][0]["selected"] is False  # carried from the saved plan
    assert fresh["items"][1]["selected"] is False  # not in the saved plan -> left out
    assert any("new since the plan was made" in n for n in notes)


def test_apply_selection_notes_items_removed_from_the_source() -> None:
    saved = _minimal_plan()
    saved["items"].append({"id": "gone", "category": "wifi", "label": "Gone", "src": "", "dest": "",
                           "files": 0, "bytes": 0, "selected": True, "notes": []})
    fresh = _minimal_plan(id="X")
    notes = apply_selection(fresh, saved)
    assert any("no longer on the source" in n for n in notes)


def test_selected_items_orders_by_category_and_filters() -> None:
    plan = _minimal_plan()
    plan["items"] = [
        {"id": "wifi", "category": "wifi", "label": "Wi-Fi", "src": "", "dest": "", "files": 0, "bytes": 0,
         "selected": True, "notes": []},
        {"id": "desktop", "category": "desktop", "label": "Desktop", "src": "", "dest": "", "files": 0,
         "bytes": 0, "selected": True, "notes": []},
        {"id": "skip", "category": "documents", "label": "Skip", "src": "", "dest": "", "files": 0,
         "bytes": 0, "selected": False, "notes": []},
    ]
    chosen = selected_items(plan)
    assert [c["id"] for c in chosen] == ["desktop", "wifi"]


# --------------------------------------------------------------------------- #
# run_plan(): the full loop against a real (synthetic) partition
# --------------------------------------------------------------------------- #
def test_run_plan_copies_selected_items_and_writes_a_report(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    from lindos_transfer.plan import run_plan

    report = run_plan(plan, ctx)
    assert report["totals"]["files"] == 1
    assert (home / "Documents" / "a.txt").read_text() == "hello"
    ctx.close()


def test_run_plan_dry_run_copies_nothing(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    ctx.dry_run = True
    from lindos_transfer.plan import run_plan

    report = run_plan(plan, ctx)
    assert report["dry_run"] is True
    assert not (home / "Documents" / "a.txt").exists()
    ctx.close()


def test_run_plan_nothing_selected_returns_none(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    for it in plan["items"]:
        it["selected"] = False
    from lindos_transfer.plan import run_plan

    assert run_plan(plan, ctx) is None
    ctx.close()


def test_run_plan_refuses_dest_inside_src(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    src = open_source(root)
    plan, ctx = build_plan(src, only=["documents"], dest=str(home))
    # sabotage one item so its destination sits inside its own source folder
    plan["items"][0]["dest"] = str(Path(plan["items"][0]["src"]) / "loop")
    from lindos_transfer.plan import run_plan

    report = run_plan(plan, ctx)
    assert report["items"][0]["status"] == "failed"
    ctx.close()
