"""The "Not supported yet" disclaimer for kernel-anti-cheat / publisher-blocked games.

SPEC 0.1 / SPEC-KERNEL 14: Lindos states what is true *today* and never promises what only a
game's publisher can deliver. The wording lives once, in compat-matrix.json ("disclaimer"), and every
surface (Settings, lindos-game, docs/COMPATIBILITY.md) reads it from there.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import SHARE, load_bin, parse_desktop, read_json  # noqa: E402

MATRIX_PATH = SHARE / "compat-matrix.json"
CATALOGUE_PATH = SHARE / "gaming" / "launchers.json"
KINDS = {"no-linux-version", "publisher-disabled"}
#: wording that would promise what only the publishers can deliver (or hint at a date)
PROMISES = ("coming soon", "will be coming", "will be supported", "will support", "arriving", "next update",
            "in the near future", "guarantee", "eta ")
#: the vocabulary the shortcut/desktop-file scan in test_play_anywhere refuses
EVASION = ("attest", "vanguard", "battleye", " eac", "eac ", "spoof", "hwid", "bypass", "user-agent", "useragent")


@pytest.fixture(scope="module")
def matrix():
    return read_json(MATRIX_PATH)


@pytest.fixture(scope="module")
def game():
    return load_bin("lindos-game")


@pytest.fixture()
def no_windows(game, monkeypatch):
    monkeypatch.setattr(game, "dualboot_status", lambda: {"can_reboot_to_windows": False, "why": "no entry"})


def test_disclaimer_block_shape_and_honest_wording(matrix):
    d = matrix["disclaimer"]
    assert set(d) >= {"badge", "short", "long", "via", "kinds"}
    assert d["badge"] == "Not supported yet"
    assert set(d["kinds"]) == KINDS
    blob = " ".join([d["badge"], d["short"], d["long"], d["via"], *d["kinds"].values()]).lower()
    for phrase in PROMISES:
        assert phrase not in blob, f"disclaimer must not promise/date support: {phrase!r}"
    # the badge is never bare: who decides, that Lindos lists it only once tested, and that there is no date
    short = d["short"].lower()
    assert "publisher" in short and "tested" in short and "no date" in short
    assert "publisher" in d["long"].lower() and "cannot promise when" in d["long"].lower()
    assert "publisher" in d["via"].lower()
    assert "cloud" in d["long"].lower() and "windows" in d["long"].lower()
    assert "{game}" in d["via"] and "{route}" in d["via"]
    assert "<" not in d["long"] and "<" not in d["short"], "no angle-bracket placeholders (Markdown would swallow them)"


def test_kind_is_only_on_blocked_titles_and_never_on_the_xbox_app(matrix):
    for e in matrix["entries"]:
        kind = e.get("unsupported_kind")
        if kind is not None:
            assert e["status"] == "not_possible", f"{e['game']}: unsupported_kind only on not_possible entries"
            assert kind in KINDS, (e["game"], kind)
        elif e["status"] == "not_possible":
            # the only non-anti-cheat blocked entry: Store/UWP DRM is Microsoft's decision, no 'yet'
            assert e["game"] == "Xbox app / PC Game Pass"
    tagged = {e["game"] for e in matrix["entries"] if e.get("unsupported_kind")}
    for must in ("Valorant", "Fortnite", "League of Legends", "Apex Legends", "Rainbow Six Siege", "Destiny 2",
                 "PUBG: Battlegrounds", "Rust", "Grand Theft Auto Online"):
        assert must in tagged, must
    assert len(tagged) == 14


def test_the_machine_status_id_is_untouched(matrix):
    """Four consumers key on `not_possible` (transfer, Settings, lindos-game, lindos-compat): it must stay."""
    assert sum(1 for e in matrix["entries"] if e["status"] == "not_possible") == 15
    assert "not_possible" in matrix["statuses"]
    assert not any("not_supported" in e["status"] for e in matrix["entries"])


def test_route_json_carries_the_disclaimer_only_for_blocked_titles(game, matrix, capsys, no_windows):
    args = game.build_parser().parse_args(["route", "Valorant", "--json", "--region", "IN"])
    assert game.cmd_route(args, matrix, []) == game.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "not_possible"          # the machine status is unchanged
    dis = data["disclaimer"]
    assert dis["badge"] == "Not supported yet" and dis["kind"] == "no-linux-version" and dis["cause"]
    assert dis["short"] == matrix["disclaimer"]["short"]     # read from the matrix, not copied
    for title in ("Elden Ring", "Xbox app / PC Game Pass"):
        args = game.build_parser().parse_args(["route", title, "--json", "--region", "IN"])
        assert game.cmd_route(args, matrix, []) == game.EXIT_OK
        assert json.loads(capsys.readouterr().out)["disclaimer"] is None, title


def test_route_text_leads_with_the_disclaimer(game, matrix, capsys, no_windows):
    args = game.build_parser().parse_args(["route", "Fortnite", "--region", "IN"])
    assert game.cmd_route(args, matrix, []) == game.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith(" ! Not supported on Lindos yet")
    assert "publisher" in lines[1] and "no date" in lines[1] and "Why:" in lines[1]


def test_route_text_for_the_xbox_app_has_no_not_supported_yet_line(game, matrix, capsys, no_windows):
    args = game.build_parser().parse_args(["route", "Xbox app / PC Game Pass", "--region", "IN"])
    assert game.cmd_route(args, matrix, []) == game.EXIT_OK
    assert "supported on Lindos yet" not in capsys.readouterr().out


def test_entry_disclaimer_absent_without_matrix_block_or_kind(game, matrix):
    valorant = next(e for e in matrix["entries"] if e["game"] == "Valorant")
    assert game.entry_disclaimer(valorant, {"entries": []}) is None
    assert game.entry_disclaimer({"game": "x", "status": "works"}, matrix) is None


def test_disclaimer_via_fills_the_template_and_survives_data_quirks(game):
    dis = {"via": "{game} is out ({x}), so {route}."}
    assert game.disclaimer_via(dis, "Valorant", {"type": "windows"}) == \
        "Valorant is out ({x}), so a restart into your PC's own Windows."
    assert game.disclaimer_via(dis, "Fortnite", {"type": "cloud", "label": "NVIDIA GeForce NOW"}) == \
        "Fortnite is out ({x}), so NVIDIA GeForce NOW."
    assert game.disclaimer_via({}, "Fortnite", {"type": "cloud"}) == ""          # no template: nothing to say


def test_play_says_why_it_goes_through_another_route(game, matrix, monkeypatch, caplog):
    monkeypatch.setattr(game, "dualboot_status", lambda: {"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 2})
    monkeypatch.setattr(game, "_play_windows", lambda entry, chosen, args: game.EXIT_OK)
    args = game.build_parser().parse_args(["play", "Valorant", "--route", "windows", "--yes"])
    with caplog.at_level(logging.INFO, logger="lindos-game"):
        assert game.cmd_play(args, matrix, []) == game.EXIT_OK
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Valorant is not supported on Lindos yet") and "publisher" in m for m in said), said


def test_shortcut_comment_says_why_it_is_off_lindos_and_stays_clean(game, matrix, tmp_path, monkeypatch):
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setattr(game, "dualboot_status",
                        lambda: {"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 2})
    args = game.build_parser().parse_args(["shortcut", "Valorant", "--route", "windows"])
    assert game.cmd_shortcut(args, matrix, []) == game.EXIT_OK
    dest = tmp_path / ".local" / "share" / "applications" / "lindos-play-valorant-windows.desktop"
    entry = parse_desktop(dest)["Desktop Entry"]
    assert entry["Comment"].startswith("Valorant is not supported on Lindos yet (that is up to its publisher), "
                                       "so this plays it through a restart")
    blob = dest.read_text(encoding="utf-8").lower()
    for tok in EVASION:
        assert tok not in blob, tok


@pytest.mark.parametrize("field", ["badge", "short", "long", "via"])
def test_disclaimer_strings_pass_the_evasion_vocabulary_scan(matrix, field):
    text = matrix["disclaimer"][field].lower()
    for tok in EVASION:
        assert tok not in text, f"{field}: {tok!r}"


def test_the_launch_dialog_shows_the_catalogue_note(game, monkeypatch):
    """`lindos-game launch steam` on a system without Steam: the install question carries the honesty note."""
    launchers = game.load_catalogue(CATALOGUE_PATH)
    asked = []
    monkeypatch.setattr(game, "launch_command", lambda item: None)
    monkeypatch.setattr(game, "have", lambda binary: True)
    monkeypatch.setattr(game, "run", lambda cmd, timeout=30.0: (asked.append(cmd) or (1, "")))
    monkeypatch.setenv("DISPLAY", ":0")
    args = argparse.Namespace(id="steam", install=False, gamescope=None)
    assert game.cmd_launch(args, launchers) == game.EXIT_ERROR        # the user pressed Cancel
    text = next(a for a in asked[0] if a.startswith("--text="))
    assert "Steam is not installed yet." in text
    assert "not supported on Lindos yet" in text and "publishers" in text
    assert text.endswith("Install it now?")


def test_launcher_notes_pair_the_badge_with_the_publisher():
    notes = {i["id"]: i.get("note", "") for i in read_json(CATALOGUE_PATH)["launchers"]}
    for lid, who in (("steam", "publishers"), ("heroic", "epic")):     # who decides is named beside the badge
        low = notes[lid].lower()
        assert "not supported on lindos yet" in low and who in low, lid
        for phrase in PROMISES:
            assert phrase not in low, (lid, phrase)


def test_generated_doc_renders_the_disclaimer(matrix):
    import importlib.util

    gen = SHARE.parents[4].parent / "tests" / "gen-compat-doc.py"
    if not gen.is_file():
        pytest.skip("tests/gen-compat-doc.py not present in this checkout")
    spec = importlib.util.spec_from_file_location("gen_compat_doc_d", str(gen))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    entries = mod.load_matrix(str(MATRIX_PATH))
    rendered = mod.render(entries, disclaimer=mod.load_disclaimer(str(MATRIX_PATH)))
    assert f"> **Not supported yet.** {matrix['disclaimer']['long']}" in rendered
    assert re.search(r"\*\*Valorant\*\* · _Not supported yet_", rendered)
    assert rendered.count("_Not supported yet_") == 14                # one badge per disclaimed title
    assert "Not possible" in rendered                                  # machine status label kept
    assert "Xbox app / PC Game Pass** · _" not in rendered            # no 'yet' for Microsoft Store DRM
    assert "Not possible on Linux today (15)" in rendered              # the heading does not say 'yet' for all 15


def test_generated_doc_without_a_disclaimer_block_marks_nothing(matrix):
    import importlib.util

    gen = SHARE.parents[4].parent / "tests" / "gen-compat-doc.py"
    if not gen.is_file():
        pytest.skip("tests/gen-compat-doc.py not present in this checkout")
    spec = importlib.util.spec_from_file_location("gen_compat_doc_e", str(gen))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod.load_disclaimer(str(SHARE / "no-such-file.json")) == {}
    rendered = mod.render(mod.load_matrix(str(MATRIX_PATH)))
    assert "· _" not in rendered and "> **Not supported yet.**" not in rendered
