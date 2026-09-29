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
from lindos_setup.plan import RunResult, Selections, StepResult, build_plan, load_accents

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
    kw = dict(selections=Selections(),
              accents=load_accents(os.path.join(SHARE, "accents.json")), modes=_modes(),
              browsers={"edge": {"name": "Microsoft Edge"}, "chrome": {"name": "Google Chrome"},
                        "firefox": {"name": "Mozilla Firefox"}},
              dry_run=True, first_run=True, wallpapers=[Selections().wallpaper],
              ram_total_mb=8192, live=core.LiveApplier(dry_run=True), executors_factory=lambda plan: {},
              install_steps={"browser": "done"},
              browser_states={"edge": "unavailable", "chrome": "installed", "firefox": "installed"})
    kw.update(over)
    return pages.PageContext(**kw)


# --------------------------------------------------------------------------- page metadata
def test_page_ids_order_and_step_metadata():
    _ensure_gi()
    from lindos_setup import pages
    assert pages.PAGE_ORDER == ["welcome", "mode", "browser", "personalize", "privacy",
                                "transfer", "summary", "apply", "done"]
    made = pages.make_pages()
    assert [p.id for p in made] == pages.PAGE_ORDER
    assert not hasattr(pages, "AppsPage"), "the installer installs the apps; the wizard has no apps page"
    assert {p.id for p in made if p.hero} == {"welcome", "apply", "done"}
    assert {p.id for p in made if p.step_counted} == {
        "mode", "browser", "personalize", "privacy", "transfer", "summary"}
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
class _Banner(_Rec):
    """Stand-in for widgets.InfoBanner that remembers its text and visibility."""

    made: List[Any] = []

    def __init__(self, text: str = "", icon_name: str = "", warn: bool = False) -> None:
        super().__init__()
        self.text = text
        self.warn = warn
        self.visible = True
        _Banner.made.append(self)

    def set_text(self, text: str) -> None:
        self.text = text

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)

    def show_all(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


def _last_shown(rec: Any) -> Any:
    """Whether the last show_all()/hide() a recorder saw was a show (None: never touched)."""
    for call in reversed(rec.calls):
        if call[0] in ("show_all", "hide"):
            return call[0] == "show_all"
    return None


def _use_banners(monkeypatch) -> List[Any]:
    from lindos_setup import pages
    _Banner.made = []
    monkeypatch.setattr(pages, "InfoBanner", _Banner)
    return _Banner.made


def test_done_page_keeps_the_wine_and_anti_cheat_reality_check(monkeypatch):
    """SPEC 0.1: the text that used to sit on the apps page moved here, whole."""
    _need_stub()
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    learn: List[Any] = []

    class Learn(_Rec):
        def __init__(self, summary: str, paragraphs: Any) -> None:
            super().__init__()
            learn.append((summary, list(paragraphs)))

    monkeypatch.setattr(pages, "LearnMore", Learn)
    pages.DonePage().build(_ctx(monkeypatch))
    text = " ".join(b.text for b in banners)
    for must in ("Wine", "Proton", "not a copy of Windows", "Valorant", "Fortnite", "do not run on any Linux"):
        assert must in text, must
    assert len(learn) == 1
    summary, paragraphs = learn[0]
    assert "Learn more" in summary
    joined = " ".join(paragraphs)
    for must in ("protondb.com", "areweanticheatyet.com", "Sober", "Minecraft", "Adobe"):
        assert must in joined, must


def test_no_page_still_carries_the_wine_text_except_done(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    for cls in (pages.WelcomePage, pages.ModePage, pages.BrowserPage, pages.PrivacyPage, pages.SummaryPage):
        banners = _use_banners(monkeypatch)
        cls().build(_ctx(monkeypatch))
        assert not any("Valorant" in b.text for b in banners), cls.__name__


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
    # the banner no longer talks about where updates or browsers are downloaded from
    for text in banners:
        assert "update" not in text.lower() and "download" not in text.lower() and "repositor" not in text.lower()


def test_done_page_keeps_the_anti_cheat_note(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    pages.DonePage().build(_ctx(monkeypatch))
    assert any("Valorant" in b.text and "Wine" in b.text for b in banners)


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
    assert seen[0] == seen[3]                            # three friendly lines repeat


def test_no_text_promises_downloads_installs_or_updates():
    """The wizard only saves choices now (the installer installed everything): none of its wording
    may still say it downloads, installs or updates anything."""
    _ensure_gi()
    from lindos_setup import pages
    for line in pages.APPLY_LINES:
        low = line.lower()
        assert "download" not in low and "few minutes" not in low and "turn off" not in low, line
    for page in pages.make_pages():
        if page.id in ("mode", "summary"):       # the only places that say what does NOT happen
            assert "nothing is downloaded" in page.subtitle.lower(), page.id
            continue
        low = (page.title + " " + page.subtitle).lower()
        assert "download" not in low and "install" not in low and "update" not in low, page.id
    src = _read(os.path.join(PY_DIR, "pages.py"))
    for gone in ("Downloaded from", "Downloads run", "during the Apply step", "needs internet",
                 "Check connection again", "Some installs are pending", "Get the apps you need"):
        assert gone not in src, gone


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
    assert len(win._counted) == 9
    win.show_page(0, animate=False)
    assert win.steps.position is None
    win.show_page(1, animate=False)
    assert win.steps.position == (1, 6)
    win.show_page(4, animate=False)                       # privacy
    assert win.steps.position == (4, 6)
    win.show_page(6, animate=False)                       # summary
    assert win.steps.position == (6, 6)
    win.show_page(7, animate=False)                       # apply
    assert win.steps.position is None
    win.show_page(8, animate=False)                       # done
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
    win.show_page(7, animate=False)                       # a running apply cannot be cancelled by a click
    win.show_page(8, animate=False)
    assert win.cancel_btn.args_of("set_visible") == [True, False, False]

    _app, _ctx_, first = _window(monkeypatch, allow_quit=False)
    first.cancel_btn = _Rec()
    first.show_page(1, animate=False)
    first.show_page(6, animate=False)
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
    ctx.plan = build_plan(Selections(mode="creator"))
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
    step = ctx.plan.get("apply-mode")
    assert step is not None
    page._on_batch_line(pages.parse_batch_line("[batch 2/2] apply-mode: apply-mode"))
    assert page.step_label.args_of("set_text") == [step.title]
    before = page.progress_model.value
    page._on_batch_line(pages.parse_batch_line("[batch 2/2] apply-mode: done"))
    assert page.progress_model.value >= before
    # a line for a step the plan does not know is harmless
    page._on_batch_line(pages.parse_batch_line("[batch 3/3] mystery: mystery"))
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
    page2._finished(RunResult([StepResult("apply-mode", ok=False, message="helper failed")]))
    assert page2.title_label.args_of("set_text") == ["Setup finished, with a few things left to do"]
    texts = " ".join(str(t) for t in page2.banner.args_of("set_text"))
    assert "apply-mode" in texts and "Lindos Settings" in texts
    assert "install" not in texts.lower() and "download" not in texts.lower()


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


# --------------------------------------------------------------------------- browser page (install-state driven)
def _browser_page(monkeypatch, states, **ctx_over):
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    ctx = _ctx(monkeypatch, browser_states=states, **ctx_over)
    page = pages.BrowserPage()
    page.build(ctx)
    return pages, ctx, page, banners


def test_browser_page_offers_only_browsers_that_are_on_this_pc(monkeypatch):
    _need_stub()
    _pages, ctx, page, banners = _browser_page(
        monkeypatch, {"edge": "unavailable", "chrome": "installed", "firefox": "installed"})
    assert page.group.order == ["chrome", "firefox"]          # Edge is not offered: it is not installed
    assert not hasattr(page, "recheck"), "no 'Check connection again': the page needs no network"
    page.on_enter(ctx)
    assert not banners[0].visible                                # nothing pending: no notice at all
    assert page.group.selected == ctx.selections.browser == "chrome"
    # Edge shows up once it really is installed
    _pages, _ctx2, page2, _b = _browser_page(
        monkeypatch, {"edge": "installed", "chrome": "installed", "firefox": "installed"})
    assert page2.group.order == ["edge", "chrome", "firefox"]


def test_browser_page_shows_a_pending_chrome_as_will_be_added_when_online(monkeypatch):
    _need_stub()
    pages, ctx, page, banners = _browser_page(
        monkeypatch, {"edge": "unavailable", "chrome": "pending", "firefox": "installed"},
        selections=Selections(browser="firefox"))
    assert page.group.order == ["chrome", "firefox"]
    assert pages.BROWSER_PENDING_HINT == "Will be added when you're online"
    page.on_enter(ctx)
    assert page.group.selected == "firefox"                     # Firefox stays preselected
    banner = banners[0]
    assert banner.visible
    assert "Google Chrome isn't on this PC yet" in banner.text and "Install now" in banner.text
    assert "Until then Firefox is your browser" in banner.text
    assert "becomes your default" not in banner.text            # not chosen: no promise about the default
    # choosing the pending browser stores it as the preference and says what happens next
    page._changed("chrome")
    assert ctx.selections.browser == "chrome"
    assert "Google Chrome becomes your default as soon as it's added" in banner.text
    page._changed("firefox")
    assert "becomes your default" not in banner.text


def test_browser_page_never_leaves_a_selection_that_is_not_a_card(monkeypatch):
    _need_stub()
    # Chrome was left out on purpose ("skipped") but the stored choice still says chrome
    _pages, ctx, page, _b = _browser_page(
        monkeypatch, {"edge": "unavailable", "chrome": "unavailable", "firefox": "installed"},
        selections=Selections(browser="chrome"))
    assert page.group.order == ["firefox"]
    page.on_enter(ctx)
    assert ctx.selections.browser == "firefox" and page.group.selected == "firefox"


def test_browser_page_leaving_needs_no_connectivity_probe(monkeypatch):
    _need_stub()
    _pages, ctx, page, _b = _browser_page(monkeypatch, {"chrome": "pending", "firefox": "installed"})
    assert page.on_leave(ctx, True) is True and page.on_leave(ctx, False) is True


# --------------------------------------------------------------------------- mode page (config only)
def test_mode_page_says_plainly_when_the_modes_extras_are_still_missing(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    modes = _modes()
    modes["work"].packages = ["thunderbird"]
    modes["work"].flatpaks = []
    modes["creator"].packages = []
    modes["creator"].flatpaks = ["com.usebottles.bottles"]
    for mode in ("everyday", "gaming", "lite"):
        modes[mode].packages, modes[mode].flatpaks = [], []
    ctx = _ctx(monkeypatch, modes=modes, install_steps={"mode_extras": "pending", "gaming": "failed"})
    page = pages.ModePage()
    page.build(ctx)
    shown: List[bool] = []
    page.extras_note = _Rec()
    page.on_enter(ctx)
    for mode, expect in (("everyday", False), ("work", True), ("creator", False), ("gaming", True)):
        page.extras_note = _Rec()
        page._changed(mode)
        assert page.extras_note.args_of("set_visible") == [expect], mode
        shown.append(expect)
    page.extras_note = _Rec()
    page._changed("work")
    note = page.extras_note.args_of("set_text")[0]
    assert "Lindos Settings › Apps" in note and "Install now" in note
    assert "download" not in note.lower()
    assert "nothing is downloaded" in pages.ModePage.subtitle
    # nothing pending: no note for any Mode
    ctx2 = _ctx(monkeypatch, modes=modes, install_steps={"mode_extras": "done", "gaming": "done"})
    page2 = pages.ModePage()
    page2.build(ctx2)
    for mode in planmodes():
        page2.extras_note = _Rec()
        page2._changed(mode)
        assert page2.extras_note.args_of("set_visible") == [False], mode


def planmodes():
    from lindos_setup.plan import MODE_IDS
    return MODE_IDS


# --------------------------------------------------------------------------- summary page
def test_summary_page_builds_an_install_free_plan_and_reports_a_pending_browser(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    ctx = _ctx(monkeypatch, selections=Selections(browser="chrome"),
               browser_states={"edge": "unavailable", "chrome": "pending", "firefox": "installed"})
    page = pages.SummaryPage()
    page.build(ctx)
    page.grid = _Rows()
    page.notes = _Rows()
    page.steps_label = _Rec()
    page.on_enter(ctx)
    assert ctx.plan is not None
    assert not {"install-browser", "install-packages", "install-flatpaks", "install-compat",
                "install-gaming"} & set(ctx.plan.actions())
    assert ctx.plan.get("set-default-browser").payload == {"browser": "chrome", "pending": True}
    assert any("isn't on this PC yet" in b.text and b.warn for b in banners)
    text = page.steps_label.args_of("set_text")[0]
    assert "password" in text and "administrator rights" not in text
    assert "nothing is downloaded" in page.subtitle and page.next_label == "Apply"


# --------------------------------------------------------------------------- done page (read-only recap)
class _Rows(_Rec):
    def __init__(self) -> None:
        super().__init__()
        self.children: List[Any] = []

    def get_children(self) -> List[Any]:
        return list(self.children)

    def remove(self, child: Any) -> None:
        self.children.remove(child)

    def pack_start(self, child: Any, *args: Any) -> None:
        self.children.append(child)


def test_done_page_recaps_what_the_installer_did_from_install_state(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    ctx = _ctx(monkeypatch, install_steps={"browser": "done", "drivers": "done", "compat": "pending",
                                           "updates": "pending"})
    ctx.window = _Rec()
    page = pages.DonePage()
    page.build(ctx)
    monkeypatch.setattr(pages, "label", lambda text, *a, **k: text)     # rows become plain strings
    page.recap, page.settings_btn = _Rec(), _Rec()
    page.installed_rows, page.installed_box = _Rows(), _Rec()
    waiting = [b for b in banners if b.warn][-1]
    page.waiting_banner = waiting
    page.on_enter(ctx)
    rows = page.installed_rows.children
    assert [r.split("  ", 1)[1].split(" — ")[0] for r in rows] == [
        "System updates", "Drivers and firmware", "Google Chrome", "Windows app support (Wine + Proton)"]
    assert rows[2].startswith("✓") and rows[0].startswith("•")
    assert _last_shown(page.installed_box) is True
    assert waiting.visible
    for must in ("System updates", "Windows app support", "Install now", "Update Manager"):
        assert must in waiting.text, must
    assert "Some installs are pending" not in " ".join(str(t) for t in page.recap.args_of("set_text"))
    # re-entering does not duplicate rows
    page.on_enter(ctx)
    assert len(page.installed_rows.children) == 4


def test_done_page_without_an_install_record_claims_nothing(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    banners = _use_banners(monkeypatch)
    ctx = _ctx(monkeypatch, install_steps={})
    ctx.window = _Rec()
    page = pages.DonePage()
    page.build(ctx)
    page.recap, page.settings_btn = _Rec(), _Rec()
    page.installed_rows, page.installed_box = _Rows(), _Rec()
    waiting = [b for b in banners if b.warn][-1]
    page.waiting_banner = waiting
    page.on_enter(ctx)
    assert page.installed_rows.children == []
    assert _last_shown(page.installed_box) is False
    assert not waiting.visible


def test_set_shown_reveals_the_children_of_a_no_show_all_container():
    """GTK: a widget built with set_no_show_all(True) is skipped by the window's show_all(), children
    included, so revealing it with set_visible() alone would show an empty box."""
    _need_stub()
    from lindos_setup import pages
    box = _Rec()
    pages.set_shown(box, True)
    assert [c[0] for c in box.calls] == ["set_no_show_all", "show_all", "set_no_show_all"]
    assert [c[1] for c in box.calls if c[0] == "set_no_show_all"] == [False, True]
    hidden = _Rec()
    pages.set_shown(hidden, False)
    assert [c[0] for c in hidden.calls] == ["hide"]


def test_composite_widgets_are_only_revealed_through_set_shown():
    src = _read(os.path.join(PY_DIR, "pages.py"))
    for name in ("self.banner", "self.waiting_banner", "self.installed_box"):
        assert name + ".set_visible(" not in src, name


def test_done_page_recap_line_names_a_pending_browser_honestly(monkeypatch):
    _need_stub()
    from lindos_setup import pages
    ctx = _ctx(monkeypatch, selections=Selections(browser="chrome"),
               browser_states={"chrome": "pending", "firefox": "installed"})
    assert pages.DonePage._browser_recap(ctx) == "Browser: Google Chrome (Firefox until it's added)"
    ctx.selections.browser = "firefox"
    assert pages.DonePage._browser_recap(ctx) == "Browser: Mozilla Firefox"


# --------------------------------------------------------------------------- start-up context
def _build_context(monkeypatch, *, first_run=True, installed=(), steps=None):
    import logging
    import socket
    _need_stub()
    from lindos_setup import app

    def no_network(*a, **k):
        raise AssertionError("the wizard must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(core, "browser_installed", lambda bid: bid in installed)
    monkeypatch.setattr(core, "install_steps", lambda: dict(steps or {}))
    ctx = app.build_context(dry_run=True, first_run=first_run, logger=logging.getLogger("t-context"))
    ctx.live.close()
    return ctx


def test_context_reads_install_state_and_preselects_firefox_while_chrome_is_pending(monkeypatch):
    ctx = _build_context(monkeypatch, steps={"browser": "pending", "compat": "done"})
    assert ctx.install_steps == {"browser": "pending", "compat": "done"}
    assert ctx.browser_states == {"edge": "unavailable", "chrome": "pending", "firefox": "installed"}
    assert ctx.selections.browser == "firefox"
    assert not hasattr(ctx, "catalog") and not hasattr(ctx, "online")


def test_context_keeps_chrome_selected_once_it_is_installed(monkeypatch):
    ctx = _build_context(monkeypatch, installed=("chrome",), steps={"browser": "done"})
    assert ctx.browser_states["chrome"] == "installed" and ctx.selections.browser == "chrome"


def test_context_falls_back_to_firefox_when_chrome_was_left_out(monkeypatch):
    for first_run in (True, False):
        ctx = _build_context(monkeypatch, first_run=first_run, steps={"browser": "skipped"})
        assert ctx.browser_states["chrome"] == "unavailable" and ctx.selections.browser == "firefox"


def test_reconfigure_keeps_a_stored_preference_for_a_pending_browser(monkeypatch):
    ctx = _build_context(monkeypatch, first_run=False, steps={"browser": "pending"})
    assert ctx.selections.browser == "chrome"           # the stored (default) preference survives
    ctx = _build_context(monkeypatch, first_run=True, steps={"browser": "pending"})
    assert ctx.selections.browser == "firefox"          # the first run never preselects what is missing
