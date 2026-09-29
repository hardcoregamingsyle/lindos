"""Tests for the Windows-11-style OOBE restyle (pages, widgets, window wiring, stylesheet).

Everything runs without real GTK: the pure helpers are plain functions, and the widget
classes are built under the repo's permissive ``gi`` stub (skipped when a real PyGObject is
present, because real widgets need a display).
"""
from __future__ import annotations

import ast
import os
import re
import sys
import types
from typing import Any, List

import pytest

from lindos_setup import core
from lindos_setup.plan import (
    Catalog, RunResult, Selections, StepResult, build_plan, load_accents, load_catalog,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.normpath(os.path.join(HERE, ".."))
LIB = os.path.join(PKG, "root", "usr", "lib", "lindos-setup")
PY_DIR = os.path.join(LIB, "lindos_setup")
CSS_PATH = os.path.join(LIB, "ui", "oobe.css")
SHARE = os.path.join(PKG, "root", "usr", "share", "lindos", "setup")

EXPECTED_WORDING = {
    "welcome": ("Let's get you set up", "Get started"),
    "mode": ("How will you use this PC?", "Next"),
    "browser": ("Choose your web browser", "Next"),
    "personalize": ("Make it yours", "Next"),
    "apps": ("Get the apps you need", "Next"),
    "privacy": ("Choose your privacy settings", "Accept"),
    "transfer": ("Bring your stuff from Windows", "Next"),
    "summary": ("Ready to set up your PC?", "Apply"),
    "apply": ("Just a moment…", "Next"),
    "done": ("All set", "Start using Lindos"),
}


def _ensure_gi() -> None:
    """Real PyGObject or the repo ``gi`` stub (tests/lindos_testsupport.py); else skip."""
    try:
        import gi  # noqa: F401
        return
    except ImportError:
        pass
    tests_dir = os.path.normpath(os.path.join(HERE, "..", "..", "..", "tests"))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        import lindos_testsupport  # noqa: F401  (installs the stub)
    except ImportError:
        pytest.skip("neither PyGObject nor the repo gi stub is available")


def _need_stub() -> None:
    """Skip tests that construct widgets when the real GTK (which needs a display) is in use."""
    _ensure_gi()
    import lindos_testsupport
    if not lindos_testsupport.gi_is_stub():
        pytest.skip("widget construction is only exercised under the gi stub (real GTK needs a display)")


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


class _Rec:
    """Records every method call made on it; any attribute is a callable no-op."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name,) + args)
            if name == "get_style_context":
                return _Rec()
            return None
        return call

    def args_of(self, name: str) -> List[Any]:
        return [c[1] if len(c) > 1 else None for c in self.calls if c[0] == name]


def _modes() -> dict:
    def mode(name: str, icon: str) -> Any:
        return types.SimpleNamespace(name=name, description="%s mode." % name, icon=icon, governor="schedutil",
                                     compositor="picom", zram_percent=25, pins=["firefox.desktop"])
    return {"everyday": mode("Everyday", "user-home"), "gaming": mode("Gaming", "applications-games"),
            "work": mode("Work", "x-office-document"), "creator": mode("Creator", "applications-graphics"),
            "lite": mode("Lite", "battery-good")}


def _ctx(monkeypatch: pytest.MonkeyPatch, **over: Any) -> Any:
    from lindos_setup import pages
    monkeypatch.setattr(core, "browser_installed", lambda bid: False)
    monkeypatch.setattr(core, "transfer_sources",
                        lambda *a, **k: {"available": False, "note": "", "partitions": [], "bundles": []})
    kw = dict(selections=Selections(), catalog=load_catalog(os.path.join(SHARE, "apps.json")),
              accents=load_accents(os.path.join(SHARE, "accents.json")), modes=_modes(),
              browsers={"edge": {"name": "Microsoft Edge"}, "chrome": {"name": "Google Chrome"},
                        "firefox": {"name": "Mozilla Firefox"}},
              online=True, dry_run=True, first_run=True, wallpapers=[Selections().wallpaper],
              ram_total_mb=8192, live=core.LiveApplier(dry_run=True), executors_factory=lambda plan: {})
    kw.update(over)
    return pages.PageContext(**kw)


# --------------------------------------------------------------------------- page metadata
def test_page_ids_order_and_step_metadata():
    _ensure_gi()
    from lindos_setup import pages
    assert pages.PAGE_ORDER == ["welcome", "mode", "browser", "personalize", "apps", "privacy",
                                "transfer", "summary", "apply", "done"]
    made = pages.make_pages()
    assert [p.id for p in made] == pages.PAGE_ORDER
    assert {p.id for p in made if p.hero} == {"welcome", "apply", "done"}
    assert {p.id for p in made if p.step_counted} == {
        "mode", "browser", "personalize", "apps", "privacy", "transfer", "summary"}
    # the entry point's --page choices must stay in sync with the wizard
    import importlib.util
    spec = importlib.util.spec_from_file_location("lindos_setup_main_style", os.path.join(LIB, "main.py"))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    assert main.PAGE_IDS == pages.PAGE_ORDER


def test_windows_oobe_wording():
    _ensure_gi()
    from lindos_setup import pages
    for page in pages.make_pages():
        title, next_label = EXPECTED_WORDING[page.id]
        assert page.title == title, page.id
        assert page.next_label == next_label, page.id
        assert len(page.subtitle) <= 260, "%s: one short subtitle, not a wall of text" % page.id


def test_first_and_last_pages_keep_navigation_rules():
    _ensure_gi()
    from lindos_setup import pages
    by_id = {p.id: p for p in pages.make_pages()}
    assert by_id["welcome"].back_visible is False
    assert by_id["apply"].back_visible is False       # never go back into a running install
    assert by_id["done"].back_visible is False
    assert all(p.next_visible for p in by_id.values())


# --------------------------------------------------------------------------- honesty (SPEC 0.1)
def test_apps_page_keeps_the_wine_and_anti_cheat_reality_check(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners: List[str] = []
    learn: List[Any] = []

    class Banner(_Rec):
        def __init__(self, text: str, icon_name: str = "", warn: bool = False) -> None:
            super().__init__()
            banners.append(text)

    class Learn(_Rec):
        def __init__(self, summary: str, paragraphs: Any) -> None:
            super().__init__()
            learn.append((summary, list(paragraphs)))

    monkeypatch.setattr(pages, "InfoBanner", Banner)
    monkeypatch.setattr(pages, "LearnMore", Learn)
    page = pages.AppsPage()
    page.build(_ctx(monkeypatch))
    text = " ".join(banners)
    for must in ("Wine", "Proton", "not a copy of Windows", "Valorant", "Fortnite", "do not run on any Linux"):
        assert must in text, must
    assert len(learn) == 1
    summary, paragraphs = learn[0]
    assert "Learn more" in summary
    joined = " ".join(paragraphs)
    for must in ("protondb.com", "areweanticheatyet.com", "Sober", "Minecraft"):
        assert must in joined, must


def test_apps_page_still_shows_the_reality_check_without_a_catalog(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners: List[str] = []

    class Banner(_Rec):
        def __init__(self, text: str, icon_name: str = "", warn: bool = False) -> None:
            super().__init__()
            banners.append(text)

    monkeypatch.setattr(pages, "InfoBanner", Banner)
    pages.AppsPage().build(_ctx(monkeypatch, catalog=Catalog([])))
    assert any("Valorant" in b and "Fortnite" in b for b in banners)


def test_privacy_page_keeps_nothing_is_sent_anywhere(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners: List[str] = []

    class Banner(_Rec):
        def __init__(self, text: str, icon_name: str = "", warn: bool = False) -> None:
            super().__init__()
            banners.append(text)

    monkeypatch.setattr(pages, "InfoBanner", Banner)
    page = pages.PrivacyPage()
    page.build(_ctx(monkeypatch))
    assert "collects nothing" in page.subtitle
    assert any("never sends usage data anywhere" in b for b in banners)


def test_done_page_keeps_the_anti_cheat_note(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners: List[str] = []

    class Banner(_Rec):
        def __init__(self, text: str, icon_name: str = "", warn: bool = False) -> None:
            super().__init__()
            banners.append(text)

    monkeypatch.setattr(pages, "InfoBanner", Banner)
    pages.DonePage().build(_ctx(monkeypatch))
    assert any("Valorant" in b and "Wine" in b for b in banners)


# --------------------------------------------------------------------------- pure helpers
def test_step_position_fraction_and_text():
    _ensure_gi()
    from lindos_setup import widgets
    counted = [False] + [True] * 7 + [False, False]
    assert widgets.step_position(counted, 0) is None
    assert widgets.step_position(counted, 1) == (1, 7)
    assert widgets.step_position(counted, 4) == (4, 7)
    assert widgets.step_position(counted, 7) == (7, 7)
    assert widgets.step_position(counted, 8) is None
    assert widgets.step_position(counted, 9) is None
    assert widgets.step_position(counted, -1) is None
    assert widgets.step_position(counted, 99) is None
    assert widgets.step_fraction((3, 7)) == pytest.approx(3 / 7.0)
    assert widgets.step_fraction((7, 7)) == 1.0
    assert widgets.step_fraction(None) == 0.0
    assert widgets.step_fraction((1, 0)) == 0.0
    assert widgets.step_text((3, 7)) == "Step 3 of 7"
    assert widgets.step_text(None) == ""


def test_column_width_is_760_and_only_shrinks_on_small_screens():
    _ensure_gi()
    from lindos_setup import widgets
    assert widgets.COLUMN_MAX_W == 760
    assert widgets.column_width(None) == 760
    assert widgets.column_width(0) == 760
    assert widgets.column_width(3840) == 760
    assert widgets.column_width(1024) == 760
    assert widgets.column_width(800) == 704            # 800 - 2 * 48
    assert widgets.column_width(640) == 544
    assert widgets.column_width(320) == widgets.COLUMN_MIN_W


def test_apply_lines_greet_then_cycle_without_repeating_hi():
    _ensure_gi()
    from lindos_setup import pages
    assert pages.apply_line(0) == "Hi"
    assert pages.apply_line(-3) == "Hi"
    seen = [pages.apply_line(t) for t in range(1, 8)]
    assert "Hi" not in seen
    assert seen[0] == "We're getting things ready for you"
    assert any("turn off your PC" in line for line in seen)
    assert seen[0] == seen[3]                            # three friendly lines repeat


def test_parse_batch_line_matches_the_helper_output():
    _ensure_gi()
    from lindos_setup.pages import BatchLine, parse_batch_line
    assert parse_batch_line("[batch 2/4] install-packages: install-packages") == \
        BatchLine(2, 4, "install-packages", "start")
    assert parse_batch_line("[batch 2/4] install-packages: done") == BatchLine(2, 4, "install-packages", "done")
    assert parse_batch_line("[batch 3/4] install-compat: FAILED (exit code 100)") == \
        BatchLine(3, 4, "install-compat", "failed")
    assert parse_batch_line("run-batch: 4 step(s)") is None
    assert parse_batch_line("  | [batch 1/2] x: done") is None       # subprocess output is indented
    assert parse_batch_line("") is None


def test_apply_progress_is_monotonic_and_credits_batch_steps():
    _ensure_gi()
    from lindos_setup.pages import ApplyProgress
    p = ApplyProgress()
    assert p.step_started(0, 6) == 0.0
    assert p.step_done(1, 6) == pytest.approx(1 / 6.0)
    p.step_started(1, 6)                                  # first system step: the whole batch runs in it
    p.batch_line("start")
    values = [p.value]
    for _ in range(3):
        values.append(p.batch_line("start"))
        values.append(p.batch_line("done"))
    assert values == sorted(values), "progress must never go backwards"
    assert p.value == pytest.approx(4 / 6.0)              # 1 finished before the batch + 3 batch steps
    for _ in range(10):
        p.batch_line("done")
    assert p.value == pytest.approx(5 / 6.0)              # never 100 % before the runner finishes
    p.step_done(2, 6)                                     # the runner catching up cannot lower it
    assert p.value == pytest.approx(5 / 6.0)
    assert p.finish() == 1.0


# --------------------------------------------------------------------------- stylesheet
def _css_text() -> str:
    text = _read(CSS_PATH)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_stylesheet_is_lf_balanced_and_uses_only_gtk3_css():
    raw = _read(CSS_PATH)
    assert "\r" not in raw and not raw.startswith("﻿")
    css = _css_text()
    assert css.count("{") == css.count("}")
    for bad in ("var(", "calc(", "display: flex", "display: grid", "grid-template", "gap:", "max-width",
                "max-height", "@media", "@supports", ":root", "!important"):
        assert bad not in css, "not GTK 3 CSS: %s" % bad
    assert not re.search(r"\d(vh|vw|rem)\b", css)
    defined = set(re.findall(r"@define-color\s+([\w-]+)", css))
    used = set(re.findall(r"@([A-Za-z_][\w-]*)", re.sub(r"@define-color", "", css)))
    assert used <= defined, "undefined colours: %s" % sorted(used - defined)


def _classes_used_by_the_ui() -> set:
    """String constants passed as CSS classes to ``add_class(widget, ...)`` / ``label(text, ...)``."""
    found = set()
    for name in ("pages.py", "widgets.py", "app.py"):
        tree = ast.parse(_read(os.path.join(PY_DIR, name)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("add_class", "label"):
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def test_every_css_class_the_ui_uses_is_styled():
    css = _css_text()
    hooks = {"summary-grid"}          # structural hook only, deliberately unstyled
    missing = []
    for cls in sorted(_classes_used_by_the_ui() - hooks):
        if not re.search(r"\.%s(?![\w-])" % re.escape(cls), css):
            missing.append(cls)
    assert not missing, "classes used in code but absent from oobe.css: %s" % missing


def test_accent_template_formats_and_targets_styled_classes():
    _ensure_gi()
    from lindos_setup import widgets
    out = widgets._ACCENT_TEMPLATE.format(hex="#0067C0")
    assert "@define-color lindos_accent #0067C0;" in out
    assert out.count("{") == out.count("}")
    css = _css_text()
    for cls in re.findall(r"\.oobe (\.[\w-]+)", out):
        assert re.search(r"%s(?![\w-])" % re.escape(cls), css), cls


def test_old_step_dots_are_gone():
    _ensure_gi()
    from lindos_setup import widgets
    assert not hasattr(widgets, "StepDots")
    assert ".dot" not in _css_text()
    assert hasattr(widgets, "StepIndicator")


# --------------------------------------------------------------------------- window wiring (gi stub)
def _window(monkeypatch, *, allow_quit: bool = False, **ctx_over: Any):
    from lindos_setup import app, pages
    ctx = _ctx(monkeypatch, **ctx_over)
    win = app.SetupWindow(ctx, pages.make_pages(), allow_quit=allow_quit)
    return app, ctx, win


def test_window_tracks_the_slim_step_indicator(monkeypatch):
    _need_stub()
    _app, _ctx_, win = _window(monkeypatch)
    assert len(win._counted) == 10
    win.show_page(0, animate=False)
    assert win.steps.position is None
    win.show_page(1, animate=False)
    assert win.steps.position == (1, 7)
    win.show_page(5, animate=False)
    assert win.steps.position == (5, 7)
    win.show_page(7, animate=False)
    assert win.steps.position == (7, 7)
    win.show_page(8, animate=False)                       # apply
    assert win.steps.position is None
    win.show_page(9, animate=False)                       # done
    assert win.steps.position is None
    assert win.steps.fraction == 0.0


def test_window_next_walks_the_pages_in_order(monkeypatch):
    _need_stub()
    _app, _ctx_, win = _window(monkeypatch)
    win.show_page(0, animate=False)
    win.go_next()
    assert win.page.id == "mode"
    win.go_next()
    assert win.page.id == "browser"
    win.go_back()
    assert win.page.id == "mode"


def test_cancel_is_offered_only_when_reconfiguring(monkeypatch):
    _need_stub()
    _app, _ctx_, win = _window(monkeypatch, allow_quit=True)
    win.cancel_btn = _Rec()
    win.show_page(1, animate=False)
    win.show_page(8, animate=False)                       # a running install cannot be cancelled by a click
    win.show_page(9, animate=False)
    assert win.cancel_btn.args_of("set_visible") == [True, False, False]

    _app, _ctx_, first = _window(monkeypatch, allow_quit=False)
    first.cancel_btn = _Rec()
    first.show_page(1, animate=False)
    first.show_page(7, animate=False)
    assert first.cancel_btn.args_of("set_visible") == [False, False]


def test_first_run_cannot_be_closed_but_reconfigure_can(monkeypatch):
    _need_stub()
    _app, _ctx_, first = _window(monkeypatch, allow_quit=False)
    assert first._on_delete() is True                     # blocked: nothing to escape into yet
    first.request_quit()
    assert first.exit_code == 0 and not first.finished
    _app, _ctx_, again = _window(monkeypatch, allow_quit=True)
    assert again._on_delete() is False
    destroyed = []
    again.destroy = lambda: destroyed.append(True)
    again.request_quit()
    assert destroyed == [True] and again.exit_code == 1


def test_enter_key_keeps_advancing_except_on_text_and_action_widgets(monkeypatch):
    _need_stub()
    app, _ctx_, win = _window(monkeypatch)
    gtk = app.Gtk

    def focus(widget: Any) -> bool:
        win.get_focus = lambda: widget
        return win._focus_handles_enter()

    assert focus(None) is False
    assert focus(win.next_btn) is False
    assert focus(gtk.Entry()) is True
    assert focus(gtk.Expander()) is True                  # "Learn more" toggles with Enter

    def button(*classes: str) -> Any:
        btn = gtk.Button()
        btn.get_style_context = lambda: types.SimpleNamespace(has_class=lambda c: c in classes)
        return btn

    for cls in ("btn-back", "btn-cancel", "btn-secondary", "btn-link"):
        assert focus(button(cls)) is True, cls
    assert focus(button("something-else")) is False


def test_window_still_releases_the_inhibitor_on_destroy(monkeypatch):
    _need_stub()
    from lindos_setup import app, pages
    released = []
    fake = types.SimpleNamespace(release=lambda: released.append(True))
    ctx = _ctx(monkeypatch)
    win = app.SetupWindow(ctx, pages.make_pages(), inhibitor=fake)
    monkeypatch.setattr(app.Gtk, "main_quit", lambda *a, **k: None, raising=False)
    win._on_destroy()
    assert released == [True]


# --------------------------------------------------------------------------- pages (gi stub)
def test_every_page_builds_and_enters_under_the_stub(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    ctx = _ctx(monkeypatch)
    for page in pages.make_pages():
        assert page.build(ctx) is not None, page.id
    for page in pages.make_pages():
        if page.id in ("transfer", "apply"):
            continue                                     # these start worker threads / timers
        page.build(ctx)
        page.on_enter(ctx)
    assert ctx.plan is not None                           # the summary page built the plan


def test_mode_page_lists_five_cards_and_records_the_choice(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    from lindos_setup.widgets import Card
    ctx = _ctx(monkeypatch)
    page = pages.ModePage()
    page.build(ctx)
    assert page.group.order == ["everyday", "gaming", "work", "creator", "lite"]
    assert all(isinstance(c, Card) for c in page.group.cards.values())
    page.on_enter(ctx)
    assert page.group.selected == ctx.selections.mode == "everyday"
    page._changed("gaming")
    assert ctx.selections.mode == "gaming"


def test_personalize_page_records_theme_accent_wallpaper_and_taskbar(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    ctx = _ctx(monkeypatch)
    ctx.window = _Rec()
    page = pages.PersonalizePage()
    page.build(ctx)
    assert set(page.theme_group.cards) == {"dark", "light"}
    assert set(page.align_group.cards) == {"center", "left"}
    assert len(page.swatches.cards) == 8
    page.on_enter(ctx)
    assert page.theme_group.selected == "dark" and page.align_group.selected == "center"
    page._theme_changed("light")
    assert ctx.selections.theme == "light" and not ctx.selections.dark
    assert ctx.window.args_of("set_light") == [True]
    page._theme_changed("dark")
    assert ctx.selections.dark and ctx.window.args_of("set_light") == [True, False]
    page._accent_changed("#0067c0")
    assert ctx.selections.accent == "#0067C0"
    page._align_changed("left")
    assert ctx.selections.taskbar_alignment == "left"
    wallpaper = Selections().wallpaper
    page._wallpaper_changed(wallpaper)
    assert ctx.selections.wallpaper == wallpaper
    # coming back to the page re-selects what was chosen
    page.on_enter(ctx)
    assert page.align_group.selected == "left"
    assert page.swatches.selected == "#0067C0"


def test_light_selection_preselects_the_light_card(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    sel = Selections()
    sel.set_theme("light")
    ctx = _ctx(monkeypatch, selections=sel)
    page = pages.PersonalizePage()
    page.build(ctx)
    page.on_enter(ctx)
    assert page.theme_group.selected == "light"


def _apply_page(monkeypatch):
    from lindos_setup import pages
    ctx = _ctx(monkeypatch)
    ctx.window = _Rec()
    sel = Selections(mode="creator")
    sel.apps = ctx.catalog.default_ids("creator")
    ctx.plan = build_plan(sel, ctx.catalog, online=True)
    page = pages.ApplyPage()
    page.build(ctx)
    page.title_label, page.subtitle_label, page.step_label = _Rec(), _Rec(), _Rec()
    page.percent_label, page.progress, page.spinner = _Rec(), _Rec(), _Rec()
    page.details_btn, page.details, page.banner = _Rec(), _Rec(), _Rec()
    return pages, ctx, page


def test_apply_page_hides_the_log_and_toggles_it(monkeypatch):
    _need_stub()
    _pages, _ctx_, page = _apply_page(monkeypatch)
    assert page._details_shown is False
    page._toggle_details(None)
    assert page._details_shown is True
    assert page.details.args_of("set_reveal_child") == [True]
    assert page.details_btn.args_of("set_label") == ["Hide details"]
    page._toggle_details(None)
    assert page.details.args_of("set_reveal_child") == [True, False]
    assert page.details_btn.args_of("set_label") == ["Hide details", "Show details"]


def test_apply_page_rotates_friendly_lines_until_it_finishes(monkeypatch):
    _need_stub()
    pages, ctx, page = _apply_page(monkeypatch)
    assert page._rotate() is True
    assert page._rotate() is True
    assert page.subtitle_label.args_of("set_text") == [pages.apply_line(1), pages.apply_line(2)]
    ctx.applied = True
    assert page._rotate() is False and page._timer_id == 0


def test_apply_page_shows_the_step_title_from_batch_lines(monkeypatch):
    _need_stub()
    pages, ctx, page = _apply_page(monkeypatch)
    step = ctx.plan.get("install-packages")
    assert step is not None
    page._on_batch_line(pages.parse_batch_line("[batch 1/2] install-packages: install-packages"))
    assert page.step_label.args_of("set_text") == [step.title]
    before = page.progress_model.value
    page._on_batch_line(pages.parse_batch_line("[batch 1/2] install-packages: done"))
    assert page.progress_model.value >= before
    # a line for a step the plan does not know is harmless
    page._on_batch_line(pages.parse_batch_line("[batch 2/2] mystery: mystery"))
    assert len(page.step_label.args_of("set_text")) == 1


def test_apply_page_finish_states(monkeypatch):
    _need_stub()
    pages, ctx, page = _apply_page(monkeypatch)
    page._finished(RunResult([StepResult("write-config", ok=True)]))
    assert ctx.applied is True and ctx.run_result is not None
    assert page.title_label.args_of("set_text") == ["Everything is in place"]
    assert ("stop",) in page.spinner.calls and ("hide",) in page.spinner.calls
    assert ctx.window.args_of("set_next_sensitive")[-1] is True
    assert page.progress_model.value == 1.0

    pages2, ctx2, page2 = _apply_page(monkeypatch)
    page2._finished(RunResult([StepResult("install-packages", ok=False, message="offline")]))
    assert page2.title_label.args_of("set_text") == ["Setup finished, with a few things left to do"]
    assert any("install-packages" in str(t) for t in page2.banner.args_of("set_text"))


def test_apply_page_is_a_spinner_hero(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    page = pages.ApplyPage()
    page.build(_ctx(monkeypatch))
    assert page.spinner is not None and page.hero and not page.step_counted
    assert page.subtitle == "Hi"


def test_done_page_hero_and_shortcuts(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    assert len(pages.SHORTCUTS) == 6
    assert ("Super", "Start menu") in pages.SHORTCUTS
    assert pages.DonePage.next_label == "Start using Lindos"
    page = pages.DonePage()
    page.build(_ctx(monkeypatch))
    assert page.hero and not page.step_counted


# --------------------------------------------------------------------------- hygiene
@pytest.mark.parametrize("path", [
    os.path.join(PY_DIR, "pages.py"), os.path.join(PY_DIR, "widgets.py"), os.path.join(PY_DIR, "app.py"),
    os.path.join(PY_DIR, "i18n.py"), CSS_PATH, os.path.join(HERE, "test_oobe_style.py"),
])
def test_touched_files_are_lf_only(path):
    raw = open(path, "rb").read()
    assert b"\r" not in raw, "%s must use LF line endings" % path
    assert not raw.startswith(b"\xef\xbb\xbf"), "%s must not have a BOM" % path


def test_n_marker_is_identity():
    from lindos_setup.i18n import N_
    assert N_("Make it yours") == "Make it yours"
