"""Tests for the ``lindos-transfer-gui`` wizard (SPEC-WINDOWS §29.12).

The GTK layer needs a real display, which this Windows host (and most CI runners) do not have, so
this file follows the project rule for GUI modules: import-smoke plus every pure-logic helper the
module exposes, none of it touching a real window.  ``tests/lindos_testsupport.py`` installs a
permissive stub ``gi`` module when the real PyGObject/GTK typelibs are missing, which is what makes
``import lindos_transfer.gui`` succeed here in the first place (see the module's own docstring).

Covered without a display:
* the module imports cleanly and exposes the documented API (import-smoke);
* the SPEC-WINDOWS §29.5/§29.7/§29.12 constants this file duplicates (categories, progress event
  names, wizard pages) match the contract exactly;
* ``format_size``/``format_count``/``action_type_text``;
* ``parse_progress_line`` on every event kind plus every way a line can be "not an event";
* ``TransferCli`` against a fake ``run``/``popen`` (never spawns a real process) -- argv shapes,
  JSON parsing, error handling (missing binary, timeout, non-zero exit, bad JSON), the ``users``
  list/dict envelope, the ``report`` "nothing yet" exit code, and the streamed ``run``/
  ``install-apps`` callbacks;
* the plan/app dict helpers the "What to bring" and "Apps" pages use;
* ``WizardState``'s page state machine (every ``can_go_back``/``can_go_next`` rule, ``go_next``/
  ``go_back``, and the source/user reset helper);
* ``build_arg_parser`` and ``run_app``'s early-exit paths (no display / CLI not installed), with
  ``_gtk_init_ok`` and the CLI availability check monkeypatched so nothing here depends on GTK
  actually being able to open a window.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

PKG = Path(__file__).resolve().parents[1]
LIB = PKG / "root" / "usr" / "lib" / "lindos-transfer"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

gui = pytest.importorskip("lindos_transfer.gui")


# --------------------------------------------------------------------------------------------
# import-smoke
# --------------------------------------------------------------------------------------------
def test_module_imports_and_exposes_its_api() -> None:
    for name in ("TransferCli", "CliError", "parse_progress_line", "WizardState", "TransferWizard",
                 "run_app", "build_arg_parser", "main", "format_size", "format_count",
                 "plan_items", "set_item_selected", "set_category_selected", "selected_totals",
                 "plan_apps", "set_app_selected", "set_app_chosen", "app_action_summary",
                 "plan_has_selection", "action_type_text", "run_exit_ok"):
        assert hasattr(gui, name), "missing export: %s" % name
    assert isinstance(gui.TransferWizard, type)


def test_module_never_imports_the_rest_of_lindos_transfer() -> None:
    """The GUI drives lindos-transfer as a subprocess only (see the module docstring); it must not
    import sibling modules owned by the concurrently-written CLI/engine."""
    src = (LIB / "lindos_transfer" / "gui.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("from . import") or stripped.startswith("from .") or \
                stripped.startswith("import lindos_transfer."):
            pytest.fail("gui.py must not import a sibling lindos_transfer module: %r" % stripped)


# --------------------------------------------------------------------------------------------
# SPEC-WINDOWS constants this file duplicates (must match the kit and the CLI contract exactly)
# --------------------------------------------------------------------------------------------
SPEC_CATEGORIES = ("desktop", "documents", "downloads", "music", "pictures", "videos", "saved-games",
                   "favorites", "onedrive", "bookmarks", "firefox", "wallpaper", "fonts", "wifi",
                   "apps", "steam-games")
SPEC_PROGRESS_EVENTS = {"start", "item", "file", "progress", "skip", "error", "done"}
SPEC_PAGE_IDS = ("welcome", "source", "user", "bring", "apps", "transfer", "done")


def test_categories_match_spec() -> None:
    assert gui.CATEGORIES == SPEC_CATEGORIES
    assert set(gui.LABELS) == set(SPEC_CATEGORIES)
    assert set(gui.FOLDER_CATEGORIES) <= set(SPEC_CATEGORIES)
    assert "onedrive" in gui.FOLDER_CATEGORIES and "apps" not in gui.FOLDER_CATEGORIES


def test_progress_events_match_spec() -> None:
    assert gui.PROGRESS_EVENTS == SPEC_PROGRESS_EVENTS


def test_page_ids_match_spec_29_12() -> None:
    assert gui.PAGE_IDS == SPEC_PAGE_IDS
    assert set(gui.PAGE_TITLES) == set(SPEC_PAGE_IDS)
    assert all(gui.PAGE_TITLES[p] for p in SPEC_PAGE_IDS)


# --------------------------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0, "0 bytes"), (999, "999 bytes"), (1024, "1 KB"), (1536, "2 KB"), (10 * 1024, "10 KB"),
    (1024 * 1024, "1 MB"), (5 * 1024 * 1024, "5 MB"), (1024 * 1024 * 1024, "1.0 GB"),
    (int(2.5 * (1 << 30)), "2.5 GB"), (-5, "0 bytes"),
])
def test_format_size(value: int, expected: str) -> None:
    assert gui.format_size(value) == expected


def test_format_size_rejects_garbage_quietly() -> None:
    assert gui.format_size(None) == "0 bytes"
    assert gui.format_size("not a number") == "0 bytes"


@pytest.mark.parametrize("value,expected", [(0, "0"), (1234567, "1,234,567"), (None, "0"), ("x", "0")])
def test_format_count(value: Any, expected: str) -> None:
    assert gui.format_count(value) == expected


def test_action_type_text_covers_every_spec_action_and_falls_back_for_unknown() -> None:
    for kind in ("builtin", "apt", "flatpak", "browser", "launcher", "recipe", "winget", "web",
                "winapps", "vm", "not_possible"):
        text = gui.action_type_text(kind)
        assert isinstance(text, str) and text
    assert gui.action_type_text("something-new") == "something-new"
    assert gui.action_type_text(None) == "Unknown"


# --------------------------------------------------------------------------------------------
# run_exit_ok: lindos-transfer's own exit-code contract (0/1/2/4), not robocopy's ">= 8" bitmask
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,expected", [
    (0, True), (None, True),
    (1, False), (2, False), (4, False), (7, False), (8, False), (127, False),
])
def test_run_exit_ok_matches_the_cli_exit_code_contract(code: Optional[int], expected: bool) -> None:
    assert gui.run_exit_ok(code) is expected


# --------------------------------------------------------------------------------------------
# parse_progress_line (SPEC-WINDOWS §29.7)
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("event", sorted(SPEC_PROGRESS_EVENTS))
def test_parse_progress_line_accepts_every_spec_event(event: str) -> None:
    line = json.dumps({"event": event, "item": "documents", "done_bytes": 10, "total_bytes": 100,
                       "path": "C:\\x", "message": "hi"})
    parsed = gui.parse_progress_line(line)
    assert parsed is not None and parsed["event"] == event


@pytest.mark.parametrize("line", [
    "", "   ", "not json at all", "Copying Documents (12 files, 3 MB)...",
    '{"event": "start"', "42", "[1, 2, 3]", '"just a string"',
    json.dumps({"item": "documents"}),                     # no "event" key
    json.dumps({"event": "not-a-real-event"}),             # unknown event name
    json.dumps({"event": "START"}),                        # case-sensitive
])
def test_parse_progress_line_rejects_everything_else(line: str) -> None:
    assert gui.parse_progress_line(line) is None


def test_parse_progress_line_strips_surrounding_whitespace_and_newline() -> None:
    parsed = gui.parse_progress_line('  {"event": "done", "message": "ok"}  \n')
    assert parsed == {"event": "done", "message": "ok"}


# --------------------------------------------------------------------------------------------
# TransferCli -- fake run/popen, never a real subprocess
# --------------------------------------------------------------------------------------------
class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Records every call; returns queued results or raises a queued exception, in order."""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self._results: List[Any] = []

    def queue(self, result: Any) -> None:
        self._results.append(result)

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> FakeCompleted:
        self.calls.append(list(argv))
        if not self._results:
            return FakeCompleted(0, "{}", "")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeStdout:
    """A line-iterable that also supports ``for line in proc.stdout`` like a real pipe."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]

    def __iter__(self):
        return iter(self._lines)


class FakePopenProc:
    def __init__(self, lines: Sequence[str], returncode: int = 0) -> None:
        self.stdout = FakeStdout(lines)
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


class FakePopen:
    def __init__(self, lines: Sequence[str] = (), returncode: int = 0, raise_exc: Optional[Exception] = None) -> None:
        self.calls: List[List[str]] = []
        self._lines = lines
        self._returncode = returncode
        self._raise = raise_exc

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> FakePopenProc:
        self.calls.append(list(argv))
        if self._raise is not None:
            raise self._raise
        return FakePopenProc(self._lines, self._returncode)


def make_cli(run: Optional[FakeRun] = None, popen: Optional[FakePopen] = None) -> Any:
    return gui.TransferCli("lindos-transfer", run=run or FakeRun(), popen=popen or FakePopen(),
                           which=lambda _name: "/usr/bin/lindos-transfer")


def test_cli_available_uses_which() -> None:
    cli = gui.TransferCli("lindos-transfer", which=lambda _n: "/usr/bin/lindos-transfer")
    assert cli.available() is True
    cli2 = gui.TransferCli("lindos-transfer", which=lambda _n: None)
    assert cli2.available() is False


def test_cli_sources_parses_and_fills_in_missing_keys() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"partitions": [{"device": "/dev/sda1"}]})))
    cli = make_cli(run)
    result = cli.sources()
    assert result["partitions"] == [{"device": "/dev/sda1"}]
    assert result["bundles"] == []
    assert run.calls == [["lindos-transfer", "sources", "--json"]]


def test_cli_sources_rejects_a_non_object_result() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, "[1, 2, 3]"))
    with pytest.raises(gui.CliError):
        make_cli(run).sources()


def test_cli_json_raises_on_nonzero_exit_with_the_first_error_line() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(2, "", "usage: lindos-transfer sources [--json]\nsome more detail\n"))
    with pytest.raises(gui.CliError) as excinfo:
        make_cli(run).sources()
    assert excinfo.value.code == 2
    assert "usage: lindos-transfer sources" in str(excinfo.value)


def test_cli_json_raises_when_binary_is_missing() -> None:
    run = FakeRun()
    run.queue(FileNotFoundError())
    with pytest.raises(gui.CliError) as excinfo:
        make_cli(run).sources()
    assert excinfo.value.code == 127


def test_cli_json_raises_on_timeout() -> None:
    run = FakeRun()
    run.queue(subprocess.TimeoutExpired(cmd="lindos-transfer", timeout=1))
    with pytest.raises(gui.CliError) as excinfo:
        make_cli(run).sources()
    assert excinfo.value.code == 124


def test_cli_json_raises_on_malformed_json() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, "{not json"))
    with pytest.raises(gui.CliError):
        make_cli(run).sources()


def test_cli_users_accepts_bare_list_and_envelope_and_coerces_strings() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps([{"name": "alice"}, "bob"])))
    users = make_cli(run).users("/media/x")
    assert users == [{"name": "alice"}, {"name": "bob"}]
    assert run.calls[-1] == ["lindos-transfer", "users", "--from", "/media/x", "--json"]

    run2 = FakeRun()
    run2.queue(FakeCompleted(0, json.dumps({"users": [{"name": "carol"}]})))
    assert make_cli(run2).users("/media/y") == [{"name": "carol"}]


def test_cli_users_rejects_a_non_list_result() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"users": "nope"})))
    with pytest.raises(gui.CliError):
        make_cli(run).users("/media/x")


def test_cli_plan_builds_the_full_argv_and_fills_defaults() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"items": [{"id": "documents"}]})))
    cli = make_cli(run)
    plan = cli.plan(path="/media/x", user="alice", only=["documents", "pictures"], exclude=["wifi"],
                    dest="/home/alice", firefox_passwords=True)
    assert plan["items"] == [{"id": "documents"}]
    assert plan["apps"] == [] and plan["skipped"] == [] and plan["warnings"] == []
    assert run.calls == [["lindos-transfer", "plan", "--from", "/media/x", "--user", "alice",
                          "--only", "documents,pictures", "--exclude", "wifi",
                          "--dest", "/home/alice", "--firefox-passwords", "--json"]]


def test_cli_plan_minimal_argv_without_optional_flags() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"items": []})))
    make_cli(run).plan(path="/media/x")
    assert run.calls == [["lindos-transfer", "plan", "--from", "/media/x", "--json"]]


def test_cli_plan_rejects_a_result_without_an_items_list() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"items": "nope"})))
    with pytest.raises(gui.CliError):
        make_cli(run).plan(path="/media/x")


def test_cli_apps_argv_and_envelope() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"apps": [{"windows_name": "Chrome"}]})))
    apps = make_cli(run).apps(path="/media/x", user="alice")
    assert apps == [{"windows_name": "Chrome"}]
    assert run.calls == [["lindos-transfer", "apps", "--from", "/media/x", "--user", "alice", "--json"]]


def test_cli_report_returns_none_on_exit_code_4() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(4, "", ""))
    assert make_cli(run).report() is None


def test_cli_report_parses_on_success_and_raises_otherwise() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, json.dumps({"totals": {"files": 3}})))
    assert make_cli(run).report() == {"totals": {"files": 3}}

    run2 = FakeRun()
    run2.queue(FakeCompleted(1, "", "boom"))
    with pytest.raises(gui.CliError):
        make_cli(run2).report()


def test_cli_report_does_not_append_json_flag_twice() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, "{}"))
    make_cli(run).report()
    assert run.calls == [["lindos-transfer", "report", "--json"]]


def test_cli_make_usb_kit_ok_and_failure() -> None:
    run = FakeRun()
    run.queue(FakeCompleted(0, "", ""))
    make_cli(run).make_usb_kit("/media/usb")
    assert run.calls == [["lindos-transfer", "make-usb-kit", "/media/usb"]]

    run2 = FakeRun()
    run2.queue(FakeCompleted(1, "", "no space left on device"))
    with pytest.raises(gui.CliError) as excinfo:
        make_cli(run2).make_usb_kit("/media/usb")
    assert "no space left" in str(excinfo.value)


def test_cli_stream_run_dispatches_events_and_raw_lines_and_returns_exit_code() -> None:
    lines = [
        json.dumps({"event": "start", "message": "2 item(s)"}),
        "some plain progress banner that is not JSON",
        json.dumps({"event": "item", "item": "documents", "message": "Documents"}),
        json.dumps({"event": "progress", "item": "documents", "done_bytes": 50, "total_bytes": 100}),
        json.dumps({"event": "done", "message": "done"}),
    ]
    popen = FakePopen(lines=lines, returncode=0)
    cli = make_cli(popen=popen)
    events: List[Dict[str, Any]] = []
    plain: List[str] = []
    code = cli.stream_run(plan_path="/tmp/plan.json", on_event=events.append, on_line=plain.append)
    assert code == 0
    assert [e["event"] for e in events] == ["start", "item", "progress", "done"]
    assert plain == ["some plain progress banner that is not JSON"]
    assert popen.calls == [["lindos-transfer", "run", "--plan", "/tmp/plan.json", "--yes", "--json-progress"]]


def test_cli_stream_run_dry_run_flag() -> None:
    popen = FakePopen(lines=[], returncode=0)
    make_cli(popen=popen).stream_run(plan_path="/tmp/plan.json", dry_run=True)
    assert popen.calls == [["lindos-transfer", "run", "--plan", "/tmp/plan.json", "--yes",
                           "--json-progress", "--dry-run"]]


def test_cli_stream_run_missing_binary_reports_and_returns_127() -> None:
    popen = FakePopen(raise_exc=FileNotFoundError())
    lines: List[str] = []
    code = make_cli(popen=popen).stream_run(plan_path="/tmp/plan.json", on_line=lines.append)
    assert code == 127
    assert lines and "not installed" in lines[0]


def test_cli_stream_install_apps_has_no_json_progress_flag_and_no_events() -> None:
    popen = FakePopen(lines=["Installing Google Chrome...", "Done."], returncode=0)
    lines: List[str] = []
    code = make_cli(popen=popen).stream_install_apps(plan_path="/tmp/plan.json", on_line=lines.append)
    assert code == 0
    assert lines == ["Installing Google Chrome...", "Done."]
    assert popen.calls == [["lindos-transfer", "install-apps", "--plan", "/tmp/plan.json", "--yes"]]
    assert "--json-progress" not in popen.calls[0]


# --------------------------------------------------------------------------------------------
# plan / app dict helpers
# --------------------------------------------------------------------------------------------
def sample_plan() -> Dict[str, Any]:
    return {
        "items": [
            {"id": "documents", "category": "documents", "label": "Documents", "files": 10, "bytes": 1000,
             "selected": True},
            {"id": "onedrive", "category": "onedrive", "label": "OneDrive (Personal)", "files": 5, "bytes": 500,
             "selected": False},
            {"id": "onedrive:2", "category": "onedrive", "label": "OneDrive (Work)", "files": 2, "bytes": 200,
             "selected": True},
        ],
        "apps": [
            {"windows_name": "Google Chrome", "selected": True, "chosen": 0,
             "actions": [{"type": "browser", "id": "chrome", "label": "Google Chrome for Linux"}]},
            {"windows_name": "Old Utility", "selected": False, "chosen": 0,
             "actions": [{"type": "not_possible", "id": "old-utility", "label": "No route"}]},
            {"windows_name": "No Route Yet", "selected": False, "actions": []},
        ],
        "warnings": [],
    }


def test_plan_items_and_item_label_fallback_to_category_label() -> None:
    plan = sample_plan()
    assert [it["id"] for it in gui.plan_items(plan)] == ["documents", "onedrive", "onedrive:2"]
    assert gui.item_label(plan["items"][0]) == "Documents"
    unlabelled = {"category": "wifi"}
    assert gui.item_label(unlabelled) == gui.LABELS["wifi"]


def test_plan_items_handles_a_missing_items_key() -> None:
    assert gui.plan_items({}) == []


def test_set_item_selected_toggles_by_id_and_reports_missing_ids() -> None:
    plan = sample_plan()
    assert gui.set_item_selected(plan, "onedrive", True) is True
    assert plan["items"][1]["selected"] is True
    assert gui.set_item_selected(plan, "does-not-exist", True) is False


def test_set_category_selected_toggles_every_item_of_that_category() -> None:
    plan = sample_plan()
    changed = gui.set_category_selected(plan, "onedrive", False)
    assert changed == 2
    assert all(not it["selected"] for it in plan["items"] if it["category"] == "onedrive")
    assert plan["items"][0]["selected"] is True     # documents untouched


def test_selected_totals_sums_only_ticked_items() -> None:
    plan = sample_plan()
    files, total_bytes = gui.selected_totals(plan)
    assert (files, total_bytes) == (12, 1200)      # documents (10,1000) + onedrive:2 (2,200)


def test_plan_apps_set_app_selected_and_chosen_bounds_checked() -> None:
    plan = sample_plan()
    assert gui.set_app_selected(plan, 1, True) is True
    assert plan["apps"][1]["selected"] is True
    assert gui.set_app_selected(plan, 99, True) is False
    assert gui.set_app_chosen(plan, 0, 0) is True
    assert gui.set_app_chosen(plan, 0, 5) is False          # out-of-range action index
    assert gui.set_app_chosen(plan, 2, 0) is False          # "No Route Yet" has no actions at all


def test_app_action_summary_uses_chosen_index_and_handles_no_actions() -> None:
    plan = sample_plan()
    assert gui.app_action_summary(plan["apps"][0]) == "Google Chrome for Linux - Installed as a Linux browser"
    assert gui.app_action_summary(plan["apps"][2]) == "No route known for this app yet."


def test_app_action_summary_falls_back_to_the_first_action_for_a_bad_chosen_index() -> None:
    app = {"chosen": 99, "actions": [{"type": "builtin", "id": "x", "label": "Already there"}]}
    assert gui.app_action_summary(app) == "Already there - Already included in Lindos"


def test_plan_has_selection() -> None:
    assert gui.plan_has_selection(None) is False
    assert gui.plan_has_selection({}) is False
    plan = sample_plan()
    assert gui.plan_has_selection(plan) is True         # documents + onedrive:2 selected
    for it in plan["items"]:
        it["selected"] = False
    assert gui.plan_has_selection(plan) is True         # Chrome is still selected among apps
    for app in plan["apps"]:
        app["selected"] = False
    assert gui.plan_has_selection(plan) is False


# --------------------------------------------------------------------------------------------
# WizardState -- the page state machine
# --------------------------------------------------------------------------------------------
def test_initial_state_is_the_welcome_page() -> None:
    state = gui.WizardState()
    assert state.page == "welcome"
    assert state.can_go_back() is False
    assert state.can_go_next() is True


def test_full_happy_path_through_every_page() -> None:
    state = gui.WizardState()
    assert state.go_next() is True and state.page == "source"          # welcome -> source

    assert state.can_go_next() is False                                 # no source chosen yet
    state.source_path = "/media/alice/OS"
    assert state.can_go_next() is True
    assert state.go_next() is True and state.page == "user"

    assert state.can_go_next() is False
    state.user_name = "alice"
    assert state.go_next() is True and state.page == "bring"

    assert state.can_go_next() is False                                 # plan not built yet
    state.plan = {"items": [], "apps": []}
    assert state.go_next() is True and state.page == "apps"

    assert state.can_go_next() is True                                  # a plan (even empty) is enough
    assert state.go_next() is True and state.page == "transfer"

    assert state.can_go_next() is False                                 # transfer not finished
    assert state.can_go_back() is True                                  # not running yet: back is fine
    state.transfer_running = True
    assert state.can_go_back() is False                                 # never back out mid-copy
    state.transfer_running = False
    state.transfer_done = True
    assert state.can_go_next() is True
    assert state.go_next() is True and state.page == "done"

    assert state.can_go_next() is False                                 # Done: Finish, not Next
    assert state.go_next() is False                                     # already the last page
    assert state.index == len(gui.PAGE_IDS) - 1


def test_go_next_refuses_when_the_current_page_is_not_answered() -> None:
    state = gui.WizardState()
    state.index = gui.PAGE_IDS.index("source")
    assert state.go_next() is False
    assert state.page == "source"


def test_go_back_refuses_at_the_first_page() -> None:
    state = gui.WizardState()
    assert state.go_back() is False
    assert state.page == "welcome"


def test_go_back_walks_back_through_answered_pages() -> None:
    state = gui.WizardState()
    state.source_path, state.user_name = "/media/x", "alice"
    state.plan = {"items": []}
    for _ in range(3):
        state.go_next()
    assert state.page == "bring"
    assert state.go_back() is True and state.page == "user"
    assert state.go_back() is True and state.page == "source"
    assert state.go_back() is True and state.page == "welcome"
    assert state.go_back() is False


def test_go_next_does_not_clear_an_already_built_plan_on_mere_revisit() -> None:
    """Regression: earlier drafts cleared ``plan`` on every Next from source/user, forcing a
    pointless CLI re-run just from clicking Back then Next again without changing anything."""
    state = gui.WizardState()
    state.source_path, state.user_name = "/media/x", "alice"
    state.plan = {"items": [{"id": "documents", "selected": True}]}
    state.go_next()                        # source -> user
    state.go_next()                        # user -> bring
    assert state.plan is not None
    state.go_back()                        # bring -> user
    state.go_next()                        # user -> bring again, unchanged
    assert state.plan is not None


def test_reset_for_new_source_clears_downstream_state() -> None:
    state = gui.WizardState()
    state.users = [{"name": "alice"}]
    state.user_name = "alice"
    state.plan = {"items": []}
    state.plan_path = "/tmp/plan.json"
    state.reset_for_new_source()
    assert state.users == [] and state.user_name == "" and state.plan is None and state.plan_path == ""


def test_selected_totals_and_app_count_helpers_on_state() -> None:
    state = gui.WizardState()
    assert state.selected_totals() == (0, 0)
    assert state.selected_app_count() == 0
    state.plan = sample_plan()
    assert state.selected_totals() == (12, 1200)
    assert state.selected_app_count() == 1


# --------------------------------------------------------------------------------------------
# argument parsing / run_app early-exit paths (no display needed: monkeypatched)
# --------------------------------------------------------------------------------------------
def test_build_arg_parser_defaults_and_from_flag() -> None:
    parser = gui.build_arg_parser()
    args = parser.parse_args([])
    assert args.from_path == "" and args.debug is False
    args2 = parser.parse_args(["--from", "/media/alice/OS", "--debug"])
    assert args2.from_path == "/media/alice/OS" and args2.debug is True


def test_run_app_returns_1_when_gtk_cannot_open_a_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui, "_gtk_init_ok", lambda: False)
    assert gui.run_app() == 1


def test_run_app_returns_1_when_lindos_transfer_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui, "_gtk_init_ok", lambda: True)
    cli = gui.TransferCli("lindos-transfer", which=lambda _n: None)
    assert cli.available() is False
    assert gui.run_app(cli=cli) == 1


def test_not_possible_summary_says_yet_only_when_the_note_does() -> None:
    """Anti-cheat games read 'not supported ... yet'; the Xbox app (Store licensing) never gets 'yet' (SPEC 0.1)."""
    game = {"actions": [{"type": "not_possible", "id": "valorant", "label": "VALORANT",
                         "note": "Not supported on Lindos yet - up to its publisher, no date. See lindos-game route"}]}
    xbox = {"actions": [{"type": "not_possible", "id": "xbox-app", "label": "Xbox app",
                         "note": "Not supported on Linux: Microsoft Store licensing."}]}
    bare = {"actions": [{"type": "not_possible", "id": "x", "label": "X"}]}
    assert gui.app_action_summary(game) == "VALORANT - Not supported on Lindos yet (see the game routes in Lindos Settings > Gaming)"
    for app in (xbox, bare):
        text = gui.app_action_summary(app)
        assert "yet" not in text.lower() and "Not supported on Linux" in text, text
    assert "cannot run" not in gui.action_type_text("not_possible").lower()
